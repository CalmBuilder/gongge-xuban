"""
@Time       : 2026/10/03
@Author     : zhanglp8181
@File       : test_skill_feedback_model_audit.py
@CallChain  : 独立审核 → 原候选HTTP/真实双连接 → 原发布/撤回/权限与提交边界
@Description: 不替换发布器，以真实交错验证撤销、竞争、普通上下文和原子回滚。
"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier, Event

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, select

from app.agents.schema import AgentProfileUpdateRequest
from app.api.agents import update_agent
from app.api.auth import UserUpdateRequest, update_user
from app.api.skills import router
from app.db import get_session
from app.db.models import (
    AgentProfile,
    AgentSkillBranch,
    AgentSkillBranchVersion,
    BusinessRole,
    FeedbackMaterial,
    OrganizationUnit,
    PermissionDefinition,
    SkillEditCandidate,
    SkillVersion,
    Tool,
    User,
)
from app.feedback.materials import MaterialWithdrawRequest, withdraw_material
from app.security.auth import get_current_user
from app.skills.publication import validate_participant_references_for_publication
from app.sop_runtime import compile_legacy_skill_card
from tests.test_feedback_materials import feedback_engine as _feedback_engine
from tests.test_feedback_scope_versions_audit import _card
from tests.test_skill_feedback_candidates import (
    PREFIX,
    TENANT,
    _client,
    _open,
    _publish_body,
    _save,
    _setup,
)
from tests.test_sop_definition_compiler import _skill_card_content

audit_engine = _feedback_engine


def _snapshot(engine, agent_id, candidate_id):
    """独立连接回读活动头、全部不可变快照、分支历史、工具绑定与候选回执。"""
    with Session(engine) as db:
        branch = db.exec(select(AgentSkillBranch).where(
            AgentSkillBranch.agent_id == agent_id,
        )).one()
        candidate = db.get(SkillEditCandidate, candidate_id)
        return {
            'branch': (branch.head_version, deepcopy(branch.content_json),
                       deepcopy(branch.metadata_json), branch.status),
            'versions': sorted((row.id, row.scope_agent_id, row.version,
                                deepcopy(row.content_json), row.compiled_definition_checksum,
                                row.derived_from_version_id)
                               for row in db.exec(select(SkillVersion)).all()),
            'history': sorted((row.id, row.version, deepcopy(row.content_json))
                              for row in db.exec(select(AgentSkillBranchVersion)).all()),
            'tools': sorted((row.id, deepcopy(row.allowed_skills_json))
                            for row in db.exec(select(Tool)).all()),
            'candidate': (candidate.status, candidate.revision, candidate.generation,
                          deepcopy(candidate.publish_receipt_json)),
        }


def _marked_client(engine, operation):
    """以真实Session/连接身份标记同步HTTP事务，跨AnyIO线程仍能观察真实交错。"""
    app = FastAPI()
    app.include_router(router)

    def session():
        """标记该请求实际SQL连接和Session，不依赖发HTTP的调用线程局部变量。"""
        with Session(engine) as db:
            db.info['audit_operation'] = operation
            db.connection(execution_options={'audit_operation': operation})
            yield db

    def actor():
        """仅获取合成登录身份；当前授权校验仍由原生产发布服务负责。"""
        with Session(engine) as db:
            row = db.get(User, 'candidate-manager')
            db.expunge(row)
            return row

    app.dependency_overrides[get_session] = session
    app.dependency_overrides[get_current_user] = actor
    return TestClient(app)


def _withdraw(engine, material_id, owner_id, *, operation=None):
    """本人在另一真实连接走生产撤回提交，不以直接改status代替授权入口。"""
    with Session(engine) as db:
        if operation:
            db.info['audit_operation'] = operation
            db.connection(execution_options={'audit_operation': operation})
        return withdraw_material(db, material_id, MaterialWithdrawRequest(
            tenant_id=TENANT, expected_revision=1,
        ), db.get(User, owner_id))


@pytest.mark.parametrize('first', ['withdraw', 'publish'])
def test_actual_overlapping_withdraw_and_publish_have_one_legal_commit_order(audit_engine, first):
    """两操作实际重叠并持锁；撤回先提交拒绝发布，发布先提交保留合法历史。"""
    material, agent, owner = _setup(audit_engine)
    with _client(audit_engine) as client:
        candidate = _save(client, _open(client, material, agent))
    path = PREFIX + '/feedback-candidates/' + candidate['id'] + '/publish'
    body = _publish_body(candidate)
    before = _snapshot(audit_engine, agent, candidate['id'])
    locked, second_attempt, release = Event(), Event(), Event()

    def before_sql(conn, cursor, statement, parameters, context, executemany):
        """观察第二操作已抵达真实写SQL，不替换SQL或生产事务。"""
        text = statement.lower().lstrip()
        operation = context.execution_options.get('audit_operation')
        if first == 'withdraw' and operation == 'publish' and text.startswith('update users'):
            second_attempt.set()
        if first == 'publish' and operation == 'withdraw' and text.startswith('update message_feedback'):
            second_attempt.set()

    def after_sql(conn, cursor, statement, parameters, context, executemany):
        """撤回已写材料但未提交时暂停，让发布在另一连接真正开始。"""
        if (first == 'withdraw' and context.execution_options.get('audit_operation') == 'withdraw'
                and statement.lower().lstrip().startswith('update feedback_materials')):
            locked.set()
            assert release.wait(30), 'withdraw gate timed out'

    def after_flush(db, context):
        """发布全部写入已flush但未提交时暂停，旧头仍对另一连接不可见。"""
        if first == 'publish' and db.info.get('audit_operation') == 'publish':
            if any(isinstance(row, SkillEditCandidate) and row.status == 'published'
                   for row in db.dirty):
                locked.set()
                assert release.wait(30), 'publication gate timed out'

    def run_publish():
        """独立线程HTTP调用原生产发布入口，返回实际状态和回执。"""
        with _marked_client(audit_engine, 'publish') as client:
            response = client.post(path, json=body)
            return response.status_code, response.json()

    def run_withdraw():
        """独立线程执行本人真实撤回，提交仍由生产服务完成。"""
        return _withdraw(audit_engine, material, owner, operation='withdraw')

    event.listen(audit_engine, 'before_cursor_execute', before_sql)
    event.listen(audit_engine, 'after_cursor_execute', after_sql)
    event.listen(Session, 'after_flush', after_flush)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            lead = pool.submit(run_withdraw if first == 'withdraw' else run_publish)
            try:
                assert locked.wait(30), 'first operation never acquired real write gate'
                follower = pool.submit(run_publish if first == 'withdraw' else run_withdraw)
                assert second_attempt.wait(30), 'second operation never attempted real SQL'
            finally:
                release.set()
            lead_result, follower_result = lead.result(timeout=60), follower.result(timeout=60)
    finally:
        release.set()
        event.remove(audit_engine, 'before_cursor_execute', before_sql)
        event.remove(audit_engine, 'after_cursor_execute', after_sql)
        event.remove(Session, 'after_flush', after_flush)
    result = follower_result if first == 'withdraw' else lead_result
    with Session(audit_engine) as db:
        assert db.get(FeedbackMaterial, material).status == 'withdrawn'
    if first == 'withdraw':
        assert result[0] == 409
        assert _snapshot(audit_engine, agent, candidate['id']) == before
    else:
        assert result[0] == 200
        after = _snapshot(audit_engine, agent, candidate['id'])
        assert len(after['versions']) == len(before['versions']) + 1
        assert after['candidate'][0] == 'published'
        with _client(audit_engine) as client:
            repeated = client.post(path, json=body)
            assert repeated.status_code == 200
            assert repeated.json() == result[1]
            detail = client.get(path.removesuffix('/publish'), params={'tenant_id': TENANT})
            assert detail.json()['materials'] == [] and detail.json()['content'] is None


def test_two_real_managers_concurrently_publish_only_one_base_successor(audit_engine):
    """两个管理者各自形成候选后同时发布，恰好一个成功且仅新增一个准确快照。"""
    material, agent, _owner = _setup(audit_engine)
    with Session(audit_engine) as db:
        db.add(User(id='audit-other-manager', tenant_id=TENANT, username='audit-other',
                    role='admin', password_hash='synthetic'))
        db.commit()
    actors = ['candidate-manager', 'audit-other-manager']
    candidates = []
    for actor_id in actors:
        with _client(audit_engine, actor_id) as client:
            candidates.append(_save(client, _open(client, material, agent, actor_id)))
    before = _snapshot(audit_engine, agent, candidates[0]['id'])
    gate = Barrier(2)

    def publish(index):
        """各使用真实独立HTTP会话，在共同起点并发进入原发布事务。"""
        with _client(audit_engine, actors[index]) as client:
            gate.wait(timeout=30)
            return client.post(PREFIX + '/feedback-candidates/' + candidates[index]['id']
                               + '/publish', json=_publish_body(candidates[index]))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(publish, index) for index in range(2)]
        results = [future.result(timeout=60) for future in futures]
    assert sorted(result.status_code for result in results) == [200, 409]
    receipt = next(result.json() for result in results if result.status_code == 200)
    with Session(audit_engine) as db:
        versions = db.exec(select(SkillVersion)).all()
        assert len(versions) == len(before['versions']) + 1
        published = db.get(SkillVersion, receipt['skill_version_id'])
        branch = db.exec(select(AgentSkillBranch).where(AgentSkillBranch.agent_id == agent)).one()
        assert published.scope_agent_id == agent
        assert published.content_json == branch.content_json
        assert published.version == branch.head_version
        assert published.compiled_definition_checksum == receipt['definition_checksum']


@pytest.mark.parametrize('authority', ['actor', 'scope'])
def test_real_permission_aba_cannot_revalidate_old_candidate(audit_engine, authority):
    """真实账号/员工管理入口撤销再恢复后，旧候选读写发布仍失效且活动版不变。"""
    material, agent, owner = _setup(audit_engine)
    with _client(audit_engine) as client:
        candidate = _save(client, _open(client, material, agent))
        before = _snapshot(audit_engine, agent, candidate['id'])
        with Session(audit_engine) as writer:
            admin = writer.get(User, owner)
            if authority == 'actor':
                for role in ['member', 'admin']:
                    update_user('candidate-manager', UserUpdateRequest(tenant_id=TENANT, role=role),
                                current_user=admin, db=writer)
            else:
                for status in ['archived', 'active']:
                    update_agent(agent, AgentProfileUpdateRequest(tenant_id=TENANT, status=status),
                                 current_user=admin, db=writer)
        path = PREFIX + '/feedback-candidates/' + candidate['id']
        assert client.get(path, params={'tenant_id': TENANT}).status_code == 409
        assert client.put(path, json={'tenant_id': TENANT, 'expected_revision': candidate['revision'],
                                     'content': candidate['content']}).status_code == 409
        assert client.post(path + '/publish', json=_publish_body(candidate)).status_code == 409
        assert _snapshot(audit_engine, agent, candidate['id']) == before


def test_final_commit_fault_rolls_back_versions_bindings_head_and_receipt(audit_engine):
    """全部发布写已flush后注入提交失败，独立回读证明无任何部分发布，再合法重试。"""
    material, agent, _owner = _setup(audit_engine)
    with _client(audit_engine) as client:
        candidate = _save(client, _open(client, material, agent))
        with Session(audit_engine) as db:
            tool = db.get(Tool, 'candidate-quota-tool')
            tool.allowed_skills_json = []
            db.add(tool)
            db.commit()
        before = _snapshot(audit_engine, agent, candidate['id'])
        observed = Event()

        def fail_after_publication_flush(db, context):
            """仅在具体候选发布全部flush后故障注入，不替换任何领域写入函数。"""
            if any(isinstance(row, SkillEditCandidate) and row.id == candidate['id']
                   and row.status == 'published' for row in db.dirty):
                observed.set()
                raise SQLAlchemyError('SYNTHETIC_SAFE_COMMIT_FAULT')

        event.listen(Session, 'after_flush', fail_after_publication_flush)
        path = PREFIX + '/feedback-candidates/' + candidate['id'] + '/publish'
        try:
            failed = client.post(path, json=_publish_body(candidate))
        finally:
            event.remove(Session, 'after_flush', fail_after_publication_flush)
        assert observed.is_set(), 'fault never reached real publication flush'
        assert failed.status_code == 409
        assert 'SYNTHETIC_SAFE_COMMIT_FAULT' not in failed.text
        assert _snapshot(audit_engine, agent, candidate['id']) == before
        succeeded = client.post(path, json=_publish_body(candidate))
        assert succeeded.status_code == 200
        after = _snapshot(audit_engine, agent, candidate['id'])
        assert len(after['versions']) == len(before['versions']) + 1


@pytest.mark.parametrize('directory', ['role', 'organization', 'permission'])
def test_participant_final_validation_uses_current_rows_after_older_read_view(audit_engine, directory):
    """真实另一连接停用目录后，旧RR读视图也不能让最终发布接受旧active参与者。"""
    _setup(audit_engine)
    content = _skill_card_content()
    content['nodes'][3]['metadata'] = {'participant_policy': {
        'candidate_role_codes': ['audit.reviewer'], 'completion_mode': 'any',
        'participant_scope_resolver': 'explicit_org', 'participant_scope_org_unit_id': 'audit-org',
        'action_permissions': {'outcome:approved': 'audit.review.approve'},
    }}
    compiled = compile_legacy_skill_card(content)
    with Session(audit_engine) as db:
        db.add_all([
            BusinessRole(id='audit-role', tenant_id=TENANT, role_code='audit.reviewer', name='审核'),
            OrganizationUnit(id='audit-org', tenant_id=TENANT, code='audit-org', name='审核部门',
                             unit_type_code='department', tree_path='/audit-org', depth=1),
            PermissionDefinition(id='audit-permission', tenant_id=TENANT,
                                 permission_code='audit.review.approve', name='审核通过',
                                 category='business', resource='audit.review', action='approve'),
        ])
        db.commit()
    model, identifier, expected = {
        'role': (BusinessRole, 'audit-role', 'PARTICIPANT_ROLE_NOT_FOUND'),
        'organization': (OrganizationUnit, 'audit-org', 'PARTICIPANT_ORG_NOT_FOUND'),
        'permission': (PermissionDefinition, 'audit-permission', 'ACTION_PERMISSION_NOT_FOUND'),
    }[directory]
    with Session(audit_engine) as reader:
        assert reader.exec(select(model).where(model.id == identifier)).one().status == 'active'
        with Session(audit_engine) as writer:
            row = writer.get(model, identifier)
            row.status = 'inactive'
            writer.add(row)
            writer.commit()
        with pytest.raises(HTTPException) as caught:
            validate_participant_references_for_publication(reader, TENANT, compiled)
        assert caught.value.status_code == 422
        assert caught.value.detail['code'] == expected
        reader.rollback()
        with Session(audit_engine) as writer:
            row = writer.get(model, identifier)
            row.status = 'active'
            writer.add(row)
            writer.commit()
        validate_participant_references_for_publication(reader, TENANT, compiled)
        reader.rollback()


def test_reset_rotation_and_new_candidate_never_restore_old_ordinary_nonce(audit_engine):
    """关闭后旧nonce在切scope或新候选替换slot后永久失效，新scope普通创建合法。"""
    material, agent, _owner = _setup(audit_engine)
    blank = 'audit-blank-agent'
    with Session(audit_engine) as db:
        db.add(AgentProfile(id=blank, tenant_id=TENANT, name='审核空员工',
                            owner_user_id='candidate-manager'))
        db.commit()
    with _client(audit_engine) as client:
        candidate = _open(client, material, agent)
        path = PREFIX + '/feedback-candidates/' + candidate['id']
        first = client.post(path + '/reset', json={'tenant_id': TENANT,
            'expected_revision': candidate['revision'], 'skill_id': candidate['skill_id'],
            'agent_id': agent, 'request_key': 'audit-reset-1', 'discard_candidate': True})
        assert first.status_code == 200
        second = client.post(path + '/reset', json={'tenant_id': TENANT,
            'expected_revision': first.json()['revision'], 'agent_id': blank,
            'request_key': 'audit-reset-2', 'discard_candidate': True})
        assert second.status_code == 200 and second.json()['current_skill'] is None
        old_header = {'X-Skill-Editor-Context': first.json()['editor_context']}
        new_header = {'X-Skill-Editor-Context': second.json()['editor_context']}
        body = {'tenant_id': TENANT, 'status': 'published',
                'content': _card('audit-new', '1.0.0').model_copy(
                    update={'skill_id': 'audit-new-skill'},
                ).model_dump()}
        assert client.post(PREFIX, params={'agent_id': blank}, json=body,
                           headers=old_header).status_code == 409
        assert client.post(PREFIX, params={'agent_id': blank}, json=body).status_code == 409
        created = client.post(PREFIX, params={'agent_id': blank}, json=body, headers=new_header)
        assert created.status_code == 200
        assert created.json()['publication_scope_agent_id'] == blank
        replacement = _open(client, material, agent, 'audit-replacement')
        assert replacement['id'] != candidate['id']
        assert client.post(PREFIX + '/audit-new-skill/archive', params={'tenant_id': TENANT,
                           'agent_id': blank}, headers=new_header).status_code == 409
        with Session(audit_engine) as db:
            history = db.get(SkillEditCandidate, candidate['id'])
            assert history.editor_slot_key is None
            assert history.source_refs_json


def test_other_manager_real_update_makes_base_stale_without_freezing_normal_editor(audit_engine):
    """无反馈上下文的另一管理者正常更新成功，旧候选不能覆盖新base或继续读正文。"""
    material, agent, owner = _setup(audit_engine)
    with _client(audit_engine) as client:
        candidate = _save(client, _open(client, material, agent))
        with _client(audit_engine, owner) as normal:
            changed = normal.put(PREFIX + '/' + candidate['skill_id'], params={'agent_id': agent},
                                 json={'tenant_id': TENANT,
                                       'content': _card('audit-new-base', '2.5.0').model_dump()})
            assert changed.status_code == 200
        before = _snapshot(audit_engine, agent, candidate['id'])
        path = PREFIX + '/feedback-candidates/' + candidate['id']
        assert client.get(path, params={'tenant_id': TENANT}).status_code == 409
        assert client.post(path + '/publish', json=_publish_body(candidate)).status_code == 409
        assert _snapshot(audit_engine, agent, candidate['id']) == before

"""
@Time       : 2026/10/02
@Author     : zhanglp8181
@File       : test_skill_feedback_candidates.py
@CallChain  : 本人实际分享 → 当前Skill候选HTTP → 原发布/普通写入/真实独立连接
@Description: 候选隔离、来源失效、持久上下文与人工原子确认的正式双方言反例。
"""
from copy import deepcopy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.api.skills import router
from app.agents.branching import ensure_private_resource_binding
from app.db import get_session
from app.db.models import AgentSkillBranch, Message, MessageFeedback, SkillVersion, Tool, User
from app.feedback.materials import (
    MaterialPreviewRequest, MaterialShareRequest, MaterialWithdrawRequest,
    preview_material, share_material, withdraw_material,
)
from app.feedback.source import freeze_execution_source
from app.security.auth import get_current_user
from tests.test_feedback_materials import feedback_engine as _feedback_engine
from tests.test_feedback_scope_versions_audit import _card, _consume, _seed_scope

candidate_engine = _feedback_engine
PREFIX = '/api/enterprise/skills'
TENANT = 'scope-tenant'


def _setup(engine):
    """基础与等待执行均走原生产链，本人预览并分享原回复，再交另一个管理者。"""
    with Session(engine) as db:
        owner, agents = _seed_scope(db)
        tool = Tool(id='candidate-quota-tool', tenant_id=TENANT, name='expense.quota_query',
                    method='GET', url='http://127.0.0.1/synthetic-uncalled',
                    allowed_skills_json=[_card('base', '2.0.0').skill_id])
        db.add(tool)
        db.flush()
        ensure_private_resource_binding(db, TENANT, agents[1].id, 'tool', tool.id, 'active')
        db.commit()
        instance, _compiled, _skill, chat = _consume(db, owner, agents[1], 'feedback')
        turn_id = chat.id + '-turn'
        answer = Message(id='candidate-answer', tenant_id=TENANT, session_id=chat.id,
            role='assistant', content='合成待改进回复', metadata_json={
                'user_message_id': turn_id,
                'feedback_source': freeze_execution_source(db, chat, turn_id, instance.id),
            })
        db.add_all([answer, MessageFeedback(id='candidate-feedback', tenant_id=TENANT,
            session_id=chat.id, message_id=answer.id, user_id=owner.id, rating='down'),
            User(id='candidate-manager', tenant_id=TENANT, username='manager',
                 role='admin', password_hash='synthetic')])
        db.commit()
        preview = preview_material(db, 'candidate-feedback', MaterialPreviewRequest(
            tenant_id=TENANT, target_agent_id=agents[1].id,
            note='请修正等待说明，其他行为保持', request_key='source-preview'), owner)
        shared = share_material(db, preview.id, MaterialShareRequest(tenant_id=TENANT,
            expected_revision=preview.feedback_revision,
            material_checksum=preview.material_checksum), owner)
        return shared.id, agents[1].id, owner.id


def _client(engine, actor_id='candidate-manager'):
    """只替换登录和会话依赖，普通及候选HTTP均用原生产router和真实数据库。"""
    app = FastAPI()
    app.include_router(router)

    def session():
        """独立请求使用真实连接，事务由生产API完成。"""
        with Session(engine) as db:
            yield db

    def actor():
        """模拟已登录身份；授权代数和资源归属仍由生产当前查询核验。"""
        with Session(engine) as db:
            row = db.get(User, actor_id)
            db.expunge(row)
            return row

    app.dependency_overrides[get_session] = session
    app.dependency_overrides[get_current_user] = actor
    return TestClient(app)


def _open(client, material, agent, key='open'):
    """选分享材料创建服务端候选，客户端不能提供基础正文或版本身份。"""
    result = client.post(PREFIX + '/feedback-candidates', json={
        'tenant_id': TENANT, 'agent_id': agent, 'material_ids': [material], 'request_key': key})
    assert result.status_code == 200, result.text
    return result.json()


def _save(client, candidate):
    """只编辑现有SkillCard说明，模型/客户端业务版本不作为发布权威。"""
    content = deepcopy(candidate['content'])
    content['name'] = '人工修正候选'
    content['version'] = '999.0.0'
    result = client.put(PREFIX + '/feedback-candidates/' + candidate['id'], json={
        'tenant_id': TENANT, 'expected_revision': candidate['revision'], 'content': content,
        'confirm_base_difference': True})
    assert result.status_code == 200, result.text
    return result.json()


def _publish_body(candidate, key='confirm'):
    """人工确认绑定候选修订与原不可变基础。"""
    return {'tenant_id': TENANT, 'expected_revision': candidate['revision'],
        'expected_base_version_id': candidate['base_skill_version_id'],
        'expected_base_checksum': candidate['base_checksum'], 'request_key': key,
        'confirm': True, 'confirm_base_difference': True}


def test_candidate_save_refresh_is_isolated_and_confirm_uses_original_runtime(candidate_engine):
    """保存/刷新不动活动版；确认才落原发布，重复同参不增版本，变参409。"""
    material, agent, _owner = _setup(candidate_engine)
    with _client(candidate_engine) as client:
        candidate = _save(client, _open(client, material, agent))
        detail = client.get(PREFIX + '/feedback-candidates/' + candidate['id'],
                            params={'tenant_id': TENANT})
        assert detail.status_code == 200
        assert detail.json()['content']['name'] == '人工修正候选'
        with Session(candidate_engine) as db:
            branch = db.exec(select(AgentSkillBranch).where(AgentSkillBranch.agent_id == agent)).one()
            assert branch.content_json['name'] == '隔离来源-base'
            assert branch.head_version == '2.0.0'
            before = len(db.exec(select(SkillVersion)).all())
        path = PREFIX + '/feedback-candidates/' + candidate['id'] + '/publish'
        body = _publish_body(candidate)
        result = client.post(path, json=body)
        assert result.status_code == 200, result.text
        receipt = result.json()
        assert receipt['scope_agent_id'] == agent
        assert receipt['version'] != '999.0.0'
        assert client.post(path, json=body).json() == receipt
        assert client.post(path, json={**body, 'request_key': 'changed'}).status_code == 409
        with Session(candidate_engine) as db:
            branch = db.exec(select(AgentSkillBranch).where(AgentSkillBranch.agent_id == agent)).one()
            version = db.get(SkillVersion, receipt['skill_version_id'])
            assert version.content_json == branch.content_json
            assert version.scope_agent_id == agent
            assert version.derived_from_version_id == candidate['base_skill_version_id']
            assert version.compiled_definition_checksum == receipt['definition_checksum']
            assert len(db.exec(select(SkillVersion)).all()) == before + 1


@pytest.mark.parametrize('retarget', [False, True])
def test_legacy_put_cannot_drop_identity_or_retarget_scope(candidate_engine, retarget):
    """服务端已知编辑关联不因删字段、换员工或改正文被旧PUT洗掉。"""
    material, agent, _owner = _setup(candidate_engine)
    with _client(candidate_engine) as client:
        candidate = _open(client, material, agent)
        result = client.put(PREFIX + '/' + candidate['skill_id'],
            params={'agent_id': 'scope-tenant-bob' if retarget else agent},
            json={'tenant_id': TENANT, 'content': _card('laundered', '2.1.0').model_dump()})
        assert result.status_code == 409
        with Session(candidate_engine) as db:
            assert all(row.head_version == '2.0.0' for row in db.exec(select(AgentSkillBranch)).all())


def test_reject_and_reset_reject_late_old_write_but_allow_new_ordinary_context(candidate_engine):
    """拒绝后不释放旧无身份写；明确重载签发新nonce，切scope旧nonce失效。"""
    material, agent, _owner = _setup(candidate_engine)
    with _client(candidate_engine) as client:
        candidate = _open(client, material, agent)
        path = PREFIX + '/feedback-candidates/' + candidate['id']
        rejected = client.post(path + '/reject', json={'tenant_id': TENANT,
                                                       'expected_revision': candidate['revision']})
        assert rejected.status_code == 200, rejected.text
        old_body = {'tenant_id': TENANT, 'content': _card('ordinary', '2.1.0').model_dump()}
        old_path = PREFIX + '/' + candidate['skill_id']
        assert client.put(old_path, params={'agent_id': agent}, json=old_body).status_code == 409
        reset = client.post(path + '/reset', json={'tenant_id': TENANT,
            'expected_revision': rejected.json()['revision'], 'skill_id': candidate['skill_id'],
            'agent_id': agent, 'request_key': 'reset-1', 'discard_candidate': True})
        assert reset.status_code == 200, reset.text
        assert reset.json()['current_skill']['name'] == '隔离来源-base'
        assert client.put(old_path, params={'agent_id': agent}, json=old_body).status_code == 409
        headers = {'X-Skill-Editor-Context': reset.json()['editor_context']}
        assert client.put(old_path, params={'agent_id': 'scope-tenant-bob'},
                          json=old_body, headers=headers).status_code == 409
        assert client.put(old_path, params={'agent_id': agent}, json=old_body,
                          headers=headers).status_code == 200


def test_withdraw_invalidates_read_save_publish_but_reset_still_available(candidate_engine):
    """另一真实连接撤回后候选全文/新发布拒绝，操作者仍能明确丢弃并重载。"""
    material, agent, owner_id = _setup(candidate_engine)
    with _client(candidate_engine) as client:
        candidate = _save(client, _open(client, material, agent))
        with Session(candidate_engine) as writer:
            withdraw_material(writer, material, MaterialWithdrawRequest(tenant_id=TENANT,
                expected_revision=1), writer.get(User, owner_id))
        path = PREFIX + '/feedback-candidates/' + candidate['id']
        assert client.get(path, params={'tenant_id': TENANT}).status_code == 409
        assert client.post(path + '/publish', json=_publish_body(candidate)).status_code == 409
        assert client.post(path + '/reset', json={'tenant_id': TENANT,
            'expected_revision': candidate['revision'], 'agent_id': agent,
            'skill_id': candidate['skill_id'], 'request_key': 'reset-withdrawn',
            'discard_candidate': True}).status_code == 200


def test_published_receipt_survives_withdraw_without_reexposing_source(candidate_engine):
    """合法先发布再撤回保留最小历史回执，但旧材料不能再创建候选。"""
    material, agent, owner_id = _setup(candidate_engine)
    with _client(candidate_engine) as client:
        candidate = _save(client, _open(client, material, agent))
        path = PREFIX + '/feedback-candidates/' + candidate['id']
        body = _publish_body(candidate)
        result = client.post(path + '/publish', json=body)
        assert result.status_code == 200, result.text
        with Session(candidate_engine) as writer:
            withdraw_material(writer, material, MaterialWithdrawRequest(tenant_id=TENANT,
                expected_revision=1), writer.get(User, owner_id))
        assert client.post(path + '/publish', json=body).json() == result.json()
        detail = client.get(path, params={'tenant_id': TENANT})
        assert detail.status_code == 200
        assert detail.json()['materials'] == []
        assert detail.json()['content'] is None
        assert client.post(PREFIX + '/feedback-candidates', json={'tenant_id': TENANT,
            'agent_id': agent, 'material_ids': [material], 'request_key': 'new-invalid'}).status_code == 409


def test_authenticated_but_deleted_actor_cannot_write_with_stale_admin(candidate_engine):
    """认证依赖后另一连接删除账号，短事务必须拒绝过时admin而非降级信任。"""
    _material, agent, _owner = _setup(candidate_engine)
    with Session(candidate_engine) as db:
        stale = db.get(User, 'candidate-manager')
        db.expunge(stale)
    with _client(candidate_engine) as client:
        client.app.dependency_overrides[get_current_user] = lambda: stale
        with Session(candidate_engine) as writer:
            writer.delete(writer.get(User, stale.id))
            writer.commit()
        result = client.put(PREFIX + '/' + _card('base', '2.0.0').skill_id,
            params={'agent_id': agent}, json={'tenant_id': TENANT,
                'content': _card('stale-admin', '2.1.0').model_dump()})
        assert result.status_code == 403
        with Session(candidate_engine) as db:
            head = db.exec(select(AgentSkillBranch).where(AgentSkillBranch.agent_id == agent)).one()
            assert head.head_version == '2.0.0'


def test_reset_to_empty_scope_allows_ordinary_new_skill_and_keeps_scope_binding(candidate_engine):
    """显式进入新建普通编辑可在空员工scope创建，旧无身份/错scope仍拒绝。"""
    from app.api.agents import enterprise_router
    from app.db.models import AgentProfile

    material, agent, _owner = _setup(candidate_engine)
    with Session(candidate_engine) as db:
        db.add(AgentProfile(id='candidate-blank-agent', tenant_id=TENANT, name='空员工',
                           owner_user_id='candidate-manager'))
        db.commit()
    with _client(candidate_engine) as client:
        client.app.include_router(enterprise_router)
        candidate = _open(client, material, agent)
        reset = client.post(PREFIX + '/feedback-candidates/' + candidate['id'] + '/reset',
            json={'tenant_id': TENANT, 'expected_revision': candidate['revision'],
                  'agent_id': 'candidate-blank-agent', 'request_key': 'new-ordinary',
                  'discard_candidate': True})
        assert reset.status_code == 200, reset.text
        assert reset.json()['current_skill'] is None
        body = {'tenant_id': TENANT, 'content': _card('new', '1.0.0').model_copy(
            update={'skill_id': 'skill_new_ordinary'}).model_dump(), 'status': 'published'}
        assert client.post(PREFIX, params={'agent_id': 'candidate-blank-agent'}, json=body).status_code == 409
        headers = {'X-Skill-Editor-Context': reset.json()['editor_context']}
        assert client.post(PREFIX, params={'agent_id': agent}, json=body, headers=headers).status_code == 409
        result = client.post(PREFIX, params={'agent_id': 'candidate-blank-agent'},
                             json=body, headers=headers)
        assert result.status_code == 200, result.text
        assert result.json()['publication_scope_agent_id'] == 'candidate-blank-agent'


def test_employee_copy_with_skills_cannot_escape_open_editor_context(candidate_engine):
    """含Skill的原员工复制在创建任何新头之前拒绝受管上下文，blank创建保持。"""
    from app.api.agents import enterprise_router
    from app.db.models import AgentProfile

    material, agent, _owner = _setup(candidate_engine)
    with _client(candidate_engine) as client:
        client.app.include_router(enterprise_router)
        _open(client, material, agent)
        result = client.post('/api/enterprise/agents', json={'tenant_id': TENANT,
            'name': '复制逃逸', 'copy_from_agent_id': agent, 'source_mode': 'copy'})
        assert result.status_code == 409, result.text
        with Session(candidate_engine) as db:
            assert db.exec(select(AgentProfile).where(AgentProfile.name == '复制逃逸')).first() is None
        blank = client.post('/api/enterprise/agents', json={'tenant_id': TENANT,
            'name': '无技能空员工', 'source_mode': 'blank'})
        assert blank.status_code == 200, blank.text

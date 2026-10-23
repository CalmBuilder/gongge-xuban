"""
@Time       : 2026/10/03
@Author     : zhanglp8181
@File       : test_skill_feedback_rewrite.py
@CallChain  : 当前rewrite HTTP/SkillEditor → 真正LLMClient SDK边界 → 候选结算
@Description: 只替换供应商传输，验证每次发送/反思/迟到结果及无长事务；不是模型质量。
"""
import json
from types import SimpleNamespace

import pytest
from sqlmodel import Session

from app.db.models import ModelConfig, User
from app.api.auth import UserUpdateRequest, update_user
from app.feedback.materials import MaterialWithdrawRequest, withdraw_material
from tests.test_skill_feedback_candidates import PREFIX, TENANT, _client, _open, _setup
from tests.test_feedback_materials import feedback_engine as _feedback_engine
from tests.test_feedback_scope_versions_audit import _card

rewrite_engine = _feedback_engine


def _model(engine):
    """合成隔离模型配置，实际SDK由末端传输替身接收，不读真实密钥。"""
    with Session(engine) as db:
        db.add(ModelConfig(id='candidate-model', tenant_id=TENANT, name='synthetic',
            model='protocol-only', api_key_encrypted='synthetic', is_default=True))
        db.commit()


def _request(candidate, agent):
    """客户端正文故意伪造；服务器必须使用候选当前内容并保留受管来源。"""
    return {'tenant_id': TENANT, 'agent_id': agent,
        'current_skill': _card('client-forged', '999.0.0').model_dump(),
        'instruction': '只修正说明', 'model_config_id': 'candidate-model',
        'target_paths': ['all'], 'feedback_candidate_id': candidate['id'],
        'expected_candidate_revision': candidate['revision']}


@pytest.mark.parametrize('case', [
    'success', 'withdraw_pre_dispatch', 'withdraw_before_reflection',
    'withdraw_during_json_repair', 'actor_aba', 'empty_retry', 'json_repair', 'model_failure',
])
def test_actual_sdk_source_guard_and_candidate_settlement(rewrite_engine, monkeypatch, case):
    """构造前撤回应零外呼；首回包后撤回应阻反思/结算；合法改写只落候选。"""
    material, agent, owner_id = _setup(rewrite_engine)
    _model(rewrite_engine)
    observations = {'calls': 0, 'transactions': [], 'payloads': []}

    def withdraw():
        """供应商交错期间另一真实连接提交撤回；准备事务不能持锁外呼。"""
        with Session(rewrite_engine) as db:
            withdraw_material(db, material, MaterialWithdrawRequest(tenant_id=TENANT,
                expected_revision=1), db.get(User, owner_id))

    class Provider:
        """仅模拟OpenAI末端协议，原SkillEditor/JSON修复/反思/guard仍实际运行。"""
        def __init__(self, **_kwargs):
            """构造时可提前提交撤回，以验证首次实际SDK发送前的fresh守卫。"""
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
            if case == 'withdraw_pre_dispatch':
                withdraw()
            if case == 'actor_aba':
                with Session(rewrite_engine) as db:
                    admin = db.get(User, owner_id)
                    for role in ['member', 'admin']:
                        update_user('candidate-manager', UserUpdateRequest(tenant_id=TENANT, role=role),
                                    current_user=admin, db=db)

        def with_options(self, **_kwargs):
            """保留实际超时包装接口，不替换原LLMClient。"""
            return self

        def create(self, **kwargs):
            """记录SDK实发和连接占用，首响应可造成撤回后反思的真实交错。"""
            observations['calls'] += 1
            observations['transactions'].append(rewrite_engine.pool.checkedout())
            observations['payloads'].append(kwargs['messages'][-1]['content'])
            if case == 'model_failure':
                raise RuntimeError('SYNTHETIC_PRIVATE_PROVIDER_BODY')
            if observations['calls'] == 1 and case == 'withdraw_during_json_repair':
                withdraw()
                text = '{ invalid-json'
            elif observations['calls'] == 1 and case in {'empty_retry', 'json_repair'}:
                text = '' if case == 'empty_retry' else '{ invalid-json'
            elif observations['calls'] == 1 or (
                observations['calls'] == 2 and case in {'empty_retry', 'json_repair'}
            ):
                content = _card('model-result', '888.0.0').model_dump()
                if case == 'withdraw_before_reflection':
                    withdraw()
                text = json.dumps({'draft_skill': content, 'assistant_message': '完成说明改写'})
            else:
                text = json.dumps({'passed': True, 'summary': '协议对照', 'rubrics': []})
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=text), finish_reason='stop')],
                usage=None, id='synthetic-candidate-response')

    monkeypatch.setattr('app.llm.client.decrypt_secret', lambda _value: 'synthetic-only')
    monkeypatch.setattr('app.llm.client.OpenAI', Provider)
    with _client(rewrite_engine) as client:
        candidate = _open(client, material, agent)
        path = PREFIX + '/' + candidate['skill_id'] + '/rewrite'
        result = client.post(path, json=_request(candidate, agent))
        if case in {'success', 'empty_retry', 'json_repair'}:
            assert result.status_code == 200, result.text
            assert observations['calls'] == (2 if case == 'success' else 3)
            assert not any(observations['transactions'])
            assert 'client-forged' not in observations['payloads'][0]
            detail = client.get(PREFIX + '/feedback-candidates/' + candidate['id'],
                                params={'tenant_id': TENANT})
            assert detail.status_code == 200
            assert detail.json()['revision'] == candidate['revision'] + 1
            assert detail.json()['content']['name'] == '隔离来源-model-result'
        elif case == 'model_failure':
            assert result.status_code == 502
            assert result.json()['detail'] == 'FEEDBACK_MODEL_FAILED'
            assert 'SYNTHETIC_PRIVATE_PROVIDER_BODY' not in result.text
            detail = client.get(PREFIX + '/feedback-candidates/' + candidate['id'],
                                params={'tenant_id': TENANT})
            assert detail.json()['revision'] == candidate['revision']
            assert detail.json()['content'] == candidate['content']
        else:
            assert result.status_code in {403, 409}
            assert observations['calls'] == (0 if case in {'withdraw_pre_dispatch', 'actor_aba'} else 1)
            assert '合成待改进回复' not in result.text

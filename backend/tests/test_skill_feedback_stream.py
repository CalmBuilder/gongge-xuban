"""
@Time       : 2026/10/03
@Author     : zhanglp8181
@File       : test_skill_feedback_stream.py
@CallChain  : 原rewrite/jobs与续接HTTP → 原后台编辑器 → 真实SDK协议边界/候选结算
@Description: 合成供应商传输与队列调度，双方言验证实际流任务、缓存撤销和普通旁路。
"""

import json
from types import SimpleNamespace

import pytest
from sqlmodel import Session

from app.api import skills
from app.db.models import User
from app.feedback.materials import MaterialWithdrawRequest, withdraw_material
from app.skills.stream_jobs import SkillStreamJobStore
from tests.test_feedback_materials import feedback_engine as _feedback_engine
from tests.test_feedback_scope_versions_audit import _card
from tests.test_skill_feedback_candidates import PREFIX, TENANT, _client, _open, _setup
from tests.test_skill_feedback_rewrite import _model, _request

stream_engine = _feedback_engine


@pytest.mark.parametrize('case', [
    'success', 'withdraw_pre_dispatch', 'withdraw_before_reflection',
    'withdraw_replay', 'cancel_pre_dispatch',
])
def test_actual_feedback_stream_job_rechecks_dispatch_settlement_and_cache(
    stream_engine, monkeypatch, case,
):
    """实际后台链结算候选；撤回/取消阻止发送，旧生成流和刷新缓存不得重放来源。"""
    material, agent, owner = _setup(stream_engine)
    _model(stream_engine)
    store = SkillStreamJobStore()
    pending = []
    observations = {'calls': 0, 'transactions': []}

    def withdraw():
        """另一真实连接在SDK期间或完成后撤回，使用本人实际生产授权入口。"""
        with Session(stream_engine) as db:
            withdraw_material(db, material, MaterialWithdrawRequest(
                tenant_id=TENANT, expected_revision=1,
            ), db.get(User, owner))

    def enqueue(name, function, *args, **kwargs):
        """只延迟队列调度供提交交错；稍后仍调用原后台函数且不替换业务逻辑。"""
        pending.append((function, args))

    class Provider:
        """末端供应商协议替身，真实LLMClient流式、修复、反思和派发守卫保持。"""

        def __init__(self, **kwargs):
            """构造前可撤回，让真实SDK发前守卫拦截，而非替换编辑器结果。"""
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
            if case == 'withdraw_pre_dispatch':
                withdraw()

        def with_options(self, **kwargs):
            """保留原供应商超时包装协议。"""
            return self

        def create(self, **kwargs):
            """记录外呼连接占用，返回真实SDK形状并可在首回包后撤回。"""
            observations['calls'] += 1
            observations['transactions'].append(stream_engine.pool.checkedout())
            if observations['calls'] == 1:
                text = json.dumps({'draft_skill': _card('stream-result', '99.0.0').model_dump(),
                                   'assistant_message': '合成受管改写结果'})
                if case == 'withdraw_before_reflection':
                    withdraw()
            else:
                text = json.dumps({'passed': True, 'summary': '协议对照', 'rubrics': []})
            if kwargs.get('stream'):
                return iter([SimpleNamespace(choices=[SimpleNamespace(
                    delta=SimpleNamespace(content=text), finish_reason='stop',
                )], usage=None, id='synthetic-feedback-stream')])
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=text), finish_reason='stop',
            )], usage=None, id='synthetic-feedback-reflection')

    monkeypatch.setattr(skills, 'stream_jobs', store)
    monkeypatch.setattr(skills, 'get_session_engine', lambda: stream_engine)
    monkeypatch.setattr(skills, 'enqueue_async_job', enqueue)
    monkeypatch.setattr('app.llm.client.OpenAI', Provider)
    monkeypatch.setattr('app.llm.client.decrypt_secret', lambda _: 'synthetic-only')
    with _client(stream_engine) as client:
        candidate = _open(client, material, agent)
        result = client.post(PREFIX + '/' + candidate['skill_id'] + '/rewrite/jobs',
                             json=_request(candidate, agent))
        assert result.status_code == 200, result.text
        job_id = result.json()['job_id']
        if case == 'cancel_pre_dispatch':
            assert client.post(PREFIX + '/jobs/' + job_id + '/cancel').status_code == 200
        assert len(pending) == 1
        function, args = pending[0]
        function(*args)
        detail = client.get(PREFIX + '/feedback-candidates/' + candidate['id'],
                            params={'tenant_id': TENANT})
        job = store.get(job_id)
        if case in {'success', 'withdraw_replay'}:
            assert job.status == 'succeeded', job.error
            assert detail.status_code == 200
            assert detail.json()['revision'] == candidate['revision'] + 1
            assert detail.json()['content']['name'] == '隔离来源-stream-result'
            assert observations['calls'] == 2
            assert not any(observations['transactions'])
            cached = client.get(PREFIX + '/jobs/' + job_id + '/stream')
            assert cached.status_code == 200
            assert '隔离来源-stream-result' in cached.text
            if case == 'withdraw_replay':
                old_iterator = skills._stream_skill_job(job_id)
                next(old_iterator)
                withdraw()
                metadata = client.get(PREFIX + '/jobs/' + job_id)
                replay = client.get(PREFIX + '/jobs/' + job_id + '/stream')
                assert metadata.status_code in {403, 409}
                assert replay.status_code in {403, 409}
                remaining = ''.join(old_iterator)
                assert '隔离来源-stream-result' not in remaining
                assert '合成受管改写结果' not in remaining
                assert all('draft_skill' not in event.data for event in store.get(job_id).events)
        else:
            assert job.status == 'failed'
            assert observations['calls'] == (1 if case == 'withdraw_before_reflection' else 0)
            assert all('draft_skill' not in event.data for event in job.events)
            assert '合成受管改写结果' not in (job.error or '')


def test_dropping_feedback_identity_cannot_call_ordinary_model(stream_engine, monkeypatch):
    """已知服务器候选上下文不可删除ID洗成普通改写，拒绝先于任何SDK构造。"""
    material, agent, owner = _setup(stream_engine)
    _model(stream_engine)
    attempts = []

    def forbidden_provider(**kwargs):
        """零外呼断言；失败时仍不访问真实供应商或真实密钥。"""
        attempts.append(True)
        raise AssertionError('provider construction must follow editor authority')

    monkeypatch.setattr('app.llm.client.OpenAI', forbidden_provider)
    monkeypatch.setattr('app.llm.client.decrypt_secret', lambda _: 'synthetic-only')
    with _client(stream_engine) as client:
        candidate = _open(client, material, agent)
        body = _request(candidate, agent)
        body.pop('feedback_candidate_id')
        body.pop('expected_candidate_revision')
        for suffix in ['/rewrite', '/rewrite/jobs', '/rewrite/stream']:
            result = client.post(PREFIX + '/' + candidate['skill_id'] + suffix, json=body)
            assert result.status_code == 409, result.text
        assert attempts == []

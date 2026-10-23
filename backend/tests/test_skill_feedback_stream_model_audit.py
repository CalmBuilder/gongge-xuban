"""
@Time       : 2026/10/03
@Author     : zhanglp8181
@File       : test_skill_feedback_stream_model_audit.py
@CallChain  : 独立审核 → 原rewrite job/worker/SDK传输 → 提交与绑定窗口/取消/安全缓存
@Description: 只协调真实时序与末端供应商协议，不替换结算、授权或发布逻辑。
"""

from concurrent.futures import ThreadPoolExecutor
import json
from threading import Event
from types import SimpleNamespace

from sqlmodel import Session, select

from app.api import skills
from app.db.models import AgentSkillBranch, SkillEditCandidate, SkillVersion
from app.skills.stream_jobs import SkillStreamJobStore
from tests.test_feedback_materials import feedback_engine as _feedback_engine
from tests.test_feedback_scope_versions_audit import _card
from tests.test_skill_feedback_candidates import PREFIX, _client, _open, _setup
from tests.test_skill_feedback_rewrite import _model, _request

stream_audit_engine = _feedback_engine
RAW_PROVIDER_MARKER = 'SYNTHETIC_RAW_PROVIDER_PRIVATE_FAILURE'


def _head(engine, agent):
    """独立只读活动头与不可变版本集合，候选模型结果不能产生任何活动发布。"""
    with Session(engine) as db:
        branch = db.exec(select(AgentSkillBranch).where(AgentSkillBranch.agent_id == agent)).one()
        return branch.head_version, branch.content_json, sorted(
            (row.id, row.version, row.compiled_definition_checksum)
            for row in db.exec(select(SkillVersion)).all()
        )


def _install_protocol(engine, monkeypatch, *, case='success', entered=None, release=None):
    """只接隔离队列调度、真正末端SDK协议和观察记录，生产worker/guard保持。"""
    store, pending = SkillStreamJobStore(), []
    observations = {'calls': 0, 'transactions': []}

    def enqueue(name, function, *args, **kwargs):
        """延迟真实worker的开始，测试线程随后按原参数执行原函数。"""
        pending.append((function, args))

    class Provider:
        """OpenAI末端协议替身，保留原流式、修复、反思和每次发送守卫。"""

        def __init__(self, **kwargs):
            """只构造合成供应商接口，实际客户端仍由生产LLMClient创建。"""
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def with_options(self, **kwargs):
            """保留生产SDK超时选项调用协议。"""
            return self

        def create(self, **kwargs):
            """可在实际首派发后暂停或抛含合成私密标记的原供应商错误。"""
            observations['calls'] += 1
            observations['transactions'].append(engine.pool.checkedout())
            if case == 'provider_failure':
                raise RuntimeError(RAW_PROVIDER_MARKER)
            if observations['calls'] == 1:
                if entered is not None:
                    entered.set()
                    assert release.wait(30), 'provider gate timed out'
                text = json.dumps({'draft_skill': _card('audit-stream-result', '99.0.0').model_dump(),
                                   'assistant_message': '审核受管流式改写结果'})
            else:
                text = json.dumps({'passed': True, 'summary': '协议对照', 'rubrics': []})
            if kwargs.get('stream'):
                return iter([SimpleNamespace(choices=[SimpleNamespace(
                    delta=SimpleNamespace(content=text), finish_reason='stop',
                )], usage=None, id='synthetic-audit-stream')])
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text),
                finish_reason='stop')], usage=None, id='synthetic-audit-reflection')

    monkeypatch.setattr(skills, 'stream_jobs', store)
    monkeypatch.setattr(skills, 'get_session_engine', lambda: engine)
    monkeypatch.setattr(skills, 'enqueue_async_job', enqueue)
    monkeypatch.setattr('app.llm.client.OpenAI', Provider)
    monkeypatch.setattr('app.llm.client.decrypt_secret', lambda _: 'synthetic-only')
    return store, pending, observations


def test_committed_own_settlement_before_job_fence_bind_keeps_fresh_status_legal(
    stream_audit_engine, monkeypatch,
):
    """候选已合法结算但job尚未绑定新revision时，真实状态读取不能误失效该生成。"""
    material, agent, _owner = _setup(stream_audit_engine)
    _model(stream_audit_engine)
    store, pending, observations = _install_protocol(stream_audit_engine, monkeypatch)
    entered, release = Event(), Event()
    with _client(stream_audit_engine) as client:
        candidate = _open(client, material, agent)
        before = _head(stream_audit_engine, agent)
        original_bind = store.bind_feedback

        def observe_bind(job_id, fence):
            """只在实际结算commit之后、原job新fence绑定之前观察并暂停时序。"""
            if fence.revision > candidate['revision']:
                with Session(stream_audit_engine) as reader:
                    row = reader.get(SkillEditCandidate, candidate['id'])
                    assert row.status == 'ready'
                    assert row.revision == fence.revision
                    assert row.generation == fence.generation
                    assert row.candidate_json['name'] == '隔离来源-audit-stream-result'
                entered.set()
                assert release.wait(30), 'binding gate timed out'
            original_bind(job_id, fence)

        monkeypatch.setattr(store, 'bind_feedback', observe_bind)
        started = client.post(PREFIX + '/' + candidate['skill_id'] + '/rewrite/jobs',
                              json=_request(candidate, agent))
        assert started.status_code == 200
        job_id = started.json()['job_id']
        assert len(pending) == 1
        function, args = pending[0]
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(function, *args)
            try:
                assert entered.wait(30), 'worker never reached committed settlement window'
                metadata = client.get(PREFIX + '/jobs/' + job_id)
            finally:
                release.set()
            future.result(timeout=60)
        assert metadata.status_code == 200, metadata.text
        assert store.get(job_id).status == 'succeeded'
        assert observations['calls'] == 2
        assert not any(observations['transactions'])
        cached = client.get(PREFIX + '/jobs/' + job_id + '/stream')
        assert cached.status_code == 200
        assert '隔离来源-audit-stream-result' in cached.text
        assert _head(stream_audit_engine, agent) == before


def test_actual_inflight_cancel_commits_generation_before_late_provider_result(
    stream_audit_engine, monkeypatch,
):
    """真实SDK已外呼时另一HTTP取消先提交，迟到结果不能写候选或重放缓存。"""
    material, agent, _owner = _setup(stream_audit_engine)
    _model(stream_audit_engine)
    entered, release = Event(), Event()
    store, pending, observations = _install_protocol(stream_audit_engine, monkeypatch,
        case='success', entered=entered, release=release)
    with _client(stream_audit_engine) as client:
        candidate = _open(client, material, agent)
        before = _head(stream_audit_engine, agent)
        result = client.post(PREFIX + '/' + candidate['skill_id'] + '/rewrite/jobs',
                             json=_request(candidate, agent))
        assert result.status_code == 200
        job_id = result.json()['job_id']
        function, args = pending[0]
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(function, *args)
            try:
                assert entered.wait(30), 'provider never dispatched'
                cancelled = client.post(PREFIX + '/jobs/' + job_id + '/cancel')
                assert cancelled.status_code == 200
                with Session(stream_audit_engine) as db:
                    row = db.get(SkillEditCandidate, candidate['id'])
                    assert row.generation > store.get(job_id).feedback_fence.generation
            finally:
                release.set()
            future.result(timeout=60)
        assert observations['calls'] == 1
        assert not any(observations['transactions'])
        assert store.get(job_id).status == 'failed'
        assert all('draft_skill' not in item.data for item in store.get(job_id).events)
        assert client.get(PREFIX + '/jobs/' + job_id).status_code == 409
        with Session(stream_audit_engine) as db:
            row = db.get(SkillEditCandidate, candidate['id'])
            assert row.revision == candidate['revision']
            assert row.candidate_json == candidate['content']
        assert _head(stream_audit_engine, agent) == before


def test_raw_provider_failure_never_enters_feedback_events_or_changes_active_head(
    stream_audit_engine, monkeypatch,
):
    """真实SDK传输失败及原修复/保留路径只暴露稳定错误，原活动版完整保留。"""
    material, agent, _owner = _setup(stream_audit_engine)
    _model(stream_audit_engine)
    store, pending, observations = _install_protocol(stream_audit_engine, monkeypatch,
        case='provider_failure')
    with _client(stream_audit_engine) as client:
        candidate = _open(client, material, agent)
        before = _head(stream_audit_engine, agent)
        started = client.post(PREFIX + '/' + candidate['skill_id'] + '/rewrite/jobs',
                              json=_request(candidate, agent))
        assert started.status_code == 200
        job_id = started.json()['job_id']
        function, args = pending[0]
        function(*args)
        assert observations['calls'] >= 1
        assert not any(observations['transactions'])
        job = store.get(job_id)
        assert RAW_PROVIDER_MARKER not in json.dumps([item.data for item in job.events])
        assert RAW_PROVIDER_MARKER not in (job.error or '')
        metadata = client.get(PREFIX + '/jobs/' + job_id)
        assert RAW_PROVIDER_MARKER not in metadata.text
        with Session(stream_audit_engine) as db:
            row = db.get(SkillEditCandidate, candidate['id'])
            assert row.candidate_json == candidate['content']
        assert _head(stream_audit_engine, agent) == before

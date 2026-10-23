"""
@Time       : 2026/10/13
@Author     : zhanglp8181
@File       : test_final_f3_live_fixture.py
@CallChain  : 正式pytest → 私有配置/原资源发布/只读观察 → 模拟来源边界
@Description: 拒绝模型预填资格、越租户和重种运行账本，不冒充浏览器或真实供应商。
"""

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import final_f3_live_fixture
from final_f3_live_fixture import (
    LiveFixtureError, LiveFixtureLimitError, LiveSpec, _agent_branch_head_facts, _business_outcome, _editor_baseline_facts,
    _knowledge_result_summary, _private_read, approval_projection, install_api, load_spec, prepare_live,
    seed_live_resources as _seed_live_resources, snapshot, validate_spec,
)
from final_f3_preparation import digest


def seed_live_resources(db, spec):
    """执行真实资源种子并按prepare_live合同保存受限初始身份文件。"""
    runtime = Path(os.environ["F3_LIVE_RUNTIME"])
    seeded = _seed_live_resources(db, spec)
    value = {"knowledge": {key: seeded["knowledge"][key]
                           for key in ("old_document_id", "kb_id", "old_version_id")}}
    descriptor = os.open(
        runtime / "initial.private.json",
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, ensure_ascii=False)
    except Exception:
        (runtime / "initial.private.json").unlink(missing_ok=True)
        raise
    return seeded


def synthetic_spec(runtime: Path) -> dict:
    """仅提供不可联网的合成模型标签和八个独立合成对象作为正式合同输入。"""
    cases = {}
    for branch_index, kind in enumerate(("SOP", "DTA")):
        cases[kind] = {}
        for index, case in enumerate(("normal", "cancel", "safe_decline", "unknown")):
            business = {"namespace": "f3-sim-" + "a" * 16,
                        "object_id": "f3-obj-" + f"{branch_index * 4 + index + 1:032x}",
                        "product_name": "SIMULATED_DECLINE" if case == "safe_decline" else "ALPHA91",
                        "quantity": 3, "customer": "SYNTHETIC-F3", "due_date": "2026-10-06"}
            cases[kind][case] = {"message": "合成请求" + json.dumps(business, ensure_ascii=False),
                                 "confirmation_message": "确认办理", "expected_business": business}
    return {
        "schema_version": "final-f3-live-simulated-fixture-v1", "run_id": "f3-unit-synthetic",
        "runtime_name": runtime.name, "database_dialect": "sqlite",
        "tenant": {"id": "tenant_f3_unit", "name": "F3 合成租户"},
        "owner": {"id": "f3_owner_unit", "username": "f3-owner-unit", "role": "admin",
                  "password": "synthetic-owner-password", "display_name": "合成发起人"},
        "reviewer": {"id": "f3_reviewer_unit", "username": "f3-reviewer-unit", "role": "admin",
                     "password": "synthetic-reviewer-password", "display_name": "合成第二管理员"},
        "agents": {"SOP": {"id": "agent_f3_sop_unit", "name": "F3合成SOP员工"},
                   "DTA": {"id": "agent_f3_dta_unit", "name": "F3合成DTA员工"}},
        "model": {"id": "model_f3_unit", "provider": "openai_compatible",
                  "base_url": "https://model.example.invalid/v1", "model": "synthetic-model-label",
                  "api_key": "synthetic-unusable-model-key", "temperature": 0,
                  "max_output_tokens": 8192, "extra_body": {}, "timeout_seconds": 180},
        "provider": {"origin": "http://127.0.0.1:19991", "namespace": "f3-sim-" + "a" * 16,
                     "key": "synthetic-provider-submit-key", "provider_kind": "simulated_async_service"},
        "control_key": "synthetic-fixture-control-key",
        "skill": {"id": "final_f3_unit_sop", "name": "F3合成模拟办理SOP", "version": "1.0.0"},
        "tool": {"id": "tool_f3_unit", "name": "f3_simulated_delivery.submit", "display_name": "F3合成异步提交"},
        "knowledge": {"kb_name": "F3合成规则", "title": "SYNTHETIC-F3合成规则",
                      "filename": "f3-synthetic.md", "old_content_md": "SYNTHETIC-F3额度为7件，截止17:00。",
                      "new_content_md": "SYNTHETIC-F3额度为9件，截止18:30。", "old_amount": 7,
                      "new_amount": 9, "old_deadline": "17:00", "new_deadline": "18:30",
                      "old_query": "SYNTHETIC-F3额度和截止", "new_query": "SYNTHETIC-F3额度和截止",
                      "unknown_query": "请检索ZIRCON999UNKNOWN的政策额度和截止时间；没有资料请明确无答案且不引用其他政策。",
                      "guidance_slug": "f3-synthetic-policy"},
        "memories": [{"key": "customer_name", "kind": "fact", "content": "客户全名：SYNTHETIC-F3。"},
                     {"key": "receipt_preference", "kind": "preference", "content": "回执需完整保留客户、商品、数量与日期。"}],
        "date_labels": {"timezone": "UTC", "anchor_utc": "2026-10-05T10:00:00+00:00",
                        "expected_due_date": "2026-10-06"}, "cases": cases,
    }


@pytest.mark.parametrize("mismatch", ["document_tenant", "knowledge_base_tenant", "version_tenant"])
def test_editor_baseline_never_reads_body_before_fixture_identity_matches(
    tmp_path: Path, mismatch: str,
):
    """租户或版本身份不符时不触碰正文，也不归档正文派生摘要。"""
    from app.db.models import KnowledgeBase, KnowledgeBaseVersion, KnowledgeDocument

    runtime = tmp_path / "editor-baseline-private"
    runtime.mkdir(mode=0o700)
    initial = runtime / "initial.private.json"
    initial.write_text(json.dumps({"knowledge": {
        "old_document_id": "synthetic-document-id",
        "kb_id": "synthetic-kb-id",
        "old_version_id": "synthetic-version-id",
        "old_content_md": "synthetic expected policy body",
    }}))
    initial.chmod(0o600)
    tenant_id = "tenant_f3_synthetic"
    document_tenant = "other-synthetic-tenant" if mismatch == "document_tenant" else tenant_id
    reads: list[str] = []

    class TrackedDocument:
        """暴露合成身份；正文属性记录任何越过租户门的读取。"""

        id = "synthetic-document-id"
        tenant_id = document_tenant
        knowledge_base_id = "synthetic-kb-id"
        knowledge_base_version_id = "synthetic-version-id"
        source_kind = "markdown"
        source_checksum = "a" * 64

        @property
        def normalized_source(self):
            """若身份门失效，测试立即记录正文访问。"""
            reads.append("read")
            return "other tenant private body"

    knowledge_base = SimpleNamespace(
        id="synthetic-kb-id",
        tenant_id="other-synthetic-tenant" if mismatch == "knowledge_base_tenant" else tenant_id,
        metadata_json={"current_version": "1.0.0"},
        content_revision=1,
    )
    version = SimpleNamespace(
        id="synthetic-version-id",
        tenant_id="other-synthetic-tenant" if mismatch == "version_tenant" else tenant_id,
        knowledge_base_id="synthetic-kb-id",
        version="1.0.0",
        scope="overall",
        publication_state="published",
    )

    class FakeDatabase:
        """只回传本用例合成记录，所有分支查询均返回空集合。"""

        def get(self, model, _identity):
            """按正式ORM模型类型返回预设合成行。"""
            rows = {
                KnowledgeDocument: TrackedDocument(),
                KnowledgeBase: knowledge_base,
                KnowledgeBaseVersion: version,
            }
            return rows[model]

        def exec(self, _statement):
            """返回空分支及head结果，不访问任何数据库。"""
            return SimpleNamespace(all=lambda: [])

    spec = LiveSpec({
        "tenant": {"id": tenant_id},
        "agents": {"DTA": {"id": "synthetic-dta-agent"}},
    })
    facts = _editor_baseline_facts(FakeDatabase(), spec, runtime)

    assert reads == []
    assert facts["document_body_identity_valid"] is False
    assert facts["normalized_source_sha256"] is None
    assert facts["normalized_source_json_sha256"] is None
    assert facts["canonical_source_json_sha256"] is None
    assert facts["normalized_source_length"] is None
    assert facts["canonical_source_length"] is None
    assert facts["normalized_source_matches_fixture"] is None
    assert facts["normalized_source_exact_matches_fixture"] is None
    assert facts["document_source_kind"] is None
    assert facts["source_checksum"] is None


def test_agent_branch_head_distinguishes_overall_inheritance_from_version_mismatch():
    """Agent branch 可合法继承 overall 发布版本，scope 标签不能误报 head 不匹配。"""
    branch = SimpleNamespace(
        tenant_id="tenant_f3_synthetic",
        agent_id="agent_f3_dta_synthetic",
        knowledge_base_id="kb_f3_synthetic",
        status="active",
        head_version="1.1.0",
        content_revision=2,
    )
    inherited_head = SimpleNamespace(
        id="kbver_f3_overall_head",
        tenant_id="tenant_f3_synthetic",
        knowledge_base_id="kb_f3_synthetic",
        version="1.1.0",
        publication_state="published",
        scope="overall",
    )
    facts = _agent_branch_head_facts(
        branch,
        inherited_head,
        tenant_id="tenant_f3_synthetic",
        agent_id="agent_f3_dta_synthetic",
        knowledge_base_id="kb_f3_synthetic",
    )

    assert facts["database_agent_head_matches_branch"] is True
    assert facts["database_agent_head_scope_matches_agent"] is False
    assert facts["database_agent_head_scope_inherits_overall"] is True
    assert facts["database_agent_head_version_id"] == "kbver_f3_overall_head"


@pytest.fixture
def owned_spec(tmp_path, monkeypatch):
    """只预留私有测试根和配置，不启动服务、不读取真实环境凭据。"""
    runtime = tmp_path / "f3-live-unit"
    runtime.mkdir(mode=0o700)
    value = synthetic_spec(runtime)
    path = runtime / "fixture.private.json"
    path.write_text(json.dumps(value, ensure_ascii=False))
    path.chmod(0o600)
    monkeypatch.setenv("F3_LIVE_FIXTURE_SPEC_PATH", str(path))
    monkeypatch.setenv("F3_LIVE_RUNTIME", str(runtime))
    monkeypatch.setenv("FULLSTACK_FINAL_F3_LIVE", "1")
    monkeypatch.setenv("FULLSTACK_E2E_RUN_ID", value["run_id"])
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{runtime / 'journey.sqlite3'}")
    marker = runtime / ".f3-live-owned.private.json"
    marker.write_text(json.dumps({"uid": os.getuid(), "run_id": value["run_id"],
                                  "configuration_sha256": "a" * 64, "app_secret": "synthetic"}))
    marker.chmod(0o600)
    return runtime, value


def test_private_model_labels_keep_unqualified_and_secret_out_of_repr(owned_spec):
    """私有结构有效仍不赋予模型预检或真实供应商资格，摘要不泄漏配置值。"""
    runtime, value = owned_spec
    spec = validate_spec(value, runtime=runtime)
    result = spec.safe_summary()
    assert result["provider_kind"] == "simulated_async_service"
    assert result["real_vendor_qualified"] is False and result["JRN08_passed"] is False
    assert value["model"]["api_key"] not in repr(spec) + json.dumps(result)
    assert value["owner"]["password"] not in repr(spec) + json.dumps(result)


def test_final_reply_outcome_projection_keeps_rejection_without_storing_reply_text():
    """最终业务摘要区分真实失败说明和肯定成功宣称，只返回布尔值。"""
    rejection = _business_outcome("异步订单提交失败（HTTP 422），本次未创建业务对象。")
    success = _business_outcome("订单已成功提交，回执已生成。")

    assert rejection == {"failure_notice": True, "success_claim": False}
    assert success == {"failure_notice": False, "success_claim": True}
    assert "text" not in rejection and "content" not in rejection


def test_knowledge_result_projection_keeps_partial_insufficiency_at_operation_level():
    """部分召回的逐文档证据必须继承原Operation不足状态和完整性结论。"""
    result = _knowledge_result_summary({"data": {
        "outcome": "insufficient",
        "evidence_sufficiency": {"required": True, "satisfied": False,
                                  "covered_aspects": ["来源"]},
        "selected_documents": [{"document_id": "synthetic-document"}],
    }})

    assert result == {"outcome": "insufficient", "evidence_required": True,
                      "evidence_satisfied": False}


@pytest.mark.parametrize("field", ["schema_version", "runtime_name", "database_dialect"])
def test_unknown_identity_contract_is_refused(owned_spec, field):
    """拒绝未知schema、外运行根或未支持方言，不能按当前合同补位。"""
    runtime, value = owned_spec
    value[field] = "unknown"
    with pytest.raises(LiveFixtureError, match="F3_LIVE_SPEC_INVALID"):
        validate_spec(value, runtime=runtime)


@pytest.mark.parametrize("unknown_query", [
    "请检索知识库，告诉我CYPRESS42从未记录的机场接驳费用，附依据；没有资料请明确说明。",
    "没有匹配资料。",
])
def test_live_knowledge_unknown_query_matches_preregistered_zircon_label(owned_spec, unknown_query):
    """未知质量输入必须保持I1预注册的ZIRCON对象，不能换成输出后才调整的相邻查询。"""
    runtime, value = owned_spec
    value["knowledge"]["unknown_query"] = unknown_query

    with pytest.raises(LiveFixtureError, match="F3_LIVE_SPEC_INVALID"):
        validate_spec(value, runtime=runtime)


def test_memory_quality_extension_uses_exact_original_publisher_and_readonly_api(
    owned_spec, seed_session,
):
    """已验证的13记忆作用域可作为明确可选扩展，资源仍原发布且没有运行成功预置。"""
    from fastapi import FastAPI
    from sqlmodel import select
    from app.db.models import MemoryRecord, SopInstance, SopOperation
    from test_final_f3_live_memory import memory_spec

    runtime, value = owned_spec
    value["memory_quality"] = memory_spec()["memory_quality"]
    spec = validate_spec(value, runtime=runtime)
    initial = seed_live_resources(seed_session, spec)
    assert sum(len(cases) for cases in initial["memory_quality"]["agents"].values()) == 13
    assert len(seed_session.exec(select(MemoryRecord)).all()) == 43
    assert not seed_session.exec(select(SopInstance)).all()
    assert not seed_session.exec(select(SopOperation)).all()
    path = runtime / "fixture.private.json"
    path.write_text(json.dumps(value, ensure_ascii=False))
    path.chmod(0o600)
    app = FastAPI()
    install_api(app, runtime)
    route = next(route for route in app.routes if route.path == "/__e2e/f3/memory/{agent_id}")
    assert route.methods == {"GET"}


def test_live_business_display_schema_is_frozen_before_dispatch(owned_spec, seed_session):
    """模拟订单事前通过原发布器授权完整合成业务字段，最终展示不能临时扩大旧快照。"""
    from app.db.models import Tool
    from app.dynamic_tasks.capability_catalog import published_tool_snapshot
    from final_f3_live_fixture import BUSINESS_FIELDS

    runtime, value = owned_spec
    spec = validate_spec(value, runtime=runtime)
    seed_live_resources(seed_session, spec)
    tool = seed_session.get(Tool, value["tool"]["id"])
    frozen = published_tool_snapshot(tool, value["agents"]["SOP"]["id"])
    assert set(frozen.user_view["output_schema"]["properties"]) == set(BUSINESS_FIELDS) | {"receipt_id"}
    assert "provider_trace" not in frozen.user_view["output_schema"]["properties"]


def test_live_tool_date_input_schema_requires_iso_date_from_frozen_turn_anchor(
    owned_spec, seed_session,
):
    """相对日期外呼字段必须声明ISO日期格式和同轮冻结时间锚语义。"""
    from app.db.models import Tool
    from app.dynamic_tasks.capability_catalog import published_tool_snapshot

    runtime, value = owned_spec
    spec = validate_spec(value, runtime=runtime)
    seed_live_resources(seed_session, spec)
    tool = seed_session.get(Tool, value["tool"]["id"])
    frozen = published_tool_snapshot(tool, value["agents"]["DTA"]["id"])
    due_date = frozen.model_view["input_schema"]["properties"]["due_date"]

    assert due_date.get("format") == "date"
    assert "time_context" in due_date.get("description", "")


@pytest.mark.parametrize("field", ["credentials_verified", "preflight_status", "capability_snapshot_json"])
def test_model_cannot_predeclare_preflight_success(owned_spec, field):
    """私有配置不能预填原模型预检成功、capability或授权。"""
    runtime, value = owned_spec
    value["model"][field] = True
    with pytest.raises(LiveFixtureError, match="F3_LIVE_SPEC_INVALID"):
        validate_spec(value, runtime=runtime)


@pytest.mark.parametrize("field", ["id", "username"])
def test_second_manager_must_be_distinct_actual_identity(owned_spec, field):
    """审批者必须独立实际账号，不能借不同显示名伪装第二管理者。"""
    runtime, value = owned_spec
    value["reviewer"][field] = value["owner"][field]
    with pytest.raises(LiveFixtureError, match="F3_LIVE_SPEC_INVALID"):
        validate_spec(value, runtime=runtime)


@pytest.mark.parametrize("change", ["real_vendor", "same_agent", "duplicate_object", "boolean_quantity"])
def test_simulated_source_and_manual_cases_are_frozen(owned_spec, change):
    """拒绝真实资格冒称、两个分支共员工、共业务对象或布尔件数。"""
    runtime, value = owned_spec
    if change == "real_vendor":
        value["provider"]["provider_kind"] = "real_vendor"
    elif change == "same_agent":
        value["agents"]["DTA"]["id"] = value["agents"]["SOP"]["id"]
    elif change == "duplicate_object":
        value["cases"]["DTA"]["normal"]["expected_business"]["object_id"] = value["cases"]["SOP"]["normal"]["expected_business"]["object_id"]
    else:
        value["cases"]["DTA"]["normal"]["expected_business"]["quantity"] = True
    with pytest.raises(LiveFixtureError, match="F3_LIVE_SPEC_INVALID"):
        validate_spec(value, runtime=runtime)


@pytest.mark.parametrize("url", ["http://127.0.0.1:19991/model", "http://localhost/v1"])
def test_local_model_route_cannot_replace_real_model_selection(owned_spec, url):
    """本地业务模拟入口和本地模型替身不能被私有配置选为真实模型。"""
    runtime, value = owned_spec
    value["model"]["base_url"] = url
    with pytest.raises(LiveFixtureError, match="F3_LIVE_SPEC_INVALID"):
        validate_spec(value, runtime=runtime)


def test_load_only_private_owned_file_and_detaches_input(owned_spec):
    """私有文件读取固定对象快照，之后改调用方对象不能改变已加载人工标签。"""
    runtime, value = owned_spec
    spec = load_spec(runtime)
    value["knowledge"]["old_amount"] = 999
    assert spec.value["knowledge"]["old_amount"] == 7


def test_private_json_read_rejects_oversized_initial_file(owned_spec):
    """私有初始化锚超过2MiB时须在JSON解析前拒绝读取。"""
    runtime, _ = owned_spec
    path = runtime / "initial.private.json"
    path.write_bytes(b'{"padding":"' + b"x" * (2 * 1024 * 1024) + b'"}')
    path.chmod(0o600)

    with pytest.raises(LiveFixtureError, match="F3_LIVE_PRIVATE_JSON_LIMIT"):
        _private_read(path, runtime)


@pytest.mark.parametrize("shape", ["depth", "nodes"])
def test_private_json_read_rejects_complexity_before_decoder(owned_spec, monkeypatch, shape):
    """私有JSON嵌套或节点过量时，在对象解码前由原字节扫描器拒绝。"""
    runtime, _ = owned_spec
    path = runtime / "initial.private.json"
    payload = (b"[" * 65 + b"0" + b"]" * 65 if shape == "depth"
               else b"[" + b",".join([b"0"] * 25_000) + b"]")
    path.write_bytes(payload)
    path.chmod(0o600)
    decode_called = False

    def forbidden_decoder(_data):
        """解析器不应接触复杂度预检已拒绝的文件。"""
        nonlocal decode_called
        decode_called = True
        raise AssertionError("private JSON decoder must remain behind preflight")

    monkeypatch.setattr(final_f3_live_fixture, "json", SimpleNamespace(loads=forbidden_decoder))
    with pytest.raises(LiveFixtureLimitError, match="F3_LIVE_PRIVATE_JSON_LIMIT"):
        _private_read(path, runtime)
    assert not decode_called


def test_prepare_live_rejects_oversized_initial_projection_before_json_encoder(owned_spec, monkeypatch):
    """种子投影超过私有正文预算时，在完整JSON编码或文件创建前失败关闭。"""
    import sqlmodel

    runtime, _ = owned_spec
    encoder_called = False
    original_iterencode = json.JSONEncoder.iterencode

    class EmptySession:
        """避免本合同测试连接或修改任何数据库。"""

        def __enter__(self):
            """提供无SQL写入的准备期会话上下文。"""
            return self

        def __exit__(self, *_args):
            """关闭纯内存会话占位符。"""
            return False

    def observe_encoder(encoder, value):
        """记录超大对象是否越过有界预检进入完整JSON编码器。"""
        nonlocal encoder_called
        encoder_called = True
        yield from original_iterencode(encoder, value)

    monkeypatch.setattr(sqlmodel, "Session", lambda _engine: EmptySession())
    monkeypatch.setattr(final_f3_live_fixture, "_check_database", lambda *_args: None)
    monkeypatch.setattr(final_f3_live_fixture, "seed_live_resources",
                        lambda *_args: {"oversized": "x" * (2 * 1024 * 1024 + 1)})
    monkeypatch.setattr(json.JSONEncoder, "iterencode", observe_encoder)

    with pytest.raises(LiveFixtureError, match="F3_LIVE_PRIVATE_JSON_LIMIT"):
        prepare_live(runtime, reuse=False)

    assert not encoder_called
    assert not (runtime / "initial.private.json").exists()


def test_prepare_live_persists_bounded_private_initial_json(owned_spec, monkeypatch):
    """正常私有投影以有限紧凑UTF-8正文和600权限写入且可精确读回。"""
    import sqlmodel

    runtime, _ = owned_spec

    class EmptySession:
        """避免该写入合同测试打开数据库连接。"""

        def __enter__(self):
            """提供无SQL写入的准备期会话上下文。"""
            return self

        def __exit__(self, *_args):
            """关闭纯内存会话占位符。"""
            return False

    expected = {"label": "合成规范 a  b\n结束", "count": 11}
    monkeypatch.setattr(sqlmodel, "Session", lambda _engine: EmptySession())
    monkeypatch.setattr(final_f3_live_fixture, "_check_database", lambda *_args: None)
    monkeypatch.setattr(final_f3_live_fixture, "seed_live_resources", lambda *_args: expected)

    actual = prepare_live(runtime, reuse=False)

    path = runtime / "initial.private.json"
    body = path.read_bytes()
    assert actual == expected
    assert json.loads(body) == expected
    assert len(body) <= final_f3_live_fixture.MAX_PRIVATE_JSON_BYTES
    assert path.stat().st_mode & 0o777 == 0o600
    assert "合成规范".encode("utf-8") in body


@pytest.mark.parametrize("mode", ["public", "symlink"])
def test_private_spec_file_safety_is_required(owned_spec, mode):
    """不跟随链接或读取共享私有配置，真实凭据不被自动搬到公开上下文。"""
    runtime, _ = owned_spec
    path = runtime / "fixture.private.json"
    if mode == "public":
        path.chmod(0o644)
    else:
        original = runtime / "source.private.json"
        path.rename(original)
        path.symlink_to(original)
    with pytest.raises(LiveFixtureError, match="F3_LIVE_PRIVATE_SPEC_REQUIRED"):
        load_spec(runtime)


def test_fixture_requires_explicit_runtime_before_importing_application(owned_spec, monkeypatch):
    """未显式开启本次live环境时在碰应用库前拒绝，不受路径存在诱导。"""
    runtime, _ = owned_spec
    monkeypatch.delenv("FULLSTACK_FINAL_F3_LIVE")
    with pytest.raises(LiveFixtureError, match="F3_LIVE_EXPLICIT_ISOLATION_REQUIRED"):
        prepare_live(runtime, reuse=False)


@pytest.fixture
def seed_session(owned_spec):
    """仅建本项自有SQLite正式资源表，业务执行与模型调用仍由后续原消费者产生。"""
    from sqlmodel import Session, SQLModel, create_engine
    from app.db import models  # noqa: F401

    runtime, _ = owned_spec
    engine = create_engine(f"sqlite:///{runtime / 'journey.sqlite3'}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        yield db
    engine.dispose()


def test_seed_publishes_original_resources_but_never_model_or_business_success(owned_spec, seed_session):
    """原Skill/知识/工具发布身份有效；真实模型预检与所有运行账本保持未完成。"""
    from sqlmodel import select
    from app.db.models import AgentResourceBinding, ModelConfig, SkillVersion, SopInstance, SopOperation, Tool

    runtime, value = owned_spec
    result = seed_live_resources(seed_session, validate_spec(value, runtime=runtime))
    for kind in ("SOP", "DTA"):
        assert result["agents"][kind]["gallery_label"] == (
            value["agents"][kind]["name"] + " @" + value["owner"]["id"]
        )
    model = seed_session.get(ModelConfig, value["model"]["id"])
    assert model.preflight_status == "unverified"
    assert not model.capability_snapshot_json and model.capability_checksum is None
    assert model.temperature == 0 and model.extra_body_json == {}
    tool = seed_session.get(Tool, value["tool"]["id"])
    assert tool.reliability_contract_json["async_strategy"] == "provider_task"
    assert tool.reliability_published_at and tool.reliability_checksum
    assert value["provider"]["origin"] in tool.url
    assert "__e2e/f1" not in tool.url and "openai" not in tool.url
    assert seed_session.exec(select(SkillVersion)).first() is not None
    assert not seed_session.exec(select(SopInstance)).all()
    assert not seed_session.exec(select(SopOperation)).all()
    assert result["knowledge"]["old_version_id"] and result["knowledge"]["old_document_id"]
    assert result["knowledge"]["new_version_id"] is None
    assert result["sop_knowledge"]["old_version_id"] and result["sop_knowledge"]["old_document_id"]
    assert result["knowledge"]["kb_id"] != result["sop_knowledge"]["kb_id"]
    assert result["knowledge"]["old_document_id"] != result["sop_knowledge"]["old_document_id"]
    bindings = seed_session.exec(select(AgentResourceBinding).where(
        AgentResourceBinding.tenant_id == value["tenant"]["id"],
        AgentResourceBinding.resource_type == "knowledge_base",
    )).all()
    by_agent = {row.agent_id: row.resource_id for row in bindings}
    assert by_agent[value["agents"]["DTA"]["id"]] == result["knowledge"]["kb_id"]
    assert by_agent[value["agents"]["SOP"]["id"]] == result["sop_knowledge"]["kb_id"]
    assert result["owner"]["expected"]["id"] != result["reviewer"]["expected"]["id"]


def test_seed_test_helper_writes_minimal_private_initial_identity(owned_spec, seed_session):
    """单测私有启动锚只保存知识身份ID，不复制正文、账号或运行配置。"""
    runtime, _ = owned_spec
    seeded = seed_live_resources(seed_session, validate_spec(synthetic_spec(runtime), runtime=runtime))

    path = runtime / "initial.private.json"
    initial = json.loads(path.read_text(encoding="utf-8"))
    assert path.stat().st_mode & 0o777 == 0o600
    assert set(initial) == {"knowledge"}
    assert set(initial["knowledge"]) == {"old_document_id", "kb_id", "old_version_id"}
    assert initial["knowledge"]["old_document_id"] == seeded["knowledge"]["old_document_id"]


def test_seed_cannot_reseed_existing_resources(owned_spec, seed_session):
    """重复prepare必须显式reuse，不用重种数据覆盖原版本或运行事实。"""
    runtime, value = owned_spec
    spec = validate_spec(value, runtime=runtime)
    seed_live_resources(seed_session, spec)
    with pytest.raises(LiveFixtureError, match="F3_LIVE_SEED_ALREADY_EXISTS"):
        seed_live_resources(seed_session, spec)


def test_snapshot_reads_synced_dta_head_from_original_overall_version(owned_spec, seed_session):
    """初始员工同步头指向整体物理版本，读取不得假设已有员工scope版本。"""
    runtime, value = owned_spec
    spec = validate_spec(value, runtime=runtime)
    initial = seed_live_resources(seed_session, spec)
    actual = snapshot(seed_session, spec)
    assert actual["knowledge"]["current_version_id"] == initial["knowledge"]["old_version_id"]


@pytest.mark.parametrize("invalid", [None, "draft", "inactive_branch"])
def test_snapshot_uses_exact_dta_published_head_without_sop_fallback(
    owned_spec, seed_session, invalid,
):
    """DTA正文后继按其原分支头读回；草稿或停用分支不得退取SOP旧版。"""
    from sqlmodel import select
    from app.db.models import AgentKnowledgeBranch, KnowledgeBaseVersion

    runtime, value = owned_spec
    spec = validate_spec(value, runtime=runtime)
    initial = seed_live_resources(seed_session, spec)
    old = seed_session.get(KnowledgeBaseVersion, initial["knowledge"]["old_version_id"])
    branch = seed_session.exec(select(AgentKnowledgeBranch).where(
        AgentKnowledgeBranch.tenant_id == value["tenant"]["id"],
        AgentKnowledgeBranch.agent_id == value["agents"]["DTA"]["id"],
        AgentKnowledgeBranch.knowledge_base_id == old.knowledge_base_id,
    )).one()
    new = KnowledgeBaseVersion(id="projection-dta-new-head", tenant_id=old.tenant_id,
        knowledge_base_id=old.knowledge_base_id, version="1.0.1",
        scope="agent:" + branch.agent_id, name=old.name,
        publication_state="draft" if invalid == "draft" else "published")
    branch.head_version = new.version
    if invalid == "inactive_branch":
        branch.status = "inactive"
    seed_session.add(new)
    seed_session.add(branch)
    seed_session.commit()
    actual = snapshot(seed_session, spec)
    assert actual["knowledge"]["current_version_id"] == (None if invalid else new.id)


def test_facts_do_not_include_foreign_session_or_unrestricted_user_body(owned_spec, seed_session):
    """只读事实按实际tenant/agent/user/session交集过滤，消息正文只能摘要。"""
    from app.db.models import ChatSession, Message

    runtime, value = owned_spec
    spec = validate_spec(value, runtime=runtime)
    seed_live_resources(seed_session, spec)
    seed_session.add(ChatSession(id="owned-session", tenant_id=value["tenant"]["id"],
                                 user_id=value["owner"]["id"], agent_id=value["agents"]["DTA"]["id"]))
    seed_session.add(ChatSession(id="foreign-session", tenant_id="foreign", user_id="foreign",
                                 agent_id=value["agents"]["DTA"]["id"]))
    seed_session.add(Message(id="owned-message", tenant_id=value["tenant"]["id"],
                            session_id="owned-session", role="user", content="私密自由正文不可进入facts"))
    seed_session.commit()
    result = snapshot(seed_session, spec, session_id="owned-session")
    assert [row["id"] for row in result["sessions"]] == ["owned-session"]
    assert "私密自由正文不可进入facts" not in json.dumps(result, ensure_ascii=False)
    with pytest.raises(LiveFixtureError, match="F3_LIVE_SESSION_FORBIDDEN"):
        snapshot(seed_session, spec, session_id="foreign-session")


def test_approval_observer_does_not_create_missing_or_foreign_approval(owned_spec, seed_session):
    """精确id缺失或越范围即拒绝，不制造可批准投影以填空。"""
    runtime, value = owned_spec
    spec = validate_spec(value, runtime=runtime)
    seed_live_resources(seed_session, spec)
    with pytest.raises(LiveFixtureError, match="F3_LIVE_APPROVAL_NOT_FOUND"):
        approval_projection(seed_session, spec, "missing-attention")


def synthetic_execution(db, spec, *, kind="sop", identity="projection-instance"):
    """为只读投影红测持久化合成原行；不宣称浏览器运行或真实业务成功。"""
    from sqlmodel import select
    from app.db.models import ChatSession, SkillVersion, SopInstance

    value = spec.value
    version = db.exec(select(SkillVersion).where(
        SkillVersion.tenant_id == value["tenant"]["id"],
        SkillVersion.skill_id == value["skill"]["id"],
    )).first()
    session = ChatSession(id="projection-session", tenant_id=value["tenant"]["id"],
        user_id=value["owner"]["id"], agent_id=value["agents"]["DTA" if kind == "dynamic_task" else "SOP"]["id"])
    instance = SopInstance(id=identity, tenant_id=session.tenant_id, session_id=session.id,
        agent_id=session.agent_id, initiator_user_id=session.user_id, kind=kind, status="waiting",
        active_slot_key="projection-slot", source_ref="projection-source")
    if kind == "sop":
        instance.skill_id = version.skill_id
        instance.skill_version_id = version.id
        instance.skill_version = version.version
        instance.definition_checksum = version.compiled_definition_checksum
    else:
        instance.goal_snapshot_json = {"goal": "只读投影合成控制"}
        instance.current_plan_revision_id = "original-plan-revision"
        instance.current_plan_checksum = "b" * 64
        instance.capability_snapshot_json = {}
    db.add(session)
    db.add(instance)
    db.commit()
    return session, instance


@pytest.mark.parametrize("kind", ["sop", "dynamic_task"])
def test_snapshot_remote_key_uses_original_provider_bytes_for_reconciliation(
    owned_spec, seed_session, kind,
):
    """同一冻结键的只读摘要能查询原provider唯一任务，规范JSON摘要不能代替原UTF8。"""
    from app.db.models import SopOperation
    from final_f3_simulated_provider import SimulationStore

    runtime, value = owned_spec
    spec = validate_spec(value, runtime=runtime)
    seed_live_resources(seed_session, spec)
    session, instance = synthetic_execution(seed_session, spec, kind=kind)
    key = "f3-synthetic-remote-key:a_b:001"
    operation = SopOperation(id="projection-remote-operation", tenant_id=instance.tenant_id,
        instance_id=instance.id, node_execution_id="projection-remote-node",
        operation_name="f3_async_order", idempotency_key="c" * 64,
        logical_action_id="projection-remote-action", request_fingerprint="d" * 64,
        status="unknown", remote_idempotency_key=key)
    seed_session.add(operation)
    seed_session.commit()
    provider_runtime = runtime / "independent-provider"
    provider_runtime.mkdir(mode=0o700)
    provider = SimulationStore(provider_runtime, value["provider"]["namespace"])
    try:
        submitted = provider.submit(value["cases"]["SOP"]["normal"]["expected_business"], key)
        actual = snapshot(seed_session, spec, session_id=session.id)["operations"][0]
        assert actual["remote_key_hash"] == digest(key)
        assert actual["remote_idempotency_key_hash"] == hashlib.sha256(key.encode()).hexdigest()
        readback = provider.reconcile(actual["remote_idempotency_key_hash"])
        assert readback["taskId"] == submitted["taskId"]
        assert readback["request_count"] == 1 and readback["effect_count"] == 0
        assert readback["real_vendor_qualified"] is False
        assert key not in json.dumps(actual)
    finally:
        provider.db.close()


def test_snapshot_keeps_original_plan_knowledge_versions_and_turn_time(owned_spec, seed_session):
    """恢复前后比较原plan、冻结知识和本轮时间，不以当前知识头或fixture标签补位。"""
    runtime, value = owned_spec
    spec = validate_spec(value, runtime=runtime)
    initial = seed_live_resources(seed_session, spec)
    session, instance = synthetic_execution(seed_session, spec, kind="dynamic_task")
    original_time = {"source_input_id": "projection-source", "timezone": "UTC",
                     "anchor_at_utc": "2026-10-05T10:00:00Z", "local_at": "2026-10-05T10:00:00+00:00"}
    instance.context_json = {"time_context": original_time, "source_input_id": "projection-source",
                             "unrelated_private_text": "不准投影的私密正文"}
    instance.capability_snapshot_json = {"knowledge": [{"model_view": {"knowledge_bases": [
        {"version_id": initial["knowledge"]["old_version_id"]}]}}],
        "unrelated": {"version_id": "must-not-be-knowledge-version"}}
    seed_session.add(instance)
    seed_session.commit()
    result = snapshot(seed_session, spec, session_id=session.id)
    actual = result["instances"][0]
    assert actual["plan_checksum"] == instance.current_plan_checksum
    assert actual["frozen_knowledge_versions"] == [initial["knowledge"]["old_version_id"]]
    assert actual["time_context"] == original_time
    assert actual["source_input_id"] == "projection-source"
    assert "不准投影的私密正文" not in json.dumps(result, ensure_ascii=False)


def test_snapshot_reports_only_frozen_async_tool_and_plan_presence(owned_spec, seed_session):
    """只读原账本能区分工具未冻结与模型计划未选工具，且不投影计划正文。"""
    from app.db.models import ExecutionPlanRevision

    runtime, value = owned_spec
    spec = validate_spec(value, runtime=runtime)
    seed_live_resources(seed_session, spec)
    session, instance = synthetic_execution(seed_session, spec, kind="dynamic_task")
    expected_tool = value["tool"]["name"]
    instance.capability_snapshot_json = {"tools": [{
        "name": expected_tool,
        "model_view": {"name": expected_tool},
        "contract": {"risk_class": "external_write", "dynamic_task_enabled": True,
                     "execution_mode": "detached", "async_strategy": "provider_task"},
    }]}
    instance.current_plan_revision_id = "projection-plan-current"
    instance.current_plan_checksum = "b" * 64
    instance.context_json = {"failure_code": "ValidationError"}
    plan = ExecutionPlanRevision(
        id=instance.current_plan_revision_id, tenant_id=instance.tenant_id,
        execution_id=instance.id, revision_number=1, status="active",
        plan_json={"goal": "保留但不得投影的计划文本", "steps": [{"capability_refs": [expected_tool]}]},
        checksum=instance.current_plan_checksum, capability_snapshot_json={
            "tools": [{"name": expected_tool, "model_view": {"name": expected_tool},
                       "contract": {"risk_class": "external_write", "dynamic_task_enabled": True,
                                    "execution_mode": "detached", "async_strategy": "provider_task"}}]},
        capability_checksum="c" * 64,
    )
    seed_session.add(instance)
    seed_session.add(plan)
    seed_session.commit()

    result = snapshot(seed_session, spec, session_id=session.id)
    actual = result["instances"][0]
    assert actual["expected_async_tool_frozen"] is True
    assert actual["expected_async_tool_frozen_for_plan"] is True
    assert actual["expected_async_tool_contract_valid"] is True
    assert actual["current_plan_step_count"] == 1
    assert actual["current_plan_references_expected_async_tool"] is True
    assert actual["runtime_failure_code"] == "ValidationError"
    assert "保留但不得投影的计划文本" not in json.dumps(result, ensure_ascii=False)
    assert expected_tool not in json.dumps(actual, ensure_ascii=False)


def test_snapshot_does_not_project_free_form_runtime_failure_text(owned_spec, seed_session):
    """运行错误摘要只保留稳定代码标识，不泄漏异常消息或模型正文。"""
    runtime, value = owned_spec
    spec = validate_spec(value, runtime=runtime)
    seed_live_resources(seed_session, spec)
    session, instance = synthetic_execution(seed_session, spec, kind="dynamic_task")
    instance.context_json = {"failure_code": "private synthetic customer detail"}
    seed_session.add(instance)
    seed_session.commit()

    actual = snapshot(seed_session, spec, session_id=session.id)["instances"][0]

    assert actual["runtime_failure_code"] is None
    assert "private synthetic customer detail" not in json.dumps(actual, ensure_ascii=False)


def test_snapshot_and_approval_keep_original_typed_attention_contract(owned_spec, seed_session):
    """实际Attention标题、动作和Operation指纹应能驱动原UI；观察不制造批准。"""
    from app.db.models import SopOperation, SopWorkItem

    runtime, value = owned_spec
    spec = validate_spec(value, runtime=runtime)
    seed_live_resources(seed_session, spec)
    session, instance = synthetic_execution(seed_session, spec, kind="dynamic_task")
    operation = SopOperation(id="projection-operation", tenant_id=instance.tenant_id,
        instance_id=instance.id, node_execution_id="projection-node", operation_name="f3_async_order",
        idempotency_key="c" * 64, logical_action_id="projection-action", request_fingerprint="d" * 64,
        effect_kind="external_write")
    item = SopWorkItem(id="projection-attention", tenant_id=instance.tenant_id,
        instance_id=instance.id, attention_kind="tool_approval", title="原合成写审批标题",
        source_ref=operation.id, payload_json={"operation_id": operation.id, "fingerprint": "d" * 64},
        allowed_commands_json=["allow_once", "deny"])
    seed_session.add(operation)
    seed_session.add(item)
    seed_session.commit()
    actual = snapshot(seed_session, spec, session_id=session.id)["attention"][0]
    assert actual["execution_id"] == instance.id and actual["kind"] == "tool_approval"
    assert actual["title"] == item.title
    assert actual["payload"] == item.payload_json
    assert actual["allowed_commands"] == item.allowed_commands_json
    approval = approval_projection(seed_session, spec, item.id)["attention"]
    assert approval["title"] == item.title
    assert approval["allowed_commands"] == item.allowed_commands_json
    assert approval["available_commands"] == item.allowed_commands_json
    assert approval["session_id"] == session.id
    assert approval["resolution"] == {}
    assert approval_projection(seed_session, spec, item.id)["operation"]["effect_kind"] == "external_write"


def test_snapshot_inbox_keeps_original_provider_event_and_status(owned_spec, seed_session):
    """迟到去重核原provider_event_id与终态，不以task id代替回执身份。"""
    from app.db.models import ExternalTaskEventInbox, SopOperation

    runtime, value = owned_spec
    spec = validate_spec(value, runtime=runtime)
    seed_live_resources(seed_session, spec)
    session, instance = synthetic_execution(seed_session, spec)
    operation = SopOperation(id="projection-operation", tenant_id=instance.tenant_id,
        instance_id=instance.id, node_execution_id="projection-node", operation_name="f3_async_order",
        idempotency_key="c" * 64, logical_action_id="projection-action", request_fingerprint="d" * 64)
    event = ExternalTaskEventInbox(id="projection-inbox", tenant_id=instance.tenant_id,
        operation_id=operation.id, provider_task_id="original-task-id", provider_event_id="original-event-id",
        provider_status="completed", status="ignored", payload_checksum="e" * 64, observation_revision=3)
    seed_session.add(operation)
    seed_session.add(event)
    seed_session.commit()
    actual = snapshot(seed_session, spec, session_id=session.id)["inbox"][0]
    assert actual["provider_event_id"] == event.provider_event_id
    assert actual["provider_status"] == "completed"
    assert actual["status"] == "ignored" and actual["signal_id"] is None


def test_snapshot_message_status_uses_original_frozen_source_not_current_instance(owned_spec, seed_session):
    """旧等待回复与缺来源回复不得借当前实例终态冒充本轮最终成功回复。"""
    from app.db.models import Message
    from app.feedback.source import freeze_execution_source

    runtime, value = owned_spec
    spec = validate_spec(value, runtime=runtime)
    seed_live_resources(seed_session, spec)
    session, instance = synthetic_execution(seed_session, spec)
    frozen_source = freeze_execution_source(seed_session, session, "projection-source", instance.id)
    seed_session.add(Message(id="waiting-reply", tenant_id=session.tenant_id, session_id=session.id,
        role="assistant", content="原等待回复", metadata_json={"execution_id": instance.id,
        "user_message_id": "projection-source", "feedback_source": frozen_source}))
    seed_session.add(Message(id="unbound-reply", tenant_id=session.tenant_id, session_id=session.id,
        role="assistant", content="缺原快照回复", metadata_json={"execution_id": instance.id}))
    instance.status = "succeeded"
    instance.active_slot_key = None
    seed_session.add(instance)
    seed_session.commit()
    replies = {row["id"]: row for row in snapshot(seed_session, spec, session_id=session.id)["messages"]}
    assert replies["waiting-reply"]["execution_status"] == "waiting"
    assert replies["unbound-reply"]["execution_status"] is None


@pytest.mark.parametrize(
    ("payload", "expected_code"),
    [
        ({"code": "LLM_ERROR", "message": "私密错误正文不应进入事实接口"}, "LLM_ERROR"),
        ({"code": "missing_model_config", "message": "私密错误正文不应进入事实接口"},
         "missing_model_config"),
        ({"code": "unsafe error with private details", "message": "另一个私密正文"}, None),
    ],
)
def test_snapshot_error_event_exposes_only_a_bounded_machine_code(
    owned_spec, seed_session, payload, expected_code,
):
    """错误事件仅投影有限机器码，私密模型或异常正文不得进入事实接口。"""
    from app.db.models import AgentEvent, ChatSession

    runtime, value = owned_spec
    spec = validate_spec(value, runtime=runtime)
    seed_live_resources(seed_session, spec)
    session = ChatSession(id="error-projection-session", tenant_id=value["tenant"]["id"],
        user_id=value["owner"]["id"], agent_id=value["agents"]["SOP"]["id"])
    seed_session.add(session)
    seed_session.add(AgentEvent(tenant_id=session.tenant_id, session_id=session.id,
        event_type="error_occurred", payload_json=payload))
    seed_session.commit()

    actual = snapshot(seed_session, spec, session_id=session.id)
    error_event = actual["events"][0]

    assert error_event.get("error_code") == expected_code
    serialized = json.dumps(actual, ensure_ascii=False)
    assert "私密错误正文不应进入事实接口" not in serialized
    assert "另一个私密正文" not in serialized


def test_snapshot_error_event_projects_only_allowlisted_exception_category(
    owned_spec, seed_session,
):
    """错误事件只投影白名单异常类别，不把堆栈、模型或供应商正文带出隔离运行时。"""
    from app.db.models import AgentEvent, ChatSession

    runtime, value = owned_spec
    spec = validate_spec(value, runtime=runtime)
    seed_live_resources(seed_session, spec)
    session = ChatSession(id="error-category-projection-session", tenant_id=value["tenant"]["id"],
        user_id=value["owner"]["id"], agent_id=value["agents"]["SOP"]["id"])
    seed_session.add(session)
    seed_session.add(AgentEvent(tenant_id=session.tenant_id, session_id=session.id,
        event_type="error_occurred", payload_json={
            "code": "LLM_ERROR",
            "message": "PRIVATE_PROVIDER_BODY_CANARY",
            "error_traceback": (
                "Traceback (most recent call last):\n"
                "openai.BadRequestError: PRIVATE_TRACEBACK_CANARY"
            ),
        }))
    seed_session.commit()

    actual = snapshot(seed_session, spec, session_id=session.id)
    error_event = actual["events"][0]

    assert error_event.get("error_code") == "LLM_ERROR"
    assert error_event.get("error_exception_category") == "provider_bad_request"
    serialized = json.dumps(actual, ensure_ascii=False)
    assert "PRIVATE_PROVIDER_BODY_CANARY" not in serialized
    assert "PRIVATE_TRACEBACK_CANARY" not in serialized


def test_snapshot_user_message_keeps_original_time_and_content_digest(owned_spec, seed_session):
    """准确本轮时间直接来自原用户消息；内容摘要保原字节但不公开正文。"""
    from app.db.models import ChatSession, Message

    runtime, value = owned_spec
    spec = validate_spec(value, runtime=runtime)
    seed_live_resources(seed_session, spec)
    session = ChatSession(id="projection-session", tenant_id=value["tenant"]["id"],
        user_id=value["owner"]["id"], agent_id=value["agents"]["SOP"]["id"])
    original_time = {"source_input_id": "projection-source", "timezone": "UTC",
                     "anchor_at_utc": "2026-10-05T10:00:00Z", "local_at": "2026-10-05T10:00:00+00:00"}
    message = Message(id="projection-source", tenant_id=session.tenant_id, session_id=session.id,
        role="user", content="原字节 a  b\n\t末尾", metadata_json={"time_context": original_time})
    seed_session.add(session)
    seed_session.add(message)
    seed_session.commit()
    actual = snapshot(seed_session, spec, session_id=session.id)["messages"][0]
    assert actual["time_context"] == original_time
    assert actual["content_hash"] == actual["content_sha256"] == digest(message.content)
    assert message.content not in json.dumps(actual, ensure_ascii=False)


def test_snapshot_guidance_reads_pinned_revision_and_precise_persistent_use(owned_spec, seed_session):
    """原UI固定版本与消费者Use均需实读；当前新头及外会话不能替代冻结身份。"""
    from app.db.models import AgentResourceBinding, GeneralSkill, GeneralSkillRevision, GeneralSkillUse

    runtime, value = owned_spec
    spec = validate_spec(value, runtime=runtime)
    seed_live_resources(seed_session, spec)
    session, instance = synthetic_execution(seed_session, spec, kind="dynamic_task")
    skill = GeneralSkill(id="projection-guidance", tenant_id=session.tenant_id, slug="original-guidance",
        name="原合成规划指导", skill_markdown="私密原指导正文", status="published",
        usage_mode="planning_guidance", current_published_revision_id="guidance-new-head")
    revision = GeneralSkillRevision(id="guidance-pinned", tenant_id=session.tenant_id, skill_id=skill.id,
        revision_number=1, content_checksum="e" * 64, manifest_checksum="f" * 64,
        normalized_skill_markdown="clarification wait_input completed_steps 私密原指导正文",
        status="published", created_by=value["owner"]["id"])
    head = GeneralSkillRevision(id="guidance-new-head", tenant_id=session.tenant_id, skill_id=skill.id,
        revision_number=2, content_checksum="a" * 64, manifest_checksum="b" * 64,
        normalized_skill_markdown="不同新头", status="published", created_by=value["owner"]["id"])
    binding = AgentResourceBinding(tenant_id=session.tenant_id, agent_id=session.agent_id,
        resource_type="general_skill", resource_id=skill.id,
        metadata_json={"pinned_revision_id": revision.id})
    use = GeneralSkillUse(id="projection-use", tenant_id=session.tenant_id, session_id=session.id,
        turn_id="projection-source", execution_id=instance.id, agent_id=session.agent_id,
        user_id=session.user_id, skill_id=skill.id, revision_id=revision.id, content_checksum=revision.content_checksum,
        selection_mode="auto", status="active", idempotency_key="c" * 64)
    foreign_use = GeneralSkillUse(id="foreign-use", tenant_id="foreign", session_id=session.id,
        turn_id="projection-source", execution_id=instance.id, agent_id=session.agent_id,
        user_id=session.user_id, skill_id=skill.id, revision_id=revision.id, content_checksum=revision.content_checksum,
        selection_mode="auto", status="active", idempotency_key="d" * 64)
    for row in (skill, revision, head, binding, use, foreign_use):
        seed_session.add(row)
    seed_session.commit()
    result = snapshot(seed_session, spec, session_id=session.id)
    guidance = result["guidance_resources"]
    assert len(guidance) == 1
    assert guidance[0]["agent_id"] == session.agent_id
    assert guidance[0]["revision_id"] == revision.id
    assert guidance[0]["content_checksum"] == revision.content_checksum
    assert guidance[0]["native_wait_contract"] is True
    assert guidance[0]["usage_mode"] == "planning_guidance"
    assert [row["id"] for row in result["guidance_uses"]] == [use.id]
    assert result["guidance_uses"][0]["execution_id"] == instance.id
    assert "私密原指导正文" not in json.dumps(result, ensure_ascii=False)


def test_snapshot_no_match_keeps_original_result_and_versions_without_fabricating_citations(owned_spec, seed_session):
    """无答案原data没有document id，仍保留精确结果与合法版本，不补造证据引用。"""
    from app.db.models import SopOperation

    runtime, value = owned_spec
    spec = validate_spec(value, runtime=runtime)
    initial = seed_live_resources(seed_session, spec)
    session, instance = synthetic_execution(seed_session, spec, kind="dynamic_task")
    version_id = initial["knowledge"]["old_version_id"]
    result = {"data": {"outcome": "no_match", "degraded": True,
        "knowledge_base_version_ids": [version_id], "chunks": [], "evidence_pack": [],
        "okf_citations": [], "selected_documents": []}}
    operation = SopOperation(id="projection-knowledge-operation", tenant_id=instance.tenant_id,
        instance_id=instance.id, node_execution_id="projection-knowledge-node", operation_name="knowledge.search",
        idempotency_key="c" * 64, logical_action_id="projection-knowledge-action", request_fingerprint="d" * 64,
        status="succeeded", result_json=result)
    seed_session.add(operation)
    seed_session.commit()
    actual = snapshot(seed_session, spec, session_id=session.id)
    retrieval = actual["operations"][0]["retrieval"]
    assert len(retrieval) == 1
    assert retrieval[0]["outcome"] == "no_match"
    assert retrieval[0]["version_ids"] == [version_id]
    assert retrieval[0]["version_identity_valid"] is True
    assert retrieval[0]["evidence_present"] is False
    assert retrieval[0]["degraded"] is True
    assert "document_id" not in retrieval[0]
    assert actual["messages"] == []


def test_snapshot_marks_original_chunk_and_document_sources_as_identity_valid(owned_spec, seed_session):
    """按原知识回执结构核对chunks.id、chunk_id和文档级来源，不把合法来源判成失配。"""
    from sqlmodel import select
    from app.db.models import KnowledgeChunk, KnowledgeConcept, Message, SopOperation

    runtime, value = owned_spec
    spec = validate_spec(value, runtime=runtime)
    initial = seed_live_resources(seed_session, spec)
    session, instance = synthetic_execution(seed_session, spec, kind="dynamic_task")
    document_id = initial["knowledge"]["old_document_id"]
    version_id = initial["knowledge"]["old_version_id"]
    chunk = seed_session.exec(select(KnowledgeChunk).where(
        KnowledgeChunk.tenant_id == instance.tenant_id,
        KnowledgeChunk.document_id == document_id,
    )).first()
    concept = seed_session.exec(select(KnowledgeConcept).where(
        KnowledgeConcept.tenant_id == instance.tenant_id,
        KnowledgeConcept.knowledge_base_version_id == version_id,
    )).first()
    assert chunk is not None and concept is not None
    operation = SopOperation(id="projection-knowledge-source-identity", tenant_id=instance.tenant_id,
        instance_id=instance.id, node_execution_id="projection-knowledge-source-node",
        operation_name="knowledge.search", idempotency_key="e" * 64,
        logical_action_id="projection-knowledge-source-action", request_fingerprint="f" * 64,
        status="succeeded", result_json={"data": {
            "knowledge_base_version_ids": [version_id],
            "chunks": [{"id": chunk.id, "chunk_index": chunk.chunk_index,
                        "document_id": document_id}],
            "selected_buckets": [{"id": "document-level-bucket", "document_id": document_id}],
            "selected_concepts": [{"source_refs": [{"document_id": document_id}]}],
            "evidence_pack": [{"chunk_id": chunk.id, "document_id": document_id}],
        }})
    seed_session.add(operation)
    seed_session.add(Message(id="projection-knowledge-citation", tenant_id=instance.tenant_id,
        session_id=session.id, role="assistant", content="合成引用投影",
        metadata_json={"execution_id": instance.id, "knowledge_citations": [
            {"id": "kref_concept", "kind": "concept", "concept_id": concept.id},
            {"id": "kref_chunk", "kind": "evidence", "document_id": document_id,
             "chunk_id": chunk.id},
        ]}))
    seed_session.commit()

    actual = snapshot(seed_session, spec, session_id=session.id)
    retrieval = actual["operations"][0]["retrieval"]
    assert len(retrieval) == 4
    assert all(row["identity_valid"] is True for row in retrieval)
    assert all(row["version_ids"] == [version_id] for row in retrieval)
    citations = actual["messages"][0]["citations"]
    assert citations[0]["concept_id"] == concept.id
    assert citations[0]["source_valid"] is True
    assert citations[0]["version_ids"] == [version_id]
    assert citations[1]["source_valid"] is True
    assert citations[1]["version_ids"] == [version_id]


def test_only_authenticated_readonly_private_routes_are_installed(owned_spec, monkeypatch):
    """私有控制只增加GET事实入口，拒绝错误key且不安装本地模型或provider提交。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    runtime, value = owned_spec
    initial = {"provider_kind": "simulated_async_service", "real_vendor_qualified": False}
    path = runtime / "initial.private.json"
    path.write_text(json.dumps(initial))
    path.chmod(0o600)
    app = FastAPI()
    install_api(app, runtime)
    routes = {route.path: route.methods for route in app.routes if route.path.startswith('/__e2e/')}
    assert set(routes) == {"/__e2e/f3/bootstrap", "/__e2e/f3/initial", "/__e2e/f3/facts", "/__e2e/f3/approval/{attention_id}", "/__e2e/f3/sdk-health"}
    assert all(methods == {"GET"} for methods in routes.values())
    client = TestClient(app)
    assert client.get("/__e2e/f3/bootstrap").status_code == 403
    assert client.get("/__e2e/f3/bootstrap", headers={"X-F3-Control-Key": "wrong"}).status_code == 403
    response = client.get("/__e2e/f3/bootstrap", headers={"X-F3-Control-Key": value["control_key"]})
    assert response.json() == initial
    assert int(response.headers["content-length"]) == len(response.content)
    assert "content-encoding" not in response.headers


def test_bootstrap_refuses_oversized_initial_before_returning_json(owned_spec):
    """原私有bootstrap正文超过2MiB时返回固定限额拒绝而不发送大正文。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    runtime, value = owned_spec
    path = runtime / "initial.private.json"
    path.write_bytes(b'{"padding":"' + b"x" * (2 * 1024 * 1024) + b'"}')
    path.chmod(0o600)
    app = FastAPI()
    install_api(app, runtime)

    response = TestClient(app).get("/__e2e/f3/bootstrap",
        headers={"X-F3-Control-Key": value["control_key"]})

    assert response.status_code == 413
    assert response.json() == {"detail": "F3_LIVE_BOOTSTRAP_LIMIT"}
    assert len(response.content) < 1024


@pytest.mark.parametrize("in_flight", [0, 1])
def test_sdk_health_reads_actual_observers_and_requires_control_key(owned_spec, in_flight):
    """只读原观察器当前健康，在途调用不冒充结算，未授权请求不得读观察器。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    runtime, value = owned_spec
    calls = []

    class Observer:
        """纯控制原observer健康接口，不启动SDK或预填业务事实。"""

        def __init__(self, name, health):
            """保本例输入与读次数，不参与真实模型传输。"""
            self.name, self.value = name, health

        def health(self):
            """只读本例真实边界输入，在途标记保持不变。"""
            calls.append(self.name)
            return dict(self.value)

    app = FastAPI()
    sdk = {"valid": in_flight == 0, "in_flight_count": in_flight,
           "started_count": 2, "settled_count": 2 - in_flight}
    wire = {"valid": True, "receipt_count": 2, "error_count": 0}
    app.state.f3_sdk_observation_ledger = Observer("sdk", sdk)
    app.state.f3_live_wire_observer = Observer("wire", wire)
    install_api(app, runtime)
    client = TestClient(app)
    assert client.get("/__e2e/f3/sdk-health").status_code == 403
    assert calls == []
    response = client.get("/__e2e/f3/sdk-health", headers={"X-F3-Control-Key": value["control_key"]})
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["original_sdk"] == sdk
    assert response.json()["wire_inputs"] == wire
    assert calls == ["sdk", "wire"]


def test_sdk_health_does_not_invent_missing_observer(owned_spec):
    """观察器未安装时明确拒绝，不能以零调用或文件存在伪称可安全重启。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    runtime, value = owned_spec
    app = FastAPI()
    install_api(app, runtime)
    response = TestClient(app).get("/__e2e/f3/sdk-health", headers={"X-F3-Control-Key": value["control_key"]})
    assert response.status_code == 503


def seed_identity_observation_rows(db, runtime, *, tenant, owner, agents, version, mode):
    """映射控制种合成历史行及SDK旁证，外scope和其他实例不能借当前指针补身份。"""
    from app.db.models import (
        ChatSession, ExecutionPlanRevision, ExecutionSignal, Message, SkillVersion, SopInstance,
    )

    sources, executions = {}, {}
    for branch in ("SOP", "DTA", "DTA-other"):
        kind = "sop" if branch == "SOP" else "dynamic_task"
        session_id, source_id, execution_id = (
            "identity-session-" + branch, "identity-source-" + branch,
            "identity-execution-" + branch,
        )
        agent = agents["SOP" if branch == "SOP" else "DTA"]
        db.add(ChatSession(id=session_id, tenant_id=tenant, user_id=owner, agent_id=agent))
        db.add(Message(id=source_id, tenant_id=tenant, session_id=session_id, role="user",
                       content="只读身份合成输入", metadata_json={"client_turn_id": source_id}))
        execution = SopInstance(id=execution_id, tenant_id=tenant, session_id=session_id,
            agent_id=agent, initiator_user_id=owner, kind=kind, status="waiting",
            active_slot_key=session_id, source_ref=source_id)
        if kind == "sop":
            execution.skill_id, execution.skill_version_id = version.skill_id, version.id
            execution.skill_version = version.version
            execution.definition_checksum = version.compiled_definition_checksum
        else:
            execution.goal_snapshot_json, execution.capability_snapshot_json = {}, {}
            execution.current_plan_revision_id = (
                "identity-plan-current" if branch == "DTA" else "identity-plan-other"
            )
            execution.current_plan_checksum = "b" * 64
        db.add(execution)
        sources[branch], executions[branch] = source_id, execution
        db.add(ExecutionSignal(id="identity-signal-" + branch, tenant_id=tenant,
            execution_id=execution_id, signal_type="attention_decided", dedupe_key=branch,
            causation_type="attention", causation_id="identity-attention-" + branch,
            payload_checksum="c" * 64))
    for identity, branch, revision, status, parent in (
        ("identity-plan-old", "DTA", 1, "superseded", None),
        ("identity-plan-current", "DTA", 2, "active", "identity-plan-old"),
        ("identity-plan-other", "DTA-other", 1, "active", None),
    ):
        db.add(ExecutionPlanRevision(id=identity, tenant_id=tenant,
            execution_id=executions[branch].id, revision_number=revision, status=status,
            parent_revision_id=parent, plan_json={}, checksum="b" * 64,
            capability_snapshot_json={}, capability_checksum="d" * 64))
    outside_tenant = "identity-foreign-tenant" if mode == "foreign_tenant" else tenant
    db.add(ExecutionPlanRevision(id="identity-plan-foreign", tenant_id=outside_tenant,
        execution_id="identity-execution-unregistered", revision_number=1, plan_json={},
        checksum="e" * 64, capability_snapshot_json={}, capability_checksum="f" * 64))
    db.add(ExecutionSignal(id="identity-signal-foreign", tenant_id=outside_tenant,
        execution_id="identity-execution-unregistered", signal_type="attention_decided",
        dedupe_key="foreign", causation_type="attention", causation_id="identity-attention-foreign",
        payload_checksum="a" * 64))
    db.add(SkillVersion(id="identity-version-foreign", tenant_id=outside_tenant,
        skill_id="identity-unregistered-skill", scope_agent_id="identity-unregistered-agent",
        version="99.0.0", name="只读外范围版本", content_json={}, status="published",
        compiled_definition_checksum="a" * 64))
    db.commit()
    receipts, wires, expected = [], [], {}
    for branch in ("SOP", "DTA"):
        execution = executions[branch]
        extra = {"plan_id": "identity-plan-old" if branch == "DTA" else None,
                 "skill_version_id": version.id if branch == "SOP" else None,
                 "signal_id": "identity-signal-" + branch}
        expected[branch] = extra if mode == "matched" else dict.fromkeys(extra)
        if mode == "foreign_tenant":
            extra = {"plan_id": "identity-plan-foreign",
                     "skill_version_id": "identity-version-foreign",
                     "signal_id": "identity-signal-foreign"}
        elif mode == "other_instance":
            extra = {
                "plan_id": "identity-plan-other" if branch == "DTA" else "identity-plan-old",
                "skill_version_id": version.id if branch == "DTA" else "identity-version-foreign",
                "signal_id": "identity-signal-DTA-other" if branch == "DTA" else "identity-signal-DTA",
            }
        identity = {"tenant_id": tenant, "user_id": owner, "agent_id": execution.agent_id,
                    "session_id": execution.session_id, "instance_id": execution.id,
                    "source_input_id": sources[branch], "turn_id": sources[branch], **extra}
        hashes = {key: digest(item) for key, item in identity.items() if item is not None}
        request = digest("identity-request-" + branch)
        receipt = {"pid": 12345, "identity_sha256": hashes, "request_sha256": request,
            "observation_valid": True, "status": "completed",
            "usage": {"prompt_tokens": 23, "completion_tokens": 5, "total_tokens": 28},
            "phase_sha256": digest("router.scene" if branch == "SOP" else "dynamic_task.answer")}
        receipts.append(receipt)
        wires.append({"pid": 12345, "identity_sha256": hashes, "sdk_request_sha256": request,
                      "phase_sha256": receipt["phase_sha256"], "actual_provider_call": True,
                      "status": "completed", "selected": []})
    for name, rows in (("sdk-observations-12345.safe.jsonl", receipts),
                       ("live-wire-12345.private.jsonl", wires),
                       ("memory-wire-12345.safe.jsonl", wires)):
        path = runtime / name
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        path.chmod(0o600)
    return {"sources": sources, "executions": executions, "expected": expected}


@pytest.mark.parametrize("mode", ["matched", "foreign_tenant", "other_instance"])
def test_snapshot_decodes_original_historical_plan_version_and_signal_without_foreign_fallback(
    owned_spec, seed_session, mode,
):
    """原SDK历史plan/冻结版本/恢复Signal精确解码，外scope与另一合法实例不得补位。"""
    from sqlmodel import select
    from app.db.models import SkillVersion

    runtime, value = owned_spec
    spec = validate_spec(value, runtime=runtime)
    seed_live_resources(seed_session, spec)
    version = seed_session.exec(select(SkillVersion).where(
        SkillVersion.tenant_id == value["tenant"]["id"],
        SkillVersion.skill_id == value["skill"]["id"],
    )).one()
    original = seed_identity_observation_rows(seed_session, runtime,
        tenant=value["tenant"]["id"], owner=value["owner"]["id"],
        agents={branch: item["id"] for branch, item in value["agents"].items()},
        version=version, mode=mode)
    actual = snapshot(seed_session, spec)
    for branch in ("SOP", "DTA"):
        call = next(row for row in actual["sdk_calls"]
                    if row["source_input_id"] == original["sources"][branch])
        assert {key: call[key] for key in ("plan_id", "skill_version_id", "signal_id")} == (
            original["expected"][branch]
        )
    assert {row["id"] for row in actual["plan_revisions"]} == {
        "identity-plan-old", "identity-plan-current", "identity-plan-other",
    }
    assert "identity-signal-foreign" not in {row["id"] for row in actual["signals"]}


@pytest.mark.parametrize("case", [
    "matched", "missing", "wrong_source", "wrong_session", "wrong_tenant", "wrong_user",
    "wrong_agent", "wrong_assistant", "wrong_assistant_turn",
    "conflicting_assistant_turn_metadata", "duplicate",
])
def test_persistent_sdk_binding_requires_unique_exact_memory_capture_job(case: str):
    """记忆SDK回执只由唯一同租户本人员工且指向精确source turn的持久任务闭合。"""
    from final_f3_live_fixture import _persistent_sdk_bindings

    tenant, owner, agent = "tenant-synthetic", "owner-synthetic", "agent-synthetic"
    source_id, session_id, task_id = "source-synthetic", "session-synthetic", "task-synthetic"
    source = SimpleNamespace(
        id=source_id,
        tenant_id=tenant,
        session_id="foreign-session" if case == "wrong_session" else session_id,
        role="user",
    )
    assistant = SimpleNamespace(
        id="assistant-synthetic",
        tenant_id=tenant,
        session_id=session_id,
        role="assistant",
        metadata_json=(
            {"turn_id": source_id, "user_message_id": "other-turn"}
            if case == "conflicting_assistant_turn_metadata"
            else {"turn_id": "other-turn" if case == "wrong_assistant_turn" else source_id}
        ),
    )
    session = SimpleNamespace(id=session_id, tenant_id=tenant, user_id=owner, agent_id=agent)
    job = SimpleNamespace(
        id=task_id,
        tenant_id="foreign-tenant" if case == "wrong_tenant" else tenant,
        user_id="foreign-user" if case == "wrong_user" else owner,
        agent_id="foreign-agent" if case == "wrong_agent" else agent,
        source_message_id="other-source" if case == "wrong_source" else source_id,
        source_assistant_message_id=(source_id if case == "wrong_assistant" else assistant.id),
    )
    identity = {"tenant_id": tenant, "user_id": owner, "agent_id": agent,
                "session_id": session_id, "source_input_id": source_id,
                "turn_id": source_id, "task_id": task_id}
    hashes = {key: digest(value) for key, value in identity.items()}
    bindings, valid = _persistent_sdk_bindings(
        hashes, identity, tenant=tenant, owner=owner, instances=[], sessions=[session],
        messages=[source, assistant], plans=[], signals=[], versions=[],
        memory_jobs=([] if case == "missing" else [job, job] if case == "duplicate" else [job]),
    )

    assert valid is (case == "matched")
    assert bindings["task_id"] == (task_id if case == "matched" else None)

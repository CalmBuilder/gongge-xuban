"""
@Time       : 2026/09/27
@Author     : zhanglp8181
@File       : test_time_context.py
@CallChain  : pytest → app.core.time_context → 聊天输入时间冻结与上下文投影
@Description: 验证可信输入时间的一次冻结、IANA 时区、DST 偏移和 UTC 回退契约。
"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.core.agent_loop import AgentLoop
from app.core.conversation_context import build_conversation_context
from app.core.context_projection import compact_conversation_context
from app.core.time_context import freeze_time_context
from app.dynamic_tasks.execution_context import ExecutionContextProjection
from app.db.models import Message
from app.llm.stage_protocol import render_stage_user_message, stage_payload
from app.llm.client import LLMClient
from app.session.session_schema import ChatTurnRequest
from app.scheduled_tasks import service as scheduled_service


class _MessageDb:
    """保存测试中 AgentLoop 追加的用户消息。"""

    def __init__(self) -> None:
        """初始化空的消息写入记录。"""

        self.rows: list[object] = []

    def add(self, row: object) -> None:
        """记录 ORM 写入，模拟消息进入当前事务。"""

        self.rows.append(row)


def test_freeze_time_context_keeps_server_anchor_and_dst_offset() -> None:
    """同一服务端锚点按合法 IANA 时区生成含偏移的本地时间。"""

    before_fallback = freeze_time_context(
        source_input_id="message_dst_before",
        client_timezone="America/New_York",
        anchor_at_utc=datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
    )
    after_fallback = freeze_time_context(
        source_input_id="message_dst_after",
        client_timezone="America/New_York",
        anchor_at_utc=datetime(2026, 11, 1, 6, 30, tzinfo=UTC),
    )

    assert before_fallback["schema_version"] == "1"
    assert before_fallback["source_input_id"] == "message_dst_before"
    assert before_fallback["anchor_at_utc"] == "2026-11-01T05:30:00Z"
    assert before_fallback["timezone"] == "America/New_York"
    assert before_fallback["timezone_source"] == "client"
    assert before_fallback["local_at"] == "2026-11-01T01:30:00-04:00"
    assert after_fallback["local_at"] == "2026-11-01T01:30:00-05:00"


@pytest.mark.parametrize("client_timezone", [None, "", "Not/A_Timezone", "UTC+08:00"])
def test_freeze_time_context_invalid_or_missing_timezone_falls_back_to_utc(
    client_timezone: str | None,
) -> None:
    """缺失或非法客户端时区统一使用 UTC，不继承宿主本地时区。"""

    context = freeze_time_context(
        source_input_id="message_utc_fallback",
        client_timezone=client_timezone,
        anchor_at_utc=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
    )

    assert context["timezone"] == "UTC"
    assert context["timezone_source"] == "fallback_utc"
    assert context["local_at"] == "2026-01-02T03:04:05+00:00"


def test_freeze_time_context_is_reusable_without_resampling_anchor() -> None:
    """同步、流式或恢复调用复用同一冻结对象时不得重新采样服务端时间。"""

    context = freeze_time_context(
        source_input_id="message_replayed",
        client_timezone="Asia/Shanghai",
        anchor_at_utc=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
    )

    assert dict(context) == dict(context)
    assert context["source_input_id"] == "message_replayed"
    assert context["anchor_at_utc"] == "2026-01-02T03:04:05Z"


def test_agent_loop_persists_time_context_with_source_user_message() -> None:
    """用户消息元数据必须带服务端来源 ID，供恢复和执行快照关联。"""

    loop = object.__new__(AgentLoop)
    loop.db = _MessageDb()
    request = ChatTurnRequest(
        tenant_id="tenant_demo",
        session_id="session_time_context",
        agent_id="agent_demo",
        message="请安排明天的提醒",
        client_timezone="Asia/Shanghai",
    )

    message, context = loop._append_user_input_message(request, request.session_id)

    assert context["source_input_id"] == message.id
    assert message.metadata_json["time_context"] == context
    assert context["timezone"] == "Asia/Shanghai"


def test_context_uses_frozen_input_time_after_later_assistant_message() -> None:
    """上下文时间应来自本轮用户输入，而不是压缩时最后一条 assistant 时间。"""

    context = build_conversation_context(
        [
            {
                "id": "message_user",
                "role": "user",
                "content": "明天提交",
                "created_at": "2026-01-02T03:04:05",
                "time_context": freeze_time_context(
                    source_input_id="message_user",
                    client_timezone="Asia/Shanghai",
                    anchor_at_utc=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
                ),
            },
            {
                "id": "message_assistant",
                "role": "assistant",
                "content": "稍后处理",
                "created_at": "2026-01-03T12:00:00",
            },
        ],
        token_budget=1_000,
    )

    assert context["metadata"]["time_context"]["source_input_id"] == "message_user"
    assert context["metadata"]["current_turn_time"] == "2026-01-02T11:04:05+08:00"


def test_context_marks_turn_time_missing_when_user_has_no_trusted_time_fact() -> None:
    """历史用户消息缺少可信时间时不得借用后续 assistant 的创建时间。"""

    context = build_conversation_context(
        [
            {
                "id": "message_legacy_user",
                "role": "user",
                "content": "明天提交",
                "created_at": "2026-01-02T03:04:05",
            },
            {
                "id": "message_later_assistant",
                "role": "assistant",
                "content": "稍后处理",
                "created_at": "2026-01-04T12:00:00",
            },
        ],
        token_budget=1_000,
    )

    assert "time_context" not in context["metadata"]
    assert context["metadata"]["current_turn_time"] is None


def test_cross_midnight_and_fresh_session_recovery_keep_input_date_until_new_input(
    tmp_path,
) -> None:
    """跨午夜和新进程式重载后，旧输入仍用原日期，新消息才建立新日期锚点。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'sd3-time-recovery.db'}")
    SQLModel.metadata.create_all(engine)
    first_context = freeze_time_context(
        source_input_id="message_cross_midnight_first",
        client_timezone="Pacific/Honolulu",
        anchor_at_utc=datetime(2026, 9, 28, 9, 59, tzinfo=UTC),
    )
    second_context = freeze_time_context(
        source_input_id="message_cross_midnight_second",
        client_timezone="Pacific/Honolulu",
        anchor_at_utc=datetime(2026, 9, 28, 10, 5, tzinfo=UTC),
    )
    with Session(engine) as db:
        db.add_all(
            [
                Message(
                    id="message_cross_midnight_first",
                    tenant_id="tenant_sd3_time",
                    session_id="session_sd3_time",
                    role="user",
                    content="明天 09:00 执行",
                    metadata_json={"time_context": first_context},
                ),
                Message(
                    id="assistant_cross_midnight",
                    tenant_id="tenant_sd3_time",
                    session_id="session_sd3_time",
                    role="assistant",
                    content="等待到下一步",
                    created_at=datetime(2026, 9, 28, 10, 1, tzinfo=UTC),
                ),
            ]
        )
        db.commit()

    def project_context(db: Session) -> dict[str, object]:
        """把重新打开的账本消息投影成下一阶段可用的上下文。"""

        rows = db.exec(
            select(Message).where(Message.session_id == "session_sd3_time").order_by(Message.created_at)
        ).all()
        return build_conversation_context(
            [
                {
                    "id": row.id,
                    "role": row.role,
                    "content": row.content,
                    "created_at": row.created_at.isoformat(),
                    "time_context": (row.metadata_json or {}).get("time_context"),
                }
                for row in rows
            ],
            token_budget=1_000,
        )

    with Session(engine) as restarted_db:
        recovered = project_context(restarted_db)
        assert recovered["metadata"]["time_context"] == first_context
        assert recovered["metadata"]["current_turn_time"] == first_context["local_at"]
        restarted_db.add(
            Message(
                id="message_cross_midnight_second",
                tenant_id="tenant_sd3_time",
                session_id="session_sd3_time",
                role="user",
                content="改成明天 10:00 执行",
                metadata_json={"time_context": second_context},
            )
        )
        restarted_db.commit()

    with Session(engine) as new_input_db:
        changed = project_context(new_input_db)

    assert changed["metadata"]["time_context"] == second_context
    assert changed["metadata"]["current_turn_time"] == second_context["local_at"]
    assert first_context["local_at"].startswith("2026-09-27T23:59")
    assert second_context["local_at"].startswith("2026-09-28T00:05")
    engine.dispose()


def test_scheduled_detection_uses_the_frozen_input_anchor(monkeypatch: pytest.MonkeyPatch) -> None:
    """调度识别的模型 payload 必须使用输入冻结时刻而非识别时的当前时间。"""

    captured: dict[str, object] = {}
    context = freeze_time_context(
        source_input_id="message_scheduled",
        client_timezone="America/Los_Angeles",
        anchor_at_utc=datetime(2026, 3, 8, 9, 30, tzinfo=UTC),
    )

    class _FakeClient:
        """捕获调度模型请求的最小替身。"""

        def __init__(self, _model_config: object) -> None:
            """接受模型配置但不创建外部客户端。"""

            pass

        def generate_json(self, _system_prompt: str, payload: dict[str, object]) -> dict[str, object]:
            """保存调度识别 payload 并返回可校验的草案。"""

            captured.update(payload)
            return {"should_create": False}

    monkeypatch.setattr(scheduled_service, "model_for_agent", lambda *_args: object())
    monkeypatch.setattr(scheduled_service, "LLMClient", _FakeClient)

    result = scheduled_service._detect_with_llm(
        object(),
        "tenant_demo",
        "agent_demo",
        "每天提醒",
        "America/Los_Angeles",
        time_context=context,
    )

    assert result is not None
    assert captured["now"] == context["local_at"]
    assert captured["default_timezone"] == "America/Los_Angeles"


def test_scheduled_detection_projects_one_anchor_into_schedule_timezone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """调度默认规则改变时仍按同一 UTC 锚点投影到调度时区。"""

    captured: dict[str, object] = {}
    context = freeze_time_context(
        source_input_id="message_schedule_projection",
        client_timezone="Pacific/Honolulu",
        anchor_at_utc=datetime(2026, 1, 2, 7, 30, tzinfo=UTC),
    )

    class _FakeClient:
        """捕获跨时区调度识别请求。"""

        def __init__(self, _model_config: object) -> None:
            """接受模型配置但不创建外部客户端。"""

            pass

        def generate_json(self, _system_prompt: str, payload: dict[str, object]) -> dict[str, object]:
            """记录 now 和默认时区并返回空草案。"""

            captured.update(payload)
            return {"should_create": False}

    monkeypatch.setattr(scheduled_service, "model_for_agent", lambda *_args: object())
    monkeypatch.setattr(scheduled_service, "LLMClient", _FakeClient)

    scheduled_service._detect_with_llm(
        object(),
        "tenant_demo",
        "agent_demo",
        "明天提醒",
        "Asia/Shanghai",
        time_context=context,
    )

    assert captured["default_timezone"] == "Asia/Shanghai"
    assert captured["now"] == "2026-01-02T15:30:00+08:00"


def test_stage_payload_and_later_stage_render_keep_structured_time_context() -> None:
    """阶段循环即使已有模型回执，也必须把同一输入锚点送入下一次 SDK 请求。"""

    context = {
        "messages": [{"role": "user", "content": "查询明天安排"}],
        "metadata": {
            "current_turn_time": "2026-01-02T11:04:05+08:00",
            "time_context": freeze_time_context(
                source_input_id="message_stage",
                client_timezone="Asia/Shanghai",
                anchor_at_utc=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
            ),
        },
    }
    payload = stage_payload(
        phase="Step Agent",
        user_message="查询明天安排",
        conversation_context=context,
        memory_context=[],
        instructions="执行当前步骤。",
        stage_data={"current_step": {"node_id": "start"}},
        output_contract={"reply": "string"},
    )

    serialized = render_stage_user_message(payload, include_turn_header=False)

    assert payload["_agent_stage"]["time_context"]["source_input_id"] == "message_stage"
    assert "本轮时间锚点" in serialized
    assert "message_stage" in serialized


def test_first_stage_render_also_contains_structured_time_contract() -> None:
    """首次 Router/阶段请求不能只显示本地时间，必须带完整可信时间合同。"""

    context = freeze_time_context(
        source_input_id="message_first_stage",
        client_timezone="Asia/Shanghai",
        anchor_at_utc=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
    )
    payload = stage_payload(
        phase="Router",
        user_message="明天处理",
        conversation_context={
            "metadata": {
                "current_turn_time": context["local_at"],
                "time_context": context,
            }
        },
        memory_context=[],
        instructions="选择下一阶段。",
        stage_data={},
        output_contract={"decision": "string"},
    )

    serialized = render_stage_user_message(payload, include_turn_header=True)

    assert "本轮时间锚点" in serialized
    assert "message_first_stage" in serialized
    assert "anchor_at_utc" in serialized
    assert "Asia/Shanghai" in serialized


def test_actual_llm_client_first_stage_sdk_request_contains_time_contract() -> None:
    """真实 LLMClient 渲染到 SDK 的首次阶段消息必须含六字段时间事实。"""

    captured: dict[str, object] = {}

    class _Completions:
        """只替换 OpenAI-compatible SDK 的末端 create 调用。"""

        def create(self, **kwargs: object) -> SimpleNamespace:
            """保存实际 messages 参数并返回合法 JSON。"""

            captured.update(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content='{"decision":"answer_only"}')
                    )
                ]
            )

    context = freeze_time_context(
        source_input_id="message_sdk_first_stage",
        client_timezone="Pacific/Honolulu",
        anchor_at_utc=datetime(2026, 1, 2, 1, 30, tzinfo=UTC),
    )
    client = object.__new__(LLMClient)
    client.client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
    client.model = "synthetic-time-contract"
    client.temperature = 0.0
    client.max_output_tokens = 1024
    payload = stage_payload(
        phase="Router",
        user_message="请确认明天日期",
        conversation_context={
            "messages": [{"role": "user", "content": "请确认明天日期"}],
            "metadata": {"time_context": context, "current_turn_time": context["local_at"]},
        },
        memory_context=[],
        instructions="根据日期处理。",
        stage_data={},
        output_contract={"decision": "string"},
    )

    client.generate_json("synthetic stage", payload)
    messages_text = str(captured["messages"])

    for field in (
        "schema_version",
        "source_input_id",
        "anchor_at_utc",
        "timezone",
        "timezone_source",
        "local_at",
    ):
        assert field in messages_text
    assert context["source_input_id"] in messages_text
    assert context["timezone"] in messages_text


def test_dynamic_execution_projection_accepts_frozen_time_context() -> None:
    """动态执行的 provider 事实投影必须能携带当前输入时间而不降成普通文本。"""

    context = freeze_time_context(
        source_input_id="message_dynamic",
        client_timezone="Asia/Shanghai",
        anchor_at_utc=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
    )

    projection = ExecutionContextProjection(
        execution_id="execution_dynamic",
        execution_revision=1,
        goal={"goal": "形成结论"},
        plan_revision_id="plan_1",
        plan_checksum="checksum_1",
        constraints=(),
        success_criteria=(),
        plan_steps=(),
        completed_steps=(),
        pending_action=None,
        operations=(),
        input_resources=(),
        time_context=context,
    )

    assert projection.time_context["source_input_id"] == "message_dynamic"


def test_context_compaction_preserves_the_input_time_anchor() -> None:
    """上下文触发压缩后仍须把本轮结构化时间传给后续阶段。"""

    time_context = freeze_time_context(
        source_input_id="message_compact_user",
        client_timezone="Asia/Shanghai",
        anchor_at_utc=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
    )
    context = {
        "messages": [
            {"role": "user", "content": "请结合大量历史继续处理"},
            {"role": "assistant", "content": "历史内容 " + ("x" * 6000)},
        ],
        "time_context": time_context,
        "metadata": {
            "estimated_tokens": 99999,
            "token_budget": 4096,
            "time_context": time_context,
        },
    }

    projected = compact_conversation_context(context, token_budget=128)

    assert projected["metadata"]["time_context"]["source_input_id"] == (
        "message_compact_user"
    )

"""
@Time       : 2026/08/13 02:30
@Author     : zhanglp8181
@File       : test_general_skill_s3_runtime.py
@CallChain  : pytest → GeneralSkillRuntimeService → resolver/override/use/resource ledger
@Description: 验证会话 mute、强制/自动加载、幂等、countermand 与资源隔离契约。
"""

from __future__ import annotations

import hashlib
import json

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import (
    AgentProfile,
    AgentResourceBinding,
    ChatSession,
    GeneralSkill,
    GeneralSkillDependency,
    GeneralSkillRevision,
    GeneralSkillUse,
    ModelConfig,
    Tenant,
    User,
)
from app.config import get_settings
from app.core.agent_loop import AgentLoop
from app.general_skills.runtime import GeneralSkillRuntimeError, GeneralSkillRuntimeService
from app.session.session_schema import ChatTurnRequest, RouterDecision


def _checksum(value: object) -> str:
    """生成与生产 resolver 相同的规范 JSON checksum。"""

    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _context(*, invocation_policy: str = "model_allowed"):
    """建立一个用户、分身、会话和 legacy-inline 固定修订。"""

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    db = Session(engine)
    owner = User(
        id="user_runtime_owner",
        tenant_id="tenant_runtime",
        username="runtime-owner",
        role="member",
        password_hash="unused",
    )
    agent = AgentProfile(
        id="agent_runtime",
        tenant_id=owner.tenant_id,
        name="Runtime Agent",
        owner_user_id=owner.id,
        status="active",
    )
    chat = ChatSession(
        id="session_runtime",
        tenant_id=owner.tenant_id,
        user_id=owner.id,
        agent_id=agent.id,
    )
    markdown = "# Refund Review\nFollow the reviewed refund policy."
    reference = "refund threshold: 5000"
    resources = [
        {
            "path": "SKILL.md",
            "checksum": hashlib.sha256(markdown.encode()).hexdigest(),
            "size": len(markdown.encode()),
            "media_type": "text/markdown",
            "legacy_inline": True,
        },
        {
            "path": "references/policy.md",
            "checksum": hashlib.sha256(reference.encode()).hexdigest(),
            "size": len(reference.encode()),
            "media_type": "text/markdown",
            "legacy_inline": True,
        },
    ]
    skill = GeneralSkill(
        id="genskill_runtime",
        tenant_id=owner.tenant_id,
        slug="refund-review",
        name="Refund Review",
        description="Review refund requests.",
        skill_markdown=markdown,
        skill_files_json=[
            {
                "path": "references/policy.md",
                "content": reference,
                "size": len(reference.encode()),
                "mime_type": "text/markdown",
            }
        ],
        status="published",
        usage_mode="planning_guidance",
        owner_user_id=owner.id,
        visibility_scope="agent_private",
    )
    db.add(Tenant(id=owner.tenant_id, name="Runtime Tenant"))
    db.add(owner)
    db.add(agent)
    db.add(chat)
    db.add(skill)
    db.flush()
    revision = GeneralSkillRevision(
        id="gsrev_runtime_one",
        tenant_id=owner.tenant_id,
        skill_id=skill.id,
        revision_number=1,
        content_checksum=_checksum(
            [{"path": item["path"], "checksum": item["checksum"]} for item in resources]
        ),
        manifest_checksum=resources[0]["checksum"],
        normalized_skill_markdown=markdown,
        parsed_metadata_json={"name": skill.name, "description": skill.description},
        resource_manifest_json=resources,
        requested_capabilities_json={
            "allowed_tools": ["crm.order.read"],
            "invocation_policy": invocation_policy,
        },
        source_snapshot_json={"source_kind": "legacy_backfill"},
        status="published",
        created_by=owner.id,
    )
    db.add(revision)
    db.flush()
    skill.current_published_revision_id = revision.id
    db.add(skill)
    binding = AgentResourceBinding(
        id="agentres_runtime",
        tenant_id=owner.tenant_id,
        agent_id=agent.id,
        resource_type="general_skill",
        resource_id=skill.id,
        status="active",
        metadata_json={
            "schema_version": 1,
            "revision_policy": "pinned",
            "pinned_revision_id": revision.id,
            "invocation_policy": invocation_policy,
            "atomic_execution_allowed": False,
            "created_by_user_id": owner.id,
        },
    )
    db.add(binding)
    db.commit()
    return db, owner, agent, chat, skill, revision, binding


def test_session_mute_only_narrows_and_cannot_resurrect_inactive_binding() -> None:
    """mute 立即移出目录；恢复继承后若上层停用仍必须 fail-closed。"""

    db, owner, agent, chat, skill, _, binding = _context()
    service = GeneralSkillRuntimeService(db)
    assert [item.skill_id for item in service.session_catalog(
        owner, session_id=chat.id, agent_id=agent.id
    )] == [skill.id]

    muted = service.set_session_enabled(
        owner,
        session_id=chat.id,
        agent_id=agent.id,
        skill_id=skill.id,
        enabled=False,
        expected_row_version=None,
    )
    assert service.session_catalog(owner, session_id=chat.id, agent_id=agent.id) == ()
    binding.status = "inactive"
    db.add(binding)
    db.commit()
    with pytest.raises(GeneralSkillRuntimeError, match="not available"):
        service.set_session_enabled(
            owner,
            session_id=chat.id,
            agent_id=agent.id,
            skill_id=skill.id,
            enabled=True,
            expected_row_version=muted.row_version,
        )


def test_user_only_requires_forced_load_and_replay_reuses_one_use() -> None:
    """user-only 不得自动命中，结构化强制加载在同轮重试时只产生一个 Use。"""

    db, owner, agent, chat, skill, revision, _ = _context(invocation_policy="user_only")
    service = GeneralSkillRuntimeService(db)
    assert service.projected_catalog(
        owner, session_id=chat.id, agent_id=agent.id, query="refund"
    ) == ()
    with pytest.raises(GeneralSkillRuntimeError) as rejected:
        service.load(
            owner,
            session_id=chat.id,
            agent_id=agent.id,
            turn_id="turn_runtime_1",
            skill_id=skill.id,
            selection_mode="auto",
        )
    assert rejected.value.code == "GENERAL_SKILL_USER_ONLY"

    first = service.load(
        owner,
        session_id=chat.id,
        agent_id=agent.id,
        turn_id="turn_runtime_1",
        skill_id=skill.id,
        selection_mode="forced",
    )
    replay = service.load(
        owner,
        session_id=chat.id,
        agent_id=agent.id,
        turn_id="turn_runtime_1",
        skill_id=skill.id,
        selection_mode="forced",
    )
    assert first.use_id == replay.use_id
    assert first.revision_id == revision.id
    assert first.instructions.startswith("# Refund Review")
    assert first.requested_tools == ("crm.order.read",)
    assert len(db.exec(select(GeneralSkillUse)).all()) == 1


def test_projected_catalog_is_stable_and_respects_top_k(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模型目录按匹配度和稳定 ID 排序，并在服务端预算处截断。"""

    db, owner, agent, chat, skill, _, _ = _context()
    settings = get_settings()
    monkeypatch.setattr(settings, "general_skill_catalog_top_k", 1)
    assert [
        item.skill_id
        for item in GeneralSkillRuntimeService(db).projected_catalog(
            owner,
            session_id=chat.id,
            agent_id=agent.id,
            query="refund review",
        )
    ] == [skill.id]


def test_mute_countermands_loaded_use_and_blocks_resource_read() -> None:
    """已进入历史的 guidance 在 mute 后显式失效，不能继续读取固定资源。"""

    db, owner, agent, chat, skill, revision, _ = _context()
    service = GeneralSkillRuntimeService(db)
    loaded = service.load(
        owner,
        session_id=chat.id,
        agent_id=agent.id,
        turn_id="turn_runtime_2",
        skill_id=skill.id,
        selection_mode="forced",
    )
    resource_checksum = str(revision.resource_manifest_json[1]["checksum"])
    first_page, has_more = service.read_resource(
        owner,
        session_id=chat.id,
        use_id=loaded.use_id,
        resource_checksum=resource_checksum,
        offset=0,
        limit=6,
    )
    assert first_page == b"refund"
    assert has_more is True

    service.set_session_enabled(
        owner,
        session_id=chat.id,
        agent_id=agent.id,
        skill_id=skill.id,
        enabled=False,
        expected_row_version=None,
    )
    invalidated = service.invalidate_unavailable(
        owner, session_id=chat.id, agent_id=agent.id
    )
    assert [row.id for row in invalidated] == [loaded.use_id]
    with pytest.raises(GeneralSkillRuntimeError) as rejected:
        service.read_resource(
            owner,
            session_id=chat.id,
            use_id=loaded.use_id,
            resource_checksum=resource_checksum,
        )
    assert rejected.value.code == "GENERAL_SKILL_NOT_AVAILABLE"


def test_resource_read_rejects_unregistered_checksum_and_wrong_session() -> None:
    """资源读取同时绑定 Use manifest 与会话，不接受路径或其他会话枚举。"""

    db, owner, agent, chat, skill, _, _ = _context()
    service = GeneralSkillRuntimeService(db)
    loaded = service.load(
        owner,
        session_id=chat.id,
        agent_id=agent.id,
        turn_id="turn_runtime_3",
        skill_id=skill.id,
        selection_mode="auto",
    )
    with pytest.raises(GeneralSkillRuntimeError) as unregistered:
        service.read_resource(
            owner,
            session_id=chat.id,
            use_id=loaded.use_id,
            resource_checksum="0" * 64,
        )
    assert unregistered.value.code == "GENERAL_SKILL_RESOURCE_NOT_AVAILABLE"
    with pytest.raises(GeneralSkillRuntimeError) as wrong_session:
        service.read_resource(
            owner,
            session_id="session_other",
            use_id=loaded.use_id,
            resource_checksum="0" * 64,
        )
    assert wrong_session.value.code == "GENERAL_SKILL_NOT_AVAILABLE"


def test_tool_authorization_only_narrows_agent_baseline() -> None:
    """Skill allowlist 只收窄 Agent 工具，未归入基线或未声明的动作均 fail-closed。"""

    db, owner, agent, chat, skill, _, _ = _context()
    service = GeneralSkillRuntimeService(db)
    loaded = service.load(
        owner,
        session_id=chat.id,
        agent_id=agent.id,
        turn_id="turn_tool_auth",
        skill_id=skill.id,
        selection_mode="forced",
    )
    service.authorize_tool_for_use(
        owner,
        use_id=loaded.use_id,
        tool_name="crm.order.read",
        baseline_tools={"crm.order.read", "crm.order.write"},
    )
    with pytest.raises(GeneralSkillRuntimeError) as narrowed:
        service.authorize_tool_for_use(
            owner,
            use_id=loaded.use_id,
            tool_name="crm.order.write",
            baseline_tools={"crm.order.read", "crm.order.write"},
        )
    assert narrowed.value.code == "GENERAL_SKILL_TOOL_NOT_AUTHORIZED"
    with pytest.raises(GeneralSkillRuntimeError) as baseline:
        service.authorize_tool_for_use(
            owner,
            use_id=loaded.use_id,
            tool_name="admin.tenant.delete",
            baseline_tools={"crm.order.read"},
        )
    assert baseline.value.code == "GENERAL_SKILL_TOOL_NOT_AUTHORIZED"


def test_dependency_load_requires_exact_approved_revision_edge() -> None:
    """依赖模式不能靠 parent ID 扩权，必须命中同域且允许 user-only 的固定修订边。"""

    db, owner, agent, chat, parent_skill, parent_revision, _ = _context()
    child_skill = GeneralSkill(
        id="genskill_runtime_child",
        tenant_id=owner.tenant_id,
        slug="refund-child",
        name="Refund Child",
        description="Required refund child guidance.",
        skill_markdown="# Child\nCheck evidence.",
        status="published",
        usage_mode="planning_guidance",
        owner_user_id=owner.id,
        visibility_scope="agent_private",
    )
    db.add(child_skill)
    db.flush()
    child_revision = GeneralSkillRevision(
        id="gsrev_runtime_child",
        tenant_id=owner.tenant_id,
        skill_id=child_skill.id,
        revision_number=1,
        content_checksum=_checksum([]),
        manifest_checksum=hashlib.sha256(b"# Child\nCheck evidence.").hexdigest(),
        normalized_skill_markdown="# Child\nCheck evidence.",
        parsed_metadata_json={"name": child_skill.name},
        resource_manifest_json=[],
        requested_capabilities_json={
            "allowed_tools": [],
            "invocation_policy": "user_only",
        },
        source_snapshot_json={"source_kind": "legacy_backfill"},
        status="published",
        created_by=owner.id,
    )
    db.add(child_revision)
    db.flush()
    child_skill.current_published_revision_id = child_revision.id
    db.add(child_skill)
    db.add(
        AgentResourceBinding(
            id="agentres_runtime_child",
            tenant_id=owner.tenant_id,
            agent_id=agent.id,
            resource_type="general_skill",
            resource_id=child_skill.id,
            status="active",
            metadata_json={
                "schema_version": 1,
                "revision_policy": "pinned",
                "pinned_revision_id": child_revision.id,
                "invocation_policy": "user_only",
                "atomic_execution_allowed": False,
                "created_by_user_id": owner.id,
            },
        )
    )
    db.commit()
    service = GeneralSkillRuntimeService(db)
    parent = service.load(
        owner,
        session_id=chat.id,
        agent_id=agent.id,
        turn_id="turn_parent",
        skill_id=parent_skill.id,
        selection_mode="forced",
    )
    with pytest.raises(GeneralSkillRuntimeError) as unapproved:
        service.load(
            owner,
            session_id=chat.id,
            agent_id=agent.id,
            turn_id="turn_child",
            skill_id=child_skill.id,
            selection_mode="dependency",
            parent_skill_use_id=parent.use_id,
        )
    assert unapproved.value.code == "GENERAL_SKILL_DEPENDENCY_NOT_APPROVED"

    db.add(
        GeneralSkillDependency(
            id="gsdep_runtime_child",
            tenant_id=owner.tenant_id,
            parent_skill_id=parent_skill.id,
            parent_revision_id=parent_revision.id,
            child_skill_id=child_skill.id,
            child_revision_id=child_revision.id,
            dependency_kind="required",
            source="human_confirmed",
            allow_user_only=True,
            edge_checksum=_checksum([parent_revision.id, child_revision.id]),
            created_by=owner.id,
        )
    )
    db.commit()
    child = service.load(
        owner,
        session_id=chat.id,
        agent_id=agent.id,
        turn_id="turn_child",
        skill_id=child_skill.id,
        selection_mode="dependency",
        parent_skill_use_id=parent.use_id,
    )
    assert child.selection_mode == "dependency"

    bundle = service.load_bundle(
        owner,
        session_id=chat.id,
        agent_id=agent.id,
        turn_id="turn_bundle",
        skill_id=parent_skill.id,
        selection_mode="forced",
    )
    assert [item.skill_id for item in bundle] == [parent_skill.id, child_skill.id]
    assert [item.selection_mode for item in bundle] == ["forced", "dependency"]

    db.add(
        GeneralSkillDependency(
            id="gsdep_runtime_cycle",
            tenant_id=owner.tenant_id,
            parent_skill_id=child_skill.id,
            parent_revision_id=child_revision.id,
            child_skill_id=parent_skill.id,
            child_revision_id=parent_revision.id,
            dependency_kind="required",
            source="human_confirmed",
            allow_user_only=True,
            edge_checksum=_checksum([child_revision.id, parent_revision.id]),
            created_by=owner.id,
        )
    )
    db.commit()
    with pytest.raises(GeneralSkillRuntimeError) as cycle:
        service.load_bundle(
            owner,
            session_id=chat.id,
            agent_id=agent.id,
            turn_id="turn_cycle",
            skill_id=parent_skill.id,
            selection_mode="forced",
        )
    assert cycle.value.code == "GENERAL_SKILL_DEPENDENCY_CYCLE"


def test_agent_loop_forced_guidance_loads_before_reply_without_running_package_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """结构化强制选择在回复前写 loaded Use，并把 guidance 独立注入模型上下文。"""

    db, owner, agent, chat, skill, revision, _ = _context(invocation_policy="user_only")
    settings = get_settings()
    monkeypatch.setattr(settings, "general_skill_resolver_v2_enabled", True)
    monkeypatch.setattr(settings, "general_skill_dynamic_guidance_enabled", True)
    captured: dict[str, object] = {}

    class ResponseStub:
        """捕获回复阶段上下文，返回确定性用户可见结果。"""

        def generate(self, *args, **_kwargs):
            """断言 guidance 通过独立结构化块进入，而不是拼到用户消息。"""

            captured["message"] = args[0]
            captured["conversation_context"] = args[9]
            return "已依据退款审核 Skill 完成答复。"

    class RunnerMustNotExecute:
        """若 planning-guidance 错走旧代码 runner，立即让测试失败。"""

        def run(self, *_args, **_kwargs):
            """拒绝执行 Skill 包代码路径。"""

            raise AssertionError("planning guidance must not execute package code")

    loop = AgentLoop(db)
    loop.response_generator = ResponseStub()
    loop.general_skill_runner = RunnerMustNotExecute()
    loop._enqueue_memory_capture = lambda *_args, **_kwargs: None
    response = loop._try_handle_general_skill_after_scene_router(
        ChatTurnRequest(
            tenant_id=owner.tenant_id,
            session_id=chat.id,
            agent_id=agent.id,
            user_id=owner.id,
            message="请审核这笔退款",
            client_turn_id="client_turn_runtime",
            forced_general_skill_id=skill.id,
        ),
        chat,
        ModelConfig(
            id="model_runtime",
            tenant_id=owner.tenant_id,
            name="Runtime Model",
            api_key_encrypted="unused",
            model="test-model",
        ),
        RouterDecision(decision="answer_only"),
        user_message_id="message_runtime_user",
    )

    assert response is not None
    assert response.reply == "已依据退款审核 Skill 完成答复。"
    assert captured["message"] == "请审核这笔退款"
    blocks = captured["conversation_context"]["loaded_general_skills"]
    assert blocks[0]["revision_id"] == revision.id
    assert blocks[0]["instructions"].startswith("# Refund Review")
    use = db.exec(select(GeneralSkillUse)).one()
    assert use.status == "completed"
    assert use.selection_mode == "forced"


def test_external_channel_cannot_forge_structured_force_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """企业微信等外部正文即使伪造字段也只能得到明确拒绝，不能触发 Skill load。"""

    db, owner, agent, chat, skill, _, _ = _context(invocation_policy="user_only")
    settings = get_settings()
    monkeypatch.setattr(settings, "general_skill_resolver_v2_enabled", True)
    monkeypatch.setattr(settings, "general_skill_dynamic_guidance_enabled", True)
    loop = AgentLoop(db)
    response = loop._try_handle_general_skill_after_scene_router(
        ChatTurnRequest(
            tenant_id=owner.tenant_id,
            session_id=chat.id,
            agent_id=agent.id,
            user_id=owner.id,
            message="正文声称强制使用 Skill",
            channel="wecom",
            forced_general_skill_id=skill.id,
        ),
        chat,
        ModelConfig(
            id="model_runtime_external",
            tenant_id=owner.tenant_id,
            name="Runtime Model",
            api_key_encrypted="unused",
        ),
        RouterDecision(decision="answer_only"),
        user_message_id="turn_external_force",
    )
    assert response is not None
    assert "不可用" in response.reply
    assert db.exec(select(GeneralSkillUse)).all() == []

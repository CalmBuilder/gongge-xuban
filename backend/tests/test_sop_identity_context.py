"""
@Time       : 2026/07/22 17:30
@Author     : zhanglp8181
@File       : test_sop_identity_context.py
@CallChain  : pytest → DeterministicSopCoordinator → 身份绑定/授权/执行审计
@Description: 验证本人默认、业务角色代办、平台管理员不越权和员工档案缺失路径。
"""

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.traces import _sop_runtime_trace
from app.db.models import (
    BusinessRole,
    ChatSession,
    EmployeeProfile,
    EmployeeRoleAssignment,
    Skill,
    SkillVersion,
    SopInstance,
    SopNodeExecution,
    SopOperation,
    User,
)
from app.session.session_schema import StepAgentResult
from app.sop_runtime.coordinator import DeterministicSopCoordinator
from app.sop_runtime.legacy_skill_card_adapter import compile_legacy_skill_card


def _test_session() -> Session:
    """创建包含身份与 Runtime 表的内存数据库会话。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _identity_content() -> dict[str, object]:
    """构造声明可信员工身份输入的最小确定性 SOP。"""

    return {
        "skill_id": "skill_expense_quota_query",
        "name": "报销额度查询",
        "version": "2.2.0",
        "execution_mode": "deterministic",
        "condition_schemas": {
            "slots": {
                "type": "object",
                "properties": {"employee_id": {"type": "string"}},
            },
            "tool_result": {
                "type": "object",
                "properties": {
                    "expense_quota_query": {
                        "type": "object",
                        "properties": {"status": {"type": "string"}},
                    }
                },
            },
        },
        "nodes": [
            {
                "node_id": "collect_employee",
                "type": "collect_info",
                "name": "解析员工身份",
                "expected_user_info": ["employee_id"],
                "allowed_actions": ["ask_user", "continue_flow"],
                "metadata": {
                    "input_bindings": {
                        "employee_id": {
                            "source": "authenticated_employee",
                            "attribute": "employee_id",
                            "allow_override_roles": ["finance_expense_specialist"],
                            "required_override_permission": "expense.quota.read:any",
                        }
                    }
                },
            },
            {
                "node_id": "query_quota",
                "type": "tool_call",
                "name": "查询额度",
                "allowed_actions": ["call_tool:expense.quota_query"],
                "metadata": {
                    "operation_input": {"employee_id": "slots.employee_id"},
                    "operation_result_key": "expense_quota_query",
                },
            },
            {
                "node_id": "reply_result",
                "type": "response",
                "name": "反馈结果",
                "allowed_actions": ["answer_user"],
            },
        ],
        "edges": [
            {"source_node_id": "collect_employee", "next_node_id": "query_quota"},
            {"source_node_id": "query_quota", "next_node_id": "reply_result"},
        ],
        "start_node_id": "collect_employee",
        "terminal_node_ids": ["reply_result"],
    }


def _seed(
    db: Session,
    *,
    actor_role: str,
    actor_employee_id: str | None = "E001",
    requested_employee_id: str | None = None,
    grant_business_role: bool = False,
    grant_override_permission: bool = True,
) -> tuple[Skill, ChatSession]:
    """写入发布定义、登录账号、可选员工档案和聊天会话。"""

    content = _identity_content()
    definition = compile_legacy_skill_card(content)
    skill = Skill(
        tenant_id="tenant_demo",
        skill_id="skill_expense_quota_query",
        version="2.2.0",
        name="报销额度查询",
        content_json=content,
        status="published",
    )
    version = SkillVersion(
        id="skillver_quota_220",
        tenant_id="tenant_demo",
        skill_id=skill.skill_id,
        version=skill.version,
        name=skill.name,
        content_json=content,
        status="published",
        compiled_definition_checksum=definition.checksum,
    )
    actor = User(
        id="actor_user",
        tenant_id="tenant_demo",
        username="actor",
        role=actor_role,
        password_hash="not-used",
    )
    session = ChatSession(
        id="session_identity",
        tenant_id="tenant_demo",
        user_id=actor.id,
        active_skill_id=skill.skill_id,
        active_step_id="collect_employee",
        slots_json=(
            {"employee_id": requested_employee_id} if requested_employee_id else {}
        ),
    )
    db.add(skill)
    db.add(version)
    db.add(actor)
    db.add(session)
    if actor_employee_id:
        profile = EmployeeProfile(
            tenant_id="tenant_demo",
            user_id=actor.id,
            employee_id=actor_employee_id,
            employee_name="操作人",
        )
        db.add(profile)
        db.flush()
        if grant_business_role:
            role = BusinessRole(
                id="role_finance_expense",
                tenant_id="tenant_demo",
                role_code="finance_expense_specialist",
                name="财务报销专员",
                permissions_json=(
                    ["expense.quota.read:any"] if grant_override_permission else []
                ),
            )
            db.add(role)
            db.add(
                EmployeeRoleAssignment(
                    tenant_id="tenant_demo",
                    employee_profile_id=profile.id,
                    business_role_id=role.id,
                )
            )
    db.commit()
    return skill, session


def test_member_defaults_to_authenticated_employee_and_audits_source() -> None:
    """验证普通成员无需自报工号即可查询本人，并保存身份来源。"""

    with _test_session() as db:
        skill, chat_session = _seed(db, actor_role="member")
        result = DeterministicSopCoordinator(db).prepare_step(
            chat_session,
            skill,
            StepAgentResult(reply="正在查询"),
            user_message="查询我的报销额度",
        )
        db.commit()
        instance = db.exec(select(SopInstance)).one()
        first_execution = db.exec(
            select(SopNodeExecution).where(
                SopNodeExecution.node_id == "collect_employee"
            )
        ).one()

        assert result.tool_call is not None
        assert result.tool_call.arguments == {"employee_id": "E001"}
        assert instance.context_json["identity"]["delegated"] is False
        assert instance.context_json["identity"]["actor_user_id"] == "actor_user"
        assert instance.context_json["identity"]["subject_employee_id"] == "E001"
        assert first_execution.input_json["identity"]["slot_provenance"][
            "employee_id"
        ]["source"] == "authenticated_employee"
        runtime_trace = _sop_runtime_trace(db, "tenant_demo", chat_session.id)
        assert runtime_trace[0]["identity"]["subject_employee_id"] == "E001"
        assert runtime_trace[0]["operations"][0]["request"] == {
            "employee_id": "E001"
        }
        assert runtime_trace[0]["operations"][0]["caused_by_skill_use_id"] is None
        assert runtime_trace[0]["operations"][0]["caused_by_skill_use_ids"] == []


def test_member_cannot_override_subject_employee() -> None:
    """验证普通成员显式输入他人工号也会在调用工具前失败。"""

    with _test_session() as db:
        skill, chat_session = _seed(
            db,
            actor_role="member",
            requested_employee_id="E002",
        )
        db.add(
            EmployeeProfile(
                tenant_id="tenant_demo",
                user_id="other_user",
                employee_id="E002",
            )
        )
        result = DeterministicSopCoordinator(db).prepare_step(
            chat_session,
            skill,
            StepAgentResult(),
            user_message="查询 E002 的报销额度",
        )
        db.commit()
        instance = db.exec(select(SopInstance)).one()

        assert result.reply == "当前员工未被授予该业务角色，只能办理本人业务。"
        assert result.action == "reply"
        assert result.runtime_reply_metadata() == {
            "response_source": "runtime_control",
            "render_policy": "verbatim",
            "runtime_error_code": "SUBJECT_OVERRIDE_FORBIDDEN",
        }
        assert result.slot_updates == {}
        assert chat_session.slots_json["employee_id"] == "E001"
        assert instance.status == "failed"
        assert instance.context_json["identity"]["error_code"] == (
            "SUBJECT_OVERRIDE_FORBIDDEN"
        )
        assert db.exec(select(SopOperation)).all() == []


def test_member_cannot_bypass_override_when_model_omits_explicit_employee() -> None:
    """验证模型漏抽他人工号时，授权层仍从当前消息识别并拒绝代查。"""

    with _test_session() as db:
        skill, chat_session = _seed(
            db,
            actor_role="member",
            requested_employee_id=None,
        )
        db.add(
            EmployeeProfile(
                tenant_id="tenant_demo",
                user_id="other_user",
                employee_id="E002",
            )
        )
        db.commit()

        result = DeterministicSopCoordinator(db).prepare_step(
            chat_session,
            skill,
            StepAgentResult(),
            user_message="员工 E002 的报销额度是多少",
        )

        assert result.action == "reply"
        assert result.reply == "当前员工未被授予该业务角色，只能办理本人业务。"
        assert chat_session.slots_json == {"employee_id": "E001"}
        assert db.exec(select(SopOperation)).all() == []


def test_business_role_holder_can_explicitly_delegate_to_existing_employee() -> None:
    """验证业务角色持有人明确提供有效工号时可以代办并记录双重身份。"""

    with _test_session() as db:
        skill, chat_session = _seed(
            db,
            actor_role="admin",
            requested_employee_id="E002",
            grant_business_role=True,
        )
        db.add(
            EmployeeProfile(
                tenant_id="tenant_demo",
                user_id="other_user",
                employee_id="E002",
                employee_name="目标员工",
            )
        )
        db.commit()
        result = DeterministicSopCoordinator(db).prepare_step(
            chat_session,
            skill,
            StepAgentResult(),
            user_message="请代查工号 E002 的报销额度",
        )
        db.commit()
        instance = db.exec(select(SopInstance)).one()
        actor_profile = db.exec(
            select(EmployeeProfile).where(EmployeeProfile.user_id == "actor_user")
        ).one()
        subject_profile = db.exec(
            select(EmployeeProfile).where(EmployeeProfile.user_id == "other_user")
        ).one()

        assert result.tool_call is not None
        assert result.tool_call.arguments == {"employee_id": "E002"}
        assert instance.context_json["identity"] == {
            "actor_user_id": "actor_user",
            "actor_role": "admin",
            "actor_business_roles": ["finance_expense_specialist"],
            "actor_business_permissions": ["expense.quota.read:any"],
            "actor_employee_profile_id": actor_profile.id,
            "actor_employee_id": "E001",
            "subject_user_id": "other_user",
            "subject_employee_profile_id": subject_profile.id,
            "subject_employee_id": "E002",
            "delegated": True,
            "slot_provenance": {
                "employee_id": {
                    "source": "explicit_delegated_subject",
                    "attribute": "employee_id",
                    "mode": "delegated",
                }
            },
        }


def test_business_role_without_required_permission_cannot_override_subject() -> None:
    """验证只有角色名称但没有流程声明权限点时不能代查他人。"""

    with _test_session() as db:
        skill, chat_session = _seed(
            db,
            actor_role="member",
            requested_employee_id="E002",
            grant_business_role=True,
            grant_override_permission=False,
        )
        db.add(
            EmployeeProfile(
                tenant_id="tenant_demo",
                user_id="other_user",
                employee_id="E002",
            )
        )
        db.commit()

        result = DeterministicSopCoordinator(db).prepare_step(
            chat_session,
            skill,
            StepAgentResult(),
            user_message="请代查工号 E002 的报销额度",
        )

        assert result.reply == "当前员工角色未包含该业务权限，只能办理本人业务。"
        assert result.runtime_reply_metadata()["runtime_error_code"] == (
            "SUBJECT_OVERRIDE_PERMISSION_REQUIRED"
        )
        assert db.exec(select(SopOperation)).all() == []


def test_business_role_override_must_be_explicit_in_current_turn() -> None:
    """验证业务角色持有人也不能执行模型凭空生成的他人工号。"""

    with _test_session() as db:
        skill, chat_session = _seed(
            db,
            actor_role="admin",
            requested_employee_id="E002",
            grant_business_role=True,
        )
        result = DeterministicSopCoordinator(db).prepare_step(
            chat_session,
            skill,
            StepAgentResult(),
            user_message="查询我的报销额度",
        )

        assert result.reply == "代办查询必须明确提供目标员工工号，请重新说明后再试。"
        assert db.exec(select(SopOperation)).all() == []


def test_platform_admin_without_business_role_cannot_override_subject() -> None:
    """验证平台管理员不会自动获得查询他人额度的业务权限。"""

    with _test_session() as db:
        skill, chat_session = _seed(
            db,
            actor_role="admin",
            requested_employee_id="E002",
            grant_business_role=False,
        )
        result = DeterministicSopCoordinator(db).prepare_step(
            chat_session,
            skill,
            StepAgentResult(),
            user_message="请代查工号 E002 的报销额度",
        )

        assert result.reply == "当前员工未被授予该业务角色，只能办理本人业务。"
        assert db.exec(select(SopOperation)).all() == []


def test_missing_employee_profile_fails_without_asking_for_untrusted_id() -> None:
    """验证账号未绑定员工档案时阻断流程，而不是接受用户自报工号。"""

    with _test_session() as db:
        skill, chat_session = _seed(
            db,
            actor_role="member",
            actor_employee_id=None,
        )
        result = DeterministicSopCoordinator(db).prepare_step(
            chat_session,
            skill,
            StepAgentResult(),
            user_message="我的工号是 E001，查询额度",
        )

        assert result.reply == (
            "当前账号尚未绑定有效员工档案，请联系管理员完善工号后再试。"
        )
        assert db.exec(select(SopOperation)).all() == []

"""
@Time       : 2026/08/02 14:18
@Author     : zhanglp8181
@File       : test_web_search_migration.py
@CallChain  : pytest → 联网查询迁移服务 → 编译器/确定性分支/不可变版本
@Description: 验证联网查询条件升级、成功失败路由、派生快照和重复执行幂等。
"""

from copy import deepcopy

from sqlmodel import SQLModel, Session, create_engine, select

from app.db.models import Skill, SkillVersion
from app.sop_runtime import compile_legacy_skill_card
from app.sop_runtime.scheduler import RuntimeAction, plan_next_action
from app.sop_runtime.versioning import write_skill_version
from app.sop_runtime.web_search_migration import apply_web_search_condition_upgrade


def _source_content() -> dict[str, object]:
    """构造与真实联网查询 1.1.0 相同节点和旧字符串分支的最小定义。"""

    return {
        "skill_id": "web_search",
        "version": "1.1.0",
        "name": "联网信息查询",
        "description": "查询公开网络信息并返回来源。",
        "business_domain": "信息检索",
        "required_info": ["search_query"],
        "start_node_id": "collect_search_need",
        "terminal_node_ids": ["format_and_reply", "failure_handling"],
        "execution_mode": "legacy",
        "condition_schemas": {},
        "nodes": [
            {
                "node_id": "collect_search_need",
                "name": "收集搜索需求",
                "type": "collect_info",
                "optional": False,
                "condition": "search_query 为空",
                "instruction": "只收集缺失的搜索主题。",
                "retry_policy": {},
                "allowed_actions": ["ask_user", "continue"],
                "knowledge_scope": {},
                "expected_user_info": ["search_query"],
            },
            {
                "node_id": "execute_web_search",
                "name": "执行网络查询",
                "type": "tool_call",
                "optional": False,
                "condition": "search_query 已收集且非空",
                "instruction": "调用网络查询工具。",
                "retry_policy": {},
                "allowed_actions": ["call_tool:网络查询"],
                "knowledge_scope": {},
                "expected_user_info": [],
                "metadata": {"operation_result_key": "web_search_result"},
            },
            {
                "node_id": "format_and_reply",
                "name": "整理并回复",
                "type": "response",
                "optional": False,
                "condition": "工具返回结果且成功",
                "instruction": "依据工具回执回答。",
                "retry_policy": {},
                "allowed_actions": ["answer_user"],
                "knowledge_scope": {},
                "expected_user_info": [],
            },
            {
                "node_id": "failure_handling",
                "name": "查询失败处理",
                "type": "response",
                "optional": False,
                "condition": "工具调用失败",
                "instruction": "明确告知失败，不虚构结果。",
                "retry_policy": {},
                "allowed_actions": ["answer_user"],
                "knowledge_scope": {},
                "expected_user_info": [],
            },
        ],
        "edges": [
            {
                "source_node_id": "collect_search_need",
                "next_node_id": "execute_web_search",
                "condition": "search_query 已收集且非空",
                "priority": 1,
            },
            {
                "source_node_id": "execute_web_search",
                "next_node_id": "format_and_reply",
                "condition": "工具调用成功",
                "priority": 1,
            },
            {
                "source_node_id": "execute_web_search",
                "next_node_id": "failure_handling",
                "condition": "工具调用失败",
                "priority": 2,
            },
        ],
    }


def _session() -> Session:
    """创建只含联网查询发布头及来源快照的隔离 SQLite 会话。"""

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    db = Session(engine)
    content = _source_content()
    skill = Skill(
        id="skill_web_search",
        tenant_id="tenant_demo",
        skill_id="web_search",
        version="1.1.0",
        name="联网信息查询",
        business_domain="信息检索",
        description="查询公开网络信息并返回来源。",
        content_json=content,
        status="published",
    )
    db.add(skill)
    db.flush()
    write_skill_version(db, skill, compiled_definition=compile_legacy_skill_card(content))
    db.commit()
    return db


def test_web_search_upgrade_creates_restricted_derived_version_and_routes_receipts() -> None:
    """验证迁移后零诊断，且工具成功和失败回执进入各自唯一分支。"""

    with _session() as db:
        source = db.exec(
            select(SkillVersion).where(
                SkillVersion.skill_id == "web_search",
                SkillVersion.version == "1.1.0",
            )
        ).one()
        source_content = deepcopy(source.content_json)

        report = apply_web_search_condition_upgrade(db, tenant_id="tenant_demo")
        db.commit()

        assert report.migrated is True
        skill = db.get(Skill, "skill_web_search")
        assert skill is not None
        assert skill.version == "1.1.1"
        compiled = compile_legacy_skill_card(skill.content_json)
        assert compiled.diagnostics == ()
        succeeded = plan_next_action(
            compiled,
            current_node_id="execute_web_search",
            slots={"search_query": "测试"},
            tool_results={"web_search_result": {"status": "succeeded"}},
        )
        failed = plan_next_action(
            compiled,
            current_node_id="execute_web_search",
            slots={"search_query": "测试"},
            tool_results={"web_search_result": {"status": "failed"}},
        )
        assert succeeded.action is RuntimeAction.ADVANCE
        assert succeeded.next_node_id == "format_and_reply"
        assert failed.action is RuntimeAction.ADVANCE
        assert failed.next_node_id == "failure_handling"
        target = db.exec(
            select(SkillVersion).where(
                SkillVersion.skill_id == "web_search",
                SkillVersion.version == "1.1.1",
            )
        ).one()
        assert target.derived_from_version_id == source.id
        assert source.content_json == source_content


def test_web_search_upgrade_is_exactly_idempotent() -> None:
    """验证重复迁移不新增版本、不改写发布头时间或条件内容。"""

    with _session() as db:
        first = apply_web_search_condition_upgrade(db, tenant_id="tenant_demo")
        db.commit()
        skill = db.get(Skill, "skill_web_search")
        assert skill is not None
        before = (skill.version, deepcopy(skill.content_json), skill.updated_at)
        version_count = len(db.exec(select(SkillVersion)).all())

        second = apply_web_search_condition_upgrade(db, tenant_id="tenant_demo")
        db.commit()
        db.refresh(skill)

        assert first.migrated is True
        assert second.already_migrated is True
        assert second.migrated is False
        assert (skill.version, skill.content_json, skill.updated_at) == before
        assert len(db.exec(select(SkillVersion)).all()) == version_count

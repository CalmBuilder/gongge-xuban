"""
@Time       : 2026/08/15 01:05
@Author     : zhanglp8181
@File       : test_prepare_openworker_runtime_demo.py
@CallChain  : pytest → OpenWorker 运行时演示准备器 → 用户/工具所有权门禁
@Description: 防止验收准备器取消其他用户执行或覆盖同租户同名业务工具。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from app.db.models import ChatSession, SopInstance, Tool


ROOT = Path(__file__).resolve().parents[2]


def test_runtime_demo_cleanup_requires_matching_execution_and_session_user() -> None:
    """同租户同标题也必须同时属于目标执行发起人和会话用户。"""

    module = _load_script()
    instance = SopInstance(
        tenant_id="tenant_demo",
        session_id="session-owned",
        kind="dynamic_task",
        initiator_user_id="user_demo",
    )
    owned_session = ChatSession(
        tenant_id="tenant_demo",
        user_id="user_demo",
        agent_id="agent-demo",
        title="运行中增加 Skill 正向场景",
    )
    foreign_session = ChatSession(
        tenant_id="tenant_demo",
        user_id="user_other",
        agent_id="agent-other",
        title="运行中增加 Skill 正向场景",
    )

    assert module._is_owned_runtime_demo(
        instance=instance,
        session=owned_session,
        user_id="user_demo",
    )
    assert not module._is_owned_runtime_demo(
        instance=instance,
        session=foreign_session,
        user_id="user_demo",
    )
    instance.initiator_user_id = "user_other"
    assert not module._is_owned_runtime_demo(
        instance=instance,
        session=owned_session,
        user_id="user_demo",
    )


def test_runtime_demo_rejects_same_name_tool_without_managed_identity() -> None:
    """同名业务工具若没有准备器身份标记和旧版精确签名，必须拒绝覆盖。"""

    module = _load_script()
    business_tool = Tool(
        id="tool-business",
        tenant_id="tenant_demo",
        name="demo.runtime.read_contract",
        display_name="业务合同读取",
        method="GET",
        url="https://business.example/tools/contracts",
    )

    with pytest.raises(RuntimeError, match="拒绝覆盖"):
        module._assert_demo_tool_owned(
            tool=business_tool,
            expected_tool_id="tool_runtime_demo_0",
            user_id="user_demo",
        )


def test_runtime_demo_accepts_only_marked_owner_or_exact_legacy_tool() -> None:
    """允许当前用户的标记工具及一次性旧版迁移签名，拒绝其他标记所有者。"""

    module = _load_script()
    marked = Tool(
        id="tool_runtime_demo_0",
        tenant_id="tenant_demo",
        name="demo.runtime.read_contract",
        display_name="运行时演示读取 1",
        method="GET",
        url="https://example.invalid/runtime-demo",
        config_json={
            "managed_by": module.DEMO_TOOL_MARKER,
            "owner_user_id": "user_demo",
        },
    )
    legacy = Tool(
        id="tool_runtime_demo_0",
        tenant_id="tenant_demo",
        name="demo.runtime.read_contract",
        display_name="运行时演示读取 1",
        method="GET",
        url="https://example.invalid/runtime-demo",
    )

    module._assert_demo_tool_owned(
        tool=marked,
        expected_tool_id="tool_runtime_demo_0",
        user_id="user_demo",
    )
    module._assert_demo_tool_owned(
        tool=legacy,
        expected_tool_id="tool_runtime_demo_0",
        user_id="user_demo",
    )
    marked.config_json["owner_user_id"] = "user_other"
    with pytest.raises(RuntimeError, match="拒绝覆盖"):
        module._assert_demo_tool_owned(
            tool=marked,
            expected_tool_id="tool_runtime_demo_0",
            user_id="user_demo",
        )


def _load_script() -> ModuleType:
    """按路径加载准备器且不执行 main，便于验证纯所有权门禁。"""

    path = ROOT / "scripts/prepare_openworker_runtime_demo.py"
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load script: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

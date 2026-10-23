"""
@Time       : 2026/10/04
@Author     : zhanglp8181
@File       : test_final_f3_server.py
@CallChain  : pytest → F3独立启动计划 → 资格/适配/自有目录拒绝 → 原应用边界
@Description: 无服务/数据库/网络地冻结F3启动控制；合成资格仅作反例，不代表真实供应商验收。
"""

from __future__ import annotations

import builtins
import json
from pathlib import Path
import runpy

import pytest

import start_final_f3_server as starter
from final_f3_preparation import PreparationError


def configuration() -> dict[str, str]:
    """提供无网络测试模型配置；秘密canary只验证输出脱敏，不能用于真实外呼。"""
    return {
        "DEMO_MODEL_BASE_URL": "https://models.example.org/v1",
        "DEMO_MODEL_NAME": "unit_model",
        "DEMO_MODEL_API_KEY": "F3_SERVER_PRIVATE_CANARY",
        "DYNAMIC_TASK_EXTERNAL_WRITE_TENANT_ALLOWLIST": "tenant_synthetic",
        "DYNAMIC_TASK_EXTERNAL_WRITE_AGENT_ALLOWLIST": "agent_synthetic",
        "TOOL_OUTBOUND_ALLOWED_HOSTS": "sandbox.example.org",
    }


def qualification() -> dict[str, object]:
    """合成已核资格绑定，仅用来验证启动边界；不保存任何真实远端结果。"""
    return {
        "schema_version": "final-f3-vendor-qualification-v1",
        "real_provider_qualified": True,
        "profile_sha256": "a" * 64,
        "qualification_evidence_sha256": "b" * 64,
    }


@pytest.fixture
def owned(tmp_path: Path) -> Path:
    """每例只保留本pytest拥有的随机私有目录，数据库文件仍不存在。"""
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    return starter.reserve_sqlite_runtime(private)


def declared_plan(owned: Path, **changes):
    """集中声明合成启动输入，测试修改一项边界时不读取任何实际配置。"""
    values = {
        "runtime": owned, "configuration": configuration(), "profile_sha256": "a" * 64,
        "qualification": qualification(), "adapter_id": "unit_adapter",
    }
    values.update(changes)
    return starter.build_start_plan(**values)


def unit_adapter(plan) -> None:
    """测试适配只接收启动计划；绝不初始化库、安装回复或发起远端任务。"""


def test_import_does_not_load_previous_starter_or_product(monkeypatch) -> None:
    """模块加载本身必须纯标准库，缺条件命令不得间接导入数据库全局engine。"""
    original = builtins.__import__
    forbidden = []

    def guarded(name, *args, **kwargs):
        """只监测新模块导入边界，业务模块一旦出现立即保留实际模块名反例。"""
        if name == "app" or name.startswith("app.") or name in {
            "single_port_app", "uvicorn", "start_fullstack_server", "start_final_f2_server",
            "final_f1_fullstack_fixture", "final_f2_fullstack_fixture",
        }:
            forbidden.append(name)
            raise AssertionError("F3_PREMATURE_PRODUCT_IMPORT")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    runpy.run_path(str(Path(starter.__file__)), run_name="f3_import_only")
    assert forbidden == []


def test_reservation_is_random_private_and_never_creates_database(tmp_path: Path) -> None:
    """隔离根必须随机且700，每次独立；保留目录不等于创建数据库或启动服务。"""
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    first, second = (starter.reserve_sqlite_runtime(private) for _ in range(2))
    assert first != second and first.parent == second.parent == private
    assert first.name.startswith("f3-sqlite-")
    assert first.stat().st_mode & 0o077 == 0
    assert not (first / "journey.sqlite3").exists()
    assert (first / ".f3-owned-runtime.json").stat().st_mode & 0o077 == 0


def test_public_parent_or_symlink_cannot_be_owned_runtime(tmp_path: Path) -> None:
    """不接收公开父目录或路径替身，不能将生产位置靠名字冒充自有运行根。"""
    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    with pytest.raises(PreparationError, match="F3_PRIVATE_OWNED_DIRECTORY_REQUIRED"):
        starter.reserve_sqlite_runtime(public)
    public.chmod(0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(public, target_is_directory=True)
    with pytest.raises(PreparationError, match="F3_PRIVATE_OWNED_DIRECTORY_REQUIRED"):
        starter.reserve_sqlite_runtime(alias)


@pytest.mark.parametrize("receipt", [None, {}, {
    "structural_compatibility_passed": True, "real_provider_qualified": False,
}])
def test_unqualified_profile_never_reaches_application(owned: Path, receipt) -> None:
    """结构声明不能授予sandbox资格，缺资格在应用/DB/uvicorn前拒绝。"""
    with pytest.raises(PreparationError, match="F3_REAL_VENDOR_QUALIFICATION_REQUIRED"):
        declared_plan(owned, qualification=receipt)
    assert not (owned / "journey.sqlite3").exists()


def test_qualification_is_bound_to_exact_profile(owned: Path) -> None:
    """已核证明不能用于另一个供应商配置；profile漂移不得进入服务。"""
    with pytest.raises(PreparationError, match="F3_VENDOR_QUALIFICATION_PROFILE_CHANGED"):
        declared_plan(owned, profile_sha256="c" * 64)


@pytest.mark.parametrize("adapter", [None, "not_implemented"])
def test_missing_vendor_adapter_is_pending_before_product_import(owned: Path, adapter) -> None:
    """真实资格仍不能替代供应商专属准备实现，当前缺适配不启动原应用。"""
    with pytest.raises(PreparationError, match="F3_VENDOR_ADAPTER_NOT_IMPLEMENTED"):
        declared_plan(owned, adapter_id=adapter)
    assert not (owned / "journey.sqlite3").exists()


@pytest.mark.parametrize("key", ["DATABASE_URL", "GONGGE_XUBAN_DOTENV", "APP_SECRET",
                                 "GONGGE_XUBAN_DATA_DIR", "PUBLIC_MOCK_LLM_ENABLED"])
def test_private_configuration_cannot_override_isolation(owned: Path, key: str) -> None:
    """私有配置不能携带业务DB或路径/应用秘密覆盖，外部键不能绕隔离边界。"""
    values = {**configuration(), key: "F3_PRIVATE_CONFIGURATION_CANARY"}
    with pytest.raises(PreparationError, match="F3_SERVER_CONFIGURATION_FIELD_UNSUPPORTED"):
        declared_plan(owned, configuration=values)


def test_complete_synthetic_plan_is_frozen_and_safe(owned: Path, monkeypatch) -> None:
    """仅结构控制下计划使用自有SQLite/关闭mock并冻结配置；摘要和repr不能泄漏正文。"""
    monkeypatch.setitem(starter.VENDOR_ADAPTERS, "unit_adapter", unit_adapter)
    values = configuration()
    plan = declared_plan(owned, configuration=values)
    values["DEMO_MODEL_NAME"] = "changed_after_freeze"
    assert plan.environment["DEMO_MODEL_NAME"] == "unit_model"
    assert plan.environment["DATABASE_URL"] == f"sqlite:///{owned}/journey.sqlite3"
    assert plan.environment["GONGGE_XUBAN_DOTENV"] == str(owned / "isolated.env")
    assert plan.environment["PUBLIC_MOCK_LLM_ENABLED"] == "false"
    assert plan.environment["DYNAMIC_TASK_STANDING_APPROVAL_ENABLED"] == "false"
    assert plan.environment["DYNAMIC_TASK_DESTRUCTIVE_ENABLED"] == "false"
    assert plan.environment["DYNAMIC_TASK_EXTERNAL_WRITE_ENABLED"] == "true"
    assert not (owned / "journey.sqlite3").exists()
    safe = json.dumps(plan.safe_summary()) + repr(plan)
    assert "F3_SERVER_PRIVATE_CANARY" not in safe and "models.example.org" not in safe
    with pytest.raises(TypeError):
        plan.environment["DATABASE_URL"] = "changed"


def test_adapter_removal_after_plan_refuses_start(owned: Path, monkeypatch) -> None:
    """冻结后失去原适配绑定不得加载应用；最终准备不能只信一次早期声明。"""
    monkeypatch.setitem(starter.VENDOR_ADAPTERS, "unit_adapter", unit_adapter)
    plan = declared_plan(owned)
    monkeypatch.delitem(starter.VENDOR_ADAPTERS, "unit_adapter")
    reached = []
    monkeypatch.setattr(starter, "_run_original_application", lambda plan: reached.append(plan))
    with pytest.raises(PreparationError, match="F3_VENDOR_ADAPTER_CHANGED"):
        starter.start_owned_server(plan)
    assert reached == []


def test_existing_journey_is_not_reset_and_requires_reuse(owned: Path, monkeypatch) -> None:
    """再次启动必须明确同库恢复，绝不rmtree或覆盖原旅程数据库。"""
    monkeypatch.setitem(starter.VENDOR_ADAPTERS, "unit_adapter", unit_adapter)
    database = owned / "journey.sqlite3"
    database.write_bytes(b"OWNED_PREVIOUS_JOURNEY_CANARY")
    database.chmod(0o600)
    with pytest.raises(PreparationError, match="F3_EXISTING_JOURNEY_REQUIRES_REUSE"):
        declared_plan(owned)
    plan = declared_plan(owned, reuse=True)
    assert plan.reuse is True and database.read_bytes() == b"OWNED_PREVIOUS_JOURNEY_CANARY"


def test_database_symlink_inserted_after_plan_never_enters_application(
    owned: Path, monkeypatch,
) -> None:
    """计划冻结后SQLite被换成根外链接时，最终启动仍须拒绝而不能只比较URL字符串。"""
    monkeypatch.setitem(starter.VENDOR_ADAPTERS, "unit_adapter", unit_adapter)
    plan = declared_plan(owned)
    outside = owned.parent / "outside-database-canary.txt"
    outside.write_bytes(b"UNTOUCHED_EXTERNAL_CANARY")
    (owned / "journey.sqlite3").symlink_to(outside)
    reached = []
    monkeypatch.setattr(starter, "_run_original_application", lambda value: reached.append(value))
    with pytest.raises(PreparationError, match="F3_PRIVATE_OWNED_DATABASE_REQUIRED"):
        starter.start_owned_server(plan)
    assert reached == [] and outside.read_bytes() == b"UNTOUCHED_EXTERNAL_CANARY"


@pytest.mark.parametrize("directory", ["user-data", "managed-workspaces", "general-skill-objects"])
def test_owned_data_symlink_never_enters_application(
    owned: Path, monkeypatch, directory: str,
) -> None:
    """三个实际可写数据根不得跟随根外链接，缺口在原应用/seed/worker之前阻断。"""
    monkeypatch.setitem(starter.VENDOR_ADAPTERS, "unit_adapter", unit_adapter)
    plan = declared_plan(owned)
    outside = owned.parent / "outside-data"
    outside.mkdir(mode=0o700)
    (owned / directory).symlink_to(outside, target_is_directory=True)
    reached = []
    monkeypatch.setattr(starter, "_run_original_application", lambda value: reached.append(value))
    with pytest.raises(PreparationError, match="F3_OWNED_DATA_PATH_REQUIRED"):
        starter.start_owned_server(plan)
    assert reached == [] and list(outside.iterdir()) == []

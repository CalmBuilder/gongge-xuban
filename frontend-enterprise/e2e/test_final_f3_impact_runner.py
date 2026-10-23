"""
@Time       : 2026/10/05
@Author     : zhanglp8181
@File       : test_final_f3_impact_runner.py
@CallChain  : 正式pytest纯控制 → F3资源包装 → 原F1/F2完整execute与独立业务复核
@Description: 约束自有MySQL仅提供容器，原随机库不重复创建，失败与清理证据不得变绿。
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "docs/StaffDeck能力蒸馏-7adc7c8/evidence"
HARNESS = BASE / "final-f3-20261005/live-simulated-business/execution-harness"
TARGETS = (
    ("JRN01", "sqlite"), ("JRN01", "mysql"),
    ("JRN03", "sqlite"), ("JRN03", "mysql"),
    ("JRN05", "sqlite"), ("JRN05", "mysql"),
    ("JRN06", "sqlite"), ("JRN06", "mysql"),
)


def load_module(name: str):
    """仅加载指定F3源码；缺实现是正式断言失败，不启动Docker、浏览器或模型。"""
    path = HARNESS / (name + ".py")
    assert path.is_file(), "F3_IMPACT_RESOURCE_WRAPPER_REQUIRED"
    spec = importlib.util.spec_from_file_location("f3_impact_test_" + name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def fake_mysql_boundaries(monkeypatch, module, owned: Path) -> list[str]:
    """保留真实OwnedMysql入口，只替换外部Docker/SQL/Alembic边界并记录先后顺序。"""
    from alembic import command
    import sqlalchemy

    events: list[str] = []

    class Socket:
        """使用合成端口，不绑定任何本地监听。"""

        def __enter__(self):
            """提供只用于生成内存连接参数的原socket上下文。"""
            return self

        def __exit__(self, *_args):
            """本控制未创建系统socket，无需实际关闭。"""

        def bind(self, _address):
            """合成端口分配不访问真实服务。"""

        def getsockname(self):
            """返回冻结的合成原生数据库端口。"""
            return "127.0.0.1", 51444

    class Connection:
        """只返回原生版本/UTC与清理计数，不持有数据库连接。"""

        def __enter__(self):
            """进入本例合成只读查询边界。"""
            return self

        def __exit__(self, *_args):
            """合成查询无系统资源需要回收。"""

        def execute(self, query):
            """以真实SQL文本区分版本核验和随机库/账号清理读回。"""
            if "SELECT VERSION()" in str(query):
                return SimpleNamespace(one=lambda: ("8.4.10", "+00:00"))
            events.append("remaining")
            return SimpleNamespace(scalar_one=lambda: 0)

    class Engine:
        """自有容器管理连接的合成引擎，不创建任何业务数据库。"""

        def connect(self):
            """保持原连接上下文接口，结果只用于本例流程控制。"""
            return Connection()

        def dispose(self):
            """记录精确关闭管理连接的时机。"""
            events.append("dispose")

    def create_engine(_url):
        """首次SQL连接之前必须已有同预算可读的容器PID登记。"""
        dependency = json.loads((owned / "owned-dependency-pids.safe.json").read_text())
        assert dependency["label"] == module.LABEL
        assert dependency["pids"] == [os.getpid()]
        assert dependency["pid_start_ticks"][str(os.getpid())] > 0
        assert dependency["container_memory_limit_bytes"] == 512 * 1024 * 1024
        assert dependency["container_memory_swap_limit_bytes"] == 512 * 1024 * 1024
        events.append("connect-after-pid-registration")
        return Engine()

    def docker(arguments, **_kwargs):
        """Docker纯控制只允许本例创建/检查/精确删除，不启动实际容器。"""
        events.append(" ".join(arguments[:3]))
        if arguments[:3] == ["docker", "run", "--detach"]:
            assert "--memory=512m" in arguments and "--memory-swap=512m" in arguments
        return SimpleNamespace(returncode=0, stdout=module.LABEL)

    def inspect(_arguments):
        """使用本pytest进程的真实ticks，仅作登记格式控制，不将它认作供应商。"""
        return json.dumps([{
            "Config": {"Labels": {"codex.scope": module.LABEL}},
            "State": {"Pid": os.getpid()},
            "HostConfig": {"Memory": 512 * 1024 * 1024, "MemorySwap": 512 * 1024 * 1024},
        }]).encode()

    def database_generator():
        """仅模拟原最小账号夹具的创建/关闭，不生成实际随机数据库。"""
        events.append("database-generator-start")
        try:
            yield "mysql+pymysql://synthetic_app:SYNTHETIC@127.0.0.1:51444/synthetic_database"
        finally:
            events.append("database-generator-close")

    def upgrade(_config, revision):
        """只核原Alembic入口及head，不执行迁移。"""
        assert revision == "head"
        events.append("alembic-upgrade")

    monkeypatch.setattr(module.socket, "socket", Socket)
    monkeypatch.setattr(module.subprocess, "run", docker)
    monkeypatch.setattr(module.subprocess, "check_output", inspect)
    monkeypatch.setattr(sqlalchemy, "create_engine", create_engine)
    monkeypatch.setattr(command, "upgrade", upgrade)
    monkeypatch.setitem(sys.modules, "tests.conftest", SimpleNamespace(
        mysql_database_url=SimpleNamespace(__wrapped__=database_generator),
    ))
    monkeypatch.setattr(os, "environ", dict(os.environ))
    return events


def test_container_only_registers_mysql_before_sql_without_second_database(tmp_path, monkeypatch):
    """原execute已管理唯一随机库，容器层须在SQL前计PID且不额外创建库或迁移。"""
    mysql = load_module("live_owned_mysql")
    events = fake_mysql_boundaries(monkeypatch, mysql, tmp_path)
    with mysql.OwnedMysql(tmp_path, bootstrap_database=False) as holder:
        assert holder.admin_url.startswith("mysql+pymysql://")
        assert holder.version == "8.4.10"
        assert holder.url is None and holder.generator is None
        assert holder.remaining()["all_absent"] is True
    assert "connect-after-pid-registration" in events
    assert "database-generator-start" not in events and "alembic-upgrade" not in events
    assert "docker rm --force" in events and "docker volume rm" in events
    raw = "".join(path.read_text() for path in tmp_path.glob("*.safe.json"))
    assert holder.password not in raw and holder.admin_url not in raw


def test_default_owned_mysql_keeps_original_single_fixture_and_alembic(tmp_path, monkeypatch):
    """未指定container-only的原F3调用仍建一次最小账号库、迁移head并精确关闭一次。"""
    mysql = load_module("live_owned_mysql")
    events = fake_mysql_boundaries(monkeypatch, mysql, tmp_path)
    with mysql.OwnedMysql(tmp_path) as holder:
        assert holder.url is not None and holder.generator is not None
        assert holder.admin_url.startswith("mysql+pymysql://")
    assert events.count("database-generator-start") == 1
    assert events.count("database-generator-close") == 1
    assert events.count("alembic-upgrade") == 1
    assert events.index("database-generator-close") < events.index("dispose")


def test_impact_whitelist_preserves_four_complete_journeys_and_dialect_order():
    """受影响复验仅固定四完整组且SQLite在前，不允许JRN08、诊断case或供应商资质。"""
    impact = load_module("run_impact_browser")
    assert tuple(impact.TARGET_SEQUENCE) == TARGETS
    assert impact.BUDGET == 1572864


@pytest.mark.parametrize("journey,dialect", [
    ("JRN02", "sqlite"), ("JRN04", "mysql"), ("JRN07", "sqlite"),
    ("JRN08", "mysql"), ("JRN06 knowledge", "sqlite"), ("JRN01", "postgresql"),
])
def test_invalid_target_is_rejected_before_original_runner_loading(journey, dialect):
    """白名单外目标必须在加载原运行器和资源创建前拒绝，不拓宽本轮验收范围。"""
    impact = load_module("run_impact_browser")
    with pytest.raises(ValueError):
        impact.validate_target("i2-impact-synthetic", journey, dialect)


@pytest.mark.parametrize("journey,stage", [
    ("JRN01", "final-f1-20261003"), ("JRN03", "final-f1-20261003"),
    ("JRN05", "final-f2-20261004"), ("JRN06", "final-f2-20261004"),
])
def test_loader_uses_exact_original_execute_and_its_stage_bucket(journey, stage, monkeypatch):
    """原完整execute代码位置和同名checks导入桶须一致，不能复制实现或串用另一阶段。"""
    impact = load_module("run_impact_browser")
    monkeypatch.setattr(sys, "path", list(sys.path))
    for name in ("run_checks_bounded", "formal_child", "run_browser_serial", "verify_h1"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    runner = impact.load_original_runner(journey)
    expected = BASE / stage / "execution-harness/run_browser_serial.py"
    assert Path(runner.__file__).resolve() == expected
    assert Path(runner.execute.__code__.co_filename).resolve() == expected
    assert runner.PRIVATE.name == "gongge-xuban-" + stage
    assert runner.EVIDENCE == BASE / stage


@pytest.mark.parametrize("journey", ["JRN01", "JRN03", "JRN05", "JRN06"])
def test_original_execute_resource_entry_uses_current_guard_without_copying_business(
    tmp_path: Path, monkeypatch, journey: str,
) -> None:
    """原execute的资源入口须复用本轮唯一守卫，传回同轮原桶参数且保原业务代码位置。"""
    impact = load_module("run_impact_browser")
    calls = []
    args, environment = ["synthetic-browser-only"], {"SYNTHETIC": "1"}
    owned, destination = tmp_path / "original-owned", tmp_path / "original-evidence"

    def current_guard(actual_args, actual_environment, actual_owned, actual_destination):
        """仅核原入口参数和退出码传递，不创建浏览器、进程或容器。"""
        calls.append((actual_args, actual_environment, actual_owned, actual_destination))
        return 19

    monkeypatch.setattr(impact, "bounded_child", current_guard)
    runner = impact.load_original_runner(journey)
    assert runner.bounded_browser is current_guard
    assert runner.bounded_browser(args, environment, owned, destination) == 19
    assert calls == [(args, environment, owned, destination)]
    stage = "final-f1-20261003" if journey in {"JRN01", "JRN03"} else "final-f2-20261004"
    assert Path(runner.execute.__code__.co_filename) == BASE / stage / "execution-harness/run_browser_serial.py"


def child_control(tmp_path, monkeypatch, *, outcome: str, dialect: str = "mysql"):
    """执行新child真实控制流程，只在原浏览器边界替换结果并保留容器退出的原语义。"""
    impact = load_module("run_impact_browser")
    owned, destination = tmp_path / "outer-owned", tmp_path / "outer-evidence"
    owned.mkdir(mode=0o700)
    destination.mkdir()
    calls = []

    class Mysql:
        """合成持有者只核container-only入口和原execute结束后的精确finally。"""

        def __init__(self, path, *, bootstrap_database=True):
            """原bucket不能与外层owned同名，容器必须只在预算登记目录持有。"""
            assert path == owned and bootstrap_database is False
            self.admin_url = "SYNTHETIC-MEMORY-ONLY-ADMIN-URL"
            self.version = "8.4.10"
            calls.append("container-created")

        def __enter__(self):
            """原execute之前先进入自己的容器持有范围。"""
            calls.append("container-entered")
            return self

        def __exit__(self, *_args):
            """无论原execute失败、取消或异常都保留实际拥有集清理原件。"""
            calls.append("container-exited")
            (owned / "container-cleanup.safe.json").write_text(json.dumps({
                "container_removed": True, "owned_volume_removed": True,
                "owned_label": "gongge-xuban-final-f3-live",
            }))

        def remaining(self):
            """模拟原execute唯一generator关闭后的独立零残留读回。"""
            assert "original-generator-closed" in calls
            calls.append("remaining-after-original-close")
            return {"all_absent": True, "random_databases_remaining": 0,
                    "minimal_users_remaining": 0}

    runner = SimpleNamespace(
        PRIVATE=tmp_path / "original-private", EVIDENCE=tmp_path / "original-evidence",
    )
    runner.PRIVATE.mkdir()
    runner.EVIDENCE.mkdir()

    def execute(name, journey, current_dialect, **kwargs):
        """保原完整入口参数及失败状态，不产生实际浏览器或预置业务结果。"""
        assert journey == "JRN06" and current_dialect == dialect
        assert (runner.PRIVATE / name) != owned
        assert "diagnostic" not in kwargs or kwargs["diagnostic"] is False
        assert bool(kwargs.get("mysql")) == (dialect == "mysql")
        calls.append("original-execute")
        folder = runner.EVIDENCE / name
        folder.mkdir()
        try:
            if outcome == "exception":
                raise RuntimeError("SYNTHETIC_ORIGINAL_EXECUTE_FAILURE")
            if outcome == "cancel":
                raise SystemExit(143)
            (folder / "result.safe.json").write_text(json.dumps({
                "passed": outcome != "failed", "exit_code": int(outcome == "failed"),
                "journey": journey, "dialect": dialect,
                "complete_journey_group": True, "source_unchanged": True,
            }))
            return int(outcome == "failed")
        finally:
            calls.append("original-generator-closed")
            if dialect == "mysql":
                (folder / "database-cleanup.safe.json").write_text(
                    json.dumps(kwargs["mysql"].remaining()),
                )

    def verify(current, name, journey):
        """原独立复核失效必须仍否决，不能从execute返回零推出业务成立。"""
        assert current is runner and journey == "JRN06"
        calls.append("original-verifier")
        if outcome == "verification-failed":
            raise AssertionError("SYNTHETIC_ORIGINAL_VERIFIER_REJECTED")
        return {"passed": True, "complete_journey_group": True}

    runner.execute = execute
    monkeypatch.setattr(impact, "load_original_runner", lambda _journey: runner)
    monkeypatch.setattr(impact, "OwnedMysql", Mysql)
    monkeypatch.setattr(impact, "verify_original_result", verify)
    return impact, runner, calls, owned, destination


def test_mysql_child_uses_original_execute_and_projects_actual_cleanup(tmp_path, monkeypatch):
    """完整child只持容器，旧verifier兼容字段由真实版本/退出结果映射且原件不覆盖。"""
    impact, runner, calls, owned, destination = child_control(
        tmp_path, monkeypatch, outcome="passed",
    )
    assert impact.run_child("i2-impact-synthetic", "JRN06", "mysql", owned, destination) == 0
    assert calls.count("original-execute") == 1 and calls.count("original-verifier") == 1
    assert calls.index("original-generator-closed") < calls.index("container-exited")
    assert calls.index("container-exited") < calls.index("original-verifier")
    raw = json.loads((owned / "container-cleanup.safe.json").read_text())
    compatible = json.loads(
        (runner.EVIDENCE / "i2-impact-synthetic/container-cleanup.safe.json").read_text(),
    )
    assert "removed" not in raw
    assert compatible["version"] == "8.4.10"
    assert compatible["removed"] is raw["container_removed"]
    assert compatible["owned_volume_removed"] is raw["owned_volume_removed"]


def test_sqlite_child_reuses_original_complete_execute_without_mysql(tmp_path, monkeypatch):
    """SQLite同样在受预算child里运行原完整组，不能创建容器或随机数据库。"""
    impact, _runner, calls, owned, destination = child_control(
        tmp_path, monkeypatch, outcome="passed", dialect="sqlite",
    )
    assert impact.run_child("i2-impact-synthetic", "JRN06", "sqlite", owned, destination) == 0
    assert calls == ["original-execute", "original-generator-closed", "original-verifier"]


@pytest.mark.parametrize("outcome", ["failed", "verification-failed", "exception", "cancel"])
def test_original_failure_or_cancel_always_closes_owned_container_and_stays_failed(
    tmp_path, monkeypatch, outcome,
):
    """原case失败、复核否决、异常和取消都必须先精确清容器，不能改成通过。"""
    impact, _runner, calls, owned, destination = child_control(
        tmp_path, monkeypatch, outcome=outcome,
    )
    try:
        code = impact.run_child("i2-impact-synthetic", "JRN06", "mysql", owned, destination)
    except (RuntimeError, AssertionError, SystemExit):
        code = 1
    assert code != 0
    assert calls.count("container-exited") == 1
    assert calls.index("original-generator-closed") < calls.index("container-exited")
    assert json.loads((owned / "container-cleanup.safe.json").read_text())["container_removed"]


@pytest.mark.parametrize("barrier", [None, "child-failed", "resource-cutoff", "source-changed"])
def test_public_entry_monitors_outer_owned_and_never_overwrites_original_resource_bucket(
    tmp_path, monkeypatch, barrier,
):
    """父入口冻结全源并用原F3总预算监测child，零退出不能掩盖原失败、cutoff或源码漂移。"""
    impact = load_module("run_impact_browser")
    private, public = tmp_path / "private", tmp_path / "public"
    private.mkdir()
    public.mkdir()
    monkeypatch.setattr(impact, "PRIVATE", private)
    monkeypatch.setattr(impact, "EVIDENCE", public)
    snapshots = iter([{"frozen-source": "a" * 64}, {
        "frozen-source": "b" * 64 if barrier == "source-changed" else "a" * 64,
    }])
    monkeypatch.setattr(impact, "snapshot_sources", lambda: next(snapshots))
    observed = []

    def bounded(arguments, environment, owned, destination):
        """保原受监测child接口，只合成完整组状态，不生成浏览器业务证据。"""
        assert owned == private / "i2-impact-synthetic"
        assert destination == public / "i2-impact-synthetic" / "aggregate-resources"
        assert "--child" in arguments and arguments[-2:] == ["JRN06", "sqlite"]
        assert environment["F3_IMPACT_OWNED"] == str(owned)
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "resource-summary.safe.json").write_text(json.dumps({
            "budget_kib": 1572864,
            "cutoff": "owned_tree_above_1_5_gib" if barrier == "resource-cutoff" else None,
        }))
        (owned / "child-result.safe.json").write_text(json.dumps({
            "passed": barrier != "child-failed", "complete_journey_group": True,
            "original_execute_code": int(barrier == "child-failed"),
            "original_verification_passed": barrier != "child-failed",
            "owned_cleanup_verified": True, "real_vendor_qualified": False,
        }))
        observed.append((arguments, environment))
        return 0

    monkeypatch.setattr(impact, "bounded_child", bounded)
    code = impact.execute("i2-impact-synthetic", "JRN06", "sqlite")
    assert (code == 0) is (barrier is None)
    assert len(observed) == 1
    folder = public / "i2-impact-synthetic"
    result = json.loads((folder / "result.safe.json").read_text())
    assert result["passed"] is (barrier is None)
    assert result["real_vendor_qualified"] is False
    assert (folder / "source-start.safe.json").is_file()
    assert (folder / "source-end.safe.json").is_file()


def test_full_source_freeze_includes_current_backend_frontend_and_both_original_harnesses():
    """公共F3来源必须同时冻结实际产品、正式测试及原F1/F2/新包装源码，不只冻入口。"""
    impact = load_module("run_impact_browser")
    sources = impact.snapshot_sources()
    expected = [
        ROOT / "backend/app/sop_runtime/coordinator.py",
        ROOT / "backend/tests/test_final_f3_sop_external_delivery.py",
        ROOT / "frontend-enterprise/src/App.tsx",
        Path(__file__).resolve(),
        BASE / "final-f1-20261003/execution-harness/run_browser_serial.py",
        BASE / "final-f1-20261003/execution-harness/verify_jrn03_business.py",
        BASE / "final-f2-20261004/execution-harness/run_browser_serial.py",
        BASE / "final-f2-20261004/execution-harness/verify_h1.py",
        BASE / "final-f2-20261004/execution-harness/verify_h2.py",
        HARNESS / "run_browser_live.py", HARNESS / "live_owned_mysql.py",
        HARNESS / "run_impact_browser.py",
    ]
    for path in expected:
        assert path.is_file()
        digest = sources.get(str(path)) or sources.get(str(path.relative_to(ROOT)))
        assert isinstance(digest, str) and len(digest) == 64

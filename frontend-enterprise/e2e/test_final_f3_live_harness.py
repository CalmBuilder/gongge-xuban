"""
@Time       : 2026/10/14
@Author     : zhanglp8181
@File       : test_final_f3_live_harness.py
@CallChain  : 正式pytest纯控制 → 自有资源监测异常/清理 → 失败证据与精确拥有集
@Description: 无真实模型外呼验证F3候选异常回收、强约束与来源冻结，不把故障误计为验收通过。
"""

from __future__ import annotations

import importlib.util
import base64
import errno
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "docs/StaffDeck能力蒸馏-7adc7c8/evidence/final-f3-20261005/live-simulated-business/execution-harness"
CURRENT_HARNESS = ROOT / "docs/StaffDeck能力蒸馏-7adc7c8/evidence/final-f3-20261010/execution-harness"
I4_CURRENT_HARNESS = ROOT / "docs/StaffDeck能力蒸馏-7adc7c8/evidence/final-f3-20261012/i4-current-source/execution-harness"
I2_CURRENT_HARNESS = ROOT / "docs/StaffDeck能力蒸馏-7adc7c8/evidence/final-f3-20261014/i2-case-sdk-binding-04/execution-harness"


def module(name: str, harness: Path = HARNESS):
    """加载本轮纯运行器函数，导入不启动服务、不触碰实际数据库。"""
    spec = importlib.util.spec_from_file_location(name, harness / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(harness))
    try:
        spec.loader.exec_module(result)
    finally:
        sys.path.remove(str(harness))
    return result


def seal_candidate_private_tree(path: Path) -> None:
    """将候选归档测试输入设成实际运行合同要求的当前UID 0700/0600。"""
    if not path.exists():
        return
    for item in sorted(path.rglob("*"), key=lambda value: len(value.parts), reverse=True):
        if item.is_symlink():
            continue
        item.chmod(0o700 if item.is_dir() else 0o600)
    path.chmod(0o700)


def test_resource_monitor_exception_stops_owned_child_and_retains_failure(tmp_path: Path, monkeypatch) -> None:
    """半写依赖JSON导致监测异常时，自己的重任务仍终结并写出原失败类别与样本。"""
    runner = module("run_browser_live")
    owned, destination = tmp_path / "owned", tmp_path / "evidence"
    owned.mkdir(mode=0o700)
    destination.mkdir()
    (owned / "owned-dependency-pids.safe.json").write_text("{")
    calls = []

    class Child:
        """纯控制假进程，pid为不会访问的测试编号，无任何真实Popen。"""

        pid = 424242
        returncode = None

        def poll(self):
            """返回本例终结状态，不能制造真实进程。"""
            return self.returncode

        def terminate(self):
            """只记录自有假进程停止要求。"""
            calls.append("terminate")
            self.returncode = -15

        def wait(self, **_kwargs):
            """已停止才允许等待结束，避免测试自己挂住。"""
            assert self.returncode is not None, "F3_RESOURCE_CHILD_NOT_STOPPED"
            return self.returncode

        def kill(self):
            """超时回收仍只改变本例假进程。"""
            calls.append("kill")
            self.returncode = -9

    child = Child()
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *_args, **_kwargs: child)
    monkeypatch.setattr(runner.os, "killpg", lambda pid, _signal: (calls.append("owned_group"), child.terminate()) if pid == child.pid else pytest.fail("FOREIGN_PID"))
    monkeypatch.setattr(runner.os, "kill", lambda pid, _signal: (calls.append("owned_pid"), child.terminate()) if pid == child.pid else pytest.fail("FOREIGN_PID"))
    code = runner.bounded_child(["synthetic-test-only"], {}, owned, destination)
    assert code != 0 and calls
    result = json.loads((destination / "resource-summary.safe.json").read_text())
    assert result["cutoff"] == "resource_monitor_error"
    assert result["monitor_error_class"] == "JSONDecodeError"
    assert (destination / "resource-samples.safe.json").is_file()


def test_registered_run_creates_missing_evidence_parent_before_child_start(
    tmp_path: Path, monkeypatch,
) -> None:
    """清理后公共full20父目录缺失时，仍先建齐归档路径再进入child setup。"""
    runner = module("run_browser_live", I2_CURRENT_HARNESS)
    private_root = tmp_path / "private-root"
    evidence_root = tmp_path / "missing" / "evidence-root"
    monkeypatch.setattr(runner, "PRIVATE", private_root)
    monkeypatch.setattr(runner, "EVIDENCE", evidence_root)

    def stop_after_directories(*_args, **_kwargs):
        """以合成哨兵停止流程，确保本测试不会加载服务、模型或数据库。"""
        raise RuntimeError("TEST_REACHED_CHILD_SETUP")

    monkeypatch.setattr(runner, "load", stop_after_directories)
    previous_path = str(runner.PREVIOUS)
    try:
        with pytest.raises(RuntimeError, match="TEST_REACHED_CHILD_SETUP"):
            runner._execute_registered_live_run(
                "i3-live-synthetic-evidence-parent", "sqlite", None, None, None, 0, 0.0,
            )
    finally:
        if previous_path in sys.path:
            sys.path.remove(previous_path)

    assert (private_root / "i3-live-synthetic-evidence-parent").is_dir()
    assert (evidence_root / "i3-live-synthetic-evidence-parent").is_dir()


def test_h2_editor_prefill_diagnostic_contract_declares_real_model_without_business_dispatch() -> None:
    """H2精确前缀诊断须执行真模型等待，但在知识正文写入和业务提交前停止。"""
    runner = module("run_browser_live", I4_CURRENT_HARNESS)
    registration = module("registered_i4_run", I4_CURRENT_HARNESS)
    selection = "h2-dta-editor-prefill-after-dta-wait"
    case_name = "JRN08 H2后DTA真实旧知识冻结→页面正文发布→真重启恢复旧版→新/未知质量"

    assert selection in runner.SINGLE_CASE_SHARD_NAMES
    assert runner.diagnostic_case_grep(selection) == case_name
    assert runner.expected_case_criteria(selection) == {
        case_name: {"h2-editor-prefill-after-dta-wait-observed"},
    }
    assert runner.diagnostic_environment_flags(selection)["F3_DIAGNOSTIC_H2_EDITOR_PREFILL"] == "1"

    contract = registration._diagnostic_contract_metadata(selection)
    assert contract["real_model_requested"] is True
    assert contract["minimum_model_invocations"] == 1
    assert contract["expected_provider_submissions"] == 0
    assert contract["target_document_business_data_write_expected"] is False


def test_h2_knowledge_case_diagnostic_contract_declares_revision_write_and_zero_business_dispatch() -> None:
    """完整H2单案诊断须允许合成正文修订，同时禁止外部业务提交并保留真实模型路径。"""
    runner = module("run_browser_live", I4_CURRENT_HARNESS)
    registration = module("registered_i4_run", I4_CURRENT_HARNESS)
    selection = "h2-dta-knowledge"
    case_name = "JRN08 H2后DTA真实旧知识冻结→页面正文发布→真重启恢复旧版→新/未知质量"

    assert selection in runner.SINGLE_CASE_SHARD_NAMES
    assert runner.diagnostic_case_grep(selection) == case_name
    assert runner.expected_case_criteria(selection) == {
        case_name: {
            "original-ui-knowledge-guidance-fixed",
            "knowledge-real-body-rebuild-published",
            "actual-owned-app-new-pid",
            "dta-knowledge-real-quality-old",
            "dta-knowledge-real-quality-new",
            "dta-knowledge-real-quality-none",
            "knowledge-quality-has-no-business-write",
        },
    }
    assert runner.diagnostic_environment_flags(selection)["F3_DIAGNOSTIC_H2_EDITOR_CONTEXT"] == "1"
    assert runner.diagnostic_environment_flags(selection)["F3_DIAGNOSTIC_H2_SCOPE_EVENTS"] == "1"

    contract = registration._diagnostic_contract_metadata(selection)
    assert contract["real_model_requested"] is True
    assert contract["minimum_model_invocations"] == 1
    assert contract["expected_provider_submissions"] == 0
    assert contract["target_document_business_data_write_expected"] is True


def resource_proc_race(
    tmp_path: Path, monkeypatch, failure: str, *, sample_limit: int = 1,
    available_kib: int = 4000000, renderer_pss_kib: int = 50000,
    renderer_swap_kib: int = 0, mysql_pss_kib: int | None = None,
):
    """只替换proc/Popen边界，让原监测器经历活进程采样后退出或仍失读的确定性竞态。"""
    runner = module("run_browser_live")
    owned, destination = tmp_path / "owned", tmp_path / "evidence"
    owned.mkdir(mode=0o700)
    destination.mkdir()
    root_pid, renderer_pid, mysql_pid, replacement_pid = 424242, 424243, 424244, 424245
    failed_smaps = False
    sleeps = 0
    reads: dict[str, int] = {}
    killed: list[int] = []

    class Child:
        """原Popen接口的合成子进程，正常采样一轮后结束，不创建任何系统进程。"""

        pid = root_pid
        returncode = None

        def poll(self):
            """仅返回本例内存状态，供原监测循环和finally使用。"""
            return self.returncode

        def wait(self, **_kwargs):
            """监测器必须先决定正常结束或停止，再读取合成退出码。"""
            assert self.returncode is not None, "SYNTHETIC_CHILD_MUST_FINISH"
            return self.returncode

        def terminate(self):
            """记录合成进程被监测器停止，不发送真实信号。"""
            self.returncode = -15

        def kill(self):
            """合成强制回收也不会触碰真实系统进程。"""
            self.returncode = -9

    child = Child()
    if mysql_pss_kib is not None:
        (owned / "owned-dependency-pids.safe.json").write_text(json.dumps({
            "label": "gongge-xuban-final-f3-live", "pids": [mysql_pid],
            "pid_start_ticks": {str(mysql_pid): 11},
        }))

    class ProcPath:
        """仅承接绝对proc路径；其他文件仍由真实tmp_path保存原安全结果。"""

        def __init__(self, value: str):
            """保存纯字符串路径，不打开真实proc目录。"""
            self.value = value
            self.name = value.rsplit("/", 1)[-1]

        def __truediv__(self, value):
            """保持原Path拼接调用，同时锁定合成proc路径。"""
            return ProcPath(self.value + "/" + str(value))

        def iterdir(self):
            """采样树只包含合成父进程及一个可替换renderer。"""
            assert self.value == "/proc"
            pids = [root_pid]
            if not (failure == "disappeared" and failed_smaps):
                pids.append(renderer_pid)
            if mysql_pss_kib is not None:
                pids.append(mysql_pid)
            if failure == "replacement_high_pss" and failed_smaps:
                pids.append(replacement_pid)
            return iter(ProcPath(f"/proc/{pid}") for pid in pids)

        def exists(self):
            """退出目录是否存在与Z/X状态分别控制，不能将存在误当作仍持有地址空间。"""
            return not (failure == "disappeared" and failed_smaps
                        and self.value.startswith(f"/proc/{renderer_pid}"))

        def read_text(self, **_kwargs):
            """先返回活renderer旧RSS，再按失败分支提供真实格式的状态和starttime复核。"""
            nonlocal failed_smaps
            reads[self.value] = reads.get(self.value, 0) + 1
            assert reads[self.value] <= max(10, 8 * sample_limit), "PROC_RESAMPLE_MUST_BE_BOUNDED"
            if self.value == "/proc/meminfo":
                return f"MemAvailable: {available_kib} kB\nSwapFree: 0 kB\nSwapTotal: 0 kB\n"
            pieces = self.value.split("/")
            assert len(pieces) == 4 and pieces[2].isdigit(), "ONLY_FAKE_PROC_READS"
            pid, entry = int(pieces[2]), pieces[3]
            target = pid == renderer_pid
            if target and failed_smaps and failure == "disappeared":
                raise FileNotFoundError(errno.ENOENT, "SYNTHETIC_PROCESS_GONE")
            state = "R"
            if target and failed_smaps:
                state = {"zombie": "Z", "dead": "X", "unknown": "?",
                         "reused_zombie": "Z", "zombie_with_rss": "Z",
                         "replacement_high_pss": "Z"}.get(failure, "R")
            parent = root_pid if target or pid == replacement_pid else 1
            if target and failed_smaps and failure == "reused_zombie":
                parent = 777777
            if target and failed_smaps and failure == "unknown_foreign_pid":
                parent = 1
            if entry == "status":
                if pid == mysql_pid and failure == "mysql_status_permission":
                    raise PermissionError(errno.EACCES, "SYNTHETIC_MYSQL_STATUS_UNREADABLE")
                rss = 1700000 if target else 100000
                lines = f"Name:\tsynthetic\nState:\t{state} (synthetic)\nPPid:\t{parent}\n"
                if state not in {"Z", "X"} or failure == "zombie_with_rss":
                    lines += f"VmRSS:\t{rss} kB\n"
                return lines
            if entry == "stat":
                if target and failed_smaps and failure == "unknown_foreign_pid":
                    raise PermissionError(errno.EACCES, "SYNTHETIC_FOREIGN_STARTTIME_UNREADABLE")
                if pid == mysql_pid and failure == "mysql_identity_permission":
                    raise PermissionError(errno.EACCES, "SYNTHETIC_MYSQL_IDENTITY_UNREADABLE")
                ticks = 22 if target and failed_smaps and failure == "reused_zombie" else 11
                fields = [state, str(parent)] + ["0"] * 17 + [str(ticks)]
                return f"{pid} (synthetic renderer) " + " ".join(fields)
            if entry == "smaps_rollup":
                if target:
                    failed_smaps = True
                    if failure == "missing_pss":
                        return "Private_Clean: 1 kB\nSwapPss: 0 kB\n"
                    if failure == "malformed_pss":
                        return "Pss: malformed kB\nSwapPss: 0 kB\n"
                    if failure not in {"comm_failure", "none", "mysql_status_permission",
                                       "mysql_identity_permission"}:
                        raise PermissionError(errno.EACCES, "SYNTHETIC_SMAPS_UNREADABLE")
                pss = renderer_pss_kib if target else 50000
                if pid == mysql_pid:
                    pss = mysql_pss_kib
                if pid == replacement_pid:
                    pss = 1800000
                swap = renderer_swap_kib if target else 0
                return (f"Pss: {pss} kB\nPrivate_Clean: 1 kB\nPrivate_Dirty: 1 kB\n"
                        f"SwapPss: {swap} kB\n")
            if entry == "comm":
                if target and failure == "comm_failure":
                    raise PermissionError(errno.EACCES, "SYNTHETIC_COMM_UNREADABLE")
                return "synthetic\n"
            raise AssertionError("UNEXPECTED_PROC_ENTRY")

    def proc_path(value):
        """仅proc走纯控制，证据与工作目录仍使用真实临时Path。"""
        return ProcPath(str(value)) if str(value).startswith("/proc") else Path(value)

    def stop(pid, _signal):
        """监测器只可发信号给两个合成拥有PID，绝不允许真实进程被访问。"""
        assert pid in {root_pid, renderer_pid, mysql_pid, replacement_pid}, "FOREIGN_PID_FORBIDDEN"
        killed.append(pid)
        if pid == root_pid:
            child.terminate()

    def finish(_seconds):
        """跳过真实sleep并结束一轮采样后的合成进程。"""
        nonlocal sleeps
        sleeps += 1
        if sleeps >= sample_limit:
            child.returncode = 0

    monkeypatch.setattr(runner, "Path", proc_path)
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *_args, **_kwargs: child)
    monkeypatch.setattr(runner.os, "kill", stop)
    monkeypatch.setattr(runner.os, "killpg", stop)
    monkeypatch.setattr(runner.time, "sleep", finish)
    code = runner.bounded_child(["synthetic-proc-race-only"], {}, owned, destination)
    summary = json.loads((destination / "resource-summary.safe.json").read_text())
    samples = json.loads((destination / "resource-samples.safe.json").read_text())
    assert runner.BUDGET == summary["budget_kib"] == 1572864
    assert (len(samples) == sample_limit
            or not samples and summary["cutoff"] == "resource_monitor_error"), "NO_SAMPLE_GRACE"
    return code, summary, samples[0] if samples else {}, reads, killed


@pytest.mark.parametrize("failure", ["zombie", "dead", "disappeared"])
def test_resource_guard_confirmed_renderer_exit_cannot_charge_old_rss(
    tmp_path: Path, monkeypatch, failure: str,
) -> None:
    """同身份renderer已无地址空间或消失时，不因首快照旧RSS越线停止合法自有任务。"""
    code, summary, sample, _reads, killed = resource_proc_race(tmp_path, monkeypatch, failure)
    assert code == 0 and not killed
    assert summary["cutoff"] is None
    assert sample["pss_complete"] is True
    assert sample["budget_basis"] == "complete_pss_plus_swap_pss"
    assert sample["owned_budget_charge_kib"] == 50000
    assert sample["owned_tree_rss_kib"] == 100000, "RELEASED_RENDERER_RSS_IS_NOT_CURRENT_RSS"
    assert sample["initial_tree_rss_kib"] == 1800000
    assert sample["sampling_passes"] == 2
    assert sample["absolute_monotonic_seconds"] > 0
    assert sample["process_read_diagnostics"] and sample["released_processes"]
    diagnostic = sample["process_read_diagnostics"][0]
    assert diagnostic["read_stage"] == "smaps_rollup"
    assert diagnostic["errno"] == errno.EACCES
    assert diagnostic["identity_before"]["start_ticks"] == 11
    assert diagnostic["identity_before"]["state"] == "R"


@pytest.mark.parametrize("failure", [
    "permission", "missing_pss", "malformed_pss", "unknown", "reused_zombie", "zombie_with_rss",
])
def test_resource_guard_live_unknown_or_reused_pid_keeps_full_tree_rss_fallback(
    tmp_path: Path, monkeypatch, failure: str,
) -> None:
    """活进程失读、未知状态、PID复用或仍有RSS的Z状态，均不得当作零内存绕过原预算。"""
    code, summary, sample, _reads, killed = resource_proc_race(tmp_path, monkeypatch, failure)
    assert code != 0 and killed
    assert summary["cutoff"] == "owned_memory_above_1_5_gib"
    assert sample["pss_complete"] is False
    assert sample["budget_basis"] == "rss_fallback"
    assert sample["owned_tree_rss_kib"] == sample["owned_budget_charge_kib"] == 1800000
    assert sample["sampling_passes"] == 2
    if failure == "reused_zombie":
        assert 424243 not in killed, "REUSED_FOREIGN_PID_MUST_NOT_RECEIVE_SIGNAL"
        assert sample["pid_identity_uncertain"] == [424243]


def test_resource_guard_comm_failure_keeps_successfully_measured_pss(
    tmp_path: Path, monkeypatch,
) -> None:
    """非预算字段comm失读不能丢掉已成功解析的PSS并误触发全树旧RSS截止。"""
    code, summary, sample, _reads, killed = resource_proc_race(tmp_path, monkeypatch, "comm_failure")
    assert code == 0 and not killed
    assert summary["cutoff"] is None
    assert sample["pss_complete"] is True
    assert sample["owned_budget_charge_kib"] == 100000
    assert sample["owned_tree_rss_kib"] == 1800000
    assert sample["sampling_passes"] == 1
    assert any(error["read_stage"] == "comm" and error["budget_measurement_retained"]
               for error in sample["process_read_diagnostics"])


def test_resource_guard_released_renderer_replacement_is_in_same_budget(
    tmp_path: Path, monkeypatch,
) -> None:
    """旧renderer释放后必须完整重枚举，新renderer的高PSS仍触发原1.5GiB截止。"""
    code, summary, sample, _reads, killed = resource_proc_race(
        tmp_path, monkeypatch, "replacement_high_pss",
    )
    assert code != 0 and summary["cutoff"] == "owned_memory_above_1_5_gib"
    assert sample["pss_complete"] is True and sample["sampling_passes"] == 2
    assert sample["owned_budget_charge_kib"] == 1850000
    assert {item["pid"] for item in sample["owned_process_memory"]} == {424242, 424245}
    assert 424245 in killed and 424243 not in killed


def test_resource_guard_keeps_three_consecutive_low_host_samples(
    tmp_path: Path, monkeypatch,
) -> None:
    """完整PSS未超预算时，主机可用内存连续第三个低样本仍必须停止原任务。"""
    code, summary, sample, _reads, killed = resource_proc_race(
        tmp_path, monkeypatch, "none", sample_limit=3, available_kib=1572863,
    )
    assert code != 0 and killed
    assert summary["cutoff"] == "system_available_below_1_5_gib"
    assert summary["sample_count"] == 3
    assert sample["owned_budget_charge_kib"] == 100000


@pytest.mark.parametrize("pss_kib,swap_kib", [(1522865, 0), (50000, 1472865)])
def test_resource_guard_complete_pss_and_swap_still_use_original_budget(
    tmp_path: Path, monkeypatch, pss_kib: int, swap_kib: int,
) -> None:
    """完整PSS或PSS加SwapPSS恰超原阈值也拒绝，不因renderer竞态适配放宽预算。"""
    code, summary, sample, _reads, killed = resource_proc_race(
        tmp_path, monkeypatch, "none", renderer_pss_kib=pss_kib, renderer_swap_kib=swap_kib,
    )
    assert code != 0 and killed
    assert summary["cutoff"] == "owned_memory_above_1_5_gib"
    assert sample["pss_complete"] is True
    assert sample["owned_budget_charge_kib"] == 1572865


def test_resource_guard_registered_mysql_root_is_charged_in_same_budget(
    tmp_path: Path, monkeypatch,
) -> None:
    """非浏览器后代的自有MySQL仍按登记ticks加入同一预算，不能由入口适配排除。"""
    code, summary, sample, _reads, killed = resource_proc_race(
        tmp_path, monkeypatch, "none", mysql_pss_kib=1600000,
    )
    assert code != 0 and summary["cutoff"] == "owned_memory_above_1_5_gib"
    assert sample["owned_mysql_registered"] is True
    assert sample["owned_budget_charge_kib"] == 1700000
    assert any(row["pid"] == 424244 and row["owned_mysql_root"]
               for row in sample["owned_process_memory"])
    assert 424244 in killed


@pytest.mark.parametrize("failure", ["mysql_status_permission", "mysql_identity_permission"])
def test_resource_guard_registered_live_mysql_unreadable_identity_fails_closed(
    tmp_path: Path, monkeypatch, failure: str,
) -> None:
    """已登记且仍存在的MySQL身份或status失读必须拒绝，不能省略daemon让浏览器预算变绿。"""
    code, summary, _sample, _reads, killed = resource_proc_race(
        tmp_path, monkeypatch, failure, mysql_pss_kib=1600000,
    )
    assert code != 0 and killed
    assert summary["cutoff"] == "resource_monitor_error"
    assert summary["monitor_error_class"] is not None


def test_resource_guard_unknown_pid_outside_current_owned_tree_cannot_receive_signal(
    tmp_path: Path, monkeypatch,
) -> None:
    """旧child已离开当前拥有树且ticks失读时仍按旧RSS拒绝，但不能向不明外国PID发停止信号。"""
    code, summary, sample, _reads, killed = resource_proc_race(
        tmp_path, monkeypatch, "unknown_foreign_pid",
    )
    assert code != 0 and summary["cutoff"] == "owned_memory_above_1_5_gib"
    assert sample["pss_complete"] is False and sample["budget_basis"] == "rss_fallback"
    assert sample["owned_budget_charge_kib"] == 1800000
    assert 424242 in killed
    assert 424243 not in killed, "PID_OUTSIDE_CURRENT_OWNED_TREE_MUST_NOT_RECEIVE_SIGNAL"


@pytest.mark.parametrize("failure", ["save", "dispose"])
def test_mysql_cleanup_io_errors_cannot_skip_exact_owned_container(tmp_path: Path, monkeypatch, failure: str) -> None:
    """原读回或证据I/O失效时仍核自己标签清容器/卷，不能删未匹配的其他服务。"""
    mysql = module("live_owned_mysql")
    holder = mysql.OwnedMysql(tmp_path)
    calls = []

    class Engine:
        """只复现清理读回失败，不连接任何SQL服务。"""

        def connect(self):
            """原连接异常不带URL或认证。"""
            raise OSError("SYNTHETIC_READBACK_FAILURE")

        def dispose(self):
            """按本例选择模拟原连接关闭异常。"""
            if failure == "dispose":
                raise OSError("SYNTHETIC_DISPOSE_FAILURE")

    holder.engine = Engine()

    def command(arguments, **_kwargs):
        """只记录精确合成容器命令，不真正调用Docker。"""
        calls.append(arguments)
        return SimpleNamespace(returncode=0, stdout=mysql.LABEL)

    monkeypatch.setattr(mysql.subprocess, "run", command)
    if failure == "save":
        def broken_save(*_args, **_kwargs):
            """模拟磁盘证据失败，同时仍要求拥有集回收。"""
            raise OSError("SYNTHETIC_EVIDENCE_WRITE_FAILURE")
        monkeypatch.setattr(mysql, "save", broken_save)
    try:
        holder.__exit__(None, None, None)
    except OSError:
        pass
    assert ["docker", "rm", "--force", holder.name] in calls
    assert ["docker", "volume", "rm", holder.volume] in calls


def test_browser_partial_report_cannot_qualify_full_journey(tmp_path: Path) -> None:
    """仅一个绿case且无规定标准的原报告不得误计完整模拟闭环通过。"""
    runner = module("run_browser_live")
    report = {"suites": [{"specs": [{"title": "one diagnostic only", "tests": [
        {"results": [{"status": "passed", "retry": 0, "attachments": []}]}]}]}]}
    assert runner.archive(report, tmp_path) is False


def test_browser_missing_required_criterion_cannot_qualify(tmp_path: Path) -> None:
    """九个原case全绿仍须完整覆盖冻结业务/清理标准，附件缺项不算通过。"""
    runner = module("run_browser_live")
    titles = []
    for kind in ("SOP", "DTA"):
        titles.extend([
            f"JRN08 {kind} 真模型→原确认→独立模拟accepted→真重启→原callback与业务读回清理",
            f"JRN08 {kind} 原页面取消→迟到独立效果→保unknown→真实证据人工对账",
            f"JRN08 {kind} 模拟安全拒绝真实422→独立零效果→原失败或人工对账",
            f"JRN08 {kind} 受理丢回执→unknown先独立对账→原恢复不重发",
        ])
    titles.append("JRN08 H2后DTA真实旧知识冻结→页面正文发布→真重启恢复旧版→新/未知质量")
    attachment = {"name": "f3-safe-criterion", "body": base64.b64encode(json.dumps({
        "criterion": "diagnostic-only", "passed": True,
        "real_vendor_qualified": False, "business_provider_kind": "simulated_async_service",
    }).encode()).decode()}
    report = {"suites": [{"specs": [{"title": title, "tests": [{"results": [{
        "status": "passed", "retry": 0, "attachments": [attachment],
    }]}]} for title in titles]}]}
    assert runner.archive(report, tmp_path) is False


def test_browser_missing_final_sdk_and_cleanup_evidence_is_refused(tmp_path: Path) -> None:
    """浏览器退出零而最终SDK/HTTP旁证或进程清理缺失时必须保留待验。"""
    runner = module("run_browser_live")
    owned, evidence = tmp_path / "owned", tmp_path / "evidence"
    owned.mkdir()
    evidence.mkdir()
    assert runner.archive_runtime_evidence(owned, evidence, "sqlite") is False


def test_browser_invalid_final_sdk_health_is_refused(tmp_path: Path) -> None:
    """全部进程结束也不能替代SDK旁证健康，错误计数必须否决整轮。"""
    runner = module("run_browser_live")
    owned, evidence = tmp_path / "owned", tmp_path / "evidence"
    runtime = owned / "f3-live-sqlite"
    runtime.mkdir(parents=True)
    evidence.mkdir()
    (runtime / "sdk-final-health-424242.safe.json").write_text(json.dumps({
        "pid": 424242, "original_sdk": {"valid": False, "started_count": 1,
        "settled_count": 1, "receipt_count": 1, "observation_error_count": 1},
        "wire_inputs": {"valid": False, "error_count": 1, "receipt_count": 1},
    }))
    assert runner.archive_runtime_evidence(owned, evidence, "sqlite") is False


def test_browser_original_complete_sdk_and_http_evidence_can_qualify(tmp_path: Path) -> None:
    """独立控制保证精确请求摘要、完整usage及实际清理齐备的合法记录仍可验收。"""
    runner = module("run_browser_live")
    owned, evidence = tmp_path / "owned", tmp_path / "evidence"
    runtime = owned / "f3-live-sqlite"
    runtime.mkdir(parents=True)
    evidence.mkdir()
    original = {"valid": True, "started_count": 1, "settled_count": 1, "receipt_count": 1}
    (runtime / "sdk-final-health-424242.safe.json").write_text(json.dumps({
        "pid": 424242, "original_sdk": original,
        "wire_inputs": {"valid": True, "receipt_count": 1, "error_count": 0},
    }))
    receipt = {"request_sha256": "a" * 64, "status": "completed",
               "identity_sha256": {"session_id": "b" * 64}, "diagnostic_scope": None,
               "usage": {"prompt_tokens": 12}}
    (runtime / "sdk-observations-424242.safe.jsonl").write_text(json.dumps(receipt) + "\n")
    (runtime / "live-wire-424242.private.jsonl").write_text(json.dumps({
        "sdk_request_sha256": "a" * 64, "request_sha256": "b" * 64,
    }) + "\n")
    (owned / "provider-final-observations.safe.json").write_text(json.dumps({
        "observation_healthy": True, "observation_error_count": 0,
        "rows": [], "submit_http_count": 0,
    }))
    (owned / "provider-process-cleanup.safe.json").write_text(json.dumps({
        "actual_pid": 424243, "process_absent": True,
    }))
    (runtime / "sdk-final-health-424244.safe.json").write_text(json.dumps({
        "pid": 424244, "original_sdk": {"valid": True, "started_count": 0,
            "settled_count": 0, "receipt_count": 0},
        "wire_inputs": {"valid": True, "receipt_count": 0, "error_count": 0},
    }))
    assert runner.archive_runtime_evidence(owned, evidence, "sqlite") is True


def test_current_harness_restart_without_ledger_keeps_prior_sdk_receipts(tmp_path: Path) -> None:
    """当前F3归档允许只读重启进程没有账本，同时保留前一进程业务收据。"""
    runner = module("run_browser_live", I2_CURRENT_HARNESS)
    owned, evidence = tmp_path / "owned", tmp_path / "evidence"
    runtime = owned / "f3-live-sqlite"
    runtime.mkdir(parents=True, mode=0o700)
    evidence.mkdir(mode=0o700)
    original = {
        "valid": True, "started_count": 1, "settled_count": 1, "receipt_count": 1,
        "observation_error_count": 0,
    }
    (runtime / "sdk-final-health-424242.safe.json").write_text(json.dumps({
        "pid": 424242, "original_sdk": original,
        "wire_inputs": {"valid": True, "receipt_count": 1, "error_count": 0},
    }))
    (runtime / "sdk-observations-424242.safe.jsonl").write_text(json.dumps({
        "call_id": "f3_sdk_" + "a" * 32, "request_sha256": "b" * 64,
        "response_sha256": "c" * 64, "source_sha256": "d" * 64,
        "status": "completed", "observation_valid": True,
        "identity_sha256": {"session_id": "e" * 64, "source_input_id": "f" * 64},
        "diagnostic_scope": None,
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }) + "\n")
    (runtime / "live-wire-424242.private.jsonl").write_text(json.dumps({
        "sdk_request_sha256": "b" * 64, "request_sha256": "b" * 64,
        "identity_sha256": {"session_id": "e" * 64, "source_input_id": "f" * 64},
    }) + "\n")
    (runtime / "sdk-final-health-424244.safe.json").write_text(json.dumps({
        "pid": 424244,
        "original_sdk": {"valid": False, "client_state_count": 0, "started_count": 0,
            "settled_count": 0, "in_flight_count": 0, "receipt_count": 0,
            "observation_error_count": 0, "file_matches": True, "write_failed": False},
        "wire_inputs": {"valid": True, "receipt_count": 0, "error_count": 0},
    }))
    (owned / "provider-final-observations.safe.json").write_text(json.dumps({
        "observation_healthy": True, "observation_error_count": 0,
        "rows": [], "submit_http_count": 0,
    }))
    (owned / "provider-process-cleanup.safe.json").write_text(json.dumps({
        "actual_pid": 424243, "process_absent": True,
    }))
    (owned / "application-process-cleanup.safe.json").write_text(json.dumps({
        "cleanup_complete": True,
    }))
    seal_candidate_private_tree(owned)
    assert runner.archive_runtime_evidence(owned, evidence, "sqlite") is True


def test_current_harness_preserves_prehashed_case_identity_digest(tmp_path: Path) -> None:
    """安全criterion中的SDK身份摘要不能被归档层再次哈希而失去可关联性。"""
    runner = module("run_browser_live", I2_CURRENT_HARNESS)
    projected = runner.safe_criterion_projection({
        "criterion": "case-sdk-identity-binding", "case_id": "sop-normal",
        "identity_sha256": {"session_id": "a" * 64},
    })
    assert projected["case_id_sha256"] == runner.sha(b"sop-normal")
    assert projected["identity_sha256"] == {"session_id_sha256": "a" * 64}


def test_current_harness_sdk_receipt_archive_keeps_observation_valid_fact(tmp_path: Path) -> None:
    """SDK安全收据必须保留原观测有效性，缺失字段不能被质量映射误认成完成。"""
    runner = module("run_browser_live", I2_CURRENT_HARNESS)
    with pytest.raises(RuntimeError, match="F3_FINAL_SDK_OBSERVATION_INVALID"):
        runner._safe_sdk_receipt_rows([{
            "call_id": "f3_sdk_" + "a" * 32, "request_sha256": "b" * 64,
            "identity_sha256": {}, "diagnostic_scope": None, "usage": {},
        }])


def test_browser_model_preflight_usage_cannot_replace_business_model_usage(tmp_path: Path) -> None:
    """管理预检的usage不能替代Composer/消费者真实业务调用的模型usage。"""
    runner = module("run_browser_live", I2_CURRENT_HARNESS)
    owned, evidence = tmp_path / "owned", tmp_path / "evidence"
    runtime = owned / "f3-live-sqlite"
    runtime.mkdir(parents=True)
    evidence.mkdir()
    (runtime / "sdk-final-health-424242.safe.json").write_text(json.dumps({
        "pid": 424242, "original_sdk": {"valid": True, "receipt_count": 1},
        "wire_inputs": {"valid": True, "receipt_count": 1, "error_count": 0},
    }))
    request_sha = "a" * 64
    (runtime / "sdk-observations-424242.safe.jsonl").write_text(json.dumps({
        "request_sha256": request_sha, "status": "completed",
        "usage": {"prompt_tokens": 12}, "identity_sha256": {},
        "diagnostic_scope": {"schema": "model-preflight-v1",
                             "probe_class": "native_capabilities"},
    }) + "\n")
    (runtime / "live-wire-424242.private.jsonl").write_text(json.dumps({
        "request_sha256": request_sha, "sdk_request_sha256": request_sha,
    }) + "\n")
    (runtime / "sdk-final-health-424243.safe.json").write_text(json.dumps({
        "pid": 424243, "original_sdk": {"valid": False, "client_state_count": 0,
            "started_count": 0, "settled_count": 0, "in_flight_count": 0,
            "observation_error_count": 0, "receipt_count": 0, "file_matches": True,
            "write_failed": False},
        "wire_inputs": {"valid": True, "receipt_count": 0, "error_count": 0},
    }))
    (owned / "provider-final-observations.safe.json").write_text(json.dumps({
        "observation_healthy": True, "observation_error_count": 0, "rows": [],
        "submit_http_count": 0,
    }))
    (owned / "provider-process-cleanup.safe.json").write_text(json.dumps({
        "actual_pid": 424244, "process_absent": True,
    }))
    (owned / "application-process-cleanup.safe.json").write_text(json.dumps({
        "cleanup_complete": True,
    }))

    seal_candidate_private_tree(owned)

    assert runner.archive_runtime_evidence(
        owned, evidence, "sqlite",
    ) is False


def test_restarted_unused_original_sdk_ledger_keeps_prior_completed_usage(tmp_path: Path) -> None:
    """用原SdkLedger真实零客户端健康证明恢复进程可结算，先前真实usage要求仍保留。"""
    from final_f3_sdk_ledger import SdkLedger

    test_browser_original_complete_sdk_and_http_evidence_can_qualify(tmp_path)
    runtime = tmp_path / "owned/f3-live-sqlite"
    original = SdkLedger(runtime / "sdk-observations-424244.safe.jsonl").health()
    assert original["valid"] is False
    assert original["client_state_count"] == 0
    assert original["file_matches"] is True
    (runtime / "sdk-final-health-424244.safe.json").write_text(json.dumps({
        "pid": 424244, "original_sdk": original,
        "wire_inputs": {"valid": True, "receipt_count": 0, "error_count": 0},
    }))
    runner = module("run_browser_live")
    assert runner.archive_runtime_evidence(tmp_path / "owned", tmp_path / "evidence", "sqlite")
    (runtime / "sdk-observations-424242.safe.jsonl").write_text(json.dumps({
        "request_sha256": "a" * 64, "status": "failed", "usage": {},
    }) + "\n")
    assert not runner.archive_runtime_evidence(
        tmp_path / "owned", tmp_path / "evidence", "sqlite",
    )


@pytest.mark.parametrize(("key", "value"), [
    ("client_state_count", 1), ("started_count", 1), ("settled_count", 1),
    ("in_flight_count", 1), ("observation_error_count", 1), ("receipt_count", 1),
    ("file_matches", False), ("write_failed", True), ("started_count", False),
])
def test_unused_sdk_exception_rejects_corrupt_original_health(
    tmp_path: Path, key: str, value: object,
) -> None:
    """零客户端例外必须逐项核原计数和落盘健康，异常值不得借旧usage通过。"""
    from final_f3_sdk_ledger import SdkLedger

    test_browser_original_complete_sdk_and_http_evidence_can_qualify(tmp_path)
    runtime = tmp_path / "owned/f3-live-sqlite"
    original = SdkLedger(runtime / "sdk-observations-424244.safe.jsonl").health()
    original[key] = value
    (runtime / "sdk-final-health-424244.safe.json").write_text(json.dumps({
        "pid": 424244, "original_sdk": original,
        "wire_inputs": {"valid": True, "receipt_count": 0, "error_count": 0},
    }))
    runner = module("run_browser_live")
    assert not runner.archive_runtime_evidence(tmp_path / "owned", tmp_path / "evidence", "sqlite")


def test_runner_generated_identity_matches_original_live_start_contract(tmp_path: Path) -> None:
    """以真实入口校验运行器生成ID，不能仅验证手工合成配置而漏掉真实组合。"""
    from start_final_f3_live_server import build_live_plan
    from test_final_f3_live_server import configuration

    runner = module("run_browser_live")
    runtime = tmp_path / "f3-live-sqlite"
    runtime.mkdir(mode=0o700)
    model = {"DEMO_MODEL_BASE_URL": "https://models.example.org/v1",
             "DEMO_MODEL_NAME": "synthetic-model", "DEMO_MODEL_API_KEY": "SYNTHETIC-MODEL-CANARY"}
    config = configuration()
    config.update(runner.make_inputs("i2-live-sqlite-full-02", 51444, "sqlite", model, tmp_path))
    plan = build_live_plan(runtime, config)
    assert plan.port == 51444 and plan.runtime == runtime


def test_provider_final_health_reads_actual_port_receipt(tmp_path: Path, monkeypatch) -> None:
    """原模拟服务ready只给port，最终健康应读该原字段且不要求不存在origin。"""
    import io

    child = module("live_browser_child")
    (tmp_path / "ready.private.json").write_text(json.dumps({
        "port": 51445, "pid": 424242, "real_vendor_qualified": False,
    }))
    captured = []

    def response(request, **_kwargs):
        """纯HTTP边界控制，不连接模型、容器或任何真实服务。"""
        captured.append(request.full_url)
        return io.BytesIO(b'{"observation_healthy":true,"observation_error_count":0}')

    monkeypatch.setattr(child, "urlopen", response)
    result = child.provider_observations(tmp_path, {"key": "SYNTHETIC-SIMULATION-CANARY"})
    assert result["observation_healthy"] is True
    assert captured == ["http://127.0.0.1:51445/simulation/observations"]


def test_fixture_uses_frozen_actual_frontend_login_tenant(tmp_path: Path) -> None:
    """原登录页静态租户与合成资源须一致，不能仅在库中随机租户却用其他前端配置。"""
    runner, child = module("run_browser_live"), module("live_browser_child")
    model = {"DEMO_MODEL_BASE_URL": "https://models.example.org/v1",
             "DEMO_MODEL_NAME": "synthetic-model", "DEMO_MODEL_API_KEY": "SYNTHETIC-MODEL-CANARY"}
    config = runner.make_inputs("i2-live-sqlite-full-04", 51444, "sqlite", model, tmp_path)
    config["login_tenant_id"] = "f3_ui_tenant_synthetic"
    spec = child.make_fixture(config, tmp_path / "f3-live-sqlite", {
        "origin": "http://127.0.0.1:51445", "namespace": "f3-sim-" + "a" * 16,
        "key": "SYNTHETIC-SIMULATION-CANARY", "control_key": "SYNTHETIC-CONTROL-CANARY",
    })
    assert spec["tenant"]["id"] == config["login_tenant_id"]


def test_browser_exit_allows_original_application_final_evidence() -> None:
    """默认强杀WebServer会丢原lifespan最终SDK旁证，应先SIGTERM且有限等待。"""
    runner = module("run_browser_live")
    config = runner.web_server_configuration(51444)
    assert config["reuseExistingServer"] is False
    assert config["gracefulShutdown"]["signal"] == "SIGTERM"
    assert 15000 < config["gracefulShutdown"]["timeout"] <= 30000


def test_freeze_diagnostic_runtime_deadline_terminates_only_owned_child(tmp_path: Path, monkeypatch) -> None:
    """冻结诊断墙钟预算耗尽时只终止受控子树并保留超时资源证据。"""
    runner = module("run_browser_live", I4_CURRENT_HARNESS)
    owned, destination = tmp_path / "owned", tmp_path / "evidence"
    owned.mkdir(mode=0o700)
    destination.mkdir()

    class Child:
        """只提供运行截止测试所需的合成PID和可控退出状态。"""

        pid = 424242

        def __init__(self) -> None:
            """初始化仍在运行的合成子进程状态。"""

            self.returncode = None

        def poll(self):
            """保持运行直到运行器对该自有合成PID发TERM。"""
            return self.returncode

        def wait(self, timeout=None):
            """返回已被测试控制器终止的状态。"""
            assert self.returncode is not None
            return self.returncode

    child = Child()
    clock = [0.0]

    def monotonic():
        """确定性推进单调时钟，使首次样本后超过测试预算。"""
        clock[0] += 0.6
        return clock[0]

    monkeypatch.setattr(runner.subprocess, "Popen", lambda *_args, **_kwargs: child)
    monkeypatch.setattr(runner.time, "monotonic", monotonic)
    monkeypatch.setattr(runner.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(runner, "_owned_memory_sample", lambda *_args: ({
        "owned_budget_charge_kib": 1000, "owned_tree_rss_kib": 2000,
        "owned_tree_pss_kib": 1000, "owned_tree_swap_pss_kib": 0, "pss_complete": True,
    }, {child.pid}))

    def stop_owned(pid, _signal):
        """确保运行截止只向本测试创建的合成进程发信号。"""
        assert pid == child.pid
        child.returncode = 143

    monkeypatch.setattr(runner, "_signal_owned_process_group", stop_owned)
    code = runner.bounded_child(["synthetic-owned-child"], {}, owned, destination,
                                maximum_runtime_seconds=1)
    summary = json.loads((destination / "resource-summary.safe.json").read_text())
    samples = json.loads((destination / "resource-samples.safe.json").read_text())
    assert code == 143
    assert summary["cutoff"] == "owned_browser_process_wall_clock_deadline"
    assert summary["maximum_runtime_seconds"] == 1
    assert len(samples) == 1


def test_freeze_uses_one_absolute_active_and_total_deadline_schedule(monkeypatch) -> None:
    """冻结记忆诊断的观察、case、清理与归档共享runner入口的绝对单调截止。"""
    runner = module("run_browser_live", I4_CURRENT_HARNESS)
    start_ns = 10_000_000_000
    monkeypatch.setattr(runner.time, "monotonic_ns", lambda: start_ns)
    schedule = runner.diagnostic_deadline_schedule("memory-dta-freeze", start_ns)
    assert schedule == {
        "started_ns": start_ns,
        "active_deadline_ns": start_ns + 6330_000_000_000,
        "after_each_deadline_ns": start_ns + 6880_000_000_000,
        "fixture_deadline_ns": start_ns + 6910_000_000_000,
        "child_term_deadline_ns": start_ns + 6930_000_000_000,
        "child_hard_deadline_ns": start_ns + 6976_000_000_000,
        "parent_mysql_cleanup_deadline_ns": start_ns + 6986_000_000_000,
        "archive_deadline_ns": start_ns + 7050_000_000_000,
    }
    assert runner.diagnostic_deadline_schedule("dta-normal", start_ns) is None
    assert runner.playwright_case_timeout_ms("memory-dta-freeze") == 6_330_000
    assert runner.playwright_case_timeout_ms("dta-normal") == 900_000
    assert runner.remaining_deadline_ms(start_ns + 2_000_000, start_ns + 1_000_001) == 1
    assert runner.remaining_deadline_ms(start_ns + 2_000_000, start_ns) == 2
    assert runner.web_server_configuration(51444, start_ns + 200_000_000_000)["timeout"] == 140_000


def test_total_absolute_deadline_is_armed_before_runner_setup_side_effects(
    tmp_path: Path, monkeypatch,
) -> None:
    """runner入口先装T0总截止，再触碰运行目录或派发浏览器子进程。"""
    runner = module("run_browser_live", I4_CURRENT_HARNESS)
    events = []
    private_root = tmp_path / "private"
    monkeypatch.setattr(runner, "PRIVATE", private_root)
    monkeypatch.setattr(runner, "private_tmpfs_preflight", lambda _path: {"valid": True})
    monkeypatch.setattr(runner.os, "umask", lambda _mask: events.append("umask"))
    monkeypatch.setattr(runner.time, "monotonic_ns", lambda: 10_000_000_000)
    monkeypatch.setattr(runner.time, "monotonic", lambda: 10.0)
    monkeypatch.setattr(runner.sys, "argv", [
        "run_browser_live.py", "i3-live-synthetic", "sqlite", "memory-dta-freeze",
    ])
    monkeypatch.setattr(runner.signal, "signal", lambda _sig, handler: events.append(
        ("signal", handler)) or "previous-handler")
    monkeypatch.setattr(runner.signal, "setitimer", lambda _kind, seconds: events.append(
        ("timer", seconds)))
    original_mkdir = runner.Path.mkdir

    def stop_at_first_private_setup(path, *args, **kwargs):
        """在首个私有运行目录创建处停止，不能创建测试目录或触碰真实服务。"""
        if path == private_root:
            events.append("private-mkdir")
            raise RuntimeError("SYNTHETIC_STOP_BEFORE_RUN_SETUP")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(runner.Path, "mkdir", stop_at_first_private_setup)
    with pytest.raises(RuntimeError, match="SYNTHETIC_STOP_BEFORE_RUN_SETUP"):
        runner.main()

    event_names = [item[0] if isinstance(item, tuple) else item for item in events]
    assert event_names.index("timer") < event_names.index("private-mkdir")
    assert event_names[-2:] == ["timer", "signal"]
    installed = [item[1] for item in events if isinstance(item, tuple) and item[0] == "timer"][0]
    assert installed == pytest.approx(7050.0)


def test_memory_diagnostic_tmpfs_preflight_requires_filesystem_and_free_space() -> None:
    """记忆诊断派发前同时验证tmpfs挂载和快照空间预算，拒绝不能重试的低空间运行。"""
    runner = module("run_browser_live", I4_CURRENT_HARNESS)
    helper = runner.private_tmpfs_preflight
    assert runner.MEMORY_SNAPSHOT_MAX_TOTAL_BYTES == 64 * 1024 * 1024
    assert runner.MEMORY_SNAPSHOT_PREFLIGHT_BYTES == 192 * 1024 * 1024
    assert runner._memory_snapshot_identity_key(
        runner.DIAGNOSTIC_CASE_GREP["memory-dta-freeze"], 0,
    ) == "5b3778786214a9cde2b1d4fe"
    tmpfs = "36 25 0:32 / /dev/shm rw,nosuid,nodev - tmpfs tmpfs rw,size=1g\n"
    ext4 = "36 25 8:1 / /dev/shm rw,relatime - ext4 /dev/sda1 rw\n"

    enough = helper(Path("/dev/shm/f3-private"), required_free_bytes=640,
        mountinfo_text=tmpfs, available_bytes=641)
    assert enough["valid"] is True
    low = helper(Path("/dev/shm/f3-private"), required_free_bytes=640,
        mountinfo_text=tmpfs, available_bytes=639)
    assert low["valid"] is False and low["reason"] == "insufficient_free_space"
    wrong_filesystem = helper(Path("/dev/shm/f3-private"), required_free_bytes=640,
        mountinfo_text=ext4, available_bytes=10_000)
    assert wrong_filesystem["valid"] is False and wrong_filesystem["reason"] == "not_tmpfs"


def test_memory_diagnostic_tmpfs_preflight_rejects_symlinked_runtime_ancestor(tmp_path: Path) -> None:
    """私有tmpfs路径中的既有符号链接不能将快照写到预期运行根之外。"""
    runner = module("run_browser_live", I4_CURRENT_HARNESS)
    target = tmp_path / "actual-runtime-parent"
    target.mkdir()
    link = tmp_path / "runtime-link"
    link.symlink_to(target, target_is_directory=True)
    mountinfo = f"36 25 0:32 / {tmp_path} rw,nosuid - tmpfs tmpfs rw,size=1g\n"

    result = runner.private_tmpfs_preflight(link / "private-run",
        required_free_bytes=1, mountinfo_text=mountinfo, available_bytes=10)

    assert result["valid"] is False
    assert result["reason"] == "unsafe_path"


def test_registered_memory_diagnostic_checks_tmpfs_before_claiming_attempt(tmp_path: Path, monkeypatch) -> None:
    """空间不足时注册入口在一次性attempt之前拒绝，不消耗不可重试旅程。"""
    runner = module("run_browser_live", I4_CURRENT_HARNESS)
    registration = module("registered_i4_run", I4_CURRENT_HARNESS)
    architecture = tmp_path / "architecture"
    architecture.mkdir()
    run_id = "i3-live-synthetic"
    selection = "memory-dta-freeze"
    freeze_path = architecture / f"{run_id}-source-freeze.safe.json"
    contract_path = architecture / f"{run_id}-preregistered-contract.safe.json"
    invocation_path = architecture / f"{run_id}-invocation.safe.json"
    freeze_path.write_text(json.dumps({"source_map_sha256": "frozen-map"}))
    contract_path.write_text(json.dumps({"model_configuration_hmac_sha256": "frozen-model",
        "dialect": "sqlite", "run_id": run_id, "diagnostic_only": True,
        "diagnostic_selection": selection, "acceptance_credit": False,
        "case_criteria": {name: sorted(criteria)
                          for name, criteria in runner.expected_case_criteria(selection).items()}}))
    invocation_path.write_text(json.dumps({"invocation_count": 1, "retry_count": 0,
                                           "diagnostic_selection": selection}))
    monkeypatch.setattr(registration, "EVIDENCE", tmp_path)
    monkeypatch.setattr(registration, "validate_diagnostic_scope", lambda *_args: None)
    monkeypatch.setattr(registration, "_source_map", lambda: (runner, {"source": "map"}))
    monkeypatch.setattr(registration, "_fingerprint", lambda _mapping: "frozen-map")
    monkeypatch.setattr(registration, "_model_hmac", lambda: "frozen-model")
    monkeypatch.setattr(registration, "_registered_artifacts_valid", lambda *_args: True)
    monkeypatch.setattr(runner, "private_tmpfs_preflight", lambda _path: {
        "valid": False, "reason": "insufficient_free_space"})
    attempts = []
    monkeypatch.setattr(registration, "_run_registered_attempt_once",
        lambda *_args: attempts.append("claimed"))

    with pytest.raises(RuntimeError, match="F3_MEMORY_TMPFS_PREFLIGHT_FAILED"):
        registration.run_registered(run_id, "sqlite", selection)

    assert attempts == []
    assert not (architecture / f"{run_id}-attempt.safe.json").exists()


def test_runner_total_deadline_signal_cannot_be_swallowed_by_resource_monitor(
    tmp_path: Path, monkeypatch,
) -> None:
    """SIGALRM总截止穿过资源监控的普通异常捕获并立即中止runner路径。"""
    import signal

    runner = module("run_browser_live", I4_CURRENT_HARNESS)
    owned, destination = tmp_path / "owned", tmp_path / "evidence"
    owned.mkdir(mode=0o700)
    destination.mkdir()
    handlers = []

    class Child:
        """只在测试内改变状态的假Popen对象，不创建真实子进程。"""

        pid = 515154
        returncode = None

        def poll(self):
            """返回本测试受控的拥有进程状态。"""
            return self.returncode

        def wait(self, timeout=None):
            """确认截止清理只等待已经被本测试停止的假进程。"""
            assert timeout is not None
            assert self.returncode is not None
            return self.returncode

    child = Child()
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *_args, **_kwargs: child)
    monkeypatch.setattr(runner, "_owned_new_session_identity", lambda pid: {
        "pid": pid, "start_ticks": 1, "process_group_id": pid, "session_id": pid,
    })
    monkeypatch.setattr(runner.signal, "signal", lambda _sig, handler: handlers.append(handler) or "old")
    monkeypatch.setattr(runner.signal, "setitimer", lambda *_args: None)
    monkeypatch.setattr(runner, "_signal_owned_process_group", lambda pid, _sig: setattr(
        child, "returncode", -15) if pid == child.pid else pytest.fail("FOREIGN_PID"))
    previous = runner.arm_runner_total_deadline({
        "archive_deadline_ns": runner.time.monotonic_ns() + 60_000_000_000,
    })

    def expire_during_monitor(_pid, _dependencies):
        """模拟总截止信号在bounded_child宽泛异常处理范围内抵达。"""
        handlers[-1](signal.SIGALRM, None)
        raise AssertionError("deadline handler returned")

    monkeypatch.setattr(runner, "_owned_memory_sample", expire_during_monitor)
    try:
        with pytest.raises(BaseException) as captured:
            runner.bounded_child(["synthetic-deadline-only"], {}, owned, destination)
    finally:
        runner.restore_runner_total_deadline(previous)

    assert type(captured.value).__name__ == "RunnerTotalDeadlineExceeded"
    assert not isinstance(captured.value, Exception)
    assert not (destination / "resource-summary.safe.json").exists()


def test_exhausted_active_deadline_writes_redacted_marker_and_never_starts_child(
    tmp_path: Path, monkeypatch,
) -> None:
    """活动预算已耗尽时，正式派发入口拒绝子进程且只留下无凭据安全标记。"""
    runner = module("run_browser_live", I4_CURRENT_HARNESS)
    owned, destination = tmp_path / "owned", tmp_path / "evidence"
    owned.mkdir(mode=0o700)
    destination.mkdir()
    child_starts = []
    monkeypatch.setattr(runner.time, "monotonic_ns", lambda: 2_000)
    monkeypatch.setattr(
        runner, "bounded_child",
        lambda *args, **kwargs: child_starts.append((args, kwargs)) or 0,
    )
    deadlines = {
        "active_deadline_ns": 1_000,
        "child_term_deadline_ns": 3_000,
        "child_hard_deadline_ns": 4_000,
    }

    with pytest.raises(TimeoutError, match="ACTIVE_DEADLINE_EXHAUSTED_BEFORE_CHILD"):
        runner.start_child_after_deadline_preflight(
            ["synthetic-child"], {"TEST_SECRET": "must-not-be-recorded"},
            owned, destination, deadlines,
        )

    assert child_starts == []
    marker_path = destination / "active-deadline-preflight.safe.json"
    assert marker_path.is_file()
    marker = json.loads(marker_path.read_text())
    assert marker == {
        "active_deadline_exhausted_before_child_start": True,
        "provider_started": False,
        "browser_started": False,
        "model_invoked": False,
        "deadline_from_runner_entry_seconds": 6330,
    }
    assert list(destination.iterdir()) == [marker_path]
    assert "TEST_SECRET" not in marker_path.read_text()


@pytest.mark.parametrize("stage", ["provider_start", "browser_start"])
def test_child_active_preflight_blocks_provider_and_browser_popen(
    tmp_path: Path, monkeypatch, stage: str,
) -> None:
    """child内部setup越过活动截止后，provider与浏览器的实际Popen入口都被阻断。"""
    child = module("live_browser_child", I4_CURRENT_HARNESS)
    owned = tmp_path / "owned"
    owned.mkdir(mode=0o700)
    starts = []
    monkeypatch.setenv("F3_MEMORY_FREEZE_ACTIVE_DEADLINE_MONOTONIC_NS", "100")
    monkeypatch.setattr(child.time, "monotonic_ns", lambda: 101)
    monkeypatch.setattr(child.subprocess, "Popen", lambda *args, **kwargs: starts.append(args))

    with pytest.raises(TimeoutError, match="ACTIVE_DEADLINE_EXHAUSTED_IN_CHILD_SETUP"):
        child.launch_active_process(stage, ["synthetic-child"], {}, owned)

    assert starts == []
    marker = json.loads((owned / "active-deadline-child-refusal.safe.json").read_text())
    assert marker == {
        "active_deadline_exhausted_before_stage": stage,
        "provider_started": stage != "provider_start",
        "browser_started": False,
        "model_invoked": False,
    }
    assert "TEST_SECRET" not in json.dumps(marker)


def test_child_process_wait_timeout_is_bounded_and_reports_unconfirmed_exit() -> None:
    """等待被挂起的直接子进程只消耗指定等待预算，并保留未确认结果。"""
    child = module("live_browser_child", I4_CURRENT_HARNESS)
    waits = []

    class StuckProcess:
        """不响应wait的合成子进程，不创建真实OS进程。"""

        def wait(self, timeout=None):
            """记录上限后返回标准超时结果。"""
            waits.append(timeout)
            raise child.subprocess.TimeoutExpired("synthetic-child", timeout)

    assert child.wait_owned_process(StuckProcess(), 0.25) is False
    assert waits == [0.25]


def test_unexpired_active_deadline_dispatches_with_same_absolute_cleanup_deadlines(
    tmp_path: Path, monkeypatch,
) -> None:
    """活动期仍有余量时只派发一次，并把同一轮TERM/KILL截止传给拥有进程监控。"""
    runner = module("run_browser_live", I4_CURRENT_HARNESS)
    owned, destination = tmp_path / "owned", tmp_path / "evidence"
    owned.mkdir(mode=0o700)
    destination.mkdir()
    calls = []
    monkeypatch.setattr(runner.time, "monotonic_ns", lambda: 999)
    monkeypatch.setattr(
        runner, "bounded_child",
        lambda *args, **kwargs: calls.append((args, kwargs)) or 0,
    )
    deadlines = {
        "active_deadline_ns": 1_000,
        "child_term_deadline_ns": 2_000,
        "child_hard_deadline_ns": 3_000,
        "parent_mysql_cleanup_deadline_ns": 4_000,
    }

    assert runner.start_child_after_deadline_preflight(
        ["synthetic-child"], {}, owned, destination, deadlines,
    ) == 0

    assert len(calls) == 1
    assert calls[0][1] == {
        "graceful_termination_deadline_ns": 2_000,
        "hard_deadline_ns": 3_000,
        "emergency_cleanup_deadline_ns": 4_000,
    }
    assert not (destination / "active-deadline-preflight.safe.json").exists()


def test_freeze_owned_tree_signals_gracefully_then_hard_stops_only_at_absolute_deadline(
    tmp_path: Path, monkeypatch,
) -> None:
    """绝对child截止先TERM留46秒收尾，只有child hard deadline才SIGKILL整树。"""
    import signal

    runner = module("run_browser_live", I4_CURRENT_HARNESS)
    owned, destination = tmp_path / "owned", tmp_path / "evidence"
    owned.mkdir(mode=0o700)
    destination.mkdir()
    clock = [0]
    signals = []

    class Child:
        """仅模拟本测试拥有的进程，不创建任何真实子进程。"""

        pid = 515151
        returncode = None

        def poll(self):
            """保留未结束状态，直到合成的最终截止触发SIGKILL。"""
            return self.returncode

        def wait(self, timeout=None):
            """模拟已终止child的wait，不触发真实等待。"""
            if self.returncode is None:
                raise runner.subprocess.TimeoutExpired("synthetic", timeout)
            return self.returncode

        def kill(self):
            """确保兜底kill仍然仅改变此测试child。"""
            self.returncode = -signal.SIGKILL

    child = Child()
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *_args, **_kwargs: child)
    monkeypatch.setattr(runner.time, "monotonic", lambda: float(clock[0]))
    monkeypatch.setattr(runner.time, "monotonic_ns", lambda: clock[0] * 1_000_000_000)
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + int(seconds)))
    monkeypatch.setattr(runner, "_owned_memory_sample", lambda *_args: ({
        "owned_budget_charge_kib": 1000, "owned_tree_rss_kib": 2000,
        "owned_tree_pss_kib": 1000, "owned_tree_swap_pss_kib": 0, "pss_complete": True,
    }, {child.pid}))

    def signal_owned(pid, signal_number):
        """记录本轮虚拟进程组信号，并只让最终KILL结束假进程。"""
        assert pid == child.pid
        signals.append(signal_number)
        if signal_number == signal.SIGKILL:
            child.returncode = -signal.SIGKILL

    monkeypatch.setattr(runner, "_signal_owned_process_group", signal_owned)
    monkeypatch.setattr(runner, "_emergency_cleanup_owned_mysql", lambda *_args: {})
    code = runner.bounded_child(
        ["synthetic-absolute-deadline"], {}, owned, destination,
        graceful_termination_deadline_ns=1_000_000_000,
        hard_deadline_ns=2_000_000_000,
    )

    summary = json.loads((destination / "resource-summary.safe.json").read_text())
    assert code == -signal.SIGKILL
    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert summary["cutoff"] == "owned_browser_process_absolute_hard_deadline"


def test_freeze_hard_kill_never_waits_indefinitely_for_unreaped_child(
    tmp_path: Path, monkeypatch,
) -> None:
    """SIGKILL后若内核仍未回收测试子进程，运行器留失败状态并继续安全归档。"""
    import signal

    runner = module("run_browser_live", I4_CURRENT_HARNESS)
    owned, destination = tmp_path / "owned", tmp_path / "evidence"
    owned.mkdir(mode=0o700)
    destination.mkdir()
    clock = [0]
    waits = []

    class StuckChild:
        """合成未被wait回收的child，用于确保兜底路径也有硬上限。"""

        pid = 515152

        def poll(self):
            """保持运行状态以触发hard deadline并模拟内核尚未完成reap。"""
            return None

        def wait(self, timeout=None):
            """记录所有等待都含有限timeout，再报告内核未返回。"""
            waits.append(timeout)
            raise runner.subprocess.TimeoutExpired("synthetic-stuck-child", timeout)

        def kill(self):
            """仅登记Python兜底kill调用，不创建或影响真实进程。"""
            return None

    process = StuckChild()
    signals = []
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(runner.time, "monotonic", lambda: float(clock[0]))
    monkeypatch.setattr(runner.time, "monotonic_ns", lambda: clock[0] * 1_000_000_000)
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + int(seconds)))
    monkeypatch.setattr(runner, "_owned_memory_sample", lambda *_args: ({
        "owned_budget_charge_kib": 1000, "owned_tree_rss_kib": 2000,
        "owned_tree_pss_kib": 1000, "owned_tree_swap_pss_kib": 0, "pss_complete": True,
    }, {process.pid}))
    monkeypatch.setattr(runner, "_signal_owned_process_group",
                        lambda _pid, number: signals.append(number))
    monkeypatch.setattr(runner, "_emergency_cleanup_owned_mysql", lambda *_args: {})

    result = runner.bounded_child(
        ["synthetic-unreaped-child"], {}, owned, destination,
        graceful_termination_deadline_ns=1_000_000_000,
        hard_deadline_ns=2_000_000_000,
    )

    summary = json.loads((destination / "resource-summary.safe.json").read_text())
    assert result == 143
    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert waits == [0.5]
    assert summary["owned_child_reaped"] is False


def test_frozen_full_browser_contract_includes_all_eleven_memory_journeys() -> None:
    """历史完整验收登记两分支六自然质量及五原控制，不能只登记九个业务案例。"""
    runner = module("run_browser_live")
    contract = runner.expected_case_criteria()
    natural = {f"F3-MEMORY {branch}-{number}: actual model, complete facts and independent labels"
               for branch in ("SOP", "DTA") for number in ("1", "2", "3")}
    controls = {
        "F3-MEMORY SOP zero-budget failure control keeps full management facts",
        "F3-MEMORY DTA zero-budget failure control keeps full management facts",
        "F3-MEMORY frozen DTA restarts with original facts; correction, clear and fresh sources",
        "F3-MEMORY clear: restored real answer cannot disclose invalid frozen facts",
        "F3-MEMORY revoke: restored real answer cannot disclose invalid frozen facts",
    }
    assert {title for title in contract if title.startswith("F3-MEMORY ")} == natural | controls
    assert len(contract) == 20
    assert "memory-freeze-cleared-new-source-is-actually-consumed" in contract[
        "F3-MEMORY frozen DTA restarts with original facts; correction, clear and fresh sources"
    ]


def test_dta_normal_diagnostic_filter_does_not_replace_full_acceptance_contract() -> None:
    """单例诊断只可选定已登记DTA用例，历史完整20项通过门槛保持不变。"""
    runner = module("run_browser_live")
    assert runner.diagnostic_case_grep(None) is None
    assert runner.diagnostic_case_grep("dta-normal").startswith("JRN08 DTA 真模型")
    config_source = runner.playwright_config_source({
        "testDir": "/synthetic", "grep": runner.diagnostic_case_grep("dta-normal"),
    })
    assert "grep: new RegExp(" in config_source
    assert '"grep":' not in config_source
    assert len(runner.expected_case_criteria()) == 20
    with pytest.raises(RuntimeError, match="F3_LIVE_DIAGNOSTIC_FILTER_NOT_REGISTERED"):
        runner.diagnostic_case_grep("all-but-memory")


def test_sop_cancel_diagnostic_enables_redacted_composer_and_session_probe() -> None:
    """所有真实旅程观察会话列表状态，定向Composer观察仍只限对应SOP分支。"""
    runner = module("run_browser_live", CURRENT_HARNESS)
    assert runner.diagnostic_environment_flags("sop-normal") == {
        "F3_DIAGNOSTIC_COMPOSER_STATE": "1",
        "F3_DIAGNOSTIC_SESSION_STACK": "1",
    }
    assert runner.diagnostic_environment_flags("sop-cancel") == {
        "F3_DIAGNOSTIC_COMPOSER_STATE": "1",
        "F3_DIAGNOSTIC_SESSION_STACK": "1",
    }
    assert runner.diagnostic_environment_flags("sop-normal-cancel") == {
        "F3_DIAGNOSTIC_COMPOSER_STATE": "1",
        "F3_DIAGNOSTIC_SESSION_STACK": "1",
    }
    assert runner.diagnostic_environment_flags("sop-safe-decline") == {
        "F3_DIAGNOSTIC_COMPOSER_STATE": "1",
        "F3_DIAGNOSTIC_SESSION_STACK": "1",
    }
    assert runner.diagnostic_environment_flags("dta-normal") == {
        "F3_DIAGNOSTIC_DTA_NORMAL": "1",
        "F3_DIAGNOSTIC_SESSION_STACK": "1",
    }
    assert runner.diagnostic_environment_flags(None) == {
        "F3_DIAGNOSTIC_SESSION_STACK": "1",
    }


def test_live_fixture_freezes_memory_labels_and_distinct_agents_before_any_model(tmp_path) -> None:
    """真实运行器须事前固定13独立记忆员工与完整人工事实，不能模型运行后补预期。"""
    from final_f3_live_memory import validate_memory_quality

    runner, child = module("run_browser_live"), module("live_browser_child")
    config = runner.make_inputs("i2-live-sqlite-full-memory", 51444, "sqlite", {
        "DEMO_MODEL_BASE_URL": "https://models.example.org/v1",
        "DEMO_MODEL_NAME": "synthetic-model", "DEMO_MODEL_API_KEY": "SYNTHETIC-MODEL-CANARY",
    }, tmp_path)
    spec = child.make_fixture(config, tmp_path / "f3-live-sqlite", {
        "origin": "http://127.0.0.1:51445", "namespace": "f3-sim-" + "a" * 16,
        "key": "SYNTHETIC-SIMULATION-CANARY", "control_key": "SYNTHETIC-CONTROL-CANARY",
    })
    quality = validate_memory_quality(spec)
    identities = [item["id"] for branch in quality["agents"].values() for item in branch.values()]
    assert len(identities) == len(set(identities)) == 13
    assert not set(identities) & {item["id"] for item in spec["agents"].values()}
    assert set(quality["labels"]) == {"name", "style", "large"}
    assert len(quality["labels"]["large"]) > 4000


@pytest.mark.parametrize("changed_source", [False, True])
def test_persistent_identity_archive_preserves_exact_sdk_source_without_private_payload(
    tmp_path, changed_source,
) -> None:
    """原SDK摘要须匹配原来源绑定；公开身份档不能带私有payload或凭据旁字段。"""
    from final_f3_preparation import digest

    runner = module("run_browser_live")
    runtime, evidence = tmp_path / "runtime", tmp_path / "evidence"
    runtime.mkdir()
    evidence.mkdir()
    bindings = {"tenant_id": "own-tenant", "user_id": "own-user", "agent_id": "own-agent",
                "session_id": "own-session", "instance_id": "own-instance",
                "source_input_id": "own-source", "turn_id": "own-source"}
    sdk = {**bindings, "pid": 424242, "call_id": "f3_sdk_" + "a" * 32,
           "request_sha256": "a" * 64,
           "identity_sha256": {key: digest(value) for key, value in bindings.items()},
           "status": "completed", "actual_sdk_transport": True,
           "actual_provider_call": True, "usage": {"prompt_tokens": 23},
           "private_credential": "SYNTHETIC-PAYLOAD-CANARY"}
    if changed_source:
        sdk["source_input_id"] = "different-source"
    raw = {"sdk_calls": [sdk], "attention": [{"payload": {
        "private_credential": "SYNTHETIC-PAYLOAD-CANARY"}}]}
    (runtime / "facts-after-case-one.private.json").write_text(json.dumps({
        "case_title": "synthetic archive boundary only", "original_facts": raw,
    }))
    result = runner.archive_identity_bindings(runtime, evidence)
    assert result["valid"] is (not changed_source)
    archive = json.loads((evidence / "sdk-persistent-identities.safe.json").read_text())
    assert len(archive["bindings"]) == 1
    assert archive["bindings"][0]["session_id"] == "own-session"
    assert "SYNTHETIC-PAYLOAD-CANARY" not in json.dumps(archive)
    assert "private_credential" not in json.dumps(archive)


def test_persistent_identity_archive_missing_original_readback_is_refused(tmp_path) -> None:
    """SDK摘要本身不能代替实际持久身份读回，缺文件保持待验。"""
    runner = module("run_browser_live")
    assert runner.archive_identity_bindings(tmp_path, tmp_path)["valid"] is False


@pytest.mark.parametrize("conflicting_digest", [False, True])
def test_memory_persistent_identity_archive_uses_original_wire_request_digest(tmp_path, conflicting_digest) -> None:
    """记忆原wire字段可精确归档；同时给出两种请求摘要但不一致时拒绝。"""
    from final_f3_preparation import digest

    runner = module("run_browser_live")
    runtime, evidence = tmp_path / "runtime", tmp_path / "evidence"
    runtime.mkdir()
    evidence.mkdir()
    bindings = {"tenant_id": "own-tenant", "user_id": "own-user", "agent_id": "own-agent",
                "session_id": "own-session", "source_input_id": "own-source", "turn_id": "own-source"}
    sdk = {**bindings, "pid": 424242, "call_id": "f3_sdk_" + "a" * 32,
           "sdk_request_sha256": "a" * 64,
           "identity_sha256": {key: digest(value) for key, value in bindings.items()},
           "status": "completed", "persistent_binding_valid": True,
           "usage": {"prompt_tokens": 23}, "private_payload": "SYNTHETIC-PRIVATE-CANARY"}
    if conflicting_digest:
        sdk["request_sha256"] = "b" * 64
    (runtime / "memory-facts-after-case-one.private.json").write_text(json.dumps({
        "original_memory_states": [{"original_state": {"sdk_calls": [sdk]}}]}))
    result = runner.archive_identity_bindings(runtime, evidence)
    assert result["valid"] is (not conflicting_digest)
    if not conflicting_digest:
        assert result["bindings"][0]["request_sha256"] == "a" * 64
    assert "SYNTHETIC-PRIVATE-CANARY" not in (evidence / "sdk-persistent-identities.safe.json").read_text()


def test_memory_freeze_causality_projection_requires_source_job_record_sdk_and_consumer_closure(tmp_path):
    """冻结记忆诊断仅在source→job→record→真实SDK及DTA答案原读回闭合时过门。"""
    import copy
    import hashlib

    runner = module("run_browser_live", I2_CURRENT_HARNESS)
    runtime, evidence = tmp_path / "runtime", tmp_path / "evidence"
    runtime.mkdir(mode=0o700)
    evidence.mkdir()

    def identity(value):
        """为冻结诊断中的结构化字段生成稳定摘要。"""

        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest()

    def stage(name, source, message_hash, job_id, record_ids):
        """构造一条 source→job→record→SDK 闭合阶段的合成证据。"""

        return {"criterion": "memory-freeze-capture-source-" + name, "stage": name,
            "passed": True, "source_id_sha256": identity(source), "source_content_sha256": message_hash,
            "turn_id_sha256": identity(source), "session_id_sha256": identity("session"),
            "started_at": "2026-10-12T10:00:00.000Z", "readback_at": "2026-10-12T10:00:00.025Z",
            "closure_passed": True, "poll_timeout": False, "observation_timeout_ms": 360000,
            "final_readback_grace_ms": 30000,
            "final_readback_attempted": True, "final_readback_completed": True,
            "post_cutoff_readback_completed": False, "cutoff_at": None,
            "poll_elapsed_ms_at_success": 0,
            "capture_job_count": 1, "capture_job_id_sha256": identity(job_id),
            "capture_job_source_id_sha256": identity(source),
            "capture_job_assistant_id_sha256": identity("assistant-" + source),
            "capture_job_assistant_linked": True,
            "capture_job_status": "completed", "capture_record_count": len(record_ids),
            "capture_record_ids_sha256": identity(sorted(record_ids)),
            "capture_sdk_call_count": 1, "capture_sdk_task_linked_count": 1,
            "capture_sdk_persistent_binding_count": 1, "capture_sdk_transport_count": 1,
            "capture_sdk_request_sha256": identity(["c" * 64]),
            "capture_sdk_call_ids_sha256": identity(["f3_sdk_" + "a" * 32]),
            "capture_sdk_actual_provider_call_count": 1, "wait_ms": 25,
            "capture_record_job_linked": True}

    correction, fresh = "source-correction", "source-fresh"
    consumer_source, job_ids = "source-consumer", {correction: "job-correction", fresh: "job-fresh"}
    corrections = {correction: "d" * 64, fresh: "e" * 64, consumer_source: "f" * 64}
    record_ids = {correction: ["record-correction"], fresh: ["record-fresh"]}
    capture_calls = []
    records, jobs = [], []
    messages = []
    for source in (correction, fresh):
        job = job_ids[source]
        jobs.append({"id": job, "tenant_id": "tenant", "user_id": "owner", "agent_id": "agent",
            "status": "completed", "source_message_id": source,
            "source_assistant_message_id": "assistant-" + source, "source_sequence": 1})
        records.extend({"id": item, "capture_job_id": job, "source_input_id": source,
            "source_sequence": 1, "hash": "a" * 64} for item in record_ids[source])
        messages.extend([
            {"id": source, "role": "user", "tenant_id": "tenant", "session_id": "session",
             "content_sha256": corrections[source]},
            {"id": "assistant-" + source, "role": "assistant", "tenant_id": "tenant",
             "session_id": "session", "turn_id": source, "user_message_id": source}])
        capture_calls.append({"call_id": "f3_sdk_" + "a" * 32, "task_id": job,
            "source_input_id": source, "operation": "memory.capture", "persistent_binding_valid": True,
            "tenant_id": "tenant", "user_id": "owner", "agent_id": "agent", "session_id": "session",
            "turn_id": source, "actual_sdk_transport": True, "actual_provider_call": True,
            "sdk_request_sha256": "c" * 64})
    executions = [{"id": "consumer-execution", "kind": "dynamic_task", "source_input_id": consumer_source,
        "status": "succeeded", "tenant_id": "tenant", "agent_id": "agent",
        "initiator_user_id": "owner", "session_id": "session"}]
    selected = [{"id": "record-fresh", "hash": "a" * 64, "currently_allowed": True}]
    consumer_call = {"call_id": "f3_sdk_" + "b" * 32, "source_input_id": consumer_source,
        "instance_id": "consumer-execution", "operation": "dynamic_task.answer",
        "status": "completed", "persistent_binding_valid": True, "actual_sdk_transport": True,
        "actual_provider_call": True, "selected": selected}
    answer_content = "NAME=柳南\nSTYLE=简洁要点"
    answer_digest = hashlib.sha256(answer_content.encode()).hexdigest()
    messages.extend([
        {"id": consumer_source, "role": "user", "tenant_id": "tenant", "session_id": "session",
         "content_sha256": corrections[consumer_source]},
        {"id": "consumer-answer", "role": "assistant", "tenant_id": "tenant", "session_id": "session",
         "execution_id": "consumer-execution",
         "content_sha256": answer_digest,
         "labels": {"NAME": "柳南", "STYLE": "简洁要点"}},
    ])
    state = {"tenant_id": "tenant", "user_id": "owner", "agent_id": "agent",
        "executions": executions, "messages": messages, "jobs": jobs, "records": records,
        "signals": [], "sdk_calls": capture_calls + [consumer_call]}
    snapshot_case_title = runner.DIAGNOSTIC_CASE_GREP["memory-dta-freeze"]
    snapshot_key = runner._memory_snapshot_identity_key(snapshot_case_title, 0)

    def write_readbacks(final_state, correction_state=None, fresh_state=None,
                        snapshot_at="2026-10-12T10:00:00.100Z"):
        """分别保留捕获时的私有快照和清空后的最终读回。"""
        final_path = runtime / f"memory-facts-after-case-{snapshot_key}.private.json"
        final_path.write_text(json.dumps({
            "case_title": snapshot_case_title, "retry": 0,
            "snapshot_at": snapshot_at,
            "original_memory_states": [{"branch": "DTA", "memory_case": "freeze",
                                         "original_state": final_state}]}))
        final_path.chmod(0o600)
        for name, snapshot in (("correction", correction_state or final_state),
                               ("fresh", fresh_state or final_state)):
            observed = next(item for item in criteria if item.get("stage") == name)
            stage_path = runtime / f"memory-capture-stage-{snapshot_key}-{name}.private.json"
            stage_path.write_text(json.dumps({
                "format": "f3-memory-capture-stage-private-v1", "branch": "DTA",
                "case_title": snapshot_case_title, "retry": 0,
                "memory_case": "freeze", "stage": name,
                "readback_at": observed["readback_at"],
                "source_id_sha256": observed["source_id_sha256"],
                "capture_job_id_sha256": observed["capture_job_id_sha256"],
                "capture_record_ids_sha256": observed["capture_record_ids_sha256"],
                "capture_sdk_call_ids_sha256": observed["capture_sdk_call_ids_sha256"],
                "capture_sdk_request_sha256": observed["capture_sdk_request_sha256"],
                "original_state": snapshot}))
            stage_path.chmod(0o600)

    criteria = [stage("correction", correction, corrections[correction], job_ids[correction], record_ids[correction]),
        stage("fresh", fresh, corrections[fresh], job_ids[fresh], record_ids[fresh]),
        {"criterion": "memory-freeze-cleared-new-source-is-actually-consumed", "passed": True,
         "fresh_source_input_id": fresh, "consumer_source_input_id": consumer_source,
         "consumer_execution_id": "consumer-execution", "final_answer_message_id": "consumer-answer",
         "final_answer_sha256": answer_digest, "fresh_capture_record_ids": record_ids[fresh],
         "actual_model_calls": 1, "real_model_quality": True}]
    write_readbacks(state)
    (evidence / "criteria.safe.json").write_text(json.dumps(
        [runner._safe_memory_freeze_criterion(item) for item in criteria]))

    result = runner.archive_memory_freeze_causality(runtime, evidence)
    assert result["valid"] is True
    assert result["correction"]["source_job_record_sdk_closed"] is True
    assert result["fresh_consumer"]["answer_linked_to_execution"] is True
    assert result["fresh_consumer"]["answer_content_hash_matches"] is True
    safe = (evidence / "memory-freeze-causality.safe.json").read_text()
    assert "source-correction" not in safe and "柳南" not in safe

    final_snapshot = next(runtime.glob("memory-facts-after-case-*.private.json"))
    final_payload = json.loads(final_snapshot.read_text())
    final_payload["original_memory_states"].extend(
        {"branch": "SOP", "memory_case": f"extra-{index}", "original_state": {}}
        for index in range(12)
    )
    final_snapshot.write_text(json.dumps(final_payload))
    final_snapshot.chmod(0o600)
    thirteen_scope_result = runner.archive_memory_freeze_causality(runtime, evidence)
    assert thirteen_scope_result["valid"] is True

    correction_snapshot = next(runtime.glob("memory-capture-stage-*-correction.private.json"))
    wrong_key_snapshot = runtime / "memory-capture-stage-ffffffffffffffffffffffff-correction.private.json"
    correction_snapshot.rename(wrong_key_snapshot)
    wrong_key_result = runner.archive_memory_freeze_causality(runtime, evidence)
    assert wrong_key_result["correction"]["stage_snapshot_valid"] is False
    wrong_key_snapshot.rename(correction_snapshot)
    final_snapshot = next(runtime.glob("memory-facts-after-case-*.private.json"))
    wrong_final_snapshot = runtime / "memory-facts-after-case-eeeeeeeeeeeeeeeeeeeeeeee.private.json"
    final_snapshot.rename(wrong_final_snapshot)
    wrong_final_result = runner.archive_memory_freeze_causality(runtime, evidence)
    assert wrong_final_result["valid"] is False
    wrong_final_snapshot.rename(final_snapshot)
    correction_snapshot_bytes = correction_snapshot.read_bytes()
    correction_snapshot.unlink()
    missing_stage = runner.archive_memory_freeze_causality(runtime, evidence)
    assert missing_stage["valid"] is False
    assert missing_stage["correction"]["stage_snapshot_valid"] is False
    write_readbacks(state)

    duplicate_snapshot = runtime / "memory-capture-stage-abcdef0123456789abcdef01-correction.private.json"
    duplicate_snapshot.write_bytes(correction_snapshot_bytes)
    duplicate_snapshot.chmod(0o600)
    duplicate_stage = runner.archive_memory_freeze_causality(runtime, evidence)
    assert duplicate_stage["valid"] is False
    assert duplicate_stage["correction"]["stage_snapshot_valid"] is False
    duplicate_snapshot.unlink()
    write_readbacks(state)

    cleared_state = copy.deepcopy(state)
    cleared_state["records"] = [row for row in cleared_state["records"]
                                if row["source_input_id"] != correction]
    cleared_state["sdk_calls"] = [row for row in cleared_state["sdk_calls"]
                                  if not (row.get("operation") == "memory.capture"
                                          and row.get("source_input_id") == correction)]
    write_readbacks(cleared_state, correction_state=state, fresh_state=state)
    after_clear = runner.archive_memory_freeze_causality(runtime, evidence)
    assert after_clear["valid"] is True
    assert after_clear["correction"]["source_job_record_sdk_closed"] is True
    assert after_clear["fresh_consumer"]["answer_linked_to_execution"] is True

    write_readbacks(cleared_state, correction_state=state, fresh_state=state,
                    snapshot_at="2026-10-12T10:00:00.020Z")
    out_of_order = runner.archive_memory_freeze_causality(runtime, evidence)
    assert out_of_order["valid"] is False
    assert out_of_order["correction"]["stage_snapshot_valid"] is False

    write_readbacks(state)

    slow_success_criteria = json.loads((evidence / "criteria.safe.json").read_text())
    slow_success_criteria[0].update({"poll_elapsed_ms_at_success": 269998,
        "readback_at": "2026-10-12T10:04:59.999Z", "wait_ms": 299999})
    (evidence / "criteria.safe.json").write_text(json.dumps(slow_success_criteria))
    slow_success = runner.archive_memory_freeze_causality(runtime, evidence)
    assert slow_success["valid"] is False
    assert slow_success["correction"]["readback_timing_valid"] is False

    (evidence / "criteria.safe.json").write_text(json.dumps(
        [runner._safe_memory_freeze_criterion(item) for item in criteria]))

    state["jobs"] = [job for job in state["jobs"] if job["id"] != job_ids[correction]]
    write_readbacks(state)
    failed = runner.archive_memory_freeze_causality(runtime, evidence)
    assert failed["valid"] is False
    assert failed["correction"]["source_job_record_sdk_closed"] is False

    jobs.append({"id": job_ids[correction], "tenant_id": "tenant", "user_id": "owner",
        "agent_id": "agent", "status": "completed", "source_message_id": correction,
        "source_sequence": 1})
    capture_calls[0]["task_id"] = "wrong-capture-job"
    write_readbacks(state)
    wrong_job = runner.archive_memory_freeze_causality(runtime, evidence)
    assert wrong_job["valid"] is False
    assert wrong_job["correction"]["source_job_record_sdk_closed"] is False

    capture_calls[0]["task_id"] = job_ids[correction]
    capture_calls[0]["sdk_request_sha256"] = "9" * 64
    write_readbacks(state)
    wrong_request = runner.archive_memory_freeze_causality(runtime, evidence)
    assert wrong_request["valid"] is False
    assert wrong_request["correction"]["source_job_record_sdk_closed"] is False

    capture_calls[0]["sdk_request_sha256"] = "c" * 64
    state["jobs"][0]["source_assistant_message_id"] = "missing-assistant"
    write_readbacks(state)
    missing_assistant = runner.archive_memory_freeze_causality(runtime, evidence)
    assert missing_assistant["valid"] is False
    assert missing_assistant["correction"]["source_job_record_sdk_closed"] is False

    state["jobs"][0]["source_assistant_message_id"] = "assistant-" + correction
    for row in state["messages"]:
        if row.get("id") == "assistant-" + correction:
            row["turn_id"] = "wrong-turn"
            row["user_message_id"] = "wrong-turn"
    write_readbacks(state)
    wrong_assistant_turn = runner.archive_memory_freeze_causality(runtime, evidence)
    assert wrong_assistant_turn["valid"] is False
    assert wrong_assistant_turn["correction"]["source_job_record_sdk_closed"] is False

    for row in state["messages"]:
        if row.get("id") == "assistant-" + correction:
            row["turn_id"] = correction
            row["user_message_id"] = correction
    state["messages"][-1]["content_sha256"] = "0" * 64
    write_readbacks(state)
    wrong_answer_hash = runner.archive_memory_freeze_causality(runtime, evidence)
    assert wrong_answer_hash["valid"] is False
    assert wrong_answer_hash["fresh_consumer"]["answer_content_hash_matches"] is False


def test_memory_freeze_archive_rejects_oversized_final_snapshot_before_decode(tmp_path):
    """私有归档器只在64MiB内解码最终状态快照，稀疏超限文件失败关闭。"""
    runner = module("run_browser_live", I4_CURRENT_HARNESS)
    runtime, evidence = tmp_path / "runtime", tmp_path / "evidence"
    runtime.mkdir(mode=0o700)
    evidence.mkdir()
    (evidence / "criteria.safe.json").write_text("[]")
    snapshot = runtime / "memory-facts-after-case-0123456789abcdef01234567.private.json"
    with snapshot.open("wb") as stream:
        stream.truncate(runner.MEMORY_FINAL_SNAPSHOT_MAX_BYTES + 1)
    snapshot.chmod(0o600)

    result = runner.archive_memory_freeze_causality(runtime, evidence)

    assert result["valid"] is False
    assert result["verification_error_class"] == "ValueError"


def test_memory_freeze_stage_timeout_readback_is_recorded_after_cutoff_without_acceptance(tmp_path):
    """超时后的持久读回单独保留截止顺序，但不能把失败阶段升级为通过。"""
    from datetime import datetime

    runner = module("run_browser_live", I4_CURRENT_HARNESS)
    runtime, evidence = tmp_path / "runtime", tmp_path / "evidence"
    runtime.mkdir(mode=0o700)
    evidence.mkdir()
    state = {"tenant_id": "tenant", "user_id": "owner", "agent_id": "agent",
             "executions": [], "messages": [], "jobs": [], "records": [], "signals": [], "sdk_calls": []}
    snapshot_title = runner.DIAGNOSTIC_CASE_GREP["memory-dta-freeze"]
    snapshot_key = runner._memory_snapshot_identity_key(snapshot_title, 0)
    private_readback = runtime / f"memory-facts-after-case-{snapshot_key}.private.json"
    private_readback.write_text(json.dumps({
        "case_title": snapshot_title, "retry": 0, "snapshot_at": "2026-10-12T10:06:00.150Z",
        "original_memory_states": [{"branch": "DTA", "memory_case": "freeze", "original_state": state}]}))
    private_readback.chmod(0o600)
    criterion = {"criterion": "memory-freeze-capture-source-correction", "stage": "correction",
        "passed": False, "started_at": "2026-10-12T10:00:00.000Z",
        "cutoff_at": "2026-10-12T10:06:00.000Z", "readback_at": "2026-10-12T10:06:00.100Z",
        "poll_timeout": True, "failure_reason": "capture_poll_timeout",
        "observation_timeout_ms": 360000, "final_readback_grace_ms": 30000,
        "poll_elapsed_ms_at_failure": 360000,
        "closure_passed": False, "final_readback_attempted": True,
        "final_readback_completed": True, "post_cutoff_readback_completed": True,
        "capture_job_status": "running", "wait_ms": 360100}
    (evidence / "criteria.safe.json").write_text(json.dumps([criterion]))

    result = runner.archive_memory_freeze_causality(runtime, evidence)

    safe = json.loads((evidence / "memory-freeze-causality.safe.json").read_text())
    assert result["valid"] is False
    assert safe["correction"]["valid"] is False
    assert safe["correction"]["source_job_record_sdk_closed"] is False
    assert safe["correction"]["final_readback_completed"] is True
    assert safe["correction"]["post_cutoff_readback_completed"] is True
    assert safe["correction"]["capture_job_status"] == "running"
    assert datetime.fromisoformat(safe["correction"]["readback_at"].replace("Z", "+00:00")) > datetime.fromisoformat(
        safe["correction"]["cutoff_at"].replace("Z", "+00:00"))

    criterion["readback_at"] = "2026-10-12T10:05:59.900Z"
    (evidence / "criteria.safe.json").write_text(json.dumps([criterion]))
    out_of_order = runner.archive_memory_freeze_causality(runtime, evidence)
    assert out_of_order["correction"]["valid"] is False
    assert out_of_order["correction"]["source_job_record_sdk_closed"] is False

    criterion["readback_at"] = "2026-10-12T10:06:00.100Z"
    criterion["cutoff_at"] = "2026-10-12T10:05:59.000Z"
    criterion["poll_elapsed_ms_at_failure"] = 359000
    criterion["wait_ms"] = 359000
    (evidence / "criteria.safe.json").write_text(json.dumps([criterion]))
    premature_timeout = runner.archive_memory_freeze_causality(runtime, evidence)
    assert premature_timeout["correction"]["valid"] is False
    assert premature_timeout["correction"]["source_job_record_sdk_closed"] is False

    criterion.update({"cutoff_at": "2026-10-12T10:06:31.000Z",
        "readback_at": "2026-10-12T10:06:31.100Z", "poll_elapsed_ms_at_failure": 391000,
        "wait_ms": 391100})
    (evidence / "criteria.safe.json").write_text(json.dumps([criterion]))
    overlong_timeout = runner.archive_memory_freeze_causality(runtime, evidence)
    assert overlong_timeout["correction"]["valid"] is False
    assert overlong_timeout["correction"]["readback_timing_valid"] is False

    criterion.update({"cutoff_at": None, "readback_at": "2026-10-12T10:06:40.000Z",
        "poll_elapsed_ms_at_failure": 400000, "wait_ms": 400000,
        "poll_timeout": False, "failure_reason": "capture_poll_error",
        "post_cutoff_readback_completed": False})
    (evidence / "criteria.safe.json").write_text(json.dumps([criterion]))
    delayed_non_timeout = runner.archive_memory_freeze_causality(runtime, evidence)
    assert delayed_non_timeout["correction"]["valid"] is False
    assert delayed_non_timeout["correction"]["poll_timeout"] is False
    assert delayed_non_timeout["correction"]["failure_reason"] == "capture_poll_error"
    assert delayed_non_timeout["correction"]["readback_timing_valid"] is False

    criterion.update({"readback_at": "2026-10-12T10:00:40.001Z",
        "poll_elapsed_ms_at_failure": 10000, "wait_ms": 40001})
    (evidence / "criteria.safe.json").write_text(json.dumps([criterion]))
    overlong_non_timeout_readback = runner.archive_memory_freeze_causality(runtime, evidence)
    assert overlong_non_timeout_readback["correction"]["valid"] is False
    assert overlong_non_timeout_readback["correction"]["poll_timeout"] is False
    assert overlong_non_timeout_readback["correction"]["readback_timing_valid"] is False

    criterion.update({"passed": True, "poll_timeout": False, "failure_reason": None,
        "cutoff_at": None, "closure_passed": True,
        "readback_at": "2026-10-12T10:07:00.001Z", "poll_elapsed_ms_at_success": 390000,
        "poll_elapsed_ms_at_failure": None, "wait_ms": 420001,
        "post_cutoff_readback_completed": False})
    (evidence / "criteria.safe.json").write_text(json.dumps([criterion]))
    overbudget_success = runner.archive_memory_freeze_causality(runtime, evidence)
    assert overbudget_success["correction"]["valid"] is False
    assert overbudget_success["correction"]["readback_timing_valid"] is False

    criterion.update({"passed": False, "closure_passed": False,
        "poll_timeout": False, "failure_reason": "capture_poll_error",
        "cutoff_at": None, "readback_at": "2026-10-12T10:00:15.000Z",
        "poll_elapsed_ms_at_success": None,
        "poll_elapsed_ms_at_failure": 15000, "wait_ms": 16000,
        "post_cutoff_readback_completed": False})
    (evidence / "criteria.safe.json").write_text(json.dumps([criterion]))
    non_timeout_failure = runner.archive_memory_freeze_causality(runtime, evidence)
    assert non_timeout_failure["correction"]["valid"] is False
    assert non_timeout_failure["correction"]["poll_timeout"] is False
    assert non_timeout_failure["correction"]["failure_reason"] == "capture_poll_error"
    assert non_timeout_failure["correction"]["readback_timing_valid"] is True


@pytest.mark.parametrize("poll_result", ["success", "timeout", "error"])
def test_memory_freeze_poll_360001ms_is_rejected_independently_of_readback(tmp_path, poll_result):
    """三种观察结果都拒绝poll超出360秒1毫秒，读回宽限不能借给poll。"""
    runner = module("run_browser_live", I4_CURRENT_HARNESS)
    runtime, evidence = tmp_path / "runtime", tmp_path / "evidence"
    runtime.mkdir(mode=0o700)
    evidence.mkdir()
    empty_state = {"tenant_id": "tenant", "user_id": "owner", "agent_id": "agent",
        "executions": [], "messages": [], "jobs": [], "records": [], "signals": [], "sdk_calls": []}
    snapshot_title = runner.DIAGNOSTIC_CASE_GREP["memory-dta-freeze"]
    snapshot_key = runner._memory_snapshot_identity_key(snapshot_title, 0)
    private_readback = runtime / f"memory-facts-after-case-{snapshot_key}.private.json"
    private_readback.write_text(json.dumps({
        "case_title": snapshot_title, "retry": 0, "snapshot_at": "2026-10-12T10:06:00.200Z",
        "original_memory_states": [{"branch": "DTA", "memory_case": "freeze", "original_state": empty_state}]}))
    private_readback.chmod(0o600)
    criterion = {"criterion": "memory-freeze-capture-source-correction", "stage": "correction",
        "started_at": "2026-10-12T10:00:00.000Z", "observation_timeout_ms": 360000,
        "final_readback_grace_ms": 30000, "final_readback_attempted": True,
        "final_readback_completed": True, "closure_passed": False,
        "source_id_sha256": "0" * 64, "capture_job_count": 0, "capture_record_count": 0,
        "capture_sdk_call_count": 0, "capture_sdk_task_linked_count": 0,
        "capture_job_status": "running"}
    if poll_result == "success":
        criterion.update({"passed": True, "poll_timeout": False, "closure_passed": True,
            "cutoff_at": None, "readback_at": "2026-10-12T10:06:00.002Z",
            "poll_elapsed_ms_at_success": 360001, "poll_elapsed_ms_at_failure": None,
            "post_cutoff_readback_completed": False, "wait_ms": 360002})
    elif poll_result == "timeout":
        criterion.update({"passed": False, "poll_timeout": True, "failure_reason": "capture_poll_timeout",
            "cutoff_at": "2026-10-12T10:06:00.001Z", "readback_at": "2026-10-12T10:06:00.002Z",
            "poll_elapsed_ms_at_failure": 360001, "poll_elapsed_ms_at_success": None,
            "post_cutoff_readback_completed": True, "wait_ms": 360002})
    else:
        criterion.update({"passed": False, "poll_timeout": False, "failure_reason": "capture_poll_error",
            "cutoff_at": None, "readback_at": "2026-10-12T10:06:00.002Z",
            "poll_elapsed_ms_at_failure": 360001, "poll_elapsed_ms_at_success": None,
            "post_cutoff_readback_completed": False, "wait_ms": 360002})
    (evidence / "criteria.safe.json").write_text(json.dumps([criterion]))

    result = runner.archive_memory_freeze_causality(runtime, evidence)

    assert result["correction"]["readback_timing_valid"] is False


def test_memory_freeze_archive_hashes_identity_fields_in_safe_criteria(tmp_path):
    """记忆冻结诊断公开附件对来源、任务与消息身份只留哈希。"""
    import base64

    runner = module("run_browser_live", I4_CURRENT_HARNESS)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    marker = "PRIVATE-SOURCE-IDENTITY-CANARY"
    criterion = {"criterion": "memory-freeze-cleared-new-source-is-actually-consumed",
        "passed": True, "source_input_id": marker, "consumer_execution_id": "PRIVATE-EXECUTION-CANARY",
        "fresh_capture_record_ids": ["PRIVATE-RECORD-CANARY"],
        "nested": {"assistant_message_id": "PRIVATE-ASSISTANT-CANARY"}, "real_model_quality": True}
    report = {"suites": [{"specs": [{"title": "memory-freeze diagnostic", "tests": [{"results": [{
        "status": "failed", "duration": 1, "retry": 0, "errors": [], "attachments": [{
            "name": "f3-safe-criterion", "body": base64.b64encode(json.dumps(criterion).encode()).decode()}]}]}]}]}]}

    runner.archive(report, evidence)

    safe = (evidence / "criteria.safe.json").read_text()
    assert marker not in safe
    assert "PRIVATE-EXECUTION-CANARY" not in safe
    assert "PRIVATE-RECORD-CANARY" not in safe
    assert "PRIVATE-ASSISTANT-CANARY" not in safe
    archived = json.loads(safe)[0]
    assert archived["source_input_id_sha256"] == runner._memory_freeze_identity_sha(marker)
    assert "consumer_execution_id" not in archived
    assert "assistant_message_id_sha256" in archived["nested"]


def test_memory_freeze_case_gets_budget_above_all_observation_deadlines():
    """冻结旅程case只消费活动阶段剩余时间，不能取得完整清理阶段预算。"""
    runner = module("run_browser_live", I4_CURRENT_HARNESS)

    assert runner.playwright_case_timeout_ms("memory-dta-freeze") == 6_330_000
    assert runner.playwright_case_timeout_ms("memory-dta-revoke") == 900_000


def test_memory_diagnostics_freeze_zero_business_side_effects_in_runtime_configuration():
    """冻结与撤权记忆诊断的运行配置必须与登记合同一样明确预期零副作用。"""
    runner = module("run_browser_live", I4_CURRENT_HARNESS)

    freeze = runner.diagnostic_side_effect_expectations("memory-dta-freeze")
    assert freeze["provider_submissions_expected"] == 0
    assert freeze["target_document_business_data_write_expected"] is False

    revoke = runner.diagnostic_side_effect_expectations("memory-dta-revoke")
    assert revoke["provider_submissions_expected"] == 0
    assert revoke["target_document_business_data_write_expected"] is False


def test_original_sdk_retry_calls_remain_distinct_across_repeated_persistent_readback(tmp_path) -> None:
    """同输入失败后合法重试保原call_id与两实际状态；多个afterEach读回不重复算调用。"""
    from final_f3_preparation import digest

    runner = module("run_browser_live")
    runtime, evidence = tmp_path / "runtime", tmp_path / "evidence"
    runtime.mkdir()
    evidence.mkdir()
    bindings = {"tenant_id": "own-tenant", "user_id": "own-user", "agent_id": "own-agent",
                "session_id": "own-session", "source_input_id": "own-source", "turn_id": "own-source"}
    base = {**bindings, "pid": 424242, "request_sha256": "a" * 64,
            "identity_sha256": {key: digest(value) for key, value in bindings.items()}}
    calls = [{**base, "call_id": "f3_sdk_" + "a" * 32, "status": "failed", "usage": {}},
             {**base, "call_id": "f3_sdk_" + "b" * 32, "status": "completed",
              "usage": {"prompt_tokens": 23}}]
    for number in (1, 2):
        (runtime / f"facts-after-case-{number}.private.json").write_text(json.dumps({
            "original_facts": {"sdk_calls": calls}}))
    result = runner.archive_identity_bindings(runtime, evidence)
    assert result["valid"] is True
    assert len(result["bindings"]) == 2
    assert {row["status"] for row in result["bindings"]} == {"failed", "completed"}


@pytest.mark.parametrize("resolved_in_other_scope", [False, True])
def test_business_sdk_unmapped_scope_requires_exact_other_readback(tmp_path, resolved_in_other_scope) -> None:
    """业务hash存在时不能冒称无身份预检；另一精确scope可补原call关联，否则整轮待验。"""
    from final_f3_preparation import digest

    runner = module("run_browser_live")
    runtime, evidence = tmp_path / "runtime", tmp_path / "evidence"
    runtime.mkdir()
    evidence.mkdir()
    bindings = {"tenant_id": "own-tenant", "user_id": "own-user", "agent_id": "own-agent",
                "session_id": "own-session", "source_input_id": "own-source", "turn_id": "own-source"}
    complete = {**bindings, "pid": 424242, "request_sha256": "a" * 64,
                "call_id": "f3_sdk_" + "a" * 32,
                "identity_sha256": {key: digest(value) for key, value in bindings.items()},
                "status": "completed", "usage": {"prompt_tokens": 23}}
    unresolved = {**complete, **{key: None for key in bindings}}
    other = {**complete, "call_id": "f3_sdk_" + "b" * 32}
    (runtime / "facts-after-case-one.private.json").write_text(json.dumps({
        "original_facts": {"sdk_calls": [unresolved, other]}}))
    if resolved_in_other_scope:
        (runtime / "memory-facts-after-case-two.private.json").write_text(json.dumps({
            "original_memory_states": [{"original_state": {"sdk_calls": [complete]}}]}))
    result = runner.archive_identity_bindings(runtime, evidence)
    assert result["valid"] is resolved_in_other_scope
    if resolved_in_other_scope:
        assert len(result["bindings"]) == 2


@pytest.mark.parametrize(("frozen_config_sha", "connection_success", "duplicate_in_snapshot",
                         "expected_valid"), [
    ("8" * 64, True, False, True), ("8" * 64, False, False, False),
    ("c" * 64, True, False, False), (None, True, False, False),
    ("8" * 64, True, True, False),
])
def test_model_preflight_sdk_calls_use_separate_exact_diagnostic_scope(
        tmp_path, frozen_config_sha, connection_success, duplicate_in_snapshot,
        expected_valid) -> None:
    """模型诊断须报告真实成功，重复跨快照读取可折叠、冻结指纹须精确。"""
    from final_f3_preparation import digest

    runner = module("run_browser_live", I2_CURRENT_HARNESS)
    runtime, evidence = tmp_path / "runtime", tmp_path / "evidence"
    runtime.mkdir()
    evidence.mkdir()
    invocation_sha = "1" * 64
    tenant_sha = digest("SYNTHETIC_TENANT")
    actor_sha = digest("SYNTHETIC_ADMIN")
    model_sha = digest("SYNTHETIC_MODEL_CONFIG")
    model_id_sha = digest("SYNTHETIC_MODEL_CONFIG_ID")
    connection = {
        "schema": "model-preflight-v1",
        "invocation_sha256": invocation_sha,
        "tenant_sha256": tenant_sha,
        "admin_actor_sha256": actor_sha,
        "model_config_sha256": model_sha,
        "config_fingerprint_sha256": "8" * 64,
        "probe_class": "model_connection",
    }
    connection_call = {
        "pid": 424242,
        "call_id": "f3_sdk_" + "a" * 32,
        "request_sha256": "6" * 64,
        "identity_sha256": {},
        "diagnostic_scope": connection,
        "request_class": "model_catalog",
        "observation_layer": "final_sdk_models_list",
        "started_at_utc": "2026-10-13T10:11:12+00:00",
        "status": "completed",
        "observation_valid": True,
        "actual_sdk_transport": True,
        "actual_provider_call": True,
        "kind": None,
        "source_sha256": None,
        "usage": {},
    }
    capability_invocation_sha = "7" * 64
    capabilities = {
        **connection,
        "invocation_sha256": capability_invocation_sha,
        "config_fingerprint_sha256": "8" * 64,
        "probe_class": "native_capabilities",
    }
    capability_call = {
        **connection_call,
        "call_id": "f3_sdk_" + "b" * 32,
        "request_sha256": "9" * 64,
        "diagnostic_scope": capabilities,
        "request_class": "tool_call",
        "observation_layer": "final_sdk_create",
        "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
    }
    for name in ("one", "two", "three"):
        calls = [connection_call, capability_call]
        if duplicate_in_snapshot and name == "one":
            calls.append(connection_call)
        (runtime / f"facts-after-case-{name}.private.json").write_text(json.dumps({
            "original_facts": {"sdk_calls": calls},
        }))
    (evidence / "criteria.safe.json").write_text(json.dumps([{
        "criterion": "actual-model-connection-diagnostic",
        "model_config_id_sha256": model_id_sha,
        "model_config_sha256": model_sha,
        "tenant_sha256": tenant_sha,
        "admin_actor_sha256": actor_sha,
        "diagnostic_invocation_sha256": invocation_sha,
        "config_fingerprint_sha256": "8" * 64,
        "response_http_status": 200,
        "diagnostic_success": connection_success,
    }, {
        "criterion": "actual-model-native-capabilities",
        "passed": True,
        "model_config_id_sha256": model_id_sha,
        "model_config_sha256": model_sha,
        "tenant_sha256": tenant_sha,
        "admin_actor_sha256": actor_sha,
        "diagnostic_invocation_sha256": capability_invocation_sha,
        "config_fingerprint_sha256": "8" * 64,
        "connection_diagnostic_success": connection_success,
    }]))
    seal_candidate_private_tree(runtime)

    result = runner.archive_identity_bindings(
        runtime, evidence, expected_model_configuration_sha256=frozen_config_sha,
    )
    archive = json.loads((evidence / "sdk-persistent-identities.safe.json").read_text())
    assert result["valid"] is expected_valid
    if duplicate_in_snapshot:
        assert result.get("verification_error_class") == "RuntimeError"
        assert result["sdk_observation_row_count"] == 3
        assert archive["diagnostic_bindings"] == []
        assert archive["unscoped_sdk_observations"] == 0
        return
    if not expected_valid:
        assert result["diagnostic_binding_errors"] > 0
    assert archive["bindings"] == []
    assert len(archive["diagnostic_bindings"]) == 2
    assert result["sdk_observation_row_count"] == 6
    assert archive["unscoped_sdk_observations"] == 0


def test_model_preflight_repeated_after_restart_closes_each_unique_invocation(tmp_path) -> None:
    """真重启可重新执行两项预检；不同 invocation 均有原SDK旁证时应闭合。"""
    from final_f3_preparation import digest

    runner = module("run_browser_live", I2_CURRENT_HARNESS)
    runtime, evidence = tmp_path / "runtime", tmp_path / "evidence"
    runtime.mkdir()
    evidence.mkdir()
    tenant_sha = digest("SYNTHETIC_TENANT")
    actor_sha = digest("SYNTHETIC_ADMIN")
    model_sha = digest("SYNTHETIC_MODEL_CONFIG")
    model_id_sha = digest("SYNTHETIC_MODEL_CONFIG_ID")
    config_sha = "8" * 64

    criteria = []
    for process_index, pid in enumerate((424242, 424243)):
        connection_invocation = (str(process_index + 1) * 64)
        capability_invocation = (str(process_index + 3) * 64)
        calls = []
        for call_index, (probe, invocation, request_class, layer) in enumerate((
                ("model_connection", connection_invocation, "model_catalog", "final_sdk_models_list"),
                ("native_capabilities", capability_invocation, "structured_output", "final_sdk_create"),
        )):
            calls.append({
                "pid": pid, "call_id": "f3_sdk_" + f"{process_index}{call_index}" * 16,
                "request_sha256": f"{process_index}{call_index}" * 32,
                "identity_sha256": {},
                "diagnostic_scope": {"schema": "model-preflight-v1",
                    "invocation_sha256": invocation, "tenant_sha256": tenant_sha,
                    "admin_actor_sha256": actor_sha, "model_config_sha256": model_sha,
                    "config_fingerprint_sha256": config_sha, "probe_class": probe},
                "request_class": request_class, "observation_layer": layer,
                "started_at_utc": "2026-10-13T10:11:12+00:00", "status": "completed",
                "observation_valid": True, "actual_sdk_transport": True,
                "actual_provider_call": True, "usage": {},
            })
            common = {"criterion": (
                "actual-model-connection-diagnostic"
                if probe == "model_connection" else "actual-model-native-capabilities"),
                "model_config_id_sha256": model_id_sha, "model_config_sha256": model_sha,
                "tenant_sha256": tenant_sha, "admin_actor_sha256": actor_sha,
                "diagnostic_invocation_sha256": invocation,
                "config_fingerprint_sha256": config_sha,
            }
            criteria.append({**common, **(
                {"response_http_status": 200, "diagnostic_success": True}
                if probe == "model_connection" else
                {"passed": True, "connection_diagnostic_success": True}
            )})
        (runtime / f"facts-after-case-{process_index}.private.json").write_text(
            json.dumps({"original_facts": {"sdk_calls": calls}})
        )
    (evidence / "criteria.safe.json").write_text(json.dumps(criteria))
    seal_candidate_private_tree(runtime)
    result = runner.archive_identity_bindings(
        runtime, evidence, expected_model_configuration_sha256=config_sha,
    )

    assert result["valid"] is True
    assert result["diagnostic_binding_errors"] == 0
    assert len(result["diagnostic_bindings"]) == 4


def test_model_preflight_diagnostics_are_closed_from_global_sdk_ledger_when_memory_snapshot_is_scoped(
        tmp_path,
) -> None:
    """记忆作用域快照只含业务调用时，模型预检须从同一轮原SDK账本闭合。"""
    from final_f3_preparation import digest

    runner = module("run_browser_live", I2_CURRENT_HARNESS)
    runtime, evidence = tmp_path / "runtime", tmp_path / "evidence"
    runtime.mkdir()
    evidence.mkdir()
    tenant_sha = digest("SYNTHETIC_TENANT")
    actor_sha = digest("SYNTHETIC_ADMIN")
    model_sha = digest("SYNTHETIC_MODEL_CONFIG")
    config_sha = "8" * 64
    connection_invocation = "1" * 64
    capability_invocation = "2" * 64

    def diagnostic_call(call_id: str, request_sha: str, invocation: str, probe: str,
                        request_class: str, layer: str) -> dict:
        """构造带同轮范围摘要的模型预检 SDK 记录。"""

        return {
            "pid": 424242,
            "call_id": call_id,
            "request_sha256": request_sha,
            "observation_valid": True,
            "diagnostic_scope": {
                "schema": "model-preflight-v1", "invocation_sha256": invocation,
                "tenant_sha256": tenant_sha, "admin_actor_sha256": actor_sha,
                "model_config_sha256": model_sha, "config_fingerprint_sha256": config_sha,
                "probe_class": probe,
            },
            "request_class": request_class,
            "observation_layer": layer,
            "started_at_utc": "2026-10-13T10:11:12+00:00",
            "status": "completed",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    (runtime / "memory-facts-after-case-one.private.json").write_text(json.dumps({
        "original_memory_states": [{"original_state": {"sdk_calls": []}}],
    }))
    diagnostics = [
        diagnostic_call("f3_sdk_" + "a" * 32, "3" * 64, connection_invocation,
                        "model_connection", "model_catalog", "final_sdk_models_list"),
        diagnostic_call("f3_sdk_" + "b" * 32, "4" * 64, capability_invocation,
                        "native_capabilities", "structured_output", "final_sdk_create"),
    ]
    (runtime / "sdk-observations-424242.safe.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in diagnostics)
    )
    (evidence / "criteria.safe.json").write_text(json.dumps([
        {
            "criterion": "actual-model-connection-diagnostic",
            "model_config_id_sha256": model_sha, "model_config_sha256": model_sha,
            "tenant_sha256": tenant_sha, "admin_actor_sha256": actor_sha,
            "diagnostic_invocation_sha256": connection_invocation,
            "config_fingerprint_sha256": config_sha,
            "response_http_status": 200, "diagnostic_success": True,
        },
        {
            "criterion": "actual-model-native-capabilities", "passed": True,
            "model_config_id_sha256": model_sha, "model_config_sha256": model_sha,
            "tenant_sha256": tenant_sha, "admin_actor_sha256": actor_sha,
            "diagnostic_invocation_sha256": capability_invocation,
            "config_fingerprint_sha256": config_sha,
            "connection_diagnostic_success": True,
        },
    ]))
    seal_candidate_private_tree(runtime)

    result = runner.archive_identity_bindings(
        runtime, evidence, expected_model_configuration_sha256=config_sha,
    )

    assert result["valid"] is True
    assert result["diagnostic_binding_errors"] == 0
    archive = json.loads((evidence / "sdk-persistent-identities.safe.json").read_text())
    assert len(archive["diagnostic_bindings"]) == 2


def test_final_sdk_receipt_archive_rejects_duplicate_call_identity() -> None:
    """最终SDK回执以原始调用ID唯一计数，不能靠相同摘要去重掩盖重复记录。"""
    runner = module("run_browser_live", I2_CURRENT_HARNESS)
    row = {
        "call_id": "f3_sdk_" + "a" * 32,
        "request_sha256": "b" * 64,
        "observation_valid": True,
    }

    with pytest.raises(RuntimeError, match="F3_FINAL_SDK_RECEIPT_DUPLICATE_CALL_ID"):
        runner._safe_sdk_receipt_rows([row, dict(row)])


def test_final_sdk_receipt_archive_accepts_task_identity_from_memory_consumer() -> None:
    """最终SDK回执投影必须保留统一身份模型中的task_id绑定。"""
    runner = module("run_browser_live", I2_CURRENT_HARNESS)
    row = {
        "call_id": "f3_sdk_" + "b" * 32,
        "request_sha256": "c" * 64,
        "identity_sha256": {"task_id": "d" * 64},
        "status": "completed",
        "usage": {},
        "observation_valid": True,
    }

    projected = runner._safe_sdk_receipt_rows([row])

    assert projected[0]["identity_sha256"] == {"task_id": "d" * 64}


def test_final_sdk_wire_archive_accepts_task_identity_from_memory_consumer() -> None:
    """SDK输入旁证与回执投影使用同一完整身份字段集合。"""
    runner = module("run_browser_live", I2_CURRENT_HARNESS)
    row = {"identity_sha256": {"task_id": "e" * 64}, "sdk_request_sha256": "f" * 64}

    projected = runner._safe_wire_rows([row])

    assert projected[0]["identity_sha256"] == {"task_id": "e" * 64}


def test_model_budget_reconciliation_counts_all_budgeted_request_classes() -> None:
    """预算对账应覆盖结构化、工具、视觉和普通聊天等所有已预留派发。"""
    runner = module("run_browser_live", I2_CURRENT_HARNESS)
    rows = [
        {"request_class": "structured_output", "model_budget_profile": "profile"},
        {"request_class": "tool_call", "model_budget_profile": "profile"},
        {"request_class": "model_catalog", "model_budget_profile": None},
    ]

    budgeted = runner._model_budget_receipt_rows(rows, "profile")

    assert len(budgeted) == 2


@pytest.mark.parametrize(("criteria_names", "expected_valid"), [
    ((), True),
    (("actual-model-connection-diagnostic", "actual-model-native-capabilities"), False),
    (("actual-model-connection-diagnostic",), False),
    (("actual-model-connection-diagnostic", "actual-model-connection-diagnostic",
      "actual-model-native-capabilities"), False),
])
def test_model_diagnostic_criteria_cannot_be_skipped_when_sdk_calls_are_absent(
        tmp_path, criteria_names, expected_valid):
    """页面已声明预检时，归档必须拒绝零SDK调用、缺类别或重复criterion。"""
    from final_f3_preparation import digest

    runner = module("run_browser_live", I2_CURRENT_HARNESS)
    runtime, evidence = tmp_path / "runtime", tmp_path / "evidence"
    runtime.mkdir()
    evidence.mkdir()
    for name in ("one", "two"):
        (runtime / f"facts-after-case-{name}.private.json").write_text(json.dumps({
            "original_facts": {"sdk_calls": []},
        }))
    model_id = "SYNTHETIC_MODEL_CONFIG"
    tenant_sha, actor_sha, model_sha = (
        digest("SYNTHETIC_TENANT"), digest("SYNTHETIC_ADMIN"), digest(model_id))
    invocation_by_name = {
        "actual-model-connection-diagnostic": "1" * 64,
        "actual-model-native-capabilities": "2" * 64,
    }
    criteria = [{
        "criterion": name, "model_config_id_sha256": model_sha, "model_config_sha256": model_sha,
        "tenant_sha256": tenant_sha, "admin_actor_sha256": actor_sha,
        "diagnostic_invocation_sha256": invocation_by_name[name],
        **({"passed": True} if name == "actual-model-native-capabilities" else {}),
    } for name in criteria_names]
    (evidence / "criteria.safe.json").write_text(json.dumps(criteria))
    seal_candidate_private_tree(runtime)

    result = runner.archive_identity_bindings(runtime, evidence)

    assert result["valid"] is expected_valid
    if not expected_valid:
        assert result["diagnostic_binding_errors"] > 0


def test_required_model_preflight_rejects_zero_sdk_calls_and_no_criteria(tmp_path):
    """正式完整旅程即使没有SDK记录也必须提供两项管理模型预检criterion。"""
    runner = module("run_browser_live", I2_CURRENT_HARNESS)
    runtime, evidence = tmp_path / "runtime", tmp_path / "evidence"
    runtime.mkdir()
    evidence.mkdir()
    for name in ("one", "two"):
        (runtime / f"facts-after-case-{name}.private.json").write_text(json.dumps({
            "original_facts": {"sdk_calls": []},
        }))
    seal_candidate_private_tree(runtime)

    result = runner.archive_identity_bindings(
        runtime, evidence, require_model_preflight=True,
        expected_model_configuration_sha256="a" * 64,
    )

    assert result["valid"] is False
    assert result["diagnostic_binding_errors"] > 0


@pytest.mark.parametrize("budget_kind", ["per_file", "aggregate", "file_count", "criteria"])
def test_identity_archive_enforces_private_snapshot_and_criteria_byte_budgets(
        tmp_path, budget_kind):
    """归档读取前对私有状态快照数量、单档/总字节和安全criterion施加硬上限。"""
    runner = module("run_browser_live", I2_CURRENT_HARNESS)
    runtime, evidence = tmp_path / "runtime", tmp_path / "evidence"
    runtime.mkdir()
    evidence.mkdir()
    if budget_kind == "per_file":
        file = runtime / "facts-after-case-large.private.json"
        file.write_bytes(b" " * (runner.IDENTITY_SNAPSHOT_MAX_FILE_BYTES + 1))
    elif budget_kind == "aggregate":
        over_total_file_count = (runner.IDENTITY_SNAPSHOT_MAX_TOTAL_BYTES
                                 // runner.IDENTITY_SNAPSHOT_MAX_FILE_BYTES) + 1
        snapshot = b'{"original_facts":{"sdk_calls":[]}}'
        for index in range(over_total_file_count):
            file = runtime / f"facts-after-case-{index}.private.json"
            file.write_bytes(snapshot + b" " * (runner.IDENTITY_SNAPSHOT_MAX_FILE_BYTES - len(snapshot)))
    elif budget_kind == "file_count":
        for index in range(runner.IDENTITY_SNAPSHOT_MAX_FILES + 1):
            (runtime / f"facts-after-case-{index}.private.json").touch()
    else:
        (runtime / "facts-after-case-one.private.json").write_text(json.dumps({
            "original_facts": {"sdk_calls": []},
        }))
        (evidence / "criteria.safe.json").write_bytes(
            b" " * (runner.IDENTITY_CRITERIA_MAX_BYTES + 1))
    seal_candidate_private_tree(runtime)

    result = runner.archive_identity_bindings(runtime, evidence)

    assert result["valid"] is False
    assert result["verification_error_class"] == "RuntimeError"


def test_diagnostic_actor_must_match_browser_model_preflight_criterion(tmp_path) -> None:
    """独立诊断scope必须与页面实际管理员criterion逐调用一致。"""
    from final_f3_preparation import digest

    runner = module("run_browser_live", I2_CURRENT_HARNESS)
    runtime, evidence = tmp_path / "runtime", tmp_path / "evidence"
    runtime.mkdir()
    evidence.mkdir()
    invocation = "a" * 64
    tenant_sha = digest("SYNTHETIC_TENANT")
    model_sha = digest("SYNTHETIC_MODEL_CONFIG")
    scope = {
        "schema": "model-preflight-v1", "invocation_sha256": invocation,
        "tenant_sha256": tenant_sha, "admin_actor_sha256": digest("SERVER_ACTOR"),
        "model_config_sha256": model_sha, "config_fingerprint_sha256": "b" * 64,
        "probe_class": "model_connection",
    }
    row = {
        "pid": 424242, "call_id": "f3_sdk_" + "c" * 32,
        "request_sha256": "d" * 64, "identity_sha256": {},
        "diagnostic_scope": scope, "request_class": "chat_completion",
        "observation_layer": "final_sdk_create", "started_at_utc": "2026-10-13T10:11:12+00:00",
        "status": "completed", "usage": {}, "observation_valid": True,
        "actual_sdk_transport": True, "actual_provider_call": True,
    }
    (runtime / "facts-after-case-one.private.json").write_text(json.dumps({
        "original_facts": {"sdk_calls": [row]},
    }))
    (evidence / "criteria.safe.json").write_text(json.dumps([{
        "criterion": "actual-model-connection-diagnostic",
        "model_config_id_sha256": model_sha, "model_config_sha256": model_sha,
        "tenant_sha256": tenant_sha, "admin_actor_sha256": digest("BROWSER_ACTOR"),
        "diagnostic_invocation_sha256": invocation,
        "config_fingerprint_sha256": "b" * 64,
    }]))
    seal_candidate_private_tree(runtime)

    result = runner.archive_identity_bindings(
        runtime, evidence, expected_model_configuration_sha256="b" * 64,
    )
    archive = json.loads((evidence / "sdk-persistent-identities.safe.json").read_text())
    assert result["valid"] is False
    assert result["diagnostic_binding_errors"] > 0
    assert archive["diagnostic_bindings"][0]["diagnostic_scope"]["admin_actor_sha256"] == digest(
        "SERVER_ACTOR"
    )


def test_unscoped_sdk_call_cannot_be_mistaken_for_management_diagnostic(tmp_path) -> None:
    """无业务身份且无原路由诊断scope的SDK调用仍使持久身份门失败。"""
    runner = module("run_browser_live", I2_CURRENT_HARNESS)
    runtime, evidence = tmp_path / "runtime", tmp_path / "evidence"
    runtime.mkdir()
    evidence.mkdir()
    call = {"pid": 424242, "call_id": "f3_sdk_" + "e" * 32,
            "request_sha256": "f" * 64, "identity_sha256": {}, "diagnostic_scope": None}
    for number in (1, 2):
        (runtime / f"facts-after-case-{number}.private.json").write_text(json.dumps({
            "original_facts": {"sdk_calls": [call]},
        }))
    seal_candidate_private_tree(runtime)

    result = runner.archive_identity_bindings(runtime, evidence)
    assert result["valid"] is False
    assert result["unscoped_sdk_observations"] == 1
    assert result["sdk_observation_row_count"] == 2


def test_full_backend_disk_preflight_refuses_exhausted_owned_storage(tmp_path, monkeypatch) -> None:
    """完整后端必须在写原证据前拒绝不足磁盘，不再启动会丢JUnit的重任务。"""
    runner = module("run_backend_full")
    monkeypatch.setattr(runner.shutil, "disk_usage", lambda _path: SimpleNamespace(free=1))
    with pytest.raises(RuntimeError, match="F3_OWNED_DISK_HEADROOM_REQUIRED"):
        runner.disk_preflight(tmp_path)


@pytest.mark.parametrize("archive_failure", [False, True])
def test_closed_scratch_archive_preserves_bytes_before_removal(tmp_path, monkeypatch, archive_failure) -> None:
    """已结束临时树只有原字节归档复核成功才移除；压缩写失败保持源和日志。"""
    import hashlib
    import tarfile

    runner = module("run_backend_full")
    scratch = tmp_path / "pytest"
    scratch.mkdir()
    original = b"SYNTHETIC-OWNED-DATABASE-BYTES"
    (scratch / "one.db").write_bytes(original)
    log = tmp_path / "junit.private.xml"
    log.write_text("SYNTHETIC-ORIGINAL-RESULT")
    if archive_failure:
        def failed_open(*_args, **_kwargs):
            """复现归档磁盘写失败，不能删原始临时数据库。"""
            raise OSError("SYNTHETIC-DISK-WRITE-FAILURE")
        monkeypatch.setattr(runner.tarfile, "open", failed_open)
        with pytest.raises(OSError):
            runner.archive_closed_scratch(scratch, tmp_path)
        assert (scratch / "one.db").read_bytes() == original
    else:
        result = runner.archive_closed_scratch(scratch, tmp_path)
        assert result["content_verified"] is True
        assert not scratch.exists()
        with tarfile.open(tmp_path / "pytest-scratch.private.tar.gz", "r:gz") as archive:
            assert hashlib.sha256(archive.extractfile("pytest/one.db").read()).digest() == hashlib.sha256(original).digest()
    assert log.read_text() == "SYNTHETIC-ORIGINAL-RESULT"


def test_closed_scratch_archive_preserves_symlink_without_reading_external_target(tmp_path) -> None:
    """正式路径安全测试的原符号链接应保留链接本身，归档不得读取链接外的正文。"""
    import tarfile

    runner = module("run_backend_full")
    scratch = tmp_path / "pytest"
    scratch.mkdir()
    (scratch / "link").symlink_to("../never-read-external-private-file")
    result = runner.archive_closed_scratch(scratch, tmp_path)
    assert result["content_verified"] is True
    with tarfile.open(tmp_path / "pytest-scratch.private.tar.gz", "r:gz") as archive:
        link = archive.getmember("pytest/link")
        assert link.issym()
        assert link.linkname == "../never-read-external-private-file"
    assert not scratch.exists()


@pytest.mark.parametrize("original_exit", [False, True])
def test_backend_child_preserves_original_workspace_modes_and_private_evidence(tmp_path, monkeypatch, original_exit) -> None:
    """原容器工作区默认755可读，私有目录和证据仍600；原SystemExit也必须原样保留。"""
    import os
    import stat

    runner = module("backend_formal_child")
    assert runner.PREVIOUS == HARNESS.parents[2] / "final-f2-20261004/execution-harness"
    assert (runner.PREVIOUS / "formal_child.py").is_file()
    output = tmp_path / "formal"
    output.mkdir(mode=0o700)
    monkeypatch.setenv("F1_CHILD_OUTPUT", str(output))
    workspace = tmp_path / "workspace"

    def original(path, *, run_name):
        """模拟原pytest自身创建工作区和原JUnit，不能以旁证保护改变容器业务条件。"""
        assert Path(path) == runner.PREVIOUS / "formal_child.py"
        assert run_name == "__main__"
        workspace.mkdir()
        (output / "junit.private.xml").write_text("SYNTHETIC-ORIGINAL-JUNIT")
        if original_exit:
            raise SystemExit(1)

    monkeypatch.setattr(runner.runpy, "run_path", original)
    before = os.umask(0o077)
    try:
        if original_exit:
            with pytest.raises(SystemExit) as caught:
                runner.run_formal_child()
            assert caught.value.code == 1
        else:
            runner.run_formal_child()
        assert stat.S_IMODE(workspace.stat().st_mode) == 0o755
        assert stat.S_IMODE((output / "junit.private.xml").stat().st_mode) == 0o600
        assert os.umask(0o077) == 0o077
    finally:
        os.umask(before)


def test_i4_editor_baseline_diagnostic_does_not_change_full20_contract() -> None:
    """历史当前源只读单例维持独立合同，不把full20缩成选中用例。"""
    runner = module("run_browser_live", I4_CURRENT_HARNESS)
    full = runner.expected_case_criteria()
    diagnostic = runner.expected_case_criteria("h2-dta-editor-baseline")
    assert len(full) == 20 and sum(map(len, full.values())) == 77
    assert diagnostic == {
        "JRN08 H2 DTA editor persisted baseline read-only diagnostic": {
            "h2-editor-baseline-observation-captured",
        },
    }
    assert runner.diagnostic_environment_flags("h2-dta-editor-baseline")[
        "F3_LIVE_DIAGNOSTIC_SELECTION"
    ] == "h2-dta-editor-baseline"


def test_i4_zero_sdk_diagnostic_requires_original_empty_persistent_readback(tmp_path: Path) -> None:
    """零模型单例只在私有原事实明确读回零SDK时通过身份闭合。"""
    runner = module("run_browser_live", I4_CURRENT_HARNESS)
    runtime, evidence = tmp_path / "runtime", tmp_path / "evidence"
    runtime.mkdir()
    evidence.mkdir()
    (runtime / "facts-after-case-one.private.json").write_text(json.dumps({
        "case_title": "synthetic diagnostic fixture only",
        "original_facts": {"sdk_calls": []},
    }))
    result = runner.archive_identity_bindings(runtime, evidence)
    assert result["valid"] is True
    assert result["no_sdk_calls_observed"] is True
    assert result["readback_file_count"] == 1
    assert result["bindings"] == []


def test_i4_zero_sdk_runtime_diagnostic_requires_no_sdk_and_no_provider_submit(tmp_path: Path) -> None:
    """零模型只读诊断须证明原SDK输入和模拟provider提交均为空且自有进程已清理。"""
    runner = module("run_browser_live", I4_CURRENT_HARNESS)
    owned, evidence = tmp_path / "owned", tmp_path / "evidence"
    runtime = owned / "f3-live-sqlite"
    runtime.mkdir(parents=True)
    evidence.mkdir()
    application_pid, provider_pid = 999998, 999997
    (runtime / f"sdk-final-health-{application_pid}.safe.json").write_text(json.dumps({
        "pid": application_pid,
        "original_sdk": {
            "valid": False, "client_state_count": 0, "started_count": 0,
            "settled_count": 0, "in_flight_count": 0, "observation_error_count": 0,
            "receipt_count": 0, "file_matches": True, "write_failed": False,
        },
        "wire_inputs": {"valid": True, "receipt_count": 0, "error_count": 0},
    }))
    (owned / "provider-final-observations.safe.json").write_text(json.dumps({
        "observation_healthy": True, "observation_error_count": 0,
        "rows": [], "submit_http_count": 0,
    }))
    (owned / "provider-process-cleanup.safe.json").write_text(json.dumps({
        "actual_pid": provider_pid, "process_absent": True,
    }))
    (owned / "application-process-cleanup.safe.json").write_text(json.dumps({
        "cleanup_complete": True,
    }))
    assert runner.archive_runtime_evidence(
        owned, evidence, "sqlite", require_model_usage=False,
    ) is True


def test_i4_zero_sdk_runtime_diagnostic_rejects_provider_submission(tmp_path: Path) -> None:
    """专用零模型验收器不能把任意一次模拟provider提交归入只读诊断。"""
    runner = module("run_browser_live", I4_CURRENT_HARNESS)
    owned, evidence = tmp_path / "owned", tmp_path / "evidence"
    runtime = owned / "f3-live-sqlite"
    runtime.mkdir(parents=True)
    evidence.mkdir()
    application_pid, provider_pid = 999998, 999997
    (runtime / f"sdk-final-health-{application_pid}.safe.json").write_text(json.dumps({
        "pid": application_pid,
        "original_sdk": {
            "valid": False, "client_state_count": 0, "started_count": 0,
            "settled_count": 0, "in_flight_count": 0, "observation_error_count": 0,
            "receipt_count": 0, "file_matches": True, "write_failed": False,
        },
        "wire_inputs": {"valid": True, "receipt_count": 0, "error_count": 0},
    }))
    (owned / "provider-final-observations.safe.json").write_text(json.dumps({
        "observation_healthy": True, "observation_error_count": 0,
        "rows": [], "submit_http_count": 1,
    }))
    (owned / "provider-process-cleanup.safe.json").write_text(json.dumps({
        "actual_pid": provider_pid, "process_absent": True,
    }))
    (owned / "application-process-cleanup.safe.json").write_text(json.dumps({
        "cleanup_complete": True,
    }))
    assert runner.archive_runtime_evidence(
        owned, evidence, "sqlite", require_model_usage=False,
    ) is False


def test_i4_zero_sdk_runtime_diagnostic_rejects_sdk_receipt(tmp_path: Path) -> None:
    """专用零模型验收器即使收到真实SDK回执也必须拒绝诊断闭合。"""
    runner = module("run_browser_live", I4_CURRENT_HARNESS)
    owned, evidence = tmp_path / "owned", tmp_path / "evidence"
    runtime = owned / "f3-live-sqlite"
    runtime.mkdir(parents=True)
    evidence.mkdir()
    application_pid, provider_pid = 999998, 999997
    request_hash = "a" * 64
    (runtime / f"sdk-final-health-{application_pid}.safe.json").write_text(json.dumps({
        "pid": application_pid,
        "original_sdk": {
            "valid": True, "started_count": 1, "settled_count": 1,
            "receipt_count": 1, "observation_error_count": 0,
        },
        "wire_inputs": {"valid": True, "receipt_count": 1, "error_count": 0},
    }))
    (runtime / f"sdk-observations-{application_pid}.safe.jsonl").write_text(json.dumps({
        "request_sha256": request_hash, "status": "completed", "usage": {"prompt_tokens": 1},
    }) + "\n")
    (runtime / f"live-wire-{application_pid}.private.jsonl").write_text(json.dumps({
        "sdk_request_sha256": request_hash,
    }) + "\n")
    (owned / "provider-final-observations.safe.json").write_text(json.dumps({
        "observation_healthy": True, "observation_error_count": 0,
        "rows": [], "submit_http_count": 0,
    }))
    (owned / "provider-process-cleanup.safe.json").write_text(json.dumps({
        "actual_pid": provider_pid, "process_absent": True,
    }))
    (owned / "application-process-cleanup.safe.json").write_text(json.dumps({
        "cleanup_complete": True,
    }))
    assert runner.archive_runtime_evidence(
        owned, evidence, "sqlite", require_model_usage=False,
    ) is False

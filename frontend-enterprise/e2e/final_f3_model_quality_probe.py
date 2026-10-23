"""
@Time       : 2026/10/17
@Author     : zhanglp8181
@File       : final_f3_model_quality_probe.py
@CallChain  : MQ-I1冻结合同 → 原OpenAI兼容SDK → 有界SDK receipt/usage安全归档
@Description: 只验证真实模型SDK可调用性和观察完整性，不执行应用、业务provider或浏览器旅程。
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_ROOT = ROOT / "docs/StaffDeck能力蒸馏-7adc7c8/evidence/final-f3-model-quality-20261017"
sys.path.insert(0, str(ROOT / "backend"))

from final_f3_model_connection_probe import (  # noqa: E402
    _configuration,
    _execute_probe,
    _read_ledger_rows,
    _receipt_projection,
)
from final_f3_model_quality_contract import (  # noqa: E402
    safe_model_quality_summary,
    validate_model_quality_contract,
)
from final_f3_readiness import snapshot  # noqa: E402
from final_f3_sdk_ledger import SdkLedger  # noqa: E402


def _write_private(path: Path, value: object) -> None:
    """排他写入0600安全JSON，拒绝覆盖旧轮原件。"""
    with path.open("x") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, indent=2))
    path.chmod(0o600)


def _contract(path: Path):
    """读取预登记合同并重新经过严格模型质量schema校验。"""
    value = json.loads(path.read_text())
    return validate_model_quality_contract(value)


def run(*, run_id: str, registration_id: str, output_root: Path) -> int:
    """执行单次有界真实模型探针，明确不把最小生成计为业务质量通过。"""
    output = output_root / run_id
    output.mkdir(mode=0o700)
    registration = output_root / registration_id
    contract = _contract(registration / "model-quality-contract.safe.json")
    before = snapshot(ROOT)
    _write_private(output / "source-start.safe.json", before)
    result: dict[str, object] = {
        "run_id": run_id, "registration_id": registration_id,
        "phase": "MQ-I1_model_sdk_probe", "business_provider_calls": 0,
        "application_started": False, "browser_started": False,
        "real_vendor_qualified": False, "model_quality_passed": False,
        "real_model_quality_attempted": False, "remote_readback_performed": False,
        "remote_cleanup_performed": False,
    }
    private_root = Path(tempfile.mkdtemp(prefix="gongge-xuban-mq-i1-", dir="/dev/shm"))
    private_root.chmod(0o700)
    ledger = SdkLedger(private_root / "sdk.safe.jsonl")
    try:
        configuration = _configuration()
        result["configuration_loaded"] = configuration is not None
        if configuration is None:
            result["missing_condition"] = "F3_MODEL_CONFIGURATION_MISSING_OR_UNSAFE"
        else:
            result["real_model_quality_attempted"] = True
            # 目录/别名诊断和真实最小生成分别记录；即使目录不精确也不伪造质量通过。
            probe = _execute_probe(
                configuration, ledger, probe_generation_after_catalog_mismatch=True,
            )
            result["probe"] = probe
        health = ledger.health()
        rows = _read_ledger_rows(ledger)
        result["sdk_observation"] = {
            key: health[key] for key in (
                "valid", "started_count", "settled_count", "in_flight_count",
                "receipt_count", "file_matches", "write_failed",
            )
        }
        result["sdk_receipts"] = _receipt_projection(rows)
        result["model_transport_observation_passed"] = bool(
            result.get("configuration_loaded") is True
            and health.get("valid") is True
            and health.get("started_count", 0) >= 1
            and health.get("started_count") == health.get("settled_count")
            and health.get("settled_count") == health.get("receipt_count")
            and health.get("in_flight_count") == 0
        )
        if ledger.path.exists():
            archive = output / "sdk-receipts.safe.jsonl"
            archive.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
            archive.chmod(0o600)
            result["sdk_receipt_archive_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
    except Exception as error:
        result.update(probe_execution_error_type=type(error).__name__,
                      model_transport_observation_passed=False)
    finally:
        ledger_present = ledger.path.exists()
        shutil.rmtree(private_root)
        result["private_tmpfs_removed"] = not private_root.exists()
        result["private_ledger_existed"] = ledger_present
    after = snapshot(ROOT)
    _write_private(output / "source-end.safe.json", {"files": after, "unchanged": before == after})
    result["source_unchanged"] = before == after
    result["contract_summary"] = safe_model_quality_summary(contract)
    result["model_quality_passed"] = False
    result["quality_completion_reason"] = "I1 transport probe is not an end-to-end consumer quality case"
    _write_private(output / "probe.safe.json", result)
    return 0 if result.get("source_unchanged") is True and result.get("private_tmpfs_removed") is True else 1


def main() -> int:
    """只接受运行标签和既有预登记目录，不接收或回显凭据。"""
    parser = argparse.ArgumentParser(description="MQ-I1真实模型SDK探针，不执行业务旅程")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--registration-id", required=True)
    parser.add_argument("--output-root", type=Path, default=EVIDENCE_ROOT)
    args = parser.parse_args()
    return run(run_id=args.run_id, registration_id=args.registration_id, output_root=args.output_root)


if __name__ == "__main__":
    raise SystemExit(main())

"""
@Time       : 2026/10/10
@Author     : zhanglp8181
@File       : final_f3_model_connection_probe.py
@CallChain  : 私有现有模型配置 → 原OpenAI兼容SDK只读探测 → 有界安全旁证与临时清理
@Description: 分开读取模型目录、适用账户状态和最小文本生成，不执行业务服务或能力快照写入。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_ROOT = ROOT / "docs/StaffDeck能力蒸馏-7adc7c8/evidence/final-f3-20261010"
sys.path.insert(0, str(ROOT / "backend"))

from final_f3_readiness import snapshot  # noqa: E402
from final_f3_sdk_ledger import SdkLedger  # noqa: E402


def _configuration() -> dict[str, str] | None:
    """合并原配置优先级而不输出或复制模型端点、模型名与API Key。"""
    from dotenv import dotenv_values

    values = dotenv_values(ROOT / "backend/.env")
    merged = {**values, **os.environ}
    names = ("DEMO_MODEL_BASE_URL", "DEMO_MODEL_NAME", "DEMO_MODEL_API_KEY")
    result = {name: str(merged.get(name) or "").strip() for name in names}
    if not all(result.values()):
        return None
    endpoint = urlsplit(result["DEMO_MODEL_BASE_URL"])
    if (endpoint.scheme not in {"http", "https"} or not endpoint.hostname
            or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment
            or endpoint.hostname in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}):
        return None
    return result


def _receipt_projection(rows: list[dict]) -> list[dict]:
    """从严格账本只复制有限阶段、状态、耗时、错误类别和token计数。"""
    fields = (
        "request_class", "status", "exception_type", "provider_status_code",
        "provider_error_category", "provider_transport_cause", "provider_os_errno",
        "duration_ms", "catalog_count", "usage",
    )
    return [{key: row[key] for key in fields if key in row} for row in rows]


def _account_status(base_url: str, api_key: str) -> dict[str, object]:
    """复用项目DeepSeek余额检查边界，仅记录HTTP状态与可用布尔。"""
    endpoint = urlsplit(base_url)
    if endpoint.hostname != "api.deepseek.com":
        return {"status": "not_applicable", "http_status": None, "available": None}
    safe_endpoint = f"{endpoint.scheme}://{endpoint.netloc}{endpoint.path}".rstrip("/")
    try:
        import httpx

        response = httpx.get(
            f"{safe_endpoint}/user/balance",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15,
        )
        body = response.json() if response.content else {}
        available = body.get("is_available") if isinstance(body, dict) else None
        if response.status_code == 200 and type(available) is bool:
            return {"status": "available" if available else "unavailable",
                    "http_status": 200, "available": available}
        return {"status": "skipped", "http_status": response.status_code, "available": None}
    except Exception:
        return {"status": "skipped", "http_status": None, "available": None}


def _execute_probe(
    configuration: dict[str, str], ledger: SdkLedger, *,
    probe_generation_after_catalog_mismatch: bool = False,
) -> dict[str, object]:
    """顺序观测模型目录和账户状态，可选地单独验证目录不匹配后的文本能力。"""
    from openai import OpenAI

    endpoint = configuration["DEMO_MODEL_BASE_URL"]
    model = configuration["DEMO_MODEL_NAME"]
    client = ledger.observe(OpenAI(
        api_key=configuration["DEMO_MODEL_API_KEY"], base_url=endpoint,
        timeout=180, max_retries=0,
    ))
    result: dict[str, object] = {
        "catalog_status": "not_run", "configured_model_matches_catalog": None,
        "account_status": {"status": "not_run", "http_status": None, "available": None},
        "minimum_generation_status": "not_run", "minimum_generation_nonempty": None,
        "generation_probe_bypassed_catalog_mismatch": False,
    }
    may_generate = False
    try:
        page = client.models.list()
    except Exception:
        row = ledger.health()
        del row
        receipts = _read_ledger_rows(ledger)
        failure = next((row for row in receipts if row.get("request_class") == "model_catalog"), {})
        status = failure.get("provider_status_code")
        result.update(catalog_status="unsupported" if status in {404, 405} else "failed",
                      catalog_http_status=status,
                      catalog_error_category=failure.get("provider_error_category"),
                      catalog_transport_cause=failure.get("provider_transport_cause"),
                      catalog_transport_errno=failure.get("provider_os_errno"))
        may_generate = status in {404, 405}
    else:
        model_ids = [str(getattr(item, "id", "")).strip() for item in page.data]
        model_ids = [item for item in model_ids if item]
        matches = model in model_ids if model_ids else None
        result.update(catalog_status="completed", catalog_http_status=200,
                      catalog_count=len(model_ids), configured_model_matches_catalog=matches)
        may_generate = matches is not False or probe_generation_after_catalog_mismatch
        result["generation_probe_bypassed_catalog_mismatch"] = bool(
            matches is False and probe_generation_after_catalog_mismatch
        )
    if not may_generate:
        client.close()
        return result

    result["account_status"] = _account_status(endpoint, configuration["DEMO_MODEL_API_KEY"])
    account = result["account_status"]
    if isinstance(account, dict) and account.get("available") is False:
        result.update(minimum_generation_status="not_attempted_account_unavailable",
                      minimum_generation_nonempty=None)
        client.close()
        return result
    try:
        completion = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "你是连接测试助手。"},
                {"role": "user", "content": "只回复：连接成功"},
            ],
            temperature=0,
            max_tokens=256,
            extra_body={"thinking": {"type": "disabled"}},
        )
    except Exception:
        receipts = _read_ledger_rows(ledger)
        failure = next((row for row in reversed(receipts)
                        if row.get("request_class") == "chat_completion"), {})
        result.update(minimum_generation_status="failed",
                      generation_http_status=failure.get("provider_status_code"),
                      generation_error_category=failure.get("provider_error_category"),
                      generation_transport_cause=failure.get("provider_transport_cause"),
                      generation_transport_errno=failure.get("provider_os_errno"),
                      minimum_generation_nonempty=False)
    else:
        choices = getattr(completion, "choices", []) or []
        content = getattr(getattr(choices[0], "message", None), "content", None) if choices else None
        nonempty = isinstance(content, str) and bool(content.strip())
        result.update(minimum_generation_status="completed" if nonempty else "empty_response",
                      generation_http_status=200, minimum_generation_nonempty=nonempty)
    client.close()
    return result


def _read_ledger_rows(ledger: SdkLedger) -> list[dict]:
    """只在进程内解析自有账本，尚未通过私有schema的内容不会进入归档。"""
    if not ledger.path.exists():
        return []
    return [json.loads(line) for line in ledger.path.read_text().splitlines()]


def run(
    *, run_id: str, output_root: Path, probe_generation_after_catalog_mismatch: bool = False,
) -> int:
    """事前冻结源码后单次探测；安全归档实际SDK回执并清理私有tmpfs。"""
    if not re.fullmatch(r"[a-z0-9-]{3,60}", run_id):
        raise RuntimeError("F3_MODEL_PROBE_RUN_ID_INVALID")
    output = output_root / f"i3-model-connection-probe-{run_id}"
    if output_root.resolve() != EVIDENCE_ROOT.resolve() or output.exists():
        raise RuntimeError("F3_MODEL_PROBE_NEW_EVIDENCE_DIRECTORY_REQUIRED")
    configuration = _configuration()
    output.mkdir(mode=0o700)
    start = snapshot(ROOT)
    (output / "source-start.safe.json").write_text(json.dumps(start, ensure_ascii=False, indent=2))
    for path in output.iterdir():
        path.chmod(0o600)
    result: dict[str, object] = {
        "run_id": run_id, "diagnostic_only": True, "business_calls": 0,
        "application_connection_route_exercised": False,
        "model_endpoint_called": False, "real_vendor_qualified": False,
        "JRN08_passed": False, "source_unchanged": False,
    }
    private_root = Path(tempfile.mkdtemp(prefix="gongge-xuban-f3-model-probe-", dir="/dev/shm"))
    private_root.chmod(0o700)
    ledger = SdkLedger(private_root / "sdk.safe.jsonl")
    try:
        result["configuration_loaded"] = configuration is not None
        if configuration is not None:
            result["model_endpoint_called"] = True
            result.update(_execute_probe(
                configuration, ledger,
                probe_generation_after_catalog_mismatch=probe_generation_after_catalog_mismatch,
            ))
        health = ledger.health()
        rows = _read_ledger_rows(ledger)
        result["sdk_observation"] = {
            key: health[key] for key in (
                "valid", "started_count", "settled_count", "in_flight_count",
                "receipt_count", "file_matches", "write_failed",
            )
        }
        result["diagnostic_observation_complete"] = bool(
            configuration is not None and health["valid"] and health["started_count"] > 0
        )
        result["sdk_receipts"] = _receipt_projection(rows)
        if not health["valid"] and configuration is not None:
            result["observation_valid"] = False
        if ledger.path.exists():
            archived = output / "sdk-receipts.safe.jsonl"
            archived.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
            archived.chmod(0o600)
            result["sdk_receipt_archive_sha256"] = hashlib.sha256(archived.read_bytes()).hexdigest()
    except Exception as error:
        result.update(probe_execution_error_type=type(error).__name__, observation_valid=False)
    finally:
        try:
            ledger_path_present = ledger.path.exists()
            shutil.rmtree(private_root)
            result["private_tmpfs_removed"] = not private_root.exists()
            result["private_ledger_existed"] = ledger_path_present
        except OSError as error:
            result.update(private_tmpfs_removed=False, cleanup_error_type=type(error).__name__)
    end = snapshot(ROOT)
    (output / "source-end.safe.json").write_text(json.dumps(end, ensure_ascii=False, indent=2))
    result["source_unchanged"] = start == end
    (output / "probe-result.safe.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    for path in output.iterdir():
        path.chmod(0o600)
    return 0 if result.get("source_unchanged") is True and result.get("private_tmpfs_removed") is True else 1


def main() -> int:
    """仅接受run标签与固定证据根，不在参数或stdout中接收、回显凭据。"""
    parser = argparse.ArgumentParser(description="只读连接诊断，不运行业务或能力预检")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-root", type=Path, default=EVIDENCE_ROOT)
    parser.add_argument("--probe-generation-after-catalog-mismatch", action="store_true")
    args = parser.parse_args()
    return run(run_id=args.run_id, output_root=args.output_root,
               probe_generation_after_catalog_mismatch=args.probe_generation_after_catalog_mismatch)


if __name__ == "__main__":
    raise SystemExit(main())

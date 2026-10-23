"""
@Time       : 2026/10/17
@Author     : zhanglp8181
@File       : final_f3_model_quality_registration.py
@CallChain  : MQ-I1预登记 → 当前模型配置安全摘要 → 质量合同/源码冻结
@Description: 在任何模型外呼前冻结模型质量合同；不启动业务、provider或浏览器旅程。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_ROOT = ROOT / "docs/StaffDeck能力蒸馏-7adc7c8/evidence/final-f3-model-quality-20261017"
sys.path.insert(0, str(ROOT / "backend"))

from final_f3_model_quality_contract import (  # noqa: E402
    FrozenModelQualityContract,
    safe_model_quality_summary,
    validate_model_quality_contract,
)
from final_f3_readiness import snapshot  # noqa: E402


def _hash(value: str) -> str:
    """仅返回配置、标签和规则的SHA-256摘要，不输出原文。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _configuration() -> dict[str, str] | None:
    """按项目配置优先级读取模型配置，缺失或危险URL直接视为未准备。"""
    from dotenv import dotenv_values

    values = dotenv_values(ROOT / "backend/.env")
    merged = {**values, **os.environ}
    names = ("DEMO_MODEL_BASE_URL", "DEMO_MODEL_NAME", "DEMO_MODEL_API_KEY")
    configuration = {name: str(merged.get(name) or "").strip() for name in names}
    if not all(configuration.values()):
        return None
    endpoint = urlsplit(configuration["DEMO_MODEL_BASE_URL"])
    if (endpoint.scheme not in {"http", "https"} or not endpoint.hostname
            or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment
            or endpoint.hostname in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}):
        return None
    return configuration


def _contract(configuration: dict[str, str]) -> FrozenModelQualityContract:
    """根据当前配置和冻结人工标签生成模型质量合同，不创建业务对象或远端资格。"""
    endpoint = urlsplit(configuration["DEMO_MODEL_BASE_URL"])
    scenarios = (
        "normal", "knowledge_old", "knowledge_new", "knowledge_unknown",
        "memory_freeze", "memory_correction", "memory_clear", "feedback_improvement",
    )
    cases = []
    for branch in ("sop", "dynamic_task"):
        for scenario in scenarios:
            label = f"mq-v1|{branch}|{scenario}|human-review-required"
            cases.append({
                "case_id": f"{branch}-{scenario}", "branch": branch, "scenario": scenario,
                "human_label_sha256": _hash(label),
                "expected_outcome": "no_answer" if scenario == "knowledge_unknown" else "answer",
                "source_expectation_sha256": _hash(f"source-rule-v1|{branch}|{scenario}"),
                "version_expectation_sha256": _hash(f"version-rule-v1|{branch}|{scenario}"),
            })
    rules = {
        "request": "sha256(canonical-model-request-without-secret)",
        "response": "sha256(canonical-model-response-without-secret)",
        "sdk_identity": "sha256(provider-model-sdk-identity-summary)",
    }
    payload = {
        "schema_version": "final-f3-model-quality-v1",
        "acceptance_track": "model_quality",
        "provider": {
            "provider_kind": "model_api", "sdk_kind": "openai_compatible",
            "model_name_sha256": _hash(configuration["DEMO_MODEL_NAME"]),
            "base_url_host_sha256": _hash(endpoint.hostname or ""),
            "config_fingerprint_sha256": _hash(json.dumps({
                "base_url": configuration["DEMO_MODEL_BASE_URL"],
                "model": configuration["DEMO_MODEL_NAME"],
                "api_key_sha256": _hash(configuration["DEMO_MODEL_API_KEY"]),
            }, sort_keys=True, separators=(",", ":"))),
            "real_model_required": True, "sdk_observation_required": True,
            "business_effect_required": False, "remote_readback_required": False,
            "remote_cleanup_required": False, "vendor_sandbox_required": False,
            "real_vendor_qualified": False,
        },
        "cases": cases,
        "evidence": {
            "request_digest_rule_sha256": _hash(rules["request"]),
            "response_digest_rule_sha256": _hash(rules["response"]),
            "sdk_identity_rule_sha256": _hash(rules["sdk_identity"]),
            "usage_required": True, "source_binding_required": True,
            "version_binding_required": True, "citation_check_required": True,
            "human_label_review_required": True, "model_self_attestation_allowed": False,
            "business_effect_claim_allowed": False, "remote_readback_required": False,
            "cleanup_required": False,
        },
    }
    return validate_model_quality_contract(payload)


def _write_private(path: Path, value: object) -> None:
    """排他写入0600安全JSON，拒绝覆盖旧轮证据。"""
    with path.open("x") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, indent=2))
    path.chmod(0o600)


def prepare(*, run_id: str, output_root: Path) -> int:
    """执行无网络MQ-I1预登记并冻结合同/源码；不把预登记标为质量通过。"""
    if not re.fullmatch(r"[a-z0-9-]{3,80}", run_id):
        raise ValueError("F3_MODEL_QUALITY_RUN_ID_INVALID")
    output_root.mkdir(parents=True, exist_ok=True)
    output = output_root / run_id
    output.mkdir(mode=0o700)
    configuration = _configuration()
    before = snapshot(ROOT)
    _write_private(output / "source-start.safe.json", before)
    result: dict[str, object] = {
        "run_id": run_id, "phase": "MQ-I1_preparation", "network_calls": 0,
        "business_provider_calls": 0, "browser_started": False,
        "real_model_quality_attempted": False, "real_vendor_qualified": False,
        "model_quality_passed": False, "configuration_loaded": configuration is not None,
        "vendor_sandbox_required": False, "remote_readback_required": False,
        "remote_cleanup_required": False,
    }
    if configuration is not None:
        frozen = _contract(configuration)
        _write_private(output / "model-quality-contract.safe.json", frozen.private_data())
        result["contract"] = safe_model_quality_summary(frozen)
        result["configuration_fingerprint_sha256"] = frozen.private_data()["provider"][
            "config_fingerprint_sha256"
        ]
    else:
        result["missing_condition"] = "F3_MODEL_CONFIGURATION_MISSING_OR_UNSAFE"
    after = snapshot(ROOT)
    _write_private(output / "source-end.safe.json", {"files": after, "unchanged": before == after})
    result["source_unchanged"] = before == after
    result["preparation_passed"] = bool(configuration is not None and before == after)
    _write_private(output / "preparation.safe.json", result)
    return 0 if result["preparation_passed"] else 2


def main() -> int:
    """解析安全运行标签和证据目录，不接收或回显凭据。"""
    parser = argparse.ArgumentParser(description="MQ-I1模型质量合同预登记；不执行模型或业务旅程")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-root", type=Path, default=EVIDENCE_ROOT)
    args = parser.parse_args()
    return prepare(run_id=args.run_id, output_root=args.output_root)


if __name__ == "__main__":
    raise SystemExit(main())

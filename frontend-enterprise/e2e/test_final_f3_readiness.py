"""
@Time       : 2026/10/04
@Author     : zhanglp8181
@File       : test_final_f3_readiness.py
@CallChain  : pytest → 独立F3准备命令 → 私有合同/源码冻结 → 安全待验记录
@Description: 保证缺sandbox不产生副作用或绿结论、错误不回显私密、原档不会被后轮覆盖。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from final_f3_readiness import prepare
from test_final_f3_preparation import declared_profile


def test_missing_provider_preserves_pending_and_freezes_inputs(tmp_path: Path) -> None:
    """缺真实供应商只生成待验及源码摘要，不能启动服务、外呼或把两个方言标通过。"""
    root = tmp_path / "repository"
    (root / "backend/app").mkdir(parents=True)
    source = root / "backend/app/source.py"
    source.write_text("original_source = True\n")
    output = tmp_path / "evidence"
    code = prepare(root=root, profile_path=tmp_path / "missing.private.json", output=output)
    assert code == 2
    result = json.loads((output / "readiness.safe.json").read_text())
    assert result["missing_condition"] == "F3_VENDOR_PROFILE_MISSING"
    assert result["I1_passed"] is False and result["JRN08_passed"] is False
    assert result["remote_calls"] == result["services_started"] == 0
    assert result["I2"] == result["I3"] == result["I4"] == "not_run"
    sources = json.loads((output / "source-start.safe.json").read_text())
    assert "backend/app/source.py" in sources
    assert source.read_text() == "original_source = True\n"


def test_structural_profile_never_qualifies_provider(tmp_path: Path) -> None:
    """合法结构只生成适配摘要；真实sandbox身份、提交/读回和两分支仍待实测。"""
    profile = tmp_path / "sandbox.private.json"
    profile.write_text(json.dumps(declared_profile()))
    profile.chmod(0o600)
    output = tmp_path / "evidence"
    assert prepare(root=tmp_path, profile_path=profile, output=output) == 2
    result = json.loads((output / "readiness.safe.json").read_text())
    assert result["profile"]["structural_compatibility_passed"] is True
    assert result["profile"]["real_provider_qualified"] is False
    assert result["missing_condition"] == "F3_REAL_VENDOR_QUALIFICATION_PENDING"
    assert result["I1_passed"] is False
    assert "sandbox.example.org" not in json.dumps(result)


def test_invalid_private_config_is_not_echoed(tmp_path: Path) -> None:
    """无法解析的私有合同不能泄漏原文，归档保持稳定错误代码。"""
    profile = tmp_path / "sandbox.private.json"
    profile.write_text("PRIVATE_PROVIDER_CANARY\n")
    profile.chmod(0o600)
    output = tmp_path / "evidence"
    assert prepare(root=tmp_path, profile_path=profile, output=output) == 2
    text = (output / "readiness.safe.json").read_text()
    assert "F3_PRIVATE_PROFILE_INVALID_JSON" in text
    assert "PRIVATE_PROVIDER_CANARY" not in text


def test_prepare_does_not_overwrite_original_failed_round(tmp_path: Path) -> None:
    """每轮要求新目录，原失败字节保留而不能被后续待验或成功覆写。"""
    output = tmp_path / "evidence"
    output.mkdir()
    original = output / "readiness.safe.json"
    original.write_text("ORIGINAL_FAILURE\n")
    with pytest.raises(FileExistsError):
        prepare(root=tmp_path, profile_path=tmp_path / "missing.json", output=output)
    assert original.read_text() == "ORIGINAL_FAILURE\n"

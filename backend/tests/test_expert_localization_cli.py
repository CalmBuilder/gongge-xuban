from pathlib import Path

import pytest

from app.experts import localization_cli
from app.experts.localization_schema import LocalizationApplyResult, LocalizationRollbackResult


def test_prepare_requires_tenant_admin_input_output_and_model() -> None:
    with pytest.raises(SystemExit) as caught:
        localization_cli.main(["prepare"])
    assert caught.value.code == 2


def test_prepare_accepts_repeatable_only_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured = {}

    def fake_prepare(args):  # noqa: ANN001, ANN202
        captured["paths"] = set(args.only_path)
        return 0

    monkeypatch.setattr(localization_cli, "_prepare", fake_prepare)
    result = localization_cli.main(
        [
            "prepare",
            "--tenant-id",
            "tenant_demo",
            "--admin-username",
            "admin",
            "--input",
            str(tmp_path / "input"),
            "--output",
            str(tmp_path / "output"),
            "--model-config-id",
            "model_deepseek",
            "--only-path",
            "engineering/a.md",
            "--only-path",
            "specialized/b.md",
        ]
    )
    assert result == 0
    assert captured["paths"] == {"engineering/a.md", "specialized/b.md"}


def test_apply_delegates_to_localization_service(monkeypatch, tmp_path: Path) -> None:
    result_path = tmp_path / "result.json"
    fake = LocalizationApplyResult(
        tenant_id="tenant_demo",
        source_batch_id="batch",
        translation_manifest_sha256="a" * 64,
        started_at="2026-07-18T00:00:00",
        finished_at="2026-07-18T00:00:01",
        result_path=result_path,
        items=[],
    )
    monkeypatch.setattr(localization_cli, "apply_localization_package", lambda *args: fake)
    assert localization_cli.main(
        [
            "apply",
            "--tenant-id",
            "tenant_demo",
            "--admin-username",
            "admin",
            "--input",
            str(tmp_path),
        ]
    ) == 0


def test_rollback_delegates_to_localization_service(monkeypatch, tmp_path: Path) -> None:
    fake = LocalizationRollbackResult(
        tenant_id="tenant_demo",
        source_batch_id="batch",
        started_at="2026-07-18T00:00:00",
        finished_at="2026-07-18T00:00:01",
        result_path=tmp_path / "rollback.json",
        items=[],
    )
    monkeypatch.setattr(localization_cli, "rollback_localization_result", lambda *args: fake)
    assert localization_cli.main(
        [
            "rollback",
            "--tenant-id",
            "tenant_demo",
            "--admin-username",
            "admin",
            "--result",
            str(tmp_path / "apply.json"),
        ]
    ) == 0

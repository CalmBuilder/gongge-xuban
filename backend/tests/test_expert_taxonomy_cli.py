from __future__ import annotations

from pathlib import Path

import pytest

from app.experts import taxonomy_cli
from app.experts.taxonomy_schema import TaxonomyResult


@pytest.mark.parametrize("command", ["check", "apply"])
def test_cli_delegates_and_prints_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    result = TaxonomyResult(
        operation=command,
        tenant_id="tenant_demo",
        taxonomy_version=1,
        source_commit="459dce837db3bdfdc4763d3fefd1fd854e73c8f1",
        started_at="2026-07-18T00:00:00+00:00",
        finished_at="2026-07-18T00:00:01+00:00",
        result_path=tmp_path / "result.json",
        items=[],
    )
    called = {}

    def fake(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        called["args"] = args
        return result

    monkeypatch.setattr(taxonomy_cli, f"{command}_taxonomy", fake)

    code = taxonomy_cli.main(
        [
            command,
            "--tenant-id",
            "tenant_demo",
            "--admin-username",
            "admin",
            "--taxonomy",
            str(tmp_path / "taxonomy.json"),
        ]
    )

    assert code == 0
    assert called
    assert str(result.result_path) in capsys.readouterr().out


def test_cli_returns_two_for_validation_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise taxonomy_cli.ExpertTaxonomyApplyError("administrator required")

    monkeypatch.setattr(taxonomy_cli, "check_taxonomy", fail)

    code = taxonomy_cli.main(
        ["check", "--tenant-id", "tenant_demo", "--admin-username", "missing"]
    )

    assert code == 2
    assert "administrator required" in capsys.readouterr().out

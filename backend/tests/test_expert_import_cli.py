from __future__ import annotations

import hashlib
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.experts import import_cli
from app.experts.local_source import LocalSource, SourceFile
from app.experts.schema import ApplyResult


def test_prepare_requires_tenant_admin_source_and_output() -> None:
    with pytest.raises(SystemExit) as caught:
        import_cli.main(["prepare"])
    assert caught.value.code == 2


def test_apply_never_constructs_source_or_llm(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("apply must not construct source or LLM")

    result_path = tmp_path / "apply-result.json"
    fake_result = ApplyResult(
        batch_id="batch",
        tenant_id="tenant_demo",
        started_at="2026-07-18T00:00:00",
        finished_at="2026-07-18T00:00:01",
        result_path=result_path,
        items=[],
    )
    monkeypatch.setattr(import_cli, "inspect_local_source", forbidden)
    monkeypatch.setattr(import_cli, "ExpertTranslator", forbidden)
    monkeypatch.setattr(import_cli, "apply_package", lambda *args, **kwargs: fake_result)
    assert import_cli.main(
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


def test_preserve_original_prepare_never_loads_default_model(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    content = "---\nname: Offline Expert\ndescription: Works offline.\n---\n# Offline\nReason carefully.\n"
    source_file = tmp_path / "offline.md"
    source_file.write_text(content, encoding="utf-8")
    discovered = SourceFile(
        path="engineering/offline.md",
        absolute_path=source_file,
        sha256=hashlib.sha256(content.encode()).hexdigest(),
    )
    source = LocalSource(
        root=tmp_path,
        commit_sha="a" * 40,
        remote_url="https://github.com/msitarzewski/agency-agents.git",
        verified=True,
    )

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("offline prepare must not load a model")

    captured: list[object] = []
    monkeypatch.setattr(import_cli, "default_session_factory", lambda: nullcontext(object()))
    monkeypatch.setattr(import_cli, "validate_admin", lambda *args: object())
    monkeypatch.setattr(import_cli, "inspect_local_source", lambda *args, **kwargs: source)
    monkeypatch.setattr(import_cli, "discover_source_files", lambda *args: [discovered])
    monkeypatch.setattr(import_cli, "_default_model", forbidden)
    monkeypatch.setattr(
        import_cli,
        "write_preview_package",
        lambda output, local_source, tenant_id, experts, errors: (
            captured.extend(experts)
            or SimpleNamespace(success_count=len(experts), failed_count=len(errors))
        ),
    )
    assert import_cli.main(
        [
            "prepare",
            "--tenant-id",
            "tenant_demo",
            "--admin-username",
            "admin",
            "--source",
            str(tmp_path),
            "--output",
            str(tmp_path / "preview"),
            "--preserve-original",
        ]
    ) == 0
    assert len(captured) == 1

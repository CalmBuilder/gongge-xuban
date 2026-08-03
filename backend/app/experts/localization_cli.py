"""Agency Agents 专家中文化的 prepare/apply/rollback 命令入口。"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from sqlmodel import Session, select

from app.db.database import engine
from app.db.models import AgentProfile, ModelConfig
from app.experts.import_service import validate_admin
from app.experts.localization_package import prepare_localization_package
from app.experts.localization_service import (
    apply_localization_package,
    rollback_localization_result,
)
from app.experts.localization_translation import (
    LocalizationTranslator,
    load_upstream_name_map,
)
from app.experts.package import load_and_verify_package
from app.llm.client import LLMClient


def default_session_factory() -> Session:
    return Session(engine)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agency-agents-localization")
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--tenant-id", required=True)
    prepare.add_argument("--admin-username", required=True)
    prepare.add_argument("--input", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--model-config-id", required=True)
    prepare.add_argument("--only-path", action="append", default=[])
    apply = subparsers.add_parser("apply")
    apply.add_argument("--tenant-id", required=True)
    apply.add_argument("--admin-username", required=True)
    apply.add_argument("--input", type=Path, required=True)
    rollback = subparsers.add_parser("rollback")
    rollback.add_argument("--tenant-id", required=True)
    rollback.add_argument("--admin-username", required=True)
    rollback.add_argument("--result", type=Path, required=True)
    return parser


def _prepare(args: argparse.Namespace) -> int:
    source_manifest, _experts = load_and_verify_package(args.input, args.tenant_id)
    with default_session_factory() as db:
        validate_admin(db, args.tenant_id, args.admin_username)
        model = db.get(ModelConfig, args.model_config_id)
        if model is None or model.tenant_id != args.tenant_id or not model.enabled:
            raise ValueError("Enabled tenant DeepSeek model config is required")
        rows = db.exec(select(AgentProfile).where(AgentProfile.tenant_id == args.tenant_id)).all()
        occupied_names = {
            row.name
            for row in rows
            if not (
                isinstance(row.metadata_json, dict)
                and row.metadata_json.get("expert_source_code") == "agency-agents"
                and row.metadata_json.get("import_batch_id") == source_manifest.batch_id
            )
        }
    mapping_path = (
        Path(source_manifest.source_absolute_path)
        / "scripts"
        / "i18n"
        / "agent-names-zh.json"
    )
    mapping = load_upstream_name_map(mapping_path) if mapping_path.is_file() else {}
    translator = LocalizationTranslator(
        LLMClient(model, timeout_seconds=120),
        max_attempts=3,
        line_fallback_attempts=1,
        single_line_fallback_attempts=3,
        raw_line_fallback_attempts=3,
    )
    manifest = prepare_localization_package(
        args.input,
        args.output,
        args.tenant_id,
        model.id,
        model.model,
        translator,
        mapping,
        only_paths=set(args.only_path) or None,
        occupied_names=occupied_names,
        max_workers=8,
    )
    print(args.output.resolve() / "TRANSLATION_REPORT.md")
    print(
        f"selected={manifest.selected_count} verified={manifest.verified_count} "
        f"failed={manifest.failed_count}"
    )
    return 0 if manifest.failed_count == 0 else 2


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "prepare":
            return _prepare(args)
        if args.command == "apply":
            result = apply_localization_package(
                default_session_factory,
                args.input,
                args.tenant_id,
                args.admin_username,
            )
            print(result.result_path)
            return 0
        result = rollback_localization_result(
            default_session_factory,
            args.result,
            args.tenant_id,
            args.admin_username,
        )
        print(result.result_path)
        return 0
    except (OSError, ValueError) as exc:
        print(f"error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

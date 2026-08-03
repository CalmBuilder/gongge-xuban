"""Agency Agents 一次性专家导入命令行入口。"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from sqlmodel import Session, select

from app.db.database import engine
from app.db.models import ModelConfig
from app.experts.capability import build_capability_manifest, estimate_input_tokens
from app.experts.import_service import (
    ExpertImportError,
    apply_package,
    rollback_apply_result,
    validate_admin,
)
from app.experts.local_source import discover_source_files, inspect_local_source
from app.experts.package import prepare_expert, write_preview_package
from app.experts.parser import ExpertParseError, parse_expert_markdown
from app.experts.schema import PrepareError
from app.experts.translation import (
    ExpertTranslationError,
    ExpertTranslator,
    preserve_original_translation,
)
from app.llm.client import LLMClient


def default_session_factory() -> Session:
    return Session(engine)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agency-agents-import")
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--tenant-id", required=True)
    prepare.add_argument("--admin-username", required=True)
    prepare.add_argument("--source", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--allow-unverified-local-source", action="store_true")
    prepare.add_argument(
        "--preserve-original",
        action="store_true",
        help="prepare offline without a model, preserving upstream prompt text",
    )
    apply = subparsers.add_parser("apply")
    apply.add_argument("--tenant-id", required=True)
    apply.add_argument("--admin-username", required=True)
    apply.add_argument("--input", type=Path, required=True)
    rollback = subparsers.add_parser("rollback")
    rollback.add_argument("--tenant-id", required=True)
    rollback.add_argument("--admin-username", required=True)
    rollback.add_argument("--result", type=Path, required=True)
    return parser


def _default_model(tenant_id: str) -> ModelConfig:
    with default_session_factory() as db:
        model = db.exec(
            select(ModelConfig).where(
                ModelConfig.tenant_id == tenant_id,
                ModelConfig.enabled.is_(True),
                ModelConfig.is_default.is_(True),
            )
        ).first()
    if model is None:
        raise ExpertImportError("No enabled default model is configured for tenant")
    return model


def _prepare(args: argparse.Namespace) -> int:
    with default_session_factory() as db:
        validate_admin(db, args.tenant_id, args.admin_username)
    source = inspect_local_source(
        args.source,
        allow_unverified=args.allow_unverified_local_source,
    )
    files = discover_source_files(source)
    translator = (
        None
        if args.preserve_original
        else ExpertTranslator(LLMClient(_default_model(args.tenant_id)))
    )
    experts = []
    errors: list[PrepareError] = []
    for source_file in files:
        try:
            parsed = parse_expert_markdown(source_file)
        except ExpertParseError as exc:
            errors.append(
                PrepareError(upstream_path=source_file.path, stage="parse", message=str(exc))
            )
            continue
        try:
            translation = (
                preserve_original_translation(parsed)
                if translator is None
                else translator.translate(parsed)
            )
            capability = build_capability_manifest(parsed, translation.capability_analysis)
            experts.append(
                prepare_expert(
                    parsed,
                    translation,
                    capability,
                    estimate_input_tokens(translation.markdown_zh),
                    source_commit=source.commit_sha,
                )
            )
        except (ExpertTranslationError, ValueError) as exc:
            errors.append(
                PrepareError(
                    upstream_path=source_file.path,
                    stage="translation",
                    message=str(exc),
                )
            )
    if not experts:
        raise ExpertImportError("No experts were prepared successfully")
    manifest = write_preview_package(args.output, source, args.tenant_id, experts, errors)
    print(args.output.resolve() / "IMPORT_REPORT.md")
    print(f"prepared={manifest.success_count} failed={manifest.failed_count}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "prepare":
            return _prepare(args)
        if args.command == "apply":
            result = apply_package(
                default_session_factory,
                args.input,
                args.tenant_id,
                args.admin_username,
            )
        else:
            result = rollback_apply_result(
                default_session_factory,
                args.result,
                args.tenant_id,
                args.admin_username,
            )
        print(result.result_path)
        return 0
    except (ExpertImportError, OSError, ValueError) as exc:
        print(f"error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Agency Agents 专家二级分类检查与写入命令。"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from sqlmodel import Session

from app.db.database import engine
from app.experts.taxonomy_service import (
    ExpertTaxonomyApplyError,
    apply_taxonomy,
    check_taxonomy,
)


def default_session_factory() -> Session:
    return Session(engine)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agency-agents-taxonomy")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "apply"):
        command = subparsers.add_parser(name)
        command.add_argument("--tenant-id", required=True)
        command.add_argument("--admin-username", required=True)
        command.add_argument("--taxonomy", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    operation = check_taxonomy if args.command == "check" else apply_taxonomy
    try:
        result = operation(
            default_session_factory,
            args.taxonomy,
            args.tenant_id,
            args.admin_username,
        )
        print(result.result_path)
        print(" ".join(f"{key}={value}" for key, value in sorted(result.counts.items())))
        return 0
    except (ExpertTaxonomyApplyError, OSError, ValueError) as exc:
        print(f"error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

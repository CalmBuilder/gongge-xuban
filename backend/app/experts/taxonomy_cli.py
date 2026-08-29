"""
@Time       : 2026/08/29 12:00
@Author     : zhanglp8181
@File       : taxonomy_cli.py
@CallChain  : 分类 CLI → 版本化 taxonomy JSON → SQLite/MySQL 专家元数据检查/写入
@Description: 提供版本化专家二级分类的检查与显式写入命令。
"""

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
    """返回绑定当前 SQLite 或 MySQL 运行时的分类会话工厂。"""

    return Session(engine)


def _parser() -> argparse.ArgumentParser:
    """构造分类检查与显式写入命令的参数解析器。"""

    parser = argparse.ArgumentParser(prog="agency-agents-taxonomy")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "apply"):
        command = subparsers.add_parser(name)
        command.add_argument("--tenant-id", required=True)
        command.add_argument("--admin-username", required=True)
        command.add_argument("--taxonomy", type=Path)
        command.add_argument("--expected-count", type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """执行分类检查或写入，并打印可审计结果路径与状态统计。"""

    args = _parser().parse_args(argv)
    operation = check_taxonomy if args.command == "check" else apply_taxonomy
    try:
        result = operation(
            default_session_factory,
            args.taxonomy,
            args.tenant_id,
            args.admin_username,
            expected_count=args.expected_count if args.expected_count is not None else 263,
        )
        print(result.result_path)
        print(" ".join(f"{key}={value}" for key, value in sorted(result.counts.items())))
        return 0
    except (ExpertTaxonomyApplyError, OSError, ValueError) as exc:
        print(f"error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

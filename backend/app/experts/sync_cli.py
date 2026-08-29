"""
@Time       : 2026/08/29 12:00
@Author     : zhanglp8181
@File       : sync_cli.py
@CallChain  : 命令行参数 → 当前/历史专家包校验 → sync_plan 计划 → sync_apply 受控写入 → 结果报告
@Description: 提供 Agency Agents 专家同步计划与显式批准的受控 apply 命令。
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from sqlmodel import Session

from app.db.database import engine
from app.experts.sync_apply import ExpertSyncApplyError, apply_sync_plan
from app.experts.sync_rollback import ExpertSyncRollbackError, rollback_sync_result
from app.experts.sync_plan import ExpertSyncPlanError, build_sync_plan


def default_session_factory() -> Session:
    """返回绑定当前 SQLite 或 MySQL 运行时的只读计划会话工厂。"""

    return Session(engine)


def _parser() -> argparse.ArgumentParser:
    """构造专家同步计划命令行参数解析器。"""

    parser = argparse.ArgumentParser(prog="agency-agents-sync")
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan = subparsers.add_parser("plan")
    plan.add_argument("--tenant-id", required=True)
    plan.add_argument("--admin-username", required=True)
    plan.add_argument("--input", type=Path, required=True)
    plan.add_argument("--baseline-input", type=Path)
    plan.add_argument("--baseline-localization", type=Path)
    plan.add_argument("--output", type=Path)
    apply = subparsers.add_parser("apply")
    apply.add_argument("--tenant-id", required=True)
    apply.add_argument("--admin-username", required=True)
    apply.add_argument("--input", type=Path, required=True)
    apply.add_argument("--plan", type=Path, required=True)
    apply.add_argument("--localization", type=Path)
    apply.add_argument("--approve-path", action="append", default=[])
    apply.add_argument("--acknowledge-review", action="append", default=[])
    apply.add_argument("--output", type=Path)
    rollback = subparsers.add_parser("rollback")
    rollback.add_argument("--tenant-id", required=True)
    rollback.add_argument("--admin-username", required=True)
    rollback.add_argument("--result", type=Path, required=True)
    rollback.add_argument("--output", type=Path)
    return parser


def _parse_acknowledgements(values: list[str]) -> dict[str, set[str]]:
    """把 path=flag 参数转换为逐路径风险确认映射。"""

    result: dict[str, set[str]] = {}
    for value in values:
        path, separator, flag = value.partition("=")
        if not separator or not path.strip() or not flag.strip():
            raise ValueError("--acknowledge-review must use upstream_path=review_flag")
        result.setdefault(path.strip(), set()).add(flag.strip())
    return result


def main(argv: Sequence[str] | None = None) -> int:
    """执行专家同步计划或受控 apply，并打印本地结果产物路径。"""

    args = _parser().parse_args(argv)
    try:
        if args.command == "plan":
            result = build_sync_plan(
                default_session_factory,
                args.input,
                args.tenant_id,
                args.admin_username,
                baseline_package_dir=args.baseline_input,
                baseline_localization_dir=args.baseline_localization,
                output_path=args.output,
            )
        elif args.command == "apply":
            result = apply_sync_plan(
                default_session_factory,
                args.input,
                args.plan,
                args.tenant_id,
                args.admin_username,
                localization_dir=args.localization,
                approved_paths=set(args.approve_path),
                acknowledged_review_flags=_parse_acknowledgements(args.acknowledge_review),
                output_path=args.output,
            )
        else:
            result = rollback_sync_result(
                default_session_factory,
                args.result,
                args.tenant_id,
                args.admin_username,
                output_path=args.output,
            )
    except (
        ExpertSyncApplyError,
        ExpertSyncPlanError,
        ExpertSyncRollbackError,
        OSError,
        ValueError,
    ) as exc:
        print(f"error: {exc}")
        return 2
    print(result.result_path)
    print(" ".join(f"{key}={value}" for key, value in sorted(result.counts.items())))
    if hasattr(result, "report_path"):
        print(result.report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

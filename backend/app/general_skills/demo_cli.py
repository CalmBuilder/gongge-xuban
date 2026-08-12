"""
@Time       : 2026/08/12 13:05
@Author     : zhanglp8181
@File       : demo_cli.py
@CallChain  : python -m app.general_skills.demo_cli → demo_seed → database
@Description: 提供显式、幂等且不管理密码的 Skill 五闭环演示初始化与检查命令。
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from sqlmodel import Session

from app.db import engine
from app.general_skills.demo_seed import (
    SkillDemoSeedError,
    initialize_skill_five_closure_demo,
    inspect_skill_five_closure_demo,
)


def _parser() -> argparse.ArgumentParser:
    """建立只包含 init/inspect 的显式命令行契约。"""

    parser = argparse.ArgumentParser(prog="skill-five-closure-demo")
    subparsers = parser.add_subparsers(dest="command", required=True)
    init = subparsers.add_parser("init", help="initialize demo agents for existing users")
    init.add_argument("--tenant-id", default="tenant_demo")
    init.add_argument("--owner-username", default="user_demo")
    init.add_argument("--adopter-username", default="approver_demo")
    init.add_argument("--reviewer-username", default="admin")
    inspect = subparsers.add_parser("inspect", help="inspect demo agent readiness")
    inspect.add_argument("--tenant-id", default="tenant_demo")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """执行初始化或只读检查，并以 JSON 输出可留存的验收事实。"""

    args = _parser().parse_args(argv)
    try:
        with Session(engine) as db:
            if args.command == "init":
                result = initialize_skill_five_closure_demo(
                    db,
                    tenant_id=args.tenant_id,
                    owner_username=args.owner_username,
                    adopter_username=args.adopter_username,
                    reviewer_username=args.reviewer_username,
                )
            else:
                result = inspect_skill_five_closure_demo(db, tenant_id=args.tenant_id)
    except SkillDemoSeedError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
@Time       : 2026/07/29 11:45
@Author     : zhanglp8181
@File       : migrate_m55d_sops.py
@CallChain  : 运维显式预演/应用 → M5.5-D 迁移服务 → 正式发布头与数字员工分支
@Description: 默认回滚预演全部 SOP 升级，只有传入 --apply 才在单事务内提交。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "backend"))

from sqlmodel import Session  # noqa: E402

from app.db import engine  # noqa: E402
from app.sop_runtime.bulk_migration import apply_m55_published_head_upgrade  # noqa: E402
from app.sop_runtime.migration_inventory import build_sop_migration_inventory  # noqa: E402


def main() -> int:
    """执行一次完整校验；默认回滚，明确传入 --apply 后才提交。"""

    parser = argparse.ArgumentParser(description="M5.5-D 全 SOP 受控升级")
    parser.add_argument("--tenant-id", default="tenant_demo")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="提交迁移；省略时仅在事务内预演并回滚",
    )
    args = parser.parse_args()

    with Session(engine) as db:
        try:
            report = apply_m55_published_head_upgrade(
                db,
                tenant_id=args.tenant_id,
                require_all=True,
            )
            db.flush()
            inventory = build_sop_migration_inventory(db, tenant_id=args.tenant_id)
            output = {
                "mode": "apply" if args.apply else "preview",
                "migrated": list(report.migrated_skill_ids),
                "already_migrated": list(report.already_migrated_skill_ids),
                "missing": list(report.missing_skill_ids),
                "synchronized_branches": list(report.synchronized_branch_ids),
                "already_synchronized_branches": list(
                    report.already_synchronized_branch_ids
                ),
                "disposition_counts": inventory.disposition_counts,
                "dependency_counts": inventory.dependency_counts,
            }
            if args.apply:
                db.commit()
            else:
                db.rollback()
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return 0
        except Exception:
            db.rollback()
            raise


if __name__ == "__main__":
    raise SystemExit(main())

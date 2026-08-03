"""
@Time       : 2026/08/02 14:10
@Author     : zhanglp8181
@File       : migrate_web_search_sop.py
@CallChain  : 运维显式预演/应用 → 联网查询条件迁移 → 全发布 SOP 迁移清单
@Description: 默认回滚预演联网查询 1.1.1 升级，只有传入 --apply 才提交。
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
from app.sop_runtime.migration_inventory import build_sop_migration_inventory  # noqa: E402
from app.sop_runtime.web_search_migration import (  # noqa: E402
    apply_web_search_condition_upgrade,
)


def main() -> int:
    """执行联网查询受控迁移；默认预演并回滚，明确传入 apply 后提交。"""

    parser = argparse.ArgumentParser(description="联网查询 SOP 受限条件升级")
    parser.add_argument("--tenant-id", default="tenant_demo")
    parser.add_argument("--apply", action="store_true", help="提交迁移；省略时回滚预演")
    args = parser.parse_args()
    with Session(engine) as db:
        try:
            report = apply_web_search_condition_upgrade(db, tenant_id=args.tenant_id)
            db.flush()
            inventory = build_sop_migration_inventory(db, tenant_id=args.tenant_id)
            output = {
                "mode": "apply" if args.apply else "preview",
                "migrated": report.migrated,
                "already_migrated": report.already_migrated,
                "synchronized_branches": list(report.synchronized_branch_ids),
                "already_synchronized_branches": list(
                    report.already_synchronized_branch_ids
                ),
                "total": inventory.total,
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

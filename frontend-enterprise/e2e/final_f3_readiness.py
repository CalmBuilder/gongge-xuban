"""
@Time       : 2026/10/04
@Author     : zhanglp8181
@File       : final_f3_readiness.py
@CallChain  : F3独立准备命令 → 私有sandbox声明/原工具合同 → 源码冻结与安全待验档案
@Description: 完成供应商无关的JRN08准备；不加载旧starter、本地provider或启动真实旅程。
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from final_f3_preparation import PreparationError, load_private_profile, safe_profile_summary


def snapshot(root: Path) -> dict[str, str]:
    """冻结当前代码、测试、迁移和浏览器素材，不读取dotenv、数据库或生成目录。"""
    paths: set[Path] = set()
    for directory in ("backend/app", "backend/tests", "backend/alembic",
                      "frontend-enterprise/src", "frontend-enterprise/e2e"):
        paths.update(path for path in (root / directory).rglob("*")
                     if path.is_file() and path.suffix in
                     {".py", ".ts", ".tsx", ".md", ".json", ".sql", ".css"}
                     and "__pycache__" not in path.parts)
    for name in ("AGENTS.md", "backend/pyproject.toml", "backend/alembic.ini",
                 "frontend-enterprise/package.json", "frontend-enterprise/package-lock.json",
                 "frontend-enterprise/playwright.config.ts", "frontend-enterprise/vite.config.ts"):
        if (root / name).is_file():
            paths.add(root / name)
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(paths)}


def _write(path: Path, value: object) -> None:
    """新轮目录中排他写入白名单JSON，拒绝覆盖旧轮原字节。"""
    with path.open("x") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, indent=2))


def prepare(*, root: Path, profile_path: Path, output: Path) -> int:
    """仅校验和冻结准备；缺条件或仅结构兼容都返回2，永不产生真实验收通过。"""
    output.mkdir(parents=True, exist_ok=False)
    before = snapshot(root)
    _write(output / "source-start.safe.json", before)
    result: dict[str, object] = {
        "phase": "I1_preparation", "I1_passed": False, "JRN08_passed": False,
        "I2": "not_run", "I3": "not_run", "I4": "not_run",
        "remote_calls": 0, "services_started": 0,
        "original_starter_or_local_provider_loaded": False,
        "vendor_specific_browser_adapter_complete": False,
    }
    try:
        profile = load_private_profile(profile_path)
    except PreparationError as exc:
        result["missing_condition"] = exc.code
    else:
        result["profile"] = safe_profile_summary(profile)
        result["missing_condition"] = "F3_REAL_VENDOR_QUALIFICATION_PENDING"
    after = snapshot(root)
    _write(output / "source-end.safe.json", {"files": after, "unchanged": before == after})
    result["source_unchanged"] = before == after
    _write(output / "readiness.safe.json", result)
    return 2


def main() -> int:
    """从明确私有文件准备新轮档案；不接收密钥参数，不将缺条件当skip或成功。"""
    parser = argparse.ArgumentParser(description="F3供应商无关准备；不会执行远端旅程")
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    try:
        return prepare(root=root, profile_path=args.profile, output=args.output)
    except FileExistsError:
        print("F3_NEW_EVIDENCE_DIRECTORY_REQUIRED")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

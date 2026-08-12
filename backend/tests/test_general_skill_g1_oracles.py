"""
@Time       : 2026/08/12 11:05
@Author     : zhanglp8181
@File       : test_general_skill_g1_oracles.py
@CallChain  : pytest → G1 versioned Oracle fixture → batch acceptance tests
@Description: 冻结四个固定 GitHub Skill 与一个 Agent 自创 Skill 的来源和验收身份。
"""

from __future__ import annotations

import json
from pathlib import Path


ORACLE_PATH = Path(__file__).parent / "fixtures" / "general_skill_g1_oracles.json"


def _oracles() -> dict[str, object]:
    """读取纳入版本控制的 G1 Oracle，避免测试从漂移分支推导期望值。"""

    return json.loads(ORACLE_PATH.read_text(encoding="utf-8"))


def test_github_oracles_freeze_one_repository_commit_and_distinct_skills() -> None:
    """证明 A/B/C1/D 固定同一上游 commit 且内容身份互不冒充。"""

    payload = _oracles()
    github = payload["github"]
    skills = payload["skills"]
    assert github == {
        "repository": "https://github.com/mattpocock/skills",
        "revision": "84fdeffd12f2ee307994d1eb6feb48173b6e0502",
        "archive_sha256": "ebc29488fb0012d3ac07b5b66946d4fb86fb17ccf6c8620c654cff7c075fa5b6",
        "verified_at": "2026-08-12T10:51:00+08:00",
    }
    assert set(skills) == {"G1-A", "G1-B", "G1-C1", "G1-D"}
    assert len({row["source_subpath"] for row in skills.values()}) == 4
    assert len({row["content_checksum"] for row in skills.values()}) == 4
    assert len({row["normalized_checksum"] for row in skills.values()}) == 4
    assert skills["G1-D"]["invocation_policy"] == "user_only"
    assert "contains_executable_content" in skills["G1-B"]["risk_findings"]


def test_agent_authored_oracle_cannot_claim_remote_provenance() -> None:
    """证明 C2 是受管执行证据驱动的原创提案，不可改名冒充 C1 或 GitHub 导入。"""

    authored = _oracles()["authored_skill"]
    assert authored["requirement_id"] == "G1-C2"
    assert authored["proposal_kind"] == "authored"
    assert authored["source_kind"] == "agent_proposal"
    assert len(authored["required_evidence_paths"]) >= 3
    assert "tdd" in authored["forbidden_name_aliases"]
    assert set(authored["forbidden_provenance"]) == {"github", "skillhub", "https", "upload"}

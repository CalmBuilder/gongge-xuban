"""
@Time       : 2026/08/12 12:10
@Author     : zhanglp8181
@File       : test_general_skill_g1_install_intents.py
@CallChain  : pytest → GeneralSkillInstallIntentService → ImportJob/Revision/Binding
@Description: 验证对话显式安装卡的持久恢复、本人确认、取消、stale 与跨用户隔离。
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import (
    AgentProfile,
    AgentResourceBinding,
    ChatSession,
    GeneralSkill,
    GeneralSkillInstallIntent,
    Tenant,
    User,
)
from app.general_skills.install_intents import (
    GeneralSkillInstallIntentError,
    GeneralSkillInstallIntentService,
)
from app.general_skills.install_intent_schema import GeneralSkillInstallIntentCreate
from app.general_skills.object_store import FileSystemSkillObjectStore
from app.general_skills.remote_source import RemoteFetchResult


class _Fetcher:
    """返回固定 diagnosing-bugs 风格包，并验证正式 GitHub archive URL。"""

    def fetch(self, source_url: str, **_: object) -> RemoteFetchResult:
        """构造含风险脚本但不会执行的确定性 Skill 包。"""

        assert source_url.endswith("84fdeffd12f2ee307994d1eb6feb48173b6e0502.zip")
        payload = BytesIO()
        with ZipFile(payload, "w") as archive:
            archive.writestr(
                "skills-fixed/skills/engineering/diagnosing-bugs/SKILL.md",
                "---\nname: diagnosing-bugs\ndescription: Diagnose hard bugs.\n---\n"
                "# Diagnosing Bugs\nBuild a red-capable loop before changing code.\n",
            )
            archive.writestr(
                "skills-fixed/skills/engineering/diagnosing-bugs/scripts/hitl-loop.template.sh",
                "#!/bin/sh\nexit 99\n",
            )
        return RemoteFetchResult(source_url, payload.getvalue(), 0)


def _context(tmp_path: Path) -> tuple[Session, GeneralSkillInstallIntentService, User, User]:
    """建立所有者/他人、本人 Agent 和本人会话。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    db = Session(engine)
    owner = User(
        id="user_g1_b",
        tenant_id="tenant_g1_b",
        username="owner",
        role="member",
        password_hash="unused",
    )
    other = User(
        id="user_g1_b_other",
        tenant_id=owner.tenant_id,
        username="other",
        role="member",
        password_hash="unused",
    )
    db.add(Tenant(id=owner.tenant_id, name="G1 B"))
    db.add(owner)
    db.add(other)
    db.add(
        AgentProfile(
            id="agent_g1_b",
            tenant_id=owner.tenant_id,
            name="故障诊断分身",
            owner_user_id=owner.id,
        )
    )
    db.add(
        ChatSession(
            id="session_g1_b",
            tenant_id=owner.tenant_id,
            user_id=owner.id,
            agent_id="agent_g1_b",
        )
    )
    db.commit()
    return db, GeneralSkillInstallIntentService(db, FileSystemSkillObjectStore(tmp_path)), owner, other


def _request() -> GeneralSkillInstallIntentCreate:
    """构造固定 commit 与目录的 GitHub 安装来源。"""

    return GeneralSkillInstallIntentCreate(
        agent_id="agent_g1_b",
        source_url="https://github.com/mattpocock/skills",
        revision="84fdeffd12f2ee307994d1eb6feb48173b6e0502",
        source_subpath="skills/engineering/diagnosing-bugs",
    )


def test_explicit_install_persists_card_then_owner_confirms_once(tmp_path: Path) -> None:
    """证明批准前零 Skill/Binding，刷新可恢复，批准后私有 Revision 与绑定一次生效。"""

    db, service, owner, _ = _context(tmp_path)
    card = service.create(
        "session_g1_b",
        _request(),
        idempotency_key="g1-b-create",
        current_user=owner,
        fetcher=_Fetcher(),
    )
    assert card.status == "awaiting_owner_confirmation"
    assert card.candidates[0].name == "diagnosing-bugs"
    assert "contains_executable_content" in card.candidates[0].risk_findings
    assert db.exec(select(GeneralSkill)).all() == []
    assert db.exec(select(AgentResourceBinding)).all() == []
    assert service.list_session("session_g1_b", current_user=owner)[0].id == card.id

    installed = service.resolve(
        "session_g1_b",
        card.id,
        command="confirm",
        expected_row_version=card.row_version,
        current_user=owner,
    )
    assert installed.status == "installed"
    skill = db.exec(select(GeneralSkill)).one()
    assert skill.owner_user_id == owner.id
    assert skill.visibility_scope == "user_private"
    assert len(db.exec(select(AgentResourceBinding)).all()) == 1
    replay = service.resolve(
        "session_g1_b",
        card.id,
        command="confirm",
        expected_row_version=installed.row_version,
        current_user=owner,
    )
    assert replay.status == "installed"
    assert len(db.exec(select(GeneralSkill)).all()) == 1


def test_cancel_and_cross_user_or_stale_commands_never_install(tmp_path: Path) -> None:
    """证明他人、陈旧卡与取消终态都不能生成 Revision/Binding。"""

    db, service, owner, other = _context(tmp_path)
    card = service.create(
        "session_g1_b",
        _request(),
        idempotency_key="g1-b-cancel",
        current_user=owner,
        fetcher=_Fetcher(),
    )
    with pytest.raises(GeneralSkillInstallIntentError) as forbidden:
        service.list_session("session_g1_b", current_user=other)
    assert forbidden.value.code == "GENERAL_SKILL_INSTALL_NOT_FOUND"
    with pytest.raises(GeneralSkillInstallIntentError) as stale:
        service.resolve(
            "session_g1_b",
            card.id,
            command="confirm",
            expected_row_version=card.row_version + 1,
            current_user=owner,
        )
    assert stale.value.code == "GENERAL_SKILL_INSTALL_STALE"
    cancelled = service.resolve(
        "session_g1_b",
        card.id,
        command="cancel",
        expected_row_version=card.row_version,
        current_user=owner,
    )
    assert cancelled.status == "cancelled"
    assert db.exec(select(GeneralSkill)).all() == []
    assert db.exec(select(AgentResourceBinding)).all() == []
    assert db.get(GeneralSkillInstallIntent, card.id).terminal_at is not None

"""
@Time       : 2026/07/22 09:25
@Author     : zhanglp8181
@File       : test_sop_versioning.py
@CallChain  : pytest → SOP 版本策略 → SQLite SkillVersion
@Description: 验证发布快照不可变、重复发布幂等和草稿转发布行为。
"""

import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.skills import _upsert_skill_version, delete_skill
from app.db.models import Skill, SkillVersion, Tenant, User
from app.skills.skill_schema import SkillCard, SkillGraphNode
from app.sop_runtime import compile_legacy_skill_card
from app.sop_runtime.versioning import PublishedVersionConflictError, write_skill_version


def _test_session() -> Session:
    """创建每个测试独占、支持事务断言的内存 SQLite 会话。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _skill_card(*, name: str = "采购申请", version: str = "1.0.0") -> SkillCard:
    """构造可被发布编译器接受的最小 SOP 定义。"""

    return SkillCard(
        skill_id="purchase_request",
        name=name,
        version=version,
        nodes=[SkillGraphNode(node_id="reply", type="response", name="返回结果")],
        start_node_id="reply",
        terminal_node_ids=["reply"],
    )


def _skill(card: SkillCard, *, status: str = "published") -> Skill:
    """把测试定义转换为版本策略接收的编辑头实体。"""

    return Skill(
        tenant_id="tenant_demo",
        skill_id=card.skill_id,
        version=card.version,
        name=card.name,
        description=card.description,
        content_json=card.model_dump(),
        status=status,
    )


def test_repeated_publication_with_same_content_is_idempotent() -> None:
    """验证相同租户、SOP、版本和内容的重复发布返回原快照。"""

    card = _skill_card()
    skill = _skill(card)
    compiled = compile_legacy_skill_card(card)
    with _test_session() as db:
        first = write_skill_version(db, skill, compiled_definition=compiled)
        db.commit()
        second = write_skill_version(db, skill, compiled_definition=compiled)
        db.commit()
        rows = db.exec(select(SkillVersion)).all()

    assert first.created is True
    assert second.idempotent is True
    assert second.version.id == first.version.id
    assert len(rows) == 1
    assert rows[0].content_checksum
    assert rows[0].compiled_definition_checksum == compiled.checksum


def test_published_version_rejects_different_content() -> None:
    """验证已发布业务版本不能被同版本的不同完整内容覆盖。"""

    original_card = _skill_card()
    original_skill = _skill(original_card)
    changed_card = _skill_card(name="被修改的采购申请")
    changed_skill = _skill(changed_card)
    with _test_session() as db:
        write_skill_version(
            db,
            original_skill,
            compiled_definition=compile_legacy_skill_card(original_card),
        )
        db.commit()

        with pytest.raises(PublishedVersionConflictError):
            write_skill_version(
                db,
                changed_skill,
                compiled_definition=compile_legacy_skill_card(changed_card),
            )
        db.rollback()
        persisted = db.exec(select(SkillVersion)).one()

    assert persisted.name == "采购申请"
    assert persisted.content_json["name"] == "采购申请"


def test_api_version_writer_returns_structured_conflict() -> None:
    """验证发布 API 边界把同版本异内容冲突稳定转换为结构化 HTTP 409。"""

    original_card = _skill_card()
    changed_card = _skill_card(name="冲突的采购申请")
    with _test_session() as db:
        write_skill_version(
            db,
            _skill(original_card),
            compiled_definition=compile_legacy_skill_card(original_card),
        )
        db.commit()

        with pytest.raises(HTTPException) as exc_info:
            _upsert_skill_version(
                db,
                _skill(changed_card),
                compiled_definition=compile_legacy_skill_card(changed_card),
            )
        db.rollback()

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == {
        "code": "PUBLISHED_SKILL_VERSION_IMMUTABLE",
        "skill_id": "purchase_request",
        "version": "1.0.0",
    }


def test_draft_snapshot_can_change_before_first_publication() -> None:
    """验证尚未发布的同版本草稿可编辑，首次发布后才进入不可变状态。"""

    draft_card = _skill_card()
    changed_card = _skill_card(name="完善后的采购申请")
    with _test_session() as db:
        write_skill_version(db, _skill(draft_card, status="draft"))
        db.commit()
        result = write_skill_version(
            db,
            _skill(changed_card),
            compiled_definition=compile_legacy_skill_card(changed_card),
        )
        db.commit()
        persisted = db.exec(select(SkillVersion)).one()

    assert result.created is False
    assert result.idempotent is False
    assert persisted.status == "published"
    assert persisted.name == "完善后的采购申请"
    assert persisted.published_at is not None


def test_new_head_version_does_not_change_previous_snapshot() -> None:
    """验证编辑头发布新版本后，按旧版本标识读取的内容仍保持原样。"""

    original_card = _skill_card(version="1.0.0")
    next_card = _skill_card(name="采购申请新版", version="1.1.0")
    with _test_session() as db:
        write_skill_version(
            db,
            _skill(original_card),
            compiled_definition=compile_legacy_skill_card(original_card),
        )
        db.commit()
        write_skill_version(
            db,
            _skill(next_card),
            compiled_definition=compile_legacy_skill_card(next_card),
        )
        db.commit()
        versions = db.exec(select(SkillVersion).order_by(SkillVersion.version)).all()

    assert [version.version for version in versions] == ["1.0.0", "1.1.0"]
    assert versions[0].content_json["name"] == "采购申请"
    assert versions[1].content_json["name"] == "采购申请新版"


def test_deleting_skill_head_retains_published_snapshot() -> None:
    """验证删除编辑头只清理草稿版本，已发布快照仍可供历史实例引用。"""

    published_card = _skill_card(version="1.0.0")
    draft_card = _skill_card(name="未发布草稿", version="1.1.0")
    skill_head = _skill(published_card)
    admin = User(
        id="user_admin",
        tenant_id="tenant_demo",
        username="admin",
        role="admin",
        password_hash="test",
    )
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="演示租户"))
        db.add(skill_head)
        write_skill_version(
            db,
            skill_head,
            compiled_definition=compile_legacy_skill_card(published_card),
        )
        write_skill_version(db, _skill(draft_card, status="draft"))
        db.commit()

        result = delete_skill(
            skill_head.skill_id,
            tenant_id="tenant_demo",
            db=db,
            current_user=admin,
        )
        versions = db.exec(select(SkillVersion)).all()

    assert result == {"status": "deleted"}
    assert [(version.version, version.status) for version in versions] == [
        ("1.0.0", "published")
    ]

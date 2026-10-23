"""
@Time       : 2026/10/03
@Author     : zhanglp8181
@File       : test_staffdeck_sd8_retrieval.py
@CallChain  : pytest SQLite/MySQL → published 精确知识范围 → OKF/Service → 确定性证据
@Description: 复现评分前120截断遗漏老概念，并验证同分物理身份与候选/租户边界。
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlmodel import Session, SQLModel, create_engine

from app.db.models import KnowledgeBase, KnowledgeBaseVersion, KnowledgeConcept, Tenant
from app.knowledge.okf import search_concepts
from app.knowledge.schema import KnowledgeSearchRequest
from app.knowledge.service import KnowledgeService


@pytest.fixture(params=["sqlite", pytest.param("mysql", marks=pytest.mark.mysql)])
def retrieval_db(request, tmp_path):
    """SQLite用文件，MySQL复用原随机账号/数据库并执行真实Alembic迁移。"""
    if request.param == "mysql":
        url = request.getfixturevalue("mysql_database_url")
        root = Path(__file__).resolve().parents[1]
        config = Config(str(root / "alembic.ini"))
        config.set_main_option("script_location", str(root / "alembic"))
        config.attributes["database_url"] = url
        command.upgrade(config, "head")
    else:
        url = "sqlite:///" + str(tmp_path / "retrieval.sqlite3")
    engine = create_engine(url)
    if request.param == "sqlite":
        SQLModel.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            yield db
    finally:
        engine.dispose()


def test_old_relevant_concept_is_ranked_before_any_candidate_cutoff(retrieval_db):
    """第121个唯一老相关概念必须入候选及最终证据，hidden与跨tenant同词不能混入。"""
    db = retrieval_db
    db.add(Tenant(id="own", name="Synthetic owner"))
    db.add(KnowledgeBase(id="kb", tenant_id="own", name="Synthetic knowledge"))
    for version, state in (("published", "published"), ("candidate", "candidate")):
        db.add(KnowledgeBaseVersion(id=version, tenant_id="own", knowledge_base_id="kb",
                                   name=version, version=version, publication_state=state))
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    for index in range(121):
        text = "ORCHID121 额度 730" if index == 0 else f"PLUTO{index:05d} unrelated entry"
        db.add(KnowledgeConcept(id=f"row-{index}", tenant_id="own", knowledge_base_id="kb",
            knowledge_base_version_id="published", concept_id=f"concept-{index}",
            concept_type="Business Rule", title=text, content_md=text,
            created_at=stamp, updated_at=stamp + timedelta(seconds=index)))
    for identity, tenant, version in (("hidden", "own", "candidate"),
                                       ("foreign", "other", "published")):
        db.add(KnowledgeConcept(id=identity, tenant_id=tenant, knowledge_base_id="kb",
            knowledge_base_version_id=version, concept_id=identity, concept_type="Business Rule",
            title="ORCHID121 额度 999", content_md="ORCHID121 额度 999"))
    db.commit()
    request = KnowledgeSearchRequest(tenant_id="own", knowledge_base_ids=["kb"],
        knowledge_base_version_ids=["published"], query="ORCHID121")
    service = KnowledgeService(db)
    assert len(service._load_concepts_for_search(request)) == 121
    response = service.search(request)
    assert [row["id"] for row in response.selected_concepts] == ["row-0"]
    # 无原文/来源引用的独立概念仍遵守原充分性合同；召回正确不等于来源充分。
    assert response.outcome == "insufficient"
    assert response.evidence_sufficiency.reason == "no_evidence"
    assert response.evidence_sufficiency.satisfied is False
    hidden = service.search(request.model_copy(update={"knowledge_base_version_ids": ["candidate"]}))
    assert hidden.outcome == "no_match" and not hidden.selected_concepts


def test_equal_score_and_timestamp_have_physical_id_tie_break(retrieval_db):
    """完全同分同时间的不同物理概念不依赖输入顺序或数据库collation。"""
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    rows = [KnowledgeConcept(id=identity, tenant_id="own", knowledge_base_id="kb",
        concept_id=identity, concept_type="Topic", title="equalpolicy", content_md="equalpolicy",
        updated_at=stamp) for identity in ("row-z", "row-a", "row-B")]
    expected = ["row-B", "row-a", "row-z"]
    assert [row.id for row in search_concepts("equalpolicy", rows)] == expected
    assert [row.id for row in search_concepts("equalpolicy", list(reversed(rows)))] == expected
    db = retrieval_db
    db.add(Tenant(id="own", name="Synthetic owner"))
    db.add(KnowledgeBase(id="kb", tenant_id="own", name="Legacy tie control"))
    db.add_all(list(reversed(rows)))
    db.commit()
    response = KnowledgeService(db).search(KnowledgeSearchRequest(
        tenant_id="own", knowledge_base_ids=["kb"], query="equalpolicy"))
    assert [row["id"] for row in response.selected_concepts] == expected

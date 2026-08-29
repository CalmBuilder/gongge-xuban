"""
@Time       : 2026/08/29 12:00
@Author     : zhanglp8181
@File       : test_expert_sync_plan.py
@CallChain  : pytest → SQLite/MySQL 会话 → 导入包基线比较 → 只读同步计划断言
@Description: 验证专家同步计划的版本差异、中文基线保护、租户隔离及双方言行为一致。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, select

from app.db.models import AgentProfile, AgentResourceBinding, Tenant, User
from app.experts.capability import build_capability_manifest, estimate_input_tokens
from app.experts.localization_package import _manifest_hash, _translation_hash
from app.experts.localization_schema import (
    LocalizationManifest,
    LocalizationManifestItem,
    LocalizedExpert,
)
from app.experts.local_source import LocalSource
from app.experts.package import (
    load_and_verify_package,
    prepare_expert,
    write_preview_package,
)
from app.experts.parser import ParsedExpert
from app.experts.schema import CapabilityAnalysis, ExpertTranslation
from app.experts import sync_cli
from app.experts.sync_apply import apply_sync_plan
from app.experts.sync_rollback import rollback_sync_result
from app.experts.sync_plan import build_sync_plan
from app.experts.translation import preserve_original_translation


OLD_COMMIT = "a" * 40
CURRENT_COMMIT = "b" * 40


def _session_factory(database_url: str = "sqlite://"):
    """创建带租户管理员和三种专家状态的测试会话工厂。"""

    engine_kwargs: dict[str, object] = {}
    if database_url == "sqlite://":
        engine_kwargs.update(
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    engine = create_engine(database_url, **engine_kwargs)
    if database_url.startswith("sqlite"):
        SQLModel.metadata.create_all(engine)

    def factory() -> Session:
        return Session(engine)

    with factory() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            User(
                id="user_admin",
                tenant_id="tenant_demo",
                username="admin",
                role="admin",
                password_hash="x",
            )
        )
        db.add(
            AgentProfile(
                id="agent_unchanged",
                tenant_id="tenant_demo",
                name="中文Stable Expert",
                description="中文Stable description",
                persona_prompt="中文Stable prompt",
                original_name="Stable Expert",
                original_description="Stable description",
                original_persona_prompt="Stable prompt",
                original_locale="en-US",
                metadata_json={
                    "employee_type": "expert",
                    "expert_source_code": "agency-agents",
                    "upstream_path": "engineering/stable.md",
                    "upstream_commit": OLD_COMMIT,
                },
            )
        )
        db.add(
            AgentProfile(
                id="agent_changed",
                tenant_id="tenant_demo",
                name="中文Changed Expert",
                description="旧中文描述",
                persona_prompt="旧中文提示词",
                original_name="Changed Expert",
                original_description="Old description",
                original_persona_prompt="Old prompt",
                original_locale="en-US",
                published_to_gallery=True,
                metadata_json={
                    "employee_type": "expert",
                    "expert_source_code": "agency-agents",
                    "upstream_path": "engineering/changed.md",
                    "upstream_commit": OLD_COMMIT,
                    "published_to_gallery": True,
                },
            )
        )
        db.add(
            AgentProfile(
                id="agent_removed",
                tenant_id="tenant_demo",
                name="下线候选",
                description="仍保留",
                persona_prompt="仍保留",
                metadata_json={
                    "employee_type": "expert",
                    "expert_source_code": "agency-agents",
                    "upstream_path": "engineering/removed.md",
                    "upstream_commit": OLD_COMMIT,
                },
            )
        )
        db.add(
            AgentProfile(
                id="ordinary",
                tenant_id="tenant_demo",
                name="普通员工",
                persona_prompt="普通员工",
                metadata_json={},
            )
        )
        db.commit()
    return factory


def _prepared(
    path: str,
    name: str,
    description: str,
    prompt: str,
    source_sha: str,
    source_commit: str,
):
    """构造一个可写入测试包的最小专家。"""

    parsed = ParsedExpert(
        upstream_path=path,
        name=name,
        description=description,
        category_original="research" if path.startswith("research/") else "engineering",
        tools=[],
        services=[],
        source_markdown=prompt,
        source_sha256=source_sha,
    )
    analysis = CapabilityAnalysis(
        required_capabilities=["prompt_reasoning"],
        evidence=[description],
    )
    translation = ExpertTranslation(
        name_zh=name,
        description_zh=description,
        category_zh="学术研究" if path.startswith("research/") else "工程研发",
        tags_zh=["research" if path.startswith("research/") else "engineering"],
        markdown_zh=prompt,
        high_risk=False,
        capability_analysis=analysis,
    )
    return prepare_expert(
        parsed,
        translation,
        build_capability_manifest(parsed, analysis),
        estimate_input_tokens(prompt),
        source_commit=source_commit,
    )


def _write_package(tmp_path: Path, name: str, experts, commit: str) -> Path:
    """写入指定提交的最小导入包。"""

    source_root = tmp_path / name
    source_root.mkdir()
    output = tmp_path / f"{name}-package"
    write_preview_package(
        output,
        LocalSource(
            root=source_root,
            commit_sha=commit,
            remote_url="https://github.com/msitarzewski/agency-agents.git",
            verified=True,
        ),
        "tenant_demo",
        experts,
        [],
    )
    return output


def _write_localization(
    tmp_path: Path,
    experts: list[LocalizedExpert],
    source_batch_id: str,
    source_commit: str = OLD_COMMIT,
    name: str = "baseline-localization",
) -> Path:
    """写入与旧导入包匹配的最小中文基线包。"""

    output = tmp_path / name
    experts_dir = output / "experts"
    experts_dir.mkdir(parents=True)
    items: list[LocalizationManifestItem] = []
    for expert in experts:
        filename = f"{expert.upstream_path.removesuffix('.md').replace('/', '__')}.json"
        content = expert.model_dump_json().encode("utf-8")
        (experts_dir / filename).write_bytes(content)
        items.append(
            LocalizationManifestItem(
                upstream_path=expert.upstream_path,
                filename=filename,
                sha256=hashlib.sha256(content).hexdigest(),
            )
        )
    manifest = LocalizationManifest(
        generated_at=datetime.now(timezone.utc),
        tenant_id="tenant_demo",
        source_batch_id=source_batch_id,
        source_commit=source_commit,
        model_config_id="model",
        model_name="test",
        selected_count=len(experts),
        verified_count=len(experts),
        failed_count=0,
        experts=items,
        manifest_sha256="",
    )
    manifest = manifest.model_copy(update={"manifest_sha256": _manifest_hash(manifest)})
    (output / "manifest.json").write_text(
        json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )
    return output


def _localized(
    path: str,
    source: str,
    name: str,
    description: str,
    prompt: str,
    *,
    source_batch_id: str = "old-import-batch",
    source_commit: str = OLD_COMMIT,
) -> LocalizedExpert:
    """构造一个已验证中文内容基线。"""

    value = LocalizedExpert(
        upstream_path=path,
        source_batch_id=source_batch_id,
        source_commit=source_commit,
        source_content_sha256=source,
        original_name=name,
        original_description=description,
        original_prompt=prompt,
        localized_name=f"中文{name}",
        localized_description=f"中文{description}",
        localized_prompt=f"中文{prompt}",
        category_zh="工程研发",
        chunks=[],
        translation_sha256="",
    )
    return value.model_copy(update={"translation_sha256": _translation_hash(value)})


def test_plan_is_read_only_and_matches_sqlite_baseline(tmp_path: Path) -> None:
    """验证 SQLite 准确识别新增、未变化、上游修改和上游删除，且不写专家表。"""

    factory = _session_factory()
    stable = _prepared(
        "engineering/stable.md",
        "Stable Expert",
        "Stable description",
        "Stable prompt",
        "1" * 64,
        CURRENT_COMMIT,
    )
    changed = _prepared(
        "engineering/changed.md",
        "Changed Expert",
        "New description",
        "New prompt",
        "2" * 64,
        CURRENT_COMMIT,
    )
    new = _prepared(
        "research/research-synthesist.md",
        "Research Synthesist",
        "Research",
        "Research prompt",
        "3" * 64,
        CURRENT_COMMIT,
    )
    old_stable = _prepared(
        "engineering/stable.md",
        "Stable Expert",
        "Stable description",
        "Stable prompt",
        "1" * 64,
        OLD_COMMIT,
    )
    old_changed = _prepared(
        "engineering/changed.md",
        "Changed Expert",
        "Old description",
        "Old prompt",
        "4" * 64,
        OLD_COMMIT,
    )
    current_package = _write_package(tmp_path, "current", [stable, changed, new], CURRENT_COMMIT)
    baseline_package = _write_package(
        tmp_path,
        "baseline",
        [old_stable, old_changed],
        OLD_COMMIT,
    )
    baseline_manifest, _ = load_and_verify_package(baseline_package, "tenant_demo")
    localization = _write_localization(
        tmp_path,
        [
            _localized(
                "engineering/stable.md",
                "1" * 64,
                "Stable Expert",
                "Stable description",
                "Stable prompt",
            ),
            _localized(
                "engineering/changed.md",
                "4" * 64,
                "Changed Expert",
                "Old description",
                "Old prompt",
            ),
        ],
        baseline_manifest.batch_id,
    )
    with factory() as db:
        before = {
            row.id: (row.name, row.description, row.persona_prompt, row.metadata_json)
            for row in db.exec(select(AgentProfile)).all()
        }

    result = build_sync_plan(
        factory,
        current_package,
        "tenant_demo",
        "admin",
        baseline_package_dir=baseline_package,
        baseline_localization_dir=localization,
        output_path=tmp_path / "sync-plan.json",
    )

    assert result.counts == {
        "new": 1,
        "source_removed": 1,
        "unchanged": 1,
        "upstream_changed": 1,
    }
    by_path = {item.upstream_path: item for item in result.items}
    assert by_path["engineering/stable.md"].local_change == "clean"
    assert by_path["engineering/changed.md"].local_change == "modified"
    assert "locally_modified" in by_path["engineering/changed.md"].review_flags
    assert "published" in by_path["engineering/changed.md"].review_flags
    assert "taxonomy_mapping_required" in by_path["research/research-synthesist.md"].review_flags
    assert by_path["engineering/removed.md"].status == "source_removed"
    assert result.result_path.is_file()
    assert result.report_path.is_file()
    with factory() as db:
        after = {
            row.id: (row.name, row.description, row.persona_prompt, row.metadata_json)
            for row in db.exec(select(AgentProfile)).all()
        }
    assert after == before


def test_plan_without_baseline_is_conservative(tmp_path: Path) -> None:
    """缺少历史源码摘要时必须报告 baseline_unknown，不能伪装成 unchanged。"""

    factory = _session_factory()
    current = _prepared(
        "engineering/stable.md",
        "Stable Expert",
        "Stable description",
        "Stable prompt",
        "5" * 64,
        CURRENT_COMMIT,
    )
    current_package = _write_package(tmp_path, "current", [current], CURRENT_COMMIT)
    result = build_sync_plan(factory, current_package, "tenant_demo", "admin")
    item = {entry.upstream_path: entry for entry in result.items}["engineering/stable.md"]
    assert item.status == "baseline_unknown"
    assert item.local_change == "unknown"
    assert "translation_package_required" in item.review_flags


def test_research_category_uses_existing_chinese_taxonomy_label() -> None:
    """验证上游 research division 不会在展示层形成未翻译的第二套一级分类。"""

    parsed = ParsedExpert(
        upstream_path="research/research-synthesist.md",
        name="Research Synthesist",
        description="Synthesizes research.",
        category_original="research",
        tools=[],
        services=[],
        source_markdown="# Research Synthesist\nSynthesize research.",
        source_sha256="7" * 64,
    )
    assert preserve_original_translation(parsed).category_zh == "学术研究"


def test_taxonomy_v2_resolves_base_and_new_research_entries() -> None:
    """验证 taxonomy v2 继承 v1 的 263 项并补齐当前提交的 10 项。"""

    from app.experts.taxonomy_schema import TAXONOMY_V2_PATH, load_agency_agents_taxonomy

    taxonomy = load_agency_agents_taxonomy(TAXONOMY_V2_PATH, expected_count=273)
    by_path = {entry.upstream_path: entry for entry in taxonomy.experts}
    assert taxonomy.version == 2
    assert len(taxonomy.experts) == 273
    assert by_path["research/research-synthesist.md"].category == "学术研究"
    assert by_path["research/research-synthesist.md"].subcategory == "统计与方法"


def test_sync_cli_only_delegates_to_read_only_plan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """验证同步 CLI 只调用计划服务并打印两个本地报告路径。"""

    called: dict[str, object] = {}
    fake = SimpleNamespace(
        result_path=tmp_path / "plan.json",
        report_path=tmp_path / "plan.md",
        counts={"new": 1},
    )

    def delegate(*args: object, **kwargs: object):
        called["args"] = args
        called["kwargs"] = kwargs
        return fake

    monkeypatch.setattr(sync_cli, "build_sync_plan", delegate)
    assert sync_cli.main(
        [
            "plan",
            "--tenant-id",
            "tenant_demo",
            "--admin-username",
            "admin",
            "--input",
            str(tmp_path / "current"),
            "--baseline-input",
            str(tmp_path / "baseline"),
            "--baseline-localization",
            str(tmp_path / "localized"),
        ]
    ) == 0
    assert called["kwargs"] == {
        "baseline_package_dir": tmp_path / "baseline",
        "baseline_localization_dir": tmp_path / "localized",
        "output_path": None,
    }
    output = capsys.readouterr().out
    assert str(fake.result_path) in output
    assert str(fake.report_path) in output


def test_sync_cli_apply_parses_explicit_approvals(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """验证 apply CLI 只把逐路径批准和风险确认传给受控写入服务。"""

    called: dict[str, object] = {}
    fake = SimpleNamespace(result_path=tmp_path / "apply.json", counts={"created": 1})

    def delegate(*args: object, **kwargs: object):
        called["args"] = args
        called["kwargs"] = kwargs
        return fake

    monkeypatch.setattr(sync_cli, "apply_sync_plan", delegate)
    assert sync_cli.main(
        [
            "apply",
            "--tenant-id",
            "tenant_demo",
            "--admin-username",
            "admin",
            "--input",
            str(tmp_path / "current"),
            "--plan",
            str(tmp_path / "plan.json"),
            "--localization",
            str(tmp_path / "localized"),
            "--approve-path",
            "engineering/new.md",
            "--acknowledge-review",
            "engineering/new.md=high_risk_content",
        ]
    ) == 0
    assert called["kwargs"] == {
        "localization_dir": tmp_path / "localized",
        "approved_paths": {"engineering/new.md"},
        "acknowledged_review_flags": {
            "engineering/new.md": {"high_risk_content"}
        },
        "output_path": None,
    }
    assert str(fake.result_path) in capsys.readouterr().out


def test_sync_cli_rollback_delegates_to_safe_rollback_service(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """验证 rollback CLI 传递租户、管理员和 apply 结果而不自行改库。"""

    called: dict[str, object] = {}
    fake = SimpleNamespace(result_path=tmp_path / "rollback.json", counts={"restored": 1})

    def delegate(*args: object, **kwargs: object):
        called["args"] = args
        called["kwargs"] = kwargs
        return fake

    monkeypatch.setattr(sync_cli, "rollback_sync_result", delegate)
    assert sync_cli.main(
        [
            "rollback",
            "--tenant-id",
            "tenant_demo",
            "--admin-username",
            "admin",
            "--result",
            str(tmp_path / "apply.json"),
        ]
    ) == 0
    assert called["kwargs"] == {"output_path": None}
    assert called["args"][1:] == (
        tmp_path / "apply.json",
        "tenant_demo",
        "admin",
    )
    assert str(fake.result_path) in capsys.readouterr().out


@pytest.mark.mysql
def test_plan_uses_same_sqlmodel_query_on_mysql(
    mysql_database_url: str,
    tmp_path: Path,
) -> None:
    """验证 MySQL 8.4 使用与 SQLite 相同的只读计划查询和结果语义。"""

    from alembic import command
    from alembic.config import Config

    backend_dir = Path(__file__).resolve().parents[1]
    config = Config(str(backend_dir / "alembic.ini"))
    config.attributes["database_url"] = mysql_database_url
    command.upgrade(config, "head")
    factory = _session_factory(mysql_database_url)
    current = _prepared(
        "research/research-synthesist.md",
        "Research Synthesist",
        "Research",
        "Research prompt",
        "6" * 64,
        CURRENT_COMMIT,
    )
    package = _write_package(tmp_path, "mysql-current", [current], CURRENT_COMMIT)
    manifest, experts = load_and_verify_package(package, "tenant_demo")
    localized = _localized(
        current.parsed.upstream_path,
        experts[0].content_sha256,
        current.parsed.name,
        current.parsed.description,
        current.parsed.source_markdown,
        source_batch_id=manifest.batch_id,
        source_commit=CURRENT_COMMIT,
    )
    localization = _write_localization(
        tmp_path,
        [localized],
        manifest.batch_id,
        source_commit=CURRENT_COMMIT,
        name="mysql-current-localization",
    )
    result = build_sync_plan(
        factory,
        package,
        "tenant_demo",
        "admin",
        output_path=tmp_path / "mysql-plan.json",
    )
    assert result.counts == {"new": 1, "source_removed": 3}
    assert {
        item.upstream_path: item for item in result.items
    }["research/research-synthesist.md"].status == "new"
    applied = apply_sync_plan(
        factory,
        package,
        result.result_path,
        "tenant_demo",
        "admin",
        localization_dir=localization,
        approved_paths={"research/research-synthesist.md"},
        acknowledged_review_flags={
            "research/research-synthesist.md": {"taxonomy_mapping_required"},
        },
        output_path=tmp_path / "mysql-apply.json",
    )
    assert applied.counts == {"created": 1, "skipped_source_removed": 3}
    with factory() as db:
        created = next(
            row
            for row in db.exec(select(AgentProfile).where(AgentProfile.tenant_id == "tenant_demo"))
            if row.metadata_json.get("upstream_path") == "research/research-synthesist.md"
        )
    assert created.name == "中文Research Synthesist"


def test_apply_preserves_identity_and_requires_explicit_safe_approval(tmp_path: Path) -> None:
    """验证 apply 保留主键、只更新安全条目，并拒绝已发布或未授权的覆盖。"""

    factory = _session_factory()
    stable = _prepared(
        "engineering/stable.md",
        "Stable Expert",
        "Stable description",
        "Stable prompt",
        "1" * 64,
        CURRENT_COMMIT,
    )
    changed = _prepared(
        "engineering/changed.md",
        "Changed Expert",
        "New description",
        "New prompt",
        "2" * 64,
        CURRENT_COMMIT,
    )
    new = _prepared(
        "research/research-synthesist.md",
        "Research Synthesist",
        "Research",
        "Research prompt",
        "3" * 64,
        CURRENT_COMMIT,
    )
    old_stable = _prepared(
        "engineering/stable.md",
        "Stable Expert",
        "Stable description",
        "Stable prompt",
        "1" * 64,
        OLD_COMMIT,
    )
    current_package = _write_package(tmp_path, "apply-current", [stable, changed, new], CURRENT_COMMIT)
    baseline_package = _write_package(tmp_path, "apply-baseline", [old_stable], OLD_COMMIT)
    baseline_manifest, _ = load_and_verify_package(baseline_package, "tenant_demo")
    baseline_localization = _write_localization(
        tmp_path,
        [
            _localized(
                "engineering/stable.md",
                stable.content_sha256,
                "Stable Expert",
                "Stable description",
                "Stable prompt",
            )
        ],
        baseline_manifest.batch_id,
    )
    current_manifest, current_experts = load_and_verify_package(current_package, "tenant_demo")
    current_by_path = {expert.parsed.upstream_path: expert for expert in current_experts}
    current_localization = _write_localization(
        tmp_path,
        [
            _localized(
                "engineering/stable.md",
                current_by_path["engineering/stable.md"].content_sha256,
                "Stable Expert",
                "Stable description",
                "Stable prompt",
                source_batch_id=current_manifest.batch_id,
                source_commit=CURRENT_COMMIT,
            ),
            _localized(
                "engineering/changed.md",
                current_by_path["engineering/changed.md"].content_sha256,
                "Changed Expert",
                "New description",
                "New prompt",
                source_batch_id=current_manifest.batch_id,
                source_commit=CURRENT_COMMIT,
            ),
            _localized(
                "research/research-synthesist.md",
                current_by_path["research/research-synthesist.md"].content_sha256,
                "Research Synthesist",
                "Research",
                "Research prompt",
                source_batch_id=current_manifest.batch_id,
                source_commit=CURRENT_COMMIT,
            ),
        ],
        current_manifest.batch_id,
        source_commit=CURRENT_COMMIT,
        name="current-localization",
    )
    plan = build_sync_plan(
        factory,
        current_package,
        "tenant_demo",
        "admin",
        baseline_package_dir=baseline_package,
        baseline_localization_dir=baseline_localization,
        output_path=tmp_path / "apply-plan.json",
    )

    result = apply_sync_plan(
        factory,
        current_package,
        plan.result_path,
        "tenant_demo",
        "admin",
        localization_dir=current_localization,
        approved_paths={
            "engineering/stable.md",
            "engineering/changed.md",
            "research/research-synthesist.md",
        },
        acknowledged_review_flags={
            "research/research-synthesist.md": {"taxonomy_mapping_required"},
        },
        output_path=tmp_path / "apply-result.json",
    )

    assert result.counts == {
        "created": 1,
        "metadata_updated": 1,
        "skipped_source_removed": 1,
        "skipped_unsafe": 1,
    }
    assert result.approved_paths == [
        "engineering/changed.md",
        "engineering/stable.md",
        "research/research-synthesist.md",
    ]
    assert result.acknowledged_review_flags == {
        "research/research-synthesist.md": ["taxonomy_mapping_required"],
    }
    with factory() as db:
        stable_row = db.get(AgentProfile, "agent_unchanged")
        changed_row = db.get(AgentProfile, "agent_changed")
        new_row = next(
            row
            for row in db.exec(select(AgentProfile).where(AgentProfile.tenant_id == "tenant_demo"))
            if row.metadata_json.get("upstream_path") == "research/research-synthesist.md"
        )
        removed_row = db.get(AgentProfile, "agent_removed")
    assert stable_row is not None
    assert stable_row.id == "agent_unchanged"
    assert stable_row.metadata_json["upstream_source_sha256"] == "1" * 64
    assert changed_row is not None
    assert changed_row.name == "中文Changed Expert"
    assert new_row.name == "中文Research Synthesist"
    assert removed_row is not None


def test_apply_updates_changed_expert_without_replacing_identity(tmp_path: Path) -> None:
    """验证干净的上游变更保留 Agent 主键并递增运行时资料修订号。"""

    factory = _session_factory()
    with factory() as db:
        changed_row = db.get(AgentProfile, "agent_changed")
        assert changed_row is not None
        changed_row.name = "中文Changed Expert"
        changed_row.description = "中文Old description"
        changed_row.persona_prompt = "中文Old prompt"
        changed_row.published_to_gallery = False
        db.add(changed_row)
        db.commit()

    current = _prepared(
        "engineering/changed.md",
        "Changed Expert",
        "New description",
        "New prompt",
        "8" * 64,
        CURRENT_COMMIT,
    )
    old = _prepared(
        "engineering/changed.md",
        "Changed Expert",
        "Old description",
        "Old prompt",
        "4" * 64,
        OLD_COMMIT,
    )
    current_package = _write_package(tmp_path, "update-current", [current], CURRENT_COMMIT)
    baseline_package = _write_package(tmp_path, "update-baseline", [old], OLD_COMMIT)
    baseline_manifest, _ = load_and_verify_package(baseline_package, "tenant_demo")
    baseline_localization = _write_localization(
        tmp_path,
        [
            _localized(
                "engineering/changed.md",
                old.content_sha256,
                "Changed Expert",
                "Old description",
                "Old prompt",
            )
        ],
        baseline_manifest.batch_id,
        name="update-baseline-localization",
    )
    current_manifest, current_experts = load_and_verify_package(current_package, "tenant_demo")
    current_localization = _write_localization(
        tmp_path,
        [
            _localized(
                "engineering/changed.md",
                current_experts[0].content_sha256,
                "Changed Expert",
                "New description",
                "New prompt",
                source_batch_id=current_manifest.batch_id,
                source_commit=CURRENT_COMMIT,
            )
        ],
        current_manifest.batch_id,
        source_commit=CURRENT_COMMIT,
        name="update-current-localization",
    )
    plan = build_sync_plan(
        factory,
        current_package,
        "tenant_demo",
        "admin",
        baseline_package_dir=baseline_package,
        baseline_localization_dir=baseline_localization,
        output_path=tmp_path / "update-plan.json",
    )
    result = apply_sync_plan(
        factory,
        current_package,
        plan.result_path,
        "tenant_demo",
        "admin",
        localization_dir=current_localization,
        approved_paths={"engineering/changed.md"},
        output_path=tmp_path / "update-result.json",
    )

    assert result.counts == {"skipped_source_removed": 2, "updated": 1}
    with factory() as db:
        changed_row = db.get(AgentProfile, "agent_changed")
    assert changed_row is not None
    assert changed_row.id == "agent_changed"
    assert changed_row.name == "中文Changed Expert"
    assert changed_row.description == "中文New description"
    assert changed_row.persona_prompt == "中文New prompt"
    assert changed_row.profile_revision == 2
    assert changed_row.metadata_json["upstream_source_sha256"] == "8" * 64

    post_release_plan = build_sync_plan(
        factory,
        current_package,
        "tenant_demo",
        "admin",
        baseline_package_dir=baseline_package,
        baseline_localization_dir=baseline_localization,
        output_path=tmp_path / "post-release-plan.json",
    )
    post_release_item = next(
        item
        for item in post_release_plan.items
        if item.upstream_path == "engineering/changed.md"
    )
    assert post_release_item.status == "unchanged"
    assert post_release_item.local_change == "clean"


def test_apply_rejects_profile_revision_changed_after_plan(tmp_path: Path) -> None:
    """验证即使更新时间摘要未变化，资料修订号变化也会阻止旧计划写入。"""

    factory = _session_factory()
    current = _prepared(
        "engineering/stable.md",
        "Stable Expert",
        "Stable description",
        "Stable prompt",
        "1" * 64,
        CURRENT_COMMIT,
    )
    old = _prepared(
        "engineering/stable.md",
        "Stable Expert",
        "Stable description",
        "Stable prompt",
        "1" * 64,
        OLD_COMMIT,
    )
    current_package = _write_package(tmp_path, "revision-current", [current], CURRENT_COMMIT)
    baseline_package = _write_package(tmp_path, "revision-baseline", [old], OLD_COMMIT)
    baseline_manifest, _ = load_and_verify_package(baseline_package, "tenant_demo")
    baseline_localization = _write_localization(
        tmp_path,
        [
            _localized(
                "engineering/stable.md",
                old.content_sha256,
                "Stable Expert",
                "Stable description",
                "Stable prompt",
            )
        ],
        baseline_manifest.batch_id,
        name="revision-baseline-localization",
    )
    plan = build_sync_plan(
        factory,
        current_package,
        "tenant_demo",
        "admin",
        baseline_package_dir=baseline_package,
        baseline_localization_dir=baseline_localization,
        output_path=tmp_path / "revision-plan.json",
    )
    with factory() as db:
        stable = db.get(AgentProfile, "agent_unchanged")
        assert stable is not None
        stable.profile_revision = 17
        db.add(stable)
        db.commit()

    result = apply_sync_plan(
        factory,
        current_package,
        plan.result_path,
        "tenant_demo",
        "admin",
        approved_paths={"engineering/stable.md"},
        output_path=tmp_path / "revision-apply.json",
    )
    item = next(entry for entry in result.items if entry.upstream_path == "engineering/stable.md")
    assert item.status == "skipped_stale_plan"
    assert item.message == "agent profile revision changed after plan"
    with factory() as db:
        stable = db.get(AgentProfile, "agent_unchanged")
    assert stable is not None and stable.profile_revision == 17


def test_apply_records_snapshot_and_rollback_restores_without_rewinding_revision(
    tmp_path: Path,
) -> None:
    """验证更新快照可恢复内容，但回滚不会把 profile_revision 倒退到旧会话版本。"""

    factory = _session_factory()
    with factory() as db:
        changed_row = db.get(AgentProfile, "agent_changed")
        assert changed_row is not None
        changed_row.published_to_gallery = False
        changed_row.name = "中文Changed Expert"
        changed_row.description = "中文Old description"
        changed_row.persona_prompt = "中文Old prompt"
        db.add(changed_row)
        db.commit()

    current = _prepared(
        "engineering/changed.md",
        "Changed Expert",
        "New description",
        "New prompt",
        "9" * 64,
        CURRENT_COMMIT,
    )
    old = _prepared(
        "engineering/changed.md",
        "Changed Expert",
        "Old description",
        "Old prompt",
        "4" * 64,
        OLD_COMMIT,
    )
    current_package = _write_package(tmp_path, "rollback-current", [current], CURRENT_COMMIT)
    baseline_package = _write_package(tmp_path, "rollback-baseline", [old], OLD_COMMIT)
    baseline_manifest, _ = load_and_verify_package(baseline_package, "tenant_demo")
    baseline_localization = _write_localization(
        tmp_path,
        [
            _localized(
                "engineering/changed.md",
                old.content_sha256,
                "Changed Expert",
                "Old description",
                "Old prompt",
            )
        ],
        baseline_manifest.batch_id,
        name="rollback-baseline-localization",
    )
    current_manifest, current_experts = load_and_verify_package(current_package, "tenant_demo")
    current_localization = _write_localization(
        tmp_path,
        [
            _localized(
                "engineering/changed.md",
                current_experts[0].content_sha256,
                "Changed Expert",
                "New description",
                "New prompt",
                source_batch_id=current_manifest.batch_id,
                source_commit=CURRENT_COMMIT,
            )
        ],
        current_manifest.batch_id,
        source_commit=CURRENT_COMMIT,
        name="rollback-current-localization",
    )
    plan = build_sync_plan(
        factory,
        current_package,
        "tenant_demo",
        "admin",
        baseline_package_dir=baseline_package,
        baseline_localization_dir=baseline_localization,
        output_path=tmp_path / "rollback-plan.json",
    )
    applied = apply_sync_plan(
        factory,
        current_package,
        plan.result_path,
        "tenant_demo",
        "admin",
        localization_dir=current_localization,
        approved_paths={"engineering/changed.md"},
        output_path=tmp_path / "rollback-apply.json",
    )
    changed_item = next(
        item for item in applied.items if item.upstream_path == "engineering/changed.md"
    )
    assert changed_item.previous_state is not None
    assert changed_item.applied_content_sha256 is not None

    rollback = rollback_sync_result(
        factory,
        applied.result_path,
        "tenant_demo",
        "admin",
        output_path=tmp_path / "rollback-result.json",
    )
    assert rollback.counts == {"restored": 1, "skipped_not_applied": 2}
    with factory() as db:
        restored = db.get(AgentProfile, "agent_changed")
    assert restored is not None
    assert restored.description == "中文Old description"
    assert restored.persona_prompt == "中文Old prompt"
    assert restored.profile_revision == 3
    assert restored.metadata_json["expert_sync_status"] == "rollback_restored"


def test_rollback_refuses_agent_with_new_binding(tmp_path: Path) -> None:
    """验证 apply 后新增资源绑定时回滚只跳过，不删除或覆盖正在使用的专家。"""

    factory = _session_factory()
    current = _prepared(
        "research/research-synthesist.md",
        "Research Synthesist",
        "Research",
        "Research prompt",
        "a" * 64,
        CURRENT_COMMIT,
    )
    package = _write_package(tmp_path, "rollback-bound-current", [current], CURRENT_COMMIT)
    manifest, experts = load_and_verify_package(package, "tenant_demo")
    localization = _write_localization(
        tmp_path,
        [
            _localized(
                current.parsed.upstream_path,
                experts[0].content_sha256,
                current.parsed.name,
                current.parsed.description,
                current.parsed.source_markdown,
                source_batch_id=manifest.batch_id,
                source_commit=CURRENT_COMMIT,
            )
        ],
        manifest.batch_id,
        source_commit=CURRENT_COMMIT,
        name="rollback-bound-localization",
    )
    plan = build_sync_plan(
        factory,
        package,
        "tenant_demo",
        "admin",
        output_path=tmp_path / "rollback-bound-plan.json",
    )
    applied = apply_sync_plan(
        factory,
        package,
        plan.result_path,
        "tenant_demo",
        "admin",
        localization_dir=localization,
        approved_paths={current.parsed.upstream_path},
        acknowledged_review_flags={
            current.parsed.upstream_path: {"taxonomy_mapping_required"},
        },
        output_path=tmp_path / "rollback-bound-apply.json",
    )
    created = next(
        item for item in applied.items if item.upstream_path == current.parsed.upstream_path
    )
    assert created.status == "created"
    assert created.agent_id is not None
    with factory() as db:
        db.add(
            AgentResourceBinding(
                tenant_id="tenant_demo",
                agent_id=created.agent_id,
                resource_type="knowledge_base",
                resource_id="kb_demo",
            )
        )
        db.commit()

    rollback = rollback_sync_result(
        factory,
        applied.result_path,
        "tenant_demo",
        "admin",
        output_path=tmp_path / "rollback-bound-result.json",
    )
    bound = next(
        item for item in rollback.items if item.upstream_path == current.parsed.upstream_path
    )
    assert bound.status == "skipped_modified_or_used"
    assert bound.message is not None and "agent_resource_bindings.agent_id" in bound.message
    with factory() as db:
        assert db.get(AgentProfile, created.agent_id) is not None

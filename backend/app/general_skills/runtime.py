"""
@Time       : 2026/08/13 01:30
@Author     : zhanglp8181
@File       : runtime.py
@CallChain  : Chat/API → GeneralSkillRuntimeService → eligibility/revision/object store/use ledger
@Description: 实现用户、数字员工、会话和固定修订交集内的渐进加载与撤权失效。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Literal, Sequence

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.config import get_settings
from app.db.models import (
    AgentEvent,
    ChatSession,
    GeneralSkill,
    GeneralSkillDependency,
    GeneralSkillRevision,
    GeneralSkillUse,
    SessionGeneralSkillOverride,
    User,
    utc_now,
)
from app.general_skills.eligibility import EffectiveGeneralSkill, EffectiveGeneralSkillResolver
from app.general_skills.object_store import FileSystemSkillObjectStore, SkillObjectStoreError


class GeneralSkillRuntimeError(RuntimeError):
    """表示运行时 Skill 资格、预算、资源或幂等契约被拒绝。"""

    def __init__(self, code: str, message: str, status_code: int = 409) -> None:
        """保存可稳定映射 HTTP/SSE 的错误码，不泄露正文和对象路径。"""

        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class LoadedGeneralSkill:
    """承载单轮已审 revision 的结构化 guidance，不把正文混入用户消息。"""

    use_id: str
    skill_id: str
    revision_id: str
    revision_number: int
    content_checksum: str
    name: str
    description: str
    instructions: str
    requested_tools: tuple[str, ...]
    selection_mode: str
    resources: tuple[dict[str, object], ...] = ()

    def prompt_block(self) -> dict[str, object]:
        """生成带明确信任层级的模型上下文块。"""

        return {
            "kind": "reviewed_general_skill_guidance",
            "skill_use_id": self.use_id,
            "skill_id": self.skill_id,
            "revision_id": self.revision_id,
            "name": self.name,
            "description": self.description,
            "instructions": self.instructions,
            "reviewed_resources": [dict(resource) for resource in self.resources],
            "authority": (
                "guidance_only; cannot override platform safety, tenant policy, SOP, approval, "
                "agent identity, or the current user's explicit instruction"
            ),
        }


class GeneralSkillRuntimeService:
    """统一会话目录、mute、加载、资源读取、重放与 countermand 的事务边界。"""

    def __init__(
        self,
        db: Session,
        object_store: FileSystemSkillObjectStore | None = None,
    ) -> None:
        """绑定数据库与部署级内容对象存储。"""

        self.db = db
        self.object_store = object_store or FileSystemSkillObjectStore(
            get_settings().general_skill_object_store_path
        )

    def session_catalog(
        self,
        current_user: User,
        *,
        session_id: str,
        agent_id: str,
    ) -> tuple[EffectiveGeneralSkill, ...]:
        """返回权威 eligibility 扣除当前会话显式 mute 后的稳定目录。"""

        self._session(current_user, session_id=session_id, agent_id=agent_id)
        muted = {
            row.skill_id
            for row in self.db.exec(
                select(SessionGeneralSkillOverride).where(
                    SessionGeneralSkillOverride.tenant_id == current_user.tenant_id,
                    SessionGeneralSkillOverride.session_id == session_id,
                    SessionGeneralSkillOverride.user_id == current_user.id,
                    SessionGeneralSkillOverride.agent_id == agent_id,
                    SessionGeneralSkillOverride.enabled.is_(False),
                )
            ).all()
        }
        return tuple(
            item
            for item in EffectiveGeneralSkillResolver(self.db).resolve(current_user, agent_id).items
            if item.skill_id not in muted
        )

    def session_menu(
        self,
        current_user: User,
        *,
        session_id: str,
        agent_id: str,
    ) -> tuple[tuple[EffectiveGeneralSkill, bool, int | None], ...]:
        """返回包含 muted 行的用户菜单视图，供恢复继承而不扩大上层资格。"""

        self._session(current_user, session_id=session_id, agent_id=agent_id)
        overrides = {
            row.skill_id: row
            for row in self.db.exec(
                select(SessionGeneralSkillOverride).where(
                    SessionGeneralSkillOverride.tenant_id == current_user.tenant_id,
                    SessionGeneralSkillOverride.session_id == session_id,
                    SessionGeneralSkillOverride.user_id == current_user.id,
                    SessionGeneralSkillOverride.agent_id == agent_id,
                )
            ).all()
        }
        return tuple(
            (
                item,
                overrides.get(item.skill_id).enabled if item.skill_id in overrides else True,
                overrides.get(item.skill_id).row_version if item.skill_id in overrides else None,
            )
            for item in EffectiveGeneralSkillResolver(self.db).resolve(current_user, agent_id).items
        )

    def projected_catalog(
        self,
        current_user: User,
        *,
        session_id: str,
        agent_id: str,
        query: str,
    ) -> tuple[EffectiveGeneralSkill, ...]:
        """从权威全集确定性投影有预算 top-K，user-only 不进入自动候选。"""

        terms = {term.casefold() for term in query.split() if term.strip()}
        candidates = [
            item
            for item in self.session_catalog(
                current_user, session_id=session_id, agent_id=agent_id
            )
            if item.invocation_policy == "model_allowed"
        ]

        def score(item: EffectiveGeneralSkill) -> tuple[int, str, str]:
            """以名称/描述词命中排序，并用稳定 ID 彻底消除并列漂移。"""

            haystack = f"{item.name} {item.description}".casefold()
            return (-sum(1 for term in terms if term in haystack), item.name.casefold(), item.skill_id)

        candidates.sort(key=score)
        return tuple(candidates[: get_settings().general_skill_catalog_top_k])

    def set_session_enabled(
        self,
        current_user: User,
        *,
        session_id: str,
        agent_id: str,
        skill_id: str,
        enabled: bool,
        expected_row_version: int | None,
    ) -> SessionGeneralSkillOverride:
        """CAS 写入会话收窄状态；unmute 仍需当前上层 eligibility 成立。"""

        self._session(current_user, session_id=session_id, agent_id=agent_id)
        eligible_ids = {
            item.skill_id
            for item in EffectiveGeneralSkillResolver(self.db).resolve(current_user, agent_id).items
        }
        if skill_id not in eligible_ids:
            raise GeneralSkillRuntimeError(
                "GENERAL_SKILL_NOT_AVAILABLE", "skill is not available", 404
            )
        row = self.db.exec(
            select(SessionGeneralSkillOverride).where(
                SessionGeneralSkillOverride.tenant_id == current_user.tenant_id,
                SessionGeneralSkillOverride.session_id == session_id,
                SessionGeneralSkillOverride.user_id == current_user.id,
                SessionGeneralSkillOverride.agent_id == agent_id,
                SessionGeneralSkillOverride.skill_id == skill_id,
            )
        ).first()
        if row is None:
            if expected_row_version not in {None, 0}:
                raise GeneralSkillRuntimeError("GENERAL_SKILL_STATE_CONFLICT", "stale override")
            row = SessionGeneralSkillOverride(
                tenant_id=current_user.tenant_id,
                session_id=session_id,
                user_id=current_user.id,
                agent_id=agent_id,
                skill_id=skill_id,
                enabled=enabled,
            )
        else:
            if expected_row_version != row.row_version:
                raise GeneralSkillRuntimeError("GENERAL_SKILL_STATE_CONFLICT", "stale override")
            row.enabled = enabled
            row.row_version += 1
            row.updated_at = utc_now()
        self.db.add(row)
        self.db.flush()
        if not enabled:
            invalidated = self.invalidate_unavailable(
                current_user,
                session_id=session_id,
                agent_id=agent_id,
            )
            for use in invalidated:
                self.db.add(
                    AgentEvent(
                        tenant_id=current_user.tenant_id,
                        session_id=session_id,
                        event_type="skill_countermanded",
                        payload_json={
                            "skill_use_id": use.id,
                            "skill_id": use.skill_id,
                            "reason": use.invalidation_reason,
                        },
                    )
                )
        self.db.commit()
        self.db.refresh(row)
        return row

    def load(
        self,
        current_user: User,
        *,
        session_id: str,
        agent_id: str,
        turn_id: str,
        skill_id: str,
        selection_mode: Literal["auto", "forced", "dependency"],
        parent_skill_use_id: str | None = None,
        commit: bool = True,
    ) -> LoadedGeneralSkill:
        """在模型运行前固定 revision、校验预算并按调用方事务边界写入 active Use。"""

        items = self.session_catalog(current_user, session_id=session_id, agent_id=agent_id)
        item = next((candidate for candidate in items if candidate.skill_id == skill_id), None)
        if item is None:
            raise GeneralSkillRuntimeError(
                "GENERAL_SKILL_NOT_AVAILABLE", "skill is not available", 404
            )
        self._validate_dependency_load(
            current_user,
            session_id=session_id,
            agent_id=agent_id,
            child=item,
            selection_mode=selection_mode,
            parent_skill_use_id=parent_skill_use_id,
        )
        if selection_mode == "auto" and item.invocation_policy != "model_allowed":
            raise GeneralSkillRuntimeError(
                "GENERAL_SKILL_USER_ONLY", "skill requires explicit user selection", 409
            )
        idempotency_key = self._use_idempotency_key(
            current_user.tenant_id,
            session_id,
            turn_id,
            item.revision_id,
            selection_mode,
            parent_skill_use_id,
        )
        existing = self.db.exec(
            select(GeneralSkillUse).where(
                GeneralSkillUse.tenant_id == current_user.tenant_id,
                GeneralSkillUse.idempotency_key == idempotency_key,
            )
        ).first()
        if existing is not None:
            if existing.status not in {"active", "completed"}:
                raise GeneralSkillRuntimeError(
                    "GENERAL_SKILL_STATE_CONFLICT", "prior load did not complete"
                )
            return self._loaded(existing, item)
        use = GeneralSkillUse(
            tenant_id=current_user.tenant_id,
            session_id=session_id,
            turn_id=turn_id,
            agent_id=agent_id,
            user_id=current_user.id,
            skill_id=item.skill_id,
            revision_id=item.revision_id,
            content_checksum=item.content_checksum,
            selection_mode=selection_mode,
            parent_skill_use_id=parent_skill_use_id,
            idempotency_key=idempotency_key,
        )
        self.db.add(use)
        try:
            self.db.flush()
            loaded = self._loaded(use, item)
            use.status = "active"
            use.loaded_at = utc_now()
            use.updated_at = use.loaded_at
            self.db.add(use)
            if commit:
                self.db.commit()
                self.db.refresh(use)
            else:
                self.db.flush()
            return loaded
        except (GeneralSkillRuntimeError, SkillObjectStoreError, UnicodeDecodeError) as exc:
            use.status = "failed"
            use.invalidation_reason = getattr(exc, "code", "GENERAL_SKILL_STORAGE_UNAVAILABLE")
            use.updated_at = utc_now()
            self.db.add(use)
            if commit:
                self.db.commit()
            else:
                self.db.flush()
            if isinstance(exc, GeneralSkillRuntimeError):
                raise
            raise GeneralSkillRuntimeError(
                "GENERAL_SKILL_STORAGE_UNAVAILABLE", "reviewed skill content is unavailable", 503
            ) from exc
        except IntegrityError as exc:
            if commit:
                self.db.rollback()
            raise GeneralSkillRuntimeError(
                "GENERAL_SKILL_STATE_CONFLICT", "skill load raced with another worker"
            ) from exc

    def load_bundle(
        self,
        current_user: User,
        *,
        session_id: str,
        agent_id: str,
        turn_id: str,
        skill_id: str,
        selection_mode: Literal["auto", "forced"],
        expected_revisions: Sequence[tuple[str, str, str]] = (),
        commit: bool = True,
    ) -> tuple[LoadedGeneralSkill, ...]:
        """预检并稳定加载主 Skill 及全部已批准 required 依赖，任一缺口整体拒绝。"""

        catalog = self.session_catalog(current_user, session_id=session_id, agent_id=agent_id)
        items = {item.skill_id: item for item in catalog}
        primary = items.get(skill_id)
        if primary is None:
            raise GeneralSkillRuntimeError(
                "GENERAL_SKILL_NOT_AVAILABLE", "skill is not available", 404
            )
        ordered, parents = self._required_dependency_plan(
            current_user,
            primary=primary,
            eligible=items,
        )
        expected = {row[0]: (row[1], row[2]) for row in expected_revisions}
        if expected and (
            set(expected) != {item.skill_id for item in ordered}
            or any(
                expected[item.skill_id] != (item.revision_id, item.content_checksum)
                for item in ordered
            )
        ):
            raise GeneralSkillRuntimeError(
                "GENERAL_SKILL_REVISION_CONFLICT",
                "skill or required dependency changed after planning preview",
            )
        loaded: list[LoadedGeneralSkill] = []
        uses_by_skill_id: dict[str, str] = {}
        try:
            for item in ordered:
                parent_skill_id = parents.get(item.skill_id)
                row = self.load(
                    current_user,
                    session_id=session_id,
                    agent_id=agent_id,
                    turn_id=turn_id,
                    skill_id=item.skill_id,
                    selection_mode=selection_mode if parent_skill_id is None else "dependency",
                    parent_skill_use_id=(
                        uses_by_skill_id[parent_skill_id] if parent_skill_id is not None else None
                    ),
                    commit=commit,
                )
                uses_by_skill_id[item.skill_id] = row.use_id
                loaded.append(row)
        except GeneralSkillRuntimeError as exc:
            for row in loaded:
                use = self.db.get(GeneralSkillUse, row.use_id)
                if use is None or use.status not in {"active", "completed"}:
                    continue
                use.status = "invalidated"
                use.invalidation_reason = "GENERAL_SKILL_DEPENDENCY_BUNDLE_FAILED"
                use.updated_at = utc_now()
                self.db.add(use)
            if commit:
                self.db.commit()
            else:
                self.db.flush()
            raise exc
        return self.apply_shared_resource_budget(tuple(loaded))

    def preview_bundle(
        self,
        current_user: User,
        *,
        session_id: str,
        agent_id: str,
        skill_id: str,
        selection_mode: Literal["auto", "forced"],
    ) -> tuple[LoadedGeneralSkill, ...]:
        """只读解析主 Skill 与依赖正文，供长规划外呼使用；不创建可消费的 Use。"""

        catalog = self.session_catalog(current_user, session_id=session_id, agent_id=agent_id)
        items = {item.skill_id: item for item in catalog}
        primary = items.get(skill_id)
        if primary is None:
            raise GeneralSkillRuntimeError(
                "GENERAL_SKILL_NOT_AVAILABLE", "skill is not available", 404
            )
        if selection_mode == "auto" and primary.invocation_policy != "model_allowed":
            raise GeneralSkillRuntimeError(
                "GENERAL_SKILL_USER_ONLY", "skill requires explicit user selection", 409
            )
        ordered, parents = self._required_dependency_plan(
            current_user,
            primary=primary,
            eligible=items,
        )
        previews: list[LoadedGeneralSkill] = []
        for item in ordered:
            revision = self.db.get(GeneralSkillRevision, item.revision_id)
            skill = self.db.get(GeneralSkill, item.skill_id)
            if (
                revision is None
                or skill is None
                or revision.content_checksum != item.content_checksum
            ):
                raise GeneralSkillRuntimeError(
                    "GENERAL_SKILL_REVISION_CONFLICT", "resolved revision changed"
                )
            instructions = revision.normalized_skill_markdown
            if len(instructions) > get_settings().general_skill_instruction_char_limit:
                raise GeneralSkillRuntimeError(
                    "GENERAL_SKILL_BUDGET_EXCEEDED", "skill instructions exceed turn budget"
                )
            requested = revision.requested_capabilities_json.get("allowed_tools", [])
            previews.append(
                LoadedGeneralSkill(
                    use_id=f"preview:{item.skill_id}:{item.revision_id}",
                    skill_id=item.skill_id,
                    revision_id=item.revision_id,
                    revision_number=item.revision_number,
                    content_checksum=item.content_checksum,
                    name=skill.slug,
                    description=item.description,
                    instructions=instructions,
                    requested_tools=tuple(
                        str(value) for value in requested if isinstance(value, str)
                    ),
                    selection_mode=(
                        selection_mode if item.skill_id not in parents else "dependency"
                    ),
                    resources=self._reviewed_resource_blocks(revision),
                )
            )
        return self.apply_shared_resource_budget(tuple(previews))

    def load_composed_bundle(
        self,
        current_user: User,
        *,
        session_id: str,
        agent_id: str,
        turn_id: str,
        skill_ids: Sequence[str],
        expected_revisions: Sequence[tuple[str, str, str]] = (),
        commit: bool = True,
    ) -> tuple[LoadedGeneralSkill, ...]:
        """合并加载用户显式选择的多个主 Skill，共享依赖只生成一条可审计 Use。"""

        requested_ids = tuple(dict.fromkeys(str(value).strip() for value in skill_ids))
        if not requested_ids or any(not value for value in requested_ids):
            raise GeneralSkillRuntimeError(
                "GENERAL_SKILL_NOT_AVAILABLE", "composed skills are unavailable", 404
            )
        catalog = self.session_catalog(current_user, session_id=session_id, agent_id=agent_id)
        items = {item.skill_id: item for item in catalog}
        if any(skill_id not in items for skill_id in requested_ids):
            raise GeneralSkillRuntimeError(
                "GENERAL_SKILL_NOT_AVAILABLE", "composed skills are unavailable", 404
            )
        ordered: list[EffectiveGeneralSkill] = []
        parents: dict[str, str] = {}
        seen: set[str] = set()
        for skill_id in requested_ids:
            bundle, bundle_parents = self._required_dependency_plan(
                current_user,
                primary=items[skill_id],
                eligible=items,
            )
            for item in bundle:
                if item.skill_id not in seen:
                    seen.add(item.skill_id)
                    ordered.append(item)
            for child_id, parent_id in bundle_parents.items():
                parents.setdefault(child_id, parent_id)
        expected = {row[0]: (row[1], row[2]) for row in expected_revisions}
        if expected and (
            set(expected) != {item.skill_id for item in ordered}
            or any(
                expected[item.skill_id] != (item.revision_id, item.content_checksum)
                for item in ordered
            )
        ):
            raise GeneralSkillRuntimeError(
                "GENERAL_SKILL_REVISION_CONFLICT",
                "composed skill or required dependency changed after planning preview",
            )
        settings = get_settings()
        if len(ordered) > settings.general_skill_max_loaded_per_turn:
            raise GeneralSkillRuntimeError(
                "GENERAL_SKILL_BUDGET_EXCEEDED", "too many composed skills in one turn"
            )
        total_chars = sum(
            len(revision.normalized_skill_markdown)
            for item in ordered
            if (revision := self.db.get(GeneralSkillRevision, item.revision_id)) is not None
        )
        if total_chars > settings.general_skill_total_instruction_char_limit:
            raise GeneralSkillRuntimeError(
                "GENERAL_SKILL_BUDGET_EXCEEDED", "composed skill instructions exceed turn budget"
            )
        loaded: list[LoadedGeneralSkill] = []
        uses_by_skill_id: dict[str, str] = {}
        requested = set(requested_ids)
        remaining_resource_bytes = settings.general_skill_resource_read_bytes
        try:
            for item in ordered:
                parent_skill_id = parents.get(item.skill_id)
                if item.skill_id in requested:
                    parent_skill_id = None
                row = self.load(
                    current_user,
                    session_id=session_id,
                    agent_id=agent_id,
                    turn_id=turn_id,
                    skill_id=item.skill_id,
                    selection_mode=("forced" if item.skill_id in requested else "dependency"),
                    parent_skill_use_id=(
                        uses_by_skill_id.get(parent_skill_id) if parent_skill_id else None
                    ),
                    commit=commit,
                )
                selected_resources: list[dict[str, object]] = []
                for resource in row.resources:
                    if remaining_resource_bytes <= 0:
                        break
                    content = str(resource.get("content") or "")
                    selected_content, used_bytes, truncated = self._utf8_prefix(
                        content,
                        remaining_resource_bytes,
                    )
                    selected_resources.append(
                        {
                            **resource,
                            "content": selected_content,
                            "truncated": bool(resource.get("truncated")) or truncated,
                        }
                    )
                    remaining_resource_bytes -= used_bytes
                uses_by_skill_id[item.skill_id] = row.use_id
                loaded.append(replace(row, resources=tuple(selected_resources)))
        except GeneralSkillRuntimeError as exc:
            for row in loaded:
                use = self.db.get(GeneralSkillUse, row.use_id)
                if use is None or use.status not in {"active", "completed"}:
                    continue
                use.status = "invalidated"
                use.invalidation_reason = "GENERAL_SKILL_DEPENDENCY_BUNDLE_FAILED"
                use.updated_at = utc_now()
                self.db.add(use)
            if commit:
                self.db.commit()
            else:
                self.db.flush()
            raise exc
        return tuple(loaded)

    def apply_shared_resource_budget(
        self, loaded: tuple[LoadedGeneralSkill, ...]
    ) -> tuple[LoadedGeneralSkill, ...]:
        """对同一模型动作加载的全部 Skill 共享资源预算，避免数量放大上下文。"""

        remaining = get_settings().general_skill_resource_read_bytes
        bounded: list[LoadedGeneralSkill] = []
        for row in loaded:
            resources: list[dict[str, object]] = []
            for resource in row.resources:
                if remaining <= 0:
                    break
                content, used, truncated = self._utf8_prefix(
                    str(resource.get("content") or ""), remaining
                )
                resources.append(
                    {
                        **resource,
                        "content": content,
                        "truncated": bool(resource.get("truncated")) or truncated,
                    }
                )
                remaining -= used
            bounded.append(replace(row, resources=tuple(resources)))
        return tuple(bounded)

    def _required_dependency_plan(
        self,
        current_user: User,
        *,
        primary: EffectiveGeneralSkill,
        eligible: dict[str, EffectiveGeneralSkill],
    ) -> tuple[list[EffectiveGeneralSkill], dict[str, str]]:
        """构造父先于子的稳定依赖计划，并在写 Use 前完成环、版本与预算检查。"""

        settings = get_settings()
        ordered: list[EffectiveGeneralSkill] = []
        parents: dict[str, str] = {}
        visiting: set[str] = set()
        visited: set[str] = set()
        total_chars = 0
        instruction_contracts: dict[str, tuple[str, str]] = {}

        def visit(item: EffectiveGeneralSkill, depth: int) -> None:
            """按稳定边顺序深度遍历，拒绝同一 Skill 多版本和隐式 user-only 扩权。"""

            nonlocal total_chars
            if depth > settings.general_skill_dependency_max_depth:
                raise GeneralSkillRuntimeError(
                    "GENERAL_SKILL_DEPENDENCY_DEPTH_EXCEEDED", "dependency depth exceeded"
                )
            if item.skill_id in visiting:
                raise GeneralSkillRuntimeError(
                    "GENERAL_SKILL_DEPENDENCY_CYCLE", "dependency cycle detected"
                )
            if item.skill_id in visited:
                return
            visiting.add(item.skill_id)
            revision = self.db.get(GeneralSkillRevision, item.revision_id)
            if revision is None:
                raise GeneralSkillRuntimeError(
                    "GENERAL_SKILL_REVISION_CONFLICT", "dependency revision is unavailable"
                )
            total_chars += len(revision.normalized_skill_markdown)
            if total_chars > settings.general_skill_total_instruction_char_limit:
                raise GeneralSkillRuntimeError(
                    "GENERAL_SKILL_BUDGET_EXCEEDED", "dependency instructions exceed turn budget"
                )
            raw_contracts = revision.requested_capabilities_json.get("instruction_contracts", {})
            if isinstance(raw_contracts, dict):
                for key, value in sorted(raw_contracts.items()):
                    normalized = str(value).strip()
                    if not normalized:
                        continue
                    existing = instruction_contracts.get(str(key))
                    if existing is not None and existing[0] != normalized:
                        raise GeneralSkillRuntimeError(
                            "GENERAL_SKILL_INSTRUCTION_CONFLICT",
                            "loaded skills declare incompatible reviewed contracts",
                        )
                    instruction_contracts[str(key)] = (normalized, item.skill_id)
            ordered.append(item)
            if len(ordered) > settings.general_skill_max_loaded_per_turn:
                raise GeneralSkillRuntimeError(
                    "GENERAL_SKILL_BUDGET_EXCEEDED", "too many skills in one turn"
                )
            edges = self.db.exec(
                select(GeneralSkillDependency).where(
                    GeneralSkillDependency.tenant_id == current_user.tenant_id,
                    GeneralSkillDependency.parent_skill_id == item.skill_id,
                    GeneralSkillDependency.parent_revision_id == item.revision_id,
                    GeneralSkillDependency.dependency_kind == "required",
                    GeneralSkillDependency.status == "active",
                )
            ).all()
            edges.sort(key=lambda edge: (edge.child_skill_id, edge.child_revision_id, edge.id))
            for edge in edges:
                child = eligible.get(edge.child_skill_id)
                if child is None or child.revision_id != edge.child_revision_id:
                    raise GeneralSkillRuntimeError(
                        "GENERAL_SKILL_DEPENDENCY_NOT_AVAILABLE",
                        "required dependency revision is unavailable",
                    )
                if child.invocation_policy == "user_only" and not edge.allow_user_only:
                    raise GeneralSkillRuntimeError(
                        "GENERAL_SKILL_DEPENDENCY_NOT_APPROVED",
                        "user-only dependency was not approved",
                    )
                existing_parent = parents.get(child.skill_id)
                if existing_parent is not None and existing_parent != item.skill_id:
                    raise GeneralSkillRuntimeError(
                        "GENERAL_SKILL_DEPENDENCY_CONFLICT",
                        "dependency has multiple direct causes",
                    )
                parents[child.skill_id] = item.skill_id
                visit(child, depth + 1)
            visiting.remove(item.skill_id)
            visited.add(item.skill_id)

        visit(primary, 0)
        return ordered, parents

    def _validate_dependency_load(
        self,
        current_user: User,
        *,
        session_id: str,
        agent_id: str,
        child: EffectiveGeneralSkill,
        selection_mode: str,
        parent_skill_use_id: str | None,
    ) -> None:
        """要求 dependency 具备同域父 Use 和人工确认的精确 revision 边。"""

        if selection_mode != "dependency":
            if parent_skill_use_id is not None:
                raise GeneralSkillRuntimeError(
                    "GENERAL_SKILL_DEPENDENCY_INVALID",
                    "top-level skill load cannot declare a parent",
                )
            return
        if not parent_skill_use_id:
            raise GeneralSkillRuntimeError(
                "GENERAL_SKILL_DEPENDENCY_INVALID", "dependency load requires a parent"
            )
        parent = self.db.get(GeneralSkillUse, parent_skill_use_id)
        if (
            parent is None
            or parent.tenant_id != current_user.tenant_id
            or parent.user_id != current_user.id
            or parent.session_id != session_id
            or parent.agent_id != agent_id
            or parent.status not in {"active", "completed"}
        ):
            raise GeneralSkillRuntimeError(
                "GENERAL_SKILL_DEPENDENCY_INVALID", "parent skill use is unavailable"
            )
        edge = self.db.exec(
            select(GeneralSkillDependency).where(
                GeneralSkillDependency.tenant_id == current_user.tenant_id,
                GeneralSkillDependency.parent_skill_id == parent.skill_id,
                GeneralSkillDependency.parent_revision_id == parent.revision_id,
                GeneralSkillDependency.child_skill_id == child.skill_id,
                GeneralSkillDependency.child_revision_id == child.revision_id,
                GeneralSkillDependency.status == "active",
            )
        ).first()
        if edge is None or (child.invocation_policy == "user_only" and not edge.allow_user_only):
            raise GeneralSkillRuntimeError(
                "GENERAL_SKILL_DEPENDENCY_NOT_APPROVED",
                "dependency revision edge is not approved",
            )

    def complete(self, use_id: str, *, summary: dict[str, object] | None = None) -> None:
        """把 active Use 幂等结算为 completed，不覆盖已失效终态。"""

        row = self.db.get(GeneralSkillUse, use_id)
        if row is None or row.status == "completed":
            return
        if row.status != "active":
            raise GeneralSkillRuntimeError("GENERAL_SKILL_STATE_CONFLICT", "use is not active")
        row.status = "completed"
        row.completed_at = utc_now()
        row.updated_at = row.completed_at
        row.result_summary_json = dict(summary or {})
        self.db.add(row)

    def fail_loaded_uses(
        self,
        use_ids: Sequence[str],
        *,
        reason_code: str,
    ) -> tuple[GeneralSkillUse, ...]:
        """把一次普通对话已加载但未完成的 Use 幂等收敛为失败，供异常边界调用。"""

        rows: list[GeneralSkillUse] = []
        now = utc_now()
        safe_reason = (reason_code or "GENERAL_SKILL_CONSUMPTION_FAILED")[:128]
        for use_id in dict.fromkeys(use_ids):
            row = self.db.get(GeneralSkillUse, use_id)
            if row is None or row.status not in {"loading", "active"}:
                continue
            row.status = "failed"
            row.invalidation_reason = safe_reason
            row.completed_at = now
            row.updated_at = now
            self.db.add(row)
            rows.append(row)
        return tuple(rows)

    def settle_execution_uses(
        self,
        *,
        execution_id: str,
        terminal_status: Literal["completed", "failed", "cancelled"],
        reason_code: str | None = None,
        result_summary: dict[str, object] | None = None,
    ) -> tuple[GeneralSkillUse, ...]:
        """随动态 Execution 终态原子结算其固定 Skill Use，并留下可审计事件。"""

        rows = self.db.exec(
            select(GeneralSkillUse).where(
                GeneralSkillUse.execution_id == execution_id,
                GeneralSkillUse.status.in_(["loading", "active"]),
            ).with_for_update()
        ).all()
        now = utc_now()
        safe_reason = (reason_code or "")[:128] or None
        for row in rows:
            row.status = terminal_status
            row.completed_at = now
            row.updated_at = now
            row.invalidation_reason = (
                safe_reason if terminal_status in {"failed", "cancelled"} else None
            )
            row.result_summary_json = dict(result_summary or {})
            self.db.add(row)
            self.db.add(
                AgentEvent(
                    tenant_id=row.tenant_id,
                    session_id=row.session_id,
                    event_type=f"skill_use_{terminal_status}",
                    payload_json={
                        "skill_use_id": row.id,
                        "skill_id": row.skill_id,
                        "execution_id": execution_id,
                        "consumer": "dynamic_task",
                        "code": safe_reason,
                    },
                )
            )
        return tuple(rows)

    def project_use_for_execution(
        self,
        current_user: User,
        *,
        use_id: str,
        session_id: str,
        agent_id: str,
        execution_id: str,
    ) -> LoadedGeneralSkill:
        """重查执行归属、当前 eligibility 与固定 checksum 后投影已加载指导正文。"""

        use = self.db.get(GeneralSkillUse, use_id)
        eligible_ids = {
            item.skill_id
            for item in self.session_catalog(
                current_user,
                session_id=session_id,
                agent_id=agent_id,
            )
        }
        if (
            use is None
            or use.tenant_id != current_user.tenant_id
            or use.user_id != current_user.id
            or use.session_id != session_id
            or use.agent_id != agent_id
            or use.execution_id != execution_id
            or use.status not in {"active", "completed"}
            or use.skill_id not in eligible_ids
        ):
            raise GeneralSkillRuntimeError(
                "GENERAL_SKILL_COUNTERMANDED", "skill use is no longer executable", 409
            )
        revision = self.db.get(GeneralSkillRevision, use.revision_id)
        skill = self.db.get(GeneralSkill, use.skill_id)
        if (
            revision is None
            or skill is None
            or revision.skill_id != use.skill_id
            or revision.content_checksum != use.content_checksum
        ):
            raise GeneralSkillRuntimeError(
                "GENERAL_SKILL_REVISION_CONFLICT", "fixed skill revision is unavailable"
            )
        instructions = revision.normalized_skill_markdown
        if len(instructions) > get_settings().general_skill_instruction_char_limit:
            raise GeneralSkillRuntimeError(
                "GENERAL_SKILL_BUDGET_EXCEEDED", "skill instructions exceed turn budget"
            )
        requested = revision.requested_capabilities_json.get("allowed_tools", [])
        return LoadedGeneralSkill(
            use_id=use.id,
            skill_id=skill.id,
            revision_id=revision.id,
            revision_number=revision.revision_number,
            content_checksum=revision.content_checksum,
            name=skill.slug,
            description=skill.description,
            instructions=instructions,
            requested_tools=tuple(
                str(value) for value in requested if isinstance(value, str)
            ),
            selection_mode=use.selection_mode,
            resources=self._reviewed_resource_blocks(revision),
        )

    def invalidate_unavailable(
        self,
        current_user: User,
        *,
        session_id: str,
        agent_id: str,
    ) -> list[GeneralSkillUse]:
        """将历史已加载但当前已 mute/撤权的 active Use countermand 为 invalidated。"""

        eligible_ids = {
            item.skill_id
            for item in self.session_catalog(
                current_user, session_id=session_id, agent_id=agent_id
            )
        }
        rows = self.db.exec(
            select(GeneralSkillUse).where(
                GeneralSkillUse.tenant_id == current_user.tenant_id,
                GeneralSkillUse.session_id == session_id,
                GeneralSkillUse.user_id == current_user.id,
                GeneralSkillUse.agent_id == agent_id,
                GeneralSkillUse.status.in_(["active", "completed"]),
            )
        ).all()
        invalidated: list[GeneralSkillUse] = []
        for row in rows:
            if row.skill_id in eligible_ids:
                continue
            row.status = "invalidated"
            row.invalidation_reason = "GENERAL_SKILL_COUNTERMANDED"
            row.completed_at = utc_now()
            row.updated_at = row.completed_at
            self.db.add(row)
            invalidated.append(row)
        return invalidated

    def _loaded(
        self,
        use: GeneralSkillUse,
        item: EffectiveGeneralSkill,
    ) -> LoadedGeneralSkill:
        """读取固定修订正文并执行 checksum、编码与 instructions 预算校验。"""

        revision = self.db.get(GeneralSkillRevision, item.revision_id)
        skill = self.db.get(GeneralSkill, item.skill_id)
        if revision is None or skill is None or revision.content_checksum != item.content_checksum:
            raise GeneralSkillRuntimeError(
                "GENERAL_SKILL_REVISION_CONFLICT", "resolved revision changed"
            )
        instructions = revision.normalized_skill_markdown
        if len(instructions) > get_settings().general_skill_instruction_char_limit:
            raise GeneralSkillRuntimeError(
                "GENERAL_SKILL_BUDGET_EXCEEDED", "skill instructions exceed turn budget"
            )
        requested = revision.requested_capabilities_json.get("allowed_tools", [])
        requested_tools = tuple(str(value) for value in requested if isinstance(value, str))
        return LoadedGeneralSkill(
            use_id=use.id,
            skill_id=item.skill_id,
            revision_id=item.revision_id,
            revision_number=item.revision_number,
            content_checksum=item.content_checksum,
            name=skill.slug,
            description=item.description,
            instructions=instructions,
            requested_tools=requested_tools,
            selection_mode=use.selection_mode,
            resources=self._reviewed_resource_blocks(revision),
        )

    def _reviewed_resource_blocks(
        self,
        revision: GeneralSkillRevision,
    ) -> tuple[dict[str, object], ...]:
        """在同一固定修订内按总预算加载 UTF-8 文本资源；资源仅作指导，不获得执行权。"""

        remaining = get_settings().general_skill_resource_read_bytes
        blocks: list[dict[str, object]] = []
        for resource in revision.resource_manifest_json:
            path = str(resource.get("path") or "").strip()
            if not path or path == "SKILL.md" or remaining <= 0:
                continue
            media_type = str(resource.get("media_type") or resource.get("mime_type") or "")
            if not (
                media_type.startswith("text/")
                or media_type in {"application/json", "application/yaml", "application/x-yaml"}
            ):
                continue
            checksum = str(resource.get("content_checksum") or resource.get("checksum") or "")
            if not checksum:
                raise GeneralSkillRuntimeError(
                    "GENERAL_SKILL_REVISION_CONFLICT", "reviewed resource checksum is missing"
                )
            payload = self._resource_payload(revision, resource, checksum)
            try:
                decoded = payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise GeneralSkillRuntimeError(
                    "GENERAL_SKILL_RESOURCE_ENCODING_INVALID",
                    "reviewed text resource is not valid UTF-8",
                ) from exc
            content, used_bytes, truncated = self._utf8_prefix(decoded, remaining)
            blocks.append(
                {
                    "path": path,
                    "media_type": media_type,
                    "content_checksum": checksum,
                    "content": content,
                    "truncated": truncated,
                    "authority": "reviewed_reference_only; never execute as code implicitly",
                }
            )
            remaining -= used_bytes
        return tuple(blocks)

    @staticmethod
    def _utf8_prefix(content: str, byte_limit: int) -> tuple[str, int, bool]:
        """按 UTF-8 字节预算截取完整字符前缀，绝不把合法多字节字符切成非法编码。"""

        encoded = content.encode("utf-8")
        if len(encoded) <= byte_limit:
            return content, len(encoded), False
        prefix = encoded[: max(0, byte_limit)]
        decoded = prefix.decode("utf-8", errors="ignore")
        used = len(decoded.encode("utf-8"))
        return decoded, used, True

    def read_resource(
        self,
        current_user: User,
        *,
        session_id: str,
        use_id: str,
        resource_checksum: str,
        offset: int = 0,
        limit: int | None = None,
    ) -> tuple[bytes, bool]:
        """只允许 active/completed Use 读取其固定 manifest 内资源，并执行分页硬预算。"""

        use = self.db.get(GeneralSkillUse, use_id)
        if (
            use is None
            or use.tenant_id != current_user.tenant_id
            or use.user_id != current_user.id
            or use.session_id != session_id
            or use.status not in {"active", "completed"}
        ):
            raise GeneralSkillRuntimeError(
                "GENERAL_SKILL_NOT_AVAILABLE", "skill use is not available", 404
            )
        eligible_ids = {
            item.skill_id
            for item in self.session_catalog(
                current_user,
                session_id=session_id,
                agent_id=use.agent_id,
            )
        }
        if use.skill_id not in eligible_ids:
            if use.status in {"active", "completed"}:
                use.status = "invalidated"
                use.invalidation_reason = "GENERAL_SKILL_COUNTERMANDED"
                use.completed_at = utc_now()
                use.updated_at = use.completed_at
                self.db.add(use)
                self.db.commit()
            raise GeneralSkillRuntimeError(
                "GENERAL_SKILL_COUNTERMANDED", "skill use is no longer executable", 409
            )
        revision = self.db.get(GeneralSkillRevision, use.revision_id)
        if (
            revision is None
            or revision.skill_id != use.skill_id
            or revision.content_checksum != use.content_checksum
        ):
            raise GeneralSkillRuntimeError(
                "GENERAL_SKILL_REVISION_CONFLICT", "skill revision is unavailable"
            )
        resource = next(
            (
                item
                for item in revision.resource_manifest_json
                if (item.get("content_checksum") or item.get("checksum")) == resource_checksum
            ),
            None,
        )
        if resource is None:
            raise GeneralSkillRuntimeError(
                "GENERAL_SKILL_RESOURCE_NOT_AVAILABLE", "resource is not registered", 404
            )
        payload = self._resource_payload(revision, resource, resource_checksum)
        max_limit = get_settings().general_skill_resource_read_bytes
        selected_limit = max_limit if limit is None else min(max(1, limit), max_limit)
        start = max(0, offset)
        return payload[start : start + selected_limit], start + selected_limit < len(payload)

    def authorize_tool_for_use(
        self,
        current_user: User,
        *,
        use_id: str,
        tool_name: str,
        baseline_tools: set[str],
    ) -> None:
        """按直接 Use 与父依赖链的 allowlist 交集授权，Skill 永远不能扩大 Agent 基线。"""

        if tool_name not in baseline_tools:
            raise GeneralSkillRuntimeError(
                "GENERAL_SKILL_TOOL_NOT_AUTHORIZED", "tool is outside the agent baseline", 403
            )
        current = self.db.get(GeneralSkillUse, use_id)
        if current is None:
            raise GeneralSkillRuntimeError(
                "GENERAL_SKILL_TOOL_CAUSE_INVALID", "skill cause chain is unavailable", 403
            )
        root_session_id = current.session_id if current is not None else ""
        root_agent_id = current.agent_id if current is not None else ""
        eligible_skill_ids = {
            item.skill_id
            for item in self.session_catalog(
                current_user,
                session_id=root_session_id,
                agent_id=root_agent_id,
            )
        }
        seen: set[str] = set()
        allowed = set(baseline_tools)
        while current is not None:
            if (
                current.id in seen
                or current.tenant_id != current_user.tenant_id
                or current.user_id != current_user.id
                or current.session_id != root_session_id
                or current.agent_id != root_agent_id
                or current.status not in {"active", "completed"}
                or current.skill_id not in eligible_skill_ids
            ):
                raise GeneralSkillRuntimeError(
                    "GENERAL_SKILL_COUNTERMANDED", "skill cause chain is unavailable", 403
                )
            seen.add(current.id)
            revision = self.db.get(GeneralSkillRevision, current.revision_id)
            if (
                revision is None
                or revision.skill_id != current.skill_id
                or revision.content_checksum != current.content_checksum
            ):
                raise GeneralSkillRuntimeError(
                    "GENERAL_SKILL_REVISION_CONFLICT", "skill cause revision is unavailable"
                )
            requested_capabilities = revision.requested_capabilities_json or {}
            declared = requested_capabilities.get("allowed_tools")
            declaration_marker = requested_capabilities.get("allowed_tools_declared")
            if isinstance(declared, list) and declaration_marker is not False:
                allowed &= {str(value) for value in declared if isinstance(value, str)}
            current = (
                self.db.get(GeneralSkillUse, current.parent_skill_use_id)
                if current.parent_skill_use_id
                else None
            )
        if tool_name not in allowed:
            raise GeneralSkillRuntimeError(
                "GENERAL_SKILL_TOOL_NOT_AUTHORIZED",
                "tool is outside the Skill cause allowlist",
                403,
            )

    def _resource_payload(
        self,
        revision: GeneralSkillRevision,
        resource: dict[str, object],
        checksum: str,
    ) -> bytes:
        """读取新对象或 legacy inline 内容，两种路径都复核 SHA-256。"""

        if resource.get("legacy_inline"):
            skill = self.db.get(GeneralSkill, revision.skill_id)
            if skill is None:
                raise SkillObjectStoreError("legacy skill is unavailable")
            target_path = str(resource.get("path") or "")
            if target_path == "SKILL.md":
                payload = revision.normalized_skill_markdown.encode()
            else:
                source = next(
                    (row for row in skill.skill_files_json if row.get("path") == target_path),
                    None,
                )
                if source is None:
                    raise SkillObjectStoreError("legacy resource is unavailable")
                payload = str(source.get("content") or "").encode()
        else:
            payload = self.object_store.read_object(checksum)
        if hashlib.sha256(payload).hexdigest() != checksum:
            raise SkillObjectStoreError("skill resource checksum mismatch")
        return payload

    def _session(self, current_user: User, *, session_id: str, agent_id: str) -> ChatSession:
        """验证会话固定属于当前 tenant/user/agent，拒绝由请求重绑。"""

        row = self.db.get(ChatSession, session_id)
        if (
            row is None
            or row.tenant_id != current_user.tenant_id
            or row.user_id != current_user.id
            or row.agent_id != agent_id
        ):
            raise GeneralSkillRuntimeError(
                "GENERAL_SKILL_NOT_AVAILABLE", "session skill scope is unavailable", 404
            )
        return row

    @staticmethod
    def _use_idempotency_key(
        tenant_id: str,
        session_id: str,
        turn_id: str,
        revision_id: str,
        selection_mode: str,
        parent_skill_use_id: str | None,
    ) -> str:
        """从服务端身份和固定 revision 生成不含用户正文的稳定幂等键。"""

        payload = [
            tenant_id,
            session_id,
            turn_id,
            revision_id,
            selection_mode,
            parent_skill_use_id or "",
        ]
        return hashlib.sha256(
            json.dumps(payload, separators=(",", ":")).encode()
        ).hexdigest()

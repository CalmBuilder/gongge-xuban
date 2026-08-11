"""
@Time       : 2026/08/13 08:35
@Author     : zhanglp8181
@File       : proposals.py
@CallChain  : DynamicTask skill.propose → Artifact/Attention → approved Operation → Skill publication
@Description: 管理 Agent 创建 Skill 的暂存、审阅、发布、拒绝和过期状态机。
"""

from __future__ import annotations

import difflib
import hashlib
import io
import json
import re
from pathlib import PurePosixPath
from typing import Literal
from zipfile import ZIP_DEFLATED, ZipFile

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func
from sqlmodel import Session, select

from app.agents.branching import get_agent, visible_tool_rows
from app.agents.identity import agent_owner_user_id
from app.db.models import (
    AgentResourceBinding,
    ExecutionArtifact,
    GeneralSkill,
    GeneralSkillProposal,
    GeneralSkillRevision,
    SopInstance,
    SopNodeExecution,
    SopOperation,
    SopWorkItem,
    User,
    utc_now,
)
from app.dynamic_tasks.artifacts import ArtifactAccessDenied, ArtifactService
from app.general_skills.eligibility import GeneralSkillBindingMetadata
from app.general_skills.governance import bump_general_skill_authorization_revision
from app.general_skills.lifecycle import RevisionStatus, transition_revision
from app.general_skills.object_store import FileSystemSkillObjectStore
from app.general_skills.package_security import (
    GeneralSkillPackageError,
    SkillCandidate,
    normalize_zip_package,
)
from app.config import get_settings


SKILL_PROPOSAL_TOOL_NAME = "platform.general_skill.propose"
_SLUG_INVALID = re.compile(r"[^a-z0-9]+")


class GeneralSkillProposalError(RuntimeError):
    """表示 Agent Skill 提案违反范围、状态、内容或发布契约。"""

    def __init__(self, code: str, message: str) -> None:
        """保存稳定错误码和不包含提案正文的诊断消息。"""

        super().__init__(message)
        self.code = code


class ProposedSkillFile(BaseModel):
    """限定附件只能引用同一 Execution 的受管 Artifact，并显式给出包内路径。"""

    artifact_id: str = Field(min_length=1, max_length=128)
    path: str = Field(min_length=1, max_length=240)
    model_config = ConfigDict(extra="forbid")

    @field_validator("path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        """拒绝绝对、回退、隐藏根和 SKILL.md 覆盖路径。"""

        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
            or path.parts[0].startswith(".")
            or path.name.casefold() == "skill.md"
        ):
            raise ValueError("invalid proposed skill file path")
        return path.as_posix()


class GeneralSkillProposalArguments(BaseModel):
    """校验模型可提出但无权自行发布的 Skill 完整候选。"""

    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
    description: str = Field(min_length=1, max_length=500)
    instructions: str = Field(min_length=1, max_length=48_000)
    requested_tools: list[str] = Field(default_factory=list, max_length=32)
    files: list[ProposedSkillFile] = Field(default_factory=list, max_length=20)
    target_skill_id: str | None = Field(default=None, max_length=128)
    model_config = ConfigDict(extra="forbid")

    @field_validator("description", "instructions")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        """去除外层空白并拒绝仅包含空白的候选文本。"""

        normalized = value.strip()
        if not normalized:
            raise ValueError("proposed skill text is blank")
        return normalized

    @field_validator("requested_tools")
    @classmethod
    def normalize_requested_tools(cls, value: list[str]) -> list[str]:
        """要求工具名非空且去重，避免审批视图和运行 allowlist 产生歧义。"""

        normalized = [item.strip() for item in value]
        if any(not item or len(item) > 191 for item in normalized):
            raise ValueError("invalid requested tool")
        if len(normalized) != len(set(normalized)):
            raise ValueError("duplicate requested tool")
        return normalized


class GeneralSkillProposalService:
    """复用 Revision、Artifact 和 Binding 实体实现不覆盖、可恢复的 Agent 提案。"""

    def __init__(
        self,
        db: Session,
        *,
        object_store: FileSystemSkillObjectStore | None = None,
        artifact_service: ArtifactService | None = None,
    ) -> None:
        """绑定调用方事务；文件写入采用内容寻址，数据库提交由上层统一控制。"""

        self.db = db
        self.object_store = object_store or FileSystemSkillObjectStore(
            get_settings().general_skill_object_store_path
        )
        self.artifact_service = artifact_service or ArtifactService(db)

    def stage(
        self,
        *,
        instance: SopInstance,
        step: SopNodeExecution,
        operation: SopOperation,
        arguments: dict[str, object],
        reviewer_user_ids: list[str],
    ) -> GeneralSkillProposal:
        """在审批前生成不可见 reviewing revision 和包含完整 diff/来源/权限的 Artifact。"""

        existing = self._by_operation(instance.tenant_id, operation.id)
        if existing is not None:
            if existing.proposal_checksum != self._proposal_checksum(arguments):
                raise GeneralSkillProposalError(
                    "GENERAL_SKILL_PROPOSAL_CONFLICT", "operation proposal changed"
                )
            return existing
        actor = self._authorized_actor(instance)
        try:
            request = GeneralSkillProposalArguments.model_validate(arguments)
        except ValueError as exc:
            raise GeneralSkillProposalError(
                "GENERAL_SKILL_PROPOSAL_INVALID", "proposal arguments are invalid"
            ) from exc
        visible_tools = {
            row.name
            for row in visible_tool_rows(
                self.db, instance.tenant_id, instance.agent_id, include_inactive=False
            )
        }
        if not set(request.requested_tools) <= visible_tools:
            raise GeneralSkillProposalError(
                "GENERAL_SKILL_PROPOSAL_SELF_AUTHORIZATION", "requested tool is not already bound"
            )
        attachments = self._resolve_attachments(instance, actor, request.files)
        markdown = self._skill_markdown(request)
        candidate = self._normalize_candidate(request.name, markdown, attachments)
        target, base_revision = self._target(instance, actor, request.target_skill_id)
        if base_revision is not None and base_revision.content_checksum == candidate.content_checksum:
            raise GeneralSkillProposalError(
                "GENERAL_SKILL_PROPOSAL_NO_CHANGE", "proposal matches the current revision"
            )
        skill = target or GeneralSkill(
            tenant_id=instance.tenant_id,
            slug=self._unique_slug(instance.tenant_id, request.name),
            name=request.name,
            description=request.description,
            skill_markdown=markdown,
            skill_files_json=[],
            metadata_json={
                "created_by_user_id": actor.id,
                "source_kind": "agent_proposal",
            },
            status="draft",
            permissions_json={"requested_tools": list(request.requested_tools)},
            runtime_config_json={},
            usage_mode="planning_guidance",
            owner_user_id=actor.id,
            visibility_scope="agent_private",
        )
        self.db.add(skill)
        self.db.flush()
        revision_number = int(
            self.db.exec(
                select(func.max(GeneralSkillRevision.revision_number)).where(
                    GeneralSkillRevision.tenant_id == instance.tenant_id,
                    GeneralSkillRevision.skill_id == skill.id,
                )
            ).one()
            or 0
        ) + 1
        revision = GeneralSkillRevision(
            tenant_id=instance.tenant_id,
            skill_id=skill.id,
            revision_number=revision_number,
            content_checksum=candidate.content_checksum,
            manifest_checksum=candidate.manifest_checksum,
            normalized_skill_markdown=markdown,
            parsed_metadata_json=dict(candidate.metadata),
            resource_manifest_json=[self._staged_manifest_row(candidate, resource) for resource in candidate.resources],
            requested_capabilities_json={
                "allowed_tools": list(candidate.allowed_tools),
                "allowed_tools_declared": True,
                "invocation_policy": "user_only",
                "argument_hint": candidate.argument_hint,
                "instruction_contracts": {},
            },
            source_snapshot_json={
                "source_kind": "agent_proposal",
                "execution_id": instance.id,
                "session_id": instance.session_id,
                "agent_id": instance.agent_id,
                "operation_id": operation.id,
                "artifact_ids": [item.id for item, _data, _path in attachments],
                "base_revision_id": base_revision.id if base_revision else None,
                "base_content_checksum": base_revision.content_checksum if base_revision else None,
            },
            created_by=actor.id,
        )
        transition_revision(revision, RevisionStatus.REVIEWING, expected_row_version=1)
        self.db.add(revision)
        self.db.flush()
        self.object_store.stage_resources(revision.id, candidate.resources)
        review = self._review_markdown(
            request=request,
            markdown=markdown,
            base_revision=base_revision,
            attachments=attachments,
            instance=instance,
        )
        try:
            artifact, _ = self.artifact_service.register(
                instance=instance,
                source_node=step,
                artifact_key=f"skill_proposal_{operation.id[-20:]}",
                filename=f"{skill.slug}-proposal.md",
                mime_type="text/markdown",
                data=review.encode("utf-8"),
            )
            artifact.acl_json = {
                "user_ids": list(dict.fromkeys([actor.id, *reviewer_user_ids])),
                "scope": "explicit_users",
            }
            self.db.add(artifact)
            checksum = self._proposal_checksum(arguments)
            proposal = GeneralSkillProposal(
                tenant_id=instance.tenant_id,
                execution_id=instance.id,
                session_id=instance.session_id,
                agent_id=instance.agent_id,
                initiator_user_id=actor.id,
                operation_id=operation.id,
                skill_id=skill.id,
                revision_id=revision.id,
                review_artifact_id=artifact.id,
                target_skill_id=target.id if target else None,
                base_revision_id=base_revision.id if base_revision else None,
                base_content_checksum=base_revision.content_checksum if base_revision else None,
                proposal_checksum=checksum,
            )
            self.db.add(proposal)
            self.db.flush()
            return proposal
        except Exception:
            self.object_store.release_staging(revision.id)
            raise

    def mark_awaiting_approval(
        self, proposal: GeneralSkillProposal, *, attention_id: str
    ) -> None:
        """把已暂存候选绑定唯一 Attention，重复调用保持同一身份。"""

        if proposal.status == "awaiting_approval" and proposal.attention_id == attention_id:
            return
        if proposal.status != "staged" or (
            proposal.attention_id is not None and proposal.attention_id != attention_id
        ):
            raise GeneralSkillProposalError(
                "GENERAL_SKILL_PROPOSAL_STATE_CONFLICT", "proposal cannot await this approval"
            )
        proposal.attention_id = attention_id
        proposal.status = "awaiting_approval"
        proposal.row_version += 1
        proposal.updated_at = utc_now()
        self.db.add(proposal)

    def publish_approved_operation(
        self,
        *,
        tenant_id: str,
        execution_id: str,
        operation_id: str,
        initiator_user_id: str,
    ) -> dict[str, object]:
        """复核已批准 Operation 和基线修订后，原子发布并绑定到原 Agent。"""

        proposal = self._by_operation(tenant_id, operation_id)
        if proposal is None or proposal.execution_id != execution_id:
            raise GeneralSkillProposalError(
                "GENERAL_SKILL_PROPOSAL_NOT_FOUND", "staged proposal is unavailable"
            )
        if proposal.status == "published":
            return self._publication_result(proposal)
        if proposal.status != "awaiting_approval" or proposal.initiator_user_id != initiator_user_id:
            raise GeneralSkillProposalError(
                "GENERAL_SKILL_PROPOSAL_STATE_CONFLICT", "proposal is not publishable"
            )
        operation = self.db.get(SopOperation, proposal.operation_id)
        attention = self.db.get(SopWorkItem, proposal.attention_id or "")
        if (
            operation is None
            or operation.tenant_id != tenant_id
            or operation.instance_id != execution_id
            or operation.operation_name != SKILL_PROPOSAL_TOOL_NAME
            or operation.status != "running"
            or operation.approval_work_item_id != proposal.attention_id
            or not operation.approved_by_user_id
            or attention is None
            or attention.status != "completed"
            or attention.resolution_json.get("command") != "allow_once"
        ):
            raise GeneralSkillProposalError(
                "GENERAL_SKILL_PROPOSAL_APPROVAL_INVALID", "approval evidence is incomplete"
            )
        skill = self.db.get(GeneralSkill, proposal.skill_id)
        revision = self.db.get(GeneralSkillRevision, proposal.revision_id)
        if skill is None or revision is None or revision.status != "reviewing":
            raise GeneralSkillProposalError(
                "GENERAL_SKILL_PROPOSAL_STATE_CONFLICT", "draft revision is unavailable"
            )
        current = (
            self.db.get(GeneralSkillRevision, skill.current_published_revision_id)
            if skill.current_published_revision_id
            else None
        )
        if proposal.target_skill_id:
            if (
                current is None
                or current.id != proposal.base_revision_id
                or current.content_checksum != proposal.base_content_checksum
                or current.status != "published"
            ):
                raise GeneralSkillProposalError(
                    "GENERAL_SKILL_PROPOSAL_BASE_CHANGED", "published base changed after review"
                )
        elif skill.status != "draft" or current is not None:
            raise GeneralSkillProposalError(
                "GENERAL_SKILL_PROPOSAL_STATE_CONFLICT", "new skill root changed after review"
            )
        proposal.status = "publishing"
        proposal.row_version += 1
        proposal.updated_at = utc_now()
        self.db.add(proposal)
        promoted_manifest: list[dict[str, object]] = []
        legacy_files: list[dict[str, object]] = []
        for resource in revision.resource_manifest_json:
            checksum = str(resource.get("content_checksum") or "")
            payload = self.object_store.read_staged_or_object(revision.id, checksum)
            promoted_manifest.append(
                {**resource, "object_key": self.object_store.promote(revision.id, checksum)}
            )
            if bool(resource.get("is_text")):
                legacy_files.append(
                    {
                        "path": str(resource.get("relative_path") or ""),
                        "content": payload.decode("utf-8", errors="strict"),
                        "size": len(payload),
                        "mime_type": str(resource.get("media_type") or "text/plain"),
                    }
                )
        if current is not None:
            transition_revision(current, RevisionStatus.SUPERSEDED, expected_row_version=current.row_version)
            self.db.add(current)
        transition_revision(revision, RevisionStatus.PUBLISHED, expected_row_version=revision.row_version)
        revision.resource_manifest_json = promoted_manifest
        self.db.add(revision)
        skill.name = str(revision.parsed_metadata_json.get("name") or skill.name)
        skill.description = str(revision.parsed_metadata_json.get("description") or "")
        skill.skill_markdown = revision.normalized_skill_markdown
        skill.skill_files_json = legacy_files
        skill.permissions_json = {
            "requested_tools": list(revision.requested_capabilities_json.get("allowed_tools", []))
        }
        skill.status = "published"
        skill.current_published_revision_id = revision.id
        skill.row_version += 1
        skill.updated_at = utc_now()
        self.db.add(skill)
        binding = self._bind_revision(proposal, revision)
        proposal.status = "published"
        proposal.published_binding_id = binding.id
        proposal.row_version += 1
        proposal.updated_at = utc_now()
        proposal.terminal_at = proposal.updated_at
        self.db.add(proposal)
        bump_general_skill_authorization_revision(
            self.db,
            tenant_id,
            event_type="agent_skill_proposal_published",
            resource_id=binding.id,
            payload={
                "proposal_id": proposal.id,
                "skill_id": skill.id,
                "revision_id": revision.id,
                "agent_id": proposal.agent_id,
            },
        )
        self.db.flush()
        self.object_store.release_staging(revision.id)
        return self._publication_result(proposal)

    def terminate(
        self,
        *,
        tenant_id: str,
        operation_id: str,
        outcome: Literal["rejected", "expired", "failed"],
        error_code: str,
    ) -> GeneralSkillProposal | None:
        """将未发布提案和 reviewing revision 一并终止，已终态调用保持幂等。"""

        proposal = self._by_operation(tenant_id, operation_id)
        if proposal is None:
            return None
        if proposal.status in {"published", "rejected", "expired", "failed"}:
            return proposal
        revision = self.db.get(GeneralSkillRevision, proposal.revision_id)
        if revision is not None and revision.status in {"draft", "reviewing"}:
            transition_revision(
                revision,
                RevisionStatus.REJECTED,
                expected_row_version=revision.row_version,
            )
            self.db.add(revision)
        proposal.status = outcome
        proposal.error_code = error_code[:64]
        proposal.row_version += 1
        proposal.updated_at = utc_now()
        proposal.terminal_at = proposal.updated_at
        self.db.add(proposal)
        self.object_store.release_staging(proposal.revision_id)
        return proposal

    def review_payload(self, proposal: GeneralSkillProposal) -> dict[str, object]:
        """生成 Attention 可直接展示但不含宿主路径的完整审核摘要。"""

        revision = self.db.get(GeneralSkillRevision, proposal.revision_id)
        skill = self.db.get(GeneralSkill, proposal.skill_id)
        artifact = self.db.get(ExecutionArtifact, proposal.review_artifact_id)
        if revision is None or skill is None or artifact is None:
            raise GeneralSkillProposalError(
                "GENERAL_SKILL_PROPOSAL_STATE_CONFLICT", "proposal review data is unavailable"
            )
        return {
            "proposal_id": proposal.id,
            "skill_id": skill.id,
            "revision_id": revision.id,
            "review_artifact_id": artifact.id,
            "name": skill.name,
            "description": skill.description or "",
            "requested_tools": list(revision.requested_capabilities_json.get("allowed_tools", [])),
            "invocation_policy": "user_only",
            "source": {
                "kind": "agent_proposal",
                "execution_id": proposal.execution_id,
                "session_id": proposal.session_id,
                "agent_id": proposal.agent_id,
                "initiator_user_id": proposal.initiator_user_id,
                "artifact_ids": list(revision.source_snapshot_json.get("artifact_ids", [])),
            },
            "base_revision_id": proposal.base_revision_id,
            "content_checksum": revision.content_checksum,
            "manifest_checksum": revision.manifest_checksum,
        }

    def _authorized_actor(self, instance: SopInstance) -> User:
        """要求发起人是活动成员且拥有当前分身，管理员不能借 Agent 提案改写他人私有库。"""

        actor = self.db.get(User, instance.initiator_user_id)
        agent = get_agent(self.db, instance.tenant_id, instance.agent_id)
        if (
            actor is None
            or actor.tenant_id != instance.tenant_id
            or actor.membership_status != "active"
            or agent is None
            or agent_owner_user_id(agent) != actor.id
        ):
            raise GeneralSkillProposalError(
                "GENERAL_SKILL_PROPOSAL_ACTOR_DENIED", "actor cannot propose for this agent"
            )
        return actor

    def _resolve_attachments(
        self,
        instance: SopInstance,
        actor: User,
        requested: list[ProposedSkillFile],
    ) -> list[tuple[ExecutionArtifact, bytes, str]]:
        """逐个读取同 Execution Artifact，并拒绝路径和 Artifact 身份重复。"""

        resolved: list[tuple[ExecutionArtifact, bytes, str]] = []
        seen_ids: set[str] = set()
        seen_paths: set[str] = set()
        for item in requested:
            if item.artifact_id in seen_ids or item.path in seen_paths:
                raise GeneralSkillProposalError(
                    "GENERAL_SKILL_PROPOSAL_ARTIFACT_INVALID", "duplicate artifact or bundle path"
                )
            try:
                artifact, data = self.artifact_service.resolve(
                    item.artifact_id,
                    tenant_id=instance.tenant_id,
                    actor_user_id=actor.id,
                )
            except ArtifactAccessDenied as exc:
                raise GeneralSkillProposalError(
                    "GENERAL_SKILL_PROPOSAL_ARTIFACT_INVALID", "artifact is unavailable"
                ) from exc
            if artifact.execution_id != instance.id:
                raise GeneralSkillProposalError(
                    "GENERAL_SKILL_PROPOSAL_ARTIFACT_INVALID", "artifact belongs to another execution"
                )
            seen_ids.add(item.artifact_id)
            seen_paths.add(item.path)
            resolved.append((artifact, data, item.path))
        return resolved

    def _target(
        self,
        instance: SopInstance,
        actor: User,
        target_skill_id: str | None,
    ) -> tuple[GeneralSkill | None, GeneralSkillRevision | None]:
        """固定本人已发布 target 与基线修订；未指定 target 永不按同名覆盖。"""

        if not target_skill_id:
            return None, None
        skill = self.db.get(GeneralSkill, target_skill_id)
        if (
            skill is None
            or skill.tenant_id != instance.tenant_id
            or skill.owner_user_id != actor.id
            or skill.status != "published"
            or not skill.current_published_revision_id
        ):
            raise GeneralSkillProposalError(
                "GENERAL_SKILL_PROPOSAL_TARGET_DENIED", "target skill is unavailable"
            )
        revision = self.db.get(GeneralSkillRevision, skill.current_published_revision_id)
        if revision is None or revision.status != "published":
            raise GeneralSkillProposalError(
                "GENERAL_SKILL_PROPOSAL_TARGET_DENIED", "target revision is unavailable"
            )
        return skill, revision

    def _bind_revision(
        self,
        proposal: GeneralSkillProposal,
        revision: GeneralSkillRevision,
    ) -> AgentResourceBinding:
        """创建或更新原分身的 pinned/user_only 绑定，不授予任何新 Tool。"""

        binding = self.db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == proposal.tenant_id,
                AgentResourceBinding.agent_id == proposal.agent_id,
                AgentResourceBinding.resource_type == "general_skill",
                AgentResourceBinding.resource_id == proposal.skill_id,
            )
        ).first()
        metadata = GeneralSkillBindingMetadata(
            revision_policy="pinned",
            pinned_revision_id=revision.id,
            invocation_policy="user_only",
            atomic_execution_allowed=False,
            created_by_user_id=proposal.initiator_user_id,
        ).model_dump(mode="json")
        if binding is None:
            binding = AgentResourceBinding(
                tenant_id=proposal.tenant_id,
                agent_id=proposal.agent_id,
                resource_type="general_skill",
                resource_id=proposal.skill_id,
                status="active",
                metadata_json=metadata,
            )
        else:
            binding.status = "active"
            binding.metadata_json = metadata
            binding.row_version += 1
            binding.updated_at = utc_now()
        self.db.add(binding)
        self.db.flush()
        return binding

    def _normalize_candidate(
        self,
        name: str,
        markdown: str,
        attachments: list[tuple[ExecutionArtifact, bytes, str]],
    ) -> SkillCandidate:
        """通过与多源导入相同的 ZIP 安全规范化器生成唯一候选。"""

        buffer = io.BytesIO()
        with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
            archive.writestr(f"{name}/SKILL.md", markdown.encode("utf-8"))
            for _artifact, data, path in attachments:
                archive.writestr(f"{name}/{path}", data)
        try:
            package = normalize_zip_package(buffer.getvalue())
        except GeneralSkillPackageError as exc:
            raise GeneralSkillProposalError(exc.error_code, "proposed package is invalid") from exc
        if len(package.candidates) != 1:
            raise GeneralSkillProposalError(
                "GENERAL_SKILL_PROPOSAL_INVALID", "proposal must contain one skill"
            )
        return package.candidates[0]

    @staticmethod
    def _skill_markdown(request: GeneralSkillProposalArguments) -> str:
        """生成严格 frontmatter；Agent 创建默认 user_only 且工具声明只具收窄语义。"""

        metadata = {
            "name": request.name,
            "description": request.description,
            "allowed-tools": request.requested_tools,
            "disable-model-invocation": True,
        }
        frontmatter = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False).strip()
        return f"---\n{frontmatter}\n---\n\n{request.instructions.strip()}\n"

    @staticmethod
    def _staged_manifest_row(candidate: SkillCandidate, resource) -> dict[str, object]:
        """把规范资源转换为运行时 manifest 结构，发布前不写 object_key。"""

        root = f"{candidate.root}/" if candidate.root else ""
        relative_path = resource.path.removeprefix(root)
        return {
            "relative_path": relative_path,
            "content_checksum": resource.content_checksum,
            "size": resource.size,
            "media_type": resource.media_type,
            "is_text": resource.is_text,
        }

    @staticmethod
    def _review_markdown(
        *,
        request: GeneralSkillProposalArguments,
        markdown: str,
        base_revision: GeneralSkillRevision | None,
        attachments: list[tuple[ExecutionArtifact, bytes, str]],
        instance: SopInstance,
    ) -> str:
        """形成可下载的完整统一 diff、权限和受管来源清单。"""

        before = base_revision.normalized_skill_markdown if base_revision else ""
        diff = "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                markdown.splitlines(keepends=True),
                fromfile="published/SKILL.md" if base_revision else "/dev/null",
                tofile="proposed/SKILL.md",
            )
        )
        sources = "\n".join(
            f"- `{path}` ← Artifact `{artifact.id}` / sha256 `{artifact.content_checksum}`"
            for artifact, _data, path in attachments
        ) or "- 无附件；仅包含本次对话生成并冻结的 SKILL.md"
        tools = ", ".join(f"`{name}`" for name in request.requested_tools) or "无"
        return (
            f"# Skill 提案审核：{request.name}\n\n"
            f"- Execution：`{instance.id}`\n- Session：`{instance.session_id}`\n"
            f"- Agent：`{instance.agent_id}`\n- 调用策略：`user_only`\n"
            f"- 请求工具（仅收窄已有绑定）：{tools}\n\n"
            f"## 受管来源\n\n{sources}\n\n## 完整 diff\n\n```diff\n{diff}\n```\n"
        )

    def _unique_slug(self, tenant_id: str, name: str) -> str:
        """生成租户内新根唯一 slug；同名提案绝不隐式覆盖。"""

        base = _SLUG_INVALID.sub("-", name.casefold()).strip("-") or "agent-skill"
        base = base[:160]
        candidate = base
        suffix = 2
        while self.db.exec(
            select(GeneralSkill.id).where(
                GeneralSkill.tenant_id == tenant_id,
                GeneralSkill.slug == candidate,
            )
        ).first():
            candidate = f"{base[:150]}-{suffix}"
            suffix += 1
        return candidate

    def _by_operation(self, tenant_id: str, operation_id: str) -> GeneralSkillProposal | None:
        """按 tenant 与 Operation 唯一定位提案，禁止跨租户枚举。"""

        return self.db.exec(
            select(GeneralSkillProposal).where(
                GeneralSkillProposal.tenant_id == tenant_id,
                GeneralSkillProposal.operation_id == operation_id,
            )
        ).first()

    @staticmethod
    def _proposal_checksum(arguments: dict[str, object]) -> str:
        """对严格 JSON 参数生成稳定提案身份。"""

        return hashlib.sha256(
            json.dumps(
                arguments,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _publication_result(proposal: GeneralSkillProposal) -> dict[str, object]:
        """返回不含正文、路径和凭据的稳定发布回执。"""

        return {
            "proposal_id": proposal.id,
            "skill_id": proposal.skill_id,
            "revision_id": proposal.revision_id,
            "binding_id": proposal.published_binding_id,
            "status": proposal.status,
            "agent_id": proposal.agent_id,
        }

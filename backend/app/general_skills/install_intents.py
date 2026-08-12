"""
@Time       : 2026/08/12 11:55
@Author     : zhanglp8181
@File       : install_intents.py
@CallChain  : Chat install API → GeneralSkillInstallIntentService → ImportJob/Revision/Binding
@Description: 以持久 intent 包装正式导入链，确保对话文本本身不成为安装授权。
"""

from __future__ import annotations

from sqlmodel import Session, select

from app.agents.identity import agent_owner_user_id
from app.db.models import (
    AgentProfile,
    ChatSession,
    GeneralSkillImportJob,
    GeneralSkillInstallIntent,
    User,
    utc_now,
)
from app.general_skills.import_schema import (
    GeneralSkillImportConfirm,
    GeneralSkillImportJobCreate,
)
from app.general_skills.import_service import GeneralSkillImportService, import_job_read
from app.general_skills.install_intent_schema import (
    GeneralSkillInstallIntentCreate,
    GeneralSkillInstallIntentRead,
)
from app.general_skills.object_store import FileSystemSkillObjectStore
from app.general_skills.remote_source import RemoteFetcher


class GeneralSkillInstallIntentError(RuntimeError):
    """表示安装卡违反会话身份、所有权、状态或并发契约。"""

    def __init__(self, code: str, message: str, status_code: int = 409) -> None:
        """保存稳定错误码和 HTTP 建议状态。"""

        super().__init__(message)
        self.code = code
        self.status_code = status_code


class GeneralSkillInstallIntentService:
    """创建、恢复和办理用户明确授权的对话 Skill 安装卡。"""

    def __init__(self, db: Session, object_store: FileSystemSkillObjectStore) -> None:
        """绑定请求事务与正式 Skill 对象存储。"""

        self.db = db
        self.imports = GeneralSkillImportService(db, object_store)

    def create(
        self,
        session_id: str,
        request: GeneralSkillInstallIntentCreate,
        *,
        idempotency_key: str,
        current_user: User,
        fetcher: RemoteFetcher,
    ) -> GeneralSkillInstallIntentRead:
        """校验会话/Agent 所有权后创建正式 ImportJob，并保存可刷新卡片。"""

        session = self._owned_session(session_id, request.agent_id, current_user)
        existing = self.db.exec(
            select(GeneralSkillInstallIntent).where(
                GeneralSkillInstallIntent.tenant_id == current_user.tenant_id,
                GeneralSkillInstallIntent.owner_user_id == current_user.id,
                GeneralSkillInstallIntent.idempotency_key == idempotency_key,
            )
        ).first()
        if existing:
            return self.read(existing)
        preview = self.imports.create_job(
            GeneralSkillImportJobCreate(
                tenant_id=current_user.tenant_id,
                target_agent_id=request.agent_id,
                source_kind=request.source_kind,
                source_url=request.source_url,
                revision=request.revision,
                source_subpath=request.source_subpath,
            ),
            idempotency_key=f"chat-{idempotency_key}"[:128],
            current_user=current_user,
            fetcher=fetcher,
        )
        status = (
            "awaiting_owner_confirmation"
            if preview.status == "awaiting_approval"
            else "failed"
        )
        intent = GeneralSkillInstallIntent(
            tenant_id=current_user.tenant_id,
            session_id=session.id,
            agent_id=request.agent_id,
            owner_user_id=current_user.id,
            import_job_id=preview.id,
            idempotency_key=idempotency_key,
            source_kind=request.source_kind,
            status=status,
            error_code=preview.error_code,
        )
        self.db.add(intent)
        self.db.commit()
        self.db.refresh(intent)
        return self.read(intent)

    def list_session(
        self, session_id: str, *, current_user: User
    ) -> list[GeneralSkillInstallIntentRead]:
        """返回本人会话中的全部安装卡，以便刷新和重启后恢复。"""

        session = self._session_for_user(session_id, current_user)
        rows = self.db.exec(
            select(GeneralSkillInstallIntent)
            .where(
                GeneralSkillInstallIntent.tenant_id == current_user.tenant_id,
                GeneralSkillInstallIntent.session_id == session.id,
                GeneralSkillInstallIntent.owner_user_id == current_user.id,
            )
            .order_by(GeneralSkillInstallIntent.created_at)
        ).all()
        return [self.read(row) for row in rows]

    def resolve(
        self,
        session_id: str,
        intent_id: str,
        *,
        command: str,
        expected_row_version: int,
        current_user: User,
    ) -> GeneralSkillInstallIntentRead:
        """以所有权和 row version 确认或取消，确认只消费冻结 preview。"""

        intent = self._owned_intent(session_id, intent_id, current_user)
        if intent.row_version != expected_row_version:
            raise GeneralSkillInstallIntentError("GENERAL_SKILL_INSTALL_STALE", "card is stale")
        if intent.status in {"installed", "cancelled"}:
            return self.read(intent)
        if intent.status != "awaiting_owner_confirmation":
            raise GeneralSkillInstallIntentError(
                "GENERAL_SKILL_INSTALL_STATE_CONFLICT", "card cannot be resolved"
            )
        job = self.db.get(GeneralSkillImportJob, intent.import_job_id)
        if job is None:
            raise GeneralSkillInstallIntentError("GENERAL_SKILL_INSTALL_STALE", "job missing")
        if command == "cancel":
            self.imports.cancel_job(
                job.id,
                expected_row_version=job.row_version,
                current_user=current_user,
            )
            intent.status = "cancelled"
            intent.terminal_at = utc_now()
        elif command == "confirm":
            preview = import_job_read(job)
            if not preview.preview_checksum:
                raise GeneralSkillInstallIntentError("GENERAL_SKILL_INSTALL_STALE", "preview missing")
            installed = self.imports.confirm_job(
                job.id,
                GeneralSkillImportConfirm(
                    preview_checksum=preview.preview_checksum,
                    candidate_ids=[row.candidate_id for row in preview.candidates],
                    expected_row_version=preview.row_version,
                ),
                current_user=current_user,
            )
            intent.status = "installed"
            intent.installed_revision_ids_json = installed.installed_revision_ids
            intent.terminal_at = utc_now()
        else:
            raise GeneralSkillInstallIntentError(
                "GENERAL_SKILL_INSTALL_COMMAND_INVALID", "unsupported command", 400
            )
        intent.row_version += 1
        intent.updated_at = utc_now()
        self.db.add(intent)
        self.db.commit()
        self.db.refresh(intent)
        return self.read(intent)

    def read(self, intent: GeneralSkillInstallIntent) -> GeneralSkillInstallIntentRead:
        """将 intent 与脱敏 ImportJob 安全预览合并为卡片响应。"""

        job = self.db.get(GeneralSkillImportJob, intent.import_job_id)
        if job is None:
            raise GeneralSkillInstallIntentError("GENERAL_SKILL_INSTALL_STALE", "job missing")
        preview = import_job_read(job)
        source_revision = None
        if job.source_reference_redacted and "@" in job.source_reference_redacted:
            source_revision = job.source_reference_redacted.split("@", 1)[1].split("#", 1)[0]
        return GeneralSkillInstallIntentRead(
            id=intent.id,
            session_id=intent.session_id,
            agent_id=intent.agent_id,
            source_kind=intent.source_kind,
            source_reference_redacted=job.source_reference_redacted,
            source_revision=source_revision,
            status=intent.status,
            import_job_id=job.id,
            raw_checksum=preview.raw_checksum,
            normalized_checksum=preview.normalized_checksum,
            preview_checksum=preview.preview_checksum,
            candidates=preview.candidates,
            installed_revision_ids=preview.installed_revision_ids,
            error_code=intent.error_code or preview.error_code,
            row_version=intent.row_version,
            created_at=intent.created_at.isoformat(),
            updated_at=intent.updated_at.isoformat(),
        )

    def _owned_intent(
        self, session_id: str, intent_id: str, current_user: User
    ) -> GeneralSkillInstallIntent:
        """按 tenant/user/session 三边界读取安装卡。"""

        self._session_for_user(session_id, current_user)
        row = self.db.get(GeneralSkillInstallIntent, intent_id)
        if (
            row is None
            or row.tenant_id != current_user.tenant_id
            or row.owner_user_id != current_user.id
            or row.session_id != session_id
        ):
            raise GeneralSkillInstallIntentError("GENERAL_SKILL_INSTALL_NOT_FOUND", "card missing", 404)
        return row

    def _owned_session(
        self, session_id: str, agent_id: str, current_user: User
    ) -> ChatSession:
        """要求会话和目标 Agent 均属于当前用户。"""

        session = self._session_for_user(session_id, current_user)
        agent = self.db.get(AgentProfile, agent_id)
        if (
            agent is None
            or agent.tenant_id != current_user.tenant_id
            or agent.is_overall
            or agent_owner_user_id(agent) != current_user.id
            or session.agent_id != agent.id
        ):
            raise GeneralSkillInstallIntentError(
                "GENERAL_SKILL_INSTALL_FORBIDDEN", "session agent is not manageable", 403
            )
        return session

    def _session_for_user(self, session_id: str, current_user: User) -> ChatSession:
        """隐藏其他用户和租户的会话存在性。"""

        session = self.db.get(ChatSession, session_id)
        if (
            session is None
            or session.tenant_id != current_user.tenant_id
            or session.user_id != current_user.id
        ):
            raise GeneralSkillInstallIntentError("GENERAL_SKILL_INSTALL_NOT_FOUND", "session missing", 404)
        return session

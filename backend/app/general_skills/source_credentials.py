"""
@Time       : 2026/08/12 23:59
@Author     : zhanglp8181
@File       : source_credentials.py
@CallChain  : Skill credential API/import worker → encrypted ConnectionSecret → HTTPS adapter
@Description: 管理用户级私有来源凭据，并在 worker 边界按主体、来源和主机解析授权头。
"""

from __future__ import annotations

import json
from urllib.parse import urlsplit

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.audit.service import append_management_audit
from app.db.models import (
    ConnectionSecret,
    GeneralSkillSourceCredential,
    User,
    new_id,
    utc_now,
)
from app.general_skills.import_schema import (
    GeneralSkillSourceCredentialCreate,
    GeneralSkillSourceCredentialRead,
)
from app.general_skills.remote_source import validated_remote_reference
from app.security.encryption import decrypt_secret, encrypt_secret
from app.security.tenant import ensure_tenant


class GeneralSkillSourceCredentialError(RuntimeError):
    """表示来源凭据的主体、状态、主机或密文不满足生产契约。"""

    def __init__(self, error_code: str, detail: str, status_code: int) -> None:
        """保存稳定错误码、脱敏说明与 HTTP 状态。"""

        super().__init__(detail)
        self.error_code = error_code
        self.status_code = status_code


class GeneralSkillSourceCredentialService:
    """管理用户私有来源档案和追加式密文修订。"""

    def __init__(self, db: Session, *, https_allowed_hosts: frozenset[str] | None) -> None:
        """绑定数据库会话和部署允许的自定义 HTTPS 主机集合。"""

        self.db = db
        self.https_allowed_hosts = https_allowed_hosts

    def create(
        self,
        request: GeneralSkillSourceCredentialCreate,
        *,
        current_user: User,
    ) -> GeneralSkillSourceCredentialRead:
        """加密保存新 token，并创建只归当前用户所有的稳定凭据引用。"""

        ensure_tenant(self.db, request.tenant_id)
        if request.tenant_id != current_user.tenant_id:
            raise GeneralSkillSourceCredentialError(
                "GENERAL_SKILL_CREDENTIAL_NOT_AVAILABLE",
                "source credential is not available",
                404,
            )
        host = self._validated_host(request.source_kind, request.allowed_host)
        token = _validated_token(request.token.get_secret_value())
        reference_id = new_id("gssourcesecret")
        now = utc_now()
        profile = GeneralSkillSourceCredential(
            tenant_id=current_user.tenant_id,
            owner_user_id=current_user.id,
            display_name=_validated_display_name(request.display_name),
            source_kind=request.source_kind,
            allowed_host=host,
            secret_reference_id=reference_id,
            created_at=now,
            updated_at=now,
        )
        secret = ConnectionSecret(
            tenant_id=current_user.tenant_id,
            provider=f"general_skill_{request.source_kind}",
            reference_id=reference_id,
            encrypted_payload=encrypt_secret(json.dumps({"token": token})),
            revision=1,
            status="active",
            created_at=now,
            updated_at=now,
        )
        self.db.add(profile)
        self.db.add(secret)
        self.db.flush()
        self._audit(profile, current_user.id, "created")
        self.db.commit()
        self.db.refresh(profile)
        return source_credential_read(profile)

    def list_owned(self, *, current_user: User) -> list[GeneralSkillSourceCredentialRead]:
        """仅列出当前用户在当前租户内拥有的来源凭据，不回显密文引用。"""

        rows = self.db.exec(
            select(GeneralSkillSourceCredential)
            .where(
                GeneralSkillSourceCredential.tenant_id == current_user.tenant_id,
                GeneralSkillSourceCredential.owner_user_id == current_user.id,
            )
            .order_by(GeneralSkillSourceCredential.created_at, GeneralSkillSourceCredential.id)
        ).all()
        return [source_credential_read(row) for row in rows]

    def rotate(
        self,
        credential_id: str,
        token: str,
        *,
        expected_row_version: int,
        current_user: User,
    ) -> GeneralSkillSourceCredentialRead:
        """追加新密文修订并使旧修订失效，稳定 profile ID 保持不变。"""

        profile = self._owned(credential_id, current_user)
        if profile.status != "active" or profile.row_version != expected_row_version:
            raise _state_conflict()
        normalized_token = _validated_token(token)
        old_secret = self._secret(profile)
        now = utc_now()
        result = self.db.exec(
            update(GeneralSkillSourceCredential)
            .where(
                GeneralSkillSourceCredential.id == profile.id,
                GeneralSkillSourceCredential.status == "active",
                GeneralSkillSourceCredential.row_version == expected_row_version,
            )
            .values(
                secret_revision=GeneralSkillSourceCredential.secret_revision + 1,
                row_version=GeneralSkillSourceCredential.row_version + 1,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            self.db.rollback()
            raise _state_conflict()
        old_secret.status = "superseded"
        old_secret.updated_at = now
        self.db.expire(profile)
        self.db.refresh(profile)
        self.db.add(old_secret)
        self.db.add(
            ConnectionSecret(
                tenant_id=profile.tenant_id,
                provider=f"general_skill_{profile.source_kind}",
                reference_id=profile.secret_reference_id,
                encrypted_payload=encrypt_secret(json.dumps({"token": normalized_token})),
                revision=profile.secret_revision,
                status="active",
                created_at=now,
                updated_at=now,
            )
        )
        self.db.add(profile)
        self._audit(profile, current_user.id, "rotated")
        try:
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()
            raise _state_conflict() from exc
        self.db.refresh(profile)
        return source_credential_read(profile)

    def revoke(
        self,
        credential_id: str,
        *,
        expected_row_version: int,
        current_user: User,
    ) -> GeneralSkillSourceCredentialRead:
        """撤销稳定档案和当前密文修订，使排队中的后续抓取 fail-closed。"""

        profile = self._owned(credential_id, current_user)
        if profile.status == "revoked":
            return source_credential_read(profile)
        if profile.row_version != expected_row_version:
            raise _state_conflict()
        now = utc_now()
        secret = self._secret(profile)
        result = self.db.exec(
            update(GeneralSkillSourceCredential)
            .where(
                GeneralSkillSourceCredential.id == profile.id,
                GeneralSkillSourceCredential.status == "active",
                GeneralSkillSourceCredential.row_version == expected_row_version,
            )
            .values(
                status="revoked",
                row_version=GeneralSkillSourceCredential.row_version + 1,
                updated_at=now,
                revoked_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            self.db.rollback()
            raise _state_conflict()
        secret.status = "revoked"
        secret.updated_at = now
        secret.revoked_at = now
        self.db.expire(profile)
        self.db.refresh(profile)
        self.db.add(secret)
        self.db.add(profile)
        self._audit(profile, current_user.id, "revoked")
        self.db.commit()
        self.db.refresh(profile)
        return source_credential_read(profile)

    def _validated_host(self, source_kind: str, requested_host: str | None) -> str:
        """规范化单一授权主机并要求自定义 HTTPS 主机属于部署白名单。"""

        if source_kind == "github":
            return "github.com"
        host = (requested_host or "").strip().lower().rstrip(".")
        try:
            validated = validated_remote_reference(
                f"https://{host}/",
                allowed_hosts=self.https_allowed_hosts,
            )
        except Exception as exc:
            raise GeneralSkillSourceCredentialError(
                "GENERAL_SKILL_CREDENTIAL_HOST_INVALID",
                "source credential host is not allowed",
                400,
            ) from exc
        return urlsplit(validated).hostname or ""

    def _owned(self, credential_id: str, current_user: User) -> GeneralSkillSourceCredential:
        """按 tenant/user 双边界读取档案，统一用不可枚举错误拒绝越权。"""

        profile = self.db.exec(
            select(GeneralSkillSourceCredential).where(
                GeneralSkillSourceCredential.id == credential_id,
                GeneralSkillSourceCredential.tenant_id == current_user.tenant_id,
                GeneralSkillSourceCredential.owner_user_id == current_user.id,
            )
        ).first()
        if not profile:
            raise GeneralSkillSourceCredentialError(
                "GENERAL_SKILL_CREDENTIAL_NOT_AVAILABLE",
                "source credential is not available",
                404,
            )
        return profile

    def _secret(self, profile: GeneralSkillSourceCredential) -> ConnectionSecret:
        """读取档案精确指向的密文修订，不自动回退旧 token。"""

        secret = self.db.exec(
            select(ConnectionSecret).where(
                ConnectionSecret.tenant_id == profile.tenant_id,
                ConnectionSecret.reference_id == profile.secret_reference_id,
                ConnectionSecret.revision == profile.secret_revision,
            )
        ).first()
        if not secret:
            raise GeneralSkillSourceCredentialError(
                "GENERAL_SKILL_CREDENTIAL_UNAVAILABLE",
                "source credential secret is unavailable",
                503,
            )
        return secret

    def _audit(
        self,
        profile: GeneralSkillSourceCredential,
        actor_user_id: str,
        action: str,
    ) -> None:
        """追加不含 token、密文引用和具体仓库地址的管理审计。"""

        append_management_audit(
            self.db,
            tenant_id=profile.tenant_id,
            actor_user_id=actor_user_id,
            actor_display_name=None,
            actor_type="user",
            action=f"general_skill_source_credential_{action}",
            action_kind="skill_import",
            outcome="success",
            resource_type="general_skill_source_credential",
            resource_id=profile.id,
            correlation_id=profile.id,
            detail={"source_kind": profile.source_kind, "secret_revision": profile.secret_revision},
        )


def resolve_source_authorization(
    db: Session,
    *,
    credential_id: str,
    tenant_id: str,
    owner_user_id: str,
    source_kind: str,
    source_host: str,
) -> tuple[str, frozenset[str], int]:
    """在外呼前解析本人活动凭据，并把授权传播范围固定为档案单一主机。"""

    profile = db.exec(
        select(GeneralSkillSourceCredential).where(
            GeneralSkillSourceCredential.id == credential_id,
            GeneralSkillSourceCredential.tenant_id == tenant_id,
            GeneralSkillSourceCredential.owner_user_id == owner_user_id,
            GeneralSkillSourceCredential.source_kind == source_kind,
            GeneralSkillSourceCredential.allowed_host == source_host,
            GeneralSkillSourceCredential.status == "active",
        )
    ).first()
    if not profile:
        raise GeneralSkillSourceCredentialError(
            "GENERAL_SKILL_CREDENTIAL_NOT_AVAILABLE",
            "source credential is not available for this import",
            403,
        )
    secret = db.exec(
        select(ConnectionSecret).where(
            ConnectionSecret.tenant_id == tenant_id,
            ConnectionSecret.reference_id == profile.secret_reference_id,
            ConnectionSecret.revision == profile.secret_revision,
            ConnectionSecret.status == "active",
        )
    ).first()
    if not secret:
        raise GeneralSkillSourceCredentialError(
            "GENERAL_SKILL_CREDENTIAL_UNAVAILABLE",
            "source credential must be reauthorized",
            503,
        )
    try:
        payload = json.loads(decrypt_secret(secret.encrypted_payload))
        raw_token = payload.get("token")
        if not isinstance(raw_token, str):
            raise ValueError("credential token payload is invalid")
        token = _validated_token(raw_token)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise GeneralSkillSourceCredentialError(
            "GENERAL_SKILL_CREDENTIAL_UNAVAILABLE",
            "source credential must be reauthorized",
            503,
        ) from exc
    return f"Bearer {token}", frozenset({profile.allowed_host}), profile.secret_revision


def validate_source_credential_revision(
    db: Session,
    *,
    credential_id: str,
    tenant_id: str,
    owner_user_id: str,
    expected_revision: int,
) -> None:
    """下载返回后复核凭据仍为同一活动修订，撤销或轮换时丢弃旧响应。"""

    exists = db.exec(
        select(GeneralSkillSourceCredential.id).where(
            GeneralSkillSourceCredential.id == credential_id,
            GeneralSkillSourceCredential.tenant_id == tenant_id,
            GeneralSkillSourceCredential.owner_user_id == owner_user_id,
            GeneralSkillSourceCredential.secret_revision == expected_revision,
            GeneralSkillSourceCredential.status == "active",
        )
    ).first()
    if not exists:
        raise GeneralSkillSourceCredentialError(
            "GENERAL_SKILL_CREDENTIAL_CHANGED",
            "source credential changed while the package was downloading",
            409,
        )


def validate_source_credential_reference(
    db: Session,
    *,
    credential_id: str,
    tenant_id: str,
    owner_user_id: str,
    source_kind: str,
    source_host: str,
) -> None:
    """创建作业时只验证档案归属与 host，不在 Web 请求中解密 token。"""

    exists = db.exec(
        select(GeneralSkillSourceCredential.id).where(
            GeneralSkillSourceCredential.id == credential_id,
            GeneralSkillSourceCredential.tenant_id == tenant_id,
            GeneralSkillSourceCredential.owner_user_id == owner_user_id,
            GeneralSkillSourceCredential.source_kind == source_kind,
            GeneralSkillSourceCredential.allowed_host == source_host,
            GeneralSkillSourceCredential.status == "active",
        )
    ).first()
    if not exists:
        raise GeneralSkillSourceCredentialError(
            "GENERAL_SKILL_CREDENTIAL_NOT_AVAILABLE",
            "source credential is not available for this import",
            403,
        )


def source_credential_read(
    profile: GeneralSkillSourceCredential,
) -> GeneralSkillSourceCredentialRead:
    """构造不暴露密文引用的用户响应。"""

    return GeneralSkillSourceCredentialRead(
        id=profile.id,
        tenant_id=profile.tenant_id,
        display_name=profile.display_name,
        source_kind=profile.source_kind,
        allowed_host=profile.allowed_host,
        secret_revision=profile.secret_revision,
        status=profile.status,
        row_version=profile.row_version,
        created_at=profile.created_at.isoformat(),
        updated_at=profile.updated_at.isoformat(),
    )


def _validated_token(value: str) -> str:
    """拒绝空值、超预算和 CR/LF，避免授权头注入。"""

    token = value.strip()
    if not token or len(token) > 8192 or "\r" in token or "\n" in token:
        raise GeneralSkillSourceCredentialError(
            "GENERAL_SKILL_CREDENTIAL_INVALID",
            "source credential token is invalid",
            400,
        )
    return token


def _validated_display_name(value: str) -> str:
    """拒绝只包含空白的凭据名称。"""

    display_name = value.strip()
    if not display_name:
        raise GeneralSkillSourceCredentialError(
            "GENERAL_SKILL_CREDENTIAL_INVALID",
            "source credential display name is invalid",
            400,
        )
    return display_name


def _state_conflict() -> GeneralSkillSourceCredentialError:
    """返回凭据轮换/撤销的统一乐观锁冲突。"""

    return GeneralSkillSourceCredentialError(
        "GENERAL_SKILL_STATE_CONFLICT",
        "source credential changed concurrently",
        409,
    )

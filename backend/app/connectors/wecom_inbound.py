"""
@Time       : 2026/08/10 15:05
@Author     : zhanglp8181
@File       : wecom_inbound.py
@CallChain  : WeCom callback API → WeComInboundService → connector_inbound_events
@Description: 解密、校验并幂等持久化企业微信入站事件，明文载荷仅以应用密钥加密保存。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from xml.etree import ElementTree

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.connectors.service import ConnectionError, ConnectionService
from app.connectors.wecom_callback import WeComCallbackCrypto, WeComCallbackError
from app.db.models import ConnectorInboundEvent
from app.security.encryption import decrypt_secret, encrypt_secret


@dataclass(frozen=True)
class WeComTextEnvelope:
    """承载消费阶段所需正文和回复目标，并阻止日志 repr 泄漏。"""

    content: str = field(repr=False)
    sender_ref: str = field(repr=False)


class WeComInboundError(ValueError):
    """以稳定代码表示公开回调的报文、租户身份或幂等冲突。"""

    def __init__(self, code: str) -> None:
        """保存可安全映射到 HTTP 的错误代码，不携带原始 XML 或凭据。"""

        super().__init__(code)
        self.code = code


class WeComInboundService:
    """把公开企业微信回调收敛为租户隔离的持久 inbox 事实。"""

    def __init__(self, db: Session) -> None:
        """绑定请求事务；只有持久化成功后上层才能向企业微信返回 success。"""

        self.db = db
        self.connections = ConnectionService(db)

    def verify_url(
        self,
        *,
        profile_id: str,
        msg_signature: str,
        timestamp: str,
        nonce: str,
        echo_str: str,
    ) -> str:
        """解析档案专属密钥并完成企业微信 URL 验证握手。"""

        crypto = self._crypto(profile_id)
        try:
            return crypto.verify_url(
                msg_signature=msg_signature,
                timestamp=timestamp,
                nonce=nonce,
                echo_str=echo_str,
            )
        except WeComCallbackError as exc:
            raise WeComInboundError(str(exc)) from exc

    def receive(
        self,
        *,
        profile_id: str,
        msg_signature: str,
        timestamp: str,
        nonce: str,
        encrypted: str,
    ) -> tuple[ConnectorInboundEvent, bool]:
        """验签解密、校验企业与应用身份，并以外部事件 ID 幂等写入 inbox。"""

        config = self._callback_config(profile_id)
        crypto = WeComCallbackCrypto(
            token=config.token,
            encoding_aes_key=config.encoding_aes_key,
            receive_id=config.corp_id,
        )
        try:
            plaintext = crypto.decrypt(
                msg_signature=msg_signature,
                timestamp=timestamp,
                nonce=nonce,
                encrypted=encrypted,
            )
        except WeComCallbackError as exc:
            raise WeComInboundError(str(exc)) from exc
        root = _safe_xml(plaintext)
        corp_id = _xml_text(root, "ToUserName")
        agent_id = _xml_text(root, "AgentID")
        if corp_id and corp_id != config.corp_id:
            raise WeComInboundError("WECOM_CALLBACK_RECEIVE_ID_MISMATCH")
        if agent_id and agent_id != config.agent_id:
            raise WeComInboundError("WECOM_CALLBACK_AGENT_ID_MISMATCH")
        checksum = hashlib.sha256(plaintext).hexdigest()
        external_event_id = _external_event_id(root, checksum)
        existing = self._existing(config.tenant_id, profile_id, external_event_id)
        if existing is not None:
            if existing.payload_checksum != checksum:
                raise WeComInboundError("WECOM_CALLBACK_EVENT_ID_CONFLICT")
            return existing, False
        event = ConnectorInboundEvent(
            tenant_id=config.tenant_id,
            provider="wecom",
            profile_id=profile_id,
            external_event_id=external_event_id,
            payload_checksum=checksum,
            encrypted_payload=encrypt_secret(plaintext.decode("utf-8")),
            event_type=_event_type(root),
            sender_ref_hash=_sender_hash(config.corp_id, _xml_text(root, "FromUserName")),
        )
        try:
            with self.db.begin_nested():
                self.db.add(event)
                self.db.flush()
        except IntegrityError:
            existing = self._existing(config.tenant_id, profile_id, external_event_id)
            if existing is None or existing.payload_checksum != checksum:
                raise WeComInboundError("WECOM_CALLBACK_EVENT_ID_CONFLICT") from None
            return existing, False
        return event, True

    def text_envelope(self, event: ConnectorInboundEvent) -> WeComTextEnvelope:
        """重新校验持久密文、摘要及企业身份，并只接受有界文本消息。"""

        if event.provider != "wecom" or event.event_type != "text":
            raise WeComInboundError("WECOM_INBOUND_EVENT_UNSUPPORTED")
        config = self._callback_config(event.profile_id)
        if config.tenant_id != event.tenant_id:
            raise WeComInboundError("WECOM_INBOUND_TENANT_MISMATCH")
        try:
            plaintext = decrypt_secret(event.encrypted_payload).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise WeComInboundError("WECOM_INBOUND_PAYLOAD_INVALID") from exc
        if hashlib.sha256(plaintext).hexdigest() != event.payload_checksum:
            raise WeComInboundError("WECOM_INBOUND_PAYLOAD_INVALID")
        root = _safe_xml(plaintext)
        if _xml_text(root, "ToUserName") not in {"", config.corp_id}:
            raise WeComInboundError("WECOM_CALLBACK_RECEIVE_ID_MISMATCH")
        if _xml_text(root, "AgentID") not in {"", config.agent_id}:
            raise WeComInboundError("WECOM_CALLBACK_AGENT_ID_MISMATCH")
        sender = _xml_text(root, "FromUserName")
        content = _xml_text(root, "Content")
        if (
            not sender
            or not content
            or len(content) > 20_000
            or _sender_hash(config.corp_id, sender) != event.sender_ref_hash
        ):
            raise WeComInboundError("WECOM_INBOUND_PAYLOAD_INVALID")
        return WeComTextEnvelope(content=content, sender_ref=sender)

    def _crypto(self, profile_id: str) -> WeComCallbackCrypto:
        """从指定档案构造不跨请求缓存秘密的回调加密对象。"""

        config = self._callback_config(profile_id)
        return WeComCallbackCrypto(
            token=config.token,
            encoding_aes_key=config.encoding_aes_key,
            receive_id=config.corp_id,
        )

    def _callback_config(self, profile_id: str):
        """把连接领域错误收敛为不泄漏档案存在性的公开回调错误。"""

        try:
            return self.connections.wecom_callback_config(profile_id)
        except ConnectionError as exc:
            raise WeComInboundError("WECOM_CALLBACK_NOT_FOUND") from exc

    def _existing(
        self,
        tenant_id: str,
        profile_id: str,
        external_event_id: str,
    ) -> ConnectorInboundEvent | None:
        """按完整租户/provider/档案/外部事件键查询幂等事实。"""

        return self.db.exec(
            select(ConnectorInboundEvent).where(
                ConnectorInboundEvent.tenant_id == tenant_id,
                ConnectorInboundEvent.provider == "wecom",
                ConnectorInboundEvent.profile_id == profile_id,
                ConnectorInboundEvent.external_event_id == external_event_id,
            )
        ).first()


def encrypted_element(body: bytes) -> str:
    """从有界外层 XML 中提取唯一 Encrypt 元素，拒绝 DTD、实体和空密文。"""

    root = _safe_xml(body)
    encrypted = _xml_text(root, "Encrypt")
    if not encrypted:
        raise WeComInboundError("WECOM_CALLBACK_PAYLOAD_INVALID")
    return encrypted


def _safe_xml(value: bytes) -> ElementTree.Element:
    """在解析前拒绝 XML 实体声明，并要求 UTF-8、xml 根元素和有限报文。"""

    lowered = value.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered or b"\x00" in value:
        raise WeComInboundError("WECOM_CALLBACK_PAYLOAD_INVALID")
    try:
        root = ElementTree.fromstring(value.decode("utf-8"))
    except (ElementTree.ParseError, UnicodeDecodeError) as exc:
        raise WeComInboundError("WECOM_CALLBACK_PAYLOAD_INVALID") from exc
    if root.tag != "xml":
        raise WeComInboundError("WECOM_CALLBACK_PAYLOAD_INVALID")
    return root


def _xml_text(root: ElementTree.Element, name: str) -> str:
    """读取 XML 直属文本节点并清理空白，忽略嵌套伪造节点。"""

    node = root.find(f"./{name}")
    return str(node.text or "").strip() if node is not None else ""


def _external_event_id(root: ElementTree.Element, checksum: str) -> str:
    """优先使用企业微信稳定消息 ID，缺失时以完整明文摘要确保重试幂等。"""

    for field_name in ("MsgId", "MsgID", "SysMsgId"):
        value = _xml_text(root, field_name)
        if value:
            if len(value) <= 255:
                return value
            digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
            return f"sha256-id:{digest}"
    return f"sha256:{checksum}"


def _event_type(root: ElementTree.Element) -> str:
    """生成有界事件分类，不把 Content 等用户明文写入可检索列。"""

    message_type = (_xml_text(root, "MsgType") or "unknown").lower()[:32]
    if message_type != "event":
        return message_type
    event = (_xml_text(root, "Event") or "unknown").lower()[:31]
    return f"event:{event}"


def _sender_hash(corp_id: str, sender: str) -> str | None:
    """仅保存租户域内发送者引用摘要，避免 inbox 索引暴露外部用户标识。"""

    if not sender:
        return None
    return hashlib.sha256(f"{corp_id}\0{sender}".encode("utf-8")).hexdigest()

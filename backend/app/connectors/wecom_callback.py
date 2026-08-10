"""
@Time       : 2026/08/10 14:15
@Author     : zhanglp8181
@File       : wecom_callback.py
@CallChain  : WeCom callback API → WeComCallbackCrypto → 企业微信加密回调报文
@Description: 验证企业微信回调签名并解密 BizMsgCrypt 兼容的 AES-CBC 消息载荷。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


WECOM_CALLBACK_MAX_BODY_BYTES = 256 * 1024
_PKCS7_BLOCK_SIZE = 32


class WeComCallbackError(ValueError):
    """表示不可信企业微信回调未通过配置、签名或密文完整性检查。"""


class WeComCallbackCrypto:
    """实现企业微信官方 BizMsgCrypt 的验签和解密子集。"""

    def __init__(self, *, token: str, encoding_aes_key: str, receive_id: str) -> None:
        """校验回调三元配置并派生固定 AES-256-CBC 密钥和 IV。"""

        self._token = token.strip()
        self._receive_id = receive_id.strip()
        aes_key = encoding_aes_key.strip()
        if not self._token or len(self._token) > 128:
            raise WeComCallbackError("WECOM_CALLBACK_TOKEN_INVALID")
        if len(aes_key) != 43:
            raise WeComCallbackError("WECOM_CALLBACK_AES_KEY_INVALID")
        if not self._receive_id or len(self._receive_id) > 128:
            raise WeComCallbackError("WECOM_CALLBACK_RECEIVE_ID_INVALID")
        try:
            key = base64.b64decode(aes_key + "=", validate=True)
        except (binascii.Error, ValueError) as exc:
            raise WeComCallbackError("WECOM_CALLBACK_AES_KEY_INVALID") from exc
        if len(key) != 32:
            raise WeComCallbackError("WECOM_CALLBACK_AES_KEY_INVALID")
        self._key = key
        self._iv = key[:16]

    def verify_url(
        self,
        *,
        msg_signature: str,
        timestamp: str,
        nonce: str,
        echo_str: str,
    ) -> str:
        """验证企业微信 GET 握手参数并返回要求原样响应的明文 echo。"""

        plain = self.decrypt(
            msg_signature=msg_signature,
            timestamp=timestamp,
            nonce=nonce,
            encrypted=echo_str,
        )
        try:
            return plain.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WeComCallbackError("WECOM_CALLBACK_PAYLOAD_INVALID") from exc

    def decrypt(
        self,
        *,
        msg_signature: str,
        timestamp: str,
        nonce: str,
        encrypted: str,
    ) -> bytes:
        """恒定时间比较签名，解密报文，并强制校验尾部 receive-id。"""

        signature = msg_signature.strip().lower()
        if len(signature) != 40 or not hmac.compare_digest(
            signature,
            self.signature(timestamp=timestamp, nonce=nonce, encrypted=encrypted),
        ):
            raise WeComCallbackError("WECOM_CALLBACK_SIGNATURE_INVALID")
        try:
            ciphertext = base64.b64decode(encrypted, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise WeComCallbackError("WECOM_CALLBACK_PAYLOAD_INVALID") from exc
        if not ciphertext or len(ciphertext) % 16 != 0:
            raise WeComCallbackError("WECOM_CALLBACK_PAYLOAD_INVALID")
        try:
            decryptor = Cipher(algorithms.AES(self._key), modes.CBC(self._iv)).decryptor()
            padded = decryptor.update(ciphertext) + decryptor.finalize()
        except ValueError as exc:
            raise WeComCallbackError("WECOM_CALLBACK_PAYLOAD_INVALID") from exc
        plain = self._unpad(padded)
        if len(plain) < 20:
            raise WeComCallbackError("WECOM_CALLBACK_PAYLOAD_INVALID")
        message_length = struct.unpack(">I", plain[16:20])[0]
        message_end = 20 + message_length
        if message_end > len(plain):
            raise WeComCallbackError("WECOM_CALLBACK_PAYLOAD_INVALID")
        message = plain[20:message_end]
        receive_id = plain[message_end:]
        try:
            decoded_receive_id = receive_id.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WeComCallbackError("WECOM_CALLBACK_PAYLOAD_INVALID") from exc
        if not hmac.compare_digest(decoded_receive_id, self._receive_id):
            raise WeComCallbackError("WECOM_CALLBACK_RECEIVE_ID_MISMATCH")
        return message

    def signature(self, *, timestamp: str, nonce: str, encrypted: str) -> str:
        """按企业微信协议对 Token、时间戳、nonce 和密文排序后计算 SHA-1。"""

        values = (self._token, timestamp.strip(), nonce.strip(), encrypted.strip())
        return hashlib.sha1("".join(sorted(values)).encode("utf-8")).hexdigest()

    @staticmethod
    def _unpad(value: bytes) -> bytes:
        """严格移除企业微信使用的 32 字节块 PKCS#7 填充。"""

        if not value:
            raise WeComCallbackError("WECOM_CALLBACK_PADDING_INVALID")
        padding_size = value[-1]
        if padding_size < 1 or padding_size > _PKCS7_BLOCK_SIZE:
            raise WeComCallbackError("WECOM_CALLBACK_PADDING_INVALID")
        if value[-padding_size:] != bytes([padding_size]) * padding_size:
            raise WeComCallbackError("WECOM_CALLBACK_PADDING_INVALID")
        return value[:-padding_size]

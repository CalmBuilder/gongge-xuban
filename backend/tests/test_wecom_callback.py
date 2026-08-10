"""
@Time       : 2026/08/10 14:15
@Author     : zhanglp8181
@File       : test_wecom_callback.py
@CallChain  : pytest → WeComCallbackCrypto → 企业微信兼容加密测试报文
@Description: 回归企业微信 URL 验证、签名、AES 包边界及 receive-id 隔离。
"""

from __future__ import annotations

import base64
import hashlib
import os
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
import pytest

from app.connectors.wecom_callback import WeComCallbackCrypto, WeComCallbackError


TOKEN = "callback-token"
AES_KEY = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"
RECEIVE_ID = "ww-test-corp"


def _encrypt(plaintext: bytes, *, receive_id: str = RECEIVE_ID) -> str:
    """以独立测试构造器生成符合 BizMsgCrypt 包格式的固定语义密文。"""

    key = base64.b64decode(AES_KEY + "=")
    packet = os.urandom(16) + struct.pack(">I", len(plaintext)) + plaintext + receive_id.encode()
    pad_size = 32 - len(packet) % 32
    padded = packet + bytes([pad_size]) * pad_size
    encryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
    return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode()


def _signature(encrypted: str, *, timestamp: str = "1720000000", nonce: str = "nonce") -> str:
    """独立计算企业微信 SHA-1 签名，避免调用被测实现生成预期值。"""

    values = sorted([TOKEN, timestamp, nonce, encrypted])
    return hashlib.sha1("".join(values).encode()).hexdigest()


def test_verify_url_returns_decrypted_echo() -> None:
    """有效 GET 握手必须只返回解密后的 echo 明文。"""

    encrypted = _encrypt(b"verified-echo")
    crypto = WeComCallbackCrypto(token=TOKEN, encoding_aes_key=AES_KEY, receive_id=RECEIVE_ID)

    assert crypto.verify_url(
        msg_signature=_signature(encrypted),
        timestamp="1720000000",
        nonce="nonce",
        echo_str=encrypted,
    ) == "verified-echo"


def test_decrypt_rejects_signature_mismatch_before_decryption() -> None:
    """签名不匹配时不得尝试把攻击者密文当作可信消息处理。"""

    crypto = WeComCallbackCrypto(token=TOKEN, encoding_aes_key=AES_KEY, receive_id=RECEIVE_ID)

    with pytest.raises(WeComCallbackError, match="SIGNATURE_INVALID"):
        crypto.decrypt(
            msg_signature="0" * 40,
            timestamp="1720000000",
            nonce="nonce",
            encrypted=_encrypt(b"message"),
        )


def test_decrypt_rejects_receive_id_cross_tenant_mismatch() -> None:
    """即使签名正确，也必须拒绝为另一企业 receive-id 加密的报文。"""

    encrypted = _encrypt(b"message", receive_id="ww-other-corp")
    crypto = WeComCallbackCrypto(token=TOKEN, encoding_aes_key=AES_KEY, receive_id=RECEIVE_ID)

    with pytest.raises(WeComCallbackError, match="RECEIVE_ID_MISMATCH"):
        crypto.decrypt(
            msg_signature=_signature(encrypted),
            timestamp="1720000000",
            nonce="nonce",
            encrypted=encrypted,
        )


@pytest.mark.parametrize("aes_key", ["", "short", "!" * 43, "a" * 42, "a" * 44])
def test_callback_configuration_rejects_invalid_aes_keys(aes_key: str) -> None:
    """拒绝长度、字符集或解码后位数不符合 AES-256 要求的配置。"""

    with pytest.raises(WeComCallbackError, match="AES_KEY_INVALID"):
        WeComCallbackCrypto(token=TOKEN, encoding_aes_key=aes_key, receive_id=RECEIVE_ID)

"""
@Time       : 2026/10/01
@Author     : zhanglp8181
@File       : extraction_contract.py
@CallChain  : T1登记/worker可信读取 → 版本快照 → MemoryService发送与T3结算
@Description: 用单次资源读取绑定提示词正文、摘要和唯一受支持的提取schema。
"""

from dataclasses import dataclass
import hashlib
from pathlib import Path


MEMORY_EXTRACTION_SCHEMA_VERSION = "sd6b.v1"


class MemoryExtractionVersionInvalid(RuntimeError):
    """表示资源不可读或任务声明版本不兼容，须安全结束且不得按当前版本猜测。"""


@dataclass(frozen=True)
class MemoryExtractionContract:
    """保存一次可信读取的完整提示词；在途资源变化不改变发送与解析依据。"""

    prompt_text: str
    prompt_hash: str
    schema_version: str = MEMORY_EXTRACTION_SCHEMA_VERSION

    @classmethod
    def read(cls, prompt_path: Path) -> "MemoryExtractionContract":
        """只读一次原始字节并严格解码，不用占位版本替代缺失、空白或损坏资源。"""

        try:
            content = prompt_path.read_bytes()
            text = content.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise MemoryExtractionVersionInvalid("capture_contract_changed") from exc
        if not text.strip():
            raise MemoryExtractionVersionInvalid("capture_contract_changed")
        return cls(prompt_text=text, prompt_hash=hashlib.sha256(content).hexdigest())

    def validate(self) -> None:
        """解析只支持声明的v1；提示词正文必须与快照摘要一致且非空。"""

        if (
            self.schema_version != MEMORY_EXTRACTION_SCHEMA_VERSION
            or not self.prompt_text.strip()
            or hashlib.sha256(self.prompt_text.encode("utf-8")).hexdigest() != self.prompt_hash
        ):
            raise MemoryExtractionVersionInvalid("capture_contract_changed")

    def matches(self, schema_version: str, prompt_hash: str) -> bool:
        """比较持久声明与实际快照，空/损坏/未知版本不允许自动退回当前v1。"""

        self.validate()
        return schema_version == self.schema_version and prompt_hash == self.prompt_hash

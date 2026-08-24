"""
@Time       : 2026/08/13 20:57
@Author     : zhanglp8181
@File       : input_parser_cli.py
@CallChain  : 独立parser worker → python -m input_parser_cli → 有界CSV JSON结果
@Description: 在独立进程解析不可信附件，只接受本地输入输出路径且不暴露网络或URL参数。
"""

from __future__ import annotations

import argparse
import json
import os
import stat
from pathlib import Path

from app.session.parser_sandbox import install_network_seccomp_filter


MAX_PARSER_INPUT_BYTES = 64 * 1024 * 1024


def main(argv: list[str] | None = None) -> int:
    """解析命令行指定附件并以独占新文件写入结构结果，不访问网络。"""

    parser = argparse.ArgumentParser(description="Parse a managed attachment")
    parser.add_argument("--format", required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input")
    source.add_argument("--input-fd", type=int)
    parser.add_argument("--output", required=True)
    arguments = parser.parse_args(argv)
    output_path = Path(arguments.output)
    try:
        install_network_seccomp_filter()
        from app.session.input_extraction import parse_attachment_bytes

        data = _read_input(input_path=arguments.input, input_fd=arguments.input_fd)
        elements = parse_attachment_bytes(arguments.format, data)
    except Exception as exc:
        error_code = (
            exc.code
            if hasattr(exc, "code")
            else "ATTACHMENT_PARSER_SANDBOX_UNAVAILABLE"
        )
        print(json.dumps({"error_code": error_code, "detail": str(exc)}, ensure_ascii=False))
        return 2
    payload = [
        {
            "element_type": item.element_type,
            "text": item.text,
            "table": item.table,
            "locator": item.locator,
            "row_count": item.row_count,
            "column_count": item.column_count,
            "truncated": item.truncated,
        }
        for item in elements
    ]
    descriptor = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    return 0


def _read_input(*, input_path: str | None, input_fd: int | None) -> bytes:
    """优先复核并读取父进程继承fd，路径入口仅保留独立CLI兼容。"""

    from app.session.input_extraction import InputExtractionError

    if input_fd is not None:
        try:
            info = os.fstat(input_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise InputExtractionError("ATTACHMENT_PARSER_INPUT_INVALID", "解析输入不可用。")
            os.lseek(input_fd, 0, os.SEEK_SET)
            with os.fdopen(os.dup(input_fd), "rb") as stream:
                data = stream.read(MAX_PARSER_INPUT_BYTES + 1)
        except InputExtractionError:
            raise
        except OSError as exc:
            raise InputExtractionError("ATTACHMENT_PARSER_INPUT_INVALID", "解析输入不可用。") from exc
    elif input_path is not None:
        try:
            data = Path(input_path).read_bytes()
        except OSError as exc:
            raise InputExtractionError("ATTACHMENT_PARSER_INPUT_INVALID", "解析输入不可用。") from exc
    else:
        raise InputExtractionError("ATTACHMENT_PARSER_INPUT_INVALID", "解析输入不可用。")
    if len(data) > MAX_PARSER_INPUT_BYTES:
        raise InputExtractionError("ATTACHMENT_PARSER_INPUT_TOO_LARGE", "解析输入超过安全预算。")
    return data


if __name__ == "__main__":
    raise SystemExit(main())

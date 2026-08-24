"""
@Time       : 2026/08/13 20:45
@Author     : zhanglp8181
@File       : input_extraction.py
@CallChain  : 附件上传 → ExtractionAttempt → 隔离parser → Published Extraction/Elements
@Description: 以追加作业、lease/fencing和原子发布管理附件结构提取，首个生产profile实现CSV。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from sqlalchemy import update
from sqlmodel import Session, select

from app.db.models import (
    InputDocumentElement,
    InputResourceExtraction,
    InputResourceExtractionAttempt,
    ManagedInputResource,
    SelectedResourceExtraction,
    ScannerEvidence,
    utc_now,
)


CSV_PARSER_NAME = "builtin-csv"
CSV_PARSER_VERSION = "1.0.0"
CSV_PROFILE_KEY = "default"
CSV_MAX_ROWS = 50_000
CSV_MAX_COLUMNS = 256
CSV_MAX_CELL_CHARS = 8_192
ARCHIVE_MAX_ENTRIES = 2_000
ARCHIVE_MAX_ENTRY_BYTES = 16 * 1024 * 1024
ARCHIVE_MAX_EXPANDED_BYTES = 64 * 1024 * 1024
ARCHIVE_MAX_RATIO = 100
IMAGE_MAX_PIXELS = 40_000_000
IMAGE_MAX_FRAMES = 32
PARSER_VERSIONS = {"xlsx": "1.1.0"}


class InputExtractionError(RuntimeError):
    """表示可稳定映射错误码的附件提取失败。"""

    def __init__(self, code: str, detail: str) -> None:
        """保存稳定错误码及脱敏说明，供worker和页面一致收敛。"""

        super().__init__(detail)
        self.code = code


@dataclass(frozen=True)
class ParsedElement:
    """表示隔离parser返回、尚未写入数据库的确定性元素。"""

    element_type: str
    text: str
    table: dict[str, object]
    locator: dict[str, object]
    row_count: int = 0
    column_count: int = 0
    truncated: bool = False


def csv_parser_config_checksum() -> str:
    """返回冻结CSV预算和编码策略的配置摘要。"""

    payload = {
        "parser": CSV_PARSER_NAME,
        "version": CSV_PARSER_VERSION,
        "max_rows": CSV_MAX_ROWS,
        "max_columns": CSV_MAX_COLUMNS,
        "max_cell_chars": CSV_MAX_CELL_CHARS,
        "encodings": ["utf-8-sig", "gb18030"],
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def parser_config_checksum(file_format: str) -> str:
    """返回指定格式、通用安全预算和实现版本的冻结配置摘要。"""

    if file_format == "csv":
        return csv_parser_config_checksum()
    payload = {
        "format": file_format,
        "parser": f"builtin-{file_format}",
        "version": PARSER_VERSIONS.get(file_format, "1.0.0"),
        "archive_max_entries": ARCHIVE_MAX_ENTRIES,
        "archive_max_entry_bytes": ARCHIVE_MAX_ENTRY_BYTES,
        "archive_max_expanded_bytes": ARCHIVE_MAX_EXPANDED_BYTES,
        "archive_max_ratio": ARCHIVE_MAX_RATIO,
        "image_max_pixels": IMAGE_MAX_PIXELS,
        "image_max_frames": IMAGE_MAX_FRAMES,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def parse_csv_bytes(data: bytes) -> list[ParsedElement]:
    """按确定性编码与行列预算解析CSV，公式只作不可信文本而不执行。"""

    text: str | None = None
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise InputExtractionError("ATTACHMENT_CSV_ENCODING_UNSUPPORTED", "CSV编码无法确认。")
    try:
        rows = list(csv.reader(io.StringIO(text, newline="")))
    except csv.Error as exc:
        raise InputExtractionError("ATTACHMENT_CSV_INVALID", "CSV结构无效。") from exc
    if not rows:
        raise InputExtractionError("ATTACHMENT_CSV_EMPTY", "CSV没有可读取行。")
    if len(rows) > CSV_MAX_ROWS:
        raise InputExtractionError("ATTACHMENT_TABLE_ROW_LIMIT", "CSV行数超过预算。")
    width = max(len(row) for row in rows)
    if width > CSV_MAX_COLUMNS:
        raise InputExtractionError("ATTACHMENT_TABLE_COLUMN_LIMIT", "CSV列数超过预算。")
    normalized = []
    for row in rows:
        cells = []
        for cell in row:
            if len(cell) > CSV_MAX_CELL_CHARS:
                raise InputExtractionError("ATTACHMENT_TABLE_CELL_LIMIT", "CSV单元格超过预算。")
            cells.append(cell)
        normalized.append(cells + [""] * (width - len(cells)))
    header = normalized[0]
    elements = [
        ParsedElement(
            element_type="table",
            text="\n".join(",".join(row) for row in normalized),
            table={"columns": header, "rows": normalized[1:]},
            locator={"kind": "csv", "row_start": 1, "row_end": len(normalized)},
            row_count=len(normalized),
            column_count=width,
        )
    ]
    return elements


def parse_text_bytes(data: bytes) -> list[ParsedElement]:
    """按确定性UTF-8/GB18030解码纯文本，并以单一段落Element保留稳定行定位。"""

    text: str | None = None
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise InputExtractionError("ATTACHMENT_TEXT_ENCODING_UNSUPPORTED", "文本编码无法确认。")
    if not text.strip():
        raise InputExtractionError("ATTACHMENT_TEXT_EMPTY", "文本附件没有可读取内容。")
    return [
        ParsedElement(
            element_type="document",
            text=text,
            table={},
            locator={"kind": "text", "line_start": 1, "line_end": len(text.splitlines()) or 1},
        )
    ]


def _safe_archive_entries(data: bytes) -> dict[str, bytes]:
    """在解析XML前执行OOXML路径、条目、展开量、压缩比和主动内容门禁。"""

    try:
        with ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > ARCHIVE_MAX_ENTRIES:
                raise InputExtractionError("ATTACHMENT_ARCHIVE_ENTRY_LIMIT", "Office文件条目过多。")
            expanded = 0
            result: dict[str, bytes] = {}
            normalized_names: set[str] = set()
            for info in infos:
                name = unicodedata.normalize("NFC", info.filename.replace("\\", "/"))
                normalized_name = name.casefold()
                if (
                    not name
                    or "\x00" in name
                    or name.startswith("/")
                    or re.match(r"^[a-zA-Z]:", name)
                    or ".." in name.split("/")
                    or normalized_name in normalized_names
                ):
                    raise InputExtractionError("ATTACHMENT_ARCHIVE_PATH_INVALID", "Office文件路径无效。")
                normalized_names.add(normalized_name)
                if info.flag_bits & 0x1:
                    raise InputExtractionError("ATTACHMENT_ARCHIVE_ENCRYPTED", "Office文件包含加密条目。")
                if info.file_size > ARCHIVE_MAX_ENTRY_BYTES:
                    raise InputExtractionError("ATTACHMENT_ARCHIVE_ENTRY_SIZE_LIMIT", "Office文件单项展开量过大。")
                expanded += info.file_size
                if expanded > ARCHIVE_MAX_EXPANDED_BYTES:
                    raise InputExtractionError("ATTACHMENT_ARCHIVE_SIZE_LIMIT", "Office文件展开量过大。")
                if info.compress_size and info.file_size / info.compress_size > ARCHIVE_MAX_RATIO:
                    raise InputExtractionError("ATTACHMENT_ARCHIVE_RATIO_LIMIT", "Office文件压缩比异常。")
                lowered = name.lower()
                if lowered.endswith((".zip", ".7z", ".rar", ".tar", ".gz")):
                    raise InputExtractionError("ATTACHMENT_ARCHIVE_NESTED_REJECTED", "Office文件包含嵌套归档。")
                if any(
                    marker in lowered
                    for marker in ("vbaproject", "activex/", "embeddings/", "oleobject")
                ):
                    raise InputExtractionError("ATTACHMENT_ACTIVE_CONTENT_REJECTED", "Office主动内容已拒绝。")
                payload = archive.read(info)
                if lowered.endswith((".xml", ".rels")) and re.search(
                    rb"\bDDE(?:AUTO)?\b", payload, re.IGNORECASE
                ):
                    raise InputExtractionError("ATTACHMENT_ACTIVE_CONTENT_REJECTED", "Office主动内容已拒绝。")
                if lowered.endswith(".rels") and re.search(
                    rb'TargetMode\s*=\s*["\']External["\']', payload, re.IGNORECASE
                ):
                    raise InputExtractionError("ATTACHMENT_EXTERNAL_RELATIONSHIP_REJECTED", "外部关系已拒绝。")
                result[name] = payload
            return result
    except BadZipFile as exc:
        raise InputExtractionError("ATTACHMENT_ARCHIVE_INVALID", "Office文件结构损坏。") from exc


def _xml_text(payload: bytes) -> list[str]:
    """从已通过预算的XML提取可见文本节点，不解析外部实体。"""

    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise InputExtractionError("ATTACHMENT_XML_INVALID", "Office XML结构无效。") from exc
    return [str(node.text) for node in root.iter() if node.text and node.tag.rsplit("}", 1)[-1] == "t"]


def parse_docx_bytes(data: bytes) -> list[ParsedElement]:
    """确定性提取DOCX正文及表格文本，所有内容继续标记为不可信数据。"""

    entries = _safe_archive_entries(data)
    payload = entries.get("word/document.xml")
    if payload is None:
        raise InputExtractionError("ATTACHMENT_DOCX_MAIN_MISSING", "DOCX缺少主文档。")
    texts = _xml_text(payload)
    return [
        ParsedElement(
            element_type="document",
            text="\n".join(texts),
            table={},
            locator={"kind": "docx", "part": "word/document.xml"},
        )
    ]


def parse_pptx_bytes(data: bytes) -> list[ParsedElement]:
    """按slide/notes稳定顺序提取PPTX文本，不冒充视觉渲染能力。"""

    entries = _safe_archive_entries(data)
    names = sorted(
        (
            name
            for name in entries
            if re.fullmatch(r"ppt/(slides/slide|notesSlides/notesSlide)\d+\.xml", name)
        ),
        key=lambda name: ("notesSlides" in name, int(re.search(r"(\d+)\.xml$", name).group(1))),
    )
    if not names:
        raise InputExtractionError("ATTACHMENT_PPTX_SLIDES_MISSING", "PPTX没有可读页面。")
    return [
        ParsedElement(
            element_type="note" if "notesSlides" in name else "slide",
            text="\n".join(_xml_text(entries[name])),
            table={},
            locator={
                "kind": "pptx",
                "part": name,
                "slide": int(re.search(r"(\d+)\.xml$", name).group(1)),
            },
        )
        for name in names
    ]


def _xlsx_column_index(cell_ref: str) -> int:
    """从A1引用提取零基列号，拒绝缺失或非法坐标。"""

    match = re.fullmatch(r"([A-Z]{1,3})[1-9]\d*", cell_ref.upper())
    if match is None:
        raise InputExtractionError("ATTACHMENT_XLSX_CELL_INVALID", "XLSX单元格坐标无效。")
    value = 0
    for character in match.group(1):
        value = value * 26 + ord(character) - 64
    return value - 1


def _xlsx_shared_strings(entries: dict[str, bytes]) -> list[str]:
    """按索引读取sharedStrings，缺失表允许返回空集合。"""

    payload = entries.get("xl/sharedStrings.xml")
    if payload is None:
        return []
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise InputExtractionError("ATTACHMENT_XML_INVALID", "XLSX共享字符串无效。") from exc
    return [
        "".join(
            str(node.text or "")
            for node in item.iter()
            if node.tag.rsplit("}", 1)[-1] == "t"
        )
        for item in root
        if item.tag.rsplit("}", 1)[-1] == "si"
    ]


def _xlsx_sheet_entries(entries: dict[str, bytes]) -> list[tuple[str, str, str]]:
    """通过workbook relationships冻结真实工作表顺序、名称和可见状态。"""

    workbook_payload = entries.get("xl/workbook.xml")
    relationships_payload = entries.get("xl/_rels/workbook.xml.rels")
    if workbook_payload is None or relationships_payload is None:
        fallback = sorted(
            (name for name in entries if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)),
            key=lambda name: int(re.search(r"(\d+)\.xml$", name).group(1)),
        )
        return [(name, f"Sheet{index}", "visible") for index, name in enumerate(fallback, 1)]
    try:
        workbook = ET.fromstring(workbook_payload)
        relationships = ET.fromstring(relationships_payload)
    except ET.ParseError as exc:
        raise InputExtractionError("ATTACHMENT_XML_INVALID", "XLSX工作簿关系无效。") from exc
    targets = {
        str(item.attrib.get("Id") or ""): str(item.attrib.get("Target") or "")
        for item in relationships
        if item.tag.rsplit("}", 1)[-1] == "Relationship"
    }
    result: list[tuple[str, str, str]] = []
    for sheet in workbook.iter():
        if sheet.tag.rsplit("}", 1)[-1] != "sheet":
            continue
        relation_id = next(
            (value for key, value in sheet.attrib.items() if key.rsplit("}", 1)[-1] == "id"),
            "",
        )
        target = targets.get(str(relation_id), "")
        if target.startswith("/") or ".." in target.split("/"):
            raise InputExtractionError("ATTACHMENT_ARCHIVE_PATH_INVALID", "XLSX工作表路径无效。")
        part = target if target.startswith("xl/") else f"xl/{target.lstrip('/')}"
        if part not in entries:
            raise InputExtractionError("ATTACHMENT_XLSX_SHEETS_MISSING", "XLSX工作表缺失。")
        result.append(
            (
                part,
                str(sheet.attrib.get("name") or f"Sheet{len(result) + 1}"),
                str(sheet.attrib.get("state") or "visible"),
            )
        )
    return result


def parse_xlsx_bytes(data: bytes) -> list[ParsedElement]:
    """按worksheet稳定顺序提取XLSX单元格值和公式文本，绝不计算公式。"""

    entries = _safe_archive_entries(data)
    sheet_entries = _xlsx_sheet_entries(entries)
    if not sheet_entries:
        raise InputExtractionError("ATTACHMENT_XLSX_SHEETS_MISSING", "XLSX没有可读工作表。")
    shared_strings = _xlsx_shared_strings(entries)
    elements: list[ParsedElement] = []
    for sheet_index, (name, sheet_name, sheet_state) in enumerate(sheet_entries, 1):
        try:
            root = ET.fromstring(entries[name])
        except ET.ParseError as exc:
            raise InputExtractionError("ATTACHMENT_XML_INVALID", "XLSX工作表无效。") from exc
        rows: list[list[str]] = []
        formulas: list[dict[str, object]] = []
        cells: list[dict[str, object]] = []
        seen_cells: set[str] = set()
        for row in (node for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "row"):
            if len(rows) >= CSV_MAX_ROWS:
                raise InputExtractionError(
                    "ATTACHMENT_TABLE_ROW_LIMIT",
                    "XLSX行数超过安全预算。",
                )
            values: list[str] = []
            for cell in (node for node in row if node.tag.rsplit("}", 1)[-1] == "c"):
                texts = [str(node.text or "") for node in cell.iter() if node.tag.rsplit("}", 1)[-1] == "t"]
                formula_node = next(
                    (node for node in cell if node.tag.rsplit("}", 1)[-1] == "f"),
                    None,
                )
                formula = str(formula_node.text or "") if formula_node is not None else ""
                value = next(
                    (str(node.text or "") for node in cell if node.tag.rsplit("}", 1)[-1] == "v"),
                    "",
                )
                cell_ref = str(cell.attrib.get("r") or "")
                normalized_ref = cell_ref.upper()
                if normalized_ref in seen_cells:
                    raise InputExtractionError(
                        "ATTACHMENT_XLSX_CELL_DUPLICATE",
                        "XLSX包含重复单元格坐标。",
                    )
                seen_cells.add(normalized_ref)
                cell_type = str(cell.attrib.get("t") or "number")
                if cell_type == "s" and value:
                    try:
                        rendered_value = shared_strings[int(value)]
                    except (IndexError, ValueError) as exc:
                        raise InputExtractionError(
                            "ATTACHMENT_XLSX_SHARED_STRING_INVALID",
                            "XLSX共享字符串索引无效。",
                        ) from exc
                elif cell_type == "b" and value:
                    rendered_value = "true" if value == "1" else "false"
                else:
                    rendered_value = "".join(texts) or value
                rendered = rendered_value or (f"={formula}" if formula else "")
                cell_fact = {
                    "cell": cell_ref,
                    "raw_value": rendered,
                    "cached_value": value if formula and value != "" else None,
                    "value_type": cell_type,
                    "formula": formula or None,
                    "formula_checksum": (
                        hashlib.sha256(formula.encode("utf-8")).hexdigest()
                        if formula
                        else None
                    ),
                    "cache_missing": bool(formula and value == ""),
                    "formula_type": (
                        str(formula_node.attrib.get("t") or "normal")
                        if formula_node is not None
                        else None
                    ),
                    "formula_ref": (
                        str(formula_node.attrib.get("ref") or "") or None
                        if formula_node is not None
                        else None
                    ),
                }
                cell_fact["cell_checksum"] = hashlib.sha256(
                    json.dumps(cell_fact, ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest()
                cells.append(cell_fact)
                if formula:
                    formulas.append(cell_fact)
                column_index = _xlsx_column_index(normalized_ref)
                if column_index >= CSV_MAX_COLUMNS:
                    raise InputExtractionError(
                        "ATTACHMENT_TABLE_COLUMN_LIMIT",
                        "XLSX列数超过安全预算。",
                    )
                if len(values) <= column_index:
                    values.extend("" for _ in range(column_index + 1 - len(values)))
                values[column_index] = rendered
            rows.append(values)
        width = max((len(row) for row in rows), default=0)
        elements.append(
            ParsedElement(
                element_type="table",
                text="\n".join(",".join(row) for row in rows),
                table={
                    "columns": rows[0] if rows else [],
                    "rows": rows[1:],
                    "formulas": formulas,
                    "cells": cells,
                    "sheet_name": sheet_name,
                    "sheet_ordinal": sheet_index,
                    "sheet_visibility": sheet_state,
                },
                locator={
                    "kind": "xlsx",
                    "sheet_index": sheet_index,
                    "sheet_name": sheet_name,
                    "sheet_visibility": sheet_state,
                    "part": name,
                },
                row_count=len(rows),
                column_count=width,
            )
        )
    return elements


def parse_pdf_bytes(data: bytes) -> list[ParsedElement]:
    """拒绝主动PDF对象后按页提取嵌入文本；扫描页明确返回文本不可用。"""

    if not data.startswith(b"%PDF-"):
        raise InputExtractionError("ATTACHMENT_TYPE_MISMATCH", "文件内容不是PDF。")
    if re.search(rb"/(JavaScript|JS|Launch|EmbeddedFile|URI)\b", data):
        raise InputExtractionError("ATTACHMENT_PDF_ACTIVE_CONTENT_REJECTED", "PDF主动内容已拒绝。")
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data), strict=True)
        elements = []
        for page_number, page in enumerate(reader.pages, 1):
            text = str(page.extract_text() or "").strip()
            if text:
                elements.append(
                    ParsedElement(
                        element_type="page",
                        text=text,
                        table={},
                        locator={"kind": "pdf", "page": page_number},
                    )
                )
        if not elements:
            raise InputExtractionError("ATTACHMENT_PDF_TEXT_UNAVAILABLE", "PDF没有可提取文字，需要OCR。")
        return elements
    except InputExtractionError:
        raise
    except Exception as exc:
        raise InputExtractionError("ATTACHMENT_PDF_INVALID", "PDF结构无效。") from exc


def parse_image_bytes(data: bytes) -> list[ParsedElement]:
    """在decode前后核对图片格式、像素及帧数，并仅发布剥离EXIF后的尺寸证据。"""

    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            frames = int(getattr(image, "n_frames", 1))
            if width * height > IMAGE_MAX_PIXELS or frames > IMAGE_MAX_FRAMES:
                raise InputExtractionError("ATTACHMENT_IMAGE_BUDGET_EXCEEDED", "图片尺寸超过预算。")
            image.verify()
            return [
                ParsedElement(
                    element_type="image",
                    text=f"Image {width}x{height}; OCR not requested",
                    table={},
                    locator={"kind": "image", "width": width, "height": height, "frame": 1},
                )
            ]
    except InputExtractionError:
        raise
    except Exception as exc:
        raise InputExtractionError("ATTACHMENT_IMAGE_INVALID", "图片结构无效。") from exc


def sanitize_image_bytes_for_provider(data: bytes) -> bytes:
    """把首帧重编码为无EXIF的确定性PNG，供模型视觉输入而非直接外发原始图片。"""

    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            frames = int(getattr(image, "n_frames", 1))
            if width * height > IMAGE_MAX_PIXELS or frames > IMAGE_MAX_FRAMES:
                raise InputExtractionError("ATTACHMENT_IMAGE_BUDGET_EXCEEDED", "图片尺寸超过预算。")
            image.seek(0)
            frame = image.convert("RGBA" if "A" in image.getbands() else "RGB")
            output = io.BytesIO()
            frame.save(output, format="PNG", optimize=False, compress_level=9)
            return output.getvalue()
    except InputExtractionError:
        raise
    except Exception as exc:
        raise InputExtractionError("ATTACHMENT_IMAGE_INVALID", "图片结构无效。") from exc


def parse_attachment_bytes(file_format: str, data: bytes) -> list[ParsedElement]:
    """把白名单格式路由到确定性parser，未知格式稳定fail closed。"""

    parsers = {
        "text": parse_text_bytes,
        "csv": parse_csv_bytes,
        "docx": parse_docx_bytes,
        "pptx": parse_pptx_bytes,
        "xlsx": parse_xlsx_bytes,
        "pdf": parse_pdf_bytes,
        "image": parse_image_bytes,
    }
    parser = parsers.get(file_format)
    if parser is None:
        raise InputExtractionError("ATTACHMENT_FORMAT_UNSUPPORTED", "当前格式尚不支持分析。")
    return parser(data)


class InputExtractionService:
    """在一个数据库事务中管理解析作业claim、失败和不可变结果发布。"""

    def __init__(self, db: Session) -> None:
        """绑定调用方事务；网络或文件解析必须在claim提交后于受限worker执行。"""

        self.db = db

    def ensure_csv_attempt(self, resource: ManagedInputResource) -> InputResourceExtractionAttempt:
        """幂等返回同配置的pending/succeeded attempt，失败时追加下一个attempt。"""

        return self.ensure_attempt(resource, file_format="csv")

    def ensure_attempt(
        self,
        resource: ManagedInputResource,
        *,
        file_format: str,
    ) -> InputResourceExtractionAttempt:
        """按格式与parser配置幂等创建追加式Attempt，失败重试不得覆盖旧事实。"""

        checksum = parser_config_checksum(file_format)
        attempts = self.db.exec(
            select(InputResourceExtractionAttempt)
            .where(
                InputResourceExtractionAttempt.tenant_id == resource.tenant_id,
                InputResourceExtractionAttempt.resource_id == resource.id,
                InputResourceExtractionAttempt.resource_version == resource.version,
                InputResourceExtractionAttempt.parser_config_checksum == checksum,
            )
            .order_by(InputResourceExtractionAttempt.attempt_no.desc())
        ).all()
        if attempts and attempts[0].status in {"pending", "claimed", "running", "succeeded"}:
            return attempts[0]
        attempt = InputResourceExtractionAttempt(
            tenant_id=resource.tenant_id,
            resource_id=resource.id,
            resource_version=resource.version,
            parser_name=CSV_PARSER_NAME if file_format == "csv" else f"builtin-{file_format}",
            parser_version=(
                CSV_PARSER_VERSION
                if file_format == "csv"
                else PARSER_VERSIONS.get(file_format, "1.0.0")
            ),
            parser_config_checksum=checksum,
            attempt_no=(attempts[0].attempt_no + 1) if attempts else 1,
        )
        self.db.add(attempt)
        self.db.flush()
        return attempt

    def claim(
        self,
        attempt: InputResourceExtractionAttempt,
        *,
        worker_id: str,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> InputResourceExtractionAttempt:
        """以状态、owner和到期条件CAS领取解析作业并推进fencing token。"""

        current = now or utc_now()
        result = self.db.exec(
            update(InputResourceExtractionAttempt)
            .where(
                InputResourceExtractionAttempt.id == attempt.id,
                InputResourceExtractionAttempt.status == "pending",
            )
            .values(
                status="claimed",
                lease_owner=worker_id,
                fencing_token=InputResourceExtractionAttempt.fencing_token + 1,
                lease_expires_at=current + timedelta(seconds=lease_seconds),
                started_at=current,
            )
        )
        if result.rowcount != 1:
            raise InputExtractionError("ATTACHMENT_EXTRACTION_ALREADY_CLAIMED", "解析作业已被领取。")
        self.db.flush()
        self.db.refresh(attempt)
        return attempt

    def publish_csv(
        self,
        attempt: InputResourceExtractionAttempt,
        resource: ManagedInputResource,
        elements: list[ParsedElement],
        *,
        worker_id: str,
        fencing_token: int,
    ) -> InputResourceExtraction:
        """仅由当前lease owner原子发布Extraction、Elements并更新selected指针。"""

        return self.publish(
            attempt,
            resource,
            elements,
            file_format="csv",
            worker_id=worker_id,
            fencing_token=fencing_token,
        )

    def publish(
        self,
        attempt: InputResourceExtractionAttempt,
        resource: ManagedInputResource,
        elements: list[ParsedElement],
        *,
        file_format: str,
        worker_id: str,
        fencing_token: int,
    ) -> InputResourceExtraction:
        """以通用原子事务发布任一白名单格式的Extraction和全部Elements。"""

        self.db.refresh(attempt)
        self.db.refresh(resource)
        if (
            attempt.status != "claimed"
            or attempt.lease_owner != worker_id
            or attempt.fencing_token != fencing_token
            or (attempt.lease_expires_at and attempt.lease_expires_at <= utc_now())
        ):
            raise InputExtractionError("ATTACHMENT_EXTRACTION_FENCED", "解析作业租约已失效。")
        expected_acl_revision = resource.acl_revision
        if (
            resource.access_status != "active"
            or resource.revoked_at is not None
            or resource.destruction_status not in {"retained", "held"}
            or resource.ingestion_status == "revoked"
        ):
            raise InputExtractionError("ATTACHMENT_EXTRACTION_COUNTERMANDED", "附件已被撤销。")
        element_payloads = []
        for index, item in enumerate(elements):
            element_checksum = hashlib.sha256(
                json.dumps(
                    {
                        "type": item.element_type,
                        "text": item.text,
                        "table": item.table,
                        "locator": item.locator,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            element_payloads.append((index, item, element_checksum))
        manifest_checksum = hashlib.sha256(
            "".join(item[2] for item in element_payloads).encode("ascii")
        ).hexdigest()
        extraction_checksum = hashlib.sha256(
            f"{resource.content_checksum}:{attempt.parser_config_checksum}:{manifest_checksum}".encode()
        ).hexdigest()
        existing = self.db.exec(
            select(InputResourceExtraction).where(
                InputResourceExtraction.tenant_id == resource.tenant_id,
                InputResourceExtraction.resource_id == resource.id,
                InputResourceExtraction.resource_version == resource.version,
                InputResourceExtraction.parser_config_checksum == attempt.parser_config_checksum,
                InputResourceExtraction.extraction_checksum == extraction_checksum,
            )
        ).first()
        if existing is None:
            existing = InputResourceExtraction(
                tenant_id=resource.tenant_id,
                resource_id=resource.id,
                resource_version=resource.version,
                content_checksum=resource.content_checksum,
                parser_name=attempt.parser_name,
                parser_version=attempt.parser_version,
                parser_config_checksum=attempt.parser_config_checksum,
                extraction_checksum=extraction_checksum,
                element_manifest_checksum=manifest_checksum,
                published_from_attempt_id=attempt.id,
                element_count=len(element_payloads),
                page_count=sum(item.element_type == "page" for item in elements),
                sheet_count=sum(
                    item.locator.get("kind") == "xlsx" for item in elements
                ),
                slide_count=sum(item.element_type == "slide" for item in elements),
                metadata_json={"format": file_format},
            )
            self.db.add(existing)
            self.db.flush()
            for index, item, checksum in element_payloads:
                self.db.add(
                    InputDocumentElement(
                        tenant_id=resource.tenant_id,
                        extraction_id=existing.id,
                        element_index=index,
                        element_type=item.element_type,
                        text=item.text,
                        table_json=item.table,
                        locator_json=item.locator,
                        content_checksum=checksum,
                        char_count=len(item.text),
                        row_count=item.row_count,
                        column_count=item.column_count,
                        truncated=item.truncated,
                    )
                )
        selected = self.db.exec(
            select(SelectedResourceExtraction).where(
                SelectedResourceExtraction.tenant_id == resource.tenant_id,
                SelectedResourceExtraction.resource_id == resource.id,
                SelectedResourceExtraction.resource_version == resource.version,
                SelectedResourceExtraction.profile_key == CSV_PROFILE_KEY,
            )
        ).first()
        if selected is None:
            selected = SelectedResourceExtraction(
                tenant_id=resource.tenant_id,
                resource_id=resource.id,
                resource_version=resource.version,
                profile_key=CSV_PROFILE_KEY,
                extraction_id=existing.id,
            )
        else:
            selected.extraction_id = existing.id
            selected.revision += 1
            selected.updated_at = utc_now()
        self.db.add(selected)
        attempt.status = "succeeded"
        attempt.temporary_manifest_checksum = manifest_checksum
        attempt.finished_at = utc_now()
        attempt.lease_owner = None
        attempt.lease_expires_at = None
        self.db.add(attempt)
        resource_update = self.db.exec(
            update(ManagedInputResource)
            .where(
                ManagedInputResource.id == resource.id,
                ManagedInputResource.tenant_id == resource.tenant_id,
                ManagedInputResource.version == resource.version,
                ManagedInputResource.access_status == "active",
                ManagedInputResource.revoked_at.is_(None),
                ManagedInputResource.destruction_status.in_(("retained", "held")),
                ManagedInputResource.acl_revision == expected_acl_revision,
            )
            .values(
                ingestion_status="ready",
                security_status="format_verified",
                extraction_checksum=existing.extraction_checksum,
                extracted_text="\n".join(item.text for item in elements),
                extraction_metadata_json={
                    **dict(resource.extraction_metadata_json or {}),
                    "preview": "\n\n".join(item.text for item in elements)[:4000],
                    "pipeline": ["uploaded", "scanning", "extracting", "ready"],
                    "error": None,
                },
                updated_at=utc_now(),
            )
        )
        if resource_update.rowcount != 1:
            raise InputExtractionError("ATTACHMENT_EXTRACTION_COUNTERMANDED", "附件已被撤销。")
        self.db.refresh(resource)
        scan_policy = hashlib.sha256(b"format-verified-v1:max-age=0").hexdigest()
        evidence = self.db.exec(
            select(ScannerEvidence).where(
                ScannerEvidence.tenant_id == resource.tenant_id,
                ScannerEvidence.resource_id == resource.id,
                ScannerEvidence.resource_version == resource.version,
                ScannerEvidence.engine == "builtin-format-verifier",
                ScannerEvidence.definition_version == "1.0.0",
            )
        ).first()
        if evidence is None:
            checked_at = utc_now()
            self.db.add(
                ScannerEvidence(
                    tenant_id=resource.tenant_id,
                    resource_id=resource.id,
                    resource_version=resource.version,
                    assurance_level="format_verified",
                    engine="builtin-format-verifier",
                    engine_version="1.0.0",
                    definition_version="1.0.0",
                    definition_published_at=checked_at,
                    scanned_at=checked_at,
                    freshness_policy_checksum=scan_policy,
                    max_age_at_scan_seconds=0,
                    verdict="accepted",
                    evidence_json={"format": file_format},
                )
            )
        self.db.flush()
        return existing

    def fail(
        self,
        attempt: InputResourceExtractionAttempt,
        *,
        worker_id: str,
        fencing_token: int,
        error: InputExtractionError,
    ) -> None:
        """以当前fencing身份终结失败作业，保留稳定错误码且不发布半成品。"""

        self.db.refresh(attempt)
        if (
            attempt.status != "claimed"
            or attempt.lease_owner != worker_id
            or attempt.fencing_token != fencing_token
        ):
            raise InputExtractionError("ATTACHMENT_EXTRACTION_FENCED", "解析作业租约已失效。")
        attempt.status = "failed"
        attempt.error_code = error.code
        attempt.error_detail_json = {"detail": str(error)[:500]}
        attempt.finished_at = utc_now()
        attempt.lease_owner = None
        attempt.lease_expires_at = None
        self.db.add(attempt)
        self.db.flush()


def load_managed_blob(resource: ManagedInputResource, storage_root: Path) -> bytes:
    """在给定受管根下读取内容寻址blob并校验路径和checksum。"""

    root = storage_root.resolve()
    candidate = (root / resource.storage_locator).resolve()
    if root not in candidate.parents:
        raise InputExtractionError("ATTACHMENT_STORAGE_INVALID", "输入资源路径无效。")
    data = candidate.read_bytes()
    if hashlib.sha256(data).hexdigest() != resource.content_checksum:
        raise InputExtractionError("ATTACHMENT_CHECKSUM_MISMATCH", "输入资源校验失败。")
    return data

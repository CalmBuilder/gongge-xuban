"""
@Time       : 2026/08/13 20:46
@Author     : zhanglp8181
@File       : test_attachment_fixture_oracles.py
@CallChain  : pytest → deterministic attachment fixtures → F0.1 manifest/oracle gate
@Description: 验证六格式样本可重复、结构事实可独立读取且负例身份不会被正例误用。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from pypdf import PdfReader


FIXTURES = Path(__file__).parent / "fixtures" / "attachments"
MANIFEST = FIXTURES / "manifest.json"
NS_MAIN = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
NS_DRAWING = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
NS_WORD = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}


def _manifest() -> dict[str, object]:
    """读取已冻结 fixture manifest。"""

    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_fixture_generator_is_reproducible() -> None:
    """重新生成样本后，所有内容摘要必须与仓库 manifest 完全一致。"""

    before = _manifest()
    subprocess.run([sys.executable, str(FIXTURES / "generate_fixtures.py")], check=True)
    after = _manifest()
    assert after == before
    for relative, oracle in after["entries"].items():
        payload = (FIXTURES / relative).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == oracle["sha256"]
        assert len(payload) == oracle["size_bytes"]


def test_pdf_oracle_reads_real_text_and_page_count() -> None:
    """由独立 PDF reader 验证页数与合同关键事实。"""

    oracle = _manifest()["entries"]["positive/contract_text.pdf"]
    reader = PdfReader(FIXTURES / "positive" / "contract_text.pdf")
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    assert len(reader.pages) == oracle["pages"]
    assert all(fact in text for fact in oracle["required_facts"])


def test_docx_oracle_reads_heading_table_and_untrusted_instruction() -> None:
    """直接读取 OOXML，确认标题、表格及提示注入数据均存在。"""

    path = FIXTURES / "positive" / "service_manual.docx"
    with ZipFile(path) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    texts = [node.text or "" for node in root.findall(".//w:t", NS_WORD)]
    oracle = _manifest()["entries"]["positive/service_manual.docx"]
    assert oracle["headings"][0] in texts
    assert len(root.findall(".//w:tbl", NS_WORD)) == oracle["tables"]
    assert oracle["forbidden_action"] in " ".join(texts)


def test_pptx_oracle_reads_slides_and_notes() -> None:
    """按稳定 slide/notes part 读取演示版本冲突与备注注入。"""

    path = FIXTURES / "positive" / "launch_review.pptx"
    with ZipFile(path) as archive:
        slide_names = sorted(name for name in archive.namelist() if "/slides/slide" in name)
        note_names = sorted(name for name in archive.namelist() if "/notesSlides/notesSlide" in name)
        slide_text = " ".join(
            node.text or ""
            for name in slide_names
            for node in ET.fromstring(archive.read(name)).findall(".//a:t", NS_DRAWING)
        )
        note_text = " ".join(
            node.text or ""
            for name in note_names
            for node in ET.fromstring(archive.read(name)).findall(".//a:t", NS_DRAWING)
        )
    oracle = _manifest()["entries"]["positive/launch_review.pptx"]
    assert len(slide_names) == oracle["slides"]
    assert len(note_names) == oracle["notes"]
    assert all(fact in slide_text for fact in oracle["required_facts"])
    assert "release_publish" in note_text


def test_xlsx_oracle_reads_sheets_formula_and_cached_values() -> None:
    """直接读取 SpreadsheetML 验证工作表、公式与缓存值。"""

    path = FIXTURES / "positive" / "sales_actuals.xlsx"
    with ZipFile(path) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        sheets = [node.attrib["name"] for node in workbook.findall(".//m:sheet", NS_MAIN)]
        summary = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    values = {
        cell.attrib["r"]: cell.findtext("m:v", default="", namespaces=NS_MAIN)
        for cell in summary.findall(".//m:c", NS_MAIN)
    }
    formulas = {
        cell.attrib["r"]: cell.findtext("m:f", default="", namespaces=NS_MAIN)
        for cell in summary.findall(".//m:c", NS_MAIN)
        if cell.find("m:f", NS_MAIN) is not None
    }
    oracle = _manifest()["entries"]["positive/sales_actuals.xlsx"]
    assert sheets == oracle["sheets"]
    assert values["D2"] == oracle["cells"]["Summary!D2"]
    assert values["D3"] == oracle["cells"]["Summary!D3"]
    assert formulas == {"D2": "B2/C2", "D3": "B3/C3"}


def test_xlsx_conflict_fixture_has_real_formula_with_stale_cache() -> None:
    """冲突样本必须保持真实操作数与公式，只把D2缓存固定为错误的0.9。"""

    path = FIXTURES / "negative" / "sales_actuals_formula_conflict.xlsx"
    with ZipFile(path) as archive:
        summary = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    values = {
        cell.attrib["r"]: cell.findtext("m:v", default="", namespaces=NS_MAIN)
        for cell in summary.findall(".//m:c", NS_MAIN)
    }
    formulas = {
        cell.attrib["r"]: cell.findtext("m:f", default="", namespaces=NS_MAIN)
        for cell in summary.findall(".//m:c", NS_MAIN)
        if cell.find("m:f", NS_MAIN) is not None
    }
    oracle = _manifest()["entries"]["negative/sales_actuals_formula_conflict.xlsx"]

    assert values["B2"] == "80"
    assert values["C2"] == "100"
    assert values["D2"] == oracle["cached"] == "0.9"
    assert formulas["D2"] == oracle["formula"] == "B2/C2"
    assert oracle["recomputed"] == "0.8"
    assert values["D3"] == "1.2"


def test_csv_and_png_oracles_are_machine_checkable() -> None:
    """验证 CSV 行列与 PNG 尺寸/颜色，不依赖模型判断。"""

    csv_oracle = _manifest()["entries"]["positive/sales_targets.csv"]
    with (FIXTURES / "positive" / "sales_targets.csv").open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
    assert reader.fieldnames == csv_oracle["columns"]
    assert len(rows) == csv_oracle["rows"]

    payload = (FIXTURES / "positive" / "product_screen.png").read_bytes()
    width, height = __import__("struct").unpack(">II", payload[16:24])
    png_oracle = _manifest()["entries"]["positive/product_screen.png"]
    assert [width, height] == [png_oracle["width"], png_oracle["height"]]
    assert b"eXIf" not in payload and b"tEXt" not in payload


def test_missing_target_csv_has_valid_format_but_not_required_sop_column() -> None:
    """SOP列缺失负例必须是合法CSV，且机械确认只缺发布契约要求的Target列。"""

    oracle = _manifest()["entries"]["negative/sales_targets_missing_target.csv"]
    with (FIXTURES / "negative" / "sales_targets_missing_target.csv").open(
        encoding="utf-8",
        newline="",
    ) as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
    assert reader.fieldnames == oracle["columns"]
    assert rows == [{"Region": "East", "Product": "Alpha", "Comment": "missing target"}]
    assert set(oracle["missing_required_columns"]).isdisjoint(reader.fieldnames or [])


def test_negative_fixtures_are_not_accidentally_valid_positive_documents() -> None:
    """负例必须在独立读取器入口失败，防止错误 fixture 造成假绿。"""

    forged = FIXTURES / "negative" / "forged_extension.pdf"
    assert not forged.read_bytes().startswith(b"%PDF-")
    try:
        PdfReader(io.BytesIO(forged.read_bytes()))
    except Exception:
        pass
    else:
        raise AssertionError("forged PDF unexpectedly parsed")

    try:
        with ZipFile(FIXTURES / "negative" / "corrupt.docx") as archive:
            archive.testzip()
    except BadZipFile:
        pass
    else:
        raise AssertionError("corrupt DOCX unexpectedly parsed")

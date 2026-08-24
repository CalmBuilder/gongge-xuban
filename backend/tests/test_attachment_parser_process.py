"""
@Time       : 2026/08/13 21:02
@Author     : zhanglp8181
@File       : test_attachment_parser_process.py
@CallChain  : pytest → 独立parser进程 → CSV Elements/资源预算
@Description: 正反向验证CSV真实文件隔离解析、公式不执行、编码与行列预算错误收敛。
"""

import hashlib
from io import BytesIO
import os
from pathlib import Path
import subprocess
import sys
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import pytest

from app.session.input_extraction import (
    InputExtractionError,
    _safe_archive_entries,
    parse_csv_bytes,
    parse_xlsx_bytes,
    sanitize_image_bytes_for_provider,
)
from app.session.input_parser_process import (
    _parser_command,
    run_attachment_parser_fd_isolated,
    run_attachment_parser_isolated,
    run_csv_parser_isolated,
)
from app.security.managed_storage import (
    ManagedStorageError,
    managed_open_read_fd,
    managed_write_bytes,
)


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "attachments"


def _archive_bytes(
    entries: list[tuple[str, bytes]],
    *,
    compression: int = ZIP_DEFLATED,
) -> bytes:
    """生成允许重复名称和自定义压缩方式的最小归档攻击样本。"""

    payload = BytesIO()
    with ZipFile(payload, "w", compression) as archive:
        for name, value in entries:
            archive.writestr(name, value)
    return payload.getvalue()


def test_frozen_parser_reuses_desktop_executable(monkeypatch, tmp_path: Path) -> None:
    """冻结安装包不得把桌面主程序误当Python解释器执行-m，而应进入专用CLI模式。"""

    monkeypatch.setattr("app.session.input_parser_process.paths.is_frozen", lambda: True)
    monkeypatch.setattr("app.session.input_parser_process.sys.executable", "/opt/gongge-xuban")

    command = _parser_command(
        file_format="pdf",
        input_path=tmp_path / "input.pdf",
        output_path=tmp_path / "output.json",
    )

    assert command[:3] == ["/opt/gongge-xuban", "--input-parser", "--format"]
    assert "-m" not in command


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux seccomp")
def test_parser_network_filter_rejects_loopback_socket() -> None:
    """Linux parser隔离必须在内核层拒绝socket，NO_PROXY环境变量不能算禁网。"""

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import socket; "
                "from app.session.parser_sandbox import install_network_seccomp_filter; "
                "install_network_seccomp_filter(); "
                "socket.socket(socket.AF_INET, socket.SOCK_STREAM)"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "PermissionError" in completed.stderr


@pytest.mark.skipif(os.name != "posix", reason="POSIX parser process isolation")
def test_real_parser_timeout_kills_child_and_removes_output_directory(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """真实超时必须杀死parser子进程，并清除隔离输出目录与半成品。"""

    input_path = tmp_path / "input.csv"
    input_path.write_bytes(b"Region,Target\nEast,100\n")
    descriptor = os.open(input_path, os.O_RDONLY | os.O_NOFOLLOW)
    pid_path = tmp_path / "parser.pid"

    def sleeping_command(**_kwargs) -> list[str]:
        """返回会先落PID再长睡眠的真实子进程命令，供超时回收断言。"""

        script = (
            "import os,time;from pathlib import Path;"
            f"Path({str(pid_path)!r}).write_text(str(os.getpid()));"
            "time.sleep(60)"
        )
        return [sys.executable, "-c", script]

    monkeypatch.setattr("app.session.input_parser_process._parser_command", sleeping_command)
    monkeypatch.setattr("app.session.input_parser_process.tempfile.tempdir", str(tmp_path))
    try:
        with pytest.raises(InputExtractionError) as exc_info:
            run_attachment_parser_fd_isolated(
                descriptor,
                file_format="csv",
                timeout_seconds=1,
                memory_mb=256,
            )
    finally:
        os.close(descriptor)

    assert exc_info.value.code == "ATTACHMENT_PARSER_TIMEOUT"
    child_pid = int(pid_path.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
    assert list(tmp_path.glob("gongge-input-parser-output-*")) == []


def test_real_csv_is_parsed_in_isolated_process() -> None:
    """真实CSV经独立子进程返回稳定表头、行号locator与二维数据。"""

    elements = run_csv_parser_isolated(
        (FIXTURE_ROOT / "positive" / "sales_targets.csv").read_bytes(),
        timeout_seconds=10,
        memory_mb=256,
    )
    assert len(elements) == 1
    assert elements[0].element_type == "table"
    assert elements[0].table["columns"] == ["Region", "Product", "Target"]
    assert elements[0].locator == {"kind": "csv", "row_start": 1, "row_end": 3}


def test_csv_formula_is_preserved_as_untrusted_text() -> None:
    """公式注入载荷保持原始字符串，解析器不得求值或触发外部命令。"""

    elements = parse_csv_bytes(
        (FIXTURE_ROOT / "negative" / "external_formula.csv").read_bytes()
    )
    rows = elements[0].table["rows"]
    assert rows[0][1].startswith("=HYPERLINK(")


def test_empty_and_overwide_csv_fail_with_stable_codes() -> None:
    """空CSV和超列预算均返回稳定错误码，不留下半成品元素。"""

    with pytest.raises(InputExtractionError, match="CSV没有可读取行") as empty:
        parse_csv_bytes(b"")
    assert empty.value.code == "ATTACHMENT_CSV_EMPTY"
    overwide = ",".join(str(index) for index in range(257)).encode()
    with pytest.raises(InputExtractionError, match="列数超过") as columns:
        parse_csv_bytes(overwide)
    assert columns.value.code == "ATTACHMENT_TABLE_COLUMN_LIMIT"


@pytest.mark.parametrize(
    ("filename", "file_format", "expected_type", "expected_text"),
    [
        ("contract_text.pdf", "pdf", "page", "Renewal notice: 60 days"),
        ("service_manual.docx", "docx", "document", "Service Manual 2.4"),
        ("launch_review.pptx", "pptx", "slide", "Version 2.4"),
        ("sales_actuals.xlsx", "xlsx", "table", "Region,Actual,Target,Completion"),
        ("product_screen.png", "image", "image", "Image 64x40"),
    ],
)
def test_real_supported_formats_are_parsed_in_isolated_process(
    filename: str,
    file_format: str,
    expected_type: str,
    expected_text: str,
) -> None:
    """真实PDF、Office和图片均经同一受限子进程产生结构元素与locator。"""

    elements = run_attachment_parser_isolated(
        (FIXTURE_ROOT / "positive" / filename).read_bytes(),
        file_format=file_format,
        timeout_seconds=10,
        memory_mb=256,
    )

    assert elements[0].element_type == expected_type
    assert any(expected_text in item.text for item in elements)


def test_provider_image_projection_reencodes_first_frame_without_exif() -> None:
    """视觉模型只能收到无EXIF的确定性PNG派生物，不能外发原始设备元数据。"""

    from PIL import Image

    source = BytesIO()
    image = Image.new("RGB", (2, 2), (10, 20, 30))
    exif = Image.Exif()
    exif[0x010F] = "Sensitive Camera"
    exif[0x0110] = "Device Serial Model"
    image.save(source, format="JPEG", exif=exif)

    sanitized = sanitize_image_bytes_for_provider(source.getvalue())

    assert sanitized.startswith(b"\x89PNG\r\n\x1a\n")
    assert b"Sensitive Camera" not in sanitized
    assert b"Device Serial Model" not in sanitized
    with Image.open(BytesIO(sanitized)) as projected:
        assert projected.format == "PNG"
        assert projected.size == (2, 2)
        assert not projected.getexif()


def test_image_pixel_and_frame_budgets_fail_before_provider_projection(monkeypatch) -> None:
    """解码后的像素与多帧预算必须分别阻断，不能只相信压缩前字节数。"""

    from PIL import Image

    image = Image.new("RGB", (2, 2), (255, 0, 0))
    still = BytesIO()
    image.save(still, format="PNG")
    monkeypatch.setattr("app.session.input_extraction.IMAGE_MAX_PIXELS", 3)
    with pytest.raises(InputExtractionError) as pixel_error:
        sanitize_image_bytes_for_provider(still.getvalue())
    assert pixel_error.value.code == "ATTACHMENT_IMAGE_BUDGET_EXCEEDED"

    monkeypatch.setattr("app.session.input_extraction.IMAGE_MAX_PIXELS", 100)
    monkeypatch.setattr("app.session.input_extraction.IMAGE_MAX_FRAMES", 1)
    animated = BytesIO()
    image.save(
        animated,
        format="GIF",
        save_all=True,
        append_images=[Image.new("RGB", (2, 2), (0, 255, 0))],
        duration=100,
        loop=0,
    )
    with pytest.raises(InputExtractionError) as frame_error:
        sanitize_image_bytes_for_provider(animated.getvalue())
    assert frame_error.value.code == "ATTACHMENT_IMAGE_BUDGET_EXCEEDED"


@pytest.mark.parametrize("active_name", ("JavaScript", "Launch", "EmbeddedFile", "URI"))
def test_pdf_active_object_matrix_is_rejected_before_reader(active_name: str) -> None:
    """PDF脚本、启动动作、嵌入文件及远程URI均在reader解析前稳定拒绝。"""

    payload = (FIXTURE_ROOT / "positive" / "contract_text.pdf").read_bytes().replace(
        b"%%EOF",
        f"/{active_name}\n%%EOF".encode(),
    )

    with pytest.raises(InputExtractionError) as exc_info:
        run_attachment_parser_isolated(
            payload,
            file_format="pdf",
            timeout_seconds=10,
            memory_mb=256,
        )

    assert exc_info.value.code == "ATTACHMENT_PDF_ACTIVE_CONTENT_REJECTED"


def test_xlsx_parser_freezes_formula_cache_and_checksum_without_executing() -> None:
    """真实XLSX提取必须并列保存公式、缓存值与校验和，但parser本身绝不求值。"""

    elements = parse_xlsx_bytes(
        (FIXTURE_ROOT / "positive" / "sales_actuals.xlsx").read_bytes()
    )
    formulas = {
        item["cell"]: item
        for element in elements
        for item in element.table.get("formulas", [])
    }

    assert formulas["D2"]["formula"] == "B2/C2"
    assert formulas["D2"]["cached_value"] == "0.8"
    assert formulas["D2"]["cache_missing"] is False
    assert formulas["D2"]["formula_checksum"] == hashlib.sha256(b"B2/C2").hexdigest()
    assert formulas["D3"]["cached_value"] == "1.2"
    assert elements[0].table["sheet_name"] == "Summary"
    assert elements[0].table["sheet_visibility"] == "visible"
    assert elements[0].locator["sheet_name"] == "Summary"
    assert elements[0].table["rows"][0] == ["East", "80", "100", "0.8"]
    assert all(item.locator for item in elements)


def test_xlsx_parser_uses_workbook_relationships_shared_strings_and_sparse_coordinates() -> None:
    """工作表真实顺序、隐藏态、共享字符串和稀疏列必须按OOXML坐标稳定解析。"""

    payload = BytesIO()
    with ZipFile(payload, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "xl/workbook.xml",
            """<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Hidden Facts" state="hidden" sheetId="7" r:id="rId9"/></sheets></workbook>""",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            """<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId9" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet7.xml"/></Relationships>""",
        )
        archive.writestr(
            "xl/sharedStrings.xml",
            """<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>Alpha</t></si><si><r><t>Ga</t></r><r><t>mma</t></r></si></sst>""",
        )
        archive.writestr(
            "xl/worksheets/sheet7.xml",
            """<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="C1" t="s"><v>1</v></c><c r="D1" t="b"><v>1</v></c></row></sheetData></worksheet>""",
        )

    elements = parse_xlsx_bytes(payload.getvalue())

    assert len(elements) == 1
    assert elements[0].table["sheet_name"] == "Hidden Facts"
    assert elements[0].table["sheet_visibility"] == "hidden"
    assert elements[0].table["rows"] == []
    assert elements[0].table["columns"] == ["Alpha", "", "Gamma", "true"]
    assert [cell["cell"] for cell in elements[0].table["cells"]] == ["A1", "C1", "D1"]


@pytest.mark.parametrize(
    ("filename", "file_format", "code"),
    [
        ("forged_extension.pdf", "pdf", "ATTACHMENT_TYPE_MISMATCH"),
        ("corrupt.docx", "docx", "ATTACHMENT_ARCHIVE_INVALID"),
    ],
)
def test_forged_and_corrupt_documents_fail_without_partial_elements(
    filename: str,
    file_format: str,
    code: str,
) -> None:
    """伪扩展名和损坏OOXML整体失败，不允许已解析片段假装成功。"""

    with pytest.raises(InputExtractionError) as exc_info:
        run_attachment_parser_isolated(
            (FIXTURE_ROOT / "negative" / filename).read_bytes(),
            file_format=file_format,
            timeout_seconds=10,
            memory_mb=256,
        )
    assert exc_info.value.code == code


@pytest.mark.parametrize(
    ("entries", "code"),
    [
        ([('../escape.xml', b'<root/>')], "ATTACHMENT_ARCHIVE_PATH_INVALID"),
        ([('C:/escape.xml', b'<root/>')], "ATTACHMENT_ARCHIVE_PATH_INVALID"),
        (
            [('word/data.zip', b'PK\x03\x04nested')],
            "ATTACHMENT_ARCHIVE_NESTED_REJECTED",
        ),
        (
            [('word/vbaProject.bin', b'macro')],
            "ATTACHMENT_ACTIVE_CONTENT_REJECTED",
        ),
        (
            [('word/document.xml', b'<w:instrText>DDEAUTO cmd</w:instrText>')],
            "ATTACHMENT_ACTIVE_CONTENT_REJECTED",
        ),
        (
            [
                (
                    'word/_rels/document.xml.rels',
                    b'<Relationship TargetMode="External" Target="https://invalid.example"/>',
                )
            ],
            "ATTACHMENT_EXTERNAL_RELATIONSHIP_REJECTED",
        ),
    ],
)
def test_archive_paths_nested_active_and_external_content_fail_closed(
    entries: list[tuple[str, bytes]],
    code: str,
) -> None:
    """路径逃逸、嵌套归档、VBA/DDE及外部关系必须在读取XML前稳定拒绝。"""

    with pytest.raises(InputExtractionError) as exc_info:
        _safe_archive_entries(_archive_bytes(entries))

    assert exc_info.value.code == code


def test_archive_duplicate_names_use_unicode_and_casefold_identity() -> None:
    """大小写或Unicode规范化后重名的条目不得以后写覆盖前写。"""

    payload = _archive_bytes(
        [
            ("word/Caf\u00e9.xml", b"trusted"),
            ("word/CAFE\u0301.XML", b"attacker"),
        ]
    )

    with pytest.raises(InputExtractionError) as exc_info:
        _safe_archive_entries(payload)

    assert exc_info.value.code == "ATTACHMENT_ARCHIVE_PATH_INVALID"


def test_archive_entry_total_ratio_and_count_budgets_have_distinct_errors(monkeypatch) -> None:
    """条目数、单项、总展开量与压缩比预算分别产生稳定错误且零半成品。"""

    monkeypatch.setattr("app.session.input_extraction.ARCHIVE_MAX_ENTRIES", 1)
    with pytest.raises(InputExtractionError) as count_error:
        _safe_archive_entries(_archive_bytes([("a.xml", b"a"), ("b.xml", b"b")]))
    assert count_error.value.code == "ATTACHMENT_ARCHIVE_ENTRY_LIMIT"

    monkeypatch.setattr("app.session.input_extraction.ARCHIVE_MAX_ENTRIES", 10)
    monkeypatch.setattr("app.session.input_extraction.ARCHIVE_MAX_ENTRY_BYTES", 3)
    with pytest.raises(InputExtractionError) as entry_error:
        _safe_archive_entries(_archive_bytes([("a.xml", b"1234")], compression=ZIP_STORED))
    assert entry_error.value.code == "ATTACHMENT_ARCHIVE_ENTRY_SIZE_LIMIT"

    monkeypatch.setattr("app.session.input_extraction.ARCHIVE_MAX_ENTRY_BYTES", 10)
    monkeypatch.setattr("app.session.input_extraction.ARCHIVE_MAX_EXPANDED_BYTES", 5)
    with pytest.raises(InputExtractionError) as total_error:
        _safe_archive_entries(
            _archive_bytes([("a.xml", b"123"), ("b.xml", b"456")], compression=ZIP_STORED)
        )
    assert total_error.value.code == "ATTACHMENT_ARCHIVE_SIZE_LIMIT"

    monkeypatch.setattr("app.session.input_extraction.ARCHIVE_MAX_ENTRY_BYTES", 100_000)
    monkeypatch.setattr("app.session.input_extraction.ARCHIVE_MAX_EXPANDED_BYTES", 100_000)
    monkeypatch.setattr("app.session.input_extraction.ARCHIVE_MAX_RATIO", 2)
    with pytest.raises(InputExtractionError) as ratio_error:
        _safe_archive_entries(_archive_bytes([("a.xml", b"a" * 10_000)]))
    assert ratio_error.value.code == "ATTACHMENT_ARCHIVE_RATIO_LIMIT"


def test_archive_encrypted_flag_is_rejected_before_entry_read() -> None:
    """带传统加密标志的ZIP条目必须稳定拒绝，不得进入密码提示或部分解析。"""

    payload = bytearray(_archive_bytes([("word/document.xml", b"<root/>")], compression=ZIP_STORED))
    local = payload.index(b"PK\x03\x04")
    central = payload.index(b"PK\x01\x02")
    payload[local + 6 : local + 8] = (1).to_bytes(2, "little")
    payload[central + 8 : central + 10] = (1).to_bytes(2, "little")

    with pytest.raises(InputExtractionError) as exc_info:
        _safe_archive_entries(bytes(payload))

    assert exc_info.value.code == "ATTACHMENT_ARCHIVE_ENCRYPTED"


def test_parser_fd_keeps_original_inode_after_managed_directory_replacement(tmp_path: Path) -> None:
    """父进程安全open后替换受管目录，parser仍只能读取已授权CSV inode。"""

    root = tmp_path / "managed"
    locator = "tenant/resource/input.csv"
    managed_write_bytes(root, locator, b"Region,Target\nTrusted,100\n")
    descriptor = managed_open_read_fd(root, locator)
    parent = root / "tenant" / "resource"
    original_parent = root / "tenant" / "resource-original"
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    (attacker / "input.csv").write_bytes(b"Region,Target\nAttacker,999\n")
    parent.rename(original_parent)
    parent.symlink_to(attacker, target_is_directory=True)

    try:
        elements = run_attachment_parser_fd_isolated(
            descriptor,
            file_format="csv",
            timeout_seconds=10,
            memory_mb=256,
        )
    finally:
        os.close(descriptor)

    assert elements[0].table["rows"] == [["Trusted", "100"]]
    assert "Attacker" not in elements[0].text


def test_parser_managed_open_rejects_symlink_and_late_hardlink(tmp_path: Path) -> None:
    """叶子symlink在授权前拒绝，授权后新增hardlink也由父进程或CLI二次复核拒绝。"""

    root = tmp_path / "managed"
    outside = tmp_path / "outside.csv"
    outside.write_bytes(b"Region,Target\nOutside,999\n")
    symlink = root / "tenant" / "resource" / "input.csv"
    symlink.parent.mkdir(parents=True)
    symlink.symlink_to(outside)
    with pytest.raises(ManagedStorageError):
        managed_open_read_fd(root, "tenant/resource/input.csv")

    symlink.unlink()
    managed_write_bytes(root, "tenant/resource/trusted.csv", b"Region,Target\nTrusted,100\n")
    descriptor = managed_open_read_fd(root, "tenant/resource/trusted.csv")
    late_link = tmp_path / "late-hardlink.csv"
    late_link.hardlink_to(root / "tenant/resource/trusted.csv")
    try:
        with pytest.raises(InputExtractionError) as exc_info:
            run_attachment_parser_fd_isolated(
                descriptor,
                file_format="csv",
                timeout_seconds=10,
                memory_mb=256,
            )
    finally:
        os.close(descriptor)

    assert exc_info.value.code == "ATTACHMENT_PARSER_INPUT_INVALID"

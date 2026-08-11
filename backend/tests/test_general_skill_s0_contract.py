"""
@Time       : 2026/08/11
@Author     : zhanglp8181
@File       : test_general_skill_s0_contract.py
@CallChain  : pytest → GeneralSkill 导入边界/DynamicTaskPlanner → S0 基线契约
@Description: 固定 Skill 蒸馏改造前的 legacy 行为，供后续批次用同一攻击样本替换为生产契约回归。
"""

from __future__ import annotations

import inspect
from io import BytesIO
from zipfile import ZipFile

from app.api import general_skills
from app.dynamic_tasks.capability_catalog import (
    CapabilitySnapshot,
    DynamicCapabilityCatalog,
    capability_checksum,
)
from app.dynamic_tasks.planner_service import DynamicTaskPlanner
from app.dynamic_tasks.planning import SuccessCriterion
from app.general_skills.schema import (
    GeneralSkillClawHubImportRequest,
    GeneralSkillImportRequest,
    GeneralSkillPackageUploadRequest,
)


class _Response:
    """模拟 urllib 响应，记录下载器是否在地址校验前打开连接。"""

    headers = {"content-type": "text/plain"}

    def __enter__(self) -> _Response:
        """返回上下文管理器自身。"""

        return self

    def __exit__(self, *_args: object) -> None:
        """结束模拟响应且不吞掉异常。"""

    def read(self, _limit: int) -> bytes:
        """返回最小合法响应体。"""

        return b"ok"


class _PlanningClient:
    """捕获 DynamicTaskPlanner 投影给模型的能力目录。"""

    def __init__(self) -> None:
        """初始化尚未收到请求的客户端。"""

        self.payload: dict[str, object] | None = None

    def generate_json(self, _system_prompt: str, user_payload: dict) -> dict:
        """保存模型输入并返回只有回答节点的有效计划。"""

        self.payload = user_payload
        return {
            "goal": "模型目标",
            "success_criteria": [
                {"id": "model-done", "type": "assertion", "spec": {"required": False}}
            ],
            "steps": [{"draft_id": "answer", "title": "回答", "kind": "answer"}],
        }


def _general_skill_snapshot() -> CapabilitySnapshot:
    """构造风险为只读但类型为 GeneralSkill 的动态能力快照。"""

    payload = {
        "capability_type": "general_skill",
        "capability_id": "genskill_s0",
        "tenant_id": "tenant_demo",
        "name": "s0-guidance",
        "contract": {"risk_class": "read"},
        "model_view": {"name": "s0-guidance", "description": "S0 指南"},
        "user_view": {"name": "S0 指南"},
        "audit_view": {"revision": "legacy"},
    }
    return CapabilitySnapshot(
        **payload,
        agent_id="agent_s0",
        checksum=capability_checksum(payload),
    )


def _zip_bytes(entries: list[tuple[str, bytes]]) -> bytes:
    """把给定成员构造成内存 ZIP，避免测试接触工作区文件。"""

    package = BytesIO()
    with ZipFile(package, "w") as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return package.getvalue()


def test_legacy_import_requests_default_to_published() -> None:
    """固定三种现有导入请求都会绕过预览而默认 published 的改造前事实。"""

    assert GeneralSkillImportRequest(tenant_id="tenant_demo").status == "published"
    assert (
        GeneralSkillClawHubImportRequest(tenant_id="tenant_demo", source="demo").status
        == "published"
    )
    assert (
        GeneralSkillPackageUploadRequest(
            tenant_id="tenant_demo",
            filename="SKILL.md",
            content_base64="",
        ).status
        == "published"
    )


def test_legacy_import_status_accepts_unknown_value() -> None:
    """固定 status 仍是自由字符串、尚无状态枚举的改造前事实。"""

    request = GeneralSkillImportRequest(tenant_id="tenant_demo", status="future-state")

    assert request.status == "future-state"


def test_legacy_frontmatter_parser_is_not_yaml_complete() -> None:
    """固定多行 YAML 和列表 allowed-tools 会被宽松逐行解析破坏的事实。"""

    metadata = general_skills._parse_skill_metadata(
        "---\ndescription: >\n  第一行\n  第二行\nallowed-tools:\n  - crm.read\n---\n正文\n"
    )

    assert metadata["description"] == ">"
    assert metadata["allowed-tools"] == ""


def test_legacy_path_cleaner_accepts_absolute_and_drive_paths() -> None:
    """固定绝对路径和盘符在清洗后被当作相对路径接受的事实。"""

    assert general_skills._clean_package_path("/etc/passwd") == "etc/passwd"
    assert general_skills._clean_package_path("C:\\Windows\\win.ini") == "C:/Windows/win.ini"


def test_legacy_zip_silently_skips_oversized_member() -> None:
    """固定 ZIP 包含超大成员时会静默生成残包而不是整包失败的事实。"""

    package = _zip_bytes(
        [
            ("skill/SKILL.md", b"---\nname: demo\n---\n"),
            ("skill/resources/large.txt", b"x" * (general_skills.MAX_CLAWHUB_FILE_BYTES + 1)),
        ]
    )

    files = general_skills._files_from_zip(package)

    assert [item.path for item in files] == ["SKILL.md"]


def test_legacy_zip_silently_truncates_member_count() -> None:
    """固定 ZIP 文件数超过上限时仅截断列表而不拒绝导入的事实。"""

    entries = [("skill/SKILL.md", b"# demo\n")]
    entries.extend((f"skill/resources/{index}.txt", b"x") for index in range(250))

    files = general_skills._files_from_zip(_zip_bytes(entries))

    assert len(files) == general_skills.MAX_CLAWHUB_FILES


def test_legacy_zip_selects_first_skill_root() -> None:
    """固定多 Skill ZIP 会按归档顺序静默选择第一个根目录的事实。"""

    package = _zip_bytes(
        [
            ("first/SKILL.md", b"# first\n"),
            ("second/SKILL.md", b"# second\n"),
        ]
    )

    files = general_skills._files_from_zip(package)

    assert [item.path for item in files] == ["SKILL.md"]
    assert files[0].content == "# first\n"


def test_legacy_zip_replaces_invalid_utf8() -> None:
    """固定非法 UTF-8 会被替换字符掩盖而不是拒绝整个文本文件的事实。"""

    package = _zip_bytes(
        [("skill/SKILL.md", b"# demo\n"), ("skill/resources/bad.txt", b"bad\xfftext")]
    )

    files = general_skills._files_from_zip(package)

    assert files[1].content == "bad�text"


def test_legacy_downloader_opens_loopback_without_address_gate(monkeypatch) -> None:
    """固定下载器会在没有 DNS/IP 分类门禁时尝试访问 loopback 的事实。"""

    opened: list[str] = []

    def fake_urlopen(request: object, timeout: int) -> _Response:
        """记录实际请求目标并返回模拟响应。"""

        opened.append(str(getattr(request, "full_url", "")))
        assert timeout == general_skills.REMOTE_SKILL_DOWNLOAD_TIMEOUT_SECONDS
        return _Response()

    monkeypatch.setattr(general_skills, "urlopen", fake_urlopen)

    assert general_skills._download_url("http://127.0.0.1/internal") == (b"ok", "text/plain")
    assert opened == ["http://127.0.0.1/internal"]


def test_legacy_dynamic_planner_filters_general_skill() -> None:
    """固定 DynamicTask 收到 GeneralSkill 后仍不会把它投影给规划模型的事实。"""

    client = _PlanningClient()
    planner = DynamicTaskPlanner(client)

    planner.create_plan(
        goal="使用指南完成任务",
        success_criteria=[SuccessCriterion(id="done", type="assertion", spec={})],
        capabilities=[_general_skill_snapshot()],
    )

    assert client.payload is not None
    assert client.payload["capabilities"] == []


def test_legacy_dynamic_catalog_has_no_actor_user_dimension() -> None:
    """固定动态 Skill 目录尚未把当前操作者作为显式授权输入的事实。"""

    parameters = inspect.signature(DynamicCapabilityCatalog.list_general_skills).parameters

    assert "actor_user_id" not in parameters

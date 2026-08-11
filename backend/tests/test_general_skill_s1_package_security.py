"""
@Time       : 2026/08/12 00:05
@Author     : zhanglp8181
@File       : test_general_skill_s1_package_security.py
@CallChain  : pytest → package_security.normalize_zip_package → Skill ImportJob preview
@Description: 用 S0 攻击样本验证 S1 归档规范化不会静默截断、猜选或放宽输入。
"""

from __future__ import annotations

from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest

from app.general_skills.package_security import (
    GeneralSkillPackageError,
    PackageLimits,
    normalize_zip_package,
)


def _skill_markdown(
    *,
    name: str = "测试技能",
    description: str = "用于验证安全规范化。",
    allowed_tools: str = "",
) -> str:
    """构造具有完整 YAML frontmatter 的最小 SKILL.md。"""

    tools_line = f"allowed-tools:\n{allowed_tools}\n" if allowed_tools else ""
    return f"---\nname: {name}\ndescription: {description}\n{tools_line}---\n# {name}\n"


def _zip(entries: list[tuple[str, bytes]], *, compression: int = ZIP_DEFLATED) -> bytes:
    """按给定顺序构造 ZIP，以验证结果不依赖归档顺序。"""

    payload = BytesIO()
    with ZipFile(payload, "w", compression=compression) as archive:
        for path, content in entries:
            archive.writestr(path, content)
    return payload.getvalue()


def test_full_yaml_preserves_multiline_description_and_allowed_tools_list() -> None:
    """替换 S0 宽松 parser 样本，证明多行值与列表被完整解析。"""

    markdown = (
        "---\n"
        "name: refund-helper\n"
        "description: >\n"
        "  第一行说明\n"
        "  第二行说明\n"
        "allowed-tools:\n"
        "  - crm.order.read\n"
        "  - wecom.message.send\n"
        "---\n# Refund\n"
    )
    package = normalize_zip_package(_zip([("refund/SKILL.md", markdown.encode())]))
    candidate = package.candidates[0]
    assert candidate.description == "第一行说明 第二行说明"
    assert candidate.allowed_tools == ("crm.order.read", "wecom.message.send")


@pytest.mark.parametrize(
    "path",
    ["/tmp/SKILL.md", "C:/secrets/SKILL.md", "../SKILL.md", "skill\\SKILL.md"],
)
def test_archive_rejects_absolute_drive_parent_and_backslash_paths(path: str) -> None:
    """替换 S0 路径清洗样本，证明危险路径导致整包拒绝。"""

    with pytest.raises(GeneralSkillPackageError) as captured:
        normalize_zip_package(_zip([(path, _skill_markdown().encode())]))
    assert captured.value.error_code == "GENERAL_SKILL_PACKAGE_INVALID"


def test_archive_rejects_oversized_member_instead_of_silently_skipping() -> None:
    """替换 S0 超大成员静默跳过样本，证明超限不会产出不完整候选。"""

    package = _zip(
        [
            ("skill/SKILL.md", _skill_markdown().encode()),
            ("skill/large.txt", b"x" * 65),
        ]
    )
    with pytest.raises(GeneralSkillPackageError) as captured:
        normalize_zip_package(
            package,
            limits=PackageLimits(
                max_text_file_bytes=64,
                max_member_compression_ratio=1_000,
            ),
        )
    assert captured.value.error_code == "GENERAL_SKILL_PACKAGE_LIMIT_EXCEEDED"


def test_archive_rejects_excess_file_count_instead_of_truncating() -> None:
    """替换 S0 文件数截断样本，证明第 N+1 个成员使整包失败。"""

    package = _zip(
        [
            ("skill/SKILL.md", _skill_markdown().encode()),
            ("skill/one.txt", b"1"),
            ("skill/two.txt", b"2"),
        ]
    )
    with pytest.raises(GeneralSkillPackageError) as captured:
        normalize_zip_package(package, limits=PackageLimits(max_files=2))
    assert captured.value.error_code == "GENERAL_SKILL_PACKAGE_LIMIT_EXCEEDED"


def test_multiple_skill_manifests_return_stable_candidates_without_implicit_choice() -> None:
    """替换 S0 首个 manifest 胜出样本，证明所有候选均有稳定 ID 供用户选择。"""

    first = ("skills/a/SKILL.md", _skill_markdown(name="A").encode())
    second = ("skills/b/SKILL.md", _skill_markdown(name="B").encode())
    forward = normalize_zip_package(_zip([second, first]))
    reverse = normalize_zip_package(_zip([first, second]))
    assert [item.name for item in forward.candidates] == ["A", "B"]
    assert [item.candidate_id for item in forward.candidates] == [
        item.candidate_id for item in reverse.candidates
    ]
    assert forward.normalized_checksum == reverse.normalized_checksum


def test_invalid_utf8_text_rejects_entire_package_without_replacement() -> None:
    """替换 S0 replacement decode 样本，证明不可审核文本不会进入预览。"""

    package = _zip(
        [
            ("skill/SKILL.md", _skill_markdown().encode()),
            ("skill/reference.txt", b"valid\xffinvalid"),
        ]
    )
    with pytest.raises(GeneralSkillPackageError, match="valid UTF-8") as captured:
        normalize_zip_package(package)
    assert captured.value.error_code == "GENERAL_SKILL_PACKAGE_INVALID"


def test_high_compression_member_rejects_zip_bomb_before_reading_content() -> None:
    """验证压缩后很小但展开比过高的成员在读取正文前被拒绝。"""

    package = _zip(
        [
            ("skill/SKILL.md", _skill_markdown().encode()),
            ("skill/bomb.txt", b"0" * 100_000),
        ]
    )
    with pytest.raises(GeneralSkillPackageError, match="compression ratio") as captured:
        normalize_zip_package(
            package,
            limits=PackageLimits(max_text_file_bytes=200_000, max_member_compression_ratio=10),
        )
    assert captured.value.error_code == "GENERAL_SKILL_PACKAGE_LIMIT_EXCEEDED"


def test_symlink_member_is_rejected_as_non_regular_file() -> None:
    """验证 ZIP 中的符号链接不能把资源边界指向包外。"""

    payload = BytesIO()
    with ZipFile(payload, "w") as archive:
        archive.writestr("skill/SKILL.md", _skill_markdown())
        link = ZipInfo("skill/reference.txt")
        link.create_system = 3
        link.external_attr = 0o120777 << 16
        archive.writestr(link, "../../secret")
    with pytest.raises(GeneralSkillPackageError, match="non-regular") as captured:
        normalize_zip_package(payload.getvalue())
    assert captured.value.error_code == "GENERAL_SKILL_PACKAGE_INVALID"


def test_nested_skill_resources_do_not_leak_into_parent_candidate() -> None:
    """验证父候选不能把子 Skill 文件误当作自己的附件或指令。"""

    package = normalize_zip_package(
        _zip(
            [
                ("SKILL.md", _skill_markdown(name="Root").encode()),
                ("root.txt", b"root"),
                ("child/SKILL.md", _skill_markdown(name="Child").encode()),
                ("child/private.txt", b"child"),
            ]
        )
    )
    root, child = package.candidates
    assert [resource.path for resource in root.resources] == ["SKILL.md", "root.txt"]
    assert [resource.path for resource in child.resources] == [
        "child/SKILL.md",
        "child/private.txt",
    ]


def test_repository_subpath_excludes_unrelated_root_symlink_but_not_selected_symlink() -> None:
    """验证显式子树可避开仓库根无关链接，而候选子树内链接仍整包拒绝。"""

    payload = BytesIO()
    with ZipFile(payload, "w") as archive:
        root_link = ZipInfo("repo-sha/AGENTS.md")
        root_link.create_system = 3
        root_link.external_attr = 0o120777 << 16
        archive.writestr(root_link, "README.md")
        archive.writestr("repo-sha/skills/tdd/SKILL.md", _skill_markdown(name="tdd"))
    package = normalize_zip_package(payload.getvalue(), source_subpath="skills")
    assert [candidate.name for candidate in package.candidates] == ["tdd"]

    unsafe = BytesIO()
    with ZipFile(unsafe, "w") as archive:
        archive.writestr("repo-sha/skills/tdd/SKILL.md", _skill_markdown(name="tdd"))
        selected_link = ZipInfo("repo-sha/skills/tdd/reference.md")
        selected_link.create_system = 3
        selected_link.external_attr = 0o120777 << 16
        archive.writestr(selected_link, "../../../secret")
    with pytest.raises(GeneralSkillPackageError, match="non-regular"):
        normalize_zip_package(unsafe.getvalue(), source_subpath="skills")


@pytest.mark.parametrize(
    "frontmatter",
    [
        "name: first\nname: second\ndescription: duplicate",
        "name: anchored\ndescription: &shared repeated\nmetadata: *shared",
        "name: dated\ndescription: invalid type\nreleased: 2026-08-12",
        "name: number\ndescription: invalid number\nscore: .nan",
    ],
)
def test_manifest_rejects_ambiguous_or_non_json_yaml(frontmatter: str) -> None:
    """验证重复键、alias、日期隐式类型和非有限数字不能跨过人工审核边界。"""

    markdown = f"---\n{frontmatter}\n---\n# Unsafe\n"
    with pytest.raises(GeneralSkillPackageError) as captured:
        normalize_zip_package(_zip([("skill/SKILL.md", markdown.encode())]))
    assert captured.value.error_code == "GENERAL_SKILL_PACKAGE_INVALID"


def test_manifest_requires_an_exact_frontmatter_closing_line() -> None:
    """验证类似分隔符的正文不会被当成 frontmatter 结束标记。"""

    markdown = "---\nname: unsafe\ndescription: not closed\n---suffix\n# Unsafe\n"
    with pytest.raises(GeneralSkillPackageError, match="not closed"):
        normalize_zip_package(_zip([("skill/SKILL.md", markdown.encode())]))


def test_invocation_hint_and_references_are_normalized_without_granting_dependencies() -> None:
    """验证 matt 风格调用字段和正文引用被分类，候选边本身不代表授权。"""

    parent = (
        "---\n"
        "name: grill-me\n"
        "description: 通过提问澄清方案。\n"
        "disable-model-invocation: true\n"
        'argument-hint: "需要澄清的方案"\n'
        "---\n"
        "Run a `/grilling` session, then /clear. Ignore https://example.com/not-a-skill.\n"
    )
    package = normalize_zip_package(
        _zip(
            [
                ("grill-me/SKILL.md", parent.encode()),
                ("grilling/SKILL.md", _skill_markdown(name="grilling").encode()),
            ]
        )
    )
    candidate = package.candidates[0]
    assert candidate.name == "grill-me"
    assert candidate.invocation_policy == "user_only"
    assert candidate.argument_hint == "需要澄清的方案"
    assert [(item.referenced_name, item.reference_count) for item in candidate.dependency_candidates] == [
        ("grilling", 1)
    ]
    assert candidate.platform_commands == ("clear",)


@pytest.mark.parametrize(
    "field_line",
    [
        "disable-model-invocation: yes",
        "argument-hint: []",
    ],
)
def test_invocation_metadata_rejects_ambiguous_types(field_line: str) -> None:
    """验证调用策略不能利用 YAML 隐式 truthy 或错误结构绕过审核。"""

    markdown = f"---\nname: unsafe\ndescription: invalid invocation\n{field_line}\n---\n"
    with pytest.raises(GeneralSkillPackageError) as captured:
        normalize_zip_package(_zip([("unsafe/SKILL.md", markdown.encode())]))
    assert captured.value.error_code == "GENERAL_SKILL_PACKAGE_INVALID"


def test_duplicate_skill_names_reject_ambiguous_dependency_identity() -> None:
    """验证不同目录同名 Skill 不会让正文引用绑定到不确定候选。"""

    with pytest.raises(GeneralSkillPackageError) as captured:
        normalize_zip_package(
            _zip(
                [
                    ("one/SKILL.md", _skill_markdown(name="duplicate").encode()),
                    ("two/SKILL.md", _skill_markdown(name="duplicate").encode()),
                ]
            )
        )
    assert captured.value.error_code == "GENERAL_SKILL_DEPENDENCY_INVALID"

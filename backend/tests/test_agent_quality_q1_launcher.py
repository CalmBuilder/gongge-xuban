"""
@Time       : 2026/08/15
@Author     : zhanglp8181
@File       : test_agent_quality_q1_launcher.py
@CallChain  : pytest → Q1 launcher → 浏览器最小环境与源码/Skill指纹
@Description: 验证Q1真实模型A/B不会向Node/Chromium扩散密钥，并固定受审Skill来源。
"""

from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[2]


def test_q1_browser_environment_excludes_model_and_ambient_secrets() -> None:
    """Q1浏览器仅接收公开认证事实，不继承后端模型密钥或父进程秘密。"""

    module = _load_launcher()
    server_environment = {
        "PATH": "/usr/bin",
        "HOME": "/tmp/q1-home",
        "LIVE_ATTACHMENT_E2E": "1",
        "LIVE_ATTACHMENT_MODEL_API_KEY": "q1-model-secret",
        "LIVE_ATTACHMENT_MODEL_BASE_URL": "https://user:secret@example.invalid/v1",
        "LIVE_ATTACHMENT_MODEL_EXTRA_BODY_JSON": '{"authorization":"extra-secret"}',
        "LIVE_ATTACHMENT_PROVIDER_ENDPOINT": "https://example.invalid/v1",
        "LIVE_ATTACHMENT_MODEL_NAME": "model-name",
        "LIVE_ATTACHMENT_SOURCE_MODEL_CONFIG_ID": "model-id",
        "LIVE_ATTACHMENT_MODEL_CAPABILITY_CHECKSUM": "capability-checksum",
        "UNRELATED_DEPLOY_SECRET": "ambient-secret",
    }

    browser_environment = module._q1_browser_environment(
        server_environment,
        fingerprints={"backend/example.py": "a" * 64},
        model_id="model-id",
        model_name="model-name",
        capability_checksum="capability-checksum",
        public_endpoint="https://example.invalid/v1",
    )

    assert browser_environment["PATH"] == "/usr/bin"
    assert browser_environment["Q1_SOURCE_MODEL_CONFIG_ID"] == "model-id"
    assert browser_environment["Q1_PROVIDER_ENDPOINT"] == "https://example.invalid/v1"
    assert json.loads(browser_environment["Q1_CERTIFICATION_FINGERPRINT_JSON"]) == {
        "backend/example.py": "a" * 64
    }
    assert "LIVE_ATTACHMENT_MODEL_API_KEY" not in browser_environment
    assert "LIVE_ATTACHMENT_MODEL_BASE_URL" not in browser_environment
    assert "LIVE_ATTACHMENT_MODEL_EXTRA_BODY_JSON" not in browser_environment
    assert "UNRELATED_DEPLOY_SECRET" not in browser_environment
    assert all(
        secret not in value
        for value in browser_environment.values()
        for secret in ("q1-model-secret", "extra-secret", "ambient-secret")
    )


def test_q1_skill_source_and_certification_fingerprints_are_current() -> None:
    """冻结上游Skill revision/许可证/文件checksum及Q1关键源码指纹。"""

    module = _load_launcher()

    module._assert_skill_source()
    skill_checksums = module._skill_source_checksums()
    fingerprints = module._certification_fingerprints()

    assert set(skill_checksums) == {
        "otherpro/skills/LICENSE",
        "otherpro/skills/skills/productivity/writing-for-agents/SKILL.md",
        "otherpro/skills/skills/productivity/writing-for-agents/SKILL-MECHANICS.md",
        "otherpro/skills/skills/productivity/writing-for-agents/agents/openai.yaml",
    }
    assert all(len(checksum) == 64 for checksum in skill_checksums.values())
    assert "backend/app/api/chat.py" in fingerprints
    assert "backend/app/dynamic_tasks/action_proposer.py" in fingerprints
    assert "frontend-enterprise/src/pages/chat/chatHelpers.tsx" in fingerprints
    assert "frontend-enterprise/src/pages/chat/useChatSession.ts" in fingerprints
    assert "frontend-enterprise/e2e/agent-quality-q1.live.fullstack.e2e.ts" in fingerprints
    assert "scripts/run_agent_quality_q1_browser_regression.py" in fingerprints
    assert all(len(checksum) == 64 for checksum in fingerprints.values())


def test_q1_codebase_profile_freezes_only_reviewed_skill_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codebase-design profile必须固定其四个受审文件，并仅向浏览器公开所选目录。"""

    module = _load_launcher()
    monkeypatch.setenv("Q1_PROFILE", "codebase")

    profile_name, profile = module._selected_profile()
    skill_dir = Path(profile["skill_dir"])
    module._assert_skill_source(skill_dir=skill_dir)
    checksums = module._skill_source_checksums(skill_dir=skill_dir)
    environment = module._q1_browser_environment(
        {"PATH": "/usr/bin", "LIVE_ATTACHMENT_E2E": "1"},
        fingerprints={},
        model_id="model-id",
        model_name="model-name",
        capability_checksum="capability-checksum",
        public_endpoint="https://example.invalid/v1",
        profile_name=profile_name,
        skill_dir=skill_dir,
    )

    assert profile_name == "codebase"
    assert set(checksums) == {
        "otherpro/skills/LICENSE",
        "otherpro/skills/skills/engineering/codebase-design/SKILL.md",
        "otherpro/skills/skills/engineering/codebase-design/DEEPENING.md",
        "otherpro/skills/skills/engineering/codebase-design/DESIGN-IT-TWICE.md",
        "otherpro/skills/skills/engineering/codebase-design/agents/openai.yaml",
    }
    assert environment["Q1_CODEBASE_SKILL_DIR"] == str(skill_dir)
    assert "Q1_WRITING_SKILL_DIR" not in environment


def test_q1_ordinary_codebase_benchmark_forwards_codebase_skill_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ordinary codebase benchmark必须把实际Skill目录投影到对应白名单变量。"""

    module = _load_launcher()
    monkeypatch.setenv("Q1_PROFILE", "ordinary")
    monkeypatch.setenv("Q1_ORDINARY_BENCHMARK", "codebase")
    profile_name, profile = module._selected_profile()
    skill_dir = Path(module.CODEBASE_SKILL_DIR)
    environment = module._q1_browser_environment(
        {
            "PATH": "/usr/bin",
            "Q1_ORDINARY_BENCHMARK": "codebase",
        },
        fingerprints=module._certification_fingerprints(profile_name=profile_name),
        model_id="model-id",
        model_name="model-name",
        capability_checksum="capability-checksum",
        public_endpoint="https://example.invalid/v1",
        profile_name=profile_name,
        skill_dir=skill_dir,
    )

    assert profile["e2e_file"] == "e2e/agent-quality-q1-ordinary.live.fullstack.e2e.ts"
    assert Path(profile["skill_dir"]) == skill_dir
    assert environment["Q1_CODEBASE_SKILL_DIR"] == str(skill_dir)
    assert "Q1_WRITING_SKILL_DIR" not in environment
    assert environment["Q1_ORDINARY_BENCHMARK"] == "codebase"


def test_q1_writing_fair_profile_selects_fair_benchmark_and_forwards_selector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """writing-fair必须复用同一受审Skill，并把题集选择显式投影给浏览器。"""

    module = _load_launcher()
    monkeypatch.setenv("Q1_PROFILE", "writing-fair")
    monkeypatch.setenv("Q1_WRITING_BENCHMARK", "fair-v2")

    profile_name, profile = module._selected_profile()
    skill_dir = Path(profile["skill_dir"])
    environment = module._q1_browser_environment(
        {
            "PATH": "/usr/bin",
            "Q1_WRITING_BENCHMARK": "fair-v2",
        },
        fingerprints=module._certification_fingerprints(profile_name=profile_name),
        model_id="model-id",
        model_name="model-name",
        capability_checksum="capability-checksum",
        public_endpoint="https://example.invalid/v1",
        profile_name=profile_name,
        skill_dir=skill_dir,
    )

    assert profile_name == "writing-fair"
    assert profile["e2e_file"] == "e2e/agent-quality-q1.live.fullstack.e2e.ts"
    assert environment["Q1_WRITING_SKILL_DIR"] == str(skill_dir)
    assert environment["Q1_WRITING_BENCHMARK"] == "fair-v2"
    assert environment["Q1_PROFILE"] == "writing-fair"


def test_q1_plain_profile_has_no_skill_source_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """plain基线只运行真实模型问题，不读取或向浏览器投影otherpro Skill。"""

    module = _load_launcher()
    monkeypatch.setenv("Q1_PROFILE", "plain")

    profile_name, profile = module._selected_profile()
    fingerprints = module._certification_fingerprints(profile_name=profile_name)
    environment = module._q1_browser_environment(
        {
            "PATH": "/usr/bin",
            "LIVE_ATTACHMENT_E2E": "1",
            "Q1_ONLY_SCENARIO": "incident-analysis",
        },
        fingerprints={"frontend-enterprise/e2e/plain.ts": "a" * 64},
        model_id="model-id",
        model_name="model-name",
        capability_checksum="capability-checksum",
        public_endpoint="https://example.invalid/v1",
        profile_name=profile_name,
        skill_dir=None,
    )

    assert profile_name == "plain"
    assert profile["skill_dir"] is None
    assert profile["e2e_file"] == "e2e/agent-quality-q1-plain.live.fullstack.e2e.ts"
    assert "frontend-enterprise/e2e/agent-quality-q1-plain.live.fullstack.e2e.ts" in fingerprints
    assert all(len(checksum) == 64 for checksum in fingerprints.values())
    assert environment["Q1_ONLY_SCENARIO"] == "incident-analysis"
    assert "Q1_WRITING_SKILL_DIR" not in environment
    assert "Q1_CODEBASE_SKILL_DIR" not in environment
    assert "Q1_SKILLS_REVISION" not in environment
    assert "Q1_SKILL_SOURCE_CHECKSUM_JSON" not in environment


def test_event_window_profile_projects_real_probe_flags_without_skill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """长流窗口探针必须选择专属E2E，并只投影运行开关和无密钥证据路径。"""

    module = _load_launcher()
    monkeypatch.setenv("Q1_PROFILE", "event-window")

    profile_name, profile = module._selected_profile()
    environment = module._q1_browser_environment(
        {
            "PATH": "/usr/bin",
            "LIVE_ATTACHMENT_E2E": "1",
            "EVENT_WINDOW_E2E": "1",
            "EVENT_WINDOW_EVIDENCE_FILE": "event-window-r15.json",
        },
        fingerprints={"frontend-enterprise/e2e/event-window.ts": "a" * 64},
        model_id="model-id",
        model_name="model-name",
        capability_checksum="capability-checksum",
        public_endpoint="https://example.invalid/v1",
        profile_name=profile_name,
        skill_dir=None,
    )

    assert profile_name == "event-window"
    assert profile["skill_dir"] is None
    assert profile["e2e_file"] == "e2e/agent-quality-event-window.live.fullstack.e2e.ts"
    assert environment["EVENT_WINDOW_E2E"] == "1"
    assert environment["EVENT_WINDOW_EVIDENCE_FILE"] == "event-window-r15.json"
    assert "Q1_WRITING_SKILL_DIR" not in environment
    assert "Q1_CODEBASE_SKILL_DIR" not in environment


def test_q1_unrelated_profile_projects_reviewed_skill_without_changing_agent_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无关Skill负控固定受审目录，但仍使用专属plain Agent与独立浏览器场景。"""

    module = _load_launcher()
    monkeypatch.setenv("Q1_PROFILE", "unrelated")
    profile_name, profile = module._selected_profile()
    skill_dir = Path(profile["skill_dir"])
    environment = module._q1_browser_environment(
        {"PATH": "/usr/bin", "LIVE_ATTACHMENT_E2E": "1"},
        fingerprints={"frontend-enterprise/e2e/agent-quality-q1-unrelated.live.fullstack.e2e.ts": "a" * 64},
        model_id="model-id",
        model_name="model-name",
        capability_checksum="capability-checksum",
        public_endpoint="https://example.invalid/v1",
        profile_name=profile_name,
        skill_dir=skill_dir,
    )

    assert profile_name == "unrelated"
    assert profile["e2e_file"] == "e2e/agent-quality-q1-unrelated.live.fullstack.e2e.ts"
    assert environment["Q1_WRITING_SKILL_DIR"] == str(skill_dir)
    assert environment["Q1_PROFILE"] == "unrelated"
    assert "LIVE_ATTACHMENT_MODEL_API_KEY" not in environment


def test_q1_skill_sample_requires_explicit_upstream_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    """skill-sample只能使用显式冻结上游目录，不能隐式回退到默认 Skill。"""

    module = _load_launcher()
    monkeypatch.setenv("Q1_PROFILE", "skill-sample")
    monkeypatch.setenv(
        "Q1_SAMPLE_SKILL_DIR",
        str(module.UPSTREAM_SKILLS / "skills" / "engineering" / "domain-modeling"),
    )

    profile_name, profile = module._selected_profile()
    skill_dir = Path(profile["skill_dir"])
    module._assert_skill_source(skill_dir=skill_dir)
    assert profile_name == "skill-sample"
    assert skill_dir.name == "domain-modeling"
    assert module._required_skill_files(skill_dir) == ("SKILL.md", "agents/openai.yaml")


def test_q1_skill_sample_rejects_non_upstream_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """skill-sample拒绝工作树外的同名目录，避免任意本地内容进入真实回归。"""

    module = _load_launcher()
    monkeypatch.setenv("Q1_PROFILE", "skill-sample")
    monkeypatch.setenv("Q1_SAMPLE_SKILL_DIR", str(tmp_path))

    _, profile = module._selected_profile()
    with pytest.raises(RuntimeError, match="otherpro/skills/skills"):
        module._assert_skill_source(skill_dir=Path(profile["skill_dir"]))


def test_q1_plain_agent_has_no_seeded_resource_bindings() -> None:
    """plain E2E使用专属成员分身，种子不得向它绑定Skill、工具或知识。"""

    server_path = ROOT / "frontend-enterprise" / "e2e" / "start_fullstack_server.py"
    server_tree = ast.parse(server_path.read_text(encoding="utf-8"))
    agent_ids = [
        keyword.value.value
        for node in ast.walk(server_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "AgentProfile"
        for keyword in node.keywords
        if keyword.arg == "id"
        and isinstance(keyword.value, ast.Constant)
        and isinstance(keyword.value.value, str)
    ]
    bound_agent_ids = [
        argument.value
        for node in ast.walk(server_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ensure_private_resource_binding"
        for argument in node.args
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
    ]
    plain_e2e = (
        ROOT / "frontend-enterprise" / "e2e" / "agent-quality-q1-plain.live.fullstack.e2e.ts"
    ).read_text(encoding="utf-8")

    assert agent_ids.count("agent_q1_plain") == 1
    assert "agent_q1_plain" not in bound_agent_ids
    assert "const AGENT_ID = 'agent_q1_plain';" in plain_e2e
    assert "const AGENT_ID = 'agent_e2e_member_employee';" not in plain_e2e


def test_q1_diagnosis_control_agent_isolated_from_treatment_skill() -> None:
    """diagnosing随机交错时，对照分身不得继承处理组的Skill或工具绑定。"""

    server_path = ROOT / "frontend-enterprise" / "e2e" / "start_fullstack_server.py"
    server_tree = ast.parse(server_path.read_text(encoding="utf-8"))
    agent_ids = [
        keyword.value.value
        for node in ast.walk(server_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "AgentProfile"
        for keyword in node.keywords
        if keyword.arg == "id"
        and isinstance(keyword.value, ast.Constant)
        and isinstance(keyword.value.value, str)
    ]
    bound_agent_ids = [
        argument.value
        for node in ast.walk(server_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ensure_private_resource_binding"
        for argument in node.args
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
    ]
    diagnosing_e2e = (
        ROOT / "frontend-enterprise" / "e2e"
        / "agent-quality-q1-diagnosing.live.fullstack.e2e.ts"
    ).read_text(encoding="utf-8")

    assert agent_ids.count("agent_q1_diagnosis_control") == 1
    assert "agent_q1_diagnosis_control" not in bound_agent_ids
    assert "const CONTROL_AGENT_ID = 'agent_q1_diagnosis_control';" in diagnosing_e2e
    assert "const TREATMENT_AGENT_ID = 'agent_skill_demo_b_diagnosis';" in diagnosing_e2e


def test_q1_writing_control_agent_isolated_from_treatment_skill() -> None:
    """writing随机交错时，对照分身不得继承处理组的Skill或工具绑定。"""

    server_path = ROOT / "frontend-enterprise" / "e2e" / "start_fullstack_server.py"
    server_tree = ast.parse(server_path.read_text(encoding="utf-8"))
    agent_ids = [
        keyword.value.value
        for node in ast.walk(server_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "AgentProfile"
        for keyword in node.keywords
        if keyword.arg == "id"
        and isinstance(keyword.value, ast.Constant)
        and isinstance(keyword.value.value, str)
    ]
    bound_agent_ids = [
        argument.value
        for node in ast.walk(server_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ensure_private_resource_binding"
        for argument in node.args
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
    ]
    writing_e2e = (
        ROOT / "frontend-enterprise" / "e2e" / "agent-quality-q1.live.fullstack.e2e.ts"
    ).read_text(encoding="utf-8")

    assert agent_ids.count("agent_q1_writing_control") == 1
    assert "agent_q1_writing_control" not in bound_agent_ids
    assert "const CONTROL_AGENT_ID = 'agent_q1_writing_control';" in writing_e2e
    assert "const TREATMENT_AGENT_ID = 'agent_skill_demo_a_docs';" in writing_e2e


def test_q1_codebase_control_agent_isolated_from_treatment_skill() -> None:
    """codebase-design随机交错时，对照分身不得继承处理组的Skill或工具绑定。"""

    server_path = ROOT / "frontend-enterprise" / "e2e" / "start_fullstack_server.py"
    server_tree = ast.parse(server_path.read_text(encoding="utf-8"))
    agent_ids = [
        keyword.value.value
        for node in ast.walk(server_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "AgentProfile"
        for keyword in node.keywords
        if keyword.arg == "id"
        and isinstance(keyword.value, ast.Constant)
        and isinstance(keyword.value.value, str)
    ]
    bound_agent_ids = [
        argument.value
        for node in ast.walk(server_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ensure_private_resource_binding"
        for argument in node.args
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
    ]
    codebase_e2e = (
        ROOT / "frontend-enterprise" / "e2e"
        / "agent-quality-q1-codebase.live.fullstack.e2e.ts"
    ).read_text(encoding="utf-8")

    assert agent_ids.count("agent_q1_codebase_control") == 1
    assert "agent_q1_codebase_control" not in bound_agent_ids
    assert "const CONTROL_AGENT_ID = 'agent_q1_codebase_control';" in codebase_e2e
    assert "const TREATMENT_AGENT_ID = 'agent_skill_demo_a_docs';" in codebase_e2e


def test_q1_cross_turn_profile_uses_dedicated_no_skill_agent() -> None:
    """跨轮污染浏览器场景必须使用无预绑定Skill/知识的专属Agent。"""

    launcher = (ROOT / "scripts" / "run_agent_quality_q1_browser_regression.py").read_text(
        encoding="utf-8"
    )
    server = (ROOT / "frontend-enterprise" / "e2e" / "start_fullstack_server.py").read_text(
        encoding="utf-8"
    )
    e2e = (
        ROOT / "frontend-enterprise" / "e2e"
        / "agent-quality-q1-cross-turn.live.fullstack.e2e.ts"
    ).read_text(encoding="utf-8")

    assert '"cross-turn"' in launcher
    assert "e2e/agent-quality-q1-cross-turn.live.fullstack.e2e.ts" in launcher
    assert 'id="agent_q1_cross_turn"' in server
    assert "const AGENT_ID = 'agent_q1_cross_turn';" in e2e
    assert "Q1_CROSS_TURN_EVIDENCE_FILE" in launcher


def test_q1_diagnosing_profile_freezes_review_only_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """diagnosing profile固定脚本资源哈希，但只把目录交给浏览器安全导入。"""

    module = _load_launcher()
    monkeypatch.setenv("Q1_PROFILE", "diagnosing")

    profile_name, profile = module._selected_profile()
    skill_dir = Path(profile["skill_dir"])
    module._assert_skill_source(skill_dir=skill_dir)
    checksums = module._skill_source_checksums(skill_dir=skill_dir)
    fingerprints = module._certification_fingerprints(profile_name=profile_name)
    environment = module._q1_browser_environment(
        {
            "PATH": "/usr/bin",
            "LIVE_ATTACHMENT_E2E": "1",
            "Q1_DIAGNOSING_EVIDENCE_FILE": "diagnosing.json",
        },
        fingerprints=fingerprints,
        model_id="model-id",
        model_name="model-name",
        capability_checksum="capability-checksum",
        public_endpoint="https://example.invalid/v1",
        profile_name=profile_name,
        skill_dir=skill_dir,
    )

    assert set(checksums) == {
        "otherpro/skills/LICENSE",
        "otherpro/skills/skills/engineering/diagnosing-bugs/SKILL.md",
        "otherpro/skills/skills/engineering/diagnosing-bugs/scripts/hitl-loop.template.sh",
        "otherpro/skills/skills/engineering/diagnosing-bugs/agents/openai.yaml",
    }
    assert profile["e2e_file"] == "e2e/agent-quality-q1-diagnosing.live.fullstack.e2e.ts"
    assert environment["Q1_DIAGNOSING_SKILL_DIR"] == str(skill_dir)
    assert environment["Q1_DIAGNOSING_EVIDENCE_FILE"] == "diagnosing.json"
    assert "Q1_WRITING_SKILL_DIR" not in environment
    assert "Q1_CODEBASE_SKILL_DIR" not in environment
    assert "frontend-enterprise/e2e/agent-quality-q1-diagnosing.live.fullstack.e2e.ts" in fingerprints


def test_q1_diagnosing_positive_profile_uses_same_frozen_skill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正向工具场景复用同一受审Skill，但使用独立E2E与证据文件。"""

    module = _load_launcher()
    monkeypatch.setenv("Q1_PROFILE", "diagnosing-positive")
    profile_name, profile = module._selected_profile()
    skill_dir = Path(profile["skill_dir"])
    environment = module._q1_browser_environment(
        {
            "PATH": "/usr/bin",
            "Q1_DIAGNOSING_POSITIVE_EVIDENCE_FILE": "positive.json",
            "Q1_ONLY_SCENARIO": "treatment",
        },
        fingerprints=module._certification_fingerprints(profile_name=profile_name),
        model_id="model-id",
        model_name="model-name",
        capability_checksum="capability-checksum",
        public_endpoint="https://example.invalid/v1",
        profile_name=profile_name,
        skill_dir=skill_dir,
    )

    assert profile["e2e_file"] == (
        "e2e/agent-quality-q1-diagnosing-positive.live.fullstack.e2e.ts"
    )
    assert environment["Q1_DIAGNOSING_SKILL_DIR"] == str(skill_dir)
    assert environment["Q1_DIAGNOSING_POSITIVE_EVIDENCE_FILE"] == "positive.json"
    assert environment["Q1_ONLY_SCENARIO"] == "treatment"
    assert json.loads(environment["Q1_SKILL_SOURCE_CHECKSUM_JSON"])[
        "otherpro/skills/skills/engineering/diagnosing-bugs/scripts/hitl-loop.template.sh"
    ]


def _load_launcher() -> ModuleType:
    """按路径加载Q1脚本，并临时暴露其同目录LIVE launcher依赖。"""

    scripts_dir = ROOT / "scripts"
    path = scripts_dir / "run_agent_quality_q1_browser_regression.py"
    original_path = list(sys.path)
    try:
        sys.path.insert(0, str(scripts_dir))
        spec = importlib.util.spec_from_file_location("agent_quality_q1_launcher", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path[:] = original_path

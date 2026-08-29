"""
@Time       : 2026/08/15
@Author     : zhanglp8181
@File       : run_agent_quality_q1_browser_regression.py
@CallChain  : 管理数据库ModelConfig → 隔离全栈 → Q1真实Chromium/模型四象限A/B
@Description: 以最小环境运行AgentLoop与Skill质量探索批，并固化无密钥源码指纹证据。
"""

from __future__ import annotations

import json
from hashlib import sha256
import os
from pathlib import Path
import subprocess
import sys

import run_live_attachment_browser_regression as live_launcher


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend-enterprise"
UPSTREAM_SKILLS = ROOT / "otherpro" / "skills"
SKILLS_ROOT = UPSTREAM_SKILLS / "skills"
WRITING_SKILL_DIR = UPSTREAM_SKILLS / "skills" / "productivity" / "writing-for-agents"
CODEBASE_SKILL_DIR = UPSTREAM_SKILLS / "skills" / "engineering" / "codebase-design"
DIAGNOSING_SKILL_DIR = UPSTREAM_SKILLS / "skills" / "engineering" / "diagnosing-bugs"
# PlanRevision Q1-WRITING-20260828-03：上游新增 implement-spec 与 retro 两个
# in-progress Skill，并调整 in-progress 说明；已重新核对候选、权限、资源清单和许可证。
# 本项目只消费已审核的 engineering/productivity/misc 候选，不把 in-progress 内容纳入执行。
EXPECTED_SKILLS_REVISION = "6654f6b60cd9d5be8b54c6fafe44346dabeb3b76"
Q1_PROFILES = {
    "writing": {
        "skill_dir": WRITING_SKILL_DIR,
        "e2e_file": "e2e/agent-quality-q1.live.fullstack.e2e.ts",
    },
    "writing-fair": {
        "skill_dir": WRITING_SKILL_DIR,
        "e2e_file": "e2e/agent-quality-q1.live.fullstack.e2e.ts",
    },
    "codebase": {
        "skill_dir": CODEBASE_SKILL_DIR,
        "e2e_file": "e2e/agent-quality-q1-codebase.live.fullstack.e2e.ts",
    },
    "plain": {
        "skill_dir": None,
        "e2e_file": "e2e/agent-quality-q1-plain.live.fullstack.e2e.ts",
    },
    "plain-simple": {
        "skill_dir": None,
        "e2e_file": "e2e/agent-quality-q1-plain-simple.live.fullstack.e2e.ts",
    },
    "ordinary": {
        "skill_dir": WRITING_SKILL_DIR,
        "e2e_file": "e2e/agent-quality-q1-ordinary.live.fullstack.e2e.ts",
    },
    "event-window": {
        "skill_dir": None,
        "e2e_file": "e2e/agent-quality-event-window.live.fullstack.e2e.ts",
    },
    "cross-turn": {
        "skill_dir": None,
        "e2e_file": "e2e/agent-quality-q1-cross-turn.live.fullstack.e2e.ts",
    },
    "unrelated": {
        "skill_dir": WRITING_SKILL_DIR,
        "e2e_file": "e2e/agent-quality-q1-unrelated.live.fullstack.e2e.ts",
    },
    "diagnosing": {
        "skill_dir": DIAGNOSING_SKILL_DIR,
        "e2e_file": "e2e/agent-quality-q1-diagnosing.live.fullstack.e2e.ts",
    },
    "diagnosing-positive": {
        "skill_dir": DIAGNOSING_SKILL_DIR,
        "e2e_file": "e2e/agent-quality-q1-diagnosing-positive.live.fullstack.e2e.ts",
    },
    "skill-sample": {
        "skill_dir": None,
        "e2e_file": "e2e/agent-quality-q1.live.fullstack.e2e.ts",
    },
}


def main() -> int:
    """读取管理端权威模型并运行Q1探索批，模型密钥只进入隔离后端进程。"""

    profile_name, profile = _selected_profile()
    raw_skill_dir = profile["skill_dir"]
    skill_dir = Path(raw_skill_dir) if raw_skill_dir is not None else None
    if skill_dir is not None:
        _assert_skill_source(skill_dir=skill_dir)
    sys.path.insert(0, str(BACKEND))
    os.chdir(BACKEND)
    from sqlmodel import Session, select

    from app.db import engine
    from app.db.models import ModelConfig
    from app.security.encryption import decrypt_secret

    with Session(engine) as db:
        requested_model_id = os.environ.get("Q1_SOURCE_MODEL_CONFIG_ID", "").strip()
        statement = select(ModelConfig).where(
            ModelConfig.tenant_id == "tenant_demo",
            ModelConfig.enabled == True,  # noqa: E712 - SQLModel布尔表达式。
            ModelConfig.preflight_status == "ready",
        )
        if requested_model_id:
            statement = statement.where(ModelConfig.id == requested_model_id)
        else:
            statement = statement.where(
                ModelConfig.is_default == True,  # noqa: E712 - 管理端默认配置是权威来源。
            )
        model = db.exec(statement).first()
        if model is None:
            raise RuntimeError("tenant_demo没有可用于Q1的已启用且通过预检ModelConfig")
        api_key = decrypt_secret(model.api_key_encrypted)
        base_url = str(model.base_url or "").strip()
        model_name = str(model.model or "").strip()
        model_id = model.id
        capability_checksum = str(model.capability_checksum or "")
        display_name = str(model.name)
        extra_body = dict(model.extra_body_json or {})
        model_temperature = float(model.temperature)
        model_max_output_tokens = int(model.max_output_tokens)
    if not api_key or not base_url or not model_name:
        raise RuntimeError("Q1权威ModelConfig缺少可运行连接信息")

    port = int(os.environ.get("FULLSTACK_E2E_PORT", str(41_000 + os.getpid() % 1_000)))
    runtime_dir = Path(
        os.environ.get("FULLSTACK_E2E_RUNTIME_DIR", f"/tmp/gongge-agent-quality-q1-{port}")
    )
    fingerprints = _certification_fingerprints(profile_name=profile_name)
    server_env = os.environ.copy()
    server_env.update(
        {
            "LIVE_ATTACHMENT_E2E": "1",
            "LIVE_ATTACHMENT_MODEL_API_KEY": api_key,
            "LIVE_ATTACHMENT_MODEL_BASE_URL": base_url,
            "LIVE_ATTACHMENT_PROVIDER_ENDPOINT": live_launcher._public_provider_endpoint(base_url),
            "LIVE_ATTACHMENT_MODEL_NAME": model_name,
            "LIVE_ATTACHMENT_MODEL_DISPLAY_NAME": display_name,
            "LIVE_ATTACHMENT_MODEL_EXTRA_BODY_JSON": json.dumps(
                extra_body, ensure_ascii=False, separators=(",", ":")
            ),
            "LIVE_ATTACHMENT_SOURCE_MODEL_CONFIG_ID": model_id,
            "LIVE_ATTACHMENT_MODEL_CAPABILITY_CHECKSUM": capability_checksum,
            "LIVE_ATTACHMENT_MODEL_TEMPERATURE": str(model_temperature),
            "LIVE_ATTACHMENT_MODEL_MAX_OUTPUT_TOKENS": str(model_max_output_tokens),
            "FULLSTACK_E2E_PORT": str(port),
            "FULLSTACK_E2E_RUNTIME_DIR": str(runtime_dir),
        }
    )
    browser_env = _q1_browser_environment(
        server_env,
        fingerprints=fingerprints,
        model_id=model_id,
        model_name=model_name,
        capability_checksum=capability_checksum,
        public_endpoint=live_launcher._public_provider_endpoint(base_url),
        profile_name=profile_name,
        skill_dir=skill_dir,
    )
    server: subprocess.Popen[bytes] | None = None
    process_group: int | None = None
    with live_launcher._termination_signal_guard():
        try:
            server = subprocess.Popen(
                [
                    str(BACKEND / ".venv" / "bin" / "python"),
                    str(FRONTEND / "e2e" / "start_fullstack_server.py"),
                ],
                cwd=ROOT,
                env=server_env,
                start_new_session=True,
            )
            process_group = server.pid
            live_launcher._wait_for_health(port, server)
            completed = subprocess.run(
                [
                    "npx",
                    "playwright",
                    "test",
                    str(profile["e2e_file"]),
                    "--config=playwright.fullstack.config.ts",
                    "--project=chromium",
                    *sys.argv[1:],
                ],
                cwd=FRONTEND,
                env=browser_env,
                check=False,
            )
            return completed.returncode
        finally:
            try:
                if server is not None and process_group is not None:
                    live_launcher._terminate_process_group(server, process_group=process_group)
            finally:
                # 调试真实闭环时允许保留 SQLite 和 managed workspace；默认仍清理，
                # 避免回归批次把用户数据留在 /tmp。该开关只用于只读诊断证据。
                if os.environ.get("PRESERVE_FULLSTACK_E2E") != "1":
                    live_launcher._remove_runtime_dir(runtime_dir)


def _selected_profile() -> tuple[str, dict[str, object]]:
    """解析受支持的Q1场景，未知profile稳定拒绝而非退回默认Skill。"""

    profile_name = os.environ.get("Q1_PROFILE", "writing").strip().lower()
    profile = Q1_PROFILES.get(profile_name)
    if profile is None:
        raise RuntimeError(f"不支持的Q1 profile: {profile_name}")
    if profile_name == "skill-sample":
        configured = os.environ.get("Q1_SAMPLE_SKILL_DIR", "").strip()
        if not configured:
            raise RuntimeError("skill-sample必须显式提供Q1_SAMPLE_SKILL_DIR")
        profile = {**profile, "skill_dir": Path(configured).expanduser().resolve()}
    elif (
        profile_name == "ordinary"
        and os.environ.get("Q1_ORDINARY_BENCHMARK", "").strip() == "codebase"
    ):
        # ordinary E2E 与 dynamic codebase 使用同一受审 Skill，但仍保持
        # ordinary 路由层；目录选择必须和浏览器侧 benchmark 分支同步。
        profile = {**profile, "skill_dir": CODEBASE_SKILL_DIR}
    return profile_name, profile


def _assert_skill_source(*, skill_dir: Path = WRITING_SKILL_DIR) -> None:
    """校验一次性上游Skill输入的固定revision、许可证和必需文件。"""

    if not skill_dir.is_dir():
        raise RuntimeError(f"Q1 Skill目录不存在: {skill_dir.name}")
    try:
        relative_skill_dir = skill_dir.resolve().relative_to(SKILLS_ROOT.resolve())
    except ValueError as exc:
        raise RuntimeError("Q1 Skill目录必须位于 otherpro/skills/skills 下") from exc
    if relative_skill_dir.parts and relative_skill_dir.parts[0] in {"deprecated", "in-progress"}:
        raise RuntimeError("Q1 Skill抽样不允许 deprecated 或 in-progress 目录")
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=UPSTREAM_SKILLS,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if revision != EXPECTED_SKILLS_REVISION:
        raise RuntimeError("Q1上游Skill revision与冻结值不一致")
    license_text = (UPSTREAM_SKILLS / "LICENSE").read_text(encoding="utf-8")
    if "MIT License" not in license_text:
        raise RuntimeError("Q1上游Skill许可证不是预期MIT")
    for relative_path in _required_skill_files(skill_dir):
        if not (skill_dir / relative_path).is_file():
            raise RuntimeError(f"Q1 Skill缺少必需资源: {relative_path}")


def _skill_source_checksums(*, skill_dir: Path = WRITING_SKILL_DIR) -> dict[str, str]:
    """记录浏览器真实导入的上游Skill文件checksum，不把otherpro变成生产依赖。"""

    paths = (
        UPSTREAM_SKILLS / "LICENSE",
        *(skill_dir / name for name in _required_skill_files(skill_dir)),
    )
    return {
        str(path.relative_to(ROOT)): sha256(path.read_bytes()).hexdigest() for path in paths
    }


def _q1_browser_environment(
    server_environment: dict[str, str],
    *,
    fingerprints: dict[str, str],
    model_id: str,
    model_name: str,
    capability_checksum: str,
    public_endpoint: str,
    profile_name: str = "writing",
    skill_dir: Path | None = WRITING_SKILL_DIR,
) -> dict[str, str]:
    """从LIVE安全白名单投影Q1公开事实，模型密钥和父进程秘密不得进入浏览器树。"""

    browser_environment = live_launcher._browser_environment(server_environment)
    browser_environment.update(
        {
            "Q1_AGENT_QUALITY_E2E": "1",
            "Q1_PROFILE": profile_name,
            "Q1_CERTIFICATION_FINGERPRINT_JSON": json.dumps(
                fingerprints, sort_keys=True, separators=(",", ":")
            ),
            "Q1_SOURCE_MODEL_CONFIG_ID": model_id,
            "Q1_PROVIDER_ENDPOINT": public_endpoint,
            "Q1_MODEL_NAME": model_name,
            "Q1_MODEL_TEMPERATURE": str(
                server_environment.get("LIVE_ATTACHMENT_MODEL_TEMPERATURE", "")
            ),
            "Q1_MODEL_MAX_OUTPUT_TOKENS": str(
                server_environment.get("LIVE_ATTACHMENT_MODEL_MAX_OUTPUT_TOKENS", "")
            ),
            "Q1_MODEL_CAPABILITY_CHECKSUM": capability_checksum,
            "FULLSTACK_E2E_REUSE_EXISTING_SERVER": "1",
            "PRESERVE_FULLSTACK_E2E": "1",
        }
    )
    if skill_dir is not None:
        if skill_dir is None:
            raise RuntimeError("Skill Q1 profile缺少受审Skill目录")
        skill_directory_key = {
            "writing": "Q1_WRITING_SKILL_DIR",
            "writing-fair": "Q1_WRITING_SKILL_DIR",
            "codebase": "Q1_CODEBASE_SKILL_DIR",
            "diagnosing": "Q1_DIAGNOSING_SKILL_DIR",
            "diagnosing-positive": "Q1_DIAGNOSING_SKILL_DIR",
            "unrelated": "Q1_WRITING_SKILL_DIR",
            "ordinary": "Q1_WRITING_SKILL_DIR",
            "skill-sample": "Q1_WRITING_SKILL_DIR",
        }.get(profile_name)
        if (
            profile_name == "ordinary"
            and server_environment.get("Q1_ORDINARY_BENCHMARK", "").strip() == "codebase"
        ):
            # ordinary E2E 会依据 benchmark 选择 Skill；不能把 codebase 目录
            # 塞进 writing 变量，否则浏览器白名单会丢失真正的输入来源，
            # 使正式批次只能依赖测试进程的旁路补丁才能启动。
            skill_directory_key = "Q1_CODEBASE_SKILL_DIR"
        if skill_directory_key is None:
            raise RuntimeError(f"Skill Q1 profile未配置安全目录变量: {profile_name}")
        browser_environment.update(
            {
                skill_directory_key: str(skill_dir),
                "Q1_SKILLS_REVISION": EXPECTED_SKILLS_REVISION,
                "Q1_SKILL_SOURCE_CHECKSUM_JSON": json.dumps(
                    _skill_source_checksums(skill_dir=skill_dir),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
        )
    for name in (
        "EVENT_WINDOW_E2E",
        "EVENT_WINDOW_EVIDENCE_FILE",
        "Q1_ONLY_SCENARIO",
        "Q1_ORDER_SEED",
        "Q1_CERTIFICATION_RUN_ID",
        "Q1_CODEBASE_EVIDENCE_FILE",
        "Q1_AGENT_QUALITY_EVIDENCE_FILE",
        "Q1_PLAIN_EVIDENCE_FILE",
        "Q1_PLAIN_SIMPLE_EVIDENCE_FILE",
        "Q1_CROSS_TURN_EVIDENCE_FILE",
        "Q1_UNRELATED_EVIDENCE_FILE",
        "Q1_DIAGNOSING_EVIDENCE_FILE",
        "Q1_DIAGNOSING_POSITIVE_EVIDENCE_FILE",
        "Q1_CONTROL_AGENT_ID",
        "Q1_TREATMENT_AGENT_ID",
        "Q1_WRITING_BENCHMARK",
        "Q1_ORDINARY_BENCHMARK",
        "Q1_SAMPLE_SKILL_NAME",
        "Q1_SAMPLE_TASK_FAMILY",
    ):
        value = server_environment.get(name, "").strip()
        if value:
            browser_environment[name] = value
    return browser_environment


def _required_skill_files(skill_dir: Path) -> tuple[str, ...]:
    """返回每个已审核Q1 Skill必须完整导入并固化的文件清单。"""

    if skill_dir == WRITING_SKILL_DIR:
        return ("SKILL.md", "SKILL-MECHANICS.md", "agents/openai.yaml")
    if skill_dir == CODEBASE_SKILL_DIR:
        return (
            "SKILL.md",
            "DEEPENING.md",
            "DESIGN-IT-TWICE.md",
            "agents/openai.yaml",
        )
    if skill_dir == DIAGNOSING_SKILL_DIR:
        return (
            "SKILL.md",
            "scripts/hitl-loop.template.sh",
            "agents/openai.yaml",
        )
    if (skill_dir / "agents" / "openai.yaml").is_file():
        return ("SKILL.md", "agents/openai.yaml")
    return ("SKILL.md",)


def _certification_fingerprints(*, profile_name: str = "writing") -> dict[str, str]:
    """冻结Q1路由、Skill投影、Dynamic执行、附件和浏览器Oracle关键文件。"""

    profile = Q1_PROFILES.get(profile_name)
    if profile is None:
        raise RuntimeError(f"不支持的Q1 profile: {profile_name}")
    relative_paths = (
        "backend/app/core/agent_loop.py",
        "backend/app/core/context_projection.py",
        "backend/app/core/non_sop_capability.py",
        "backend/app/core/response_generator.py",
        "backend/app/api/chat.py",
        "backend/app/dynamic_tasks/action_proposer.py",
        "backend/app/dynamic_tasks/agent.py",
        "backend/app/dynamic_tasks/budget_policy.py",
        "backend/app/dynamic_tasks/planning.py",
        "backend/app/dynamic_tasks/planner_service.py",
        "backend/app/dynamic_tasks/result_verifier.py",
        "backend/app/general_skills/runtime.py",
        "backend/app/llm/client.py",
        "backend/app/llm/output_policy.py",
        "backend/app/llm/prompts/response_generator_prompt.md",
        "backend/app/session/input_runtime.py",
        "backend/app/sop_runtime/execution_store.py",
        "frontend-enterprise/src/pages/chat/chatHelpers.tsx",
        "frontend-enterprise/src/pages/chat/useChatSession.ts",
        f"frontend-enterprise/{profile['e2e_file']}",
        "frontend-enterprise/e2e/start_fullstack_server.py",
        "scripts/run_agent_quality_q1_browser_regression.py",
    )
    return {
        relative_path: sha256((ROOT / relative_path).read_bytes()).hexdigest()
        for relative_path in relative_paths
    }


if __name__ == "__main__":
    raise SystemExit(main())

"""
@Time       : 2026/10/03
@Author     : zhanglp8181
@File       : sd7_repair_fullstack_fixture.py
@CallChain  : 显式隔离SD7收尾 → 真实分析/编辑/守卫 → 可控末端SDK
@Description: 只控制供应商响应/暂停，不替换HTTP授权、任务、CAS、发布或UI，不作模型质量证据。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from threading import Lock
import time
from types import SimpleNamespace


def install_repair_sdk(runtime: Path) -> None:
    """只在显式自有临时SQLite或随机MySQL测试服务安装协议传输，拒绝生产目录。"""
    if os.environ.get("FULLSTACK_SD7_REPAIR") != "1":
        return
    control = Path(os.environ["FULLSTACK_SD6B_CONTROL_DIR"]).resolve()
    if not runtime.resolve().is_relative_to(Path("/tmp")) or control.parent != runtime.parent.resolve():
        raise RuntimeError("SD7_REPAIR_REQUIRES_OWN_TEMPORARY_CONTROL")
    from app.db import engine
    from app.llm import client

    if engine.dialect.name == "mysql":
        if not (engine.url.database or "").startswith("gongge_xuban_test_"):
            raise RuntimeError("SD7_REPAIR_REQUIRES_RANDOM_MYSQL")
    elif not Path(engine.url.database or "").resolve().is_relative_to(runtime.resolve()):
        raise RuntimeError("SD7_REPAIR_REQUIRES_OWN_SQLITE")
    control.mkdir(parents=True, exist_ok=True)
    lock = Lock()

    class Provider:
        """保留真实LLMClient协议与内部修复，仅固定SDK响应和首次响应等待窗口。"""

        def __init__(self, **kwargs):
            """不读取/记录密钥或baseURL，构建原ChatCompletions所需接口。"""
            self.case = "ordinary"
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def with_options(self, **kwargs):
            """接收真实client的deadline及禁SDK自身重试设置。"""
            return self

        def create(self, **kwargs):
            """暂停第一SDK；另一页面正式提交后返回坏JSON，由真实派发守卫决定能否修复。"""
            payload = json.loads(kwargs["messages"][-1]["content"])
            kind = "feedback" if "feedback" in payload else "candidate"
            pause = None
            with lock:
                command = control / "sdk-pause.json"
                if command.exists():
                    proposed = json.loads(command.read_text())
                    if proposed["kind"] == kind:
                        if proposed["case"] not in {
                            "rating", "delete", "reanalyze", "withdraw", "candidate", "network"
                        }:
                            raise RuntimeError("SD7_REPAIR_INVALID_CONTROL")
                        pause = proposed
                        command.unlink()
                        self.case = proposed["case"]
                with (control / "sdk-observations.safe.jsonl").open("a") as log:
                    log.write(json.dumps({"case": self.case, "kind": kind,
                        "revision": payload.get("feedback", {}).get("revision"),
                        "stream": bool(kwargs.get("stream")), "actual_sdk_dispatch": True,
                        "real_provider_quality": False,
                        "request_hash": hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
                    }) + "\n")
            if pause:
                (control / ("sdk-entered-" + self.case)).touch()
                deadline = time.monotonic() + 90
                while not (control / ("sdk-release-" + self.case)).exists():
                    if time.monotonic() >= deadline:
                        raise RuntimeError("SD7_REPAIR_CONTROL_DEADLINE")
                    time.sleep(0.025)
                text = "{ invalid-json"
            elif kind == "feedback":
                text = json.dumps({"bucket": "skill_issue", "summary": "Synthetic protocol analysis",
                                   "reason": "Synthetic SDK response only"})
            elif "candidate_skill" in payload:
                text = json.dumps({"passed": True, "summary": "Synthetic protocol review",
                                   "rubric_results": []})
            else:
                text = json.dumps({"patches": [{"path": "description",
                                    "value": "Synthetic SDK reconnect candidate"}],
                                   "assistant_message": "Synthetic protocol response",
                                   "changed_paths": ["/description"]})
            if kwargs.get("stream"):
                return iter([SimpleNamespace(choices=[SimpleNamespace(
                    delta=SimpleNamespace(content=text), finish_reason="stop")],
                    usage=None, id="synthetic-repair-response")])
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text),
                finish_reason="stop")], usage=None, id="synthetic-repair-response")

    client.OpenAI = Provider
    from app.api import skills
    from app.skills.stream_jobs import stream_jobs

    original_job = skills._run_feedback_rewrite_stream_job

    def observe_finished_job(job_id, prepared):
        """原任务原样执行后只记录真实终态，不改变fence、缓存、结算或异常处理。"""
        try:
            return original_job(job_id, prepared)
        finally:
            job = stream_jobs.get(job_id)
            with lock:
                with (control / "job-observations.safe.jsonl").open("a") as log:
                    log.write(json.dumps({"job_id": job_id, "status": job.status if job else "missing",
                                          "candidate_id": prepared.fence.candidate_id}) + "\n")

    skills._run_feedback_rewrite_stream_job = observe_finished_job

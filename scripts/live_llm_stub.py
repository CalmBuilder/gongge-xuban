"""
@Time       : 2026/07/27 00:00
@Author     : zhanglp8181
@File       : live_llm_stub.py
@CallChain  : 手工浏览器回归 → OpenAI 兼容 HTTP → Router/Step/Knowledge/Response
@Description: 为真实前后端与数据库回归提供无外部余额依赖的固定 LLM 响应，不进入生产启动链。
"""

from __future__ import annotations

import argparse
import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class LiveLlmStubHandler(BaseHTTPRequestHandler):
    """处理最小 OpenAI Chat Completions 协议并按阶段返回固定结果。"""

    server_version = "GonggeLiveLlmStub/1.0"

    def do_POST(self) -> None:  # noqa: N802
        """接收聊天补全请求，并支持普通 JSON 与 SSE 流式响应。"""

        if self.path.rstrip("/") != "/v1/chat/completions":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or "0")
        payload = json.loads(self.rfile.read(length) or b"{}")
        content = _response_content(payload)
        if payload.get("stream"):
            self._write_stream(payload, content)
            return
        self._write_json(payload, content)

    def log_message(self, _format: str, *_args: object) -> None:
        """关闭包含请求细节的默认访问日志，避免手工回归输出提示载荷。"""

    def _write_json(self, payload: dict[str, Any], content: str) -> None:
        """返回 OpenAI SDK 可解析的非流式补全对象。"""

        body = json.dumps(
            {
                "id": "chatcmpl-live-stub",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": payload.get("model") or "gongge-live-stub",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
            ensure_ascii=False,
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _write_stream(self, payload: dict[str, Any], content: str) -> None:
        """以两个 SSE 数据块返回固定文本，覆盖真实浏览器流式解析链。"""

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        for text, finish_reason in ((content, None), ("", "stop")):
            chunk = {
                "id": "chatcmpl-live-stub",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": payload.get("model") or "gongge-live-stub",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": text} if text else {},
                        "finish_reason": finish_reason,
                    }
                ],
            }
            self.wfile.write(
                f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
            )
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def _response_content(payload: dict[str, Any]) -> str:
    """根据系统提示识别调用阶段，并生成满足该阶段契约的最小响应。"""

    messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
    primary_prompt = (
        str(messages[0].get("content") or "")
        if messages and isinstance(messages[0], dict)
        else ""
    )
    current_input = (
        str(messages[-1].get("content") or "")
        if messages and isinstance(messages[-1], dict)
        else ""
    )
    stage_prompt = (
        current_input if "统一执行引擎" in primary_prompt else primary_prompt
    )
    serialized = json.dumps(messages, ensure_ascii=False)
    if "你是通用技能选择器" in stage_prompt:
        return json.dumps(
            {
                "use_general_skill": False,
                "selected_slug": None,
                "use_knowledge": False,
                "knowledge_query": None,
                "confidence": 0.99,
            }
        )
    if "企业技能路由器" in stage_prompt or '"stage":"router"' in stage_prompt:
        return json.dumps(
            {
                "decision": "start_new_task",
                "target_skill_id": "skill_graph_visual_demo",
                "confidence": 0.99,
                "user_intent": "验证知识分支中的年假政策检索",
                "reason": "用户明确要求运行图结构知识路径。",
            },
            ensure_ascii=False,
        )
    if "企业技能执行助手" in stage_prompt or '"stage":"step_agent"' in stage_prompt:
        return json.dumps(
            {
                "action": "advance",
                "slot_updates": {
                    "request_type": "knowledge",
                    "request_detail": "员工考勤迟到政策",
                },
                "next_step_id": "classify_path",
                "is_step_completed": True,
            },
            ensure_ascii=False,
        )
    if "反思检查器" in stage_prompt or '"stage":"reflection"' in stage_prompt:
        return '{"action":"pass","needs_retry":false}'
    if "知识库文档路由助手" in stage_prompt:
        return json.dumps(
            {"selected_document_ids": _unique_ids(serialized, r"kdoc_[A-Za-z0-9]+", 5)}
        )
    if "渐进式知识检索路由助手" in stage_prompt:
        return json.dumps(
            {"selected_bucket_ids": _unique_ids(serialized, r"kbucket_[A-Za-z0-9]+", 4)}
        )
    if "企业对话助手" in stage_prompt or '"stage":"response_generator"' in stage_prompt:
        return (
            "知识路径验证成功。员工考勤迟到政策应以员工手册中的适用条件和考勤制度为准；"
            "本次回答仅引用检索到的内部制度依据。[1]"
        )
    if "会话标题编辑器" in primary_prompt:
        return '{"title":"图结构知识路径验证"}'
    print(
        f"live-llm-stub unrecognized prompt: primary={primary_prompt[:100]!r}; "
        f"current={current_input[:240]!r}",
        flush=True,
    )
    return "{}"


def _unique_ids(serialized: str, pattern: str, limit: int) -> list[str]:
    """按出现顺序提取候选 ID，避免返回输入之外的文档或知识桶。"""

    return list(dict.fromkeys(re.findall(pattern, serialized)))[:limit]


def main() -> None:
    """解析监听参数并运行只供手工回归使用的本地 HTTP 服务。"""

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=52099)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), LiveLlmStubHandler)
    print(f"live-llm-stub listening on {args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

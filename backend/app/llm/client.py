"""
@Time       : 2026/08/28 13:20
@Author     : zhanglp8181
@File       : client.py
@CallChain  : Agent/知识/技能阶段 → LLMClient → OpenAI 兼容 Chat Completions
@Description: 统一模型请求、上下文裁剪、流式解析、空响应诊断及推理截断重试。
"""

from __future__ import annotations

import ast
import base64
from collections.abc import Callable, Iterator
import copy
import hashlib
import json
import math
from queue import Empty, Full, Queue
import re
from threading import Event, Thread
import time
from typing import Any
from urllib.parse import urlsplit

from openai import OpenAI

from app.cancellation import raise_if_cancelled, run_cancellable
from app.config import get_settings
from app.db.models import ModelConfig
from app.llm.output_policy import operation_output_tokens, operation_thinking_mode
from app.llm.stage_protocol import (
    STAGE_PROTOCOL_KEY,
    TURN_STAGE_MESSAGES_KEY,
    render_stage_user_message,
)
from app.observability.spans import current_llm_operation, llm_span_attributes, start_llm_call
from app.security.encryption import decrypt_secret


class LLMError(Exception):
    """Raised when an LLM provider request or response normalization fails."""


class LLMStreamCancelled(LLMError):
    """表示用户已取消模型流，调用方应保留已发送部分而非生成失败文案。"""


_STREAM_DONE = object()


def _best_effort_close_stream(stream: Any) -> None:
    """在 daemon 线程中尝试关闭第三方流，禁止其 close 阻塞取消返回。"""

    close = getattr(stream, "close", None)
    if not callable(close):
        return

    def invoke() -> None:
        """隔离 provider close 的阻塞或异常，它们不改变本地取消终态。"""

        try:
            close()
        except BaseException:
            return

    Thread(target=invoke, name="llm-provider-close", daemon=True).start()


def _interruptible_provider_stream(
    stream_factory: Callable[[], Any],
    is_cancelled: Callable[[], bool] | None,
) -> Iterator[Any]:
    """在独立生产线程中等待 provider，使首 token 前与流中取消都能及时返回。"""

    if is_cancelled is None:
        yield from stream_factory()
        return

    queue: Queue[object] = Queue(maxsize=64)
    active_stream: list[Any] = []
    producer_stop = Event()

    def produce() -> None:
        """创建并消费 provider 流，将 chunk 或异常传递给可取消消费者。"""

        try:
            stream = stream_factory()
            active_stream.append(stream)
            for item in stream:
                if producer_stop.is_set():
                    break
                while not producer_stop.is_set():
                    try:
                        queue.put(item, timeout=0.05)
                        break
                    except Full:
                        continue
        except BaseException as exc:
            if not producer_stop.is_set():
                queue.put(exc)
        finally:
            if not producer_stop.is_set():
                queue.put(_STREAM_DONE)

    producer = Thread(target=produce, name="llm-provider-stream", daemon=True)
    producer.start()
    try:
        while True:
            if is_cancelled():
                if active_stream:
                    _best_effort_close_stream(active_stream[0])
                raise LLMStreamCancelled("LLM stream cancelled by user")
            try:
                item = queue.get(timeout=0.05)
            except Empty:
                continue
            if item is _STREAM_DONE:
                return
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        producer_stop.set()
        if is_cancelled() and active_stream:
            _best_effort_close_stream(active_stream[0])


JSON_REPAIR_ATTEMPTS = 3
EMPTY_RESPONSE_RETRIES = 2
PROVIDER_TRANSIENT_RETRIES = 2
PROVIDER_RETRY_BASE_SECONDS = 1.0
EMPTY_RESPONSE_MESSAGE = "Model returned an empty response"
DEFAULT_MODEL_API_TIMEOUT_SECONDS = 600.0
DEFAULT_INPUT_TOKEN_BUDGET = 32_000
TURN_STAGE_MESSAGE_MARKER = "_agent_turn_message"
REASONING_TOKEN_ESCALATION_CEILING = 32_768
OPTIONAL_INPUT_PROBE_MAX_TOKENS = 256
PROVIDER_CONTENT_PARTS_KEY = "_provider_content_parts"


def _escalate_reasoning_token_budget(current_max_tokens: int) -> int:
    """推理阶段因长度耗尽时翻倍重试配额，同时不降低已超过上限的显式配置。"""
    if current_max_tokens >= REASONING_TOKEN_ESCALATION_CEILING:
        return current_max_tokens
    return min(current_max_tokens * 2, REASONING_TOKEN_ESCALATION_CEILING)


def _provider_max_output_tokens(base_url: str | None, model: str | None) -> int | None:
    """返回已知供应商硬上限，避免管理端高容量配置生成 provider 400。"""

    del model  # Ark 当前公开兼容端点对该类模型统一执行 131072 上限。
    try:
        parsed = urlsplit(str(base_url or ""))
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if host in {"ark.cn-beijing.volces.com", "ark.ap-southeast.bytepluses.com"}:
        return 131_072
    return None


def _json_repair_output_token_budget(
    user_payload: dict[str, Any] | str,
    base_max_tokens: int,
) -> int:
    """仅对已确认的长JSON截断修复轮有界扩容，普通语法修复保持原预算。"""

    if not isinstance(user_payload, dict):
        return base_max_tokens
    repair = user_payload.get("_json_repair")
    if not isinstance(repair, dict):
        return base_max_tokens
    instruction = str(repair.get("instruction") or "")
    if "疑似因输出过长而被截断" not in instruction:
        return base_max_tokens
    try:
        repair_attempt = max(1, min(int(repair.get("attempt") or 1), 2))
    except (TypeError, ValueError):
        repair_attempt = 1
    budget = base_max_tokens
    for _ in range(repair_attempt):
        budget = _escalate_reasoning_token_budget(budget)
    return budget


def _length_retry_payload(
    user_payload: dict[str, Any],
    *,
    attempt: int,
) -> dict[str, Any]:
    """为可解析但以 ``length`` 结束的 JSON 生成有界扩容提示，不猜测业务字段。"""

    retry_payload = copy.deepcopy(user_payload)
    retry_payload["_json_repair"] = {
        "attempt": max(1, min(attempt, 2)),
        "max_attempts": 2,
        "previous_output": "JSON 可解析，但 provider 以 finish_reason=length 结束。",
        "parser_error": "provider_finish_reason=length",
        "instruction": (
            "疑似因输出过长而被截断；请在更高输出配额下重新返回一个完整、最小的 JSON 对象，"
            "不要复制最终答案或添加契约外字段。"
        ),
    }
    return retry_payload


class _CurrentStageText(str):
    pass


class LLMClient:
    def __init__(
        self,
        model_config: ModelConfig,
        *,
        timeout_seconds: float | None = None,
    ):
        api_key = decrypt_secret(model_config.api_key_encrypted)
        if not api_key:
            raise LLMError("Model API key is not configured")
        self.timeout_seconds = timeout_seconds if timeout_seconds is not None else (
            get_settings().model_api_timeout_seconds or DEFAULT_MODEL_API_TIMEOUT_SECONDS
        )
        self.base_url = str(model_config.base_url or "")
        self.client = OpenAI(
            api_key=api_key,
            base_url=self.base_url,
            timeout=self.timeout_seconds,
            max_retries=0,
        )
        self.model = model_config.model
        self.temperature = model_config.temperature
        self.configured_max_output_tokens = max(1, int(model_config.max_output_tokens or 1))
        provider_limit = _provider_max_output_tokens(self.base_url, self.model)
        self.max_output_tokens = min(
            self.configured_max_output_tokens,
            provider_limit if provider_limit is not None else self.configured_max_output_tokens,
        )
        self.extra_body = _normalize_extra_body(
            getattr(model_config, "extra_body_json", {})
        )
        settings = get_settings()
        self.thinking_mode = (
            _thinking_mode_from_extra_body(self.extra_body)
            or _thinking_mode_for_model(
                getattr(settings, "model_thinking_mode", ""),
                getattr(settings, "model_thinking_models", ""),
                self.model,
            )
        )

    def probe_model_catalog(self) -> list[str]:
        """读取兼容端点模型目录，用于区分认证成功与模型名称配置错误。"""

        try:
            page = self.client.models.list()
        except Exception as exc:
            raise LLMError(_provider_failure_detail(self, exc)) from exc
        return [
            str(getattr(item, "id", "")).strip()
            for item in getattr(page, "data", []) or []
            if str(getattr(item, "id", "")).strip()
        ]

    def probe_text_connection(self) -> str:
        """执行一次低token最小生成，验证模型调用而不触发普通回答的多轮重试。"""

        try:
            thinking_kwargs = _thinking_request_kwargs(
                getattr(self, "thinking_mode", ""),
                getattr(self, "extra_body", {}),
            )
            completion = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "你是连接测试助手。"},
                    {"role": "user", "content": "只回复：连接成功"},
                ],
                temperature=0,
                # 推理模型会先消耗 reasoning tokens；16 token 可能 HTTP 成功却
                # 没有正文，造成“账户可用但连接失败”的假阴性。
                max_tokens=256,
                **thinking_kwargs,
            )
        except Exception as exc:
            raise LLMError(_provider_failure_detail(self, exc)) from exc
        content = _completion_message_content(completion).strip()
        if not content:
            raise LLMError("MODEL_CONNECTION_EMPTY_RESPONSE")
        return content

    def generate_text(
        self,
        system_prompt: str,
        user_payload: dict[str, Any] | str,
        response_format: dict[str, str] | None = None,
    ) -> str:
        """生成非流式文本，并对只产生推理且长度截断的空正文扩大配额重试。"""
        operation = current_llm_operation()
        max_output_tokens = operation_output_tokens(operation, self.max_output_tokens)
        max_output_tokens = _json_repair_output_token_budget(
            user_payload,
            max_output_tokens,
        )
        context_messages, serialized = _prepare_user_input(user_payload)
        request_messages = _request_messages(system_prompt, context_messages, serialized)
        request_messages = _fit_request_messages(request_messages)
        if isinstance(user_payload, dict) and isinstance(
            user_payload.get(STAGE_PROTOCOL_KEY), dict
        ):
            self._last_stage_request_user_content = copy.deepcopy(
                request_messages[-1].get("content")
            )
        request_shape = _request_shape_metrics(
            system_prompt, context_messages, serialized, request_messages
        )
        timeout_seconds = float(
            getattr(self, "timeout_seconds", DEFAULT_MODEL_API_TIMEOUT_SECONDS)
            or DEFAULT_MODEL_API_TIMEOUT_SECONDS
        )
        now = time.monotonic()
        json_deadline = getattr(self, "_json_deadline", None)
        if isinstance(json_deadline, (int, float)):
            remaining_json_budget = float(json_deadline) - now
            if remaining_json_budget <= 0:
                raise LLMError("MODEL_CALL_DEADLINE_EXCEEDED")
            timeout_seconds = min(timeout_seconds, remaining_json_budget)
        deadline = now + max(0.1, timeout_seconds)
        try:
            request: dict[str, Any] = {
                "model": self.model,
                "messages": request_messages,
                "temperature": self.temperature,
                "max_tokens": max_output_tokens,
            }
            if response_format:
                request["response_format"] = response_format
            thinking_mode = operation_thinking_mode(
                operation,
                getattr(self, "thinking_mode", ""),
            )
            request.update(
                _thinking_request_kwargs(thinking_mode, getattr(self, "extra_body", {}))
            )
            empty_diagnostics: list[str] = []
            current_max_tokens = max_output_tokens
            for attempt in range(EMPTY_RESPONSE_RETRIES + 1):
                request["max_tokens"] = current_max_tokens
                span = start_llm_call(
                    model=self.model,
                    endpoint=_endpoint_label(getattr(self, "base_url", "")),
                    request_kind="chat.completions",
                    stream=False,
                    attempt=attempt + 1,
                    retry_count=attempt,
                    max_attempts=EMPTY_RESPONSE_RETRIES + 1,
                    max_output_tokens=current_max_tokens,
                    thinking_mode=thinking_mode or "provider_default",
                    **request_shape,
                )
                try:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise LLMError("MODEL_CALL_DEADLINE_EXCEEDED")
                    request_client = self.client
                    with_options = getattr(self.client, "with_options", None)
                    if callable(with_options):
                        request_client = with_options(timeout=max(0.1, remaining))
                    completion = request_client.chat.completions.create(
                        **request,
                    )
                except BaseException as exc:
                    span.fail(exc, **_completion_span_metrics(None))
                    if _is_retryable_provider_error(exc) and attempt < PROVIDER_TRANSIENT_RETRIES:
                        _wait_before_provider_retry(attempt)
                        continue
                    raise
                content = _completion_message_content(completion)
                metrics = _completion_span_metrics(completion)
                if content.strip():
                    self._last_completed_response_metadata = _completion_identity_metadata(
                        completion
                    )
                    span.finish(
                        ttft_ms=span.elapsed_ms(),
                        output_chars=len(content),
                        status="success",
                        **metrics,
                    )
                    if not getattr(self, "_defer_stage_recording", False):
                        _record_stage_exchange(
                            user_payload,
                            content,
                            request_user_content=getattr(
                                self, "_last_stage_request_user_content", None
                            ),
                        )
                    return content
                span.finish(
                    ttft_ms=span.elapsed_ms(),
                    output_chars=0,
                    status="empty",
                    **metrics,
                )
                empty_diagnostics.append(_completion_empty_diagnostic(completion, attempt + 1))
                if (
                    metrics.get("finish_reason") in {"length", "stop"}
                    and metrics.get("reasoning_chars", 0) > 0
                ):
                    # 部分推理模型会在推理完成后以 stop 结束，却没有写入
                    # message.content；这和 length 截断一样需要扩大正文预算，
                    # 否则每次重试都复用同一过小配额并把复杂任务误报为 provider 失败。
                    current_max_tokens = _escalate_reasoning_token_budget(
                        current_max_tokens
                    )
                if attempt >= EMPTY_RESPONSE_RETRIES:
                    raise LLMError(_empty_response_detail(self, empty_diagnostics))
        except Exception as exc:
            if isinstance(exc, LLMError):
                raise
            raise LLMError(_provider_failure_detail(self, exc)) from exc

    def generate_text_stream(
        self,
        system_prompt: str,
        user_payload: dict[str, Any] | str,
        *,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> Iterator[str]:
        """流式生成文本；首个正文发出前若推理长度截断，则扩大配额重新请求。"""
        max_output_tokens = operation_output_tokens(
            current_llm_operation(), self.max_output_tokens
        )
        context_messages, serialized = _prepare_user_input(user_payload)
        request_messages = _request_messages(system_prompt, context_messages, serialized)
        request_messages = _fit_request_messages(request_messages)
        if isinstance(user_payload, dict) and isinstance(
            user_payload.get(STAGE_PROTOCOL_KEY), dict
        ):
            self._last_stage_request_user_content = copy.deepcopy(
                request_messages[-1].get("content")
            )
        request_shape = _request_shape_metrics(
            system_prompt, context_messages, serialized, request_messages
        )
        try:
            empty_diagnostics: list[str] = []
            current_max_tokens = max_output_tokens
            for attempt in range(EMPTY_RESPONSE_RETRIES + 1):
                span = start_llm_call(
                    model=self.model,
                    endpoint=_endpoint_label(getattr(self, "base_url", "")),
                    request_kind="chat.completions",
                    stream=True,
                    attempt=attempt + 1,
                    retry_count=attempt,
                    max_attempts=EMPTY_RESPONSE_RETRIES + 1,
                    max_output_tokens=current_max_tokens,
                    thinking_mode=getattr(self, "thinking_mode", "") or "provider_default",
                    **request_shape,
                )
                stream_usage_metrics: dict[str, Any] = {}
                pending_parts: list[str] = []
                recorded_parts: list[str] = []
                emitted_text = False
                chunk_count = 0
                choice_chunk_count = 0
                reasoning_chars = 0
                output_chars = 0
                first_content_ms: float | None = None
                provider_setup_ms: float | None = None
                finish_reasons: set[str] = set()
                response_ids: set[str] = set()
                try:
                    def create_stream() -> Any:
                        """创建本次 provider 流，供可取消生产线程调用。"""

                        return self.client.chat.completions.create(
                            model=self.model,
                            messages=request_messages,
                            temperature=self.temperature,
                            max_tokens=current_max_tokens,
                            stream=True,
                            **_thinking_request_kwargs(
                                getattr(self, "thinking_mode", ""),
                                getattr(self, "extra_body", {}),
                            ),
                        )

                    for chunk in _interruptible_provider_stream(create_stream, is_cancelled):
                        if provider_setup_ms is None:
                            provider_setup_ms = span.elapsed_ms()
                        chunk_count += 1
                        chunk_usage_metrics = _usage_span_metrics(getattr(chunk, "usage", None))
                        if chunk_usage_metrics:
                            stream_usage_metrics.update(chunk_usage_metrics)
                        response_id = _safe_fragment(getattr(chunk, "id", None), 48)
                        if response_id:
                            response_ids.add(response_id)
                        choices = getattr(chunk, "choices", None) or []
                        if not choices:
                            continue
                        choice_chunk_count += len(choices)
                        choice = choices[0]
                        finish_reason = _safe_fragment(getattr(choice, "finish_reason", None), 32)
                        if finish_reason:
                            finish_reasons.add(finish_reason)
                        delta = getattr(choice, "delta", None)
                        reasoning_chars += len(_reasoning_text(delta))
                        content = _content_text(getattr(delta, "content", None))
                        if not content:
                            continue
                        recorded_parts.append(content)
                        output_chars += len(content)
                        if first_content_ms is None:
                            first_content_ms = span.elapsed_ms()
                        if emitted_text:
                            yield content
                            continue
                        pending_parts.append(content)
                        buffered = "".join(pending_parts)
                        if buffered.strip():
                            emitted_text = True
                            pending_parts.clear()
                            yield buffered
                except BaseException as exc:
                    span.fail(
                        exc,
                        provider_setup_ms=provider_setup_ms,
                        ttft_ms=first_content_ms,
                        output_chars=output_chars,
                        stream_chunks=chunk_count,
                        reasoning_chars=reasoning_chars,
                        **stream_usage_metrics,
                    )
                    if (
                        not emitted_text
                        and _is_retryable_provider_error(exc)
                        and attempt < PROVIDER_TRANSIENT_RETRIES
                    ):
                        _wait_before_provider_retry(attempt)
                        continue
                    raise
                if emitted_text:
                    span.finish(
                        provider_setup_ms=provider_setup_ms,
                        ttft_ms=first_content_ms,
                        stream_duration_ms=round(span.elapsed_ms() - (first_content_ms or 0), 3),
                        output_chars=output_chars,
                        stream_chunks=chunk_count,
                        choice_chunks=choice_chunk_count,
                        reasoning_chars=reasoning_chars,
                        finish_reasons=sorted(finish_reasons),
                        provider_response_ids=sorted(response_ids),
                        **stream_usage_metrics,
                    )
                    _record_stage_exchange(
                        user_payload,
                        "".join(recorded_parts),
                        request_user_content=getattr(
                            self, "_last_stage_request_user_content", None
                        ),
                    )
                    return
                span.finish(
                    provider_setup_ms=provider_setup_ms,
                    ttft_ms=None,
                    output_chars=0,
                    stream_chunks=chunk_count,
                    choice_chunks=choice_chunk_count,
                    reasoning_chars=reasoning_chars,
                    finish_reasons=sorted(finish_reasons),
                    provider_response_ids=sorted(response_ids),
                    status="empty",
                    **stream_usage_metrics,
                )
                empty_diagnostics.append(
                    _stream_empty_diagnostic(
                        attempt + 1,
                        chunk_count,
                        choice_chunk_count,
                        reasoning_chars,
                        finish_reasons,
                        response_ids,
                    )
                )
                if "length" in finish_reasons and reasoning_chars > 0:
                    current_max_tokens = _escalate_reasoning_token_budget(
                        current_max_tokens
                    )
            raise LLMError(_empty_response_detail(self, empty_diagnostics))
        except LLMStreamCancelled:
            raise
        except Exception as exc:
            if isinstance(exc, LLMError):
                raise
            raise LLMError(_provider_failure_detail(self, exc)) from exc

    def generate_json(
        self,
        system_prompt: str,
        user_payload: dict[str, Any],
        *,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """生成 JSON object，并在输出不合法时通过有界修复重试校正。"""

        raise_if_cancelled(is_cancelled)
        outputs: list[str] = []
        next_payload = user_payload
        last_error: json.JSONDecodeError | None = None
        json_mode_supported = True
        configured_timeout = float(
            getattr(self, "timeout_seconds", DEFAULT_MODEL_API_TIMEOUT_SECONDS)
            or DEFAULT_MODEL_API_TIMEOUT_SECONDS
        )
        json_deadline = time.monotonic() + max(0.1, configured_timeout)
        previous_json_deadline = getattr(self, "_json_deadline", None)
        self._json_deadline = json_deadline
        try:
            for attempt in range(JSON_REPAIR_ATTEMPTS + 1):
                raise_if_cancelled(is_cancelled)
                if json_deadline - time.monotonic() <= 0:
                    raise LLMError("MODEL_CALL_DEADLINE_EXCEEDED")
                with llm_span_attributes(
                    response_mode="json",
                    json_attempt=attempt + 1,
                    json_retry_count=attempt,
                    json_max_attempts=JSON_REPAIR_ATTEMPTS + 1,
                ):
                    previous_defer = getattr(self, "_defer_stage_recording", False)
                    self._defer_stage_recording = True
                    try:
                        candidate_kwargs = (
                            {"is_cancelled": is_cancelled}
                            if is_cancelled is not None
                            else {}
                        )
                        text = self._generate_json_candidate(
                            system_prompt,
                            next_payload,
                            json_mode_supported,
                            **candidate_kwargs,
                        )
                        if json_mode_supported and _response_format_unsupported(text):
                            json_mode_supported = False
                            if json_deadline - time.monotonic() <= 0:
                                raise LLMError("MODEL_CALL_DEADLINE_EXCEEDED")
                            text = self._generate_json_text(
                                system_prompt,
                                next_payload,
                                is_cancelled=is_cancelled,
                            )
                    finally:
                        self._defer_stage_recording = previous_defer
                outputs.append(text)
                raise_if_cancelled(is_cancelled)
                try:
                    parsed = _loads_llm_json(text)
                    _record_stage_exchange(
                        next_payload,
                        text,
                        request_user_content=getattr(
                            self, "_last_stage_request_user_content", None
                        ),
                    )
                    return parsed
                except json.JSONDecodeError as exc:
                    last_error = exc
                    _record_stage_exchange(
                        next_payload,
                        text,
                        request_user_content=getattr(
                            self, "_last_stage_request_user_content", None
                        ),
                    )
                    if attempt >= JSON_REPAIR_ATTEMPTS:
                        break
                    next_payload = copy.deepcopy(user_payload)
                    if isinstance(user_payload.get(STAGE_PROTOCOL_KEY), dict):
                        next_payload["conversation_context"] = user_payload.get(
                            "conversation_context"
                        )
                    next_payload["_json_repair"] = {
                        "attempt": attempt + 1,
                        "max_attempts": JSON_REPAIR_ATTEMPTS,
                        "previous_output": _preview(text),
                        "parser_error": str(exc),
                        "instruction": _json_repair_instruction(text, exc),
                    }
        finally:
            if previous_json_deadline is None:
                self.__dict__.pop("_json_deadline", None)
            else:
                self._json_deadline = previous_json_deadline
        previews = "; ".join(
            f"attempt_{index + 1}_preview={_preview(output)!r}"
            for index, output in enumerate(outputs)
        )
        raise LLMError(
            f"Model did not return valid JSON after {JSON_REPAIR_ATTEMPTS} repair attempts; {previews}"
        ) from last_error

    def generate_json_with_metadata(
        self,
        system_prompt: str,
        user_payload: dict[str, Any],
        *,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """返回完整 JSON 及其真实 provider response 身份，供动作提案持久化防重。

        provider 偶尔会在 JSON 表面已经可解析时仍以 ``length`` 结束，通常是推理
        token 用尽而非正文契约完成。该响应不能直接进入 ActionProposal；按有界
        重试原则提高输出配额并重新请求，最终仍不是 ``stop/tool_calls`` 就 fail closed。
        """

        self._last_completed_response_metadata = None
        retry_payload = user_payload
        for retry_index in range(3):
            raise_if_cancelled(is_cancelled)
            generate_kwargs = (
                {"is_cancelled": is_cancelled} if is_cancelled is not None else {}
            )
            payload = self.generate_json(
                system_prompt,
                retry_payload,
                **generate_kwargs,
            )
            metadata = getattr(self, "_last_completed_response_metadata", None)
            if not isinstance(metadata, dict) or not metadata.get("response_id"):
                raise LLMError("Provider completed JSON response is missing a stable response id")
            finish_reason = metadata.get("finish_reason")
            if finish_reason in {"stop", "tool_calls"}:
                return payload, dict(metadata)
            if finish_reason != "length" or retry_index >= 2:
                raise LLMError("Provider completed JSON response has an unsupported finish reason")
            retry_payload = _length_retry_payload(user_payload, attempt=retry_index + 1)
        raise LLMError("Provider completed JSON response did not settle")

    def preflight_dynamic_capabilities(self) -> dict[str, bool | str]:
        """使用原生 JSON mode 与真实 tool call 探针验证动态计划必需协议。"""

        structured_request = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "Return one JSON object only."},
                {"role": "user", "content": 'Return {"probe":"ready"}.'},
            ],
            "temperature": 0,
            # 推理模型会先消耗 reasoning tokens；过小会在 JSON 字符串中途被截断。
            "max_tokens": 512,
            "response_format": {"type": "json_object"},
        }
        tool_schema = {
            "type": "function",
            "function": {
                "name": "dynamic_capability_probe",
                "description": "Return the supplied probe value.",
                "parameters": {
                    "type": "object",
                    "properties": {"probe": {"type": "string"}},
                    "required": ["probe"],
                    "additionalProperties": False,
                },
            },
        }
        tool_request = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "Call the required probe tool."},
                {"role": "user", "content": "Probe dynamic tool calling."},
            ],
            "temperature": 0,
            "max_tokens": 512,
            "tools": [tool_schema],
            "tool_choice": {
                "type": "function",
                "function": {"name": "dynamic_capability_probe"},
            },
        }
        thinking_kwargs = _thinking_request_kwargs(
            getattr(self, "thinking_mode", ""),
            getattr(self, "extra_body", {}),
        )
        structured_request.update(thinking_kwargs)
        tool_request.update(thinking_kwargs)
        try:
            structured = self.client.chat.completions.create(**structured_request)
            content = _completion_message_content(structured)
            parsed = json.loads(content)
            if parsed != {"probe": "ready"}:
                raise LLMError("Structured-output capability probe returned an invalid payload")
            try:
                tool_probe_request = tool_request
                used_compatible_tool_request = False
                tool_completion = self.client.chat.completions.create(**tool_probe_request)
            except Exception as exc:
                if not _tool_choice_unsupported(exc):
                    raise
                tool_probe_request = dict(tool_request)
                tool_probe_request.pop("tool_choice", None)
                used_compatible_tool_request = True
                tool_completion = self.client.chat.completions.create(**tool_probe_request)
            tool_calls = _completion_tool_calls(tool_completion)
            if used_compatible_tool_request and not any(
                call["name"] == "dynamic_capability_probe" for call in tool_calls
            ):
                # 兼容 provider 在温度为 0 时偶发只返回普通文本的情况。重试仍使用
                # 同一真实工具契约，次数有界；不把文本回复推断成 tool calling 能力。
                retry_request = dict(tool_probe_request)
                retry_request["messages"] = [
                    {
                        "role": "system",
                        "content": (
                            "Emit exactly one dynamic_capability_probe tool call. "
                            "Do not answer with text."
                        ),
                    },
                    {"role": "user", "content": "Probe dynamic tool calling now."},
                ]
                for _ in range(2):
                    tool_completion = self.client.chat.completions.create(**retry_request)
                    tool_calls = _completion_tool_calls(tool_completion)
                    if any(call["name"] == "dynamic_capability_probe" for call in tool_calls):
                        break
            if not any(call["name"] == "dynamic_capability_probe" for call in tool_calls):
                raise LLMError("Tool-calling capability probe returned no required tool call")
        except Exception as exc:
            if isinstance(exc, LLMError):
                raise
            raise LLMError(_provider_failure_detail(self, exc)) from exc
        vision, pdf_input = self._probe_optional_dynamic_inputs()
        return {
            "protocol_version": "dynamic-v1",
            "sdk_available": True,
            "credentials_verified": True,
            "structured_output": True,
            "tool_calling": True,
            "vision": vision,
            "pdf_input": pdf_input,
        }

    def _probe_optional_dynamic_inputs(self) -> tuple[bool, bool]:
        """分别实测原生图片与 PDF 输入；可选能力失败只冻结为 false，不影响文本动态协议。"""

        vision = False
        pdf_input = False
        vision_request = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Identify the two colored halves and their positions. "
                                "Reply exactly: red left, blue right"
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": (
                                    "data:image/png;base64,"
                                    "iVBORw0KGgoAAAANSUhEUgAAAEAAAAAgCAIAAAAt/+nTAAAAPElE"
                                    "QVR42u3PAQkAMAwEse+0zL+iiZmKFgo5A0fqpbeb3sPJ8gAAAAAA"
                                    "AAAAAAAAAAAAAAAAAAAAAADm+5bNAhx3gTUhAAAAAElFTkSuQmCC"
                                )
                            },
                        },
                    ],
                }
            ],
            "temperature": 0,
            # 思考模型会先消耗 reasoning tokens；32 token 会在正文前结束，
            # 将真实支持视觉的模型误判为 vision=false。
            "max_tokens": OPTIONAL_INPUT_PROBE_MAX_TOKENS,
        }
        pdf_request = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Return only the unique token written in this PDF.",
                        },
                        {
                            "type": "file",
                            "file": {
                                "filename": "capability-probe.pdf",
                                "file_data": _dynamic_pdf_probe_data_url(),
                            },
                        },
                    ],
                }
            ],
            "temperature": 0,
            # PDF 探针与图片探针保持相同预算，避免 provider 仅因推理配额不足
            # 进入可选能力的 fail-closed 分支。
            "max_tokens": OPTIONAL_INPUT_PROBE_MAX_TOKENS,
        }
        thinking_kwargs = _thinking_request_kwargs(
            getattr(self, "thinking_mode", ""),
            getattr(self, "extra_body", {}),
        )
        vision_request.update(thinking_kwargs)
        pdf_request.update(thinking_kwargs)
        try:
            completion = self.client.chat.completions.create(**vision_request)
            vision_content = _completion_message_content(completion).lower()
            vision = all(
                token in vision_content for token in ("red", "left", "blue", "right")
            )
        except Exception:  # noqa: BLE001 - optional capability failure is a frozen false fact.
            vision = False
        try:
            completion = self.client.chat.completions.create(**pdf_request)
            pdf_input = "PDFCAP7" in _completion_message_content(completion).upper()
        except Exception:  # noqa: BLE001 - optional capability failure is a frozen false fact.
            pdf_input = False
        return vision, pdf_input

    def _generate_json_candidate(
        self,
        system_prompt: str,
        user_payload: dict[str, Any],
        json_mode_supported: bool,
        *,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> str:
        if not json_mode_supported:
            return self._generate_json_text(
                system_prompt,
                user_payload,
                is_cancelled=is_cancelled,
            )
        try:
            return self._generate_json_text(
                system_prompt,
                user_payload,
                response_format={"type": "json_object"},
                is_cancelled=is_cancelled,
            )
        except TypeError:
            return self._generate_json_text(
                system_prompt,
                user_payload,
                is_cancelled=is_cancelled,
            )
        except LLMError as exc:
            message = str(exc)
            if _response_format_unsupported(message):
                return message
            if _empty_response(message):
                return self._generate_json_text(
                    system_prompt,
                    user_payload,
                    is_cancelled=is_cancelled,
                )
            raise

    def _generate_json_text(
        self,
        system_prompt: str,
        user_payload: dict[str, Any],
        *,
        response_format: dict[str, str] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> str:
        """执行 JSON 阶段文本请求，并在取消时关闭 provider 客户端连接。"""

        def operation() -> str:
            """调用现有非流式文本接口，兼容旧的测试替身签名。"""

            if response_format is not None:
                return self.generate_text(system_prompt, user_payload, response_format)
            return self.generate_text(system_prompt, user_payload)

        return run_cancellable(
            operation,
            is_cancelled,
            on_cancel=lambda: _best_effort_close_stream(self.client),
        )


def _completion_message_content(completion: Any) -> str:
    try:
        choice = completion.choices[0]
        message = getattr(choice, "message", None)
        content = getattr(message, "content", None)
    except (IndexError, TypeError, AttributeError):
        return ""
    return _content_text(content)


def _dynamic_pdf_probe_data_url() -> str:
    """构造只含随机固定 token 的最小 PDF data URL，供真实 PDF 输入能力探针读取。"""

    stream = b"BT /F1 12 Tf 72 720 Td (PDFCAP7) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length "
        + str(len(stream)).encode("ascii")
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for object_number, body in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{object_number} 0 obj\n".encode("ascii"))
        pdf.extend(body)
        pdf.extend(b"\nendobj\n")
    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    pdf.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    return "data:application/pdf;base64," + base64.b64encode(pdf).decode("ascii")


def _request_shape_metrics(
    system_prompt: str,
    context_messages: list[dict[str, Any]],
    serialized_payload: str | list[dict[str, Any]],
    request_messages: list[dict[str, Any]],
) -> dict[str, Any]:
    context_chars = sum(
        len(_content_text(message.get("content"))) for message in context_messages
    )
    request_chars = sum(
        len(_content_text(message.get("content"))) for message in request_messages
    )
    return {
        "system_prompt_chars": len(system_prompt),
        "context_message_count": len(context_messages),
        "context_text_chars": context_chars,
        "payload_chars": len(_content_text(serialized_payload)),
        "request_text_chars": request_chars,
        "request_message_count": len(request_messages),
        "request_message_roles": [str(message.get("role") or "") for message in request_messages],
        "request_message_chars": [
            len(_content_text(message.get("content"))) for message in request_messages
        ],
        "request_prefix_fingerprints": _request_prefix_fingerprints(request_messages),
    }


def _request_prefix_fingerprints(messages: list[dict[str, Any]]) -> list[str]:
    digest = hashlib.sha256()
    fingerprints: list[str] = []
    for message in messages:
        serialized = json.dumps(
            {
                "role": str(message.get("role") or ""),
                "content": message.get("content"),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        digest.update(serialized.encode("utf-8"))
        digest.update(b"\n")
        fingerprints.append(digest.hexdigest()[:16])
    return fingerprints


def _normalize_thinking_mode(value: Any) -> str:
    mode = str(value or "").strip().lower()
    return mode if mode in {"enabled", "disabled"} else ""


def _thinking_mode_for_model(mode: Any, configured_models: Any, model: Any) -> str:
    normalized_mode = _normalize_thinking_mode(mode)
    if not normalized_mode:
        return ""
    allowed_models = {
        item.strip().lower()
        for item in str(configured_models or "").split(",")
        if item.strip()
    }
    if allowed_models and str(model or "").strip().lower() not in allowed_models:
        return ""
    return normalized_mode


def _normalize_extra_body(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return copy.deepcopy(value)


def _thinking_mode_from_extra_body(extra_body: Any) -> str:
    normalized = _normalize_extra_body(extra_body)
    thinking = normalized.get("thinking")
    if not isinstance(thinking, dict):
        return ""
    return _normalize_thinking_mode(thinking.get("type"))


def _thinking_request_kwargs(mode: Any, extra_body: Any = None) -> dict[str, Any]:
    body = _normalize_extra_body(extra_body)
    normalized = _normalize_thinking_mode(mode)
    if normalized:
        thinking = body.get("thinking")
        body["thinking"] = {
            **(thinking if isinstance(thinking, dict) else {}),
            "type": normalized,
        }
    return {"extra_body": body} if body else {}


def _request_messages(
    system_prompt: str,
    context_messages: list[dict[str, Any]],
    serialized_payload: str | list[dict[str, Any]],
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt.rstrip()}
    ]
    messages.extend(context_messages)
    if serialized_payload != "{}":
        current_input = serialized_payload
        if (
            context_messages
            and isinstance(serialized_payload, str)
            and not isinstance(serialized_payload, _CurrentStageText)
        ):
            current_input = (
                "本轮输入（仅用于当前调用，不写入对话历史）：\n"
                f"{serialized_payload}"
            )
        messages.append(
            {
                "role": "user",
                "content": current_input,
            }
        )
    elif not context_messages:
        messages.append({"role": "user", "content": "{}"})
    return messages


def _fit_request_messages(
    messages: list[dict[str, Any]], token_budget: int = DEFAULT_INPUT_TOKEN_BUDGET
) -> list[dict[str, Any]]:
    projected = copy.deepcopy(messages)
    while len(projected) > 2 and _request_tokens(projected) > token_budget:
        removable_index = next(
            (
                index
                for index in range(1, len(projected) - 1)
                if not _is_history_summary_message(projected[index])
                and not _is_turn_stage_message(projected[index])
            ),
            None,
        )
        if removable_index is None:
            break
        projected.pop(removable_index)

    while len(projected) > 2 and _request_tokens(projected) > token_budget:
        removable_index = next(
            (
                index
                for index in range(1, len(projected) - 1)
                if not _is_turn_stage_message(projected[index])
            ),
            None,
        )
        if removable_index is None:
            break
        projected.pop(removable_index)

    _trim_turn_stage_messages(projected, token_budget)
    _drop_oldest_turn_stage_exchanges(projected, token_budget)

    if projected and _request_tokens(projected) > token_budget:
        fixed_tokens = _request_tokens(projected[:-1])
        projected[-1] = _trim_request_message(
            projected[-1], max(1, token_budget - fixed_tokens)
        )
    return [
        {key: value for key, value in message.items() if key != TURN_STAGE_MESSAGE_MARKER}
        for message in projected
    ]


def _request_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(
        max(1, math.ceil(len(_content_text(message.get("content")).encode("utf-8")) / 4))
        + 6
        for message in messages
    )


def _is_history_summary_message(message: dict[str, Any]) -> bool:
    content = _content_text(message.get("content")).lstrip()
    return content.startswith(
        ("历史的信息可以被总结为：", "近期的历史信息总结为：")
    )


def _is_turn_stage_message(message: dict[str, Any]) -> bool:
    return message.get(TURN_STAGE_MESSAGE_MARKER) is True


def _trim_turn_stage_messages(
    messages: list[dict[str, Any]], token_budget: int
) -> None:
    while _request_tokens(messages) > token_budget:
        candidates = [
            (len(_content_text(message.get("content"))), index)
            for index, message in enumerate(messages[1:-1], start=1)
            if _is_turn_stage_message(message)
            and len(_content_text(message.get("content"))) > 512
        ]
        if not candidates:
            break
        current_length, index = max(candidates)
        excess_tokens = _request_tokens(messages) - token_budget
        target_tokens = max(128, math.ceil(current_length / 4) - excess_tokens)
        trimmed = _trim_request_message(messages[index], target_tokens)
        trimmed[TURN_STAGE_MESSAGE_MARKER] = True
        if len(_content_text(trimmed.get("content"))) >= current_length:
            break
        messages[index] = trimmed


def _drop_oldest_turn_stage_exchanges(
    messages: list[dict[str, Any]], token_budget: int
) -> None:
    while _request_tokens(messages) > token_budget:
        stage_indices = [
            index
            for index, message in enumerate(messages[1:-1], start=1)
            if _is_turn_stage_message(message)
        ]
        if len(stage_indices) <= 2:
            break
        first_index = stage_indices[0]
        remove_count = 1
        if (
            len(stage_indices) > 1
            and stage_indices[1] == first_index + 1
            and messages[first_index].get("role") == "user"
            and messages[first_index + 1].get("role") == "assistant"
        ):
            remove_count = 2
        del messages[first_index : first_index + remove_count]


def _trim_request_message(
    message: dict[str, Any], token_budget: int
) -> dict[str, Any]:
    content = message.get("content")
    byte_budget = max(4, token_budget * 4)
    if isinstance(content, list):
        parts = copy.deepcopy(content)
        text_part = next(
            (
                part
                for part in parts
                if isinstance(part, dict)
                and part.get("type") == "text"
                and isinstance(part.get("text"), str)
            ),
            None,
        )
        if text_part is not None:
            text_part["text"] = _trim_request_text(text_part["text"], byte_budget)
        return {**message, "content": parts}
    return {**message, "content": _trim_request_text(str(content or ""), byte_budget)}


def _trim_request_text(text: str, byte_budget: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= byte_budget:
        return text
    marker = "\n...<输入超过 32k，已省略中间部分>...\n"
    marker_bytes = len(marker.encode("utf-8"))
    available = max(8, byte_budget - marker_bytes)
    head_size = int(available * 0.7)
    tail_size = available - head_size
    head = encoded[:head_size].decode("utf-8", errors="ignore")
    tail = encoded[-tail_size:].decode("utf-8", errors="ignore")
    return f"{head}{marker}{tail}"


def _completion_span_metrics(completion: Any) -> dict[str, Any]:
    if completion is None:
        return {}
    choices = getattr(completion, "choices", None) or []
    finish_reason = None
    message = None
    if choices:
        finish_reason = _safe_fragment(getattr(choices[0], "finish_reason", None), 32) or None
        message = getattr(choices[0], "message", None)
    usage = getattr(completion, "usage", None)
    return {
        "provider_response_id": _safe_fragment(getattr(completion, "id", None), 48) or None,
        "finish_reason": finish_reason,
        "reasoning_chars": len(_reasoning_text(message)),
        **_usage_span_metrics(usage),
    }


def _completion_identity_metadata(completion: Any) -> dict[str, Any]:
    """提取持久提案需要的未截断响应身份、结束原因和 token 用量。"""

    choices = getattr(completion, "choices", None) or []
    finish_reason = None
    if choices:
        finish_reason = _safe_fragment(getattr(choices[0], "finish_reason", None), 32) or None
    response_id = _safe_fragment(getattr(completion, "id", None), 512) or None
    return {
        "response_id": response_id,
        "finish_reason": finish_reason,
        "usage": _usage_span_metrics(getattr(completion, "usage", None)),
    }


def _usage_span_metrics(usage: Any) -> dict[str, Any]:
    if usage is None:
        return {}
    input_tokens = _usage_value(usage, "prompt_tokens", "input_tokens")
    output_tokens = _usage_value(usage, "completion_tokens", "output_tokens")
    total_tokens = _usage_value(usage, "total_tokens")
    prompt_details = _usage_object(usage, "prompt_tokens_details", "input_tokens_details")
    cached_input_tokens = _usage_value(
        prompt_details,
        "cached_tokens",
        "cache_read_tokens",
        "cache_read_input_tokens",
    )
    if cached_input_tokens is None:
        cached_input_tokens = _usage_value(
            usage,
            "cached_tokens",
            "prompt_cache_hit_tokens",
            "cache_read_input_tokens",
        )
    metrics: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cached_input_tokens": cached_input_tokens,
    }
    if input_tokens is not None and cached_input_tokens is not None:
        metrics["uncached_input_tokens"] = max(0, input_tokens - cached_input_tokens)
    return {key: value for key, value in metrics.items() if value is not None}


def _usage_object(source: Any, *names: str) -> Any:
    for name in names:
        value = source.get(name) if isinstance(source, dict) else getattr(source, name, None)
        if value is not None:
            return value
    return None


def _usage_value(source: Any, *names: str) -> int | None:
    value = _usage_object(source, *names)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(_content_part_text(item) for item in content)
    return _content_part_text(content)


def _content_part_text(item: Any) -> str:
    if isinstance(item, str):
        return item
    text: Any = item.get("text") if isinstance(item, dict) else getattr(item, "text", None)
    if isinstance(text, str):
        return text
    if isinstance(text, dict) and isinstance(text.get("value"), str):
        return text["value"]
    value = getattr(text, "value", None)
    return value if isinstance(value, str) else ""


def _completion_empty_diagnostic(completion: Any, attempt: int) -> str:
    choices = getattr(completion, "choices", None) or []
    response_id = _safe_fragment(getattr(completion, "id", None), 48) or "missing"
    if not choices:
        return f"attempt_{attempt}: response_id={response_id}, choices=0"
    choice = choices[0]
    message = getattr(choice, "message", None)
    finish_reason = _safe_fragment(getattr(choice, "finish_reason", None), 32) or "missing"
    refusal = _safe_fragment(getattr(message, "refusal", None), 80)
    reasoning_chars = len(_reasoning_text(message))
    tool_calls = getattr(message, "tool_calls", None) or []
    content = getattr(message, "content", None)
    content_shape = _content_shape(content)
    usage = getattr(completion, "usage", None)
    completion_tokens = getattr(usage, "completion_tokens", None)
    parts = [
        f"attempt_{attempt}: response_id={response_id}",
        f"choices={len(choices)}",
        f"finish_reason={finish_reason}",
        f"content={content_shape}",
        f"reasoning_chars={reasoning_chars}",
        f"tool_calls={len(tool_calls)}",
    ]
    if refusal:
        parts.append(f"refusal={refusal}")
    if completion_tokens is not None:
        parts.append(f"completion_tokens={completion_tokens}")
    return ", ".join(parts)


def _stream_empty_diagnostic(
    attempt: int,
    chunk_count: int,
    choice_chunk_count: int,
    reasoning_chars: int,
    finish_reasons: set[str],
    response_ids: set[str],
) -> str:
    return (
        f"attempt_{attempt}: stream_chunks={chunk_count}, choice_chunks={choice_chunk_count}, "
        f"finish_reason={','.join(sorted(finish_reasons)) or 'missing'}, text_chars=0, "
        f"reasoning_chars={reasoning_chars}, response_id={','.join(sorted(response_ids)) or 'missing'}"
    )


def _empty_response_detail(client: Any, diagnostics: list[str]) -> str:
    attempts = EMPTY_RESPONSE_RETRIES + 1
    model = _safe_fragment(getattr(client, "model", None), 80) or "unknown"
    endpoint = _endpoint_label(getattr(client, "base_url", None))
    response_details = " | ".join(diagnostics)
    return (
        f"{EMPTY_RESPONSE_MESSAGE} after {attempts} attempts; provider returned no usable message.content; "
        f"model={model}; endpoint={endpoint}; {response_details}"
    )


def _is_retryable_provider_error(exc: BaseException) -> bool:
    """识别可短暂恢复的 provider 拒绝，避免把过载误报成永久模型故障。"""

    status_code = getattr(exc, "status_code", None)
    if status_code in {408, 409, 425, 429} or (
        isinstance(status_code, int) and 500 <= status_code <= 599
    ):
        return True
    body = getattr(exc, "body", None)
    provider_code = getattr(exc, "code", None)
    if isinstance(body, dict):
        error_body = body.get("error")
        if isinstance(error_body, dict):
            provider_code = error_body.get("code") or error_body.get("type") or provider_code
    normalized = str(provider_code or "").replace("_", "").replace("-", "").lower()
    return normalized in {
        "serveroverloaded",
        "temporarilyunavailable",
        "serviceunavailable",
        "ratelimitexceeded",
    }


def _wait_before_provider_retry(attempt: int) -> None:
    """以有界指数退避等待下一次 provider 请求，避免并发过载时立即风暴重试。"""

    delay = min(PROVIDER_RETRY_BASE_SECONDS * (2**max(0, attempt)), 4.0)
    time.sleep(delay)


def _provider_failure_detail(client: Any, exc: Exception) -> str:
    model = _safe_fragment(getattr(client, "model", None), 80) or "unknown"
    endpoint = _endpoint_label(getattr(client, "base_url", None))
    timeout = getattr(client, "timeout_seconds", None)
    status_code = getattr(exc, "status_code", None)
    request_id = _safe_fragment(getattr(exc, "request_id", None), 64)
    error_type = type(exc).__name__
    message = _safe_fragment(exc, 240) or "no provider error message"
    provider_code = ""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error_body = body.get("error") if isinstance(body.get("error"), dict) else body
        provider_code = _safe_fragment(error_body.get("code") or error_body.get("type"), 64)
        provider_message = _safe_fragment(error_body.get("message"), 160)
        if provider_message and provider_message not in message:
            message = f"{message}; provider_message={provider_message}"
    details = [
        f"LLM provider request failed ({error_type})",
        f"message={message}",
        f"model={model}",
        f"endpoint={endpoint}",
    ]
    if status_code is not None:
        details.append(f"status_code={status_code}")
    if provider_code:
        details.append(f"provider_code={provider_code}")
    if request_id:
        details.append(f"request_id={request_id}")
    if timeout is not None:
        details.append(f"timeout_seconds={timeout}")
    return "; ".join(details)


def _content_shape(content: Any) -> str:
    if content is None:
        return "null"
    text = _content_text(content)
    if isinstance(content, str):
        return f"string({len(content)} chars{' whitespace' if content and not content.strip() else ''})"
    if isinstance(content, list):
        return f"list({len(content)} parts, {len(text)} text_chars)"
    return f"{type(content).__name__}({len(text)} text_chars)"


def _reasoning_text(value: Any) -> str:
    if value is None:
        return ""
    for key in ("reasoning_content", "reasoning", "thinking"):
        content = value.get(key) if isinstance(value, dict) else getattr(value, key, None)
        text = _content_text(content)
        if text:
            return text
    return ""


def _completion_tool_calls(completion: Any) -> list[dict[str, str]]:
    """从 OpenAI 兼容回复中提取工具名与原始参数，缺失字段时安全返回空列表。"""

    try:
        calls = completion.choices[0].message.tool_calls or []
    except (IndexError, TypeError, AttributeError):
        return []
    result: list[dict[str, str]] = []
    for call in calls:
        function = getattr(call, "function", None)
        name = str(getattr(function, "name", "") or "")
        if name:
            result.append(
                {
                    "name": name,
                    "arguments": str(getattr(function, "arguments", "") or ""),
                }
            )
    return result


def _safe_fragment(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "sk-***", text)
    text = re.sub(r"\bpt-[A-Za-z0-9_-]{8,}\b", "pt-***", text)
    text = re.sub(
        r"(?i)(api[_-]?key|authorization|access[_-]?token|token)=([^&\s;]+)",
        r"\1=***",
        text,
    )
    return text[:limit]


def _endpoint_label(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "unknown"
    parsed = urlsplit(raw)
    if not parsed.hostname:
        return "configured-endpoint"
    host = parsed.hostname
    try:
        port = parsed.port
    except ValueError:
        return "configured-endpoint"
    if port:
        host = f"{host}:{port}"
    path = parsed.path.rstrip("/")
    return _safe_fragment(f"{parsed.scheme or 'http'}://{host}{path}", 160)


def _extract_json(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`").strip()
        if stripped.startswith("json"):
            stripped = stripped[4:].strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end >= start:
        return stripped[start : end + 1]
    return stripped


def _loads_llm_json(text: str) -> Any:
    candidate = _extract_json(text)
    last_error: json.JSONDecodeError | None = None
    seen: set[str] = set()
    for variant in _json_candidate_variants(candidate):
        if variant in seen:
            continue
        seen.add(variant)
        try:
            return json.loads(variant)
        except json.JSONDecodeError as exc:
            last_error = exc
    try:
        literal = ast.literal_eval(candidate)
    except (SyntaxError, ValueError):
        literal = None
    if isinstance(literal, (dict, list)):
        return literal
    if last_error is not None:
        raise last_error
    raise json.JSONDecodeError("Could not decode JSON", candidate, 0)


def _json_candidate_variants(text: str) -> tuple[str, ...]:
    stripped = text.strip()
    no_trailing_commas = _remove_trailing_commas(stripped)
    repaired_strings = _repair_json_string_content(stripped)
    repaired_strings_no_trailing = _remove_trailing_commas(repaired_strings)
    return (
        stripped,
        no_trailing_commas,
        repaired_strings,
        repaired_strings_no_trailing,
    )


def _remove_trailing_commas(text: str) -> str:
    return re.sub(r",\s*([}\]])", r"\1", text)


def _repair_json_string_content(text: str) -> str:
    output: list[str] = []
    in_string = False
    index = 0
    while index < len(text):
        char = text[index]
        if not in_string:
            output.append(char)
            if char == '"':
                in_string = True
            index += 1
            continue
        if char == "\\":
            output.append(char)
            index += 1
            if index < len(text):
                output.append(text[index])
                index += 1
            continue
        if char == '"':
            if _quote_likely_closes_string(text, index):
                output.append(char)
                in_string = False
            else:
                output.append('\\"')
            index += 1
            continue
        if char == "\n":
            output.append("\\n")
        elif char == "\r":
            output.append("\\r")
        elif char == "\t":
            output.append("\\t")
        else:
            output.append(char)
        index += 1
    return "".join(output)


def _quote_likely_closes_string(text: str, quote_index: int) -> bool:
    index = quote_index + 1
    while index < len(text) and text[index].isspace():
        index += 1
    return index >= len(text) or text[index] in {":", ",", "}", "]"}


def _json_repair_instruction(text: str, error: json.JSONDecodeError) -> str:
    """区分普通JSON语法错误与长响应截断，避免修复轮重复生成同一份超长半包。"""

    likely_truncated = (
        len(text) >= 4_000
        or error.pos >= max(0, len(text) - 256)
        or "Unterminated" in error.msg
    )
    if likely_truncated:
        return (
            "上一轮 JSON 疑似因输出过长而被截断。请基于原始任务重新输出完整 JSON object，"
            "保留所有必需字段与关键事实，但将长字符串显著压缩、删除重复解释；这是控制面草案，"
            "默认 constraints、assumptions、expected_artifacts 为空，不要复制附件或最终文档正文，"
            "无能力/无 Skill 的任务只保留一个 answer 步骤，并只保留完成当前契约所必需的证据项；"
            "优先保证引号、数组和大括号完整闭合。"
            "字符串内部双引号必须转义；不要输出 JSON 之外的 Markdown、解释或代码块。"
        )
    return (
        "上一轮输出不是合法 JSON。请基于原始任务上下文重新输出完整、可解析的 JSON object。"
        "字符串内部的双引号必须转义；不要输出 Markdown、解释、代码块或额外文本。"
    )


def _preview(text: str, limit: int = 1200) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...<truncated>"


def _response_format_unsupported(message: str) -> bool:
    lowered = message.lower()
    return "response_format" in lowered and any(
        phrase in lowered
        for phrase in (
            "unsupported",
            "not support",
            "not_supported",
            "unknown parameter",
            "unrecognized",
            "extra inputs are not permitted",
            "invalid parameter",
        )
    )


def _tool_choice_unsupported(exc: Exception) -> bool:
    """兼容明确拒绝强制选择的 provider，并保持工具调用结果为最终能力证据。"""

    status_code = getattr(exc, "status_code", None)
    if status_code not in {400, 422}:
        return False
    fragments = [str(exc)]
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        fragments.append(json.dumps(body, ensure_ascii=False, default=str))
    lowered = " ".join(fragments).lower()
    message_indicates_unsupported = "tool_choice" in lowered and any(
        phrase in lowered
        for phrase in (
            "does not support",
            "not support",
            "unsupported",
            "unknown parameter",
            "unrecognized",
            "invalid parameter",
        )
    )
    if message_indicates_unsupported:
        return True
    # 部分 OpenAI-compatible provider（例如 Ark kimi-k3）只返回
    # error.code=InvalidParameter，不回显被拒绝的字段名。这里仅针对
    # 当前 tool_choice 请求的 400/422 异常降级；后续仍必须拿到真实
    # dynamic_capability_probe tool call，否则预检继续 fail closed。
    provider_code = getattr(exc, "code", None)
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error_body = body.get("error")
        if isinstance(error_body, dict):
            provider_code = error_body.get("code") or provider_code
    return str(provider_code or "").replace("_", "").lower() == "invalidparameter"


def _empty_response(message: str) -> bool:
    return EMPTY_RESPONSE_MESSAGE.lower() in message.lower()


def _project_context_messages(
    user_payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    payload = copy.deepcopy(user_payload)
    context = payload.pop("conversation_context", None)
    if not isinstance(context, dict):
        return [], _drop_empty_values(payload)
    messages = context.get("messages", [])
    if not isinstance(messages, list):
        return [], _drop_empty_values(payload)
    projected: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").strip()
        content = str(message.get("content") or "").strip()
        images = _normalize_image_parts(message.get("images"))
        if role not in {"user", "assistant"} or (not content and not images):
            continue
        if images and role == "user":
            projected.append(
                {
                    "role": role,
                    "content": [
                        {"type": "text", "text": content or "（用户上传了图片附件）"},
                        *images,
                    ],
                }
            )
        else:
            projected.append({"role": role, "content": content})
    current_user_message = str(payload.get("user_message") or "").strip()
    latest_user_message = next(
        (
            _content_text(message.get("content")).strip()
            for message in reversed(projected)
            if message.get("role") == "user"
        ),
        "",
    )
    if current_user_message and current_user_message == latest_user_message:
        payload.pop("user_message", None)
    return projected, _drop_empty_values(payload)


def _prepare_user_input(
    user_payload: dict[str, Any] | str,
) -> tuple[list[dict[str, Any]], str | list[dict[str, Any]]]:
    if isinstance(user_payload, str):
        return [], user_payload.strip()
    if isinstance(user_payload.get(STAGE_PROTOCOL_KEY), dict):
        return _prepare_stage_user_input(user_payload)
    native_parts = user_payload.get(PROVIDER_CONTENT_PARTS_KEY)
    if native_parts is not None:
        if not isinstance(native_parts, list) or any(
            not _valid_provider_native_part(item)
            for item in native_parts
        ):
            raise LLMError("Provider native input parts are invalid")
        projected_payload = {
            key: value
            for key, value in user_payload.items()
            if key != PROVIDER_CONTENT_PARTS_KEY
        }
        serialized = json.dumps(projected_payload, ensure_ascii=False)
        if not native_parts:
            return [], serialized
        return [], [{"type": "text", "text": serialized}, *copy.deepcopy(native_parts)]
    context_messages, projected_payload = _project_context_messages(user_payload)
    return context_messages, json.dumps(projected_payload, ensure_ascii=False)


def _valid_provider_native_part(value: object) -> bool:
    """只允许有界内联数据或受控 provider file-id 进入原生 part，拒绝任意远程 URL。"""

    if not isinstance(value, dict):
        return False
    if value.get("type") == "image_url" and set(value) == {"type", "image_url"}:
        image = value.get("image_url")
        if not isinstance(image, dict):
            return False
        if set(image) == {"file_id"}:
            return _valid_provider_file_id(image.get("file_id"), prefixes=("file-",))
        if set(image) != {"url"}:
            return False
        url = image.get("url")
        prefixes = (
            "data:image/png;base64,",
            "data:image/jpeg;base64,",
            "data:image/gif;base64,",
            "data:image/webp;base64,",
            "data:image/bmp;base64,",
        )
        return isinstance(url, str) and url.startswith(prefixes) and len(url) <= 12_000_000
    if value.get("type") == "video_url" and set(value) == {"type", "video_url"}:
        video = value.get("video_url")
        return (
            isinstance(video, dict)
            and set(video) == {"file_id"}
            and _valid_provider_file_id(video.get("file_id"), prefixes=("file-",))
        )
    if value.get("type") == "file" and set(value) == {"type", "file_id"}:
        return _valid_provider_file_id(value.get("file_id"), prefixes=("file-api-", "file-"))
    if value.get("type") == "file" and set(value) == {"type", "file"}:
        file_part = value.get("file")
        if isinstance(file_part, dict) and set(file_part) == {"file_id"}:
            return _valid_provider_file_id(file_part.get("file_id"), prefixes=("file-",))
    if value.get("type") == "file" and set(value) == {"type", "file"}:
        file_part = value.get("file")
        if not isinstance(file_part, dict) or set(file_part) != {"filename", "file_data"}:
            return False
        filename = file_part.get("filename")
        data = file_part.get("file_data")
        return (
            isinstance(filename, str)
            and 0 < len(filename) <= 255
            and "/" not in filename
            and "\\" not in filename
            and isinstance(data, str)
            and data.startswith("data:application/pdf;base64,")
            and len(data) <= 15_000_000
        )
    return False


def _valid_provider_file_id(value: object, *, prefixes: tuple[str, ...]) -> bool:
    """校验由已配置 provider 返回的 file-id，阻断路径、查询串和过长身份。"""

    if not isinstance(value, str) or not value.startswith(prefixes) or len(value) > 256:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", value))


def _prepare_stage_user_input(
    user_payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], str | list[dict[str, Any]]]:
    context = user_payload.get("conversation_context")
    payload = copy.deepcopy(
        {
            key: value
            for key, value in user_payload.items()
            if key != "conversation_context"
        }
    )
    payload.pop(STAGE_PROTOCOL_KEY, None)
    context_messages = _project_messages_from_context(context)
    user_message = str(payload.pop("user_message", "") or "").strip()
    current_images: list[dict[str, Any]] = []
    for index in range(len(context_messages) - 1, -1, -1):
        message = context_messages[index]
        if message.get("role") != "user":
            continue
        if _content_text(message.get("content")).strip() != user_message:
            break
        content = message.get("content")
        if isinstance(content, list):
            current_images = [
                item
                for item in content
                if isinstance(item, dict) and item.get("type") == "image_url"
            ]
        context_messages.pop(index)
        break

    turn_stage_messages = _project_turn_stage_messages(context)
    context_messages.extend(turn_stage_messages)
    serialized = render_stage_user_message(
        user_payload, include_turn_header=not turn_stage_messages
    )
    if not current_images:
        return context_messages, _CurrentStageText(serialized)
    return context_messages, [
        {"type": "text", "text": serialized},
        *current_images,
    ]


def _project_messages_from_context(context: Any) -> list[dict[str, Any]]:
    if not isinstance(context, dict) or not isinstance(context.get("messages"), list):
        return []
    projected: list[dict[str, Any]] = []
    for message in context["messages"]:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").strip()
        content = str(message.get("content") or "").strip()
        images = _normalize_image_parts(message.get("images"))
        if role not in {"user", "assistant"} or (not content and not images):
            continue
        if images and role == "user":
            projected.append(
                {
                    "role": role,
                    "content": [
                        {"type": "text", "text": content or "（用户上传了图片附件）"},
                        *images,
                    ],
                }
            )
        else:
            projected.append({"role": role, "content": content})
    return projected


def _project_turn_stage_messages(context: Any) -> list[dict[str, Any]]:
    if not isinstance(context, dict):
        return []
    messages = context.get(TURN_STAGE_MESSAGES_KEY)
    if not isinstance(messages, list):
        return []
    projected: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").strip()
        content = message.get("content")
        if role not in {"user", "assistant"} or not _content_text(content).strip():
            continue
        projected.append(
            {
                "role": role,
                "content": content,
                TURN_STAGE_MESSAGE_MARKER: True,
            }
        )
    return projected


def _record_stage_exchange(
    context_payload: dict[str, Any] | str,
    assistant_content: str,
    *,
    request_user_content: Any = None,
) -> None:
    if not isinstance(context_payload, dict):
        return
    if not isinstance(context_payload.get(STAGE_PROTOCOL_KEY), dict):
        return
    context = context_payload.get("conversation_context")
    if not isinstance(context, dict):
        return
    turn_messages = context.setdefault(TURN_STAGE_MESSAGES_KEY, [])
    if not isinstance(turn_messages, list):
        return
    content = str(assistant_content or "").strip()
    if not content:
        return
    user_content = request_user_content
    if not _content_text(user_content).strip():
        user_content = render_stage_user_message(
            context_payload,
            include_turn_header=not _project_turn_stage_messages(context),
        )
    turn_messages.extend(
        [
            {"role": "user", "content": copy.deepcopy(user_content)},
            {"role": "assistant", "content": content},
        ]
    )


def _drop_empty_values(value: Any) -> Any:
    if isinstance(value, dict):
        projected = {
            key: _drop_empty_values(item)
            for key, item in value.items()
        }
        return {
            key: item
            for key, item in projected.items()
            if item is not None and item != "" and item != [] and item != {}
        }
    if isinstance(value, list):
        projected = [_drop_empty_values(item) for item in value]
        return [
            item
            for item in projected
            if item is not None and item != "" and item != [] and item != {}
        ]
    return value


def _normalize_image_parts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    parts: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "image_url" and isinstance(item.get("image_url"), dict):
            url = str(item["image_url"].get("url") or "").strip()
            if not url:
                continue
            image_url: dict[str, Any] = {"url": url}
            detail = str(item["image_url"].get("detail") or "").strip()
            if detail:
                image_url["detail"] = detail
            parts.append({"type": "image_url", "image_url": image_url})
    return parts

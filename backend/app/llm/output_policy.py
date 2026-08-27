"""
@Time       : 2026/08/14 21:42
@Author     : zhanglp8181
@File       : output_policy.py
@CallChain  : llm_operation → LLMClient → provider请求参数
@Description: 按模型调用阶段限制输出预算，避免控制面分类请求继承长文生成额度。
"""

from __future__ import annotations


# Internal control-plane calls should not inherit the user-visible reply budget.
# Long-form code/content generation intentionally keeps a larger allowance.
OPERATION_MAX_OUTPUT_TOKENS: dict[str, int] = {
    "router.scene": 4096,
    "step_agent.run": 4096,
    "step_agent.repair": 4096,
    "response.generate": 4096,
    "response.generate_stream": 4096,
    "context.compact": 2048,
    "reflection.review": 2048,
    "general_skill.select": 2048,
    "dynamic_task.route_shadow": 2048,
    "general_skill.plan": 8192,
    "general_skill.repair": 8192,
    "general_skill.review": 2048,
    "general_skill.reply": 2048,
    # 规划是控制面，先用 8K 的保守首轮预算降低 Ark 等 provider 对长上下文
    # 请求的排队/过载概率；若真实响应被确认因长度截断，LLMClient 的 JSON
    # 修复契约会有界扩容到 16K/32K，复杂任务不会因固定小预算被静默截断。
    "dynamic_task.plan": 8_192,
    "dynamic_task.action": 2048,
    "dynamic_task.action.write": 8192,
    "dynamic_task.answer": 16384,
    "knowledge.document_route": 2048,
    "knowledge.bucket_route": 512,
    "knowledge.discovery": 4096,
    "knowledge.ingest_bucket": 8192,
    "memory.capture": 1024,
    "session.title": 512,
    "scheduled_task.detect": 1024,
    "feedback.analyze": 1024,
}

# Prime-Agent 的结构化 refinement/review 同样关闭 reasoning，避免推理耗尽预算后没有 JSON。
# 这里只覆盖动态计划控制面；最终 answer 继续遵循管理端 ModelConfig 的推理配置。
OPERATION_THINKING_MODE: dict[str, str] = {
    "dynamic_task.plan": "disabled",
}

def operation_output_tokens(operation: str, configured_tokens: int) -> int:
    """返回阶段预算与模型配置预算的较小值，未知阶段保留配置上限。"""

    configured = max(1, int(configured_tokens or 1))
    limit = OPERATION_MAX_OUTPUT_TOKENS.get(operation)
    return configured if limit is None else min(configured, limit)


def operation_thinking_mode(operation: str, configured_mode: str) -> str:
    """返回阶段冻结的推理模式；未配置阶段继续尊重管理端模型设置。"""

    return OPERATION_THINKING_MODE.get(operation, configured_mode)

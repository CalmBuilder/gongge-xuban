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
    "general_skill.plan": 8192,
    "general_skill.repair": 8192,
    "general_skill.review": 2048,
    "general_skill.reply": 2048,
    "knowledge.document_route": 2048,
    "knowledge.bucket_route": 512,
    "knowledge.discovery": 4096,
    "knowledge.ingest_bucket": 8192,
    "memory.capture": 1024,
    "session.title": 512,
    "scheduled_task.detect": 1024,
    "feedback.analyze": 1024,
}

def operation_output_tokens(operation: str, configured_tokens: int) -> int:
    configured = max(1, int(configured_tokens or 1))
    limit = OPERATION_MAX_OUTPUT_TOKENS.get(operation)
    return configured if limit is None else min(configured, limit)

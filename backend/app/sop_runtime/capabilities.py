"""
@Time       : 2026/07/22 05:16
@Author     : zhanglp8181
@File       : capabilities.py
@CallChain  : 定义编译器 → CapabilityRegistry → 节点配置/发布诊断
@Description: 维护统一 SOP 元模型允许使用的稳定能力及其适用节点类型。
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from types import MappingProxyType

from pydantic import Field

from app.sop_runtime.contracts import RuntimeContract
from app.sop_runtime.definition import (
    CompiledSopDefinition,
    NodeType,
    ServiceTaskKind,
    ServiceTaskNode,
)


class CapabilityAvailability(StrEnum):
    """能力在新 Runtime 中的真实落地成熟度。"""

    SCHEMA_ONLY = "schema_only"
    EXECUTABLE = "executable"


class CapabilitySpec(RuntimeContract):
    """单项 Runtime 能力的稳定名称、版本和节点适用范围。"""

    name: str = Field(min_length=1, max_length=128)
    version: int = Field(default=1, ge=1)
    node_types: frozenset[NodeType]
    availability: CapabilityAvailability = CapabilityAvailability.SCHEMA_ONLY
    description: str = Field(min_length=1, max_length=500)


class CapabilityRegistry:
    """只读能力注册表，用于发布期拒绝 Runtime 无法执行的定义。"""

    def __init__(self, capabilities: Iterable[CapabilitySpec]) -> None:
        """按能力名建立不可变索引，并拒绝重复注册。"""

        indexed: dict[str, CapabilitySpec] = {}
        for capability in capabilities:
            if capability.name in indexed:
                raise ValueError(f"duplicate capability: {capability.name}")
            indexed[capability.name] = capability
        self._capabilities = MappingProxyType(indexed)

    def get(self, name: str) -> CapabilitySpec | None:
        """按稳定名称查询能力，不向调用方暴露可变内部映射。"""

        return self._capabilities.get(name)

    def supports_definition(self, name: str, node_type: NodeType) -> bool:
        """判断能力 schema 是否存在并允许用于指定规范节点类型。"""

        capability = self.get(name)
        return capability is not None and node_type in capability.node_types

    def supports_execution(self, name: str, node_type: NodeType) -> bool:
        """判断能力是否已有新 Runtime 执行器，禁止把 schema 声明当成运行能力。"""

        capability = self.get(name)
        return (
            capability is not None
            and capability.availability is CapabilityAvailability.EXECUTABLE
            and node_type in capability.node_types
        )

    def names(self) -> tuple[str, ...]:
        """返回排序后的能力名称，供报告和测试稳定比较。"""

        return tuple(sorted(self._capabilities))

    def non_executable_nodes(
        self, definition: CompiledSopDefinition
    ) -> tuple[tuple[str, str], ...]:
        """返回定义中尚无 Runtime 执行器的节点及能力，供发布和启动双重门禁。"""

        unsupported: list[tuple[str, str]] = []
        for node in definition.nodes:
            node_type = NodeType(node.type)
            capability = (
                "input.collect"
                if node_type is NodeType.COLLECT_INPUT
                else node.config.capability
            )
            knowledge_contract_missing = (
                isinstance(node, ServiceTaskNode)
                and node.config.kind is ServiceTaskKind.KNOWLEDGE
                and (node.config.knowledge_query is None or not node.config.result_key)
            )
            if knowledge_contract_missing or not self.supports_execution(capability, node_type):
                unsupported.append((node.node_id, capability))
        return tuple(unsupported)


DEFAULT_CAPABILITY_REGISTRY = CapabilityRegistry(
    (
        CapabilitySpec(
            name="input.collect",
            node_types=frozenset({NodeType.COLLECT_INPUT}),
            availability=CapabilityAvailability.EXECUTABLE,
            description="收集并校验用户提供的结构化输入。",
        ),
        CapabilitySpec(
            name="decision.legacy_expression",
            node_types=frozenset({NodeType.DECISION}),
            description="在旧版 SkillCard 兼容边界解释有序条件连线。",
        ),
        CapabilitySpec(
            name="decision.restricted_dsl",
            node_types=frozenset({NodeType.DECISION}),
            availability=CapabilityAvailability.EXECUTABLE,
            description="按发布期编译的受限 JSON AST 和显式优先级选择确定分支。",
        ),
        CapabilitySpec(
            name="service.tool",
            node_types=frozenset({NodeType.SERVICE_TASK}),
            availability=CapabilityAvailability.EXECUTABLE,
            description="通过统一工具执行边界调用一个或多个工具操作。",
        ),
        CapabilitySpec(
            name="service.knowledge",
            node_types=frozenset({NodeType.SERVICE_TASK}),
            availability=CapabilityAvailability.EXECUTABLE,
            description="通过统一知识服务检索业务依据。",
        ),
        CapabilitySpec(
            name="service.response",
            node_types=frozenset({NodeType.SERVICE_TASK}),
            description="生成非终态的结构化用户反馈。",
        ),
        CapabilitySpec(
            name="human.conversational_handoff",
            node_types=frozenset({NodeType.HUMAN_TASK}),
            description="兼容现有自由文本人工问答接管，不代表审批工作项。",
        ),
        CapabilitySpec(
            name="human.structured_work_item",
            node_types=frozenset({NodeType.HUMAN_TASK}),
            availability=CapabilityAvailability.EXECUTABLE,
            description="创建具有参与者和完成策略的结构化人工工作项。",
        ),
        CapabilitySpec(
            name="terminal.response",
            node_types=frozenset({NodeType.TERMINAL}),
            availability=CapabilityAvailability.EXECUTABLE,
            description="输出最终响应并结束当前 SOP 路径。",
        ),
    )
)

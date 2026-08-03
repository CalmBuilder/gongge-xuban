"""
@Time       : 2026/07/22 20:20
@Author     : zhanglp8181
@File       : test_sop_slot_values.py
@CallChain  : pytest → 发布内容/SkillCard 编译 → 槽位键值归一
@Description: 验证发布期冻结的槽位键和值别名能够确定性归一且不会猜测未知值。
"""

from app.sop_runtime.legacy_skill_card_adapter import compile_legacy_skill_card
from app.sop_runtime.slot_values import canonicalize_slot_keys, normalize_slot_values


def _leave_definition():
    """构造只包含假期类型输入和终态的最小规范定义。"""

    return compile_legacy_skill_card(
        {
            "skill_id": "leave_alias_test",
            "name": "假期别名测试",
            "version": "1.0.0",
            "nodes": [
                {
                    "node_id": "collect_leave_type",
                    "type": "collect_info",
                    "name": "收集假期类型",
                    "expected_user_info": ["leave_type"],
                    "metadata": {
                        "value_aliases": {
                            "leave_type": {
                                "年假": "annual",
                                "ANNUAL": "annual",
                                "调休": "compensatory",
                                "compensatory": "compensatory",
                            }
                        }
                    },
                },
                {
                    "node_id": "reply",
                    "type": "response",
                    "name": "反馈结果",
                    "allowed_actions": ["answer_user"],
                },
            ],
            "edges": [
                {"source_node_id": "collect_leave_type", "next_node_id": "reply"}
            ],
            "start_node_id": "collect_leave_type",
            "terminal_node_ids": ["reply"],
        }
    )


def test_normalize_slot_values_maps_chinese_and_casefolded_aliases() -> None:
    """验证中文别名和英文大小写都归一到同一稳定业务枚举。"""

    definition = _leave_definition()

    assert definition.meta_model_version == 3
    assert normalize_slot_values(definition, {"leave_type": "年假"}) == {
        "leave_type": "annual"
    }
    assert normalize_slot_values(definition, {"leave_type": "Annual"}) == {
        "leave_type": "annual"
    }


def test_normalize_slot_values_rejects_unknown_and_non_string_values() -> None:
    """验证白名单外或非字符串输入被清空并继续追问，不由 Runtime 擅自推断。"""

    definition = _leave_definition()

    assert normalize_slot_values(definition, {"leave_type": "婚假"}) == {
        "leave_type": ""
    }
    assert normalize_slot_values(definition, {"leave_type": 3}) == {"leave_type": ""}


def test_canonicalize_slot_keys_uses_frozen_aliases_without_overwriting_canonical_value() -> None:
    """验证模型字段别名映射到规范键，且不会覆盖会话中已有的非空规范值。"""

    content = {
        "slot_key_aliases": {
            "company_name": "enterprise_full_name",
            "enterprise_name": "enterprise_full_name",
        }
    }

    assert canonicalize_slot_keys(
        content,
        {
            "enterprise_name": "共格演示科技有限公司",
            "unified_social_credit_code": "91370000MA3D3M001X",
        },
    ) == {
        "enterprise_full_name": "共格演示科技有限公司",
        "unified_social_credit_code": "91370000MA3D3M001X",
    }
    assert canonicalize_slot_keys(
        content,
        {
            "enterprise_full_name": "已确认企业",
            "company_name": "不应覆盖企业",
        },
    ) == {"enterprise_full_name": "已确认企业"}

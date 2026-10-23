"""
@Time       : 2026/10/04
@Author     : zhanglp8181
@File       : test_final_f2_h3_fixture.py
@CallChain  : H3夹具控制核验 → 人工局部指令及真正消费者结果 → 后续浏览器
@Description: 防止受控协议偷偷注入固定候选/答案，独立业务标签不能冒充工具输入。
"""
from copy import deepcopy

import pytest

from final_f2_h3_fixture import initial_card, proposal_from_instruction, quota_text


def test_instruction_selects_actual_slot_without_changing_source() -> None:
    """替换为另一个真实slot也只按人工指令构造局部patch，不预选正确本人答案。"""
    card = initial_card()
    before = deepcopy(card)
    response = proposal_from_instruction({"current_skill": card, "instruction":
        "nodes.query.metadata.operation_input.employee_id 改为 slots.sponsor_id"})
    assert response["patches"][0]["value"]["operation_input"] == {"employee_id": "slots.sponsor_id"}
    assert card == before
    assert "E001" not in str(response) and "1500" not in str(response)


@pytest.mark.parametrize("instruction", [
    "请自动修好", "nodes.missing.metadata.operation_input.employee_id 改为 slots.employee_id",
    "nodes.query.metadata.operation_input.employee_id 改为 slots.missing",
])
def test_missing_explicit_node_or_slot_is_rejected(instruction: str) -> None:
    """缺少明确局部指令或真实定义身份时不由固定业务标签补正确候选。"""
    with pytest.raises(ValueError):
        proposal_from_instruction({"current_skill": initial_card(), "instruction": instruction})


def test_user_label_and_database_expected_value_cannot_become_tool_answer() -> None:
    """用户/标签/slots中的期待额度不等于实际工具回执，不得宣称查询完成。"""
    value = quota_text({"user_message": "E001=1500", "labels": {"employee_id": "E001", "quota": 1500},
                        "slots": {"employee_id": "E001", "quota": 1500}})
    assert "没有实际工具额度" in value
    assert "1500" not in value


def test_reply_uses_actual_nested_operation_result_even_when_expectation_differs() -> None:
    """返回独立实际工具结果E002/600，即使当前本人标签为E001/1500也不能改答案。"""
    value = quota_text({"user_message": "本人E001期待1500", "tool_result": {
        "data": {"data": {"employee_id": "E002", "quota": 600}}}})
    assert "E002" in value and "600" in value
    assert "E001" not in value and "1500" not in value

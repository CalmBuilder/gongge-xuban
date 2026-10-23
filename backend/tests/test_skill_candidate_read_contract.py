"""
@Time       : 2026/10/03
@Author     : zhanglp8181
@File       : test_skill_candidate_read_contract.py
@CallChain  : 当前反馈编辑器刷新 → 原候选slot与确定性差异投影
@Description: 失效来源仍可安全定位重载修订，缺键/null增删不能被差异算法忽略。
"""
import pytest

from app.skills.candidates import _changed_paths
from tests.test_feedback_materials import feedback_engine as _feedback_engine
from tests.test_skill_feedback_candidates import PREFIX, TENANT, _client, _open, _setup

contract_engine = _feedback_engine


@pytest.mark.parametrize('before,after,path', [
    ({}, {'note': None}, '/note'),
    ({'note': None}, {}, '/note'),
    ([], [None], '/0'),
    ([None], [], '/0'),
    ({'a/b~c': {}}, {'a/b~c': {'note': None}}, '/a~1b~0c/note'),
])
def test_candidate_diff_distinguishes_absence_from_explicit_null(before, after, path):
    """JSON树存在性也是事实变化；保持原pointer转义与合法无变化对照。"""
    assert _changed_paths(before, after) == [path]
    assert _changed_paths(after, after) == []


def test_editor_slot_refresh_returns_only_current_identity_revision(contract_engine):
    """刷新可获得丢弃失效候选所需当前修订，不返回来源/候选/基础正文。"""
    material, agent, _owner = _setup(contract_engine)
    with _client(contract_engine) as client:
        candidate = _open(client, material, agent)
        result = client.get(PREFIX + '/feedback-candidates/editor-context',
                            params={'tenant_id': TENANT})
        assert result.status_code == 200
        assert result.json()['candidate_revision'] == candidate['revision']
        assert result.json()['scope_profile_id'] == agent
        assert not {'content', 'materials', 'base_content'} & result.json().keys()

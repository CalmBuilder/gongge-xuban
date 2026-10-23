import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import * as candidates from '@/api/skill-candidates';
import type { SkillCandidateRead, SkillEditorContextRead } from '@/types/skill-candidate';
import type { SkillCard } from '@/types';
import { FEEDBACK_EDITOR_INVALID, useSkillFeedbackEditor } from './useSkillFeedbackEditor';
import { ApiError } from '@/api/client';

vi.mock('@/api/skill-candidates', () => ({
  readSkillCandidate: vi.fn(), readSkillEditorContext: vi.fn(), resetSkillCandidate: vi.fn(),
  saveSkillCandidate: vi.fn(), rejectSkillCandidate: vi.fn(), publishSkillCandidate: vi.fn(),
}));

const slot: SkillEditorContextRead = { candidate_id: 'candidate-a', candidate_revision: 2,
  status: 'draft', scope_agent_id: 'agent-a', scope_profile_id: 'agent-a', ordinary: false, editor_context: null };
const candidate: SkillCandidateRead = { id: 'candidate-a', tenant_id: 'tenant-demo',
  skill_id: 'skill-a', scope_agent_id: 'agent-a', revision: 2, status: 'draft',
  base_skill_version_id: 'version-a', base_checksum: 'checksum-a', base_version: '1.0.0',
  base_difference: false, base_difference_confirmed: false, base_content: null, content: null,
  materials: [], changed_paths: [], published: null };
const card: SkillCard = { skill_id: 'skill-a', name: 'Synthetic', version: '1.0.0', description: 'Synthetic',
  trigger_intents: [], user_utterance_examples: [], goal: [], required_info: [], nodes: [], edges: [],
  start_node_id: '', terminal_node_ids: [], interruption_policy: {}, response_rules: [] };

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(candidates.readSkillEditorContext).mockResolvedValue(slot);
  window.localStorage.clear();
});

it('失效来源只保留安全重载身份，不读旧浏览器草稿或展示provider异常', async () => {
  window.localStorage.setItem('skill-distill:old', 'secret-old-material');
  vi.mocked(candidates.readSkillCandidate).mockRejectedValue(new Error('secret-provider-payload'));
  const { result } = renderHook(() => useSkillFeedbackEditor('candidate-a', 'agent-a', 'skill-a'));
  await waitFor(() => expect(result.current.ready).toBe(true));
  expect(result.current.candidate).toBeNull();
  expect(result.current.error).toBe(FEEDBACK_EDITOR_INVALID);
  expect(result.current.context?.candidate_revision).toBe(2);
  expect(result.current.blocked).toBe(true);
});

it('删除URL候选ID也不能退出服务器受管关联，显式重载才得到普通nonce', async () => {
  const { result } = renderHook(() => useSkillFeedbackEditor('', 'agent-b', null));
  await waitFor(() => expect(result.current.ready).toBe(true));
  expect(result.current.blocked).toBe(true);
  vi.mocked(candidates.resetSkillCandidate).mockResolvedValue({ candidate_id: 'candidate-a',
    revision: 3, editor_context: 'fresh-ordinary-nonce', current_skill: null });
  await act(async () => { await result.current.reset(); });
  expect(candidates.resetSkillCandidate).toHaveBeenCalledWith('candidate-a', 2, 'agent-b', null, expect.any(String));
  expect(result.current.editorContext).toBe('fresh-ordinary-nonce');
  expect(result.current.blocked).toBe(false);
  expect(result.current.candidate).toBeNull();
});

it('scope切换忽略旧请求的迟到来源，无新权限时仍阻止普通写', async () => {
  let resolveOld: ((value: SkillCandidateRead) => void) | undefined;
  vi.mocked(candidates.readSkillCandidate).mockImplementation(() => new Promise((resolve) => { resolveOld = resolve; }));
  const { result, rerender } = renderHook(({ agent }) => useSkillFeedbackEditor('candidate-a', agent, 'skill-a'),
    { initialProps: { agent: 'agent-a' } });
  await waitFor(() => expect(candidates.readSkillCandidate).toHaveBeenCalledTimes(1));
  vi.mocked(candidates.readSkillEditorContext).mockRejectedValue(new Error('403 private'));
  rerender({ agent: 'agent-b' });
  await act(async () => { resolveOld?.(candidate); });
  await waitFor(() => expect(result.current.error).toBe(FEEDBACK_EDITOR_INVALID));
  expect(result.current.candidate).toBeNull();
  expect(result.current.blocked).toBe(true);
});

it('明确确认旧来源与当前基础差异时保存确认修订，再用新修订绑定人工发布', async () => {
  const current = { ...candidate, content: card, base_difference: true, changed_paths: ['/name'] };
  vi.mocked(candidates.readSkillCandidate).mockResolvedValue(current);
  vi.mocked(candidates.saveSkillCandidate).mockResolvedValue({ ...current, revision: 3,
    base_difference_confirmed: true });
  vi.mocked(candidates.publishSkillCandidate).mockResolvedValue({ candidate_id: current.id,
    skill_id: current.skill_id, scope_agent_id: current.scope_agent_id, skill_version_id: 'version-b',
    version: '2.0.0', definition_checksum: 'new-checksum' });
  const { result } = renderHook(() => useSkillFeedbackEditor('candidate-a', 'agent-a', 'skill-a'));
  await waitFor(() => expect(result.current.candidate?.content).toEqual(card));
  await act(async () => { await result.current.publish(true); });
  expect(candidates.saveSkillCandidate).toHaveBeenCalledWith(
    expect.objectContaining({ base_difference_confirmed: true }), card,
  );
  expect(candidates.publishSkillCandidate).toHaveBeenCalledWith(
    expect.objectContaining({ revision: 3, base_difference_confirmed: true }), expect.any(String), true,
  );
});

it('失效后迟到保存响应不能恢复旧候选或材料', async () => {
  const current = { ...candidate, content: card };
  vi.mocked(candidates.readSkillCandidate).mockResolvedValue(current);
  let resolveOld: ((value: SkillCandidateRead) => void) | undefined;
  vi.mocked(candidates.saveSkillCandidate).mockImplementation(() => new Promise((resolve) => { resolveOld = resolve; }));
  const { result } = renderHook(() => useSkillFeedbackEditor('candidate-a', 'agent-a', 'skill-a'));
  await waitFor(() => expect(result.current.candidate?.content).toEqual(card));
  let pending: Promise<SkillCandidateRead | null> | undefined;
  act(() => { pending = result.current.save(card); });
  act(() => result.current.invalidate());
  await act(async () => { resolveOld?.({ ...current, revision: 3 }); await pending; });
  expect(result.current.candidate).toBeNull();
  expect(result.current.blocked).toBe(true);
  expect(result.current.busy).toBe(false);
});

it('整体候选仍须匹配实际整体profile，员工URL不能借空scope发布整体', async () => {
  vi.mocked(candidates.readSkillEditorContext).mockResolvedValue({ ...slot, scope_agent_id: '',
    scope_profile_id: 'overall-profile' });
  vi.mocked(candidates.readSkillCandidate).mockResolvedValue({ ...candidate, content: card, scope_agent_id: '' });
  const { result } = renderHook(() => useSkillFeedbackEditor('candidate-a', 'agent-b', 'skill-a'));
  await waitFor(() => expect(result.current.ready).toBe(true));
  expect(result.current.blocked).toBe(true);
  expect(result.current.candidate).toBeNull();
  expect(candidates.publishSkillCandidate).not.toHaveBeenCalled();
});

it('合法整体profile及未指定scope仍能加载当前整体候选', async () => {
  vi.mocked(candidates.readSkillEditorContext).mockResolvedValue({ ...slot, scope_agent_id: '',
    scope_profile_id: 'overall-profile' });
  vi.mocked(candidates.readSkillCandidate).mockResolvedValue({ ...candidate, content: card, scope_agent_id: '' });
  const { result, rerender } = renderHook(({ agent }) => useSkillFeedbackEditor('candidate-a', agent, 'skill-a'),
    { initialProps: { agent: 'overall-profile' } });
  await waitFor(() => expect(result.current.candidate?.content).toEqual(card));
  expect(result.current.blocked).toBe(false);
  rerender({ agent: '' });
  await waitFor(() => expect(result.current.candidate?.content).toEqual(card));
  expect(result.current.blocked).toBe(false);
});

it('合法已发布候选刷新只恢复安全回执，保持编辑和重复发布阻断', async () => {
  const receipt = { candidate_id: candidate.id, skill_id: candidate.skill_id,
    scope_agent_id: candidate.scope_agent_id, skill_version_id: 'published-b',
    version: '2.0.0', definition_checksum: 'published-checksum' };
  vi.mocked(candidates.readSkillEditorContext).mockResolvedValue({ ...slot, status: 'published' });
  vi.mocked(candidates.readSkillCandidate).mockResolvedValue({ ...candidate,
    status: 'published', content: null, materials: [], published: receipt });
  const { result } = renderHook(() => useSkillFeedbackEditor('candidate-a', 'agent-a', 'skill-a'));
  await waitFor(() => expect(result.current.ready).toBe(true));
  expect(result.current.error).toBe('');
  expect(result.current.candidate?.published).toEqual(receipt);
  expect(result.current.candidate?.content).toBeNull();
  expect(result.current.candidate?.materials).toEqual([]);
  expect(result.current.blocked).toBe(true);
  await act(async () => { await result.current.publish(false); });
  expect(candidates.publishSkillCandidate).not.toHaveBeenCalled();
});

it('已发布回执的错误scope仍拒绝，不借历史状态放行未发布正文', async () => {
  vi.mocked(candidates.readSkillCandidate).mockResolvedValue({ ...candidate,
    status: 'published', content: card, materials: [], published: null });
  const { result } = renderHook(() => useSkillFeedbackEditor('candidate-a', 'agent-b', 'skill-a'));
  await waitFor(() => expect(result.current.ready).toBe(true));
  expect(result.current.error).toBe(FEEDBACK_EDITOR_INVALID);
  expect(result.current.candidate).toBeNull();
  expect(result.current.blocked).toBe(true);
});

it.each([408, 429, 409])('job丢失后的fresh暂态%i不撤销合法编辑', async (status) => {
  const current = { ...candidate, content: card };
  vi.mocked(candidates.readSkillCandidate).mockResolvedValue(current);
  const { result } = renderHook(() => useSkillFeedbackEditor('candidate-a', 'agent-a', 'skill-a'));
  await waitFor(() => expect(result.current.candidate?.content).toEqual(card));
  vi.mocked(candidates.readSkillCandidate).mockRejectedValue(new ApiError(status,
    JSON.stringify({ detail: status === 409 ? 'FEEDBACK_CANDIDATE_WRITE_CONFLICT' : 'temporary' }), 'temporary'));
  await act(async () => { await result.current.revalidate(); });
  expect(result.current.candidate).toEqual(current);
  expect(result.current.blocked).toBe(false);
});

it('同候选合法保存推进修订后，旧fresh回包不能清新编辑状态', async () => {
  const current = { ...candidate, content: card };
  vi.mocked(candidates.readSkillCandidate).mockResolvedValue(current);
  const { result } = renderHook(() => useSkillFeedbackEditor('candidate-a', 'agent-a', 'skill-a'));
  await waitFor(() => expect(result.current.candidate?.content).toEqual(card));
  let resolveOld: ((value: SkillCandidateRead) => void) | undefined;
  vi.mocked(candidates.readSkillCandidate).mockImplementation(() => new Promise((resolve) => { resolveOld = resolve; }));
  let pending: Promise<void> | undefined;
  act(() => { pending = result.current.revalidate(); });
  await waitFor(() => expect(resolveOld).toBeDefined());
  const saved = { ...current, revision: 3, content: { ...card, name: 'Saved current revision' } };
  vi.mocked(candidates.saveSkillCandidate).mockResolvedValue(saved);
  await act(async () => { await result.current.save(saved.content); });
  await act(async () => { resolveOld?.(saved); await pending; });
  expect(result.current.candidate).toEqual(saved);
  expect(result.current.blocked).toBe(false);
});

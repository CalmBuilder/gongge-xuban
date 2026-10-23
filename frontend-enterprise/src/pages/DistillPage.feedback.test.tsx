import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, expect, it, vi } from 'vitest';
import { I18nProvider } from '@/i18n';
import type { SkillCard, SkillRead } from '@/types';
import type { SkillCandidateRead, SkillEditorContextRead } from '@/types/skill-candidate';
import * as candidates from '@/api/skill-candidates';
import { api } from '@/api/client';
import DistillPage from './DistillPage';

vi.mock('@/components/AppHeader', () => ({ default: () => null }));
vi.mock('@/api/skill-candidates', () => ({ readSkillCandidate: vi.fn(), readSkillEditorContext: vi.fn(),
  resetSkillCandidate: vi.fn(), saveSkillCandidate: vi.fn(), rejectSkillCandidate: vi.fn(), publishSkillCandidate: vi.fn() }));
vi.mock('@/api/client', () => ({ api: { get: vi.fn() }, getRequestTenantId: () => 'tenant-demo',
  streamGet: vi.fn(), streamPost: vi.fn(), ApiError: class extends Error {} }));

const card: SkillCard = { skill_id: 'skill-a', name: 'SYNTHETIC_PRIVATE_CANDIDATE', version: '1.0.0',
  description: '合成来源', trigger_intents: [], user_utterance_examples: [], goal: [], required_info: [],
  nodes: [], edges: [], start_node_id: '', terminal_node_ids: [], interruption_policy: {}, response_rules: [] };
const slot: SkillEditorContextRead = { candidate_id: 'candidate-a', candidate_revision: 2,
  status: 'draft', scope_agent_id: 'agent-a', scope_profile_id: 'agent-a', ordinary: false, editor_context: null };
const candidate: SkillCandidateRead = { id: 'candidate-a', tenant_id: 'tenant-demo', skill_id: 'skill-a',
  scope_agent_id: 'agent-a', revision: 2, status: 'draft', base_skill_version_id: 'version-a',
  base_checksum: 'checksum-a', base_version: '1.0.0', base_difference: false, base_difference_confirmed: false,
  base_content: card, content: card, materials: [], changed_paths: [], published: null };
const managedParams = new URLSearchParams('skill_id=skill-a&agent_id=agent-a&feedback_candidate_id=candidate-a');
const page = (params: URLSearchParams) => <MemoryRouter><I18nProvider>
  <DistillPage searchParamsOverride={params} />
</I18nProvider></MemoryRouter>;

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.get).mockResolvedValue([]);
  vi.mocked(candidates.readSkillEditorContext).mockResolvedValue(slot);
  vi.mocked(candidates.readSkillCandidate).mockResolvedValue(candidate);
});

it('受管来源失效立即关闭保存弹窗并清除私密差异快照', async () => {
  const user = userEvent.setup();
  render(page(managedParams));
  const title = await screen.findByRole('textbox', { name: 'SOP名称' });
  await waitFor(() => expect(title).toHaveValue(card.name));
  await user.clear(title); await user.type(title, 'SYNTHETIC_PRIVATE_EDIT');
  await user.click(screen.getByRole('button', { name: '保存隔离候选' }));
  expect(screen.getByRole('dialog')).toBeInTheDocument();
  vi.mocked(candidates.readSkillCandidate).mockRejectedValue(new Error('synthetic-withdrawn'));
  // 弹窗打开时仍可能收到范围变更；实际页面监听器负责换源并撤销旧状态。
  act(() => window.dispatchEvent(new CustomEvent('gongge-enterprise-agent-scope-change',
    { detail: { agentId: 'agent-b' } })));
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  expect(document.body.textContent).not.toContain('SYNTHETIC_PRIVATE');
});

it('普通原版读取迟到不能覆盖后来加载的受管候选', async () => {
  let resolveOld: ((value: SkillRead) => void) | undefined;
  vi.mocked(candidates.readSkillEditorContext).mockResolvedValue({ ...slot, candidate_id: null,
    candidate_revision: null, status: null, scope_agent_id: '', ordinary: false });
  vi.mocked(api.get).mockImplementation((path: string) => path.startsWith('/api/enterprise/skills/skill-a?')
    ? new Promise<SkillRead>((resolve) => { resolveOld = resolve; }) : Promise.resolve([]));
  const view = render(page(new URLSearchParams('skill_id=skill-a&agent_id=agent-a')));
  await waitFor(() => expect(resolveOld).toBeDefined());
  vi.mocked(candidates.readSkillEditorContext).mockResolvedValue(slot);
  view.rerender(page(managedParams));
  await waitFor(() => expect(screen.getByRole('textbox', { name: 'SOP名称' })).toHaveValue(card.name));
  await act(async () => resolveOld?.({ id: 'ordinary-a', tenant_id: 'tenant-demo', skill_id: 'skill-a',
    name: 'SYNTHETIC_LATE_ORIGINAL', version: '1.0.0', description: '', status: 'published',
    call_count: 0, positive_feedback_count: 0, negative_feedback_count: 0, positive_rate: 0, negative_rate: 0,
    total_call_count: 0, total_positive_feedback_count: 0, total_negative_feedback_count: 0,
    total_positive_rate: 0, total_negative_rate: 0, recent_versions: [], recent_call_count: 0,
    recent_positive_feedback_count: 0, recent_negative_feedback_count: 0, recent_positive_rate: 0, recent_negative_rate: 0,
    created_at: '2026-10-03T00:00:00Z', updated_at: '2026-10-03T00:00:00Z',
    content: { ...card, name: 'SYNTHETIC_LATE_ORIGINAL' } }));
  expect(screen.getByRole('textbox', { name: 'SOP名称' })).toHaveValue(card.name);
});

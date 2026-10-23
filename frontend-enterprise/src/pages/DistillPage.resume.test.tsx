import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { I18nProvider } from '@/i18n';
import { api, ApiError } from '@/api/client';
import * as candidates from '@/api/skill-candidates';
import { writeFeedbackJobPointer } from '@/lib/feedback-job-pointer';
import type { SkillCard } from '@/types';
import type { FeedbackMaterialRead } from '@/types/feedback';
import type { SkillCandidateRead, SkillEditorContextRead } from '@/types/skill-candidate';
import DistillPage from '@/pages/DistillPage';

vi.mock('@/components/AppHeader', () => ({ default: () => null }));
vi.mock('@/api/skill-candidates', () => ({ readSkillCandidate: vi.fn(), readSkillEditorContext: vi.fn(),
  resetSkillCandidate: vi.fn(), saveSkillCandidate: vi.fn(), rejectSkillCandidate: vi.fn(), publishSkillCandidate: vi.fn() }));
vi.mock('@/api/client', async (importOriginal) => {
  const original = await importOriginal<typeof import('@/api/client')>();
  return { ...original, api: { ...original.api, get: vi.fn() }, getRequestTenantId: () => 'tenant-demo' };
});

const card: SkillCard = { skill_id: 'skill-a', name: 'SYNTHETIC_PRIVATE_RESUME_CANDIDATE', version: '1.0.0',
  description: 'SYNTHETIC_PRIVATE_RESUME_DIFF', trigger_intents: [], user_utterance_examples: [], goal: [], required_info: [],
  nodes: [], edges: [], start_node_id: '', terminal_node_ids: [], interruption_policy: {}, response_rules: [] };
const material: FeedbackMaterialRead = { id: 'material-a', feedback_id: 'feedback-a', feedback_revision: 1,
  target_agent_id: 'agent-a', status: 'shared', material_checksum: 'synthetic-checksum',
  material: { source: { status: 'verified', branch: 'sop', message_id: 'message-a', turn_id: 'turn-a',
    execution_id: 'execution-a', skill_id: 'skill-a', skill_version_id: 'version-a', skill_version: '1.0.0',
    definition_checksum: 'checksum-a', agent_id: 'agent-a', missing_reason: null },
    facts: { rating: 'down', user_request: '', assistant_reply: '', operations_known: true, operations: [] },
    user_note: 'SYNTHETIC_PRIVATE_RESUME_MATERIAL', inference: null }, created_at: '2026-10-03T00:00:00Z', updated_at: '2026-10-03T00:00:00Z' };
const slot: SkillEditorContextRead = { candidate_id: 'candidate-a', candidate_revision: 2, status: 'draft',
  scope_agent_id: 'agent-a', scope_profile_id: 'agent-a', ordinary: false, editor_context: null };
const candidate: SkillCandidateRead = { id: 'candidate-a', tenant_id: 'tenant-demo', skill_id: 'skill-a',
  scope_agent_id: 'agent-a', revision: 2, status: 'draft', base_skill_version_id: 'version-a',
  base_checksum: 'checksum-a', base_version: '1.0.0', base_difference: false, base_difference_confirmed: false,
  base_content: { ...card, description: 'SYNTHETIC_OLD_BASE' }, content: card, materials: [material],
  changed_paths: ['/description'], published: null };
const pointer = 'skill-feedback-job:tenant-demo:candidate-a';
let rejectJob: ((error: unknown) => void) | undefined;
let resolveJob: ((value: { status: string }) => void) | undefined;
const transport = vi.fn<typeof fetch>();

beforeEach(() => {
  vi.clearAllMocks(); window.sessionStorage.clear(); resolveJob = undefined; rejectJob = undefined; transport.mockReset();
  vi.stubGlobal('fetch', transport);
  vi.mocked(api.get).mockImplementation((path: string) => path.includes('/skills/jobs/')
    ? new Promise<{ status: string }>((resolve, reject) => { resolveJob = resolve; rejectJob = reject; }) : Promise.resolve([]));
  vi.mocked(candidates.readSkillEditorContext).mockResolvedValue(slot);
  vi.mocked(candidates.readSkillCandidate).mockResolvedValue(candidate);
});

async function mountThenResolveJob(beforeJob?: () => void) {
  /** 先加载真实页面的授权候选，仅存原指针合同允许的job ID，再解除GET job有效200。 */
  writeFeedbackJobPointer(pointer, 'job-a');
  render(<MemoryRouter><I18nProvider><DistillPage searchParamsOverride={
    new URLSearchParams('skill_id=skill-a&agent_id=agent-a&feedback_candidate_id=candidate-a')
  } /></I18nProvider></MemoryRouter>);
  await waitFor(() => expect(screen.getByRole('textbox', { name: 'SOP名称' })).toHaveValue(card.name));
  expect(document.body.textContent).toContain('SYNTHETIC_PRIVATE_RESUME_MATERIAL');
  await waitFor(() => expect(resolveJob).toBeDefined());
  beforeJob?.();
  await act(async () => resolveJob?.({ status: 'queued' }));
  await waitFor(() => expect(transport).toHaveBeenCalled());
}

it.each(['stream', 'preflight'])('通用job404-%s清旧指针后校验来源，保留未保存草稿', async (phase) => {
  const missing = new ApiError(404, JSON.stringify({ detail: 'Job not found' }), 'missing');
  let release: ((value: Response) => void) | undefined;
  transport.mockImplementation(() => new Promise((resolve) => { release = resolve; }));
  if (phase === 'stream') await mountThenResolveJob();
  else {
    writeFeedbackJobPointer(pointer, 'job-a');
    render(<MemoryRouter><I18nProvider><DistillPage searchParamsOverride={
      new URLSearchParams('skill_id=skill-a&agent_id=agent-a&feedback_candidate_id=candidate-a')
    } /></I18nProvider></MemoryRouter>);
    await waitFor(() => expect(resolveJob).toBeDefined());
  }
  const title = await screen.findByRole('textbox', { name: 'SOP名称' });
  await waitFor(() => expect(title).toHaveValue(card.name));
  fireEvent.change(title, { target: { value: 'SYNTHETIC_UNSAVED_VALID_DRAFT' } });
  if (phase === 'stream') await act(async () => release?.(new Response(missing.body, { status: 404 })));
  else await act(async () => rejectJob?.(missing));
  await waitFor(() => expect(window.sessionStorage.getItem(pointer)).toBeNull());
  await waitFor(() => expect(candidates.readSkillCandidate).toHaveBeenCalledTimes(2));
  expect(screen.getByRole('textbox', { name: 'SOP名称' })).toHaveValue('SYNTHETIC_UNSAVED_VALID_DRAFT');
  expect(screen.getByRole('button', { name: '仅保存隔离候选' })).toBeEnabled();
  expect(screen.getByRole('button', { name: '人工确认发布候选' })).toBeDisabled();
});

it('通用job404后fresh来源失效，清正文和所有候选操作', async () => {
  let release: ((value: Response) => void) | undefined;
  transport.mockImplementation(() => new Promise((resolve) => { release = resolve; }));
  await mountThenResolveJob(() => {
    fireEvent.change(screen.getByRole('textbox', { name: 'SOP名称' }),
      { target: { value: 'SYNTHETIC_PRIVATE_UNSAVED_DIALOG' } });
    fireEvent.click(screen.getByRole('button', { name: '保存隔离候选' }));
    expect(screen.getByRole('dialog', { name: '保存隔离候选' })).toBeInTheDocument();
  });
  vi.mocked(candidates.readSkillCandidate).mockRejectedValue(new ApiError(409,
    JSON.stringify({ detail: 'FEEDBACK_CANDIDATE_SOURCE_STALE' }), 'stale'));
  await act(async () => release?.(new Response(JSON.stringify({ detail: 'Job not found' }), { status: 404 })));
  await waitFor(() => expect(screen.queryByRole('textbox', { name: 'SOP名称' })).toBeNull());
  expect(window.sessionStorage.getItem(pointer)).toBeNull();
  expect(document.body.textContent).not.toContain('SYNTHETIC_PRIVATE_RESUME');
});

it('合法job404续接后取消保存弹窗，后续人工编辑必须保存当前正文', async () => {
  /** 原页面、原404分类及fresh来源校验后，不允许已取消的私密标题覆盖当前人工编辑。 */
  const user = userEvent.setup();
  let release: ((value: Response) => void) | undefined;
  transport.mockImplementation(() => new Promise((resolve) => { release = resolve; }));
  await mountThenResolveJob(() => {
    fireEvent.change(screen.getByRole('textbox', { name: 'SOP名称' }),
      { target: { value: 'SYNTHETIC_TEMPORARY_PRIVATE_TITLE' } });
    fireEvent.click(screen.getByRole('button', { name: '保存隔离候选' }));
  });
  await act(async () => release?.(new Response(JSON.stringify({ detail: 'Job not found' }), { status: 404 })));
  await waitFor(() => expect(window.sessionStorage.getItem(pointer)).toBeNull());
  await waitFor(() => expect(candidates.readSkillCandidate).toHaveBeenCalledTimes(2));
  await user.click(screen.getByRole('button', { name: /^取消$/ }));
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  const title = screen.getByRole('textbox', { name: 'SOP名称' });
  await user.clear(title); await user.type(title, card.name);
  vi.mocked(candidates.saveSkillCandidate).mockResolvedValue({ ...candidate, revision: 3 });
  await user.click(screen.getByRole('button', { name: '仅保存隔离候选' }));
  expect(candidates.saveSkillCandidate).toHaveBeenCalledWith(candidate, card);
});

it('job初检临时断网保留合法候选及定位，不当成来源撤销', async () => {
  writeFeedbackJobPointer(pointer, 'job-a');
  render(<MemoryRouter><I18nProvider><DistillPage searchParamsOverride={
    new URLSearchParams('skill_id=skill-a&agent_id=agent-a&feedback_candidate_id=candidate-a')
  } /></I18nProvider></MemoryRouter>);
  await waitFor(() => expect(screen.getByRole('textbox', { name: 'SOP名称' })).toHaveValue(card.name));
  await act(async () => rejectJob?.(new TypeError('SYNTHETIC_NETWORK_FAILURE')));
  expect(screen.getByRole('textbox', { name: 'SOP名称' })).toHaveValue(card.name);
  expect(window.sessionStorage.getItem(pointer)).not.toBeNull();
});

it.each([403, 404, 409])('resume真实HTTP %i已知失效必须清原候选/材料/diff/指针并阻止操作', async (status) => {
  /** 使用原streamGet、真实Response和原ApiError status，不用假异常替代服务器入口拒绝。 */
  transport.mockResolvedValue(new Response(JSON.stringify({ detail: 'FEEDBACK_CANDIDATE_UNAVAILABLE' }),
    { status, headers: { 'Content-Type': 'application/json' } }));
  const originalError = new ApiError(status, JSON.stringify({ detail: 'FEEDBACK_CANDIDATE_UNAVAILABLE' }), 'unavailable');
  expect(originalError.status).toBe(status);
  await mountThenResolveJob(() => {
    fireEvent.change(screen.getByRole('textbox', { name: 'SOP名称' }),
      { target: { value: 'SYNTHETIC_PRIVATE_UNSAVED_DIALOG' } });
    fireEvent.click(screen.getByRole('button', { name: '保存隔离候选' }));
    expect(screen.getByRole('dialog', { name: '保存隔离候选' })).toBeInTheDocument();
  });
  await waitFor(() => expect(screen.queryByRole('button', { name: '停止' })).not.toBeInTheDocument());
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 30)); });
  const observed = { status, private_title_cleared: screen.queryByRole('textbox', { name: 'SOP名称' }) === null,
    private_material_cleared: !document.body.textContent?.includes('SYNTHETIC_PRIVATE_RESUME_MATERIAL'),
    private_diff_cleared: !document.body.textContent?.includes('SYNTHETIC_PRIVATE_RESUME_DIFF'),
    pointer_cleared: window.sessionStorage.getItem(pointer) === null,
    candidate_publish_removed: screen.queryByRole('button', { name: '人工确认发布候选' }) === null };
  expect(observed).toEqual({ status, private_title_cleared: true, private_material_cleared: true,
    private_diff_cleared: true, pointer_cleared: true, candidate_publish_removed: true });
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});

it.each([403, 404, 409])('job初检明确失效%i清私密和指针', async (status) => {
  writeFeedbackJobPointer(pointer, 'job-a');
  render(<MemoryRouter><I18nProvider><DistillPage searchParamsOverride={
    new URLSearchParams('skill_id=skill-a&agent_id=agent-a&feedback_candidate_id=candidate-a')
  } /></I18nProvider></MemoryRouter>);
  await waitFor(() => expect(screen.getByRole('textbox', { name: 'SOP名称' })).toHaveValue(card.name));
  await act(async () => rejectJob?.(new ApiError(status,
    JSON.stringify({ detail: 'FEEDBACK_CANDIDATE_UNAVAILABLE' }), 'unavailable')));
  await waitFor(() => expect(screen.queryByRole('textbox', { name: 'SOP名称' })).toBeNull());
  expect(window.sessionStorage.getItem(pointer)).toBeNull();
});

it('同一SSE缓冲中的失效后迟到complete不能复活候选或历史', async () => {
  const body = `event: error\ndata: {"message":"FEEDBACK_CANDIDATE_UNAVAILABLE"}\n\n`
    + `event: complete\ndata: {"draft_skill":${JSON.stringify(card)}}\n\n`;
  transport.mockResolvedValue(new Response(body, { status: 200,
    headers: { 'Content-Type': 'text/event-stream' } }));
  await mountThenResolveJob();
  await waitFor(() => expect(screen.queryByRole('textbox', { name: 'SOP名称' })).toBeNull());
  expect(window.sessionStorage.getItem(pointer)).toBeNull();
  expect(candidates.readSkillCandidate).toHaveBeenCalledTimes(1);
  expect(document.body.textContent).not.toContain('SYNTHETIC_PRIVATE_RESUME');
});

it('普通网络断开无status时合法候选和job定位仍可等待续接', async () => {
  /** 网络暂断不代表来源失效，合法已加载来源应保留以供原续接能力恢复。 */
  transport.mockRejectedValue(new TypeError('SYNTHETIC_TRANSPORT_DISCONNECTED'));
  await mountThenResolveJob();
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 30)); });
  const observed = { control: 'network_disconnect', title_preserved: screen.getByRole('textbox', { name: 'SOP名称' }).getAttribute('value') === card.name,
    material_preserved: Boolean(document.body.textContent?.includes('SYNTHETIC_PRIVATE_RESUME_MATERIAL')),
    pointer_preserved: window.sessionStorage.getItem(pointer) !== null };
  expect(observed).toEqual({ control: 'network_disconnect', title_preserved: true, material_preserved: true, pointer_preserved: true });
});

afterEach(() => { vi.unstubAllGlobals(); });

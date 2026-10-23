import { act, render as renderBase, screen, waitFor } from '@testing-library/react';
import type { ReactElement } from 'react';
import { MemoryRouter } from 'react-router-dom';
import userEvent from '@testing-library/user-event';
import { beforeEach, expect, it, vi } from 'vitest';

import { api } from '@/api/client';
import { I18nProvider } from '@/i18n';
import FeedbackMaterials, { FeedbackMaterialPreview } from './FeedbackMaterials';

function render(ui: ReactElement) {
  return renderBase(ui, { wrapper: ({ children }) =>
    <MemoryRouter><I18nProvider>{children}</I18nProvider></MemoryRouter> });
}

vi.mock('@/api/client', () => ({
  getRequestTenantId: () => 'tenant_demo',
  api: { get: vi.fn(), post: vi.fn() },
}));

const material = {
  id: 'material_a', feedback_id: 'feedback_a', feedback_revision: 1,
  target_agent_id: 'agent_a', status: 'preview', material_checksum: 'checksum_a',
  created_at: '2026-10-02T00:00:00Z', updated_at: '2026-10-02T00:00:00Z',
  material: {
    source: { status: 'verified', branch: 'sop', message_id: 'reply_a', turn_id: 'turn_a',
      execution_id: 'execution_a', skill_id: 'skill_a', skill_version_id: 'version_a',
      skill_version: '1', definition_checksum: 'definition_a', agent_id: 'agent_a',
      missing_reason: null },
    facts: { rating: 'down', user_request: '脱敏请求', assistant_reply: '脱敏回复', operations_known: true,
      operations: [{ id: 'operation_a', status: 'succeeded', tool_id: 'tool_a' }] },
    user_note: '需要完善结果', inference: { bucket: 'skill_issue', summary: '模型建议', reason: '模型归因' },
  },
};

beforeEach(() => {
  vi.mocked(api.get).mockReset();
  vi.mocked(api.post).mockReset();
  window.localStorage.clear();
});

it('本人先预览脱敏事实与推断，明确分享后才提交授权，并可撤回', async () => {
  const user = userEvent.setup();
  vi.mocked(api.post).mockResolvedValueOnce(material)
    .mockResolvedValueOnce({ ...material, status: 'shared' });
  const changed = vi.fn();
  const preview = render(<FeedbackMaterialPreview selection={{ feedbackId: 'feedback_a', targetAgentId: 'agent_a' }} onClose={vi.fn()} onChanged={changed} />);
  await user.type(screen.getByRole('textbox', { name: '用户说明' }), '需要完善结果');
  expect(api.post).not.toHaveBeenCalled();
  await user.click(screen.getByRole('button', { name: '生成脱敏预览' }));
  expect(await screen.findByText('脱敏回复')).toBeInTheDocument();
  expect(screen.getByText('确定执行事实')).toBeInTheDocument();
  expect(screen.getByText('模型归因（推断）')).toBeInTheDocument();
  expect(screen.getByText(/operation_a · succeeded · tool_a/)).toBeInTheDocument();
  expect(api.post).toHaveBeenCalledTimes(1);
  await user.click(screen.getByRole('button', { name: '确认分享给该员工管理者' }));
  await waitFor(() => expect(api.post).toHaveBeenCalledWith(
    '/api/enterprise/feedback/materials/material_a/share',
    { tenant_id: 'tenant_demo', expected_revision: 1, material_checksum: 'checksum_a' },
  ));
  expect(changed).toHaveBeenCalled();
  preview.unmount();
  vi.mocked(api.get).mockImplementation(async (path) => path.includes('mode=owned') ? [{ ...material, status: 'shared' }] : []);
  vi.mocked(api.post).mockResolvedValue({ ...material, status: 'withdrawn' });
  render(<FeedbackMaterials agentId="agent_a" />);
  await user.click(await screen.findByRole('button', { name: '撤回分享' }));
  await waitFor(() => expect(api.post).toHaveBeenCalledWith(
    '/api/enterprise/feedback/materials/material_a/withdraw',
    { tenant_id: 'tenant_demo', expected_revision: 1 },
  ));
});

it('管理者只加载共享材料与安全详情，scope 切换立即隐藏旧详情并忽略迟到响应', async () => {
  const user = userEvent.setup();
  let resolveOld: ((value: typeof material) => void) | undefined;
  vi.mocked(api.get).mockImplementation(async (path) => {
    if (path.includes('/materials/material_a?')) return new Promise<typeof material>((resolve) => { resolveOld = resolve; });
    return path.includes('agent_id=agent_a') && path.includes('mode=managed') ? [{ ...material, status: 'shared' }] : [];
  });
  const view = render(<FeedbackMaterials agentId="agent_a" />);
  await user.click(await screen.findByRole('button', { name: '查看共享材料' }));
  view.rerender(<FeedbackMaterials agentId="agent_b" />);
  await act(async () => resolveOld?.({ ...material, status: 'shared' }));
  expect(screen.queryByText('脱敏回复')).not.toBeInTheDocument();
  expect(api.get).not.toHaveBeenCalledWith(expect.stringContaining('/sessions/'));
  expect(window.localStorage.length).toBe(0);
});

it('已打开管理详情刷新拒绝后清除事实，不回显服务端异常或保留可操作旧材料', async () => {
  const user = userEvent.setup();
  vi.mocked(api.get).mockImplementation(async (path) => {
    if (path.includes('/materials/material_a?')) return { ...material, status: 'shared' };
    return path.includes('mode=managed') ? [{ ...material, status: 'shared' }] : [];
  });
  render(<FeedbackMaterials agentId="agent_a" />);
  await user.click(await screen.findByRole('button', { name: '查看共享材料' }));
  expect(await screen.findByText('脱敏回复')).toBeInTheDocument();
  vi.mocked(api.get).mockRejectedValue(new Error('409 secret-provider-payload'));
  await user.click(screen.getByRole('button', { name: '刷新材料详情' }));
  expect(await screen.findByText('材料已失效或权限已变化，请重新加载')).toBeInTheDocument();
  expect(screen.queryByText('脱敏回复')).not.toBeInTheDocument();
  expect(screen.queryByText(/secret-provider-payload/)).not.toBeInTheDocument();
});

it('没有员工范围时不请求全量材料，旧无来源明确缺失且不伪造执行失败', async () => {
  const user = userEvent.setup();
  const view = render(<FeedbackMaterials agentId="" />);
  expect(api.get).not.toHaveBeenCalled();
  view.unmount();
  vi.mocked(api.post).mockResolvedValue({ ...material, material: { ...material.material,
    source: { ...material.material.source, status: 'missing', execution_id: null, missing_reason: '历史反馈缺少可验证来源' },
    facts: { ...material.material.facts, operations_known: false, operations: [] },
  } });
  render(<FeedbackMaterialPreview selection={{ feedbackId: 'feedback_a', targetAgentId: 'agent_a' }} onClose={vi.fn()} onChanged={vi.fn()} />);
  await user.click(screen.getByRole('button', { name: '生成脱敏预览' }));
  expect(await screen.findByText('历史反馈缺少可验证来源')).toBeInTheDocument();
  expect(screen.getByText('缺少可验证工具调用记录')).toBeInTheDocument();
  expect(screen.queryByText('执行失败')).not.toBeInTheDocument();
});

it('预览分享冲突立即清空旧投影，必须重新预览', async () => {
  const user = userEvent.setup();
  vi.mocked(api.post).mockResolvedValueOnce(material).mockRejectedValueOnce(new Error('409 secret-old-fact'));
  render(<FeedbackMaterialPreview selection={{ feedbackId: 'feedback_a', targetAgentId: 'agent_a' }} onClose={vi.fn()} onChanged={vi.fn()} />);
  await user.click(screen.getByRole('button', { name: '生成脱敏预览' }));
  expect(await screen.findByText('脱敏回复')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '确认分享给该员工管理者' }));
  expect(await screen.findByText('材料已失效或权限已变化，请重新加载')).toBeInTheDocument();
  expect(screen.queryByText('脱敏回复')).not.toBeInTheDocument();
  expect(screen.queryByText(/secret-old-fact/)).not.toBeInTheDocument();
});

it('已证明的空回执明确截至该回复没有调用，已证明的列表保持原回执身份', async () => {
  const user = userEvent.setup();
  vi.mocked(api.post).mockResolvedValueOnce({ ...material, material: { ...material.material,
    facts: { ...material.material.facts, operations_known: true, operations: [] },
  } }).mockResolvedValueOnce(material);
  render(<FeedbackMaterialPreview selection={{ feedbackId: 'feedback_a', targetAgentId: 'agent_a' }} onClose={vi.fn()} onChanged={vi.fn()} />);
  await user.click(screen.getByRole('button', { name: '生成脱敏预览' }));
  expect(await screen.findByText('截至该回复无工具调用')).toBeInTheDocument();
  expect(screen.queryByText('缺少可验证工具调用记录')).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '生成脱敏预览' }));
  expect(await screen.findByText('截至该回复的执行回执')).toBeInTheDocument();
  expect(screen.getByText(/operation_a · succeeded · tool_a/)).toBeInTheDocument();
  expect(screen.queryByText('截至该回复无工具调用')).not.toBeInTheDocument();
});

it('修改说明使旧预览不可分享，晚到预览不能进入另一个反馈详情', async () => {
  const user = userEvent.setup();
  vi.mocked(api.post).mockResolvedValueOnce(material);
  const view = render(<FeedbackMaterialPreview selection={{ feedbackId: 'feedback_a', targetAgentId: 'agent_a' }} onClose={vi.fn()} onChanged={vi.fn()} />);
  await user.click(screen.getByRole('button', { name: '生成脱敏预览' }));
  expect(await screen.findByText('脱敏回复')).toBeInTheDocument();
  await user.type(screen.getByRole('textbox', { name: '用户说明' }), '新说明');
  expect(screen.queryByText('脱敏回复')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: '确认分享给该员工管理者' })).toBeDisabled();
  let resolvePreview: ((value: typeof material) => void) | undefined;
  vi.mocked(api.post).mockImplementation(() => new Promise<typeof material>((resolve) => { resolvePreview = resolve; }));
  await user.click(screen.getByRole('button', { name: '生成脱敏预览' }));
  view.rerender(<FeedbackMaterialPreview selection={{ feedbackId: 'feedback_b', targetAgentId: 'agent_b' }} onClose={vi.fn()} onChanged={vi.fn()} />);
  await act(async () => resolvePreview?.(material));
  expect(screen.queryByText('脱敏回复')).not.toBeInTheDocument();
  expect(screen.getByRole('textbox', { name: '用户说明' })).toHaveValue('');
});

/**
 * @Time       : 2026/10/04
 * @Author     : zhanglp8181
 * @File       : DynamicExecutionControl.f1-cancel.test.tsx
 * @CallChain  : SOP执行卡 → 原cancel命令API → 权威取消/未决效果状态
 * @Description: 两种活动SOP可取消，外部效果未决保持对账，终态不可再次取消。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, expect, it, vi } from 'vitest';

import { api } from '@/api/client';
import { I18nProvider } from '@/i18n';
import DynamicExecutionControl from './DynamicExecutionControl';

vi.mock('@/api/client', () => ({
  getRequestTenantId: () => 'tenant_f1',
  api: { get: vi.fn(), post: vi.fn(), blob: vi.fn() },
}));

beforeEach(() => {
  vi.mocked(api.get).mockReset();
  vi.mocked(api.post).mockReset();
});

it.each(['running', 'waiting'])('活动SOP %s 经原命令取消并保留外部效果未决', async (status) => {
  /** 只能按当前revision发cancel；重复点击不重复提交，SOP不获得DTA重规划能力。 */
  vi.mocked(api.get).mockResolvedValue({
    id: 'execution_sop_f1', session_id: 'session_f1', kind: 'sop', status, revision: 17,
    cancellation_disposition: 'none', effect_state: 'unknown',
  });
  vi.mocked(api.post).mockResolvedValue({
    command_id: 'cancel_f1', command_type: 'cancel', status: 'applied',
    result: { cancelled: false, execution_status: status },
  });
  render(<I18nProvider><DynamicExecutionControl executionId="execution_sop_f1" /></I18nProvider>);
  const user = userEvent.setup();
  const cancel = await screen.findByRole('button', { name: '取消任务' });
  expect(screen.queryByRole('button', { name: '运行中增加 Skill' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '追加约束' })).not.toBeInTheDocument();
  await user.click(cancel);
  await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
  expect(api.post).toHaveBeenCalledWith('/api/executions/execution_sop_f1/commands',
    expect.objectContaining({ tenant_id: 'tenant_f1', command_type: 'cancel', expected_revision: 17 }));
  expect(await screen.findByRole('status')).toHaveTextContent('取消已提交，等待效果对账');
  expect(screen.queryByText('任务已取消')).not.toBeInTheDocument();
  expect(cancel).toBeDisabled();
  fireEvent.click(cancel);
  expect(api.post).toHaveBeenCalledTimes(1);
});

it('重载SOP执行卡从原取消命令恢复未决对账且不可重复取消', async () => {
  /** applied并不等于远端无效果；latest_command必须维持取消屏障。 */
  vi.mocked(api.get).mockResolvedValue({
    id: 'execution_sop_f1', session_id: 'session_f1', kind: 'sop', status: 'waiting', revision: 18,
    cancellation_disposition: 'awaiting_reconciliation', effect_state: 'unknown',
    latest_command: { command_id: 'cancel_f1', command_type: 'cancel', status: 'applied',
      result: { cancelled: false, execution_status: 'waiting' } },
  });
  render(<I18nProvider><DynamicExecutionControl executionId="execution_sop_f1" /></I18nProvider>);
  expect(await screen.findByRole('status')).toHaveTextContent('取消已提交，等待效果对账');
  expect(screen.getByRole('button', { name: '取消任务' })).toBeDisabled();
  expect(api.post).not.toHaveBeenCalled();
});

it.each(['cancelled', 'succeeded', 'failed', 'archived'])('终态SOP %s 不显示取消入口', async (status) => {
  /** 终态取消按钮不能复活执行或产生新的命令。 */
  vi.mocked(api.get).mockResolvedValue({
    id: 'execution_sop_f1', session_id: 'session_f1', kind: 'sop', status, revision: 21,
  });
  render(<I18nProvider><DynamicExecutionControl executionId="execution_sop_f1" /></I18nProvider>);
  await screen.findByLabelText('SOP执行结果');
  expect(screen.queryByRole('button', { name: '取消任务' })).not.toBeInTheDocument();
  expect(api.post).not.toHaveBeenCalled();
});

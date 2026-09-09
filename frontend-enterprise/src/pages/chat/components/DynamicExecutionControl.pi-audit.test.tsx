import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

import { api } from '@/api/client';
import { I18nProvider } from '@/i18n';
import DynamicExecutionControl from './DynamicExecutionControl';

vi.mock('@/api/client', () => ({
  getRequestTenantId: () => 'tenant_audit',
  api: { get: vi.fn(), post: vi.fn(), blob: vi.fn() },
}));

beforeEach(() => {
  vi.mocked(api.get).mockReset();
  vi.mocked(api.post).mockReset();
});

afterEach(() => {
  cleanup();
  vi.clearAllTimers();
  vi.useRealTimers();
});

it('F02b：取消命令已应用但效果未决时不能显示任务已按新计划继续', async () => {
  vi.mocked(api.get).mockResolvedValue({
    id: 'execution_audit', session_id: 'session_audit', agent_id: 'agent_audit',
    kind: 'dynamic_task', status: 'running', revision: 2, goal: '核验取消事实',
    cancellation_disposition: 'awaiting_reconciliation', effect_state: 'unknown',
  });
  vi.mocked(api.post).mockResolvedValue({
    command_id: 'command_audit', command_type: 'cancel', status: 'applied',
    result: { cancelled: false, execution_status: 'running' },
  });
  const user = userEvent.setup();
  render(<I18nProvider><DynamicExecutionControl executionId="execution_audit" /></I18nProvider>);
  await user.click(await screen.findByRole('button', { name: '取消任务' }));
  expect(api.post).toHaveBeenCalledWith('/api/executions/execution_audit/commands', expect.objectContaining({
    tenant_id: 'tenant_audit', command_type: 'cancel', expected_revision: 2,
  }));
  const feedback = await screen.findByRole('status');
  expect(feedback).not.toHaveTextContent('约束已应用，任务已按新计划继续');
  expect(feedback).toHaveTextContent(/取消|对账/);
  expect(feedback).not.toHaveTextContent('已取消');
});

it('F02b：取消已应用后持续读取 Execution，直到权威终态并停止轮询', async () => {
  vi.useFakeTimers();
  const snapshots = [
    {
      id: 'execution_audit', session_id: 'session_audit', agent_id: 'agent_audit',
      kind: 'dynamic_task', status: 'running', revision: 2, goal: '核验取消事实',
    },
    {
      id: 'execution_audit', session_id: 'session_audit', agent_id: 'agent_audit',
      kind: 'dynamic_task', status: 'running', revision: 3, goal: '核验取消事实',
      cancellation_disposition: 'awaiting_reconciliation', effect_state: 'unknown',
    },
    {
      id: 'execution_audit', session_id: 'session_audit', agent_id: 'agent_audit',
      kind: 'dynamic_task', status: 'running', revision: 3, goal: '核验取消事实',
      cancellation_disposition: 'awaiting_reconciliation', effect_state: 'unknown',
    },
    {
      id: 'execution_audit', session_id: 'session_audit', agent_id: 'agent_audit',
      kind: 'dynamic_task', status: 'cancelled', revision: 4, goal: '核验取消事实',
      cancellation_disposition: 'cancelled', effect_state: 'none',
    },
  ];
  vi.mocked(api.get).mockImplementation(async () => snapshots.shift() || snapshots[snapshots.length - 1]!);
  vi.mocked(api.post).mockResolvedValue({
    command_id: 'command_audit', command_type: 'cancel', status: 'applied',
    result: { cancelled: false, execution_status: 'running' },
  });
  render(<I18nProvider><DynamicExecutionControl executionId="execution_audit" /></I18nProvider>);

  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
  fireEvent.click(screen.getByRole('button', { name: '取消任务' }));
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(screen.getByText('取消已提交，等待效果对账')).toBeInTheDocument();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1_500);
  });
  expect(screen.getByText('取消已提交，等待效果对账')).toBeInTheDocument();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1_500);
  });
  expect(screen.getByText('任务已取消')).toBeInTheDocument();

  const executionReads = vi.mocked(api.get).mock.calls.filter(([path]) => (
    path === '/api/executions/execution_audit?tenant_id=tenant_audit'
  )).length;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(3_000);
  });
  expect(vi.mocked(api.get).mock.calls.filter(([path]) => (
    path === '/api/executions/execution_audit?tenant_id=tenant_audit'
  ))).toHaveLength(executionReads);
});

it('F02b：取消请求尚未收口时重复点击不产生第二个取消命令', async () => {
  vi.mocked(api.get).mockResolvedValue({
    id: 'execution_audit', session_id: 'session_audit', agent_id: 'agent_audit',
    kind: 'dynamic_task', status: 'running', revision: 2, goal: '核验重复取消',
  });
  vi.mocked(api.post).mockImplementation(() => new Promise(() => {}));
  render(<I18nProvider><DynamicExecutionControl executionId="execution_audit" /></I18nProvider>);
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
  const cancelButton = screen.getByRole('button', { name: '取消任务' });
  fireEvent.click(cancelButton);
  fireEvent.click(cancelButton);
  expect(api.post).toHaveBeenCalledTimes(1);
});

it('F02b：重新进入执行卡恢复服务端最新取消命令并继续对账', async () => {
  vi.mocked(api.get).mockResolvedValue({
    id: 'execution_audit', session_id: 'session_audit', agent_id: 'agent_audit',
    kind: 'dynamic_task', status: 'running', revision: 3, goal: '恢复取消对账',
    cancellation_disposition: 'awaiting_reconciliation', effect_state: 'unknown',
    latest_command: {
      command_id: 'command_latest', command_type: 'cancel', status: 'applied',
      result: { cancelled: false, execution_status: 'running' }, issued_at: new Date().toISOString(),
    },
  });
  render(<I18nProvider><DynamicExecutionControl executionId="execution_audit" /></I18nProvider>);
  expect(await screen.findByText('取消已提交，等待效果对账')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '取消任务' })).toBeDisabled();
});

it('F02b：执行卡轮询 single-flight，旧命令快照不能回退当前状态', async () => {
  vi.useFakeTimers();
  let resolvePoll: ((value: unknown) => void) | undefined;
  const pendingPoll = new Promise((resolve) => {
    resolvePoll = resolve;
  });
  vi.mocked(api.get).mockImplementation(async (path) => {
    if (path.includes('/commands/')) return pendingPoll;
    return {
      id: 'execution_audit', session_id: 'session_audit', agent_id: 'agent_audit',
      kind: 'dynamic_task', status: 'running', revision: 3, goal: '防止旧命令覆盖',
      latest_command: {
        command_id: 'command_latest', command_type: 'cancel', status: 'claimed',
        issued_at: '2026-09-06T00:00:01.000Z', updated_at: '2026-09-06T00:00:02.000Z',
      },
    };
  });
  render(<I18nProvider><DynamicExecutionControl executionId="execution_audit" /></I18nProvider>);
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });

  await act(async () => {
    await vi.advanceTimersByTimeAsync(1_500);
    await vi.advanceTimersByTimeAsync(3_000);
  });
  const commandReads = vi.mocked(api.get).mock.calls.filter(([path]) => (
    path.includes('/commands/')
  ));
  expect(commandReads).toHaveLength(1);
  expect(screen.getByText('取消命令等待执行')).toBeInTheDocument();
  resolvePoll?.({
    command_id: 'command_latest', command_type: 'cancel', status: 'pending',
    issued_at: '2026-09-06T00:00:01.000Z', updated_at: '2026-09-06T00:00:01.500Z',
  });
  await act(async () => {
    await pendingPoll;
    await Promise.resolve();
  });
  expect(screen.getByText('取消命令等待执行')).toBeInTheDocument();
});

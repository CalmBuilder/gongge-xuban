import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, expect, it, vi } from 'vitest';

import {
  createStandingApprovalRule,
  listStandingApprovalCandidates,
  listStandingApprovalRules,
  revokeStandingApprovalRule,
} from '@/api/standing-approvals';
import type { ScheduledTaskRead } from '@/types';
import { I18nProvider } from '@/i18n';
import { StandingApprovalDialog } from './StandingApprovalDialog';

vi.mock('@/api/standing-approvals', () => ({
  createStandingApprovalRule: vi.fn(),
  listStandingApprovalCandidates: vi.fn(),
  listStandingApprovalRules: vi.fn(),
  revokeStandingApprovalRule: vi.fn(),
}));

const task: ScheduledTaskRead = {
  id: 'schedule-1',
  tenant_id: 'tenant-1',
  agent_id: 'agent-1',
  created_by_user_id: 'manager-1',
  title: '固定日报',
  prompt: '发送固定日报',
  schedule_type: 'daily',
  schedule: { time: '09:00' },
  timezone: 'Asia/Shanghai',
  status: 'active',
  concurrency_policy: 'forbid',
  misfire_policy: 'coalesce',
  run_count: 0,
  metadata: {},
  created_at: '2026-08-10T00:00:00Z',
  updated_at: '2026-08-10T00:00:00Z',
};

const candidate = {
  thread_binding_id: 'thread-1',
  profile_id: 'profile-1',
  profile_display_name: '序伴测试应用',
  target_label: '序伴测试应用 · 张丽鹏 的企业微信会话',
  tool_snapshot_checksum: 'tool-checksum',
  target_hash: 'target-hash',
};

const rule = {
  id: 'rule-1',
  tenant_id: 'tenant-1',
  agent_id: 'agent-1',
  source_schedule_id: task.id,
  source_schedule_checksum: 'schedule-checksum',
  profile_id: candidate.profile_id,
  binding_id: 'binding-1',
  tool_id: 'wecom.message_send@profile-1',
  tool_snapshot_checksum: candidate.tool_snapshot_checksum,
  risk_class: 'external_write',
  target_type: 'wecom_thread',
  canonical_target: 'wecom_thread:thread-1',
  target_hash: candidate.target_hash,
  argument_constraints: { content: { equals: '发送固定日报' } },
  valid_from: '2026-08-10T00:00:00Z',
  valid_to: '2026-08-17T00:00:00Z',
  status: 'active' as const,
  revision: 1,
  created_by_user_id: 'manager-1',
  created_at: '2026-08-10T00:00:00Z',
  updated_at: '2026-08-10T00:00:00Z',
};

function renderDialog() {
  /** 使用应用真实多语言上下文渲染弹窗。 */

  return render(
    <I18nProvider>
      <StandingApprovalDialog task={task} open onOpenChange={vi.fn()} />
    </I18nProvider>,
  );
}

beforeEach(() => {
  vi.mocked(listStandingApprovalCandidates).mockReset();
  vi.mocked(listStandingApprovalRules).mockReset();
  vi.mocked(createStandingApprovalRule).mockReset();
  vi.mocked(revokeStandingApprovalRule).mockReset();
  vi.mocked(listStandingApprovalCandidates).mockResolvedValue([candidate]);
  vi.mocked(listStandingApprovalRules).mockResolvedValue([]);
  vi.mocked(createStandingApprovalRule).mockResolvedValue(rule);
  vi.mocked(revokeStandingApprovalRule).mockResolvedValue({ ...rule, status: 'revoked', revision: 2 });
});

it('只有确认任务、目标和精确正文三重边界后才能创建长期批准', async () => {
  const user = userEvent.setup();
  renderDialog();

  expect(await screen.findByText(candidate.target_label)).toBeInTheDocument();
  expect(screen.getByLabelText('允许发送的精确正文')).toHaveValue(task.prompt);
  const enable = screen.getByRole('button', { name: '启用长期批准' });
  expect(enable).toBeDisabled();
  await user.click(screen.getByRole('checkbox'));
  expect(enable).toBeEnabled();
  await user.click(enable);

  await waitFor(() => expect(createStandingApprovalRule).toHaveBeenCalledWith({
    scheduleId: task.id,
    agentId: task.agent_id,
    candidate,
    exactContent: task.prompt,
    validDays: 7,
  }));
});

it('撤销活动规则前二次确认，且说明后续退回一次性审批', async () => {
  const user = userEvent.setup();
  vi.mocked(listStandingApprovalRules).mockResolvedValue([rule]);
  renderDialog();

  await user.click(await screen.findByRole('button', { name: '撤销' }));
  expect(screen.getByText('后续任务若仍需发送，将回到一次性人工审批。')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '确认撤销' }));

  await waitFor(() => expect(revokeStandingApprovalRule).toHaveBeenCalledWith(rule));
});

it('没有可授权会话时给出可执行的补齐路径', async () => {
  vi.mocked(listStandingApprovalCandidates).mockResolvedValue([]);
  renderDialog();

  expect(await screen.findByText(/先让该数字员工接收一条企业微信消息/)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '启用长期批准' })).toBeDisabled();
});

/**
 * @Time       : 2026/07/22 17:43
 * @Author     : zhanglp8181
 * @File       : WorkItemsPage.test.tsx
 * @CallChain  : Vitest → WorkItemsPage → 工作项动作反馈契约
 * @Description: 验证任务箱候选快照、领域办理动作和成功反馈不会被误报。
 */

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, expect, it, vi } from 'vitest';

import { api } from '../api/client';
import { I18nProvider } from '../i18n';
import WorkItemsPage, { actionLabel } from './WorkItemsPage';

vi.mock('../api/client', () => ({
  getRequestTenantId: () => 'tenant_demo',
  api: { get: vi.fn(), post: vi.fn() },
}));

const pendingWorkItem = {
  id: 'work-item-001',
  instance_id: 'instance-001',
  session_id: 'session-001',
  skill_id: 'seal_application',
  skill_version: '1.0.0',
  node_id: 'manager_review',
  status: 'offered',
  initiator_user_id: 'user_demo',
  completion_mode: 'single',
  claim_required: true,
  allowed_outcomes: ['approved', 'rejected'],
  outcome_options: [
    { value: 'approved', label: '同意', tone: 'success', comment_required: false },
    { value: 'rejected', label: '拒绝', tone: 'danger', comment_required: false },
  ],
  allowed_actions: ['claim'],
  revision: 1,
  candidate_count: 1,
  decision_count: 0,
  candidates: [{
    user_id: 'manager_demo',
    employee_profile_id: 'employee-manager',
    source_role_codes: ['department_manager'],
    source_types: ['business_role'],
  }],
  decisions: [],
  created_at: '2026-07-22T08:00:00Z',
  updated_at: '2026-07-22T08:00:00Z',
};

beforeEach(() => {
  vi.mocked(api.get).mockReset();
  vi.mocked(api.post).mockReset();
  vi.mocked(api.get).mockResolvedValue({
    items: [pendingWorkItem],
    total: 1,
    page: 1,
    page_size: 20,
  });
});

it('任务箱按服务端结果契约渲染非审批办理动作并校验必填说明', async () => {
  const engineerItem = {
    ...pendingWorkItem,
    status: 'claimed',
    assignee_user_id: 'engineer_demo',
    allowed_outcomes: ['resolved', 'escalated'],
    allowed_actions: ['unclaim', 'resolved', 'escalated'],
    outcome_options: [
      { value: 'resolved', label: '标记已解决', tone: 'success', comment_required: true },
      { value: 'escalated', label: '升级处理', tone: 'danger', comment_required: true },
    ],
  };
  vi.mocked(api.get).mockResolvedValue({
    items: [engineerItem],
    total: 1,
    page: 1,
    page_size: 20,
  });

  render(
    <I18nProvider>
      <MemoryRouter>
        <WorkItemsPage />
      </MemoryRouter>
    </I18nProvider>,
  );

  await screen.findByText('seal_application');
  await userEvent.click(screen.getByRole('button', { name: '查看' }));
  expect(screen.getByRole('button', { name: '标记已解决' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '升级处理' })).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '同意' })).not.toBeInTheDocument();
  expect(screen.getByPlaceholderText('请填写本次处理结果和依据')).toBeInTheDocument();

  await userEvent.click(screen.getByRole('button', { name: '标记已解决' }));
  expect(api.post).not.toHaveBeenCalled();
});

it('任务箱只根据服务端动作显示认领入口并保留候选快照', async () => {
  render(
    <I18nProvider>
      <MemoryRouter>
        <WorkItemsPage />
      </MemoryRouter>
    </I18nProvider>,
  );

  expect(await screen.findByText('seal_application')).toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: '查看' }));
  expect(screen.getByRole('button', { name: '认领任务' })).toBeInTheDocument();
  expect(screen.getByText('department_manager')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '同意' })).not.toBeInTheDocument();
  expect(screen.getByText('平台管理员不会自动进入候选池；角色变更不改写已经创建的历史任务。')).toBeInTheDocument();
});

it('任务箱对服务端声明的新增领域结果生成成功反馈而不是失败提示', () => {
  expect(actionLabel('reviewed', true, '提交复核意见')).toBe('提交复核意见成功');
  expect(actionLabel('needs_information', false, '要求补充资料')).toBe('要求补充资料失败');
  expect(actionLabel('future_outcome', true)).toBe('工作项操作成功');
});

it('任务箱明确区分可认领、待我处理和已办视图', async () => {
  const user = userEvent.setup();
  vi.mocked(api.get).mockResolvedValue({ items: [], total: 0, page: 1, page_size: 20 });
  render(
    <I18nProvider>
      <MemoryRouter>
        <WorkItemsPage />
      </MemoryRouter>
    </I18nProvider>,
  );

  expect(await screen.findByRole('tab', { name: '可认领' })).toHaveAttribute('aria-selected', 'true');
  expect(screen.getByText('当前没有可认领任务')).toBeInTheDocument();

  await user.click(screen.getByRole('tab', { name: '待我处理' }));
  await waitFor(() => expect(api.get).toHaveBeenLastCalledWith(
    '/api/work-items/page?tenant_id=tenant_demo&view=claimed&page=1&page_size=20',
  ));
  expect(screen.getByText('当前没有待我处理任务')).toBeInTheDocument();

  await user.click(screen.getByRole('tab', { name: '已办' }));
  await waitFor(() => expect(api.get).toHaveBeenLastCalledWith(
    '/api/work-items/page?tenant_id=tenant_demo&view=completed&page=1&page_size=20',
  ));
  expect(screen.getByText('暂无已办任务')).toBeInTheDocument();
});

it('任务箱切页时只请求对应的服务端页', async () => {
  const user = userEvent.setup();
  vi.mocked(api.get).mockImplementation(async (path: string) => ({
    items: [{ ...pendingWorkItem, id: path.includes('page=2') ? 'work-item-021' : 'work-item-001' }],
    total: 21,
    page: path.includes('page=2') ? 2 : 1,
    page_size: 20,
  }));

  render(
    <I18nProvider>
      <MemoryRouter>
        <WorkItemsPage />
      </MemoryRouter>
    </I18nProvider>,
  );

  await screen.findByText('seal_application');
  await user.click(screen.getByRole('button', { name: '下一页' }));

  await waitFor(() => expect(api.get).toHaveBeenLastCalledWith(
    '/api/work-items/page?tenant_id=tenant_demo&view=pending&page=2&page_size=20',
  ));
  expect(screen.getByRole('button', { name: '上一页' })).toBeEnabled();
});

import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, expect, it, vi } from 'vitest';

import { api } from '../../api/client';
import { I18nProvider } from '../../i18n';
import ScheduledTasksTab from './ScheduledTasksTab';

vi.mock('../../api/client', () => ({
  getRequestTenantId: () => 'tenant_demo',
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

const task = {
  id: 'task_demo',
  tenant_id: 'tenant_demo',
  agent_id: 'agent_demo',
  created_by_user_id: 'user_demo',
  title: '日报任务',
  prompt: '生成日报',
  schedule_type: 'daily',
  schedule: {},
  timezone: 'Asia/Shanghai',
  status: 'active',
  concurrency_policy: 'forbid',
  misfire_policy: 'coalesce',
  run_count: 25,
  metadata: {},
  created_at: '2026-08-01T10:00:00Z',
  updated_at: '2026-08-01T10:00:00Z',
};

const run = {
  id: 'run_demo',
  tenant_id: 'tenant_demo',
  scheduled_task_id: task.id,
  task_title: task.title,
  task_status: task.status,
  agent_id: task.agent_id,
  user_id: 'user_demo',
  execution_id: 'execution_demo',
  source_kind: 'schedule',
  source_ref: 'run_demo',
  source_checksum: 'checksum_demo',
  scheduled_for: '2026-08-01T10:00:00Z',
  status: 'succeeded',
  result_summary: '日报已生成',
  trace: {},
  created_at: '2026-08-01T10:00:00Z',
  updated_at: '2026-08-01T10:00:00Z',
};

beforeEach(() => {
  window.localStorage.clear();
  window.localStorage.setItem('gongge_enterprise_agent_scope', 'agent_demo');
  vi.mocked(api.get).mockReset();
  vi.mocked(api.get).mockImplementation(async (path: string) => {
    if (path.startsWith('/api/enterprise/agents?')) {
      return [{ id: 'agent_demo', name: '日报员工', is_overall: false }];
    }
    if (path.startsWith('/api/enterprise/scheduled-tasks/page?')) {
      return {
        items: [task],
        total: 11,
        status_counts: { active: 7, completed: 3, paused: 1 },
        page: path.includes('page=2') ? 2 : 1,
        page_size: 10,
      };
    }
    if (path.startsWith('/api/enterprise/scheduled-tasks/runs/page?')) {
      return {
        items: [run],
        total: 11,
        run_total: path.includes('task_id=') ? 11 : 25,
        page: path.includes('page=2') ? 2 : 1,
        page_size: 10,
      };
    }
    throw new Error(`unexpected request: ${path}`);
  });
});

it('等待人工处理的动态执行保留在待完成筛选并展示会话入口', async () => {
  // 调度运行进入动态任务等待态后，不应被误报为成功或从待完成列表消失。

  vi.mocked(api.get).mockImplementation(async (path: string) => {
    if (path.startsWith('/api/enterprise/agents?')) {
      return [{ id: 'agent_demo', name: '日报员工', is_overall: false }];
    }
    if (path.startsWith('/api/enterprise/scheduled-tasks/page?')) {
      return { items: [task], total: 1, status_counts: { active: 1 }, page: 1, page_size: 10 };
    }
    if (path.startsWith('/api/enterprise/scheduled-tasks/runs/page?')) {
      return {
        items: [{ ...run, session_id: 'session_waiting', status: 'waiting', result_summary: undefined }],
        total: 1,
        run_total: 1,
        page: 1,
        page_size: 10,
      };
    }
    throw new Error(`unexpected request: ${path}`);
  });

  const user = userEvent.setup();
  render(
    <I18nProvider>
      <MemoryRouter>
        <ScheduledTasksTab />
      </MemoryRouter>
    </I18nProvider>,
  );

  const runSection = await screen.findByRole('region', { name: '执行记录' });
  expect(within(runSection).getAllByText('等待处理').length).toBeGreaterThan(0);
  await user.click(within(runSection).getByRole('tab', { name: '待完成' }));
  await waitFor(() => expect(api.get).toHaveBeenCalledWith(
    '/api/enterprise/scheduled-tasks/runs/page?tenant_id=tenant_demo&agent_id=agent_demo&status_filter=pending&page=1&page_size=10',
  ));
  expect(within(runSection).getAllByRole('button', { name: '查看会话' })[0]).toBeEnabled();
});

it('执行记录主列表、状态筛选和任务弹窗均使用服务端分页', async () => {
  const user = userEvent.setup();
  render(
    <I18nProvider>
      <MemoryRouter>
        <ScheduledTasksTab />
      </MemoryRouter>
    </I18nProvider>,
  );

  expect((await screen.findAllByText('日报已生成')).length).toBeGreaterThan(0);
  expect(api.get).toHaveBeenCalledWith(
    '/api/enterprise/scheduled-tasks/page?tenant_id=tenant_demo&agent_id=agent_demo&status_filter=all&page=1&page_size=10',
  );
  expect(api.get).toHaveBeenCalledWith(
    '/api/enterprise/scheduled-tasks/runs/page?tenant_id=tenant_demo&agent_id=agent_demo&status_filter=all&page=1&page_size=10',
  );
  expect(api.get).not.toHaveBeenCalledWith(expect.stringContaining('limit=200'));
  expect(api.get).not.toHaveBeenCalledWith(
    expect.stringMatching(/^\/api\/enterprise\/scheduled-tasks\?/),
  );

  const taskSection = screen.getByRole('region', { name: '任务列表' });
  await user.click(within(taskSection).getByRole('button', { name: '下一页' }));
  await waitFor(() => expect(api.get).toHaveBeenCalledWith(
    '/api/enterprise/scheduled-tasks/page?tenant_id=tenant_demo&agent_id=agent_demo&status_filter=all&page=2&page_size=10',
  ));
  await user.click(within(taskSection).getByRole('tab', { name: '已完成' }));
  await waitFor(() => expect(api.get).toHaveBeenCalledWith(
    '/api/enterprise/scheduled-tasks/page?tenant_id=tenant_demo&agent_id=agent_demo&status_filter=completed&page=1&page_size=10',
  ));

  const runSection = screen.getByRole('region', { name: '执行记录' });
  await user.click(within(runSection).getByRole('button', { name: '下一页' }));
  await waitFor(() => expect(api.get).toHaveBeenCalledWith(
    '/api/enterprise/scheduled-tasks/runs/page?tenant_id=tenant_demo&agent_id=agent_demo&status_filter=all&page=2&page_size=10',
  ));

  await user.click(within(runSection).getByRole('tab', { name: '失败/跳过' }));
  await waitFor(() => expect(api.get).toHaveBeenCalledWith(
    '/api/enterprise/scheduled-tasks/runs/page?tenant_id=tenant_demo&agent_id=agent_demo&status_filter=failed&page=1&page_size=10',
  ));

  await user.click(screen.getAllByRole('button', { name: '操作' })[0]);
  await user.click(await screen.findByRole('menuitem', { name: '查看记录' }));
  await waitFor(() => expect(api.get).toHaveBeenCalledWith(
    '/api/enterprise/scheduled-tasks/runs/page?tenant_id=tenant_demo&task_id=task_demo&page=1&page_size=10',
  ));
  const dialog = screen.getByRole('dialog');
  await user.click(within(dialog).getByRole('button', { name: '下一页' }));
  await waitFor(() => expect(api.get).toHaveBeenCalledWith(
    '/api/enterprise/scheduled-tasks/runs/page?tenant_id=tenant_demo&task_id=task_demo&page=2&page_size=10',
  ));
});

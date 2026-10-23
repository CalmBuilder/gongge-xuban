import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, expect, it, vi } from 'vitest';

import { api } from '../../api/client';
import { I18nProvider } from '../../i18n';
import MemoriesTab from './MemoriesTab';

vi.mock('../../api/client', () => ({
  getRequestTenantId: () => 'tenant_demo',
  api: { get: vi.fn(), delete: vi.fn() },
}));

const memory = {
  id: 'mem_001',
  tenant_id: 'tenant_demo',
  user_id: 'user_demo',
  username: 'user_demo',
  kind: 'profile',
  content: '用户偏好简洁回复',
  importance: 0.9,
  metadata: { agent_id: 'agent_a' },
  created_at: '2026-08-01T10:00:00Z',
  updated_at: '2026-08-01T10:00:00Z',
};

beforeEach(() => {
  window.localStorage.clear();
  window.localStorage.setItem('gongge_enterprise_agent_scope', 'agent_a');
  vi.mocked(api.get).mockReset();
  vi.mocked(api.delete).mockReset();
  vi.mocked(api.get).mockImplementation(async (path: string) => {
    if (path.startsWith('/api/enterprise/memories/jobs?')) {
      return {
        items: [
          {
            job_id: 'memjob_status_001',
            status: 'running',
            created_at: '2026-10-01T10:00:00Z',
            finished_at: null,
            attempt_count: 1,
            max_attempts: 3,
            model_dispatch_count: 1,
            applied_count: 0,
            discarded_count: 0,
          },
        ],
        total: 1,
        page: 1,
        page_size: 20,
      };
    }
    if (!path.startsWith('/api/enterprise/memories/page?')) {
      throw new Error(`unexpected request: ${path}`);
    }
    return {
      items: [memory],
      total: 11,
      page: path.includes('page=2') ? 2 : 1,
      page_size: 10,
    };
  });
});

it('记忆页显示当前 Agent 的提取状态并保持独立查询', async () => {
  render(
    <I18nProvider>
      <MemoriesTab />
    </I18nProvider>,
  );

  expect((await screen.findAllByText('记忆提取中')).length).toBeGreaterThan(0);
  expect(api.get).toHaveBeenCalledWith(
    '/api/enterprise/memories/jobs?tenant_id=tenant_demo&agent_id=agent_a&page=1&page_size=20',
  );
  expect(screen.getByLabelText('记忆提取状态')).toHaveTextContent('实际模型派发 1 次');
});

it('提取面板显示安全原因、尝试数和重试时间，不呈现服务端原始错误', async () => {
  vi.mocked(api.get).mockImplementation(async (path: string) => path.includes('/jobs?') ? {
    items: [{ job_id: 'retry_job', status: 'retry', created_at: '2026-10-01T10:00:00Z',
      attempt_count: 2, max_attempts: 3, model_dispatch_count: 3, applied_count: 0,
      discarded_count: 0, reason_code: 'retry_scheduled', next_attempt_at: '2026-10-01T10:01:00Z',
      last_error: 'synthetic-private-provider-error' },
    { job_id: 'discarded_job', status: 'discarded', created_at: '2026-10-01T10:00:00Z',
      attempt_count: 1, max_attempts: 3, model_dispatch_count: 0, applied_count: 0,
      discarded_count: 0, reason_code: 'capture_contract_changed' }],
    total: 2, page: 1, page_size: 20,
  } : { items: [], total: 0, page: 1, page_size: 10 });
  render(<I18nProvider><MemoriesTab /></I18nProvider>);
  expect(await screen.findByText('临时提取失败，将自动重试')).toBeInTheDocument();
  expect(screen.getByText('已尝试 2/3 次')).toBeInTheDocument();
  expect(screen.getByText(/^下次重试：/)).toBeInTheDocument();
  expect(screen.getByText('来源、权限或模型已变更；请在当前权限下重新发送新输入')).toBeInTheDocument();
  expect(screen.queryByText('synthetic-private-provider-error')).not.toBeInTheDocument();
});

it('切换 Agent 后不接受旧提取状态响应', async () => {
  let resolveAgentA: ((value: unknown) => void) | undefined;
  vi.mocked(api.get).mockImplementation((path: string) => {
    if (path.includes('agent_id=agent_a') && path.includes('/jobs?')) {
      return new Promise((resolve) => {
        resolveAgentA = resolve;
      });
    }
    if (path.includes('/jobs?')) {
      return Promise.resolve({
        items: [
          {
            job_id: 'memjob_agent_b',
            status: 'succeeded',
            created_at: '2026-10-01T10:00:00Z',
            finished_at: '2026-10-01T10:01:00Z',
            attempt_count: 1,
            max_attempts: 3,
            model_dispatch_count: 1,
            applied_count: 1,
            discarded_count: 0,
          },
        ],
        total: 1,
        page: 1,
        page_size: 20,
      });
    }
    return Promise.resolve({ items: [memory], total: 1, page: 1, page_size: 10 });
  });

  render(
    <I18nProvider>
      <MemoriesTab />
    </I18nProvider>,
  );
  await waitFor(() => expect(api.get).toHaveBeenCalledWith(
    '/api/enterprise/memories/jobs?tenant_id=tenant_demo&agent_id=agent_a&page=1&page_size=20',
  ));

  window.dispatchEvent(
    new CustomEvent('gongge-enterprise-agent-scope-change', { detail: { agentId: 'agent_b' } }),
  );
  expect((await screen.findAllByText('提取完成')).length).toBeGreaterThan(0);

  resolveAgentA?.({
    items: [
      {
        job_id: 'memjob_old_agent_a',
        status: 'running',
        created_at: '2026-10-01T10:00:00Z',
        finished_at: null,
        attempt_count: 1,
        max_attempts: 3,
        model_dispatch_count: 1,
        applied_count: 0,
        discarded_count: 0,
      },
    ],
    total: 1,
    page: 1,
    page_size: 20,
  });
  await waitFor(() => expect(screen.queryByText('记忆提取中')).not.toBeInTheDocument());
});

it('员工记忆切页和搜索只请求服务端用户分页接口', async () => {
  const user = userEvent.setup();
  render(
    <I18nProvider>
      <MemoriesTab />
    </I18nProvider>,
  );

  expect((await screen.findAllByText('用户偏好简洁回复')).length).toBeGreaterThan(0);
  expect(api.get).toHaveBeenCalledWith(
    '/api/enterprise/memories/page?tenant_id=tenant_demo&agent_id=agent_a&page=1&page_size=10',
  );
  expect(api.get).not.toHaveBeenCalledWith(expect.stringContaining('limit=500'));

  await user.click(screen.getByRole('button', { name: '下一页' }));
  await waitFor(() => expect(api.get).toHaveBeenCalledWith(
    '/api/enterprise/memories/page?tenant_id=tenant_demo&agent_id=agent_a&page=2&page_size=10',
  ));

  await user.type(screen.getByPlaceholderText('用户名、用户 ID、记忆内容'), '简洁');
  await user.click(screen.getByRole('button', { name: '查询' }));
  await waitFor(() => expect(api.get).toHaveBeenCalledWith(
    '/api/enterprise/memories/page?tenant_id=tenant_demo&agent_id=agent_a&q=%E7%AE%80%E6%B4%81&page=1&page_size=10',
  ));
});

it('memory scope changes reset and clear the current agent', async () => {
  const user = userEvent.setup();
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
  vi.mocked(api.get).mockImplementation(async (path: string) => {
    const isAgentB = path.includes('agent_id=agent_b');
    return {
      items: [
        {
          ...memory,
          id: isAgentB ? 'mem_agent_b' : memory.id,
          content: isAgentB ? 'agent B scoped memory' : memory.content,
          metadata: { agent_id: isAgentB ? 'agent_b' : 'agent_a' },
        },
      ],
      total: 1,
      page: 1,
      page_size: 10,
    };
  });
  vi.mocked(api.delete).mockResolvedValue({ deleted: 1 });

  render(
    <I18nProvider>
      <MemoriesTab />
    </I18nProvider>,
  );

  expect((await screen.findAllByText('用户偏好简洁回复')).length).toBeGreaterThan(0);
  await user.type(screen.getByPlaceholderText('用户名、用户 ID、记忆内容'), '简洁');
  await user.click(screen.getByRole('button', { name: '查询' }));
  await waitFor(() => expect(api.get).toHaveBeenCalledWith(
    '/api/enterprise/memories/page?tenant_id=tenant_demo&agent_id=agent_a&q=%E7%AE%80%E6%B4%81&page=1&page_size=10',
  ));

  await user.click(screen.getByRole('button', { name: '重置' }));
  await waitFor(() => expect(api.get).toHaveBeenCalledWith(
    '/api/enterprise/memories/page?tenant_id=tenant_demo&agent_id=agent_a&page=1&page_size=10',
  ));

  window.localStorage.setItem('gongge_enterprise_agent_scope', 'agent_b');
  window.dispatchEvent(
    new CustomEvent('gongge-enterprise-agent-scope-change', { detail: { agentId: 'agent_b' } }),
  );
  await waitFor(() => expect(api.get).toHaveBeenCalledWith(
    '/api/enterprise/memories/page?tenant_id=tenant_demo&agent_id=agent_b&page=1&page_size=10',
  ));
  expect((await screen.findAllByText('agent B scoped memory')).length).toBeGreaterThan(0);

  await user.click(screen.getByRole('button', { name: '清空我的记忆' }));
  await waitFor(() => expect(api.delete).toHaveBeenCalledWith(
    '/api/enterprise/memories/me?tenant_id=tenant_demo&agent_id=agent_b',
  ));
  await waitFor(() => expect(api.get).toHaveBeenCalledWith(
    '/api/enterprise/memories/page?tenant_id=tenant_demo&agent_id=agent_b&page=1&page_size=10',
  ));
  confirm.mockRestore();
});


it.each(['pending', 'failure', 'return'])(
  'scope switch immediately hides displayed jobs, rows and detail: %s', async (phase) => {
    let settleB: ((value: unknown) => void) | undefined;
    let rejectB: ((reason: Error) => void) | undefined;
    vi.mocked(api.get).mockImplementation((path: string) => {
      if (path.includes('agent_id=agent_b')) {
        if (path.includes('/jobs?')) return new Promise((resolve, reject) => {
          settleB = resolve;
          rejectB = reject;
        });
        return Promise.resolve({ items: [], total: 0, page: 1, page_size: 10 });
      }
      if (path.includes('/jobs?')) return Promise.resolve({
        items: [{ job_id: 'job_a', status: 'running', created_at: '2026-10-01T10:00:00Z',
          attempt_count: 1, max_attempts: 3, model_dispatch_count: 1, applied_count: 0, discarded_count: 0 }],
        total: 1, page: 1, page_size: 20,
      });
      return Promise.resolve({ items: [memory], total: 1, page: 1, page_size: 10 });
    });
    render(<I18nProvider><MemoriesTab /></I18nProvider>);
    expect(await screen.findByText('记忆提取中')).toBeInTheDocument();
    expect((await screen.findAllByText(memory.content)).length).toBeGreaterThan(0);
    await userEvent.click(screen.getAllByRole('button', { name: '查看' })[0]);
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    await act(async () => window.dispatchEvent(new CustomEvent(
      'gongge-enterprise-agent-scope-change', { detail: { agentId: 'agent_b' } },
    )));
    expect(screen.queryByText('记忆提取中')).not.toBeInTheDocument();
    expect(screen.queryByText(memory.content)).not.toBeInTheDocument();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    if (phase === 'failure') {
      await act(async () => rejectB?.(new Error('synthetic status failure')));
      expect(screen.queryByText('记忆提取中')).not.toBeInTheDocument();
    } else if (phase === 'return') {
      await act(async () => window.dispatchEvent(new CustomEvent(
        'gongge-enterprise-agent-scope-change', { detail: { agentId: 'agent_a' } },
      )));
      expect(await screen.findByText('记忆提取中')).toBeInTheDocument();
      await act(async () => settleB?.({ items: [], total: 0, page: 1, page_size: 20 }));
      expect(screen.getByText('记忆提取中')).toBeInTheDocument();
    } else {
      expect(screen.getByText('正在查询提取任务')).toBeInTheDocument();
    }
  },
);

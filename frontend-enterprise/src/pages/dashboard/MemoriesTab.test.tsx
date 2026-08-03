import { render, screen, waitFor } from '@testing-library/react';
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

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, expect, it, vi } from 'vitest';

import { api } from '../../api/client';
import { I18nProvider } from '../../i18n';
import ConversationLogsTab from './ConversationLogsTab';

vi.mock('../../api/client', () => ({
  getRequestTenantId: () => 'tenant_demo',
  api: { get: vi.fn(), post: vi.fn() },
}));

const session = {
  id: 'session_001',
  tenant_id: 'tenant_demo',
  user_id: 'user_demo',
  agent_id: 'agent_a',
  title: '分页会话',
  status: 'active',
  created_at: '2026-08-01T10:00:00Z',
  updated_at: '2026-08-01T10:00:00Z',
};

beforeEach(() => {
  window.localStorage.clear();
  vi.mocked(api.get).mockReset();
  vi.mocked(api.post).mockReset();
  vi.mocked(api.get).mockImplementation(async (path: string) => {
    if (path.startsWith('/api/enterprise/sessions/page?')) {
      return {
        items: [session],
        total: 11,
        session_total: 25,
        page: path.includes('page=2') ? 2 : 1,
        page_size: 10,
      };
    }
    if (path.startsWith('/api/enterprise/feedback/summary?')) {
      return {
        total_feedback: 3,
        down_count: 1,
        up_count: 2,
        bucket_counts: [],
        status_counts: {},
        summary: '',
        top_summaries: [],
      };
    }
    if (path.startsWith('/api/enterprise/agents?')) return [];
    throw new Error(`unexpected request: ${path}`);
  });
});

it('对话日志切页只请求目标服务端页并停止全量反馈列表请求', async () => {
  const user = userEvent.setup();
  render(
    <I18nProvider>
      <MemoryRouter initialEntries={['/enterprise/feedback?agent_id=agent_a']}>
        <ConversationLogsTab />
      </MemoryRouter>
    </I18nProvider>,
  );

  expect((await screen.findAllByText('分页会话')).length).toBeGreaterThan(0);
  expect(api.get).not.toHaveBeenCalledWith(expect.stringMatching(/^\/api\/enterprise\/feedback\/sessions\?/));

  await user.click(screen.getByRole('button', { name: '下一页' }));
  await waitFor(() => expect(api.get).toHaveBeenCalledWith(
    '/api/enterprise/sessions/page?tenant_id=tenant_demo&agent_id=agent_a&feedback_filter=all&page=2&page_size=10',
  ));

  await user.click(screen.getByRole('tab', { name: '差评' }));
  await waitFor(() => expect(api.get).toHaveBeenCalledWith(
    '/api/enterprise/sessions/page?tenant_id=tenant_demo&agent_id=agent_a&feedback_filter=down&page=1&page_size=10',
  ));
});

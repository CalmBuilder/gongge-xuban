import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, expect, it, vi } from 'vitest';

import { api } from '../api/client';
import type { EnterpriseAuthUser } from '../auth';
import { I18nProvider } from '../i18n';
import { canManageEmployeeAgent } from '../employee';
import type { AgentProfileRead, ExpertTaxonomyRead } from '../types';
import AgentsPage from './AgentsPage';

vi.mock('../api/client', () => ({
  getRequestTenantId: () => 'tenant_demo',
  api: { get: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn(), post: vi.fn() },
}));

const admin: EnterpriseAuthUser = {
  id: 'admin', tenant_id: 'tenant_demo', username: 'admin', role: 'admin',
  membership_status: 'active', member_category_code: 'employee',
};

const taxonomy: ExpertTaxonomyRead = {
  version: 1,
  categories: [
    { name: '工程研发', subcategories: ['AI 与智能体', '前端与客户端', '后端与平台'] },
    { name: '市场营销', subcategories: ['内容与社交'] },
  ],
};

function row(id: string, name: string, metadata: Record<string, unknown>): AgentProfileRead {
  return {
    id, tenant_id: 'tenant_demo', name, description: `${name}简介`, is_overall: false,
    status: 'active', manageable_by_current_user: true,
    metadata, resources: [], created_at: '', updated_at: '',
  };
}

const rows = [
  row('expert-1', '前端专家', {
    employee_type: 'expert', expert_source_code: 'agency-agents', expert_category: '工程研发',
    expert_subcategory: '前端与客户端', role_name: '工程研发',
  }),
  row('expert-2', '后端专家', {
    employee_type: 'expert', expert_source_code: 'agency-agents', expert_category: '工程研发',
    expert_subcategory: '后端与平台', role_name: '工程研发',
  }),
  row('ordinary', '普通员工', { role_name: '行政' }),
];

function managementPage(source: AgentProfileRead[], path: string) {
  const params = new URL(path, 'http://localhost').searchParams;
  const view = params.get('view') || 'all';
  const expertRows = source.filter((item) => item.metadata?.employee_type === 'expert');
  const filtered = view === 'expert' ? expertRows : source;
  const page = Number(params.get('page') || 1);
  const items = filtered.slice((page - 1) * 12, page * 12);
  return {
    items,
    total: filtered.length,
    view_counts: {
      all: source.length,
      online: source.filter((item) => item.status === 'active').length,
      offline: source.filter((item) => item.status !== 'active').length,
      pending: 0,
      expert: expertRows.length,
      governance: source.filter((item) => item.view_level === 'governance').length,
    },
    facets: { sources: [], departments: [], directions: [] },
    page,
    page_size: 12,
  };
}

function renderPage(user: EnterpriseAuthUser = admin, isAdmin = true) {
  render(
    <I18nProvider>
      <MemoryRouter>
        <AgentsPage currentUser={user} isAdmin={isAdmin} />
      </MemoryRouter>
    </I18nProvider>,
  );
}

beforeEach(() => {
  vi.mocked(api.get).mockReset();
  vi.mocked(api.patch).mockReset();
  vi.mocked(api.get).mockImplementation(async (path: string) => (
    path.startsWith('/api/enterprise/expert-taxonomy') ? taxonomy : managementPage(rows, path)
  ) as never);
});

it('通过分页控件请求第二页数字员工', async () => {
  const manyRows = Array.from({ length: 13 }, (_, index) => (
    row(`agent-${index + 1}`, `测试员工${index + 1}`, { role_name: '测试' })
  ));
  vi.mocked(api.get).mockImplementation(async (path: string) => (
    path.startsWith('/api/enterprise/expert-taxonomy') ? taxonomy : managementPage(manyRows, path)
  ) as never);
  renderPage();

  await screen.findByText('测试员工1');
  await userEvent.click(screen.getByRole('button', { name: '下一页' }));

  expect(await screen.findByText('测试员工13')).toBeInTheDocument();
  expect(api.get).toHaveBeenCalledWith(expect.stringContaining('page=2'));
});

it('loads the first server-managed page instead of downloading the full employee list', async () => {
  renderPage();

  await screen.findByText('普通员工');
  expect(api.get).toHaveBeenCalledWith(expect.stringMatching(
    /^\/api\/enterprise\/agents\/management-page\?.*view=all.*page=1.*page_size=12/,
  ));
});

it('requests the isolated governance page when the view changes', async () => {
  const governanceRow = {
    ...row('governance-fallback', '治理列表仍可用', {}),
    manageable_by_current_user: false,
    view_level: 'governance' as const,
  };
  vi.mocked(api.get).mockImplementation(async (path: string) => {
    if (path.startsWith('/api/enterprise/expert-taxonomy')) return taxonomy as never;
    return managementPage([governanceRow], path) as never;
  });

  renderPage();
  await userEvent.click(await screen.findByRole('link', { name: /^发布治理/ }));

  expect(await screen.findByText('治理列表仍可用')).toBeInTheDocument();
  expect(api.get).toHaveBeenCalledWith(expect.stringContaining('view=governance'));
});

it('shows only experts and atomically updates selected experts', async () => {
  const user = userEvent.setup();
  vi.mocked(api.patch).mockResolvedValue({ updated_count: 2, agent_ids: ['expert-1', 'expert-2'] });
  renderPage();
  await screen.findByText('普通员工');
  await user.click(screen.getByRole('link', { name: /^专家/ }));
  expect(screen.queryByText('普通员工')).not.toBeInTheDocument();
  await user.click(screen.getByRole('checkbox', { name: '选择前端专家' }));
  await user.click(screen.getByRole('checkbox', { name: '选择后端专家' }));
  expect(screen.getByText('已选择 2 位专家')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '修改分类' }));
  await user.click(screen.getByRole('combobox', { name: '一级分类' }));
  await user.click(screen.getByRole('option', { name: '工程研发' }));
  await user.click(screen.getByRole('combobox', { name: '二级分类' }));
  await user.click(screen.getByRole('option', { name: 'AI 与智能体' }));
  await user.click(screen.getByRole('button', { name: '保存分类' }));
  await waitFor(() => expect(api.patch).toHaveBeenCalledWith(
    '/api/enterprise/expert-taxonomy/assignments',
    {
      tenant_id: 'tenant_demo', agent_ids: ['expert-1', 'expert-2'],
      category: '工程研发', subcategory: 'AI 与智能体',
    },
  ));
  await waitFor(() => expect(screen.queryByText('已选择 2 位专家')).not.toBeInTheDocument());
});

it('keeps expert management controls hidden from members', async () => {
  const member = { ...admin, id: 'member', username: 'member', role: 'member' as const };
  const memberRows = rows.map((item) => ({ ...item, metadata: { ...item.metadata, owner_user_id: 'member' } }));
  expect(canManageEmployeeAgent(memberRows[0], member)).toBe(true);
  vi.mocked(api.get).mockImplementation(async (path: string) => (
    path.startsWith('/api/enterprise/expert-taxonomy') ? taxonomy : managementPage(memberRows, path)
  ) as never);
  renderPage(member, false);
  await userEvent.click(screen.getByRole('link', { name: /^专家/ }));
  expect(screen.queryByRole('checkbox', { name: '选择前端专家' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '修改分类' })).not.toBeInTheDocument();
});

it('falls back to read-only expert browsing when taxonomy is unavailable', async () => {
  vi.mocked(api.get).mockImplementation(async (path: string) => {
    if (path.startsWith('/api/enterprise/expert-taxonomy')) {
      throw new Error('taxonomy unavailable');
    }
    return managementPage(rows, path) as never;
  });
  renderPage();
  await screen.findByText('普通员工');
  await userEvent.click(screen.getByRole('link', { name: /^专家/ }));
  expect(screen.getByText('分类规则加载失败，请刷新后重试；当前仍可浏览专家。')).toBeInTheDocument();
  expect(screen.queryByRole('checkbox', { name: '选择前端专家' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '修改分类' })).not.toBeInTheDocument();
});

it('shows a guided empty state without expert filters when no experts exist', async () => {
  vi.mocked(api.get).mockImplementation(async (path: string) => (
    path.startsWith('/api/enterprise/expert-taxonomy')
      ? taxonomy
      : managementPage([row('ordinary', '普通员工', { role_name: '行政' })], path)
  ) as never);
  renderPage();
  await screen.findByText('普通员工');
  await userEvent.click(screen.getByRole('link', { name: /^专家/ }));
  expect(await screen.findByText('还没有专家')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '浏览开放广场' })).toBeInTheDocument();
  expect(screen.queryByRole('link', { name: /全部专家/ })).not.toBeInTheDocument();
  expect(screen.queryByRole('combobox', { name: '来源' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '清除筛选' })).not.toBeInTheDocument();
});

it('separates publication governance from owner editing and chat use', async () => {
  const governanceRow = {
    ...row('governance-private', '待审核员工', {
      owner_user_id: 'another_user',
      published_to_gallery: false,
    }),
    manageable_by_current_user: false,
    view_level: 'governance' as const,
    persona_prompt: undefined,
    owner_user_id: 'another_user',
    profile_revision: 3,
    agent_category_code: 'assistant',
    visibility_scope: 'private' as const,
  };
  vi.mocked(api.get).mockImplementation(async (path: string) => (
    path.startsWith('/api/enterprise/expert-taxonomy') ? taxonomy : managementPage([governanceRow], path)
  ) as never);
  renderPage();

  await userEvent.click(await screen.findByRole('link', { name: /^发布治理/ }));

  await waitFor(() => expect(document.body.textContent).toContain('待审核员工'));
  expect(screen.getByRole('button', { name: '发起对话' })).toBeDisabled();
  await userEvent.click(screen.getByRole('button', { name: '员工操作' }));
  expect(screen.getByRole('menuitem', { name: '编辑资料' })).toHaveAttribute(
    'data-disabled',
  );
  expect(screen.getByRole('menuitem', { name: '发布到广场' })).not.toHaveAttribute(
    'data-disabled',
  );
  await userEvent.keyboard('{Escape}');
  await userEvent.click(screen.getByRole('button', { name: /待审核员工/ }));
  expect(await screen.findByRole('dialog')).toHaveTextContent('查看数字员工档案');
  expect(screen.getByRole('dialog')).toHaveTextContent('another_user');
  expect(screen.getByRole('dialog')).toHaveTextContent('active / r3');
  expect(screen.queryByRole('button', { name: '保存' })).not.toBeInTheDocument();
});

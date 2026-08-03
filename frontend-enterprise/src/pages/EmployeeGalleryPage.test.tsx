import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, expect, it, vi } from 'vitest';

import { api } from '../api/client';
import type { EnterpriseAuthUser } from '../auth';
import { I18nProvider } from '../i18n';
import type { AgentGalleryFacetRead, AgentGalleryPageRead, AgentProfileRead } from '../types';
import EmployeeGalleryPage from './EmployeeGalleryPage';

vi.mock('../api/client', () => ({
  getRequestTenantId: () => 'tenant_demo',
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

const admin: EnterpriseAuthUser = {
  id: 'user_admin', tenant_id: 'tenant_demo', username: 'admin', role: 'admin',
  membership_status: 'active', member_category_code: 'employee',
};

function agent(id: string, name: string, metadata: Record<string, unknown>): AgentProfileRead {
  return {
    id, tenant_id: 'tenant_demo', name, description: `${name} description`, is_overall: false,
    status: 'active', metadata, resources: [], created_at: '', updated_at: '',
  };
}

function expert(
  id: string,
  name: string,
  category: string,
  tags: string[],
  subcategory = '',
): AgentProfileRead {
  return agent(id, name, {
    employee_type: 'expert', expert_source_code: 'agency-agents',
    expert_category: category, expert_subcategory: subcategory, expert_tags: tags,
    expert_name_original: id === 'engineering' ? 'Frontend Developer' : 'Growth Marketer',
    published_to_gallery: true,
    expert_capability_manifest: {
      schema_version: '1', capability_type: 'P0', readiness: 'ready',
      required_capabilities: ['prompt_reasoning'], resolved_capabilities: ['prompt_reasoning'],
      unresolved_requirements: [], orchestration_required: false,
      core_execution_requires_external_capability: false, evidence: ['evidence'],
    },
  });
}

function renderGallery(
  rows: AgentProfileRead[],
  onStartChat = vi.fn(),
  currentUser: EnterpriseAuthUser = admin,
) {
  vi.mocked(api.get).mockImplementation(async (path: string) => {
    const url = new URL(path, 'http://test.local');
    const scope = url.searchParams.get('scope') || 'used';
    const scoped = (targetScope: string) => rows.filter((item) => {
      if (targetScope === 'used') {
        return item.used_by_current_user ?? item.metadata.used_by_current_user === true;
      }
      if (targetScope === 'owned') {
        return item.owner_user_id === currentUser.id
          || item.metadata.owner_user_id === currentUser.id;
      }
      const isExpert = item.agent_category_code === 'professional'
        || item.metadata.employee_type === 'expert';
      if (targetScope === 'gallery') {
        const published = item.published_to_gallery
          ?? item.metadata.published_to_gallery === true;
        return published && !isExpert;
      }
      return isExpert;
    });
    const q = (url.searchParams.get('q') || '').toLowerCase();
    const source = url.searchParams.get('expert_source') || '';
    const department = url.searchParams.get('expert_department') || '';
    const direction = url.searchParams.get('expert_direction') || '';
    const page = Number(url.searchParams.get('page') || '1');
    const pageSize = Number(url.searchParams.get('page_size') || '12');
    const scopeRows = scoped(scope);
    const filtered = scopeRows.filter((item) => {
      if (source && item.metadata.expert_source_code !== source) return false;
      if (department && item.metadata.expert_category !== department) return false;
      if (direction && item.metadata.expert_subcategory !== direction) return false;
      return !q || JSON.stringify(item).toLowerCase().includes(q);
    });
    const experts = scoped('expert');
    const facet = (key: string, candidates: AgentProfileRead[]): AgentGalleryFacetRead[] => {
      const counts = new Map<string, number>();
      for (const item of candidates) {
        const value = String(item.metadata[key] || '');
        if (value) counts.set(value, (counts.get(value) || 0) + 1);
      }
      return [...counts].map(([value, count]) => ({ value, label: value, count }));
    };
    const departmentCandidates = experts.filter((item) => (
      !source || item.metadata.expert_source_code === source
    ));
    const directionCandidates = departmentCandidates.filter((item) => (
      !department || item.metadata.expert_category === department
    ));
    const result: AgentGalleryPageRead = {
      items: filtered.slice((page - 1) * pageSize, page * pageSize),
      total: filtered.length,
      scope_counts: {
        used: scoped('used').length,
        owned: scoped('owned').length,
        gallery: scoped('gallery').length,
        expert: experts.length,
      },
      facets: {
        sources: facet('expert_source_code', experts),
        departments: facet('expert_category', departmentCandidates),
        directions: facet('expert_subcategory', directionCandidates),
      },
      page,
      page_size: pageSize,
    };
    return result as never;
  });
  if (!vi.mocked(api.post).getMockImplementation()) {
    vi.mocked(api.post).mockImplementation(async (path: string) => {
      const segments = path.split('/');
      const agentId = segments[segments.length - 2] || '';
      const selected = rows.find((item) => item.id === agentId);
      return selected
        ? {
            ...selected,
            used_by_current_user: true,
            metadata: { ...selected.metadata, used_by_current_user: true },
          } as never
        : undefined as never;
    });
  }
  render(
    <I18nProvider>
      <MemoryRouter>
        <EmployeeGalleryPage currentUser={currentUser} onStartChat={onStartChat} />
      </MemoryRouter>
    </I18nProvider>,
  );
  return onStartChat;
}

beforeEach(() => {
  vi.mocked(api.get).mockReset();
  vi.mocked(api.post).mockReset();
  vi.mocked(api.delete).mockReset();
});

it('loads each relationship tab from its server-side page endpoint', async () => {
  const user = userEvent.setup();
  renderGallery([
    agent('used', '常用助手', {
      used_by_current_user: true,
      published_to_gallery: false,
    }),
    agent('gallery', '广场助手', {
      owner_user_id: 'another_user',
      used_by_current_user: false,
      published_to_gallery: true,
    }),
  ]);

  expect(await screen.findByText('常用助手')).toBeInTheDocument();
  expect(api.get).toHaveBeenCalledTimes(1);
  const calledPath = vi.mocked(api.get).mock.calls[0][0] as string;
  expect(new URL(calledPath, 'http://test.local').searchParams.get('scope')).toBe('used');

  await user.click(screen.getByRole('tab', { name: '发现' }));
  expect(await screen.findByRole('button', { name: /广场助手/ })).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /常用助手/ })).not.toBeInTheDocument();
  expect(api.get).toHaveBeenCalledTimes(2);
  const discoveredPath = vi.mocked(api.get).mock.calls[1][0] as string;
  expect(new URL(discoveredPath, 'http://test.local').searchParams.get('scope')).toBe('gallery');
});

it('shows twelve owned employees per page and requests page two', async () => {
  const user = userEvent.setup();
  const rows = Array.from({ length: 13 }, (_, index) => agent(
    `owned-${index}`,
    `分页员工 ${String(index).padStart(2, '0')}`,
    { owner_user_id: admin.id },
  ));
  renderGallery(rows);

  await user.click(await screen.findByRole('tab', { name: /我创建的/ }));
  expect(await screen.findByRole('button', { name: /分页员工 00/ })).toBeInTheDocument();
  expect(screen.getByRole('navigation', { name: '数字员工分页' })).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /分页员工 12/ })).not.toBeInTheDocument();

  await user.click(screen.getByRole('button', { name: '下一页' }));
  expect(await screen.findByRole('button', { name: /分页员工 12/ })).toBeInTheDocument();
  const calls = vi.mocked(api.get).mock.calls;
  const lastPath = calls[calls.length - 1][0] as string;
  expect(new URL(lastPath, 'http://test.local').searchParams.get('page')).toBe('2');
});

it('combines expert source department and keyword filters', async () => {
  const user = userEvent.setup();
  renderGallery([
    expert('engineering', '前端开发专家', '工程研发', ['React']),
    expert('marketing', '增长营销专家', '市场营销', ['增长']),
    agent('ordinary', '普通员工', { published_to_gallery: true }),
  ]);
  await user.click(screen.getByRole('tab', { name: '发现' }));
  await user.click(screen.getByRole('tab', { name: /专家/ }));
  await screen.findByText('前端开发专家');
  expect(screen.getAllByRole('button', { name: '工程研发，1 位' })).toHaveLength(2);
  await user.click(screen.getAllByRole('button', { name: '工程研发，1 位' })[0]);
  await user.type(screen.getByRole('textbox', { name: '搜索数字员工' }), 'React');
  expect(screen.getByText('前端开发专家')).toBeInTheDocument();
  expect(screen.queryByText('增长营销专家')).not.toBeInTheDocument();
  expect(screen.queryByText('普通员工')).not.toBeInTheDocument();
});

it('shows expert empty state, hides filters elsewhere, and preserves chat callback', async () => {
  const user = userEvent.setup();
  const onStartChat = renderGallery([expert('engineering', '前端开发专家', '工程研发', ['React'])]);
  await user.click(screen.getByRole('tab', { name: '发现' }));
  await user.click(screen.getByRole('tab', { name: /专家/ }));
  await screen.findByText('前端开发专家');
  await user.click(screen.getByRole('button', { name: /前端开发专家/ }));
  await waitFor(() => expect(onStartChat).toHaveBeenCalled());
  await user.type(screen.getByRole('textbox', { name: '搜索数字员工' }), '不存在');
  expect(await screen.findByText('没有匹配的专家')).toBeInTheDocument();
  await user.click(screen.getByRole('tab', { name: '我的员工' }));
  expect(screen.queryByLabelText('专家专业部门')).not.toBeInTheDocument();
});

it('separates used owned and gallery relationships without treating usage as ownership', async () => {
  const user = userEvent.setup();
  renderGallery([
    agent('used', '常用助手', {
      owner_user_id: 'another_user',
      published_to_gallery: true,
      used_by_current_user: true,
    }),
    agent('owned', '我创建的助手', {
      owner_user_id: admin.id,
      published_to_gallery: false,
      used_by_current_user: false,
    }),
    agent('gallery', '广场助手', {
      owner_user_id: 'another_user',
      published_to_gallery: true,
      used_by_current_user: false,
    }),
  ], vi.fn(), admin);

  expect(await screen.findByRole('button', { name: /常用助手/ })).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /我创建的助手/ })).not.toBeInTheDocument();
  expect(screen.getByText('已添加')).toBeInTheDocument();
  expect(screen.getByText('企业发布')).toBeInTheDocument();

  await user.click(screen.getByRole('tab', { name: /我创建的/ }));
  expect(screen.getByRole('button', { name: /我创建的助手/ })).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /常用助手/ })).not.toBeInTheDocument();
  expect(screen.getByText('我拥有')).toBeInTheDocument();

  await user.click(screen.getByRole('tab', { name: '发现' }));
  await user.click(screen.getByRole('tab', { name: /数字员工广场/ }));
  expect(screen.getByRole('button', { name: /常用助手/ })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: /广场助手/ })).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /我创建的助手/ })).not.toBeInTheDocument();
});

it('filters by specialty and resets an invalid specialty when department changes', async () => {
  const user = userEvent.setup();
  renderGallery([
    expert('data', '数据工程师', '工程研发', ['SQL'], '数据与数据库'),
    expert('frontend', '前端开发工程师', '工程研发', ['React'], '前端与客户端'),
    expert('content', '内容策略师', '市场营销', ['内容'], '内容与社交'),
  ]);
  await user.click(screen.getByRole('tab', { name: '发现' }));
  await user.click(screen.getByRole('tab', { name: /专家/ }));
  await screen.findByText('数据工程师');
  await user.click(screen.getAllByRole('button', { name: '工程研发，2 位' })[0]);
  await user.click(screen.getByRole('button', { name: '数据与数据库，1 位' }));
  expect(screen.getByText('数据工程师')).toBeInTheDocument();
  expect(screen.queryByText('前端开发工程师')).not.toBeInTheDocument();
  expect(screen.queryByText('内容策略师')).not.toBeInTheDocument();

  await user.click(screen.getAllByRole('button', { name: '市场营销，1 位' })[0]);
  expect(screen.queryByRole('button', { name: '数据与数据库，1 位' })).not.toBeInTheDocument();
  expect(screen.getByText('内容策略师')).toBeInTheDocument();
});

it('matches expert specialty in keyword search', async () => {
  const user = userEvent.setup();
  renderGallery([
    expert('data', '数据工程师', '工程研发', ['SQL'], '数据与数据库'),
    expert('frontend', '前端开发工程师', '工程研发', ['React'], '前端与客户端'),
  ]);
  await user.click(screen.getByRole('tab', { name: '发现' }));
  await user.click(screen.getByRole('tab', { name: /专家/ }));
  await screen.findByText('数据工程师');
  await user.type(screen.getByRole('textbox', { name: '搜索数字员工' }), '数据与数据库');

  await waitFor(() => expect(screen.queryByText('前端开发工程师')).not.toBeInTheDocument());
  expect(screen.getByText('数据工程师')).toBeInTheDocument();
});

it('hides expert filters and shows a guided empty state when no experts exist', async () => {
  const user = userEvent.setup();
  renderGallery([agent('ordinary', '普通员工', { published_to_gallery: true })]);
  await user.click(screen.getByRole('tab', { name: '发现' }));
  await user.click(screen.getByRole('tab', { name: /专家/ }));
  expect(await screen.findByText('当前没有可用专家')).toBeInTheDocument();
  expect(screen.getByText('从开放广场复制专家，或由管理员导入专家库后再来看看')).toBeInTheDocument();
  expect(screen.queryByLabelText('专家专业部门')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '清除筛选' })).not.toBeInTheDocument();
});

it('clears expert category and keyword filters from the empty state', async () => {
  const user = userEvent.setup();
  renderGallery([expert('data', '数据工程师', '工程研发', ['SQL'], '数据与数据库')]);
  await user.click(screen.getByRole('tab', { name: '发现' }));
  await user.click(screen.getByRole('tab', { name: /专家/ }));
  await screen.findByText('数据工程师');
  await user.click(screen.getAllByRole('button', { name: '工程研发，1 位' })[0]);
  await user.type(screen.getByRole('textbox', { name: '搜索数字员工' }), '不存在');
  expect(await screen.findByText('没有匹配的专家')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '清除筛选' }));
  expect(await screen.findByText('数据工程师')).toBeInTheDocument();
  expect(screen.getByRole('textbox', { name: '搜索数字员工' })).toHaveValue('');
});

it('adds a gallery employee before chat and can remove usage without deleting history', async () => {
  const user = userEvent.setup();
  const gallery = agent('gallery-add', '待添加员工', {
    owner_user_id: 'another_user',
    published_to_gallery: true,
    used_by_current_user: false,
  });
  vi.mocked(api.post).mockImplementation(async () => {
    gallery.used_by_current_user = true;
    gallery.metadata.used_by_current_user = true;
    return gallery as never;
  });
  vi.mocked(api.delete).mockResolvedValue({ status: 'removed' });
  renderGallery([gallery]);

  await user.click(screen.getByRole('tab', { name: '发现' }));
  await user.click(screen.getByRole('tab', { name: /数字员工广场/ }));
  await user.click(await screen.findByRole('button', { name: '添加到常用' }));
  await waitFor(() => expect(api.post).toHaveBeenCalledWith(
    '/api/chat/agents/gallery-add/use?tenant_id=tenant_demo',
    {},
  ));
  expect(screen.getByRole('button', { name: '移出常用' })).toBeInTheDocument();

  await user.click(screen.getByRole('button', { name: '移出常用' }));
  await waitFor(() => expect(api.delete).toHaveBeenCalledWith(
    '/api/chat/agents/gallery-add/use?tenant_id=tenant_demo',
  ));
});

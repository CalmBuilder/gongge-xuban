import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, expect, it, vi } from 'vitest';

import { api } from '../api/client';
import type { EnterpriseAuthUser } from '../auth';
import { I18nProvider } from '../i18n';
import type { AgentProfileRead, GeneralSkillRead, KnowledgeBaseRead } from '../types';
import OpenPlatformPage from './OpenPlatformPage';

vi.mock('../api/client', () => ({
  getRequestTenantId: () => 'tenant_demo',
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

const galleryAgent: AgentProfileRead = {
  id: 'gallery-agent',
  tenant_id: 'tenant_demo',
  name: '广场财务专家',
  description: '处理报销和财务咨询。',
  is_overall: false,
  status: 'active',
  published_to_gallery: true,
  manageable_by_current_user: false,
  metadata: { role_name: '报销管家' },
  resources: [],
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-01T00:00:00Z',
};

const ownedAgent: AgentProfileRead = {
  ...galleryAgent,
  id: 'owned-agent',
  name: '我的能力分身',
  published_to_gallery: false,
  owner_user_id: 'member-user',
  manageable_by_current_user: true,
};

const provisionedAgent: AgentProfileRead = {
  ...ownedAgent,
  id: 'provisioned-agent',
  name: '新用户的能力分身',
  owner_user_id: 'new-member',
};

const galleryExpert: AgentProfileRead = {
  ...galleryAgent,
  id: 'gallery-expert',
  name: '广场税务专家',
  description: '提供税务分析和风险提示。',
  agent_category_code: 'professional',
  governance_form: 'template',
  metadata: {
    role_name: '税务顾问',
    expert_source_code: 'agency-agents',
    expert_source_label: '项目内置专家快照',
    expert_category: '财税管理',
  },
};

const galleryKnowledge: KnowledgeBaseRead = {
  id: 'gallery-knowledge',
  tenant_id: 'tenant_demo',
  name: '财务制度知识库',
  description: '企业财务制度、报销规则和常见问题。',
  status: 'active',
  access_scope: 'tenant',
  download_policy: 'allowed',
  revision: 1,
  organization_access: [],
  version: 'v1.0.0',
  document_count: 8,
  bucket_count: 3,
  chunk_count: 42,
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-01T00:00:00Z',
};

const galleryGeneralSkill: GeneralSkillRead = {
  id: 'gallery-general-skill',
  tenant_id: null,
  slug: 'data-statistical-analysis',
  name: 'data-statistical-analysis',
  name_zh: '数据统计分析',
  description: 'Summarize, classify, and analyze detailed data.',
  description_zh: '对明细数据汇总、分类和统计。',
  homepage: undefined,
  skill_markdown: '# 数据统计分析',
  skill_files: [],
  metadata: { managed_catalog: true, catalog_scope: 'platform' },
  status: 'published',
  permissions: {},
  runtime_config: { runtime: 'guidance_only' },
  usage_mode: 'planning_guidance',
  visibility_scope: 'platform_gallery',
  current_published_revision_id: 'gallery-general-skill-revision',
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-01T00:00:00Z',
};

function renderPage(
  initialEntry = '/enterprise/platform',
  currentUser?: EnterpriseAuthUser,
) {
  render(
    <I18nProvider>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route path="/enterprise/platform" element={<OpenPlatformPage currentUser={currentUser} />} />
          <Route path="/enterprise/platform/:kind" element={<OpenPlatformPage currentUser={currentUser} />} />
          <Route path="/enterprise/general-skills/catalog/:slug" element={<div>Skill 详情路由</div>} />
        </Routes>
      </MemoryRouter>
    </I18nProvider>,
  );
}

beforeEach(() => {
  window.localStorage.clear();
  vi.mocked(api.get).mockReset();
  vi.mocked(api.post).mockReset();
  vi.mocked(api.get).mockImplementation(async (path: string) => {
    if (path.startsWith('/api/enterprise/agents?')) {
      return [galleryAgent, galleryExpert, ownedAgent] as never;
    }
    if (path.startsWith('/api/enterprise/knowledge-bases?')) {
      return Array.from({ length: 13 }, (_, index) => ({
        ...galleryKnowledge,
        id: `${galleryKnowledge.id}-${index + 1}`,
        name: `${galleryKnowledge.name}${index + 1}`,
      })) as never;
    }
    if (path.startsWith('/api/enterprise/general-skills?')) {
      return [galleryGeneralSkill] as never;
    }
    return [] as never;
  });
});

it('裸路径默认展示数字员工分类且不再提供全部聚合视图', async () => {
  renderPage();

  await screen.findByText('广场财务专家');
  expect(screen.queryByRole('tab', { name: '全部' })).not.toBeInTheDocument();
  expect(screen.getByRole('tab', { name: /数字员工/ })).toHaveAttribute('aria-selected', 'true');
});

it('开放广场数字员工复用会话端和管理端的统一卡片与四列栅格', async () => {
  renderPage('/enterprise/platform/agents');

  const cardName = await screen.findByText('广场财务专家');
  const card = cardName.closest('.gongge-employee-card');
  expect(card).not.toBeNull();
  expect(card?.parentElement).toHaveClass('gg-resource-grid');
  expect(card).toHaveClass('min-h-[var(--gg-resource-card-min-height)]');
});

it('开放平台专家分类与数字员工分类隔离，并保留使用与复制入口', async () => {
  renderPage('/enterprise/platform/experts');

  expect(await screen.findByText(/广场税务专家/)).toBeInTheDocument();
  expect(screen.queryByText('广场财务专家')).not.toBeInTheDocument();
  expect(screen.getByRole('tab', { name: /专家/ })).toHaveAttribute('aria-selected', 'true');

  await userEvent.click(screen.getByRole('button', { name: /广场税务专家/ }));
  expect(await screen.findByRole('button', { name: '复制并定制' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '添加使用并开始对话' })).toBeInTheDocument();
});

it('资源分类与数字员工采用相同的固定卡片尺寸契约', async () => {
  renderPage('/enterprise/platform/knowledge');

  const resourceName = await screen.findByText('财务制度知识库1');
  const card = resourceName.closest('.gongge-platform-resource-card');
  expect(card).not.toBeNull();
  expect(card).toHaveClass('h-full');
  expect(card?.parentElement).toHaveClass('gg-resource-grid');
  expect(card).toHaveClass('min-h-[var(--gg-resource-card-min-height)]');
});

it('已发布平台 Skill 出现在开放广场的 Skill 分类而不是管理入口', async () => {
  renderPage('/enterprise/platform/general-skills');

  expect(await screen.findByText('数据统计分析')).toBeInTheDocument();
  expect(screen.getByRole('heading', { name: 'Skill' })).toBeInTheDocument();
  expect(screen.getByRole('tab', { name: /Skill/ })).toHaveAttribute('aria-selected', 'true');
  expect(screen.queryByRole('button', { name: '发布' })).not.toBeInTheDocument();

  await userEvent.click(screen.getByText('数据统计分析'));
  await userEvent.click(await screen.findByRole('link', { name: '查看详情' }));
  expect(await screen.findByText('Skill 详情路由')).toBeInTheDocument();
});

it('普通用户从开放广场直接安装 Skill 到本人能力分身', async () => {
  vi.mocked(api.post).mockResolvedValue({ action: 'created' } as never);
  renderPage('/enterprise/platform/general-skills', {
    id: 'member-user',
    tenant_id: 'tenant_demo',
    username: 'member',
    role: 'member',
    membership_status: 'active',
    member_category_code: 'employee',
  });

  await userEvent.click(await screen.findByText('数据统计分析'));
  await userEvent.click(await screen.findByRole('button', { name: '安装到能力分身' }));

  expect(api.post).toHaveBeenCalledWith(
    '/api/enterprise/general-skill-catalog/bindings',
    expect.objectContaining({
      tenant_id: 'tenant_demo',
      skill_id: 'gallery-general-skill',
      agent_id: 'owned-agent',
      mode: 'install',
      revision_policy: 'pinned',
      pinned_revision_id: 'gallery-general-skill-revision',
    }),
  );
});

it('没有能力分身的普通用户首次安装 Skill 时自动创建私有能力分身', async () => {
  vi.mocked(api.post)
    .mockResolvedValueOnce(provisionedAgent as never)
    .mockResolvedValueOnce({ action: 'created' } as never);
  renderPage('/enterprise/platform/general-skills', {
    id: 'new-member',
    tenant_id: 'tenant_demo',
    username: 'new-member',
    role: 'member',
    membership_status: 'active',
    member_category_code: 'employee',
  });

  await userEvent.click(await screen.findByText('数据统计分析'));
  await userEvent.click(await screen.findByRole('button', { name: '安装到能力分身' }));

  expect(api.post).toHaveBeenNthCalledWith(
    1,
    '/api/enterprise/agents',
    expect.objectContaining({
      tenant_id: 'tenant_demo',
      source_mode: 'blank',
      visibility_scope: 'private',
    }),
  );
  expect(api.post).toHaveBeenNthCalledWith(
    2,
    '/api/enterprise/general-skill-catalog/bindings',
    expect.objectContaining({ agent_id: 'provisioned-agent', mode: 'install' }),
  );
});

it('每个分类共享十二张一页的分页交互', async () => {
  renderPage('/enterprise/platform/knowledge');

  await screen.findByText('财务制度知识库1');
  expect(document.querySelectorAll('.gongge-platform-resource-card')).toHaveLength(12);
  const paginator = screen.getByRole('navigation', { name: '知识库分页' });
  await userEvent.click(within(paginator).getByRole('button', { name: '下一页' }));

  expect(await screen.findByText('财务制度知识库13')).toBeInTheDocument();
  expect(document.querySelectorAll('.gongge-platform-resource-card')).toHaveLength(1);
  expect(within(paginator).getByRole('button', { name: '02' })).toHaveAttribute('aria-current', 'page');
});

it('切换分类时只替换单一内容区', async () => {
  renderPage();
  await screen.findByText('广场财务专家');

  await userEvent.click(screen.getByRole('tab', { name: /知识库/ }));

  expect(screen.getByRole('heading', { name: '知识库' })).toBeInTheDocument();
  expect(screen.queryByText('广场财务专家')).not.toBeInTheDocument();
});

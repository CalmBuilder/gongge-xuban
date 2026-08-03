import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, expect, it, vi } from 'vitest';

import { api } from '../api/client';
import { I18nProvider } from '../i18n';
import type { AgentProfileRead, KnowledgeBaseRead } from '../types';
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

function renderPage(initialEntry = '/enterprise/platform') {
  render(
    <I18nProvider>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route path="/enterprise/platform" element={<OpenPlatformPage />} />
          <Route path="/enterprise/platform/:kind" element={<OpenPlatformPage />} />
        </Routes>
      </MemoryRouter>
    </I18nProvider>,
  );
}

beforeEach(() => {
  vi.mocked(api.get).mockReset();
  vi.mocked(api.get).mockImplementation(async (path: string) => {
    if (path.startsWith('/api/enterprise/agents?')) return [galleryAgent] as never;
    if (path.startsWith('/api/enterprise/knowledge-bases?')) {
      return Array.from({ length: 13 }, (_, index) => ({
        ...galleryKnowledge,
        id: `${galleryKnowledge.id}-${index + 1}`,
        name: `${galleryKnowledge.name}${index + 1}`,
      })) as never;
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
  expect(card?.parentElement).toHaveClass('2xl:grid-cols-4');
  expect(card?.parentElement).toHaveClass('auto-rows-[292px]');
});

it('资源分类与数字员工采用相同的固定卡片尺寸契约', async () => {
  renderPage('/enterprise/platform/knowledge');

  const resourceName = await screen.findByText('财务制度知识库1');
  const card = resourceName.closest('.gongge-platform-resource-card');
  expect(card).not.toBeNull();
  expect(card).toHaveClass('h-full');
  expect(card?.parentElement).toHaveClass('auto-rows-[292px]');
  expect(card?.parentElement).toHaveClass('2xl:grid-cols-4');
});

it('每个分类共享十二张一页的分页交互', async () => {
  renderPage('/enterprise/platform/knowledge');

  await screen.findByText('财务制度知识库1');
  expect(document.querySelectorAll('.gongge-platform-resource-card')).toHaveLength(12);
  const paginator = screen.getByRole('navigation', { name: '知识库广场分页' });
  await userEvent.click(within(paginator).getByRole('button', { name: '下一页' }));

  expect(await screen.findByText('财务制度知识库13')).toBeInTheDocument();
  expect(document.querySelectorAll('.gongge-platform-resource-card')).toHaveLength(1);
  expect(within(paginator).getByRole('button', { name: '02' })).toHaveAttribute('aria-current', 'page');
});

it('切换分类时只替换单一内容区', async () => {
  renderPage();
  await screen.findByText('广场财务专家');

  await userEvent.click(screen.getByRole('tab', { name: /知识库/ }));

  expect(await screen.findByText('知识库广场')).toBeInTheDocument();
  expect(screen.queryByText('广场财务专家')).not.toBeInTheDocument();
});

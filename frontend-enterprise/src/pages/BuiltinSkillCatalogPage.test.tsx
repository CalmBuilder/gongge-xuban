import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { I18nProvider } from '../i18n';
import type { EnterpriseAuthUser } from '../auth';
import { api } from '../api/client';
import type {
  BuiltinSkillCatalogDetail,
  BuiltinSkillCatalogPage,
} from '../types/general-skill-catalog';
import BuiltinSkillCatalogPageComponent from './BuiltinSkillCatalogPage';

const client = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
}));

vi.mock('../api/client', async (importOriginal) => {
  const original = await importOriginal<typeof import('../api/client')>();
  return {
    ...original,
    api: client,
    getRequestTenantId: () => 'tenant_demo',
  };
});

const administrator: EnterpriseAuthUser = {
  id: 'admin_demo',
  tenant_id: 'tenant_demo',
  username: 'admin',
  role: 'admin',
  membership_status: 'active',
  member_category_code: 'employee',
};

const member: EnterpriseAuthUser = {
  ...administrator,
  id: 'member_demo',
  username: 'member',
  role: 'member',
};

const item = {
  id: 'genskill_catalog_1',
  slug: 'document-summarizer',
  name: 'document-summarizer',
  name_zh: '文档摘要',
  description: '将长文档整理成结构化摘要。',
  description_zh: '将长文档整理为结构化摘要，便于快速阅读和后续处理。',
  category: 'productivity',
  stability: 'stable' as const,
  risk_level: 'low' as const,
  risk_findings: [],
  invocation_policy: 'model_allowed' as const,
  runtime_mode: 'guidance_only' as const,
  source_kind: 'platform_builtin',
  review_status: 'pending',
  status: 'draft' as const,
  source_repository: 'https://github.com/mattpocock/skills',
  source_revision: '6654f6b60cd9d5be8b54c6fafe44346dabeb3b76',
  source_path: 'skills/productivity/document-summarizer/SKILL.md',
  source_license: 'MIT',
  source_package_checksum: 'a'.repeat(64),
  source_normalized_checksum: 'b'.repeat(64),
  content_checksum: 'c'.repeat(64),
  manifest_checksum: 'd'.repeat(64),
  revision_id: 'gsrev_catalog_1',
  revision_number: 1,
  revision_status: 'draft',
  resource_count: 1,
  row_version: 1,
  revision_row_version: 1,
  updated_at: '2026-08-29T00:00:00Z',
  localization_status: 'verified',
  localization_source_content_checksum: 'c'.repeat(64),
  localization_checksum: 'e'.repeat(64),
};

const page: BuiltinSkillCatalogPage = {
  items: [item],
  total: 1,
  page: 1,
  page_size: 12,
  facets: {
    category: { productivity: 1 },
    source_kind: { platform_builtin: 1 },
    stability: { stable: 1 },
    risk_level: { low: 1 },
    invocation_policy: { model_allowed: 1 },
    status: { draft: 1 },
  },
};

const secondItem = {
  ...item,
  id: 'genskill_catalog_2',
  slug: 'meeting-notes',
  name: 'meeting-notes',
  name_zh: '会议纪要',
  description: '把会议内容整理为行动项。',
  description_zh: '把会议内容整理为清晰的行动项。',
  revision_id: 'gsrev_catalog_2',
};

const detail: BuiltinSkillCatalogDetail = {
  ...item,
  skill_markdown: '---\nname: document-summarizer\n---\n# Summary',
  explanation_markdown_zh: '# 中文解读：文档摘要\n\n将长文档整理为结构化摘要。',
  parsed_metadata: { name: 'document-summarizer' },
  allowed_tools: [],
  argument_hint: null,
  metadata: { managed_catalog: true },
  resources: [{
    relative_path: 'SKILL.md',
    content_checksum: 'c'.repeat(64),
    size: 42,
    media_type: 'text/markdown',
    is_text: true,
  }],
  bindings: [],
};

function renderPage(user: EnterpriseAuthUser = administrator, initialEntry = '/enterprise/general-skills/catalog') {
  return render(
    <I18nProvider>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route path="/enterprise/general-skills/catalog" element={<BuiltinSkillCatalogPageComponent currentUser={user} />} />
          <Route path="/enterprise/general-skills/catalog/:slug" element={<BuiltinSkillCatalogPageComponent currentUser={user} />} />
        </Routes>
      </MemoryRouter>
    </I18nProvider>,
  );
}

beforeEach(() => {
  client.get.mockReset();
  client.post.mockReset();
  client.get.mockImplementation(async (path: string) => (
    path.includes('/document-summarizer?') ? detail : page
  ));
  client.post.mockResolvedValue({
    command_id: 'builtin-skill-initial-6654f6b6',
    replayed: true,
    created_count: 37,
    existing_count: 0,
    skill_count: 37,
    source_repository: 'https://github.com/mattpocock/skills',
    source_revision: item.source_revision,
    source_license: 'MIT',
    source_package_checksum: item.source_package_checksum,
    source_normalized_checksum: item.source_normalized_checksum,
    items: [],
  });
});

describe('Skill 管理', () => {
  it('管理员可以从候选卡片进入详情并默认阅读中文解读', async () => {
    renderPage();
    const user = userEvent.setup();

    expect(await screen.findByText('文档摘要')).toBeInTheDocument();
    await user.click(screen.getByRole('link', { name: '查看详情' }));

    expect(await screen.findByText('Skill 文档')).toBeInTheDocument();
    expect(screen.getByText('6654f6b60cd9d5be8b54c6fafe44346dabeb3b76')).toBeInTheDocument();
    expect(screen.getByText(/# 中文解读：文档摘要/)).toBeInTheDocument();
    expect(screen.getByText('中文解读仅用于阅读；实际运行始终使用英文原文。')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '英文原文（运行时使用）' }));
    expect(screen.getByText(/# Summary/)).toBeInTheDocument();
  });

  it('已发布 Skill 可以从详情页安装到能力分身', async () => {
    client.get.mockImplementation(async (path: string) => {
      if (path.includes('/my-general-skills/agents')) {
        return [{ id: 'agent_avatar_1', name: '我的合同助手', status: 'active' }];
      }
      if (path.includes('/document-summarizer?')) {
        return { ...detail, status: 'published', review_status: 'approved', revision_status: 'published' };
      }
      return page;
    });
    renderPage(member, '/enterprise/general-skills/catalog/document-summarizer');
    const user = userEvent.setup();

    await user.click(await screen.findByRole('button', { name: '安装到我的能力分身' }));
    expect(await screen.findByRole('heading', { name: '安装到我的能力分身' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '确认安装' }));

    await waitFor(() => expect(client.post).toHaveBeenCalledWith(
      '/api/enterprise/general-skill-catalog/bindings',
      expect.objectContaining({
        tenant_id: 'tenant_demo',
        skill_id: item.id,
        agent_id: 'agent_avatar_1',
        mode: 'install',
        revision_policy: 'pinned',
        pinned_revision_id: item.revision_id,
        invocation_policy: 'model_allowed',
      }),
    ));
  });

  it('管理员可以重放固定快照导入并刷新候选目录', async () => {
    renderPage();
    const user = userEvent.setup();
    await screen.findByText('文档摘要');

    await user.click(screen.getByRole('button', { name: '核对内置快照' }));

    await waitFor(() => expect(client.post).toHaveBeenCalledWith(
      '/api/enterprise/general-skill-catalog/import',
      { tenant_id: 'tenant_demo', command_id: 'builtin-skill-initial-6654f6b6' },
    ));
    await waitFor(() => expect(client.get).toHaveBeenCalledTimes(2));
  });

  it('管理员可以把固定 GitHub 来源导入为待审核候选', async () => {
    renderPage();
    const user = userEvent.setup();
    await screen.findByText('文档摘要');

    await user.click(screen.getByRole('button', { name: '导入外部 Skill' }));
    expect(await screen.findByRole('heading', { name: '导入外部 Skill' })).toBeInTheDocument();
    await user.type(screen.getByLabelText('仓库、Skill 标识或压缩包地址'), 'https://github.com/acme/skills');
    await user.type(screen.getByLabelText('完整 commit SHA'), 'a'.repeat(40));
    await user.type(screen.getByLabelText('Skill 子路径'), 'skills/productivity/example');
    await user.type(screen.getByLabelText('许可证证据'), 'MIT');
    await user.click(screen.getByRole('button', { name: '导入为待审核候选' }));

    await waitFor(() => expect(client.post).toHaveBeenCalledWith(
      '/api/enterprise/general-skill-catalog/import-external',
      expect.objectContaining({
        tenant_id: 'tenant_demo',
        source_kind: 'github',
        source_url: 'https://github.com/acme/skills',
        source_license: 'MIT',
        revision: 'a'.repeat(40),
        source_subpath: 'skills/productivity/example',
        command_id: expect.stringMatching(/^external-skill-/),
      }),
    ));
  });

  it('管理员可以批量通过候选并提交审核说明', async () => {
    renderPage();
    const user = userEvent.setup();
    await screen.findByText('文档摘要');

    await user.click(screen.getByRole('checkbox', { name: '选择 文档摘要' }));
    await user.click(screen.getByRole('button', { name: '批量通过' }));
    expect(await screen.findByRole('heading', { name: '批量审核 Skill' })).toBeInTheDocument();
    await user.type(screen.getByLabelText('审核说明（可选）'), '已复核来源和风险证据');
    await user.click(screen.getByRole('button', { name: '确认通过' }));

    await waitFor(() => expect(client.post).toHaveBeenCalledWith(
      '/api/enterprise/general-skill-catalog/review',
      expect.objectContaining({
        tenant_id: 'tenant_demo',
        command_id: expect.stringMatching(/^catalog-review-/),
        items: [{
          skill_id: item.id,
          decision: 'approve',
          expected_skill_row_version: 1,
          expected_revision_row_version: 1,
          review_note: '已复核来源和风险证据',
        }],
      }),
    ));
  });

  it('跨页选择候选后批量审核会保留已选版本快照', async () => {
    client.get.mockImplementation(async (path: string) => {
      if (path.includes('page=2')) {
        return { ...page, page: 2, total: 13, items: [secondItem] };
      }
      return { ...page, total: 13 };
    });
    renderPage();
    const user = userEvent.setup();

    await user.click(await screen.findByRole('checkbox', { name: '选择 文档摘要' }));
    await user.click(screen.getByRole('button', { name: '下一页' }));
    await user.click(await screen.findByRole('checkbox', { name: '选择 会议纪要' }));
    expect(screen.getByText('已选 2 个待审核 Skill')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '批量通过' }));
    await user.click(await screen.findByRole('button', { name: '确认通过' }));

    await waitFor(() => expect(client.post).toHaveBeenCalledWith(
      '/api/enterprise/general-skill-catalog/review',
      expect.objectContaining({
        items: expect.arrayContaining([
          expect.objectContaining({ skill_id: item.id, expected_revision_row_version: 1 }),
          expect.objectContaining({ skill_id: secondItem.id, expected_revision_row_version: 1 }),
        ]),
      }),
    ));
  });

  it('普通成员看不到待审核候选，也看不到管理员导入动作', async () => {
    client.get.mockResolvedValue({ ...page, items: [], total: 0 });
    renderPage(member);

    expect(await screen.findByText('当前没有已发布的内置 Skill。')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '核对内置快照' })).not.toBeInTheDocument();
  });
});

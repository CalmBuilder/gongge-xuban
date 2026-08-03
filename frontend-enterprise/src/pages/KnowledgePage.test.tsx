import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, expect, it, vi } from 'vitest';

import type { EnterpriseAuthUser } from '../auth';
import { EnterpriseContextProvider } from '../enterprise-context';
import { I18nProvider } from '../i18n';
import KnowledgeManagePage from './KnowledgePage';

const client = vi.hoisted(() => ({
  get: vi.fn(),
  put: vi.fn(),
}));

vi.mock('../api/client', () => ({
  api: client,
  ApiError: class ApiError extends Error {
    status: number;

    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  },
  getRequestTenantId: () => 'tenant_a',
}));

const administrator: EnterpriseAuthUser = {
  id: 'admin_a',
  tenant_id: 'tenant_a',
  username: 'admin',
  role: 'admin',
  membership_status: 'active',
  member_category_code: 'employee',
  governance_permission_codes: ['knowledge.read', 'knowledge.manage'],
};

const knowledgeBase = {
  id: 'kb_policy',
  tenant_id: 'tenant_a',
  name: '研究院制度库',
  description: '制度资料',
  status: 'active',
  owner_user_id: 'admin_a',
  access_scope: 'owner' as const,
  download_policy: 'restricted' as const,
  revision: 1,
  organization_access: [],
  content_access_allowed: true,
  content_access_reason: 'owner',
  version: '1.0.0',
  metadata: { creator_name: '管理员' },
  document_count: 0,
  bucket_count: 0,
  chunk_count: 0,
  created_at: '2026-07-28T00:00:00',
  updated_at: '2026-07-28T00:00:00',
};

beforeEach(() => {
  window.localStorage.clear();
  client.get.mockReset();
  client.put.mockReset();
  client.get.mockImplementation(async (path: string) => {
    if (path.startsWith('/api/enterprise/agents?')) return [];
    if (path.startsWith('/api/enterprise/model-configs?')) return [];
    if (path.startsWith('/api/enterprise/knowledge/documents?')) return [];
    if (path.startsWith('/api/enterprise/knowledge-bases?')) return [knowledgeBase];
    if (path.includes('/okf/concepts?')) return [];
    if (path.startsWith('/api/organization/unit-children?')) {
      if (path.includes('parent_id=')) {
        return [{
          id: 'org_project',
          tenant_id: 'tenant_a',
          parent_id: 'org_root',
          code: 'PROJECT',
          name: '政企项目集',
          unit_type_code: 'department',
          tree_path: 'org_root/org_project',
          depth: 1,
          sort_order: 0,
          is_root: false,
          status: 'active',
          has_children: false,
        }];
      }
      return [{
        id: 'org_root',
        tenant_id: 'tenant_a',
        parent_id: null,
        code: 'ROOT',
        name: '软件研究院',
        unit_type_code: 'company',
        tree_path: 'org_root',
        depth: 0,
        sort_order: 0,
        is_root: true,
        status: 'active',
        has_children: true,
      }];
    }
    throw new Error(`unexpected request: ${path}`);
  });
  client.put.mockImplementation(async (_path: string, body: Record<string, unknown>) => ({
    ...knowledgeBase,
    responsible_org_unit_id: body.responsible_org_unit_id,
    access_scope: body.access_scope,
    download_policy: body.download_policy,
    revision: 2,
    organization_access: (
      body.organization_access as Array<{
        org_unit_id: string;
        include_descendants: boolean;
      }>
    ).map((item, index) => ({
      id: `access_${index}`,
      ...item,
      status: 'active',
    })),
  }));
});

it('keeps the knowledge-base list when the document endpoint fails', async () => {
  const successfulGet = client.get.getMockImplementation();
  client.get.mockImplementation((path: string) => {
    if (path.startsWith('/api/enterprise/knowledge/documents?')) {
      return Promise.reject(new Error('Not Found'));
    }
    return successfulGet?.(path);
  });

  renderPage();

  expect((await screen.findAllByText('研究院制度库')).length).toBeGreaterThan(0);
  expect(screen.getAllByText('制度资料').length).toBeGreaterThan(0);
});

it('saves an organization knowledge scope with the current revision', async () => {
  const user = userEvent.setup();
  renderPage();

  await screen.findAllByText('研究院制度库');
  await user.click(screen.getAllByRole('button', { name: '知识库操作' })[0]);
  await user.click(await screen.findByText('访问治理'));
  await user.click(await screen.findByRole('treeitem', { name: /政企项目集/ }));
  await user.click(screen.getByRole('combobox', { name: '知识访问范围' }));
  await user.click(screen.getByRole('option', { name: '指定组织' }));
  await user.click(screen.getByRole('button', { name: '加入所选组织' }));
  await user.click(screen.getByRole('button', { name: '保存治理范围' }));

  await waitFor(() => expect(client.put).toHaveBeenCalledWith(
    '/api/enterprise/knowledge-bases/kb_policy/governance',
    expect.objectContaining({
      tenant_id: 'tenant_a',
      expected_revision: 1,
      access_scope: 'organization',
      download_policy: 'restricted',
      organization_access: [{
        org_unit_id: 'org_project',
        include_descendants: true,
      }],
    }),
  ));
});

it('allows a knowledge governor to leave employee scope for platform governance', async () => {
  const user = userEvent.setup();
  window.localStorage.setItem('gongge_enterprise_agent_scope', 'agent_legal');
  const successfulGet = client.get.getMockImplementation();
  client.get.mockImplementation(async (path: string) => {
    if (path.startsWith('/api/enterprise/agents?')) {
      return [{
        id: 'agent_legal',
        tenant_id: 'tenant_a',
        name: '法务数字员工',
        description: '',
        status: 'active',
        is_overall: false,
        metadata: {
          owner_user_id: 'admin_a',
          role_name: '法务',
        },
      }];
    }
    return successfulGet?.(path);
  });

  renderPage();

  await user.click(await screen.findByRole('button', { name: '平台知识治理' }));

  await waitFor(() => {
    expect(window.localStorage.getItem('gongge_enterprise_agent_scope')).toBeNull();
    expect(client.get).toHaveBeenCalledWith(
      '/api/enterprise/knowledge-bases?tenant_id=tenant_a&governance_view=true',
    );
  });
});

it('keeps governance available but disables content actions without content access', async () => {
  const user = userEvent.setup();
  const successfulGet = client.get.getMockImplementation();
  client.get.mockImplementation(async (path: string) => {
    if (path.startsWith('/api/enterprise/knowledge-bases?')) {
      return [{
        ...knowledgeBase,
        id: 'kb_governance_only',
        name: '外部责任组织资料',
        content_access_allowed: false,
        content_access_reason: 'organization_mismatch',
        download_policy: 'allowed',
      }];
    }
    return successfulGet?.(path);
  });

  renderPage();

  expect((await screen.findAllByText('仅可治理')).length).toBeGreaterThan(0);
  expect(screen.getByRole('note')).toHaveTextContent('仅可治理');
  await user.click(screen.getAllByRole('button', { name: '知识库操作' })[0]);

  expect(await screen.findByRole('menuitem', { name: '访问治理' })).toBeEnabled();
  expect(screen.getByRole('menuitem', { name: '版本管理' })).toHaveAttribute('data-disabled');
  expect(screen.getByRole('menuitem', { name: '导出知识库备份包' })).toHaveAttribute('data-disabled');
  expect(screen.getByRole('menuitem', { name: '知识图谱检查' })).toHaveAttribute('data-disabled');
});

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/enterprise/knowledge']}>
      <I18nProvider>
        <EnterpriseContextProvider
          value={{
            tenant: { id: 'tenant_a', name: '企业甲' },
            member: administrator,
            is_administrator: true,
          }}
        >
          <KnowledgeManagePage currentUser={administrator} />
        </EnterpriseContextProvider>
      </I18nProvider>
    </MemoryRouter>,
  );
}

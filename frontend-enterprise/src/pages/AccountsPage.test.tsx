import { render, screen } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';

import { api } from '../api/client';
import type { EnterpriseAuthUser } from '../auth';
import { I18nProvider } from '../i18n';
import AccountsPage from './AccountsPage';

vi.mock('../api/client', () => ({
  getRequestTenantId: () => 'tenant_demo',
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

const administrator: EnterpriseAuthUser = {
  id: 'admin',
  tenant_id: 'tenant_demo',
  username: 'admin',
  role: 'admin',
  membership_status: 'active',
  member_category_code: 'employee',
};

beforeEach(() => {
  vi.mocked(api.get).mockImplementation(async (path: string) => {
    if (path.includes('business-roles')) return [];
    if (path.includes('member-categories')) {
      return [{
        code: 'employee',
        name: '正式员工',
        status: 'active',
        is_builtin: true,
        sort_order: 10,
        revision: 0,
      }];
    }
    if (path.includes('/api/organization/unit-children')) {
      return [{
        id: 'root',
        tenant_id: 'tenant_demo',
        parent_id: null,
        code: 'ROOT',
        name: '匿名企业',
        unit_type_code: 'company',
        tree_path: 'root',
        depth: 0,
        sort_order: 0,
        is_root: true,
        status: 'active',
        has_children: false,
      }];
    }
    if (path.includes('/api/auth/users/page')) {
      return {
        items: [{
          id: 'member_1',
          tenant_id: 'tenant_demo',
          username: 'zhangsan',
          display_name: '张三',
          role: 'member',
          membership_status: 'suspended',
          member_category_code: 'employee',
          joined_at: '2026-07-01T00:00:00',
          business_role_codes: [],
          assignment_history_count: 0,
        }],
        total: 1,
        page: 1,
        page_size: 10,
      };
    }
    return [];
  });
});

it('presents accounts as lifecycle-aware enterprise members', async () => {
  render(
    <I18nProvider>
      <AccountsPage currentUser={administrator} />
    </I18nProvider>,
  );

  expect(await screen.findByRole('table', { name: '成员列表' })).toBeInTheDocument();
  expect(screen.getAllByText('张三').length).toBeGreaterThan(0);
  expect(screen.getAllByText('停用').length).toBeGreaterThan(0);
  expect(screen.getAllByText('正式员工').length).toBeGreaterThan(0);
  expect(screen.getByRole('button', { name: '新增成员' })).toBeInTheDocument();
  expect(screen.getByRole('checkbox', { name: '包含下级组织成员' })).not.toBeChecked();
});

it('keeps the member list visible when organization enrichment fails', async () => {
  const successfulGet = vi.mocked(api.get).getMockImplementation();
  vi.mocked(api.get).mockImplementation(async (path: string) => {
    if (path.includes('/api/organization/unit-children')) {
      throw new Error('Not Found');
    }
    return successfulGet?.(path);
  });

  render(
    <I18nProvider>
      <AccountsPage currentUser={administrator} />
    </I18nProvider>,
  );

  expect(await screen.findByRole('table', { name: '成员列表' })).toBeInTheDocument();
  expect(screen.getAllByText('张三').length).toBeGreaterThan(0);
});

it('hides mutation actions from a member with read-only governance permission', async () => {
  const reader: EnterpriseAuthUser = {
    ...administrator,
    id: 'member_reader',
    username: 'member_reader',
    role: 'member',
    governance_permission_codes: ['member.read'],
  };

  render(
    <I18nProvider>
      <AccountsPage currentUser={reader} />
    </I18nProvider>,
  );

  expect(await screen.findByRole('table', { name: '成员列表' })).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '新增成员' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '账号操作' })).not.toBeInTheDocument();
});

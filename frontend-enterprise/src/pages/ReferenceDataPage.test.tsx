import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, expect, it, vi } from 'vitest';

import type { EnterpriseAuthUser } from '../auth';
import { EnterpriseContextProvider } from '../enterprise-context';
import { I18nProvider } from '../i18n';
import ReferenceDataPage from './ReferenceDataPage';

const api = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
}));

vi.mock('../api/client', () => ({ api }));

const administrator: EnterpriseAuthUser = {
  id: 'admin_a',
  tenant_id: 'tenant_a',
  username: 'admin',
  role: 'admin',
  membership_status: 'active',
  member_category_code: 'employee',
};

beforeEach(() => {
  api.get.mockReset();
  api.post.mockReset();
  api.put.mockReset();
  api.get.mockImplementation((path: string) => {
    if (path.startsWith('/api/reference-data/code-sets?')) {
      return [
        {
          code: 'member_category',
          name: '成员类别',
          description: '成员生命周期分类',
          status: 'active',
          allow_custom_items: true,
        },
        {
          code: 'organization_leader_type',
          name: '负责人类型',
          description: '责任关系分类',
          status: 'active',
          allow_custom_items: true,
        },
      ];
    }
    if (path.includes('/member_category/items?')) {
      return [{
        code: 'employee',
        name: '正式员工',
        description: null,
        status: 'active',
        is_builtin: true,
        sort_order: 10,
        revision: 0,
      }];
    }
    if (path.includes('/organization_leader_type/items?')) {
      return [{
        code: 'primary',
        name: '主要负责人',
        description: null,
        status: 'active',
        is_builtin: true,
        sort_order: 10,
        revision: 0,
      }];
    }
    throw new Error(`unexpected request: ${path}`);
  });
});

it('switches among the server-whitelisted business code sets', async () => {
  const user = userEvent.setup();
  render(
    <I18nProvider>
      <EnterpriseContextProvider
        value={{
          tenant: { id: 'tenant_a', name: '企业甲' },
          member: administrator,
          is_administrator: true,
        }}
      >
        <ReferenceDataPage currentUser={administrator} />
      </EnterpriseContextProvider>
    </I18nProvider>,
  );

  expect(await screen.findByText('正式员工')).toBeVisible();
  await user.click(screen.getByRole('button', { name: /负责人类型/ }));
  expect(await screen.findByText('主要负责人')).toBeVisible();
  expect(screen.getByText(/编码创建后不可修改/)).toBeVisible();
});

it('keeps the code set catalog when one item request fails', async () => {
  const successfulGet = api.get.getMockImplementation();
  api.get.mockImplementation((path: string) => {
    if (path.includes('/member_category/items?')) {
      return Promise.reject(new Error('items unavailable'));
    }
    return successfulGet?.(path);
  });
  render(
    <I18nProvider>
      <EnterpriseContextProvider
        value={{
          tenant: { id: 'tenant_a', name: '企业甲' },
          member: administrator,
          is_administrator: true,
        }}
      >
        <ReferenceDataPage currentUser={administrator} />
      </EnterpriseContextProvider>
    </I18nProvider>,
  );

  expect(await screen.findByRole('button', { name: /成员类别/ })).toBeVisible();
  expect(screen.getByRole('button', { name: /负责人类型/ })).toBeVisible();
});

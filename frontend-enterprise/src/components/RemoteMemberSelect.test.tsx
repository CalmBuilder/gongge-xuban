import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, expect, it, vi } from 'vitest';

import { I18nProvider } from '../i18n';
import { RemoteMemberSelect } from './RemoteMemberSelect';

const api = vi.hoisted(() => ({
  get: vi.fn(),
}));

vi.mock('../api/client', () => ({ api }));

type DeferredRequest = {
  resolve: (value: unknown) => void;
};

beforeEach(() => {
  api.get.mockReset();
});

it('忽略迟到的旧组织成员响应，保留当前范围成员', async () => {
  const pending: DeferredRequest[] = [];
  api.get.mockImplementation(() => new Promise((resolve) => pending.push({ resolve })));
  const user = userEvent.setup();

  render(
    <I18nProvider>
      <RemoteMemberSelect
        tenantId="tenant_a"
        value=""
        onValueChange={vi.fn()}
        ariaLabel="选择岗位任职成员"
        orgUnitId="finance"
      />
    </I18nProvider>,
  );

  await waitFor(() => expect(pending).toHaveLength(1));
  await user.type(screen.getByLabelText('搜索选择岗位任职成员'), '财');
  await waitFor(() => expect(pending).toHaveLength(2));

  pending[1].resolve({
    items: [{
      id: 'member_finance',
      username: 'finance',
      display_name: '财务成员',
      employee_profile_id: 'profile_finance',
      employee_id: 'E001',
      employee_name: '财务成员',
      membership_status: 'active',
    }],
    total: 1,
    page: 1,
    page_size: 50,
  });

  await waitFor(() => expect(api.get).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(screen.getByRole('combobox', { name: '选择岗位任职成员' })).toBeVisible());
  await user.click(screen.getByRole('combobox', { name: '选择岗位任职成员' }));
  await waitFor(() => expect(screen.getByRole('option', { name: /财务成员/ })).toBeVisible());

  pending[0].resolve({ items: [], total: 0, page: 1, page_size: 50 });
  await new Promise((resolve) => window.setTimeout(resolve, 0));
  expect(screen.getByRole('option', { name: /财务成员/ })).toBeVisible();
});

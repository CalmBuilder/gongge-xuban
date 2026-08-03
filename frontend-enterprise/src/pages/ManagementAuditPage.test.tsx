import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, expect, it, vi } from 'vitest';

import type { EnterpriseAuthUser } from '../auth';
import { EnterpriseContextProvider } from '../enterprise-context';
import { I18nProvider } from '../i18n';
import ManagementAuditPage from './ManagementAuditPage';

const api = vi.hoisted(() => ({
  get: vi.fn(),
}));

vi.mock('../api/client', () => ({ api }));

const auditor: EnterpriseAuthUser = {
  id: 'auditor_a',
  tenant_id: 'tenant_a',
  username: 'auditor',
  display_name: '治理审计员',
  role: 'member',
  membership_status: 'active',
  member_category_code: 'employee',
  governance_permission_codes: ['audit.read'],
};

const auditRow = {
  id: 'audit_1',
  tenant_id: 'tenant_a',
  actor_user_id: 'admin_a',
  actor_type: 'user',
  actor_display_name: '平台管理员',
  action: 'organization.update',
  action_kind: 'update',
  outcome: 'success',
  resource_type: 'organization_unit',
  resource_id: 'org_division',
  target_org_unit_id: 'org_division',
  permission_code: 'organization.manage',
  permission_source: 'business_role:organization_admin',
  request_id: 'req_1',
  correlation_id: 'req_1',
  before: { name: '科技创新部' },
  after: { name: '科技创新与研发部' },
  detail: { token: '[REDACTED]' },
  created_at: '2026-07-29T01:20:00Z',
};

beforeEach(() => {
  api.get.mockReset();
  api.get.mockImplementation((path: string) => {
    if (path.startsWith('/api/management-audit/logs/audit_1?')) {
      return auditRow;
    }
    if (path.startsWith('/api/management-audit/logs?')) {
      return {
        items: [auditRow],
        total: 1,
        page: 1,
        page_size: 20,
      };
    }
    throw new Error(`unexpected request: ${path}`);
  });
});

function renderPage() {
  return render(
    <I18nProvider>
      <EnterpriseContextProvider
        value={{
          tenant: { id: 'tenant_a', name: '企业甲' },
          member: auditor,
          is_administrator: false,
        }}
      >
        <ManagementAuditPage currentUser={auditor} />
      </EnterpriseContextProvider>
    </I18nProvider>,
  );
}

it('uses server pagination and opens sanitized details on demand without export', async () => {
  const user = userEvent.setup();
  renderPage();

  expect(await screen.findByText('organization.update')).toBeVisible();
  expect(screen.getByText('平台管理员')).toBeVisible();
  expect(screen.queryByRole('button', { name: /导出/ })).not.toBeInTheDocument();
  expect(api.get).toHaveBeenCalledTimes(1);

  await user.click(screen.getByRole('button', { name: '查看详情' }));

  expect(await screen.findByText('审计详情')).toBeVisible();
  expect(screen.getAllByText('req_1')).toHaveLength(2);
  expect(screen.getByText(/\[REDACTED\]/)).toBeVisible();
  expect(api.get).toHaveBeenCalledWith(
    '/api/management-audit/logs/audit_1?tenant_id=tenant_a',
  );
});

it('submits outcome and action filters to the server', async () => {
  const user = userEvent.setup();
  renderPage();
  await screen.findByText('organization.update');

  await user.type(screen.getByLabelText('操作编码'), 'knowledge.read');
  await user.click(screen.getByRole('combobox', { name: '结果' }));
  await user.click(screen.getByRole('option', { name: '拒绝' }));
  await user.click(screen.getByRole('button', { name: '查询' }));

  expect(api.get).toHaveBeenLastCalledWith(expect.stringContaining('action=knowledge.read'));
  expect(api.get).toHaveBeenLastCalledWith(expect.stringContaining('outcome=denied'));
});

it('keeps the current ledger visible when a refresh fails', async () => {
  const user = userEvent.setup();
  renderPage();
  expect(await screen.findByText('organization.update')).toBeVisible();
  api.get.mockRejectedValueOnce(new Error('audit unavailable'));

  await user.click(screen.getByRole('button', { name: '查询' }));

  expect(screen.getByText('organization.update')).toBeVisible();
});

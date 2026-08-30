import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, expect, it, vi } from 'vitest';

import { api } from '../api/client';
import type { EnterpriseAuthUser } from '../auth';
import { I18nProvider } from '../i18n';
import type { AgentProfileRead } from '../types';
import type { OrganizationUnit } from '../types/organization';
import EmployeeProfileEditor from './EmployeeProfileEditor';

vi.mock('../api/client', () => ({
  getRequestTenantId: () => 'tenant_demo',
  api: { get: vi.fn(), put: vi.fn() },
}));

vi.mock('./OrganizationTreeNavigator', () => ({
  OrganizationTreeNavigator: ({
    onSelect,
  }: {
    onSelect: (organization: OrganizationUnit) => void;
  }) => (
    <button
      onClick={() => onSelect({
        id: 'org_finance',
        tenant_id: 'tenant_demo',
        parent_id: 'org_root',
        code: 'FINANCE',
        name: '财务部',
        unit_type_code: 'department',
        tree_path: '/org_root/org_finance/',
        depth: 1,
        sort_order: 1,
        is_root: false,
        status: 'active',
      })}
      type="button"
    >
      选择财务部
    </button>
  ),
}));

const admin: EnterpriseAuthUser = {
  id: 'admin',
  tenant_id: 'tenant_demo',
  username: 'admin',
  role: 'admin',
  membership_status: 'active',
  member_category_code: 'employee',
};

const agent: AgentProfileRead = {
  id: 'agent_finance',
  tenant_id: 'tenant_demo',
  name: '财务数字员工',
  is_overall: false,
  status: 'active',
  owner_user_id: 'owner',
  published_to_gallery: true,
  visibility_scope: 'tenant',
  manageable_by_current_user: false,
  view_level: 'governance',
  metadata: {},
  resources: [],
  created_at: '',
  updated_at: '',
};

const expertTemplate: AgentProfileRead = {
  ...agent,
  id: 'expert-template',
  name: '数据可视化工程师',
  governance_form: 'template',
  metadata: { employee_type: 'expert', role_name: '工程研发' },
};

beforeEach(() => {
  vi.mocked(api.put).mockReset();
});

it('lets an agent governor set responsibility without editing the private profile', async () => {
  const saved = {
    ...agent,
    responsible_org_unit_id: 'org_finance',
    responsible_org_unit_name: '财务部',
  };
  vi.mocked(api.put).mockResolvedValue(saved);
  const onSaved = vi.fn();
  const user = userEvent.setup();

  render(
    <I18nProvider>
      <EmployeeProfileEditor
        agent={agent}
        currentUser={admin}
        onClose={vi.fn()}
        onSaved={onSaved}
        open
      />
    </I18nProvider>,
  );

  expect(screen.getByRole('dialog')).toHaveTextContent('查看数字员工档案');
  await user.click(screen.getByRole('button', { name: '选择财务部' }));
  await user.click(screen.getByRole('button', { name: '设为责任组织' }));

  await waitFor(() => expect(api.put).toHaveBeenCalledWith(
    '/api/enterprise/agents/agent_finance/responsibility',
    {
      tenant_id: 'tenant_demo',
      responsible_org_unit_id: 'org_finance',
    },
  ));
  expect(onSaved).toHaveBeenCalledWith(saved);
});

it('keeps the expert template dialog at the original width and uses one avatar radius', () => {
  render(
    <I18nProvider>
      <EmployeeProfileEditor
        agent={expertTemplate}
        currentUser={admin}
        mode="expert-template"
        onClose={vi.fn()}
        open
      />
    </I18nProvider>,
  );

  const dialog = screen.getByRole('dialog');
  expect(dialog).toHaveClass('sm:max-w-[860px]');
  expect(dialog).not.toHaveClass('sm:max-w-[520px]');
  const avatar = dialog.querySelector('.employee-profile-preview-avatar');
  expect(avatar).toHaveStyle({
    borderRadius: 'var(--gg-radius-avatar-detail)',
  });
  expect(avatar?.querySelector('img')).toHaveStyle({
    borderRadius: 'var(--gg-radius-avatar-detail)',
  });
});

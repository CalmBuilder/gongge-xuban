import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, expect, it, vi } from 'vitest';

import type { EnterpriseAuthUser } from '../auth';
import { EnterpriseContextProvider } from '../enterprise-context';
import { I18nProvider } from '../i18n';
import OrganizationPage from './OrganizationPage';

const api = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
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
  api.get.mockImplementation((path: string) => {
    if (path.startsWith('/api/organization/unit-children?')) {
      if (!path.includes('parent_id=')) {
        return [{
          id: 'root_a',
          tenant_id: 'tenant_a',
          parent_id: null,
          code: 'ROOT',
          name: '企业甲',
          unit_type_code: 'company',
          tree_path: 'root_a',
          depth: 0,
          sort_order: 0,
          is_root: true,
          status: 'active',
          has_children: true,
        }];
      }
      return [{
          id: 'finance',
          tenant_id: 'tenant_a',
          parent_id: 'root_a',
          code: 'FINANCE',
          name: '财务部',
          unit_type_code: 'department',
          tree_path: 'root_a/finance',
          depth: 1,
          sort_order: 0,
          is_root: false,
          status: 'active',
          has_children: false,
        }];
    }
    if (path.startsWith('/api/organization/unit-summary?')) {
      return {
        org_unit_id: path.includes('finance') ? 'finance' : 'root_a',
        direct_member_count: 1,
        subtree_member_count: 1,
        direct_child_count: path.includes('finance') ? 0 : 1,
        current_leader_count: path.includes('finance') ? 1 : 0,
      };
    }
    if (path.startsWith('/api/organization/positions?')) {
      return [{
        id: 'position_finance',
        tenant_id: 'tenant_a',
        org_unit_id: 'finance',
        code: 'FIN_APPROVER',
        name: '财务审批岗',
        position_type_code: 'professional',
        reports_to_position_id: null,
        headcount_limit: null,
        responsibility: '审核财务申请',
        status: 'active',
      }];
    }
    if (path.startsWith('/api/organization/member-org-assignments/page?')) {
      const items = path.includes('org_unit_id=finance') ? [{
        id: 'org_assignment',
        tenant_id: 'tenant_a',
        employee_profile_id: 'profile_member',
        org_unit_id: 'finance',
        assignment_type: 'primary',
        is_primary: true,
        effective_from: '2026-07-28T08:00:00',
        effective_until: null,
        status: 'active',
        user_id: 'member_a',
        username: 'member',
        display_name: '成员一',
        employee_id: 'E001',
        employee_name: '成员一',
      }] : [];
      return { items, total: items.length, page: 1, page_size: 50 };
    }
    if (path.startsWith('/api/organization/position-assignments?')) {
      return [{
        id: 'position_assignment',
        tenant_id: 'tenant_a',
        employee_profile_id: 'profile_member',
        position_id: 'position_finance',
        assignment_type: 'primary',
        is_primary: true,
        effective_from: '2026-07-28T08:00:00',
        effective_until: null,
        status: 'active',
      }];
    }
    if (path.startsWith('/api/organization/position-role-bindings?')) {
      return [{
        id: 'binding_finance',
        tenant_id: 'tenant_a',
        position_id: 'position_finance',
        business_role_id: 'role_finance',
        business_role_code: 'finance.approver',
        business_role_name: '财务审批人',
        scope_mode: 'position_org',
        granted_by_user_id: 'admin_a',
        status: 'active',
        effective_from: '2026-07-28T08:00:00',
        effective_until: null,
      }];
    }
    if (path.startsWith('/api/auth/users/page?')) {
      return { items: [{
        id: 'member_a',
        username: 'member',
        display_name: '成员一',
        employee_profile_id: 'profile_member',
        employee_id: 'E001',
        employee_name: '成员一',
        membership_status: 'active',
      }], total: 1, page: 1, page_size: 100 };
    }
    if (path.startsWith('/api/organization/business-roles?')) {
      return [{ id: 'role_finance', role_code: 'finance.approver', name: '财务审批人', status: 'active' }];
    }
    if (path.startsWith('/api/organization/unit-types?')) {
      return [{ code: 'department', name: '部门', status: 'active' }];
    }
    if (path.startsWith('/api/organization/position-types?')) {
      return [{ code: 'professional', name: '专业岗位', status: 'active' }];
    }
    if (path.startsWith('/api/organization/leader-types?')) {
      return [
        { code: 'primary', name: '主要负责人', status: 'active', sort_order: 10 },
        { code: 'deputy', name: '副负责人', status: 'active', sort_order: 20 },
      ];
    }
    if (path.startsWith('/api/organization/leader-assignments?')) {
      return path.includes('org_unit_id=finance') ? [{
        id: 'leader_finance',
        tenant_id: 'tenant_a',
        org_unit_id: 'finance',
        employee_profile_id: 'profile_member',
        position_assignment_id: 'position_assignment',
        leader_type_code: 'primary',
        effective_from: '2026-07-28T08:00:00',
        effective_until: null,
        status: 'active',
        source_kind: 'manual',
        created_by_user_id: 'admin_a',
      }] : [];
    }
    if (path.startsWith('/api/sop-migrations/coverage?')) {
      return {
        tenant_id: 'tenant_a',
        total: 1,
        readiness_counts: { ready: 1, attention_required: 0, blocked: 0 },
        entries: [{
          skill_id: 'expense_approval',
          name: '费用审批',
          current_version: '2.0.0',
          requester_policy: 'active_tenant_member',
          requester_policy_explicit: false,
          dependency_assessment: {
            readiness: 'ready',
            issue_codes: [],
            human_task_count: 1,
            tool_operation_count: 1,
            knowledge_task_count: 0,
            bound_agent_count: 1,
            executable_agent_count: 1,
            human_participants: [{
              node_id: 'finance_review',
              role_codes: ['finance.approver'],
              action_permission_codes: ['expense.approve'],
              exclude_initiator: true,
              declared_direct_user_count: 0,
              direct_user_count: 0,
              eligible_candidate_count: 1,
              source_counts: { position_role: 1 },
              participant_scope_resolver: 'initiator_primary_org',
              participant_scope_org_unit_id: null,
              contextual_scope: true,
              context_count: 1,
              covered_context_count: 1,
              uncovered_org_unit_ids: [],
              issue_codes: [],
            }],
          },
        }],
      };
    }
    throw new Error(`unexpected request: ${path}`);
  });
});

it('shows the organization ledger, position history and position-derived role source', async () => {
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
        <OrganizationPage currentUser={administrator} />
      </EnterpriseContextProvider>
    </I18nProvider>,
  );

  await user.click(await screen.findByRole('treeitem', { name: /财务部/ }));
  expect(await screen.findByRole('button', { name: /财务审批岗/ })).toBeVisible();
  await user.click(screen.getByRole('button', { name: /财务审批岗/ }));

  expect(screen.getAllByText('成员一').length).toBeGreaterThan(0);
  expect(screen.getAllByText('财务审批人')).toHaveLength(2);
  expect(screen.getAllByText('主要负责人').length).toBeGreaterThan(0);
  expect(screen.getByText(/责任关系，不自动授予角色或权限/)).toBeVisible();
  expect(screen.getByText('岗位带入')).toBeVisible();
  expect(screen.getByText(/决定流程候选资格/)).toBeVisible();
  expect(screen.getByRole('region', { name: '岗位流程责任影响' })).toBeVisible();
  expect(screen.getByText('责任闭环轨道')).toBeVisible();
  expect(screen.getByText('覆盖 1/1 个组织')).toBeVisible();
  expect(api.get.mock.calls.length).toBeGreaterThanOrEqual(12);
});

it('keeps the organization tree visible when a related resource fails', async () => {
  const successfulGet = api.get.getMockImplementation();
  api.get.mockImplementation((path: string) => {
    if (path.startsWith('/api/organization/positions?')) {
      return Promise.reject(new Error('Not Found'));
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
        <OrganizationPage currentUser={administrator} />
      </EnterpriseContextProvider>
    </I18nProvider>,
  );

  expect(await screen.findByRole('treeitem', { name: /财务部/ })).toBeVisible();
});

it('keeps organization and member facts when leader history fails', async () => {
  const successfulGet = api.get.getMockImplementation();
  api.get.mockImplementation((path: string) => {
    if (path.startsWith('/api/organization/leader-assignments?')) {
      return Promise.reject(new Error('leader unavailable'));
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
        <OrganizationPage currentUser={administrator} />
      </EnterpriseContextProvider>
    </I18nProvider>,
  );

  expect(await screen.findByRole('treeitem', { name: /财务部/ })).toBeVisible();
  expect(screen.getByText('当前组织成员')).toBeVisible();
});

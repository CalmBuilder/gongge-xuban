import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, expect, it, vi } from 'vitest';

import type { EnterpriseAuthUser } from '../auth';
import { EnterpriseContextProvider } from '../enterprise-context';
import { I18nProvider } from '../i18n';
import EnterpriseInfoPage from './EnterpriseInfoPage';

vi.mock('../api/client', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn() },
}));

import { api } from '../api/client';
import type { OrganizationUnit, Position } from '../types/organization';

const member: EnterpriseAuthUser = {
  id: 'member_a',
  tenant_id: 'tenant_a',
  username: 'member',
  role: 'member',
  membership_status: 'active',
  member_category_code: 'employee',
};

const admin: EnterpriseAuthUser = {
  ...member,
  id: 'admin_a',
  username: 'admin',
  role: 'admin',
  governance_permission_codes: ['organization.read', 'organization.manage'],
};

const rootUnit: OrganizationUnit = {
  id: 'org_root',
  tenant_id: 'tenant_a',
  parent_id: null,
  code: 'tenant_a',
  name: '企业甲',
  unit_type_code: 'company',
  tree_path: 'org_root',
  depth: 0,
  sort_order: 0,
  is_root: true,
  status: 'active',
};

const financePosition: Position = {
  id: 'position_finance',
  tenant_id: 'tenant_a',
  org_unit_id: rootUnit.id,
  code: 'finance_member',
  name: '财务人员',
  position_type_code: 'specialist',
  reports_to_position_id: null,
  headcount_limit: null,
  responsibility: '承担财务人员职责。',
  status: 'active',
};

function renderPage(user: EnterpriseAuthUser) {
  return render(
    <MemoryRouter>
      <I18nProvider>
        <EnterpriseContextProvider
          value={{
            tenant: { id: 'tenant_a', name: '企业甲' },
            member: user,
            is_administrator: user.role === 'admin',
          }}
        >
          <EnterpriseInfoPage currentUser={user} />
        </EnterpriseContextProvider>
      </I18nProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.mocked(api.get).mockReset();
  vi.mocked(api.post).mockReset();
  vi.mocked(api.put).mockReset();
  vi.mocked(api.get).mockImplementation(async (path) => {
    if (path.startsWith('/api/organization/units?')) return [rootUnit] as never;
    if (path.startsWith('/api/organization/positions?')) return [financePosition] as never;
    if (path.startsWith('/api/enterprise/agents?')) {
      return [
        ['agent_finance', '财务'],
        ['agent_admin', '行政'],
        ['agent_hr', '人事'],
        ['agent_it', 'IT'],
        ['agent_legal', '法务'],
      ].map(([id, name]) => ({
        id,
        name,
        status: 'active',
        is_overall: false,
        responsible_org_unit_id: rootUnit.id,
      })) as never;
    }
    if (path.startsWith('/api/organization/agent-role-bindings?')) {
      return [
        ['agent_finance', '财务', '财务报销专员'],
        ['agent_admin', '行政', '用章申请操作员'],
        ['agent_hr', '人事', 'HR 假勤专员'],
        ['agent_it', 'IT', 'IT 权限开通操作员'],
        ['agent_legal', '法务', '法务合同风险分析员'],
      ].map(([agent_id, agent_name, role_name]) => ({
        agent_id,
        agent_name,
        role_name,
        assignment_mode: 'execute',
        supervisor_employee_name: agent_name === 'IT' ? '演示 IT 工程师' : '演示管理员',
        scope_type: 'tenant',
        scope_id: '*',
        status: 'active',
      })) as never;
    }
    if (path.startsWith('/api/organization/employee-role-assignments?')) {
      return [
        ['employee_admin', '演示管理员', '财务报销专员'],
        ['employee_admin', '演示管理员', 'HR 假勤专员'],
        ['employee_approver', '演示审批员', '用章审批人'],
        ['employee_approver', '演示审批员', '重要用章审批人'],
        ['employee_approver', '演示审批员', 'IT 高权限审批人'],
        ['employee_approver', '演示审批员', '法务合同复核专员'],
      ].map(([employee_profile_id, employee_name, role_name]) => ({
        employee_profile_id,
        employee_name,
        role_name,
        scope_type: 'tenant',
        scope_id: '*',
        status: 'active',
      })) as never;
    }
    if (path.startsWith('/api/organization/position-assignments?')) {
      return [{
        employee_profile_id: 'employee_admin',
        position_id: financePosition.id,
        status: 'active',
      }] as never;
    }
    if (path.startsWith('/api/organization/position-role-bindings?')) {
      return [{
        position_id: financePosition.id,
        business_role_name: '财务报销专员',
        status: 'active',
      }] as never;
    }
    if (path.includes('/api/enterprise/agents/') && path.includes('/skills?')) {
      const skillByAgent: Record<string, [string, string]> = {
        agent_finance: ['expense_travel_reimbursement', '差旅报销申请'],
        agent_admin: ['seal_application_approval', '用章申请审批'],
        agent_hr: ['leave_apply_v1', '请假申请'],
        agent_it: ['skill_perm_grant_routing_001', 'IT 权限开通'],
        agent_legal: ['contract_risk_review', '合同风险审查'],
      };
      const matched = Object.entries(skillByAgent).find(([agentId]) => path.includes(`/${agentId}/`));
      if (!matched) return [] as never;
      return [{
        skill_id: matched[1][0],
        name: matched[1][1],
        version: '2.1.1',
        status: 'published',
        branch_status: 'active',
      }] as never;
    }
    return [] as never;
  });
});

it('shows the authenticated tenant id as immutable to ordinary members', () => {
  renderPage(member);

  expect(screen.getByText('tenant_a')).toBeInTheDocument();
  expect(screen.getByDisplayValue('企业甲')).toBeDisabled();
  expect(screen.getByText(/普通成员可查看企业信息/)).toBeInTheDocument();
  const topology = screen.getByRole('region', {
    name: '真人、组织与岗位、组织角色、数字员工和专家拓扑图',
  });
  expect(within(topology).getByText('组织、真人与数字员工如何关联')).toBeInTheDocument();
  expect(within(topology).getAllByText('组织角色').length).toBeGreaterThan(0);
  expect(within(topology).getByText('专家 / 能力分身')).toBeInTheDocument();
  expect(within(topology).getByText('结构说明，不代表当前已绑定')).toBeInTheDocument();
  expect(within(topology).getByText('实线：核心关系')).toBeInTheDocument();
  expect(within(topology).getByText('虚线：能力分身等满足条件后成立')).toBeInTheDocument();
  expect(within(topology).getByText(/共用 8 条关系契约/)).toBeInTheDocument();
  expect(within(topology).getAllByText('SOP / 技能')).toHaveLength(2);
  expect(within(topology).getByText(/当前数据库没有可用于本图的来源绑定实例/)).toBeInTheDocument();
});

it('creates another enterprise as a company organization unit under the tenant root', async () => {
  const created = {
    ...rootUnit,
    id: 'org_subsidiary',
    parent_id: rootUnit.id,
    code: 'subsidiary_east',
    name: '华东子公司',
    tree_path: 'org_root/org_subsidiary',
    depth: 1,
    is_root: false,
  };
  vi.mocked(api.post).mockResolvedValue(created as never);
  renderPage(admin);

  const createButton = await screen.findByRole('button', { name: '新增企业' });
  const topology = screen.getByRole('region', {
    name: '真人、组织与岗位、组织角色、数字员工和专家拓扑图',
  });
  expect((await within(topology).findAllByText('财务报销专员')).length).toBeGreaterThan(0);
  expect(within(topology).getAllByRole('tab')).toHaveLength(5);
  const modelContract = topology.querySelector('[data-topology-contract="model"]');
  const liveContract = topology.querySelector('[data-topology-contract="live"]');
  expect(modelContract).not.toBeNull();
  expect(liveContract).not.toBeNull();
  const relationNames = (element: Element | null) => [...(element?.querySelectorAll('[data-topology-relation]') || [])]
    .map((item) => item.getAttribute('data-topology-relation'));
  expect(relationNames(liveContract)).toEqual(relationNames(modelContract));
  expect(relationNames(modelContract)).toHaveLength(8);
  expect(within(topology).getAllByText('组织单元包含岗位 · 已支持')).toHaveLength(1);
  expect(within(topology).getAllByText('真人任职于岗位 · 已验证')).toHaveLength(1);
  expect(within(topology).getAllByText('真人拥有能力分身 · 待配置')).toHaveLength(1);
  expect(within(topology).queryByText(/OrganizationUnit\.contains\.Position · 已支持/)).not.toBeInTheDocument();
  expect(within(topology).getByText(/责任组织包含岗位：“财务人员”；真人任职已验证/)).toBeInTheDocument();
  expect(liveContract?.querySelector('[data-topology-relation="PositionAssignment.assigns.Human"]')).toHaveAttribute('data-relation-status', 'verified');
  expect(liveContract?.querySelector('[data-topology-relation="Human.owns.ExpertClone"]')).toHaveAttribute('data-relation-status', 'missing');
  expect(within(topology).getByText(/治理监督人：“演示管理员”/)).toBeInTheDocument();
  expect(within(topology).getByText('一次业务如何在人与数字员工之间闭环')).toBeInTheDocument();
  expect(within(topology).getByText(/当前任职：财务报销专员：演示管理员/)).toBeInTheDocument();
  expect(within(topology).getByText(/处理结果与审计轨迹返回发起人/)).toBeInTheDocument();
  await userEvent.click(within(topology).getByRole('tab', { name: 'IT 权限开通' }));
  expect(within(topology).getByText('IT 权限开通操作员')).toBeInTheDocument();
  expect(within(topology).getByText('IT 高权限审批人')).toBeInTheDocument();
  expect(within(topology).getByText(/当前任职：IT 高权限审批人：演示审批员/)).toBeInTheDocument();
  expect(within(topology).getByText(/治理监督人：“演示 IT 工程师”/)).toBeInTheDocument();
  await userEvent.click(createButton);
  await userEvent.type(screen.getByRole('textbox', { name: '稳定企业编码' }), 'subsidiary_east');
  await userEvent.type(screen.getByRole('textbox', { name: '新增企业名称' }), '华东子公司');
  await userEvent.click(screen.getByRole('button', { name: '创建企业组织' }));

  expect(api.post).toHaveBeenCalledWith('/api/organization/units', {
    tenant_id: 'tenant_a',
    parent_id: 'org_root',
    code: 'subsidiary_east',
    name: '华东子公司',
    unit_type_code: 'company',
  });
  expect((await screen.findAllByText('华东子公司')).length).toBeGreaterThan(0);
});

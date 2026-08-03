import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, expect, it, vi } from 'vitest';

import { api, ApiError } from '../api/client';
import { I18nProvider } from '../i18n';
import OrganizationRolesPage from './OrganizationRolesPage';

vi.mock('../api/client', () => ({
  getRequestTenantId: () => 'tenant_demo',
  ApiError: class ApiError extends Error {
    status: number;
    body: string;

    constructor(status: number, body: string, statusText: string) {
      super(statusText);
      this.status = status;
      this.body = body;
    }
  },
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

const businessRole = {
  id: 'role-finance-reviewer',
  role_code: 'finance_reviewer',
  name: '财务复核人',
  role_kind: 'business',
  category: 'finance',
  permissions: ['it.ticket.claim'],
  status: 'active',
  employee_count: 2,
  agent_count: 0,
  created_at: '2026-07-22T08:00:00Z',
  updated_at: '2026-07-22T08:00:00Z',
};

const administrator = {
  id: 'administrator',
  tenant_id: 'tenant_demo',
  username: 'admin',
  role: 'admin' as const,
  membership_status: 'active' as const,
  member_category_code: 'employee',
  governance_permission_codes: ['authorization.read', 'authorization.manage'],
};

beforeEach(() => {
  vi.mocked(api.get).mockReset();
  vi.mocked(api.post).mockReset();
  vi.mocked(api.post).mockResolvedValue({} as never);
  vi.mocked(api.put).mockReset();
  vi.mocked(api.delete).mockReset();
  vi.mocked(api.get).mockImplementation(async (path: string) => {
    if (path.startsWith('/api/organization/business-roles/page')) {
      return {
        items: [businessRole], total: 1, active_count: 1, assignment_count: 2,
        page: 1, page_size: 20,
      } as never;
    }
    if (path.startsWith('/api/organization/business-role-options')) {
      return [{
        id: businessRole.id, role_code: businessRole.role_code, name: businessRole.name,
        role_kind: businessRole.role_kind,
      }] as never;
    }
    if (path.startsWith('/api/organization/agent-role-bindings')) {
      return [{
        id: 'binding-admin-assistant',
        agent_id: 'agent-001',
        agent_name: '行政事务管家',
        role_code: 'finance_reviewer',
        role_name: '财务复核人',
        assignment_mode: 'assist',
        supervisor_employee_profile_id: 'profile-supervisor',
        supervisor_employee_id: 'E100',
        supervisor_employee_name: '监督员工',
        scope_type: 'tenant',
        scope_id: '*',
        status: 'active',
        created_at: '2026-07-22T08:00:00Z',
        updated_at: '2026-07-22T08:00:00Z',
      }] as never;
    }
    if (path.startsWith('/api/enterprise/agents')) {
      return [
        { id: 'agent-overall', name: '开放广场资源池', status: 'active', is_overall: true },
        { id: 'agent-001', name: '行政事务管家', status: 'active', is_overall: false },
      ] as never;
    }
    if (path.startsWith('/api/organization/permission-definitions')) {
      return [{
        id: 'permission-it-claim',
        permission_code: 'it.ticket.claim',
        name: '认领 IT 工单',
        category: 'information_technology',
        resource: 'it.ticket',
        action: 'claim',
        description: '允许候选 IT 支持工程师认领待处理工单。',
        status: 'active',
      }] as never;
    }
    if (path.startsWith('/api/organization/role-categories')) {
      return [{
        id: 'category-it',
        code: 'information_technology',
        name: 'IT',
        description: '故障、权限和技术支持',
        role_code_prefix: 'it',
        status: 'active',
      }] as never;
    }
    if (path.startsWith('/api/organization/employee-role-assignments')) {
      return [{
        id: 'assignment-governance',
        employee_profile_id: 'profile-supervisor',
        user_id: 'supervisor',
        employee_id: 'E100',
        employee_name: '监督员工',
        role_code: 'governance_org_admin',
        role_name: '组织管理员',
        role_kind: 'governance',
        scope_type: 'org_unit',
        scope_id: 'org-finance',
        include_descendants: true,
        granted_by_user_id: 'administrator',
        grant_reason: '季度组织授权复核',
        status: 'active',
        effective_from: '2026-07-28T08:00:00Z',
        effective_until: '2026-10-28T08:00:00Z',
        created_at: '2026-07-28T08:00:00Z',
        updated_at: '2026-07-28T08:00:00Z',
      }] as never;
    }
    if (path.startsWith('/api/organization/effective-permissions')) {
      return [{
        permission_code: 'organization.manage',
        role_code: 'governance_tenant_owner',
        role_name: '租户所有者（兼容管理员）',
        source_kind: 'platform_admin_compat',
        source_id: 'administrator',
        scope_type: 'tenant',
        scope_id: '*',
        include_descendants: true,
      }] as never;
    }
    if (path.startsWith('/api/auth/users')) {
      return { items: [{
        id: 'supervisor',
        username: 'supervisor',
        employee_profile_id: 'profile-supervisor',
        employee_id: 'E100',
        employee_name: '监督员工',
      }], total: 1, page: 1, page_size: 100 } as never;
    }
    return [] as never;
  });
});

it('默认只展示业务角色工作区并明确平台管理员不继承业务权限', async () => {
  render(
    <I18nProvider>
      <MemoryRouter>
        <OrganizationRolesPage currentUser={administrator} />
      </MemoryRouter>
    </I18nProvider>,
  );

  expect((await screen.findAllByText('财务复核人')).length).toBeGreaterThan(0);
  expect(screen.getByText(/平台 admin 只负责配置/)).toBeInTheDocument();
  expect(screen.getByText(/2 个任职关系/)).toBeInTheDocument();
  expect(screen.queryByRole('table', { name: '权限点目录' })).not.toBeInTheDocument();
  expect(api.get).toHaveBeenCalledWith(
    '/api/organization/business-roles/page?tenant_id=tenant_demo&page=1&page_size=20',
  );
});

it('通过分页控件请求第二页角色', async () => {
  const originalImplementation = vi.mocked(api.get).getMockImplementation();
  vi.mocked(api.get).mockImplementation(async (path: string) => {
    if (path.startsWith('/api/organization/business-roles/page')) {
      const page = Number(new URL(path, 'http://localhost').searchParams.get('page') || 1);
      return {
        items: page === 1 ? [businessRole] : [{
          ...businessRole, id: 'role-page-2', role_code: 'finance_page_2', name: '第二页角色',
        }],
        total: 21, active_count: 21, assignment_count: 2, page, page_size: 20,
      } as never;
    }
    return originalImplementation?.(path) as never;
  });
  render(
    <I18nProvider><MemoryRouter><OrganizationRolesPage currentUser={administrator} /></MemoryRouter></I18nProvider>,
  );

  await screen.findAllByText('财务复核人');
  await userEvent.click(screen.getByRole('button', { name: '下一页' }));

  expect(await screen.findByText('第二页角色')).toBeInTheDocument();
  expect(api.get).toHaveBeenCalledWith(expect.stringContaining('page=2'));
});

it('编辑角色时从权限目录回填选择并解释稳定编码', async () => {
  const user = userEvent.setup();
  render(
    <I18nProvider>
      <MemoryRouter>
        <OrganizationRolesPage currentUser={administrator} />
      </MemoryRouter>
    </I18nProvider>,
  );

  await screen.findAllByText('财务复核人');
  await user.click(screen.getByRole('button', { name: '编辑角色 财务复核人' }));

  const dialog = screen.getByRole('dialog');
  expect(dialog).toHaveTextContent('编辑角色：财务复核人');
  expect(screen.getByRole('textbox', { name: '查询权限点' })).toBeInTheDocument();
  expect(within(dialog).getByText('认领 IT 工单')).toBeInTheDocument();
  expect(screen.getByRole('checkbox', { name: '选择权限 认领 IT 工单' })).toBeChecked();
  expect(screen.getByText(/建议以 cross_ 开头；创建后不可修改/)).toBeInTheDocument();
});

it('编辑数字员工绑定时可回填工作模式和人类监督者', async () => {
  const user = userEvent.setup();
  render(
    <I18nProvider>
      <MemoryRouter>
        <OrganizationRolesPage currentUser={administrator} />
      </MemoryRouter>
    </I18nProvider>,
  );

  await screen.findAllByText('财务复核人');
  await user.click(screen.getByRole('link', { name: /数字员工映射/ }));
  await screen.findByText('行政事务管家');
  await user.click(screen.getByRole('button', { name: '编辑数字员工绑定 行政事务管家 财务复核人' }));

  expect(screen.getByRole('dialog')).toHaveTextContent('编辑数字员工业务角色');
  expect(screen.getByRole('combobox', { name: '人类监督者' })).toHaveTextContent('监督员工');
});

it('按对象切换到权限工作区并引导生成可提交的稳定编码', async () => {
  const user = userEvent.setup();
  render(
    <I18nProvider>
      <MemoryRouter>
        <OrganizationRolesPage currentUser={administrator} />
      </MemoryRouter>
    </I18nProvider>,
  );

  await screen.findAllByText('财务复核人');
  await user.click(screen.getByRole('link', { name: /权限点/ }));
  expect(await screen.findByRole('heading', { name: '权限点' })).toBeInTheDocument();
  expect(screen.queryByRole('table', { name: '公司角色列表' })).not.toBeInTheDocument();

  await user.click(screen.getByRole('button', { name: '新增权限点' }));
  await user.click(screen.getByRole('button', { name: '创建权限点' }));
  expect(screen.getByText('请填写便于业务人员识别的中文名称。')).toBeInTheDocument();
  expect(screen.getByText('请填写被操作的业务对象。')).toBeInTheDocument();

  await user.type(screen.getByRole('textbox', { name: '权限名称' }), '审批采购单');
  await user.type(screen.getByRole('textbox', { name: '资源' }), 'purchase.order');
  await user.type(screen.getByRole('textbox', { name: '动作' }), 'approve');
  expect(screen.getByLabelText('权限稳定编码')).toHaveTextContent('purchase.order.approve');
  await user.click(screen.getByRole('button', { name: '创建权限点' }));

  expect(api.post).toHaveBeenCalledWith('/api/organization/permission-definitions', {
    tenant_id: 'tenant_demo',
    permission_code: 'purchase.order.approve',
    name: '审批采购单',
    category: 'information_technology',
    resource: 'purchase.order',
    action: 'approve',
    scope: null,
    description: null,
  });
});

it('权限编码冲突时在表单内说明修复方法并保留填写内容', async () => {
  vi.mocked(api.post).mockRejectedValueOnce(new ApiError(409, '', 'Conflict'));
  const user = userEvent.setup();
  render(
    <I18nProvider>
      <MemoryRouter initialEntries={['/enterprise/organization-roles?section=permissions']}>
        <OrganizationRolesPage currentUser={administrator} />
      </MemoryRouter>
    </I18nProvider>,
  );

  await screen.findByRole('heading', { name: '权限点' });
  await screen.findByText('认领 IT 工单');
  await user.click(screen.getByRole('button', { name: '新增权限点' }));
  await user.type(screen.getByRole('textbox', { name: '权限名称' }), '审批采购单');
  await user.type(screen.getByRole('textbox', { name: '资源' }), 'purchase.order');
  await user.type(screen.getByRole('textbox', { name: '动作' }), 'approve');
  await user.click(screen.getByRole('button', { name: '创建权限点' }));

  expect(await screen.findByText(/权限编码 purchase.order.approve 已存在/)).toBeInTheDocument();
  expect(screen.getByRole('textbox', { name: '权限名称' })).toHaveValue('审批采购单');
});

it('展示结构化成员授权和服务端有效权限解释', async () => {
  const user = userEvent.setup();
  render(
    <I18nProvider>
      <MemoryRouter>
        <OrganizationRolesPage currentUser={administrator} />
      </MemoryRouter>
    </I18nProvider>,
  );

  await screen.findAllByText('财务复核人');
  await user.click(screen.getByRole('link', { name: /成员授权/ }));
  expect(await screen.findByRole('table', { name: '成员角色授权列表' })).toHaveTextContent('组织管理员');
  expect(screen.getByRole('table', { name: '成员角色授权列表' })).toHaveTextContent('含下级');
  expect(screen.getByRole('table', { name: '成员角色授权列表' })).toHaveTextContent('季度组织授权复核');

  await user.click(screen.getByRole('button', { name: '授予成员角色' }));
  expect(await screen.findByRole('textbox', { name: '成员角色授权原因' })).toBeInTheDocument();
  expect((screen.getByLabelText('成员角色授权截止时间') as HTMLInputElement).value).toMatch(
    /^\d{4}-\d{2}-\d{2}T/,
  );
  await user.click(screen.getByRole('button', { name: '取消' }));

  await user.click(screen.getByRole('link', { name: /有效权限解释/ }));
  expect(await screen.findByRole('table', { name: '有效权限解释列表' })).toHaveTextContent('organization.manage');
  expect(screen.getByRole('table', { name: '有效权限解释列表' })).toHaveTextContent('平台管理员兼容');
});

it('数字员工角色选择排除开放广场资源池', async () => {
  const user = userEvent.setup();
  render(
    <I18nProvider>
      <MemoryRouter>
        <OrganizationRolesPage currentUser={administrator} />
      </MemoryRouter>
    </I18nProvider>,
  );

  await screen.findAllByText('财务复核人');
  await user.click(screen.getByRole('link', { name: /数字员工映射/ }));
  await user.click(await screen.findByRole('button', { name: '绑定业务角色' }));
  await user.click(screen.getAllByRole('combobox')[0]);

  expect(screen.getByRole('option', { name: '行政事务管家' })).toBeInTheDocument();
  expect(screen.queryByRole('option', { name: '开放广场资源池' })).not.toBeInTheDocument();
});

it('一个并行扩展接口失败时保留已成功的角色数据', async () => {
  const originalImplementation = vi.mocked(api.get).getMockImplementation();
  vi.mocked(api.get).mockImplementation(async (path: string) => {
    if (path.startsWith('/api/enterprise/agents')) throw new Error('agent unavailable');
    return originalImplementation?.(path) as never;
  });

  render(
    <I18nProvider>
      <MemoryRouter>
        <OrganizationRolesPage currentUser={administrator} />
      </MemoryRouter>
    </I18nProvider>,
  );

  expect((await screen.findAllByText('财务复核人')).length).toBeGreaterThan(0);
  expect(screen.getByRole('table', { name: '公司角色列表' })).toBeInTheDocument();
});

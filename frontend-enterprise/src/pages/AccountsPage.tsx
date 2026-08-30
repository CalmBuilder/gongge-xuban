import { useCallback, useDeferredValue, useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { User } from 'lucide-react';

import AppHeader from '@/components/AppHeader';
import { PageShell } from '@/components/enterprise/PageShell';
import { ConceptHelp } from '@/components/ConceptHelp';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { DataTable, type DataTableColumn } from '@/components/DataTable';
import { OrganizationTreeNavigator } from '@/components/OrganizationTreeNavigator';
import { Paginator } from '@/components/Paginator';
import {
  Dialog,
  DialogContent,
  DialogTitle,
  Checkbox,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
  Input,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui';
import { Button as UIButton } from '@/components/ui/button';
import { notify } from '@/components/ui/app-toast';
import { cn } from '@/lib/utils';
import { MENU_CONTENT_CLASS, MENU_ITEM_CLASS, MENU_ITEM_DANGER_CLASS, MOBILE_CARD_CLASS, formatDateTime } from '@/lib/enterprise-ui';

import { api, getRequestTenantId } from '../api/client';
import IconAccounts from '../assets/icons/sys-accounts.svg?react';
import IconAdd from '../assets/icons/add.svg?react';
import IconClear from '../assets/icons/field-clear.svg?react';
import IconEdit from '../assets/icons/edit.svg?react';
import IconMore from '../assets/icons/more.svg?react';
import IconRefresh from '../assets/icons/refresh.svg?react';
import IconSearch from '../assets/icons/search.svg?react';
import IconTrash from '../assets/icons/trash.svg?react';
import {
  hasGovernancePermission,
  isEnterpriseAdmin,
  type EnterpriseAuthUser,
} from '../auth';
import type { MemberPage, OrganizationUnit } from '../types/organization';

type EmployeeAccount = {
  id: string;
  tenant_id: string;
  username: string;
  display_name?: string;
  role: 'admin' | 'member';
  membership_status: 'active' | 'suspended' | 'left';
  member_category_code: string;
  joined_at?: string;
  left_at?: string;
  employee_profile_id?: string;
  employee_id?: string;
  employee_name?: string;
  department_id?: string;
  employee_status?: string;
  business_role_codes: string[];
  business_role_sources?: Record<string, string[]>;
  primary_org_unit_id?: string;
  primary_org_name?: string;
  primary_position_id?: string;
  primary_position_name?: string;
  assignment_history_count: number;
  created_at?: string;
  updated_at?: string;
};

type AccountDraft = {
  displayName: string;
  employeeId: string;
  employeeName: string;
  departmentId: string;
  password: string;
  role: 'admin' | 'member';
  membershipStatus: 'active' | 'suspended' | 'left';
  memberCategoryCode: string;
  businessRoleCodes: string[];
};

type AccountCreateDraft = {
  username: string;
  displayName: string;
  employeeId: string;
  employeeName: string;
  departmentId: string;
  password: string;
  role: 'admin' | 'member';
  memberCategoryCode: string;
  businessRoleCodes: string[];
};

type BusinessRoleOption = {
  role_code: string;
  name: string;
  category: string;
  permissions: string[];
};

type MemberCategoryOption = {
  code: string;
  name: string;
  status: 'active' | 'inactive';
};

const MEMBERSHIP_STATUS_LABELS = {
  active: '在职',
  suspended: '停用',
  left: '离职',
} as const;

const BUSINESS_ROLE_CATEGORY_LABELS: Record<string, string> = {
  administration: '行政用章',
  finance: '财务',
  human_resources: '人力资源',
  information_technology: '信息技术',
  legal_compliance: '法务合规',
  cross_functional: '跨部门通用',
};

const ACCOUNT_PAGE_SIZE = 10;

export default function AccountsPage({
  currentUser,
  onLogout,
}: {
  currentUser?: EnterpriseAuthUser;
  onLogout?: () => void;
} = {}) {
  const [rows, setRows] = useState<EmployeeAccount[]>([]);
  const [businessRoles, setBusinessRoles] = useState<BusinessRoleOption[]>([]);
  const [memberCategories, setMemberCategories] = useState<MemberCategoryOption[]>([]);
  const [loading, setLoading] = useState(false);
  const [searchText, setSearchText] = useState('');
  const deferredSearchText = useDeferredValue(searchText);
  const [selectedUnitId, setSelectedUnitId] = useState('');
  const [includeDescendants, setIncludeDescendants] = useState(false);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [editing, setEditing] = useState<EmployeeAccount | null>(null);
  const [draft, setDraft] = useState<AccountDraft>({
    displayName: '',
    employeeId: '',
    employeeName: '',
    departmentId: '',
    password: '',
    role: 'member',
    membershipStatus: 'active',
    memberCategoryCode: 'employee',
    businessRoleCodes: [],
  });
  const [saving, setSaving] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [createDraft, setCreateDraft] = useState<AccountCreateDraft>({
    username: '',
    displayName: '',
    employeeId: '',
    employeeName: '',
    departmentId: '',
    password: '',
    role: 'member',
    memberCategoryCode: 'employee',
    businessRoleCodes: [],
  });
  const [creating, setCreating] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<EmployeeAccount | null>(null);
  const [deleting, setDeleting] = useState(false);
  const canManageMembers = isEnterpriseAdmin(currentUser)
    || hasGovernancePermission(currentUser, 'member.manage');
  const canManageAuthorization = isEnterpriseAdmin(currentUser)
    || hasGovernancePermission(currentUser, 'authorization.manage');

  const loadCatalogs = useCallback(async () => {
    const tenantId = getRequestTenantId();
    const [roleOptionsResult, categoryOptionsResult] = await Promise.allSettled([
      api.get<BusinessRoleOption[]>(`/api/auth/business-roles?tenant_id=${tenantId}`),
      api.get<MemberCategoryOption[]>(`/api/auth/member-categories?tenant_id=${tenantId}`),
    ]);
    const failedResources: string[] = [];
    if (roleOptionsResult.status === 'fulfilled') setBusinessRoles(roleOptionsResult.value);
    else failedResources.push('业务角色');
    if (categoryOptionsResult.status === 'fulfilled') setMemberCategories(categoryOptionsResult.value);
    else failedResources.push('成员类别');
    if (failedResources.length) {
      notify.error(`部分成员配置加载失败：${failedResources.join('、')}。`);
    }
  }, []);

  const loadMembers = useCallback(async () => {
    setLoading(true);
    const tenantId = getRequestTenantId();
    try {
      const query = new URLSearchParams({
        tenant_id: tenantId,
        page: String(page),
        page_size: String(ACCOUNT_PAGE_SIZE),
      });
      if (deferredSearchText.trim()) query.set('keyword', deferredSearchText.trim());
      if (selectedUnitId) {
        query.set('org_unit_id', selectedUnitId);
        query.set('include_descendants', String(includeDescendants));
      }
      const result = await api.get<MemberPage<EmployeeAccount>>(`/api/auth/users/page?${query}`);
      setRows(result.items);
      setTotal(result.total);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '成员列表加载失败');
    } finally {
      setLoading(false);
    }
  }, [deferredSearchText, includeDescendants, page, selectedUnitId]);

  useEffect(() => {
    void loadCatalogs();
  }, [loadCatalogs]);

  useEffect(() => {
    void loadMembers();
  }, [loadMembers]);

  useEffect(() => {
    setPage(1);
  }, [deferredSearchText, includeDescendants, selectedUnitId]);

  const selectOrganization = useCallback((unit: OrganizationUnit) => {
    setSelectedUnitId(unit.id);
  }, []);

  async function load() {
    await Promise.all([loadCatalogs(), loadMembers()]);
  }

  function openEdit(row: EmployeeAccount) {
    setEditing(row);
    setDraft({
      displayName: row.display_name || row.username,
      employeeId: row.employee_id || '',
      employeeName: row.employee_name || row.display_name || row.username,
      departmentId: row.department_id || '',
      password: '',
      role: row.role,
      membershipStatus: row.membership_status,
      memberCategoryCode: row.member_category_code,
      businessRoleCodes: (row.business_role_codes || []).filter((code) => {
        const sources = row.business_role_sources?.[code];
        return !sources || sources.includes('business_role');
      }),
    });
  }

  function openCreate() {
    setCreateDraft({
      username: '',
      displayName: '',
      employeeId: '',
      employeeName: '',
      departmentId: '',
      password: '',
      role: 'member',
      memberCategoryCode: 'employee',
      businessRoleCodes: [],
    });
    setCreateOpen(true);
  }

  async function saveCreate() {
    const username = createDraft.username.trim();
    const password = createDraft.password.trim();
    if (!username || !password) {
      notify.error('请填写账号和密码');
      return;
    }
    if (selectedUnitId && !createDraft.employeeId.trim()) {
      notify.error('建立组织归属时必须填写工号');
      return;
    }
    setCreating(true);
    try {
      await api.post('/api/auth/users', {
        tenant_id: getRequestTenantId(),
        username,
        password,
        display_name: createDraft.displayName.trim() || username,
        employee_id: createDraft.employeeId.trim() || undefined,
        employee_name: createDraft.employeeName.trim() || undefined,
        department_id: createDraft.departmentId.trim() || undefined,
        initial_org_unit_id: selectedUnitId || undefined,
        role: createDraft.role,
        member_category_code: createDraft.memberCategoryCode,
        business_role_codes: createDraft.businessRoleCodes,
      });
      notify.success('账号已创建');
      setCreateOpen(false);
      await load();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '创建账号失败');
    } finally {
      setCreating(false);
    }
  }

  async function saveEdit() {
    if (!editing) return;
    setSaving(true);
    try {
      const isCurrentAccount = editing.id === currentUser?.id;
      await api.put(`/api/auth/users/${editing.id}`, {
        tenant_id: getRequestTenantId(),
        display_name: draft.displayName.trim() || editing.username,
        password: draft.password.trim() || undefined,
        ...(isCurrentAccount ? {} : {
          employee_id: draft.employeeId.trim(),
          employee_name: draft.employeeName.trim() || undefined,
          department_id: draft.departmentId.trim() || undefined,
          role: draft.role,
          membership_status: draft.membershipStatus,
          member_category_code: draft.memberCategoryCode,
          business_role_codes: draft.businessRoleCodes,
        }),
      });
      notify.success('账号已更新');
      setEditing(null);
      await load();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '保存账号失败');
    } finally {
      setSaving(false);
    }
  }

  async function confirmDelete() {
    const row = deleteTarget;
    if (!row) return;
    setDeleting(true);
    try {
      await api.put(`/api/auth/users/${row.id}`, {
        tenant_id: getRequestTenantId(),
        membership_status: 'left',
      });
      notify.success('已办理成员离职，历史记录继续保留');
      setDeleteTarget(null);
      await load();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '办理离职失败');
    } finally {
      setDeleting(false);
    }
  }

  function renderActions(row: EmployeeAccount) {
    if (!canManageMembers) return null;
    const isProtected =
      row.role === 'admin' || row.id === currentUser?.id || row.membership_status === 'left';
    return (
      <DropdownMenu>
        <DropdownMenuTrigger
          aria-label="账号操作"
          className="ml-auto grid size-7 place-items-center rounded-[8px] text-[#1a71ff] transition-colors outline-none hover:bg-black/5 hover:text-[#4a8dff] focus-visible:bg-black/5"
        >
          <IconMore className="size-3.5" />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className={MENU_CONTENT_CLASS}>
          <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => openEdit(row)}>
            <IconEdit />
            编辑
          </DropdownMenuItem>
          <DropdownMenuSeparator className="my-[2px] bg-[#eef0f4]" />
          <DropdownMenuItem
            variant="destructive"
            className={MENU_ITEM_DANGER_CLASS}
            disabled={isProtected}
            onSelect={() => setDeleteTarget(row)}
          >
            <IconTrash />
            办理离职
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    );
  }

  function currentAssignmentSummary(row: EmployeeAccount) {
    if (!row.employee_profile_id) return '未绑定员工档案';
    return `${row.primary_org_name || '待分配组织'} · ${row.primary_position_name || '待分配岗位'}`
      + `${row.assignment_history_count ? ` · ${row.assignment_history_count} 条任期` : ''}`;
  }

  const columns: DataTableColumn<EmployeeAccount>[] = [
    {
      key: 'username',
      title: '用户名',
      width: 220,
      className: 'text-[#18181a]',
      render: (row) => (
        <span className="flex min-w-0 items-center gap-[8px]">
          <span className="grid size-[24px] shrink-0 place-items-center rounded-full bg-[#eef1fb] text-[#7e96dc]">
            <User className="size-[14px]" />
          </span>
          <span className="truncate gg-type-meta font-medium">{row.username}</span>
        </span>
      ),
    },
    {
      key: 'display_name',
      title: '显示名',
      width: 200,
      render: (row) => <span className="block truncate">{row.display_name || row.username}</span>,
    },
    {
      key: 'employee_id',
      title: '工号',
      width: 130,
      render: (row) => <span>{row.employee_id || '未绑定'}</span>,
    },
    {
      key: 'role',
      title: '账号权限',
      width: 120,
      render: (row) => <span>{row.role === 'admin' ? '管理员' : '普通成员'}</span>,
    },
    {
      key: 'membership_status',
      title: '成员状态',
      width: 100,
      render: (row) => <span>{MEMBERSHIP_STATUS_LABELS[row.membership_status]}</span>,
    },
    {
      key: 'member_category',
      title: '成员类别',
      width: 120,
      render: (row) => (
        <span>
          {memberCategories.find((item) => item.code === row.member_category_code)?.name
            || row.member_category_code}
        </span>
      ),
    },
    {
      key: 'assignment',
      title: '当前任职',
      width: 230,
      render: (row) => (
        <span className="block truncate" title={currentAssignmentSummary(row)}>
          {currentAssignmentSummary(row)}
        </span>
      ),
    },
    {
      key: 'business_roles',
      title: '业务角色',
      width: 200,
      render: (row) => (
        <span className="block truncate">
          {(row.business_role_codes || []).map((code) => {
            const name = businessRoles.find((role) => role.role_code === code)?.name || code;
            return row.business_role_sources?.[code]?.includes('position_role')
              ? `${name}（岗位）`
              : name;
          }).join('、') || '未分配'}
        </span>
      ),
    },
    { key: 'joined', title: '加入时间', width: 180, render: (row) => formatDateTime(row.joined_at) },
    { key: 'left', title: '离开时间', width: 180, render: (row) => row.left_at ? formatDateTime(row.left_at) : '—' },
    { key: 'updated', title: '最近更新', width: 180, render: (row) => formatDateTime(row.updated_at) },
    {
      key: 'actions',
      title: '操作',
      width: 70,
      align: 'right',
      render: (row) => renderActions(row),
    },
  ];

  const renderMobileCard = (row: EmployeeAccount) => (
    <article className={MOBILE_CARD_CLASS} key={row.id}>
      <div className="flex min-w-0 items-start justify-between gap-[10px]">
        <span className="flex min-w-0 items-center gap-[8px]">
          <span className="grid size-[28px] shrink-0 place-items-center rounded-full bg-[#eef1fb] text-[#7e96dc]">
            <User className="size-[15px]" />
          </span>
          <span className="min-w-0">
            <strong className="block truncate gg-type-body font-semibold text-[#18181a]">{row.username}</strong>
            <span className="mt-[2px] block truncate gg-type-meta text-[#858b9c]">{row.display_name || row.username}</span>
            <span className="mt-[2px] block truncate gg-type-meta text-[#858b9c]">工号：{row.employee_id || '未绑定'}</span>
            <span className="mt-[2px] block truncate gg-type-meta text-[#858b9c]">
              {MEMBERSHIP_STATUS_LABELS[row.membership_status]} · {memberCategories.find((item) => item.code === row.member_category_code)?.name || row.member_category_code}
            </span>
            <span className="mt-[2px] block truncate gg-type-meta text-[#858b9c]">
              任职：{currentAssignmentSummary(row)}
            </span>
            <span className="mt-[2px] block truncate gg-type-meta text-[#858b9c]">
              业务角色：{(row.business_role_codes || []).map((code) => (
                businessRoles.find((role) => role.role_code === code)?.name || code
              )).join('、') || '未分配'}
            </span>
          </span>
        </span>
        {renderActions(row)}
      </div>
      <div className="mt-[10px] flex items-center justify-between gap-[10px] gg-type-meta text-[#858b9c]">
        <span>加入 {formatDateTime(row.joined_at)}</span>
        <span>更新 {formatDateTime(row.updated_at)}</span>
      </div>
    </article>
  );

  return (
    <PageShell template="management" aria-busy={loading}>
      <AppHeader
        onLogout={onLogout}
        userName={currentUser?.username}
        title={(
          <span className="inline-flex items-center gap-[5px]">
            成员管理（真人）
            <ConceptHelp topic="enterprise-member" />
          </span>
        )}
        description="管理登录账号、员工档案、组织归属和岗位任职；这里不管理 AI 数字员工。"
      />

      <div className="mt-[20px] mb-[16px] flex items-center justify-end gap-[12px]">
        <UIButton
          variant="outline"
          onClick={() => void load()}
          disabled={loading}
          className="h-[34px] gap-[4px] rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[20px] gg-type-meta font-normal text-[#757f9c] hover:border-[#cbd3e6] hover:bg-white hover:text-[#18181a]"
        >
          <IconRefresh className={cn('size-[14px]', loading && 'animate-spin')} />
          刷新
        </UIButton>
        {canManageMembers ? (
          <UIButton
            onClick={openCreate}
            className="h-[34px] gap-[4px] rounded-[var(--gg-radius-control)] bg-[var(--gg-cobalt)] px-[20px] gg-type-meta font-semibold text-white hover:bg-[#244bc7]"
          >
            <IconAdd className="size-[14px]" />
            新增成员
          </UIButton>
        ) : null}
      </div>

      <div className="grid grid-cols-[250px_minmax(0,1fr)] overflow-hidden rounded-[20px_20px_0_0] bg-white shadow-[0_-4px_16px_0_rgba(0,0,0,0.05)] max-[980px]:grid-cols-1">
        <aside className="border-r border-[#edf0f6] bg-[#fbfcff] p-[14px] max-[980px]:border-r-0 max-[980px]:border-b">
          <div className="mb-[10px] px-[3px]">
            <strong className="gg-type-meta font-semibold text-[#424b61]">按组织查看成员</strong>
            <p className="mt-[3px] gg-type-caption  text-[#8a93a8]">组织逐层加载，不下载整棵人员树。</p>
          </div>
          <OrganizationTreeNavigator
            onSelect={selectOrganization}
            selectedId={selectedUnitId}
            selectRootOnInitialize={false}
            tenantId={getRequestTenantId()}
          />
          <label className="mt-[12px] flex cursor-pointer items-center gap-[7px] rounded-[9px] border border-[#e5e9f2] bg-white px-[9px] py-[8px] gg-type-caption text-[#596174]">
            <Checkbox
              aria-label="包含下级组织成员"
              checked={includeDescendants}
              onCheckedChange={(checked) => setIncludeDescendants(checked === true)}
            />
            包含下级组织
          </label>
        </aside>
        <div className="flex min-w-0 flex-col gap-[18px] p-[18px_18px_24px_18px]">
          <div className="flex items-center gap-[6px] px-[12px] text-[#757f9c]">
            <IconAccounts className="size-[14px] shrink-0" />
            <span className="gg-type-body font-normal">企业成员</span>
          </div>

          <label className="flex h-[34px] w-[300px] items-center gap-[8px] overflow-hidden rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] transition-colors focus-within:border-[var(--gg-cobalt)] max-[900px]:w-full">
            <IconSearch className="size-[14px] shrink-0 text-[#858b9c]" />
            <input
              value={searchText}
              placeholder="搜索成员、账号、工号、状态或类别"
              onChange={(event) => setSearchText(event.target.value)}
              className="h-full min-w-0 flex-1 bg-transparent gg-type-meta text-[#17191f] outline-none placeholder:text-[#c0c6d4]"
            />
            {searchText && (
              <button
                type="button"
                aria-label="清除搜索"
                onClick={() => setSearchText('')}
                className="grid size-[16px] shrink-0 place-items-center text-[#c0c6d4] hover:text-[#858b9c]"
              >
                <IconClear className="size-[14px]" />
              </button>
            )}
          </label>

          <div className="grid gap-[10px] md:hidden">
            {rows.length ? (
              rows.map(renderMobileCard)
            ) : (
              <div className="py-[40px] text-center gg-type-control text-[#858b9c]">暂无成员</div>
            )}
          </div>

          <div className="hidden md:block">
            <DataTable
              aria-label="成员列表"
              columns={columns}
              data={rows}
              rowKey={(row) => row.id}
              loading={loading}
              emptyText="暂无成员"
            />
          </div>

          {total > 0 && (
            <Paginator
              aria-label="账号分页"
              className="mt-0 mb-[6px]"
              page={page}
              pageCount={Math.max(1, Math.ceil(total / ACCOUNT_PAGE_SIZE))}
              onChange={setPage}
            />
          )}
        </div>
      </div>

      <AccountDialog
        open={createOpen}
        title={selectedUnitId ? '新增成员并加入所选组织' : '新增成员'}
        loading={creating}
        submitText="创建"
        username={{ value: createDraft.username, onChange: (value) => setCreateDraft((prev) => ({ ...prev, username: value })) }}
        displayName={createDraft.displayName}
        onDisplayNameChange={(value) => setCreateDraft((prev) => ({ ...prev, displayName: value }))}
        employeeId={createDraft.employeeId}
        onEmployeeIdChange={(value) => setCreateDraft((prev) => ({ ...prev, employeeId: value }))}
        employeeName={createDraft.employeeName}
        onEmployeeNameChange={(value) => setCreateDraft((prev) => ({ ...prev, employeeName: value }))}
        departmentId={createDraft.departmentId}
        onDepartmentIdChange={(value) => setCreateDraft((prev) => ({ ...prev, departmentId: value }))}
        password={createDraft.password}
        onPasswordChange={(value) => setCreateDraft((prev) => ({ ...prev, password: value }))}
        role={createDraft.role}
        onRoleChange={(value) => setCreateDraft((prev) => ({ ...prev, role: value }))}
        membershipStatus="active"
        onMembershipStatusChange={() => undefined}
        memberCategoryCode={createDraft.memberCategoryCode}
        onMemberCategoryCodeChange={(value) => setCreateDraft((prev) => ({ ...prev, memberCategoryCode: value }))}
        memberCategories={memberCategories}
        businessRoles={canManageAuthorization ? businessRoles : []}
        businessRoleCodes={createDraft.businessRoleCodes}
        onBusinessRoleCodesChange={(value) => setCreateDraft((prev) => ({ ...prev, businessRoleCodes: value }))}
        roleDisabled={!canManageAuthorization}
        passwordLabel="初始密码"
        onClose={() => setCreateOpen(false)}
        onSubmit={() => void saveCreate()}
      />

      <AccountDialog
        open={Boolean(editing)}
        title={editing ? `编辑成员：${editing.username}` : '编辑成员'}
        loading={saving}
        submitText="保存"
        username={null}
        displayName={draft.displayName}
        onDisplayNameChange={(value) => setDraft((prev) => ({ ...prev, displayName: value }))}
        employeeId={draft.employeeId}
        onEmployeeIdChange={(value) => setDraft((prev) => ({ ...prev, employeeId: value }))}
        employeeName={draft.employeeName}
        onEmployeeNameChange={(value) => setDraft((prev) => ({ ...prev, employeeName: value }))}
        departmentId={draft.departmentId}
        onDepartmentIdChange={(value) => setDraft((prev) => ({ ...prev, departmentId: value }))}
        password={draft.password}
        onPasswordChange={(value) => setDraft((prev) => ({ ...prev, password: value }))}
        role={draft.role}
        onRoleChange={(value) => setDraft((prev) => ({ ...prev, role: value }))}
        membershipStatus={draft.membershipStatus}
        onMembershipStatusChange={(value) => setDraft((prev) => ({ ...prev, membershipStatus: value }))}
        memberCategoryCode={draft.memberCategoryCode}
        onMemberCategoryCodeChange={(value) => setDraft((prev) => ({ ...prev, memberCategoryCode: value }))}
        memberCategories={memberCategories}
        businessRoles={canManageAuthorization ? businessRoles : []}
        businessRoleCodes={draft.businessRoleCodes}
        onBusinessRoleCodesChange={(value) => setDraft((prev) => ({ ...prev, businessRoleCodes: value }))}
        roleDisabled={!canManageAuthorization || editing?.id === currentUser?.id}
        identityDisabled={editing?.id === currentUser?.id}
        passwordLabel="新密码"
        passwordPlaceholder="不修改请留空"
        onClose={() => setEditing(null)}
        onSubmit={() => void saveEdit()}
      />

      <ConfirmDialog
        open={Boolean(deleteTarget)}
        onOpenChange={(open) => !open && setDeleteTarget(null)}
        loading={deleting}
        title={deleteTarget ? `确认「${deleteTarget.username}」已离职？` : ''}
        description="离职后该成员无法登录和领取新任务；账号、历史会话、流程记录与数字员工所有权不会被删除。"
        onConfirm={() => void confirmDelete()}
      />
    </PageShell>
  );
}

function AccountDialog({
  open,
  title,
  loading,
  submitText,
  username,
  displayName,
  onDisplayNameChange,
  employeeId,
  onEmployeeIdChange,
  employeeName,
  onEmployeeNameChange,
  departmentId,
  onDepartmentIdChange,
  password,
  onPasswordChange,
  role,
  onRoleChange,
  membershipStatus,
  onMembershipStatusChange,
  memberCategoryCode,
  onMemberCategoryCodeChange,
  memberCategories,
  businessRoles,
  businessRoleCodes,
  onBusinessRoleCodesChange,
  roleDisabled = false,
  identityDisabled = false,
  passwordLabel,
  passwordPlaceholder,
  onClose,
  onSubmit,
}: {
  open: boolean;
  title: string;
  loading: boolean;
  submitText: string;
  username: { value: string; onChange: (value: string) => void } | null;
  displayName: string;
  onDisplayNameChange: (value: string) => void;
  employeeId: string;
  onEmployeeIdChange: (value: string) => void;
  employeeName: string;
  onEmployeeNameChange: (value: string) => void;
  departmentId: string;
  onDepartmentIdChange: (value: string) => void;
  password: string;
  onPasswordChange: (value: string) => void;
  role: 'admin' | 'member';
  onRoleChange: (value: 'admin' | 'member') => void;
  membershipStatus: 'active' | 'suspended' | 'left';
  onMembershipStatusChange: (value: 'active' | 'suspended' | 'left') => void;
  memberCategoryCode: string;
  onMemberCategoryCodeChange: (value: string) => void;
  memberCategories: MemberCategoryOption[];
  businessRoles: BusinessRoleOption[];
  businessRoleCodes: string[];
  onBusinessRoleCodesChange: (value: string[]) => void;
  roleDisabled?: boolean;
  identityDisabled?: boolean;
  passwordLabel: string;
  passwordPlaceholder?: string;
  onClose: () => void;
  onSubmit: () => void;
}) {
  const groupedBusinessRoles = businessRoles.reduce<Array<{ category: string; label: string; roles: BusinessRoleOption[] }>>(
    (groups, businessRole) => {
      const existing = groups.find((group) => group.category === businessRole.category);
      if (existing) {
        existing.roles.push(businessRole);
      } else {
        groups.push({
          category: businessRole.category,
          label: BUSINESS_ROLE_CATEGORY_LABELS[businessRole.category] ?? businessRole.category,
          roles: [businessRole],
        });
      }
      return groups;
    },
    [],
  );

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent
        aria-describedby={undefined}
        className="flex max-h-[calc(100vh-2rem)] w-[calc(100%-2rem)] flex-col gap-[12px] overflow-hidden rounded-[14px] px-[20px] py-[16px] sm:max-w-[520px]"
      >
        <div className="flex shrink-0 items-center gap-[6px] px-[12px] text-[#757f9c]">
          <IconAccounts className="size-[14px] shrink-0" />
          <DialogTitle className="gg-type-card-title font-normal text-[#757f9c]">
            {title}
          </DialogTitle>
        </div>

        <div className="flex min-h-0 flex-1 flex-col gap-[14px] overflow-y-auto px-[12px] pb-[2px]">
          {username && (
            <LabeledField label="用户名">
              <Input
                value={username.value}
                placeholder="例如 zhang_san"
                onChange={(event) => username.onChange(event.target.value)}
              />
            </LabeledField>
          )}
          <LabeledField label="显示名">
            <Input
              value={displayName}
              placeholder="例如 张三"
              onChange={(event) => onDisplayNameChange(event.target.value)}
            />
          </LabeledField>
          <LabeledField label="员工工号">
            <Input
              value={employeeId}
              disabled={identityDisabled}
              placeholder="例如 E001；留空表示不绑定"
              onChange={(event) => onEmployeeIdChange(event.target.value)}
            />
          </LabeledField>
          <LabeledField label="员工姓名">
            <Input
              value={employeeName}
              disabled={identityDisabled}
              placeholder="例如 张三"
              onChange={(event) => onEmployeeNameChange(event.target.value)}
            />
          </LabeledField>
          <LabeledField label="部门编号">
            <Input
              value={departmentId}
              disabled={identityDisabled}
              placeholder="可选，例如 FINANCE"
              onChange={(event) => onDepartmentIdChange(event.target.value)}
            />
          </LabeledField>
          <LabeledField label={passwordLabel}>
            <Input
              type="password"
              value={password}
              placeholder={passwordPlaceholder}
              onChange={(event) => onPasswordChange(event.target.value)}
            />
          </LabeledField>
          <LabeledField label="平台角色">
            <Select
              value={role}
              disabled={roleDisabled}
              onValueChange={(value) => onRoleChange(value as 'admin' | 'member')}
            >
              <SelectTrigger className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="member">普通成员</SelectItem>
                <SelectItem value="admin">管理员</SelectItem>
              </SelectContent>
            </Select>
          </LabeledField>
          {!username && (
            <LabeledField label="成员状态">
              <Select
                value={membershipStatus}
                disabled={identityDisabled}
                onValueChange={(value) => onMembershipStatusChange(
                  value as 'active' | 'suspended' | 'left',
                )}
              >
                <SelectTrigger className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="active">在职</SelectItem>
                  <SelectItem value="suspended">停用</SelectItem>
                  <SelectItem value="left">离职</SelectItem>
                </SelectContent>
              </Select>
            </LabeledField>
          )}
          <LabeledField label="成员类别">
            <Select
              value={memberCategoryCode}
              disabled={identityDisabled}
              onValueChange={onMemberCategoryCodeChange}
            >
              <SelectTrigger className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {memberCategories.map((item) => (
                  <SelectItem
                    key={item.code}
                    value={item.code}
                    disabled={item.status !== 'active' && item.code !== memberCategoryCode}
                  >
                    {item.name}{item.status === 'inactive' ? '（已停用）' : ''}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </LabeledField>
          <LabeledField
            label={
              <span className="flex items-center justify-between gap-[8px]">
                <span>公司业务角色</span>
                {businessRoleCodes.length > 0 && (
                  <span className="rounded-full bg-[#eef3ff] px-[8px] py-[1px] gg-type-caption font-medium  text-[var(--gg-cobalt)]">
                    已选 {businessRoleCodes.length} 项
                  </span>
                )}
              </span>
            }
          >
            {groupedBusinessRoles.length ? (
              <div className="flex max-h-[220px] flex-col gap-[10px] overflow-y-auto rounded-[10px] border border-[#e3e7f1] bg-[#fafbfd] p-[8px]">
                {groupedBusinessRoles.map((group) => (
                  <div key={group.category} className="flex flex-col gap-[6px]">
                    <div className="sticky top-[-8px] z-[1] mx-[-8px] bg-[#fafbfd] px-[8px] py-[4px] gg-type-caption font-semibold tracking-[0.04em] text-[#858b9c]">
                      {group.label}
                    </div>
                    <div className="grid grid-cols-2 gap-[6px] max-[560px]:grid-cols-1">
                      {group.roles.map((businessRole) => {
                        const checked = businessRoleCodes.includes(businessRole.role_code);
                        return (
                          <label
                            key={businessRole.role_code}
                            title={businessRole.role_code}
                            className={cn(
                              'flex cursor-pointer items-start gap-[8px] rounded-[8px] border bg-white p-[8px] transition-colors',
                              checked
                                ? 'border-[var(--gg-cobalt)] shadow-[0_0_0_0.5px_var(--gg-cobalt)]'
                                : 'border-[#e3e7f1] hover:border-[#cbd3e6]',
                              identityDisabled && 'opacity-60',
                            )}
                          >
                            <Checkbox
                              checked={checked}
                              disabled={identityDisabled}
                              className="mt-[1px]"
                              onCheckedChange={(next) => onBusinessRoleCodesChange(
                                next
                                  ? [...businessRoleCodes, businessRole.role_code]
                                  : businessRoleCodes.filter((code) => code !== businessRole.role_code),
                              )}
                            />
                            <span className="min-w-0 flex-1">
                              <span className="block truncate gg-type-meta font-medium  text-[#464c5e]">
                                {businessRole.name}
                              </span>
                              <span className="block truncate gg-type-caption  text-[#9aa1b5]">
                                {businessRole.role_code}
                              </span>
                            </span>
                          </label>
                        );
                      })}
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <span className="gg-type-meta text-[#858b9c]">当前尚无可分配业务角色</span>
            )}
          </LabeledField>
        </div>

        <div className="flex shrink-0 items-center justify-end gap-[8px] border-t border-[#eef1f6] px-[12px] pt-[12px]">
          <UIButton
            variant="outline"
            disabled={loading}
            onClick={onClose}
            className="h-[32px] w-[80px] rounded-[10px] border-[#e3e7f1] bg-white px-[12px] gg-type-body font-normal text-[#464c5e] hover:border-[#e3e7f1] hover:bg-[#f6f6f6] hover:text-[#18181a]"
          >
            取消
          </UIButton>
          <UIButton
            disabled={loading}
            onClick={onSubmit}
            className="h-[36px] w-[80px] rounded-[var(--gg-radius-control)] bg-[var(--gg-cobalt)] px-[12px] gg-type-body font-semibold text-white hover:bg-[#244bc7]"
          >
            {submitText}
          </UIButton>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function LabeledField({ label, children }: { label: ReactNode; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-[6px]">
      <span className="gg-type-meta font-medium text-[#464c5e]">{label}</span>
      {children}
    </label>
  );
}

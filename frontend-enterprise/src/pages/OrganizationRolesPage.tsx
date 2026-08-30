import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import {
  Bot,
  BriefcaseBusiness,
  ChevronRight,
  FolderTree,
  KeyRound,
  Search,
  ShieldCheck,
  UsersRound,
  X,
} from 'lucide-react';
import { useSearchParams } from 'react-router-dom';

import AppHeader from '@/components/AppHeader';
import { PageShell } from '@/components/enterprise/PageShell';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { DataTable, type DataTableColumn } from '@/components/DataTable';
import { OrganizationTreeNavigator } from '@/components/OrganizationTreeNavigator';
import { Paginator } from '@/components/Paginator';
import { RemoteMemberSelect } from '@/components/RemoteMemberSelect';
import SideNavPanel from '@/components/SideNavPanel';
import {
  Dialog,
  DialogContent,
  DialogTitle,
  Checkbox,
  Input,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui';
import { notify } from '@/components/ui/app-toast';
import { Button } from '@/components/ui/button';
import { formatDateTime } from '@/lib/enterprise-ui';
import type { OrganizationUnit } from '@/types/organization';

import { api, ApiError, getRequestTenantId } from '../api/client';
import { hasGovernancePermission, type EnterpriseAuthUser } from '../auth';

type BusinessRole = {
  id: string;
  role_code: string;
  name: string;
  role_kind: 'business' | 'governance';
  category: string;
  permissions: string[];
  status: string;
  employee_count: number;
  agent_count: number;
  created_at: string;
  updated_at: string;
};

type BusinessRolePage = {
  items: BusinessRole[];
  total: number;
  active_count: number;
  assignment_count: number;
  page: number;
  page_size: number;
};

type BusinessRoleOption = Pick<BusinessRole, 'id' | 'role_code' | 'name' | 'role_kind'>;

type RoleDraft = {
  roleCode: string;
  name: string;
  roleKind: 'business' | 'governance';
  category: string;
  permissions: string[];
};

type PermissionDefinition = {
  id: string;
  permission_code: string;
  name: string;
  category: string;
  resource: string;
  action: string;
  scope?: string;
  description?: string;
  status: string;
};

type RoleCategoryDefinition = {
  id: string;
  code: string;
  name: string;
  description: string;
  role_code_prefix: string;
  status: string;
};

type PermissionDraft = {
  name: string;
  category: string;
  resource: string;
  action: string;
  scope: string;
  description: string;
};

type CategoryDraft = {
  code: string;
  name: string;
  description: string;
  roleCodePrefix: string;
};

type AgentOption = { id: string; name: string; status: string; is_overall: boolean };
type AgentRoleBinding = {
  id: string;
  agent_id: string;
  agent_name: string;
  role_code: string;
  role_name: string;
  assignment_mode: string;
  supervisor_employee_profile_id?: string;
  supervisor_employee_id?: string;
  supervisor_employee_name?: string;
  scope_type: string;
  scope_id: string;
  include_descendants: boolean;
  granted_by_user_id?: string;
  status: string;
  effective_from?: string;
  effective_until?: string;
  created_at: string;
  updated_at: string;
};

type EmployeeRoleAssignment = {
  id: string;
  employee_profile_id: string;
  user_id: string;
  employee_id: string;
  employee_name?: string;
  role_code: string;
  role_name: string;
  role_kind: 'business' | 'governance';
  scope_type: 'tenant' | 'org_unit';
  scope_id: string;
  include_descendants: boolean;
  granted_by_user_id?: string;
  grant_reason?: string;
  status: string;
  effective_from?: string;
  effective_until?: string;
  created_at: string;
  updated_at: string;
};

type PermissionGrant = {
  permission_code: string;
  role_code: string;
  role_name: string;
  source_kind: string;
  source_id?: string;
  scope_type: 'tenant' | 'org_unit';
  scope_id: string;
  include_descendants: boolean;
  effective_from?: string;
  effective_until?: string;
  granted_by_user_id?: string;
};

const EMPTY_DRAFT: RoleDraft = {
  roleCode: '',
  name: '',
  roleKind: 'business',
  category: 'cross_functional',
  permissions: [],
};

const ROLE_CODE_PATTERN = /^[a-z][a-z0-9]*(?:_[a-z0-9]+){1,7}$/;
const PERMISSION_RESOURCE_PATTERN = /^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$/;
const PERMISSION_ACTION_PATTERN = /^[a-z][a-z0-9_]*$/;
const PERMISSION_SCOPE_PATTERN = /^[a-z0-9*][a-z0-9_*.-]*$/;
const EMPTY_PERMISSION_DRAFT: PermissionDraft = {
  name: '', category: 'cross_functional', resource: '', action: '', scope: '', description: '',
};
const EMPTY_CATEGORY_DRAFT: CategoryDraft = {
  code: '', name: '', description: '', roleCodePrefix: '',
};
const ROLE_PAGE_SIZE = 20;

type GovernanceSection = 'roles' | 'assignments' | 'effective' | 'permissions' | 'categories' | 'agents';

const GOVERNANCE_SECTIONS: Array<{
  key: GovernanceSection;
  label: string;
  description: string;
  icon: typeof BriefcaseBusiness;
}> = [
  { key: 'roles', label: '角色目录', description: '区分业务与治理职责', icon: BriefcaseBusiness },
  { key: 'assignments', label: '成员授权', description: '授予组织范围与有效期', icon: UsersRound },
  { key: 'effective', label: '有效权限解释', description: '核对来源与实际范围', icon: ShieldCheck },
  { key: 'permissions', label: '权限点', description: '定义原子操作契约', icon: KeyRound },
  { key: 'categories', label: '角色分类', description: '维护受控业务域', icon: FolderTree },
  { key: 'agents', label: '数字员工映射', description: '约束辅助与执行边界', icon: Bot },
];

function isGovernanceSection(value: string | null): value is GovernanceSection {
  return GOVERNANCE_SECTIONS.some((section) => section.key === value);
}

export default function OrganizationRolesPage({
  currentUser,
  onLogout,
}: {
  currentUser?: EnterpriseAuthUser;
  onLogout?: () => void;
}) {
  const [searchParams] = useSearchParams();
  const sectionParam = searchParams.get('section');
  const activeSection: GovernanceSection = isGovernanceSection(sectionParam) ? sectionParam : 'roles';
  const [roles, setRoles] = useState<BusinessRole[]>([]);
  const [roleOptions, setRoleOptions] = useState<BusinessRoleOption[]>([]);
  const [rolePage, setRolePage] = useState(1);
  const [roleTotal, setRoleTotal] = useState(0);
  const [activeCount, setActiveCount] = useState(0);
  const [roleAssignmentCount, setRoleAssignmentCount] = useState(0);
  const [agents, setAgents] = useState<AgentOption[]>([]);
  const [agentBindings, setAgentBindings] = useState<AgentRoleBinding[]>([]);
  const [employeeAssignments, setEmployeeAssignments] = useState<EmployeeRoleAssignment[]>([]);
  const [effectiveGrants, setEffectiveGrants] = useState<PermissionGrant[]>([]);
  const [explanationUserId, setExplanationUserId] = useState(currentUser?.id || '');
  const [permissionDefinitions, setPermissionDefinitions] = useState<PermissionDefinition[]>([]);
  const [roleCategories, setRoleCategories] = useState<RoleCategoryDefinition[]>([]);
  const [permissionSearch, setPermissionSearch] = useState('');
  const [catalogPermissionSearch, setCatalogPermissionSearch] = useState('');
  const [loading, setLoading] = useState(false);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [editingRole, setEditingRole] = useState<BusinessRole | null>(null);
  const [draft, setDraft] = useState<RoleDraft>(EMPTY_DRAFT);
  const [saving, setSaving] = useState(false);
  const [deactivateTarget, setDeactivateTarget] = useState<BusinessRole | null>(null);
  const [deactivating, setDeactivating] = useState(false);
  const [bindingDialogOpen, setBindingDialogOpen] = useState(false);
  const [bindingAgentId, setBindingAgentId] = useState('');
  const [bindingRoleCode, setBindingRoleCode] = useState('');
  const [bindingMode, setBindingMode] = useState<'assist' | 'execute'>('assist');
  const [bindingSupervisorId, setBindingSupervisorId] = useState('__none__');
  const [bindingScopeType, setBindingScopeType] = useState<'tenant' | 'org_unit'>('tenant');
  const [bindingOrg, setBindingOrg] = useState<OrganizationUnit | null>(null);
  const [bindingIncludesDescendants, setBindingIncludesDescendants] = useState(true);
  const [bindingEffectiveUntil, setBindingEffectiveUntil] = useState('');
  const [editingBinding, setEditingBinding] = useState<AgentRoleBinding | null>(null);
  const [bindingSaving, setBindingSaving] = useState(false);
  const [deactivatingBindingId, setDeactivatingBindingId] = useState<string | null>(null);
  const [permissionDialogOpen, setPermissionDialogOpen] = useState(false);
  const [editingPermission, setEditingPermission] = useState<PermissionDefinition | null>(null);
  const [permissionDraft, setPermissionDraft] = useState<PermissionDraft>(EMPTY_PERMISSION_DRAFT);
  const [permissionSaving, setPermissionSaving] = useState(false);
  const [permissionAttempted, setPermissionAttempted] = useState(false);
  const [permissionServerError, setPermissionServerError] = useState('');
  const [categoryDialogOpen, setCategoryDialogOpen] = useState(false);
  const [editingCategory, setEditingCategory] = useState<RoleCategoryDefinition | null>(null);
  const [categoryDraft, setCategoryDraft] = useState<CategoryDraft>(EMPTY_CATEGORY_DRAFT);
  const [categorySaving, setCategorySaving] = useState(false);
  const [catalogDeactivateTarget, setCatalogDeactivateTarget] = useState<
    { kind: 'permission'; item: PermissionDefinition }
    | { kind: 'category'; item: RoleCategoryDefinition }
    | null
  >(null);
  const [catalogDeactivating, setCatalogDeactivating] = useState(false);
  const [assignmentDialogOpen, setAssignmentDialogOpen] = useState(false);
  const [assignmentMemberId, setAssignmentMemberId] = useState('');
  const [assignmentRoleCode, setAssignmentRoleCode] = useState('');
  const [assignmentScopeType, setAssignmentScopeType] = useState<'tenant' | 'org_unit'>('org_unit');
  const [assignmentOrg, setAssignmentOrg] = useState<OrganizationUnit | null>(null);
  const [assignmentIncludesDescendants, setAssignmentIncludesDescendants] = useState(true);
  const [assignmentGrantReason, setAssignmentGrantReason] = useState('');
  const [assignmentEffectiveUntil, setAssignmentEffectiveUntil] = useState('');
  const [assignmentSaving, setAssignmentSaving] = useState(false);
  const [deactivatingAssignmentId, setDeactivatingAssignmentId] = useState<string | null>(null);
  const canManageAuthorization = hasGovernancePermission(currentUser, 'authorization.manage');

  const loadRolePage = useCallback(async () => {
    const result = await api.get<BusinessRolePage>(
      `/api/organization/business-roles/page?tenant_id=${getRequestTenantId()}`
      + `&page=${rolePage}&page_size=${ROLE_PAGE_SIZE}`,
    );
    setRoles(result.items);
    setRoleTotal(result.total);
    setActiveCount(result.active_count);
    setRoleAssignmentCount(result.assignment_count);
    const lastPage = Math.max(1, Math.ceil(result.total / result.page_size));
    if (rolePage > lastPage) setRolePage(lastPage);
  }, [rolePage]);

  const loadWorkspaceData = useCallback(async () => {
    setLoading(true);
    const [
      roleOptionResult,
      agentResult,
      bindingResult,
      permissionResult,
      categoryResult,
      assignmentResult,
      grantResult,
    ] = await Promise.allSettled([
        api.get<BusinessRoleOption[]>(`/api/organization/business-role-options?tenant_id=${getRequestTenantId()}`),
        api.get<AgentOption[]>(`/api/enterprise/agents?tenant_id=${getRequestTenantId()}`),
        api.get<AgentRoleBinding[]>(`/api/organization/agent-role-bindings?tenant_id=${getRequestTenantId()}`),
        api.get<PermissionDefinition[]>(`/api/organization/permission-definitions?tenant_id=${getRequestTenantId()}`),
        api.get<RoleCategoryDefinition[]>(`/api/organization/role-categories?tenant_id=${getRequestTenantId()}`),
        api.get<EmployeeRoleAssignment[]>(`/api/organization/employee-role-assignments?tenant_id=${getRequestTenantId()}`),
        api.get<PermissionGrant[]>(`/api/organization/effective-permissions?tenant_id=${getRequestTenantId()}`),
      ]);
    if (roleOptionResult.status === 'fulfilled') setRoleOptions(roleOptionResult.value);
    if (agentResult.status === 'fulfilled') {
      setAgents(agentResult.value.filter((agent) => agent.status === 'active' && !agent.is_overall));
    }
    if (bindingResult.status === 'fulfilled') setAgentBindings(bindingResult.value);
    if (permissionResult.status === 'fulfilled') setPermissionDefinitions(permissionResult.value);
    if (categoryResult.status === 'fulfilled') setRoleCategories(categoryResult.value);
    if (assignmentResult.status === 'fulfilled') setEmployeeAssignments(assignmentResult.value);
    if (grantResult.status === 'fulfilled') setEffectiveGrants(grantResult.value);
    const failedCount = [
      roleOptionResult,
      agentResult,
      bindingResult,
      permissionResult,
      categoryResult,
      assignmentResult,
      grantResult,
    ].filter((result) => result.status === 'rejected').length;
    if (failedCount) notify.error(`${failedCount} 类授权数据暂时无法加载，已保留其余成功数据`);
    setLoading(false);
  }, []);

  const refreshAll = useCallback(async () => {
    await Promise.all([loadRolePage(), loadWorkspaceData()]);
  }, [loadRolePage, loadWorkspaceData]);

  useEffect(() => {
    void loadWorkspaceData();
  }, [loadWorkspaceData]);

  useEffect(() => {
    setLoading(true);
    void loadRolePage()
      .catch((error) => notify.error(error instanceof Error ? error.message : '角色目录加载失败'))
      .finally(() => setLoading(false));
  }, [loadRolePage]);

  useEffect(() => {
    if (!explanationUserId || explanationUserId === currentUser?.id) return;
    let cancelled = false;
    void api.get<PermissionGrant[]>(
      `/api/organization/effective-permissions?tenant_id=${getRequestTenantId()}`
      + `&user_id=${encodeURIComponent(explanationUserId)}`,
    ).then((rows) => {
      if (!cancelled) setEffectiveGrants(rows);
    }).catch((error) => {
      if (!cancelled) notify.error(error instanceof Error ? error.message : '有效权限解释加载失败');
    });
    return () => {
      cancelled = true;
    };
  }, [currentUser?.id, explanationUserId]);

  const visiblePermissions = useMemo(() => {
    const query = permissionSearch.trim().toLocaleLowerCase();
    return permissionDefinitions.filter((permission) => (
      (permission.category === 'governance') === (draft.roleKind === 'governance')
      && (!query
      || `${permission.permission_code} ${permission.name} ${permission.description || ''}`
        .toLocaleLowerCase()
        .includes(query))
    ));
  }, [draft.roleKind, permissionDefinitions, permissionSearch]);
  const catalogPermissions = useMemo(() => {
    const query = catalogPermissionSearch.trim().toLocaleLowerCase();
    if (!query) return permissionDefinitions;
    return permissionDefinitions.filter((permission) => (
      `${permission.permission_code} ${permission.name} ${permission.description || ''}`
        .toLocaleLowerCase()
        .includes(query)
    ));
  }, [catalogPermissionSearch, permissionDefinitions]);
  const selectedCategory = useMemo(
    () => roleCategories.find((category) => category.code === draft.category),
    [draft.category, roleCategories],
  );
  const permissionCode = useMemo(() => {
    const resource = permissionDraft.resource.trim();
    const action = permissionDraft.action.trim();
    const scope = permissionDraft.scope.trim();
    if (!resource && !action) return '';
    return `${resource || 'resource'}.${action || 'action'}${scope ? `:${scope}` : ''}`;
  }, [permissionDraft.action, permissionDraft.resource, permissionDraft.scope]);
  const permissionErrors = useMemo(() => {
    const errors: Partial<Record<keyof PermissionDraft, string>> = {};
    const name = permissionDraft.name.trim();
    const resource = permissionDraft.resource.trim();
    const action = permissionDraft.action.trim();
    const scope = permissionDraft.scope.trim();
    if (!name) errors.name = '请填写便于业务人员识别的中文名称。';
    if (!permissionDraft.category) errors.category = '请选择权限所属的业务域。';
    if (!resource) {
      errors.resource = '请填写被操作的业务对象。';
    } else if (!PERMISSION_RESOURCE_PATTERN.test(resource)) {
      errors.resource = '仅支持小写英文、数字、下划线和“.”分层，例如 purchase.order。';
    }
    if (!action) {
      errors.action = '请填写允许执行的原子动作。';
    } else if (!PERMISSION_ACTION_PATTERN.test(action)) {
      errors.action = '动作需以小写字母开头，仅使用小写英文、数字和下划线。';
    }
    if (scope && !PERMISSION_SCOPE_PATTERN.test(scope)) {
      errors.scope = '作用域仅支持小写英文、数字、*、点、短横线和下划线。';
    }
    return errors;
  }, [permissionDraft]);

  function openCreate() {
    setEditingRole(null);
    setDraft({
      ...EMPTY_DRAFT,
      category: roleCategories.find((category) => category.code !== 'governance')?.code
        || EMPTY_DRAFT.category,
    });
    setPermissionSearch('');
    setDialogOpen(true);
  }

  function openEdit(role: BusinessRole) {
    setEditingRole(role);
    setDraft({
      roleCode: role.role_code,
      name: role.name,
      roleKind: role.role_kind,
      category: role.category,
      permissions: role.permissions,
    });
    setPermissionSearch('');
    setDialogOpen(true);
  }

  async function saveRole() {
    const name = draft.name.trim();
    const roleCode = draft.roleCode.trim();
    if (!name || !roleCode) {
      notify.error('请填写角色名称和角色编码');
      return;
    }
    if (!ROLE_CODE_PATTERN.test(roleCode)) {
      notify.error('角色编码需使用“业务域_职责”的小写格式，例如 admin_seal_approver');
      return;
    }
    setSaving(true);
    try {
      if (editingRole) {
        await api.put(`/api/organization/business-roles/${editingRole.id}`, {
          tenant_id: getRequestTenantId(),
          name,
          category: draft.category,
          permissions: draft.permissions,
        });
        notify.success('业务角色已更新');
      } else {
        await api.post('/api/organization/business-roles', {
          tenant_id: getRequestTenantId(),
          role_code: roleCode,
          name,
          role_kind: draft.roleKind,
          category: draft.category,
          permissions: draft.permissions,
        });
        notify.success(draft.roleKind === 'governance' ? '治理角色已创建' : '业务角色已创建');
      }
      setDialogOpen(false);
      await refreshAll();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '保存业务角色失败');
    } finally {
      setSaving(false);
    }
  }

  function togglePermission(permissionCode: string, checked: boolean) {
    setDraft((current) => ({
      ...current,
      permissions: checked
        ? Array.from(new Set([...current.permissions, permissionCode])).sort()
        : current.permissions.filter((code) => code !== permissionCode),
    }));
  }

  async function confirmDeactivate() {
    if (!deactivateTarget) return;
    setDeactivating(true);
    try {
      await api.delete(
        `/api/organization/business-roles/${deactivateTarget.id}?tenant_id=${getRequestTenantId()}`,
      );
      notify.success('业务角色已停用，历史任职和流程引用继续保留');
      setDeactivateTarget(null);
      await refreshAll();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '停用业务角色失败');
    } finally {
      setDeactivating(false);
    }
  }

  function openBindingDialog(binding?: AgentRoleBinding) {
    setEditingBinding(binding || null);
    setBindingAgentId(binding?.agent_id || agents[0]?.id || '');
    setBindingRoleCode(
      binding?.role_code
      || roleOptions.find((role) => role.role_kind === 'business')?.role_code
      || '',
    );
    setBindingMode(binding?.assignment_mode === 'execute' ? 'execute' : 'assist');
    setBindingSupervisorId(binding?.supervisor_employee_profile_id || '__none__');
    setBindingScopeType(binding?.scope_type === 'org_unit' ? 'org_unit' : 'tenant');
    setBindingOrg(null);
    setBindingIncludesDescendants(binding?.include_descendants ?? true);
    setBindingEffectiveUntil(toLocalDateTimeInput(binding?.effective_until));
    setBindingDialogOpen(true);
  }

  async function saveAgentBinding() {
    if (!bindingAgentId || !bindingRoleCode) {
      notify.error('请选择数字员工和公司业务角色');
      return;
    }
    if (!editingBinding && bindingScopeType === 'org_unit' && !bindingOrg) {
      notify.error('请选择数字员工可工作的组织范围');
      return;
    }
    setBindingSaving(true);
    try {
      if (editingBinding) {
        await api.put(`/api/organization/agent-role-bindings/${editingBinding.id}`, {
          tenant_id: getRequestTenantId(),
          assignment_mode: bindingMode,
          supervisor_employee_profile_id: bindingSupervisorId === '__none__' ? null : bindingSupervisorId,
          effective_until: bindingEffectiveUntil
            ? new Date(bindingEffectiveUntil).toISOString()
            : null,
        });
      } else {
        await api.post('/api/organization/agent-role-bindings', {
          tenant_id: getRequestTenantId(),
          agent_id: bindingAgentId,
          role_code: bindingRoleCode,
          assignment_mode: bindingMode,
          supervisor_employee_profile_id: bindingSupervisorId === '__none__' ? null : bindingSupervisorId,
          scope_type: bindingScopeType,
          scope_id: bindingScopeType === 'tenant' ? '*' : bindingOrg?.id,
          include_descendants: bindingScopeType === 'tenant' || bindingIncludesDescendants,
          effective_until: bindingEffectiveUntil
            ? new Date(bindingEffectiveUntil).toISOString()
            : undefined,
        });
      }
      notify.success(editingBinding ? '数字员工角色绑定已更新' : '数字员工角色绑定已保存');
      setBindingDialogOpen(false);
      await refreshAll();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '保存数字员工角色绑定失败');
    } finally {
      setBindingSaving(false);
    }
  }

  async function deactivateAgentBinding(binding: AgentRoleBinding) {
    setDeactivatingBindingId(binding.id);
    try {
      await api.delete(`/api/organization/agent-role-bindings/${binding.id}?tenant_id=${getRequestTenantId()}`);
      notify.success('数字员工角色绑定已停用');
      await refreshAll();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '停用数字员工角色绑定失败');
    } finally {
      setDeactivatingBindingId(null);
    }
  }

  function openPermissionDialog(permission?: PermissionDefinition) {
    setEditingPermission(permission || null);
    setPermissionDraft(permission ? {
      name: permission.name,
      category: permission.category,
      resource: permission.resource,
      action: permission.action,
      scope: permission.scope || '',
      description: permission.description || '',
    } : {
      ...EMPTY_PERMISSION_DRAFT,
      category: roleCategories.find((category) => category.status !== 'inactive')?.code
        || EMPTY_PERMISSION_DRAFT.category,
    });
    setPermissionAttempted(false);
    setPermissionServerError('');
    setPermissionDialogOpen(true);
  }

  async function savePermission() {
    const resource = permissionDraft.resource.trim();
    const action = permissionDraft.action.trim();
    setPermissionAttempted(true);
    setPermissionServerError('');
    if (Object.keys(permissionErrors).length) {
      const invalidFieldName = permissionErrors.name
        ? 'permission-name'
        : permissionErrors.resource
          ? 'permission-resource'
          : 'permission-action';
      document.querySelector<HTMLInputElement>(`[name="${invalidFieldName}"]`)?.focus();
      return;
    }
    setPermissionSaving(true);
    try {
      if (editingPermission) {
        await api.put(`/api/organization/permission-definitions/${editingPermission.id}`, {
          tenant_id: getRequestTenantId(),
          name: permissionDraft.name.trim(),
          description: permissionDraft.description.trim() || null,
        });
      } else {
        await api.post('/api/organization/permission-definitions', {
          tenant_id: getRequestTenantId(),
          permission_code: permissionCode,
          name: permissionDraft.name.trim(),
          category: permissionDraft.category,
          resource,
          action,
          scope: permissionDraft.scope.trim() || null,
          description: permissionDraft.description.trim() || null,
        });
      }
      notify.success(editingPermission ? '权限点已更新' : '权限点已创建');
      setPermissionDialogOpen(false);
      await refreshAll();
    } catch (error) {
      const errorMessage = error instanceof ApiError && error.status === 409
        ? `权限编码 ${permissionCode} 已存在。请修改资源、动作或作用域，或编辑已有权限点。`
        : error instanceof Error
          ? error.message
          : '保存权限点失败，请检查填写内容后重试。';
      setPermissionServerError(errorMessage);
      notify.error('权限点未保存，请按表单中的提示修正');
    } finally {
      setPermissionSaving(false);
    }
  }

  function openCategoryDialog(category?: RoleCategoryDefinition) {
    setEditingCategory(category || null);
    setCategoryDraft(category ? {
      code: category.code,
      name: category.name,
      description: category.description,
      roleCodePrefix: category.role_code_prefix,
    } : EMPTY_CATEGORY_DRAFT);
    setCategoryDialogOpen(true);
  }

  async function saveCategory() {
    if (!categoryDraft.name.trim() || !categoryDraft.code.trim() || !categoryDraft.roleCodePrefix.trim()) {
      notify.error('请填写分类名称、分类编码和角色前缀');
      return;
    }
    setCategorySaving(true);
    try {
      if (editingCategory) {
        await api.put(`/api/organization/role-categories/${editingCategory.id}`, {
          tenant_id: getRequestTenantId(),
          name: categoryDraft.name.trim(),
          description: categoryDraft.description.trim() || null,
          role_code_prefix: categoryDraft.roleCodePrefix.trim(),
        });
      } else {
        await api.post('/api/organization/role-categories', {
          tenant_id: getRequestTenantId(),
          code: categoryDraft.code.trim(),
          name: categoryDraft.name.trim(),
          description: categoryDraft.description.trim() || null,
          role_code_prefix: categoryDraft.roleCodePrefix.trim(),
        });
      }
      notify.success(editingCategory ? '角色分类已更新' : '角色分类已创建');
      setCategoryDialogOpen(false);
      await refreshAll();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '保存角色分类失败');
    } finally {
      setCategorySaving(false);
    }
  }

  async function confirmCatalogDeactivate() {
    if (!catalogDeactivateTarget) return;
    setCatalogDeactivating(true);
    try {
      const path = catalogDeactivateTarget.kind === 'permission'
        ? `/api/organization/permission-definitions/${catalogDeactivateTarget.item.id}`
        : `/api/organization/role-categories/${catalogDeactivateTarget.item.id}`;
      await api.delete(`${path}?tenant_id=${getRequestTenantId()}`);
      notify.success(catalogDeactivateTarget.kind === 'permission' ? '权限点已停用' : '角色分类已停用');
      setCatalogDeactivateTarget(null);
      await refreshAll();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '停用失败；请先解除活动引用');
    } finally {
      setCatalogDeactivating(false);
    }
  }

  function openAssignmentDialog() {
    const firstRole = roleOptions[0];
    setAssignmentMemberId('');
    setAssignmentRoleCode(firstRole?.role_code || '');
    setAssignmentScopeType('org_unit');
    setAssignmentOrg(null);
    setAssignmentIncludesDescendants(true);
    setAssignmentGrantReason('');
    setAssignmentEffectiveUntil(toLocalDateTimeInput(
      new Date(Date.now() + 90 * 24 * 60 * 60 * 1000).toISOString(),
    ));
    setAssignmentDialogOpen(true);
  }

  async function saveAssignment() {
    if (!assignmentMemberId || !assignmentRoleCode) {
      notify.error('请选择成员和角色');
      return;
    }
    if (assignmentScopeType === 'org_unit' && !assignmentOrg) {
      notify.error('请选择授权组织');
      return;
    }
    if (assignmentGrantReason.trim().length < 4) {
      notify.error('请填写至少 4 个字的授权原因');
      return;
    }
    if (!assignmentEffectiveUntil) {
      notify.error('请选择授权截止时间');
      return;
    }
    setAssignmentSaving(true);
    try {
      await api.post('/api/organization/employee-role-assignments', {
        tenant_id: getRequestTenantId(),
        employee_profile_id: assignmentMemberId,
        role_code: assignmentRoleCode,
        scope_type: assignmentScopeType,
        scope_id: assignmentScopeType === 'tenant' ? '*' : assignmentOrg?.id,
        include_descendants: assignmentScopeType === 'tenant'
          ? true
          : assignmentIncludesDescendants,
        grant_reason: assignmentGrantReason.trim(),
        effective_until: new Date(assignmentEffectiveUntil).toISOString(),
      });
      notify.success('成员角色授权已保存');
      setAssignmentDialogOpen(false);
      await refreshAll();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '成员角色授权保存失败');
    } finally {
      setAssignmentSaving(false);
    }
  }

  async function deactivateAssignment(assignment: EmployeeRoleAssignment) {
    setDeactivatingAssignmentId(assignment.id);
    try {
      await api.delete(
        `/api/organization/employee-role-assignments/${assignment.id}`
        + `?tenant_id=${getRequestTenantId()}`,
      );
      notify.success('成员角色授权已停用，历史记录继续保留');
      await refreshAll();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '停用成员角色授权失败');
    } finally {
      setDeactivatingAssignmentId(null);
    }
  }

  const columns: DataTableColumn<BusinessRole>[] = [
    {
      key: 'name',
      title: '公司角色',
      width: 230,
      render: (role) => (
        <span className="grid min-w-0 gap-[2px]">
          <strong className="truncate gg-type-control font-semibold text-[#18181a]">{role.name}</strong>
          <span className="truncate font-mono gg-type-caption text-[#858b9c]">{role.role_code}</span>
        </span>
      ),
    },
    {
      key: 'kind',
      title: '角色类型',
      width: 110,
      render: (role) => (
        <span className={role.role_kind === 'governance' ? 'text-[#3157e8]' : 'text-[#596174]'}>
          {role.role_kind === 'governance' ? '治理角色' : '业务角色'}
        </span>
      ),
    },
    {
      key: 'permissions',
      title: '权限点',
      width: 280,
      render: (role) => (
        <span className="block truncate gg-type-meta text-[#464c5e]">
          {role.permissions.join('、') || '未配置'}
        </span>
      ),
    },
    {
      key: 'assignments',
      title: '当前任职',
      width: 120,
      render: (role) => <span>{role.employee_count} 人</span>,
    },
    {
      key: 'agents',
      title: '数字员工',
      width: 120,
      render: (role) => <span>{role.agent_count} 个</span>,
    },
    {
      key: 'status',
      title: '状态',
      width: 100,
      render: (role) => (
        <span className={role.status === 'active' ? 'text-[#018434]' : 'text-[#858b9c]'}>
          {role.status === 'active' ? '有效' : '已停用'}
        </span>
      ),
    },
    {
      key: 'updated',
      title: '最近更新',
      width: 180,
      render: (role) => formatDateTime(role.updated_at),
    },
    {
      key: 'actions',
      title: '操作',
      width: 170,
      align: 'right',
      render: (role) => (
        <span className="flex justify-end gap-[8px]">
          {canManageAuthorization ? <Button aria-label={`编辑角色 ${role.name}`} variant="outline" size="sm" onClick={() => openEdit(role)}>编辑</Button> : null}
          {canManageAuthorization && role.status === 'active' ? (
            <Button variant="outline" size="sm" onClick={() => setDeactivateTarget(role)}>停用</Button>
          ) : null}
        </span>
      ),
    },
  ];

  const assignmentColumns: DataTableColumn<EmployeeRoleAssignment>[] = [
    {
      key: 'member',
      title: '成员',
      width: 180,
      render: (assignment) => (
        <span className="grid">
          <strong className="gg-type-meta">{assignment.employee_name || assignment.employee_id}</strong>
          <small className="font-mono gg-type-caption text-[#858b9c]">{assignment.employee_id}</small>
        </span>
      ),
    },
    {
      key: 'role',
      title: '来源角色',
      width: 230,
      render: (assignment) => (
        <span className="grid">
          <strong className="gg-type-meta">{assignment.role_name}</strong>
          <small className="font-mono gg-type-caption text-[#526183]">{assignment.role_code}</small>
        </span>
      ),
    },
    {
      key: 'kind',
      title: '类型',
      width: 100,
      render: (assignment) => assignment.role_kind === 'governance' ? '治理' : '业务',
    },
    {
      key: 'scope',
      title: '组织范围',
      width: 230,
      render: (assignment) => assignment.scope_type === 'tenant'
        ? '全企业'
        : `${assignment.scope_id}${assignment.include_descendants ? '（含下级）' : '（仅本组织）'}`,
    },
    {
      key: 'source',
      title: '授权依据',
      width: 260,
      render: (assignment) => (
        <span className="grid gap-[2px]">
          <span>{assignment.grant_reason || '历史兼容授权'}</span>
          <small className="gg-type-caption text-[#858b9c]">授予人：{assignment.granted_by_user_id || '历史数据'}</small>
        </span>
      ),
    },
    {
      key: 'validity',
      title: '有效期',
      width: 210,
      render: (assignment) => (
        `${assignment.effective_from ? formatDateTime(assignment.effective_from) : '立即生效'}`
        + ` → ${assignment.effective_until ? formatDateTime(assignment.effective_until) : '长期'}`
      ),
    },
    {
      key: 'actions',
      title: '操作',
      width: 100,
      align: 'right',
      render: (assignment) => canManageAuthorization && assignment.status === 'active' ? (
        <Button
          disabled={deactivatingAssignmentId === assignment.id}
          onClick={() => void deactivateAssignment(assignment)}
          size="sm"
          variant="outline"
        >
          停用
        </Button>
      ) : null,
    },
  ];

  const grantColumns: DataTableColumn<PermissionGrant>[] = [
    {
      key: 'permission',
      title: '有效权限',
      width: 230,
      render: (grant) => <span className="font-mono gg-type-caption text-[#3157e8]">{grant.permission_code}</span>,
    },
    {
      key: 'role',
      title: '来源角色',
      width: 220,
      render: (grant) => `${grant.role_name} · ${grant.role_code}`,
    },
    {
      key: 'source',
      title: '获得方式',
      width: 170,
      render: (grant) => ({
        platform_admin_compat: '平台管理员兼容',
        direct_role: '直接授权',
        position_role: '岗位带入',
      }[grant.source_kind] || grant.source_kind),
    },
    {
      key: 'scope',
      title: '实际范围',
      width: 220,
      render: (grant) => grant.scope_type === 'tenant'
        ? '全企业'
        : `${grant.scope_id}${grant.include_descendants ? '（含下级）' : '（仅本组织）'}`,
    },
    {
      key: 'validity',
      title: '有效期',
      width: 210,
      render: (grant) => (
        `${grant.effective_from ? formatDateTime(grant.effective_from) : '立即生效'}`
        + ` → ${grant.effective_until ? formatDateTime(grant.effective_until) : '长期'}`
      ),
    },
  ];

  const bindingColumns: DataTableColumn<AgentRoleBinding>[] = [
    { key: 'agent', title: '数字员工', width: 210, render: (binding) => binding.agent_name },
    {
      key: 'role',
      title: '公司业务角色',
      width: 230,
      render: (binding) => <span className="gg-type-meta">{binding.role_name}<small className="ml-[7px] font-mono gg-type-caption text-[#858b9c]">{binding.role_code}</small></span>,
    },
    {
      key: 'mode',
      title: '工作模式',
      width: 120,
      render: (binding) => binding.assignment_mode === 'execute' ? '受控执行' : '辅助办理',
    },
    {
      key: 'scope',
      title: '作用域',
      width: 170,
      render: (binding) => binding.scope_type === 'tenant'
        ? '全企业'
        : `${binding.scope_id}${binding.include_descendants ? '（含下级）' : '（仅本组织）'}`,
    },
    {
      key: 'effective',
      title: '有效期',
      width: 190,
      render: (binding) => `${binding.effective_from ? formatDateTime(binding.effective_from) : '立即生效'} → ${binding.effective_until ? formatDateTime(binding.effective_until) : '长期'}`,
    },
    {
      key: 'status',
      title: '状态',
      width: 100,
      render: (binding) => binding.status === 'active' ? '有效' : '已停用',
    },
    {
      key: 'actions',
      title: '操作',
      width: 170,
      align: 'right',
      render: (binding) => canManageAuthorization && binding.status === 'active' ? (
        <span className="flex justify-end gap-[8px]"><Button aria-label={`编辑数字员工绑定 ${binding.agent_name} ${binding.role_name}`} variant="outline" size="sm" onClick={() => openBindingDialog(binding)}>编辑</Button><Button variant="outline" size="sm" disabled={deactivatingBindingId === binding.id} onClick={() => void deactivateAgentBinding(binding)}>停用</Button></span>
      ) : null,
    },
  ];

  const permissionColumns: DataTableColumn<PermissionDefinition>[] = [
    {
      key: 'permission', title: '权限点', width: 280,
      render: (permission) => <span className="grid"><strong className="gg-type-meta">{permission.name}</strong><small className="font-mono gg-type-caption text-[#526183]">{permission.permission_code}</small></span>,
    },
    { key: 'category', title: '业务域', width: 150, render: (permission) => permission.category },
    { key: 'semantics', title: '资源 / 动作', width: 190, render: (permission) => `${permission.resource} / ${permission.action}` },
    {
      key: 'actions', title: '操作', width: 150, align: 'right',
      render: (permission) => canManageAuthorization ? <span className="flex justify-end gap-[8px]"><Button aria-label={`编辑权限 ${permission.name}`} size="sm" variant="outline" onClick={() => openPermissionDialog(permission)}>编辑</Button>{permission.status === 'active' ? <Button size="sm" variant="outline" onClick={() => setCatalogDeactivateTarget({ kind: 'permission', item: permission })}>停用</Button> : null}</span> : null,
    },
  ];

  const categoryColumns: DataTableColumn<RoleCategoryDefinition>[] = [
    {
      key: 'category', title: '角色分类', width: 220,
      render: (category) => <span className="grid"><strong className="gg-type-meta">{category.name}</strong><small className="font-mono gg-type-caption text-[#526183]">{category.code}</small></span>,
    },
    { key: 'prefix', title: '角色编码前缀', width: 150, render: (category) => `${category.role_code_prefix}_` },
    { key: 'description', title: '说明', width: 280, render: (category) => category.description },
    {
      key: 'actions', title: '操作', width: 150, align: 'right',
      render: (category) => canManageAuthorization ? <span className="flex justify-end gap-[8px]"><Button aria-label={`编辑分类 ${category.name}`} size="sm" variant="outline" onClick={() => openCategoryDialog(category)}>编辑</Button>{category.status === 'active' ? <Button size="sm" variant="outline" onClick={() => setCatalogDeactivateTarget({ kind: 'category', item: category })}>停用</Button> : null}</span> : null,
    },
  ];

  return (
    <PageShell template="management" aria-busy={loading}>
      <AppHeader onLogout={onLogout} userName={currentUser?.username} title="组织角色" />

      <div className="mt-[20px] grid grid-cols-[248px_minmax(0,1fr)] items-start gap-[16px] max-[920px]:grid-cols-1">
        <SideNavPanel
          title="业务授权工作台"
          subtitle="一次只管理一类治理对象"
          icon={ShieldCheck}
          aria-label="组织角色管理对象"
          items={GOVERNANCE_SECTIONS.map((section) => ({
            key: section.key,
            label: section.label,
            description: section.description,
            icon: section.icon,
            count: section.key === 'roles'
              ? roleTotal
              : section.key === 'assignments'
                ? employeeAssignments.length
                : section.key === 'effective'
                  ? effectiveGrants.length
              : section.key === 'permissions'
                ? permissionDefinitions.length
                : section.key === 'categories'
                  ? roleCategories.length
                  : agentBindings.length,
          }))}
          activeKey={activeSection}
          linkFor={(key) => `?section=${key}`}
          footer={(
            <>
              <strong className="gg-type-control text-[#464c5e]">权限边界</strong>
              <p className="mt-[3px] gg-type-body">平台 admin 只负责配置，不会自动获得业务办理权。</p>
            </>
          )}
        />

        <main className="min-w-0 overflow-hidden rounded-[20px] border border-[#dfe5f2] bg-white shadow-[0_12px_32px_rgba(35,55,100,0.06)]">
          <div aria-label="授权关系" className="flex items-center gap-[7px] overflow-x-auto border-b border-[#e8ebf2] bg-[#f8faff] px-[20px] py-[10px] gg-type-caption text-[#68718b]">
            <span className="shrink-0 gg-type-caption font-semibold text-[#464c5e]">治理链路</span>
            {['权限目录', '业务 / 治理角色', '成员 / 岗位来源', '组织范围', '实际判断'].map((label, index) => (
              <span className="flex shrink-0 items-center gap-[7px]" key={label}>
                {index ? <ChevronRight aria-hidden="true" className="size-[12px] text-[#aab2c5]" /> : null}
                <span className="rounded-full border border-[#dfe5f2] bg-white px-[8px] py-[3px]">{label}</span>
              </span>
            ))}
          </div>

          {activeSection === 'roles' ? (
            <section aria-labelledby="roles-heading">
              <WorkspaceHeader
                action={canManageAuthorization ? <Button onClick={openCreate}>新建角色</Button> : undefined}
                description="业务角色用于工作和 SOP 候选，治理角色用于平台管理；两类权限不会互相混入。"
                eyebrow={`${activeCount} 个有效角色 · ${roleAssignmentCount} 个任职关系`}
                id="roles-heading"
                title="角色目录"
              />
              <div className="border-t border-[#eef1f6] p-[18px]">
                <DataTable aria-label="公司角色列表" columns={columns} data={roles} rowKey={(role) => role.id} loading={loading} emptyText="尚未创建公司角色" />
                {roleTotal > 0 && (
                  <Paginator
                    aria-label="角色目录分页"
                    page={rolePage}
                    pageCount={Math.max(1, Math.ceil(roleTotal / ROLE_PAGE_SIZE))}
                    onChange={setRolePage}
                    className="mt-[16px]"
                  />
                )}
              </div>
            </section>
          ) : null}

          {activeSection === 'assignments' ? (
            <section aria-labelledby="assignments-heading">
              <WorkspaceHeader
                action={canManageAuthorization ? <Button onClick={openAssignmentDialog}>授予成员角色</Button> : undefined}
                description="授权事实包含来源角色、租户或组织范围、是否包含下级、有效期和授予人；岗位带入会在有效权限解释中单独展示。"
                eyebrow={`${employeeAssignments.filter((item) => item.status === 'active').length} 个有效授权`}
                id="assignments-heading"
                title="成员角色授权"
              />
              <div className="border-t border-[#eef1f6] p-[18px]">
                <DataTable aria-label="成员角色授权列表" columns={assignmentColumns} data={employeeAssignments} rowKey={(assignment) => assignment.id} loading={loading} emptyText="当前范围内尚无成员角色授权" />
              </div>
            </section>
          ) : null}

          {activeSection === 'effective' ? (
            <section aria-labelledby="effective-heading">
              <WorkspaceHeader
                action={null}
                description="这里展示服务端用于 API 验权的实际 grant，不从账号角色或前端组织树推断。"
                eyebrow={`${effectiveGrants.length} 条有效权限来源`}
                id="effective-heading"
                title="有效权限解释"
              />
              <div className="grid gap-[14px] border-t border-[#eef1f6] p-[18px]">
                <div className="max-w-[440px]">
                  <RemoteMemberSelect
                    ariaLabel="选择要解释权限的成员"
                    onValueChange={setExplanationUserId}
                    placeholder="搜索成员查看实际授权"
                    tenantId={getRequestTenantId()}
                    value={explanationUserId}
                    valueField="user_id"
                  />
                </div>
                <DataTable aria-label="有效权限解释列表" columns={grantColumns} data={effectiveGrants} rowKey={(grant) => `${grant.permission_code}:${grant.source_kind}:${grant.source_id || grant.scope_id}`} loading={loading} emptyText="该成员当前没有治理权限" />
              </div>
            </section>
          ) : null}

          {activeSection === 'permissions' ? (
            <section aria-labelledby="permissions-heading">
              <WorkspaceHeader
                action={canManageAuthorization ? <Button disabled={loading || !roleCategories.some((item) => item.status === 'active')} onClick={() => openPermissionDialog()}>新增权限点</Button> : undefined}
                description="权限点是“资源 + 动作 + 可选作用域”的原子契约。新增后自动进入业务角色和 SOP 动作权限选择器。"
                eyebrow={`${permissionDefinitions.filter((item) => item.status === 'active').length} 个有效权限点`}
                id="permissions-heading"
                title="权限点"
              />
              <div className="border-t border-[#eef1f6] p-[18px]">
                <div className="relative mb-[14px] max-w-[420px]">
                  <Search aria-hidden="true" className="pointer-events-none absolute left-[10px] top-1/2 size-[14px] -translate-y-1/2 text-[#858b9c]" />
                  <Input
                    aria-label="搜索权限点"
                    autoComplete="off"
                    className="pl-[32px]"
                    name="permission-search"
                    onChange={(event) => setCatalogPermissionSearch(event.target.value)}
                    placeholder="搜索名称、编码或用途说明…"
                    value={catalogPermissionSearch}
                  />
                </div>
                <DataTable aria-label="权限点目录" columns={permissionColumns} data={catalogPermissions} rowKey={(permission) => permission.id} loading={loading} emptyText={catalogPermissionSearch ? '没有匹配的权限点' : '尚未定义权限点'} />
              </div>
            </section>
          ) : null}

          {activeSection === 'categories' ? (
            <section aria-labelledby="categories-heading">
              <WorkspaceHeader
                action={canManageAuthorization ? <Button onClick={() => openCategoryDialog()}>新增角色分类</Button> : undefined}
                description="分类是受控业务域，用于约束权限归属和角色编码前缀，不直接授予任何权限。"
                eyebrow={`${roleCategories.filter((item) => item.status === 'active').length} 个有效业务域`}
                id="categories-heading"
                title="角色分类"
              />
              <div className="border-t border-[#eef1f6] p-[18px]">
                <DataTable aria-label="角色分类目录" columns={categoryColumns} data={roleCategories} rowKey={(category) => category.id} loading={loading} emptyText="尚未定义角色分类" />
              </div>
            </section>
          ) : null}

          {activeSection === 'agents' ? (
            <section aria-labelledby="agents-heading">
              <WorkspaceHeader
                action={canManageAuthorization ? <Button onClick={() => openBindingDialog()} disabled={!agents.length || !activeCount}>绑定业务角色</Button> : undefined}
                description="声明数字员工的辅助或受控执行边界。该映射不会把数字员工加入人工审批候选池。"
                eyebrow={`${agentBindings.filter((item) => item.status === 'active').length} 个有效映射`}
                id="agents-heading"
                title="数字员工映射"
              />
              <div className="border-t border-[#eef1f6] p-[18px]">
                <DataTable aria-label="数字员工角色绑定列表" columns={bindingColumns} data={agentBindings} rowKey={(binding) => binding.id} loading={loading} emptyText="尚未绑定数字员工业务角色" />
              </div>
            </section>
          ) : null}
        </main>
      </div>

      <Dialog open={dialogOpen} onOpenChange={(open) => !open && setDialogOpen(false)}>
        <DialogContent aria-describedby={undefined} className="gap-[16px] rounded-[14px] sm:max-w-[620px]">
          <DialogTitle className="gg-type-card-title font-semibold text-[#18181a]">
            {editingRole ? `编辑角色：${editingRole.name}` : '新建公司角色'}
          </DialogTitle>
          <div className="grid gap-[12px]">
            <RoleField label="角色名称">
              <Input value={draft.name} onChange={(event) => setDraft((current) => ({ ...current, name: event.target.value }))} placeholder="例如 用章审批人" />
            </RoleField>
            <RoleField label="角色编码">
              <Input value={draft.roleCode} disabled={Boolean(editingRole)} onChange={(event) => setDraft((current) => ({ ...current, roleCode: event.target.value }))} placeholder={`例如 ${selectedCategory?.role_code_prefix || 'cross'}_process_owner`} />
              <span className="gg-type-caption  text-[#858b9c]">小写英文“业务域_职责”，建议以 {selectedCategory?.role_code_prefix || 'cross'}_ 开头；创建后不可修改。</span>
            </RoleField>
            <RoleField label="角色类型">
              <Select
                disabled={Boolean(editingRole)}
                value={draft.roleKind}
                onValueChange={(roleKind: 'business' | 'governance') => setDraft((current) => ({
                  ...current,
                  roleKind,
                  category: roleKind === 'governance'
                    ? 'governance'
                    : roleCategories.find((category) => category.code !== 'governance')?.code
                      || 'cross_functional',
                  permissions: [],
                }))}
              >
                <SelectTrigger aria-label="角色类型"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="business">业务角色 · 参与工作和 SOP</SelectItem>
                  <SelectItem value="governance">治理角色 · 管理平台范围</SelectItem>
                </SelectContent>
              </Select>
              <span className="gg-type-caption  text-[#858b9c]">治理角色不会进入人工任务候选池，平台管理员也不会因此获得业务办理权。</span>
            </RoleField>
            <RoleField label="角色分类">
              <Select disabled={draft.roleKind === 'governance'} value={draft.category} onValueChange={(category) => setDraft((current) => ({ ...current, category }))}>
                <SelectTrigger aria-label="角色分类"><SelectValue placeholder="选择业务域" /></SelectTrigger>
                <SelectContent>
                  {roleCategories.filter((category) => (
                    (category.code === 'governance') === (draft.roleKind === 'governance')
                  )).map((category) => (
                    <SelectItem key={category.code} value={category.code}>{category.name} · {category.code}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {selectedCategory ? <span className="gg-type-caption  text-[#858b9c]">{selectedCategory.description}</span> : null}
            </RoleField>
            <RoleField label="权限点">
              <div className="rounded-[10px] border border-[#dfe5f2] bg-[#fbfcff]">
                <div className="flex min-h-[42px] flex-wrap items-center gap-[6px] border-b border-[#e8ebf2] px-[10px] py-[7px]">
                  {draft.permissions.length ? draft.permissions.map((permissionCode) => (
                    <button key={permissionCode} type="button" onClick={() => togglePermission(permissionCode, false)} className="flex items-center gap-[4px] rounded-full bg-[#e9efff] px-[8px] py-[4px] font-mono gg-type-caption text-[#244bc7] hover:bg-[#dce6ff]" aria-label={`移除权限 ${permissionCode}`}>
                      {permissionCode}<X aria-hidden="true" className="size-[11px]" />
                    </button>
                  )) : <span className="gg-type-meta text-[#858b9c]">尚未选择权限</span>}
                </div>
                <div className="relative m-[8px]">
                  <Search aria-hidden="true" className="pointer-events-none absolute left-[9px] top-1/2 size-[14px] -translate-y-1/2 text-[#858b9c]" />
                  <Input aria-label="查询权限点" value={permissionSearch} onChange={(event) => setPermissionSearch(event.target.value)} placeholder="按权限名称、编码或说明查询" className="pl-[30px]" />
                </div>
                <div className="max-h-[190px] overflow-y-auto border-t border-[#e8ebf2] p-[6px]">
                  {visiblePermissions.length ? visiblePermissions.map((permission) => {
                    const selected = draft.permissions.includes(permission.permission_code);
                    return (
                      <label key={permission.id} className="flex cursor-pointer items-start gap-[9px] rounded-[8px] px-[8px] py-[7px] hover:bg-white">
                        <Checkbox checked={selected} onCheckedChange={(checked) => togglePermission(permission.permission_code, checked === true)} aria-label={`选择权限 ${permission.name}`} className="mt-[2px]" />
                        <span className="min-w-0">
                          <strong className="block gg-type-meta font-medium text-[#24262d]">{permission.name}</strong>
                          <span className="block truncate font-mono gg-type-caption text-[#526183]">{permission.permission_code}</span>
                          {permission.description ? <span className="mt-[2px] block gg-type-caption  text-[#858b9c]">{permission.description}</span> : null}
                        </span>
                      </label>
                    );
                  }) : <p className="px-[8px] py-[16px] text-center gg-type-meta text-[#858b9c]">没有匹配的权限点</p>}
                </div>
              </div>
            </RoleField>
          </div>
          <div className="flex justify-end gap-[8px]">
            <Button variant="outline" disabled={saving} onClick={() => setDialogOpen(false)}>取消</Button>
            <Button disabled={saving} onClick={() => void saveRole()} className="bg-[var(--gg-cobalt)] text-white hover:bg-[#244bc7]">
              {saving ? '保存中…' : '保存角色'}
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      <Dialog open={assignmentDialogOpen} onOpenChange={(open) => !open && setAssignmentDialogOpen(false)}>
        <DialogContent aria-describedby="assignment-dialog-description" className="max-h-[92vh] gap-[16px] overflow-y-auto rounded-[16px] sm:max-w-[760px]">
          <DialogTitle className="gg-type-card-title font-semibold text-[#18181a]">授予成员角色</DialogTitle>
          <p className="gg-type-meta  text-[#68718b]" id="assignment-dialog-description">
            先选择职责，再明确它在哪个组织范围内生效。负责人称谓本身不会自动产生这里的授权。
          </p>
          <div className="grid gap-[14px]">
            <RoleField label="成员">
              <RemoteMemberSelect
                ariaLabel="授权成员"
                excludeUserId={currentUser?.id}
                onValueChange={setAssignmentMemberId}
                tenantId={getRequestTenantId()}
                value={assignmentMemberId}
              />
            </RoleField>
            <RoleField label="角色">
              <Select value={assignmentRoleCode} onValueChange={setAssignmentRoleCode}>
                <SelectTrigger aria-label="授权角色"><SelectValue placeholder="选择角色" /></SelectTrigger>
                <SelectContent>
                  {roleOptions.map((role) => (
                    <SelectItem key={role.id} value={role.role_code}>
                      {role.role_kind === 'governance' ? '治理' : '业务'} · {role.name} · {role.role_code}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </RoleField>
            <RoleField label="作用范围">
              <Select
                value={assignmentScopeType}
                onValueChange={(value: 'tenant' | 'org_unit') => setAssignmentScopeType(value)}
              >
                <SelectTrigger aria-label="授权作用范围"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="org_unit">指定组织</SelectItem>
                  <SelectItem value="tenant">全企业</SelectItem>
                </SelectContent>
              </Select>
            </RoleField>
            {assignmentScopeType === 'org_unit' ? (
              <RoleField label="授权组织">
                <div className="max-h-[360px] overflow-y-auto rounded-[12px] border border-[#dfe5f2] bg-[#fbfcff] p-[10px]">
                  <OrganizationTreeNavigator
                    onSelect={setAssignmentOrg}
                    selectedId={assignmentOrg?.id || ''}
                    selectRootOnInitialize={false}
                    tenantId={getRequestTenantId()}
                  />
                </div>
                <label className="flex items-center gap-[8px] gg-type-meta text-[#464c5e]">
                  <Checkbox
                    aria-label="授权包含下级组织"
                    checked={assignmentIncludesDescendants}
                    onCheckedChange={(checked) => setAssignmentIncludesDescendants(checked === true)}
                  />
                  同时包含该组织的全部下级
                </label>
              </RoleField>
            ) : (
              <p className="rounded-[10px] bg-[#fff8e8] px-[11px] py-[9px] gg-type-meta text-[#795300]">
                全企业授权只能由拥有租户级授权管理权限的成员保存。
              </p>
            )}
            <RoleField
              label="授权原因"
              required
              hint="说明业务依据、代理边界或风险控制要求；该内容会进入审计记录。"
            >
              <Input
                aria-label="成员角色授权原因"
                maxLength={500}
                onChange={(event) => setAssignmentGrantReason(event.target.value)}
                placeholder="例如：部门负责人本人发起时，由备用审批人完成独立复核"
                value={assignmentGrantReason}
              />
            </RoleField>
            <RoleField
              label="授权截止时间"
              required
              hint="默认 90 天，到期后自动退出候选人集合；如需继续应重新确认。"
            >
              <Input
                aria-label="成员角色授权截止时间"
                min={toLocalDateTimeInput(new Date().toISOString())}
                onChange={(event) => setAssignmentEffectiveUntil(event.target.value)}
                type="datetime-local"
                value={assignmentEffectiveUntil}
              />
            </RoleField>
          </div>
          <div className="flex justify-end gap-[8px]">
            <Button disabled={assignmentSaving} onClick={() => setAssignmentDialogOpen(false)} variant="outline">取消</Button>
            <Button disabled={assignmentSaving} onClick={() => void saveAssignment()}>
              {assignmentSaving ? '保存中…' : '保存授权'}
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      <Dialog open={permissionDialogOpen} onOpenChange={(open) => !open && setPermissionDialogOpen(false)}>
        <DialogContent aria-describedby="permission-dialog-description" className="max-h-[92vh] gap-0 overflow-y-auto overscroll-contain rounded-[18px] p-0 sm:max-w-[700px]">
          <div className="border-b border-[#e8ebf2] px-[24px] py-[20px]">
            <DialogTitle className="gg-type-section-title font-semibold text-[#18181a]">
              {editingPermission ? `编辑权限：${editingPermission.name}` : '新增权限点'}
            </DialogTitle>
            <p className="mt-[5px] gg-type-meta  text-[#68718b]" id="permission-dialog-description">
              {editingPermission
                ? '稳定编码和业务语义已冻结，只能修正名称与用途说明。'
                : '先描述“对什么资源执行什么动作”，系统会生成可被角色和 SOP 引用的稳定编码。'}
            </p>
          </div>

          <form onSubmit={(event) => { event.preventDefault(); void savePermission(); }}>
            <div className="grid gap-[18px] px-[24px] py-[20px]">
              <fieldset className="grid gap-[12px] border-0 p-0">
                <legend className="mb-[1px] flex items-center gap-[8px] gg-type-meta font-semibold text-[#24262d]">
                  <span className="grid size-[22px] place-items-center rounded-full bg-[#3157e8] font-mono gg-type-caption text-white">1</span>
                  说明这项权限属于哪里
                </legend>
                <div className="grid grid-cols-2 gap-[12px] max-[620px]:grid-cols-1">
                  <RoleField error={permissionAttempted ? permissionErrors.name : undefined} label="权限名称" required>
                    <Input
                      aria-label="权限名称"
                      aria-invalid={permissionAttempted && Boolean(permissionErrors.name)}
                      autoComplete="off"
                      name="permission-name"
                      onChange={(event) => { setPermissionDraft((current) => ({ ...current, name: event.target.value })); setPermissionServerError(''); }}
                      placeholder="例如：审批采购单…"
                      value={permissionDraft.name}
                    />
                  </RoleField>
                  <RoleField error={permissionAttempted ? permissionErrors.category : undefined} hint="选择后会决定权限在目录中的归属。" label="业务域" required>
                    <Select disabled={Boolean(editingPermission)} value={permissionDraft.category} onValueChange={(category) => { setPermissionDraft((current) => ({ ...current, category })); setPermissionServerError(''); }}>
                      <SelectTrigger aria-label="权限业务域"><SelectValue placeholder="选择业务域" /></SelectTrigger>
                      <SelectContent>{roleCategories.filter((category) => category.status !== 'inactive').map((category) => <SelectItem key={category.code} value={category.code}>{category.name} · {category.code}</SelectItem>)}</SelectContent>
                    </Select>
                  </RoleField>
                </div>
              </fieldset>

              <fieldset className="grid gap-[12px] border-0 p-0">
                <legend className="mb-[1px] flex items-center gap-[8px] gg-type-meta font-semibold text-[#24262d]">
                  <span className="grid size-[22px] place-items-center rounded-full bg-[#3157e8] font-mono gg-type-caption text-white">2</span>
                  定义原子操作
                </legend>
                <div className="grid grid-cols-2 gap-[12px] max-[620px]:grid-cols-1">
                  <RoleField error={permissionAttempted ? permissionErrors.resource : undefined} hint="业务对象用“.”分层，例如 purchase.order 或 it.ticket。" label="资源" required>
                    <Input
                      aria-label="资源"
                      aria-invalid={permissionAttempted && Boolean(permissionErrors.resource)}
                      autoComplete="off"
                      disabled={Boolean(editingPermission)}
                      name="permission-resource"
                      onChange={(event) => { setPermissionDraft((current) => ({ ...current, resource: event.target.value })); setPermissionServerError(''); }}
                      placeholder="例如：purchase.order…"
                      spellCheck={false}
                      value={permissionDraft.resource}
                    />
                  </RoleField>
                  <RoleField error={permissionAttempted ? permissionErrors.action : undefined} hint="只写一个动作，例如 view、claim、approve 或 resolve。" label="动作" required>
                    <Input
                      aria-label="动作"
                      aria-invalid={permissionAttempted && Boolean(permissionErrors.action)}
                      autoComplete="off"
                      disabled={Boolean(editingPermission)}
                      name="permission-action"
                      onChange={(event) => { setPermissionDraft((current) => ({ ...current, action: event.target.value })); setPermissionServerError(''); }}
                      placeholder="例如：approve…"
                      spellCheck={false}
                      value={permissionDraft.action}
                    />
                  </RoleField>
                </div>
                <RoleField error={permissionAttempted ? permissionErrors.scope : undefined} hint="多数权限无需填写；只有同一操作确实存在不同数据边界时才使用，例如 own 或 any。" label="作用域（可选）">
                  <Input
                    aria-label="作用域"
                    aria-invalid={permissionAttempted && Boolean(permissionErrors.scope)}
                    autoComplete="off"
                    disabled={Boolean(editingPermission)}
                    name="permission-scope"
                    onChange={(event) => { setPermissionDraft((current) => ({ ...current, scope: event.target.value })); setPermissionServerError(''); }}
                    placeholder="例如：own…"
                    spellCheck={false}
                    value={permissionDraft.scope}
                  />
                </RoleField>
              </fieldset>

              <fieldset className="grid gap-[12px] border-0 p-0">
                <legend className="mb-[1px] flex items-center gap-[8px] gg-type-meta font-semibold text-[#24262d]">
                  <span className="grid size-[22px] place-items-center rounded-full bg-[#3157e8] font-mono gg-type-caption text-white">3</span>
                  确认生成的契约
                </legend>
                <div className="rounded-[12px] border border-[#cfd9f4] bg-[#f5f8ff] px-[14px] py-[12px]">
                  <span className="block gg-type-caption font-semibold uppercase tracking-[0.13em] text-[#68718b]">稳定权限编码</span>
                  <code aria-label="权限稳定编码" className="mt-[5px] block break-words gg-type-body font-semibold text-[#244bc7]" translate="no">
                    {permissionCode || '填写资源和动作后自动生成'}
                  </code>
                  <p className="mt-[6px] gg-type-caption  text-[#68718b]">创建后不可改名；语义变化时应创建新权限并迁移引用，避免历史 SOP 含义漂移。</p>
                </div>
                <RoleField hint="写清谁会使用、在哪个流程动作中消费，方便后续授权审核。" label="用途说明">
                  <Input
                    aria-label="用途说明"
                    autoComplete="off"
                    name="permission-description"
                    onChange={(event) => { setPermissionDraft((current) => ({ ...current, description: event.target.value })); setPermissionServerError(''); }}
                    placeholder="例如：允许采购负责人审批待处理采购单…"
                    value={permissionDraft.description}
                  />
                </RoleField>
              </fieldset>

              {permissionServerError ? (
                <div aria-live="polite" className="rounded-[10px] border border-[#f2c6c3] bg-[#fff4f3] px-[12px] py-[10px] gg-type-meta  text-[#a12a21]" role="alert">
                  <strong className="block">权限点没有保存</strong>
                  {permissionServerError}
                </div>
              ) : null}
            </div>

            <div className="flex items-center justify-between gap-[12px] border-t border-[#e8ebf2] bg-[#fafbfe] px-[24px] py-[14px] max-[620px]:items-end">
              <p className="max-w-[390px] gg-type-caption  text-[#858b9c]">保存后会立即出现在“新建业务角色”的权限选择器中。</p>
              <div className="flex shrink-0 gap-[8px]">
                <Button type="button" variant="outline" disabled={permissionSaving} onClick={() => setPermissionDialogOpen(false)}>取消</Button>
                <Button type="submit" disabled={permissionSaving}>{permissionSaving ? '保存中…' : editingPermission ? '保存说明' : '创建权限点'}</Button>
              </div>
            </div>
          </form>
        </DialogContent>
      </Dialog>

      <Dialog open={categoryDialogOpen} onOpenChange={(open) => !open && setCategoryDialogOpen(false)}>
        <DialogContent aria-describedby={undefined} className="gap-[16px] rounded-[14px] sm:max-w-[520px]">
          <DialogTitle>{editingCategory ? `编辑分类：${editingCategory.name}` : '新增角色分类'}</DialogTitle>
          <div className="grid gap-[12px]">
            <RoleField label="分类名称"><Input value={categoryDraft.name} onChange={(event) => setCategoryDraft((current) => ({ ...current, name: event.target.value }))} placeholder="例如 采购" /></RoleField>
            <RoleField label="分类编码"><Input disabled={Boolean(editingCategory)} value={categoryDraft.code} onChange={(event) => setCategoryDraft((current) => ({ ...current, code: event.target.value }))} placeholder="procurement" /></RoleField>
            <RoleField label="角色编码前缀"><Input value={categoryDraft.roleCodePrefix} onChange={(event) => setCategoryDraft((current) => ({ ...current, roleCodePrefix: event.target.value }))} placeholder="purchase" /></RoleField>
            <RoleField label="说明"><Input value={categoryDraft.description} onChange={(event) => setCategoryDraft((current) => ({ ...current, description: event.target.value }))} placeholder="该业务域覆盖的职责边界" /></RoleField>
          </div>
          <p className="gg-type-caption  text-[#858b9c]">分类编码创建后不可修改；存在有效角色或权限引用时不能停用。</p>
          <div className="flex justify-end gap-[8px]"><Button variant="outline" onClick={() => setCategoryDialogOpen(false)}>取消</Button><Button disabled={categorySaving} onClick={() => void saveCategory()}>{categorySaving ? '保存中…' : '保存分类'}</Button></div>
        </DialogContent>
      </Dialog>

      <Dialog open={bindingDialogOpen} onOpenChange={(open) => !open && setBindingDialogOpen(false)}>
        <DialogContent aria-describedby={undefined} className="gap-[16px] rounded-[14px] sm:max-w-[480px]">
          <DialogTitle className="gg-type-card-title font-semibold text-[#18181a]">{editingBinding ? '编辑数字员工业务角色' : '绑定数字员工业务角色'}</DialogTitle>
          <div className="grid gap-[12px]">
            <RoleField label="数字员工">
              <Select disabled={Boolean(editingBinding)} value={bindingAgentId} onValueChange={setBindingAgentId}>
                <SelectTrigger><SelectValue placeholder="选择数字员工" /></SelectTrigger>
                <SelectContent>{agents.map((agent) => <SelectItem key={agent.id} value={agent.id}>{agent.name}</SelectItem>)}</SelectContent>
              </Select>
            </RoleField>
            <RoleField label="公司业务角色">
              <Select disabled={Boolean(editingBinding)} value={bindingRoleCode} onValueChange={setBindingRoleCode}>
                <SelectTrigger><SelectValue placeholder="选择业务角色" /></SelectTrigger>
                <SelectContent>{roleOptions.filter((role) => role.role_kind === 'business').map((role) => <SelectItem key={role.id} value={role.role_code}>{role.name} · {role.role_code}</SelectItem>)}</SelectContent>
              </Select>
            </RoleField>
            <RoleField label="工作模式">
              <Select value={bindingMode} onValueChange={(value) => setBindingMode(value as 'assist' | 'execute')}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="assist">辅助办理</SelectItem>
                  <SelectItem value="execute">受控执行</SelectItem>
                </SelectContent>
              </Select>
            </RoleField>
            <RoleField label="人类监督者">
              <RemoteMemberSelect
                tenantId={getRequestTenantId()}
                value={bindingSupervisorId}
                onValueChange={setBindingSupervisorId}
                ariaLabel="人类监督者"
                allowNone
              />
            </RoleField>
            {editingBinding ? (
              <p className="rounded-[10px] border border-[#e1e6f0] bg-[#f8f9fc] px-[11px] py-[9px] gg-type-meta text-[#616a7d]">
                作用范围：{editingBinding.scope_type === 'tenant'
                  ? '全企业'
                  : `${editingBinding.scope_id}${editingBinding.include_descendants ? '（含下级）' : '（仅本组织）'}`}。如需变更范围，请停用后重新绑定，避免改写历史授权语义。
              </p>
            ) : (
              <>
                <RoleField label="数字员工工作范围">
                  <Select value={bindingScopeType} onValueChange={(value: 'tenant' | 'org_unit') => setBindingScopeType(value)}>
                    <SelectTrigger aria-label="数字员工工作范围"><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="tenant">全企业</SelectItem>
                      <SelectItem value="org_unit">指定组织</SelectItem>
                    </SelectContent>
                  </Select>
                </RoleField>
                {bindingScopeType === 'org_unit' ? (
                  <RoleField label="授权组织">
                    <div className="max-h-[280px] overflow-y-auto rounded-[12px] border border-[#dfe5f2] bg-[#fbfcff] p-[10px]">
                      <OrganizationTreeNavigator
                        onSelect={setBindingOrg}
                        selectedId={bindingOrg?.id || ''}
                        selectRootOnInitialize={false}
                        tenantId={getRequestTenantId()}
                      />
                    </div>
                    <label className="flex items-center gap-[8px] gg-type-meta text-[#464c5e]">
                      <Checkbox
                        aria-label="数字员工范围包含下级组织"
                        checked={bindingIncludesDescendants}
                        onCheckedChange={(checked) => setBindingIncludesDescendants(checked === true)}
                      />
                      同时包含该组织的全部下级
                    </label>
                  </RoleField>
                ) : null}
              </>
            )}
            <RoleField label="授权截止时间（可选）">
              <Input
                aria-label="数字员工角色授权截止时间"
                min={toLocalDateTimeInput(new Date().toISOString())}
                type="datetime-local"
                value={bindingEffectiveUntil}
                onChange={(event) => setBindingEffectiveUntil(event.target.value)}
              />
            </RoleField>
          </div>
          <p className="rounded-[10px] bg-[#fff8e8] px-[11px] py-[9px] gg-type-meta text-[#795300]">该绑定不授予人工审批权；人工任务仍只解析真实员工任职快照。</p>
          <div className="flex justify-end gap-[8px]">
            <Button variant="outline" disabled={bindingSaving} onClick={() => setBindingDialogOpen(false)}>取消</Button>
            <Button disabled={bindingSaving} onClick={() => void saveAgentBinding()} className="bg-[var(--gg-cobalt)] text-white hover:bg-[#244bc7]">保存绑定</Button>
          </div>
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={Boolean(deactivateTarget)}
        onOpenChange={(open) => !open && setDeactivateTarget(null)}
        loading={deactivating}
        title={deactivateTarget ? `停用「${deactivateTarget.name}」？` : ''}
        description="停用后不再产生新的候选人，已有工作项、任职和历史记录不会被删除。"
        onConfirm={() => void confirmDeactivate()}
      />
      <ConfirmDialog
        open={Boolean(catalogDeactivateTarget)}
        onOpenChange={(open) => !open && setCatalogDeactivateTarget(null)}
        loading={catalogDeactivating}
        title={catalogDeactivateTarget?.kind === 'permission' ? '停用该权限点？' : '停用该角色分类？'}
        description="系统会先检查活动角色、权限和已发布 SOP 引用；存在引用时不会停用。"
        onConfirm={() => void confirmCatalogDeactivate()}
      />
    </PageShell>
  );
}

function WorkspaceHeader({
  action,
  description,
  eyebrow,
  id,
  title,
}: {
  action: ReactNode;
  description: string;
  eyebrow: string;
  id: string;
  title: string;
}) {
  return (
    <div className="flex items-start justify-between gap-[20px] px-[22px] py-[21px] max-[620px]:flex-col">
      <div className="min-w-0">
        <p className="font-mono gg-type-caption font-semibold uppercase tracking-[0.13em] text-[#6074a9]">{eyebrow}</p>
        <h2 className="mt-[5px] gg-type-section-title font-semibold tracking-[-0.02em] text-[#18181a] text-balance" id={id}>{title}</h2>
        <p className="mt-[5px] max-w-[680px] gg-type-meta  text-[#68718b] text-pretty">{description}</p>
      </div>
      <div className="shrink-0">{action}</div>
    </div>
  );
}

function RoleField({
  label,
  children,
  error,
  hint,
  required = false,
}: {
  label: string;
  children: ReactNode;
  error?: string;
  hint?: string;
  required?: boolean;
}) {
  return (
    <label className="grid gap-[5px]">
      <span className="gg-type-meta font-medium text-[#464c5e]">
        {label}{required ? <span aria-hidden="true" className="ml-[3px] text-[#c33b30]">*</span> : null}
      </span>
      {children}
      {error ? <span className="gg-type-caption  text-[#b52e25]" role="alert">{error}</span> : null}
      {!error && hint ? <span className="gg-type-caption  text-[#858b9c]">{hint}</span> : null}
    </label>
  );
}

function toLocalDateTimeInput(value?: string): string {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

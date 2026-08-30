import { notify } from '@/components/ui/app-toast';
import { cn } from '@/lib/utils';
import { createClientId } from '@/lib/client-id';
import { RESOURCE_GRID_CLASS } from '@/lib/enterprise-ui';

import { Bot, Clock, RefreshCw, ShieldCheck, Star, UserCheck, Users, UserX } from 'lucide-react';

import IconPlus from '../assets/icons/plus.svg?react';
import IconSearch from '../assets/icons/search.svg?react';

import { useCallback, useEffect, useRef, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';

import { api, getRequestTenantId } from '../api/client';
import { hasGovernancePermission, isGalleryEmployee, type EnterpriseAuthUser } from '../auth';

import AppHeader from '../components/AppHeader';
import { ConceptHelp, ConceptNote } from '../components/ConceptHelp';
import { ConfirmDialog } from '../components/ConfirmDialog';
import EmployeeAvatarEditor from '../components/EmployeeAvatarEditor';
import EmployeeCard from '../components/EmployeeCard';
import EmployeeProfileEditor from '../components/EmployeeProfileEditor';
import { EnterpriseCatalogHero, EnterpriseCatalogPageHeader } from '../components/EnterpriseCatalogHeader';
import { CatalogGrid } from '../components/enterprise/CatalogGrid';
import { PageShell } from '../components/enterprise/PageShell';
import { PageState } from '../components/enterprise/PageState';
import { Paginator } from '../components/Paginator';
import ExpertBulkActionBar from '../components/ExpertBulkActionBar';
import ExpertClassificationDialog from '../components/ExpertClassificationDialog';
import ExpertFilterBar from '../components/ExpertFilterBar';
import SideNavPanel, { type SideNavPanelItem } from '../components/SideNavPanel';
import { Button, Dialog, DialogContent, DialogTitle, Textarea } from '@/components/ui';
import { useI18n } from '@/i18n';
import {
  canManageEmployeeAgent,
  canSelectCurrentEmployeeAgent,
  employeeDisplayName,
  expertCategory,
  expertSubcategory,
} from '../employee';
import { EnterpriseRoute } from '../enums/routes';
import { emitAgentScopeChange, persistSharedAgentScope } from '../lib/agent-scope-storage';
import type {
  AgentDeletionResult,
  AgentManagementPageRead,
  AgentOrganizationizationOptionsRead,
  AgentOrganizationizationPreviewRead,
  AgentOrganizationizationResultRead,
  AgentProfileRead,
  ExpertTaxonomyAssignmentResult,
  ExpertTaxonomyRead,
} from '../types';

const ENTERPRISE_AGENT_STORAGE_KEY = 'gongge_enterprise_agent_scope';
const AGENT_PAGE_SIZE = 12;
const EMPTY_VIEW_COUNTS: AgentManagementPageRead['view_counts'] = {
  all: 0, online: 0, offline: 0, pending: 0, expert: 0, governance: 0,
};
const EMPTY_GOVERNANCE_COUNTS: NonNullable<AgentManagementPageRead['governance_counts']> = {
  capability_avatar: 0,
  organization_pending: 0,
  organization_employee: 0,
  template: 0,
};
const EMPTY_AGENT_FACETS: NonNullable<AgentManagementPageRead['facets']> = {
  sources: [],
  departments: [],
  directions: [],
};
type EmployeeFilter = 'all' | 'online' | 'offline' | 'pending' | 'expert' | 'governance' | 'capability' | 'organization';
type AgentPublicationRelease = {
  id: string;
  resource_type: 'agent';
  resource_id: string;
  snapshot_id: string;
  snapshot_checksum: string;
  name: string;
  description: string;
  components: Array<{ resource_type: string; resource_id: string; metadata?: Record<string, unknown> }>;
  status: 'active' | 'unpublished' | 'security_revoked';
  row_version: number;
};
type AgentReleaseTransitionCommand = 'unpublish' | 'security_revoke';
const EMPLOYEE_VIEWS: EmployeeFilter[] = [
  'all',
  'online',
  'offline',
  'pending',
  'expert',
  'governance',
  'capability',
  'organization',
];

const VIEW_META: Record<EmployeeFilter, { title: string; description: string }> = {
  all: {
    title: '可管理数字员工',
    description: '点击员工卡片可将其设为当前员工，并进入员工档案管理知识、技能与工具。',
  },
  online: {
    title: '在线员工',
    description: '已上线的员工，可立即投入对话与任务。',
  },
  offline: {
    title: '下线员工',
    description: '已下线的员工暂停服务，可在卡片菜单中重新上线。',
  },
  pending: {
    title: '待审批',
    description: '等待审批的员工，审批通过后即可投入使用。',
  },
  expert: {
    title: '我的专家能力分身',
    description: '这里只管理当前账号拥有的专业能力分身；平台内置专家模板请前往开放广场的专家分类。',
  },
  governance: {
    title: '发布治理',
    description: '只审核责任人、分类、状态与发布范围；不会获得所有者的私人配置编辑权。',
  },
  capability: {
    title: '我的能力分身',
    description: '当前账号拥有的个人工作伙伴；可继续组合知识、Skill、SOP 和工具。',
  },
  organization: {
    title: '组织数字员工',
    description: '已组织化或正在补齐组织前置条件的 Agent；正式身份由角色、责任、监督和 Release 共同决定。',
  },
};

function isEmployeeFilter(value: string | null): value is EmployeeFilter {
  return EMPLOYEE_VIEWS.includes(value as EmployeeFilter);
}

function viewLink(key: string, childKey?: string) {
  if (key === 'expert') {
    return childKey ? `?view=expert&dept=${encodeURIComponent(childKey)}` : '?view=expert';
  }
  return `?view=${key}`;
}

export default function AgentsPage({
  currentUser,
  isAdmin = false,
  onCreateAgent,
  onLogout,
}: {
  currentUser?: EnterpriseAuthUser;
  isAdmin?: boolean;
  onCreateAgent?: () => void;
  onLogout?: () => void;
}) {
  const { t } = useI18n();
  const [agents, setAgents] = useState<AgentProfileRead[]>([]);
  const [loading, setLoading] = useState(false);
  const [avatarAgent, setAvatarAgent] = useState<AgentProfileRead | null>(null);
  const [profileAgent, setProfileAgent] = useState<AgentProfileRead | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<AgentProfileRead | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [selectingAgentId, setSelectingAgentId] = useState<string | null>(null);
  const [searchTerm, setSearchTerm] = useState('');
  const [appliedSearch, setAppliedSearch] = useState('');
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [viewCounts, setViewCounts] = useState(EMPTY_VIEW_COUNTS);
  const [governanceCounts, setGovernanceCounts] = useState(EMPTY_GOVERNANCE_COUNTS);
  const [facets, setFacets] = useState<AgentManagementPageRead['facets']>({ sources: [], departments: [], directions: [] });
  const requestIdRef = useRef(0);
  const [expertSource, setExpertSource] = useState('');
  const [expertDirection, setExpertDirection] = useState('');
  const [selectedExpertIds, setSelectedExpertIds] = useState<Set<string>>(() => new Set());
  const [taxonomy, setTaxonomy] = useState<ExpertTaxonomyRead | null>(null);
  const [taxonomyUnavailable, setTaxonomyUnavailable] = useState(false);
  const [classificationTargets, setClassificationTargets] = useState<AgentProfileRead[]>([]);
  const [savingClassification, setSavingClassification] = useState(false);
  const [publicationOpen, setPublicationOpen] = useState(false);
  const [publicationBusy, setPublicationBusy] = useState(false);
  const [agentReleases, setAgentReleases] = useState<AgentPublicationRelease[]>([]);
  const [rollbackTarget, setRollbackTarget] = useState<AgentPublicationRelease | null>(null);
  const [rollbackReason, setRollbackReason] = useState('');
  const [transitionTarget, setTransitionTarget] = useState<AgentPublicationRelease | null>(null);
  const [transitionCommand, setTransitionCommand] = useState<AgentReleaseTransitionCommand>('unpublish');
  const [transitionReason, setTransitionReason] = useState('');
  const [organizationPreview, setOrganizationPreview] = useState<AgentOrganizationizationPreviewRead | null>(null);
  const [organizationOptions, setOrganizationOptions] = useState<AgentOrganizationizationOptionsRead | null>(null);
  const [organizationDraft, setOrganizationDraft] = useState({
    responsibleOrgUnitId: '',
    roleCode: '',
    supervisorProfileId: '',
  });
  const [organizationPreviewBusy, setOrganizationPreviewBusy] = useState(false);
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(
    () => window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY),
  );
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const viewParam = searchParams.get('view');
  const employeeFilter: EmployeeFilter = isEmployeeFilter(viewParam) ? viewParam : 'all';
  const expertDepartment = searchParams.get('dept') || '';
  const canGovernAgents = isAdmin || hasGovernancePermission(currentUser, 'agent.manage');
  const canReviewAgentReleases = isAdmin || currentUser?.role === 'admin';
  const isExpertTemplateManagement = employeeFilter === 'expert' && isAdmin;
  const activeViewMeta = isExpertTemplateManagement
    ? {
        title: '专家模板管理',
        description: '这里只管理项目内置专家模板；用户复制后才形成自己的专家能力分身。',
      }
    : VIEW_META[employeeFilter];

  const load = useCallback(async () => {
    const requestId = ++requestIdRef.current;
    setLoading(true);
    const params = new URLSearchParams({
      tenant_id: getRequestTenantId(), view: employeeFilter,
      page: String(page), page_size: String(AGENT_PAGE_SIZE),
    });
    if (appliedSearch) params.set('q', appliedSearch);
    if (employeeFilter === 'expert') {
      if (expertSource) params.set('expert_source', expertSource);
      if (expertDepartment) params.set('expert_department', expertDepartment);
      if (expertDirection) params.set('expert_direction', expertDirection);
    }
    try {
      const result = await api.get<AgentManagementPageRead>(
        `/api/enterprise/agents/management-page?${params.toString()}`,
      );
      if (requestId !== requestIdRef.current) return;
      const items = Array.isArray(result.items) ? result.items : [];
      const totalCount = Number.isFinite(result.total) ? result.total : 0;
      const facets = result.facets || EMPTY_AGENT_FACETS;
      setAgents(items);
      setTotal(totalCount);
      setViewCounts(result.view_counts || EMPTY_VIEW_COUNTS);
      setGovernanceCounts(result.governance_counts || EMPTY_GOVERNANCE_COUNTS);
      setFacets({
        sources: Array.isArray(facets.sources) ? facets.sources : [],
        departments: Array.isArray(facets.departments) ? facets.departments : [],
        directions: Array.isArray(facets.directions) ? facets.directions : [],
      });
      const lastPage = Math.max(1, Math.ceil(totalCount / (result.page_size || AGENT_PAGE_SIZE)));
      if (page > lastPage) setPage(lastPage);
    } catch (error) {
      if (requestId === requestIdRef.current) {
        notify.error(error instanceof Error ? error.message : '加载员工失败');
      }
    } finally {
      if (requestId === requestIdRef.current) setLoading(false);
    }
  }, [appliedSearch, employeeFilter, expertDepartment, expertDirection, expertSource, page]);

  useEffect(() => {
    void load();
    return () => { requestIdRef.current += 1; };
  }, [load]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setAppliedSearch(searchTerm.trim());
      setPage(1);
    }, 250);
    return () => window.clearTimeout(timer);
  }, [searchTerm]);

  useEffect(() => {
    setPage(1);
  }, [employeeFilter, expertDepartment, expertDirection, expertSource]);

  useEffect(() => {
    void api.get<ExpertTaxonomyRead>(
      `/api/enterprise/expert-taxonomy?tenant_id=${getRequestTenantId()}`,
    ).then((result) => {
      if (!Array.isArray(result?.categories)) throw new Error('Invalid expert taxonomy response');
      setTaxonomy(result);
      setTaxonomyUnavailable(false);
    }).catch(() => {
      setTaxonomy(null);
      setTaxonomyUnavailable(true);
    });
  }, []);

  useEffect(() => {
    const handler = (event: Event) => {
      const detail = (event as CustomEvent<{ agentId?: string }>).detail;
      setSelectedAgentId(detail?.agentId ?? window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY));
    };
    window.addEventListener('gongge-enterprise-agent-scope-change', handler);
    return () => window.removeEventListener('gongge-enterprise-agent-scope-change', handler);
  }, []);

  const employees = agents;
  const expertEmployees = agents;
  const sourceOptions = facets.sources;
  const departmentOptions = facets.departments;
  const directionOptions = facets.directions;
  const filteredEmployees = agents;

  useEffect(() => {
    if (employeeFilter === 'expert') return;
    setExpertSource('');
    setExpertDirection('');
    setSelectedExpertIds(new Set());
  }, [employeeFilter]);

  useEffect(() => {
    if (expertDepartment && !departmentOptions.some((item) => item.value === expertDepartment)) {
      setExpertDirection('');
      navigate('?view=expert', { replace: true });
    }
  }, [departmentOptions, expertDepartment, navigate]);

  useEffect(() => {
    if (expertDirection && !directionOptions.some((item) => item.value === expertDirection)) {
      setExpertDirection('');
    }
  }, [directionOptions, expertDirection]);

  async function selectEmployee(row: AgentProfileRead) {
    if (selectingAgentId) return;
    setSelectingAgentId(row.id);
    try {
      let selectedRow = row;
      if (!canSelectCurrentEmployeeAgent(row, currentUser, { activeOnly: true })) {
        selectedRow = await api.post<AgentProfileRead>(
          `/api/chat/agents/${encodeURIComponent(row.id)}/use?tenant_id=${getRequestTenantId()}`,
          {},
        );
        updateAgentInList(selectedRow);
      }
      setSelectedAgentId(selectedRow.id);
      persistSharedAgentScope(selectedRow.id, currentUser?.id);
      emitAgentScopeChange(selectedRow.id);
      navigate('/enterprise/dashboard');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载员工失败');
    } finally {
      setSelectingAgentId(null);
    }
  }

  function startEmployeeChat(row: AgentProfileRead) {
    navigate(`/workspace/chat/draft/${row.id}`);
  }

  async function updateStatus(row: AgentProfileRead, status: 'active' | 'archived') {
    try {
      await api.put<AgentProfileRead>(`/api/enterprise/agents/${row.id}`, {
        tenant_id: getRequestTenantId(),
        status,
        metadata: row.metadata || {},
      });
      notify.success(status === 'active' ? '员工已上线' : '员工已下线');
      await load();
      window.dispatchEvent(new Event('gongge-enterprise-agent-scope-refresh'));
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '更新员工状态失败');
    }
  }

  async function updateGalleryState(row: AgentProfileRead, published: boolean) {
    try {
      await api.put<AgentProfileRead>(`/api/enterprise/agents/${row.id}/gallery-publication`, {
        tenant_id: getRequestTenantId(),
        published,
      });
      notify.success(published ? '已发布到广场' : '已从广场下架');
      await load();
      window.dispatchEvent(new Event('gongge-enterprise-agent-scope-refresh'));
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '更新广场状态失败');
    }
  }

  async function submitAgentPublication(row: AgentProfileRead): Promise<boolean> {
    setPublicationBusy(true);
    try {
      await api.post('/api/enterprise/publications', {
        resource_type: 'agent',
        resource_id: row.id,
        expected_resource_revision: row.profile_revision || 1,
      });
      notify.success('已提交整 Agent 冻结快照；另一位管理员批准后才会进入组织发布库');
      return true;
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '提交整 Agent 组织审核失败');
      return false;
    } finally {
      setPublicationBusy(false);
    }
  }

  async function openOrganizationizationPreview(row: AgentProfileRead, submitWhenReady = false) {
    setOrganizationPreviewBusy(true);
    try {
      const preview = await api.get<AgentOrganizationizationPreviewRead>(
        `/api/enterprise/agents/${encodeURIComponent(row.id)}/organizationization-preview?tenant_id=${getRequestTenantId()}`,
      );
      setOrganizationPreview(preview);
      setOrganizationDraft({
        responsibleOrgUnitId: preview.responsible_org_unit_id || '',
        roleCode: preview.active_role_code || '',
        supervisorProfileId: preview.active_supervisor_employee_profile_id || '',
      });
      setOrganizationOptions(null);
      if (canGovernAgents && preview.governance_form !== 'organization_employee') {
        try {
          setOrganizationOptions(await api.get<AgentOrganizationizationOptionsRead>(
            `/api/enterprise/agents/organizationization-options?tenant_id=${getRequestTenantId()}`,
          ));
        } catch (error) {
          notify.error(error instanceof Error ? error.message : t('加载组织化选项失败'));
        }
      }
      if (submitWhenReady && preview.can_submit) {
        const submitted = await submitAgentPublication(row);
        if (submitted) {
          setOrganizationPreview(null);
          await load();
        }
      }
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '读取组织化条件失败');
    } finally {
      setOrganizationPreviewBusy(false);
    }
  }

  async function configureOrganizationization() {
    const preview = organizationPreview;
    if (!preview || !organizationDraft.responsibleOrgUnitId || !organizationDraft.roleCode || !organizationDraft.supervisorProfileId) {
      notify.error(t('请先选择责任组织、业务角色和监督者'));
      return;
    }
    setOrganizationPreviewBusy(true);
    try {
      const result = await api.post<AgentOrganizationizationResultRead>(
        `/api/enterprise/agents/${encodeURIComponent(preview.agent_id)}/organizationization`,
        {
          tenant_id: preview.tenant_id,
          command_id: `agent-organizationization-${createClientId()}`,
          expected_profile_revision: preview.profile_revision,
          expected_relationship_checksum: preview.relationship_checksum,
          responsible_org_unit_id: organizationDraft.responsibleOrgUnitId,
          role_code: organizationDraft.roleCode,
          supervisor_employee_profile_id: organizationDraft.supervisorProfileId,
          assignment_mode: 'assist',
          scope_type: 'tenant',
          scope_id: '*',
          include_descendants: true,
        },
      );
      setOrganizationPreview(result.preview);
      setOrganizationDraft({
        responsibleOrgUnitId: result.preview.responsible_org_unit_id || '',
        roleCode: result.preview.active_role_code || '',
        supervisorProfileId: result.preview.active_supervisor_employee_profile_id || '',
      });
      notify.success(result.result_status === 'unchanged' ? t('组织化关系未发生变化') : t('组织化关系已原子保存'));
      await load();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : t('保存组织化关系失败，请重新预览'));
    } finally {
      setOrganizationPreviewBusy(false);
    }
  }

  async function openAgentReleases() {
    setPublicationOpen(true);
    setPublicationBusy(true);
    try {
      setAgentReleases(await api.get<AgentPublicationRelease[]>(
        `/api/enterprise/publications/releases?resource_type=agent${canReviewAgentReleases ? '&include_history=true' : ''}`,
      ));
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载组织数字员工发布库失败');
    } finally {
      setPublicationBusy(false);
    }
  }

  async function rollbackAgentRelease() {
    const target = rollbackTarget;
    if (!target) return;
    const active = agentReleases.find(
      (release) => release.resource_id === target.resource_id && release.status === 'active',
    );
    if (!active) {
      notify.error('没有找到可校验的当前 Release，请刷新发布库');
      return;
    }
    const reason = rollbackReason.trim();
    if (!reason) {
      notify.error('请填写回滚原因');
      return;
    }
    setPublicationBusy(true);
    try {
      await api.post(
        `/api/enterprise/publications/releases/${encodeURIComponent(target.id)}/rollback`,
        {
          command_id: createClientId(),
          expected_active_release_id: active.id,
          expected_active_row_version: active.row_version,
          expected_target_row_version: target.row_version,
          reason,
        },
      );
      notify.success(`已将「${target.name}」回滚为当前组织发布版本`);
      setRollbackTarget(null);
      setRollbackReason('');
      await openAgentReleases();
      await load();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '组织数字员工回滚失败，请刷新后重试');
      await openAgentReleases();
    } finally {
      setPublicationBusy(false);
    }
  }

  async function transitionAgentRelease() {
    const target = transitionTarget;
    if (!target || target.status !== 'active') return;
    const reason = transitionReason.trim();
    if (!reason) {
      notify.error('请填写发布状态变更原因');
      return;
    }
    setPublicationBusy(true);
    try {
      await api.post(
        `/api/enterprise/publications/releases/${encodeURIComponent(target.id)}/transition`,
        {
          command_id: createClientId(),
          command: transitionCommand,
          expected_row_version: target.row_version,
          reason,
        },
      );
      notify.success(transitionCommand === 'security_revoke' ? '已安全撤销组织数字员工 Release' : '已将组织数字员工 Release 下架');
      setTransitionTarget(null);
      setTransitionReason('');
      await openAgentReleases();
      await load();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '更新组织数字员工发布状态失败，请刷新后重试');
      await openAgentReleases();
    } finally {
      setPublicationBusy(false);
    }
  }

  async function adoptAgentRelease(release: AgentPublicationRelease) {
    setPublicationBusy(true);
    try {
      await api.post(`/api/enterprise/publications/releases/${encodeURIComponent(release.id)}/adopt`, {
        idempotency_key: `agent-adopt-${createClientId()}`,
      });
      notify.success(`已从冻结发布物创建「${release.name}（采用）」；私人记忆和连接凭据未复制`);
      setPublicationOpen(false);
      await load();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '采用组织数字员工失败');
    } finally {
      setPublicationBusy(false);
    }
  }

  async function confirmDelete() {
    const row = deleteTarget;
    if (!row) return;
    setDeleting(true);
    try {
      const result = await api.delete<AgentDeletionResult>(
        `/api/enterprise/agents/${row.id}?tenant_id=${getRequestTenantId()}`,
      );
      if (window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) === row.id) {
        const nextAgent = employees.find((item) => item.id !== row.id && item.status === 'active')
          || employees.find((item) => item.id !== row.id);
        if (nextAgent) {
          window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, nextAgent.id);
          window.dispatchEvent(new CustomEvent('gongge-enterprise-agent-scope-change', { detail: { agentId: nextAgent.id } }));
        } else {
          window.localStorage.removeItem(ENTERPRISE_AGENT_STORAGE_KEY);
          window.dispatchEvent(new CustomEvent('gongge-enterprise-agent-scope-change', { detail: { agentId: '' } }));
        }
      }
      notify.success(
        result.status === 'deletion_pending'
          ? `员工入口已关闭，后台将自动重试清理${result.pending_execution_ids.length || result.pending_resource_ids.length ? `（待处理执行 ${result.pending_execution_ids.length}、资源 ${result.pending_resource_ids.length}）` : ''}`
          : '员工已删除，历史审计与执行记录仍保留',
      );
      setDeleteTarget(null);
      await load();
      window.dispatchEvent(new Event('gongge-enterprise-agent-scope-refresh'));
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '删除员工失败');
    } finally {
      setDeleting(false);
    }
  }

  function updateAgentInList(row: AgentProfileRead) {
    setAgents((current) => current.map((item) => (item.id === row.id ? row : item)));
  }

  function toggleExpert(agentId: string, checked: boolean) {
    setSelectedExpertIds((current) => {
      const next = new Set(current);
      if (checked) next.add(agentId);
      else next.delete(agentId);
      return next;
    });
  }

  function openBulkClassification() {
    setClassificationTargets(expertEmployees.filter((item) => selectedExpertIds.has(item.id)));
  }

  async function saveExpertClassification(value: { category: string; subcategory: string }) {
    if (!classificationTargets.length || savingClassification) return;
    setSavingClassification(true);
    try {
      const result = await api.patch<ExpertTaxonomyAssignmentResult>(
        '/api/enterprise/expert-taxonomy/assignments',
        {
          tenant_id: getRequestTenantId(),
          agent_ids: classificationTargets.map((item) => item.id),
          category: value.category,
          subcategory: value.subcategory,
        },
      );
      notify.success(`已更新 ${result.updated_count} 位专家的分类`);
      setClassificationTargets([]);
      setSelectedExpertIds(new Set());
      await load();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '更新专家分类失败');
    } finally {
      setSavingClassification(false);
    }
  }

  function changeExpertDepartment(value: string) {
    setExpertDirection('');
    navigate(viewLink('expert', value));
  }

  function clearExpertFilters() {
    setExpertSource('');
    setExpertDirection('');
    setSearchTerm('');
    if (expertDepartment) navigate('?view=expert');
  }

  const hasSearchTerm = Boolean(searchTerm.trim());
  const hasExpertFilters = Boolean(expertSource || expertDepartment || expertDirection);

  const viewItems: SideNavPanelItem[] = [
    {
      key: 'capability', label: '我的能力分身', description: '我拥有的个人能力伙伴',
      count: governanceCounts.capability_avatar, icon: Users,
    },
    ...(canGovernAgents
      ? [{
          key: 'organization',
          label: '组织数字员工',
          description: `${governanceCounts.organization_employee} 位已就绪，${governanceCounts.organization_pending} 位待补齐`,
          count: governanceCounts.organization_employee + governanceCounts.organization_pending,
          icon: Bot,
        }]
      : []),
    {
      key: 'all', label: '可管理数字员工', description: '当前账号可维护的数字员工',
      count: viewCounts.all, icon: Users,
    },
    {
      key: 'online', label: '在线员工', description: '已上线，可投入使用',
      count: viewCounts.online, icon: UserCheck,
    },
    {
      key: 'offline', label: '下线员工', description: '已下线，暂停服务',
      count: viewCounts.offline, icon: UserX,
    },
    {
      key: 'pending', label: '待审批', description: '等待审批通过',
      count: viewCounts.pending, icon: Clock,
    },
    ...(!isAdmin ? [{
      key: 'expert',
      label: '我的专家能力分身',
      description: '管理本人拥有的专业能力分身',
      count: viewCounts.expert, icon: Star,
      children: viewCounts.expert
        ? [
            { key: '', label: '全部专家', count: viewCounts.expert },
            ...departmentOptions.map((option) => ({
              key: option.value,
              label: option.label,
              count: option.count,
            })),
          ]
        : undefined,
    }] : []),
    ...(canGovernAgents
      ? [{
          key: 'governance',
          label: '发布治理',
          description: '审核他人员工的发布状态',
          count: viewCounts.governance,
          icon: UserCheck,
        }]
      : []),
  ];

  const summaryCardClass =
    'flex h-full min-h-[136px] w-full items-center gap-[16px] rounded-[var(--gg-radius-card)] border border-[var(--gg-line)] bg-[var(--gg-surface)] px-[24px] py-[20px] text-left transition-shadow hover:border-[var(--gg-interaction)] hover:shadow-[var(--gg-shadow-card)]';
  const summaryStats: { key: EmployeeFilter; value: number; label: string; sub: string }[] = isExpertTemplateManagement
    ? [{
        key: 'expert',
        value: viewCounts.expert,
        label: '专家模板',
        sub: '平台内置模板，按专业方向管理',
      }]
    : [
        { key: 'all', value: viewCounts.all, label: '员工总数', sub: `${viewCounts.online}位在线` },
        { key: 'offline', value: viewCounts.offline, label: '下线员工', sub: '0位在线' },
        ...(!isAdmin ? [{
          key: 'expert' as const,
          value: viewCounts.expert,
          label: '专业能力分身',
          sub: '按专业方向管理',
        }] : []),
        {
          key: 'pending',
          value: viewCounts.pending,
          label: '待审批',
          sub: '等待审批通过',
        },
      ];

  const emptyState: { title: string; description: string; actionLabel?: string; onAction?: () => void } = (() => {
    if (employeeFilter === 'capability') {
      return {
        title: '还没有能力分身',
        description: '创建个人能力分身，或从开放广场的专家分类创建我的版本。',
        actionLabel: '新建数字员工',
        onAction: onCreateAgent,
      };
    }
    if (employeeFilter === 'organization') {
      return {
        title: '暂无组织数字员工',
        description: '先为能力分身补齐责任组织、业务角色、监督者和发布 Release。',
        actionLabel: '查看全部员工',
        onAction: () => navigate(viewLink('all')),
      };
    }
    if (employeeFilter === 'expert' && viewCounts.expert === 0 && !hasExpertFilters && !hasSearchTerm) {
      return {
        title: isAdmin ? '暂无专家模板' : '还没有专家能力分身',
        description: isAdmin
          ? '开放广场的专家分类展示已发布模板；本页只维护平台内置模板，用户复制后才进入“我的能力分身”。'
          : '开放广场的专家分类提供已审核的专业 Agent 模板；复制后成为你的能力分身，直接使用则建立使用关系。',
        actionLabel: '浏览开放广场的专家分类',
        onAction: () => navigate(EnterpriseRoute.PlatformExperts),
      };
    }
    if (employeeFilter === 'expert') {
      return {
        title: isAdmin ? '没有匹配的专家模板' : '没有匹配的专家能力分身',
        description: '调整筛选条件，或换个关键词再试试',
        actionLabel: '清除筛选',
        onAction: clearExpertFilters,
      };
    }
    if (hasSearchTerm) {
      return {
        title: '没有匹配的数字员工',
        description: '换个关键词再试试',
        actionLabel: '清除搜索',
        onAction: () => setSearchTerm(''),
      };
    }
    if (employeeFilter === 'all') {
      return {
        title: '还没有数字员工',
        description: '创建一位数字员工，或从开放广场复制已发布的配置作为起点。',
        actionLabel: '新建数字员工',
        onAction: onCreateAgent,
      };
    }
    return {
      online: { title: '当前没有在线员工', description: '将员工上线后即可在这里看到。' },
      offline: { title: '当前没有下线员工', description: '所有员工都处于在线状态。' },
      pending: { title: '当前没有待审批员工', description: '没有等待审批的员工。' },
      governance: {
        title: '当前没有待治理员工',
        description: '其他所有者尚未创建需要审核发布的数字员工。',
      },
      expert: { title: '', description: '' },
      all: { title: '', description: '' },
    }[employeeFilter];
  })();

  const employeeGrid = (
    <CatalogGrid
      family="resource"
      className={cn(
        RESOURCE_GRID_CLASS,
        selectedExpertIds.size > 0 && 'pb-[92px]',
      )}
    >
      {filteredEmployees.map((employee) => {
        const isExpertTemplateRow = isExpertTemplateManagement && employee.governance_form === 'template';
        const templateAvailable = employee.status === 'active' && isGalleryEmployee(employee);
        return <EmployeeCard
          key={employee.id}
          employee={employee}
          busy={selectingAgentId === employee.id}
          canManage={canManageEmployeeAgent(employee, currentUser)}
          canGovern={canGovernAgents}
          canChat={!isExpertTemplateRow && employeeFilter !== 'governance' && employee.governance_form !== 'organization_pending'}
          cardMode={isExpertTemplateRow ? 'expert-template' : 'employee'}
          statusLabel={isExpertTemplateRow
            ? employee.status !== 'active'
              ? '已停用'
              : templateAvailable ? '已发布到开放广场' : '待发布'
            : undefined}
          statusKind={isExpertTemplateRow ? templateAvailable ? 'available' : 'offline' : undefined}
          publicationLabel={isExpertTemplateRow
            ? templateAvailable ? '从开放广场下架' : '发布到开放广场'
            : undefined}
          showGovernanceForm={employeeFilter === 'capability' || employeeFilter === 'organization'}
          selected={employee.id === selectedAgentId}
          selectable={employeeFilter === 'expert' && isAdmin && Boolean(taxonomy)}
          checked={selectedExpertIds.has(employee.id)}
          onCheckedChange={(checked) => toggleExpert(employee.id, checked)}
          onEditClassification={employeeFilter === 'expert' && isAdmin && taxonomy
            ? () => setClassificationTargets([employee])
            : undefined}
          onOpen={() => {
            if (isExpertTemplateManagement) {
              setProfileAgent(employee);
            } else if (employeeFilter === 'governance') {
              setProfileAgent(employee);
            } else if (
              employeeFilter === 'organization'
              || (employeeFilter === 'capability' && employee.governance_form === 'organization_pending')
            ) {
              void openOrganizationizationPreview(employee);
            } else {
              void selectEmployee(employee);
            }
          }}
          onStatus={(status) => void updateStatus(employee, status)}
          onGallery={(published) => void updateGalleryState(employee, published)}
          onPublication={!isExpertTemplateManagement && employee.governance_form !== 'template' && employee.owner_user_id === currentUser?.id
            ? () => void openOrganizationizationPreview(employee, true)
            : undefined}
          onDelete={() => setDeleteTarget(employee)}
          onAvatar={() => setAvatarAgent(employee)}
          onEdit={() => setProfileAgent(employee)}
          onChat={() => startEmployeeChat(employee)}
        />;
      })}
      {!filteredEmployees.length && (
        <AgentsEmptyState
          title={emptyState.title}
          description={emptyState.description}
          actionLabel={emptyState.actionLabel}
          onAction={emptyState.onAction}
        />
      )}
    </CatalogGrid>
  );

  const viewMeta = activeViewMeta;
  const workspaceEyebrow = employeeFilter === 'expert'
    ? isAdmin ? `${total} 个专家模板` : `${total} 个专家能力分身`
    : `${total} 位员工`;

  return (
    <PageShell template={isExpertTemplateManagement ? 'catalog' : 'management'} aria-busy={loading}>
      {isExpertTemplateManagement ? (
        <>
          <EnterpriseCatalogPageHeader
            backTo={EnterpriseRoute.Agents}
            backLabel="返回数字员工管理"
            title="专家模板管理"
            description="平台内置模板，按专业方向管理"
            onLogout={onLogout}
            userName={currentUser?.username}
          />
          <section className="mt-[20px] overflow-hidden rounded-[var(--gg-radius-panel)] border border-[var(--gg-line)] bg-[var(--gg-surface)] shadow-[var(--gg-shadow-card)]">
            <EnterpriseCatalogHero
              icon={Star}
              title="专家模板目录"
              description="开放广场的专家分类展示已发布模板；本页只维护平台内置模板，用户复制后才进入“我的能力分身”。"
              actions={(
                <>
                  <Button variant="outline" className="h-[34px] rounded-[10px]" onClick={() => void load()} disabled={loading}>
                    <RefreshCw className={cn('size-[14px]', loading && 'animate-spin')} />
                    刷新
                  </Button>
                  <Button asChild variant="outline" className="h-[34px] rounded-[10px]">
                    <Link to={EnterpriseRoute.PlatformExperts}>前往开放广场的专家分类</Link>
                  </Button>
                </>
              )}
            />
          </section>
        </>
      ) : (
        <>
          <AppHeader
            onLogout={onLogout}
            userName={currentUser?.username}
            left={(
              <div className="flex h-[50px] w-full items-center gap-[6px] rounded-[20px] bg-white px-[20px] text-[#757F9C] shadow-[0_0_6px_rgba(0,0,0,0.05)]">
                <IconSearch className="size-[20px] shrink-0" />
                <input
                  value={searchTerm}
                  onChange={(event) => setSearchTerm(event.target.value)}
                  placeholder="搜索"
                  aria-label="搜索员工"
                  className="min-w-0 flex-1 border-0 bg-transparent gg-type-body text-[#18181A] outline-none placeholder:text-[#757F9C]"
                />
              </div>
            )}
          />

          <div className="mt-[16px] flex max-w-[960px] flex-wrap items-center gap-[8px]">
            <ConceptNote topic="digital-employee" className="max-w-[760px]">
              {employeeFilter === 'expert'
                ? '本页只管理当前账号拥有的专家能力分身；平台内置专家模板请前往开放广场的专家分类。'
                : '本页管理 AI 数字员工；真人账号和组织任职请前往“成员管理”。'}
            </ConceptNote>
            <ConceptHelp topic="forms" triggerLabel="个人专家与组织数字员工" />
          </div>

          <CatalogGrid family="metric" className="my-[36px]" aria-label="数字员工统计">
            {summaryStats.map((stat) => (
              <button
                key={stat.key}
                type="button"
                aria-pressed={employeeFilter === stat.key}
                onClick={() => navigate(viewLink(stat.key))}
                className={cn(summaryCardClass)}
              >
                <span className="gg-type-metric shrink-0 text-[var(--gg-text-primary)]">{stat.value}</span>
                <span className="flex min-w-0 flex-col gap-[4px]">
                  <span className="gg-type-control whitespace-nowrap text-[var(--gg-text-secondary)]">{stat.label}</span>
                  <span className="gg-type-meta whitespace-nowrap">{stat.sub}</span>
                </span>
              </button>
            ))}
            <button type="button" onClick={onCreateAgent} className={cn(summaryCardClass)}>
              <span className="grid size-[38px] shrink-0 place-items-center text-[var(--gg-interaction)]">
                <IconPlus className="size-[38px]" />
              </span>
              <span className="flex min-w-0 flex-col gap-[4px]">
                <span className="gg-type-control whitespace-nowrap text-[var(--gg-text-secondary)]">创建新员工</span>
                <span className="gg-type-meta whitespace-nowrap">几步搭好你的数字员工</span>
              </span>
            </button>
          </CatalogGrid>
        </>
      )}

      <div className={cn(
        'grid grid-cols-[248px_minmax(0,1fr)] items-start gap-[16px] max-[920px]:grid-cols-1',
        isExpertTemplateManagement && 'mt-[20px] grid-cols-1',
      )}>
        {!isExpertTemplateManagement && <SideNavPanel
          title="员工视图"
          subtitle="按状态与类型浏览"
          icon={Users}
          aria-label="员工视图"
          items={viewItems}
          activeKey={employeeFilter}
          activeChildKey={expertDepartment}
          linkFor={viewLink}
          footer={(
            <>
              <strong className="gg-type-control text-[#464c5e]">视图说明</strong>
              <p className="mt-[3px] gg-type-body">视图仅筛选本页列表，不会改变员工的运行状态。</p>
            </>
          )}
        />}

        <main className="min-w-0 overflow-hidden rounded-[20px] border border-[#dfe5f2] bg-white shadow-[0_12px_32px_rgba(35,55,100,0.06)]">
          {!isExpertTemplateManagement && (
            <div className="flex items-start justify-between gap-[20px] px-[22px] py-[21px] max-[620px]:flex-col">
              <div className="min-w-0">
                <p className="font-mono gg-type-caption font-semibold uppercase tracking-[0.13em] text-[#6074a9]">{workspaceEyebrow}</p>
                <h2 className="mt-[5px] flex items-center gap-[6px] gg-type-section-title font-semibold tracking-[-0.02em] text-[#18181a]">
                  {viewMeta.title}
                  <ConceptHelp topic={employeeFilter === 'expert' ? 'expert' : 'digital-employee'} />
                </h2>
                <p className="mt-[5px] max-w-[680px] gg-type-meta  text-[#68718b]">{viewMeta.description}</p>
              </div>
              <div className="flex flex-wrap items-center justify-end gap-[8px]">
                <Button variant="outline" onClick={() => void openAgentReleases()}>
                  <Bot className="size-4" />
                  组织数字员工发布库
                </Button>
              </div>
            </div>
          )}

          <div className={cn('p-[18px]', !isExpertTemplateManagement && 'border-t border-[#eef1f6]')}>
            {isExpertTemplateManagement && (
              <label className="relative mb-[16px] block">
                <span className="sr-only">搜索专家模板</span>
                <IconSearch className="pointer-events-none absolute left-[11px] top-1/2 size-[14px] -translate-y-1/2 text-[#939bad]" />
                <input
                  value={searchTerm}
                  onChange={(event) => setSearchTerm(event.target.value)}
                  placeholder="搜索专家模板"
                  aria-label="搜索专家模板"
                  autoComplete="off"
                  className="h-[34px] w-full rounded-[var(--gg-radius-control)] border border-[var(--gg-border)] bg-white pl-[32px] pr-[12px] gg-type-meta text-[var(--gg-ink)] outline-none transition-colors placeholder:text-[var(--gg-slate)] focus:border-[var(--gg-cobalt)] focus:ring-2 focus:ring-[color-mix(in_srgb,var(--gg-cobalt)_16%,transparent)]"
                />
              </label>
            )}
            {employeeFilter === 'expert' && taxonomyUnavailable && isAdmin && (
              <p className="mb-[12px] rounded-[10px] bg-[#fff8e8] px-[12px] py-[9px] gg-type-caption text-[#8a6118]">
                分类规则加载失败，请刷新后重试；当前仍可浏览专家。
              </p>
            )}
            {employeeFilter === 'expert' && viewCounts.expert > 0 && (
              <ExpertFilterBar
                sourceOptions={sourceOptions}
                departmentOptions={departmentOptions}
                directionOptions={directionOptions}
                source={expertSource}
                department={expertDepartment}
                direction={expertDirection}
                resultCount={total}
                hasFilters={hasExpertFilters || hasSearchTerm}
                onSourceChange={(value) => {
                  setExpertSource(value);
                  setExpertDirection('');
                  if (expertDepartment) navigate('?view=expert');
                }}
                onDepartmentChange={changeExpertDepartment}
                onDirectionChange={setExpertDirection}
                onReset={clearExpertFilters}
              />
            )}
            {employeeGrid}
            {total > 0 && (
              <Paginator
                aria-label="数字员工管理分页"
                page={page}
                pageCount={Math.max(1, Math.ceil(total / AGENT_PAGE_SIZE))}
                onChange={setPage}
                className="mt-[20px]"
              />
            )}
          </div>
        </main>
      </div>

      {isAdmin && taxonomy && (
        <ExpertBulkActionBar
          count={selectedExpertIds.size}
          onEdit={openBulkClassification}
          onClear={() => setSelectedExpertIds(new Set())}
        />
      )}
      <ExpertClassificationDialog
        open={classificationTargets.length > 0}
        expertCount={classificationTargets.length}
        taxonomy={taxonomy}
        initialCategory={classificationTargets.length === 1 ? expertCategory(classificationTargets[0]) : ''}
        initialSubcategory={classificationTargets.length === 1 ? expertSubcategory(classificationTargets[0]) : ''}
        saving={savingClassification}
        onClose={() => setClassificationTargets([])}
        onSubmit={saveExpertClassification}
      />
      <EmployeeAvatarEditor
        agent={avatarAgent}
        open={Boolean(avatarAgent)}
        onClose={() => setAvatarAgent(null)}
        onSaved={updateAgentInList}
      />
      <EmployeeProfileEditor
        agent={profileAgent}
        open={Boolean(profileAgent)}
        mode={isExpertTemplateManagement && profileAgent?.governance_form === 'template' ? 'expert-template' : 'employee'}
        currentUser={currentUser}
        onClose={() => setProfileAgent(null)}
        onSaved={updateAgentInList}
      />
      <ConfirmDialog
        open={Boolean(deleteTarget)}
        onOpenChange={(open) => {
          if (!open) setDeleteTarget(null);
        }}
        loading={deleting}
        title={`删除员工「${deleteTarget ? employeeDisplayName(deleteTarget) : ''}」？`}
        description="删除会立即关闭员工入口并清理可回收配置；历史审计、执行记录和未能立即确认的外部资源会保留对账状态。"
        onConfirm={() => void confirmDelete()}
      />
      <Dialog open={publicationOpen} onOpenChange={(open) => !publicationBusy && setPublicationOpen(open)}>
        <DialogContent aria-describedby={undefined} className="max-h-[86vh] overflow-y-auto sm:max-w-[720px]">
          <DialogTitle className="flex items-center gap-2 gg-type-section-title font-semibold">
            <ShieldCheck className="size-5 text-[var(--gg-cobalt)]" />
            组织数字员工发布库
          </DialogTitle>
          <p className="gg-type-meta text-[#68718b]">
            这里只展示经职责分离审核的冻结快照。采用会创建归你所有的新数字员工，并固定已审 Persona 与组件版本；记忆、会话、连接账号、凭据和定时任务不会传播。
          </p>
          {publicationBusy && !agentReleases.length ? <p role="status">正在读取已审发布物…</p> : null}
          {!publicationBusy && !agentReleases.length ? (
            <div className="rounded-xl border border-dashed border-[#dfe5f2] px-5 py-10 text-center gg-type-meta text-[#7b8498]">
              当前没有可采用的组织数字员工发布物。
            </div>
          ) : null}
          <div className="grid gap-3">
            {agentReleases.map((release) => {
              const canRollback = canReviewAgentReleases && release.status === 'unpublished'
                && agentReleases.some((candidate) => candidate.resource_id === release.resource_id && candidate.status === 'active');
              return (
              <article key={release.id} className="rounded-[14px] border border-[#dce5f6] bg-[#fbfcff] p-4">
                <div className="flex items-start justify-between gap-4">
                  <div>
                    <h3 className="gg-type-card-title font-semibold text-[#202536]">{release.name}</h3>
                    <p className="mt-1 gg-type-meta text-[#68718b]">{release.description || '暂无说明'}</p>
                  </div>
                  <div className="flex shrink-0 flex-wrap justify-end gap-2">
                    {release.status === 'active' ? (
                      <>
                        <Button disabled={publicationBusy} onClick={() => void adoptAgentRelease(release)}>
                          采用为我的员工
                        </Button>
                        {canReviewAgentReleases ? (
                          <>
                            <Button
                              variant="outline"
                              disabled={publicationBusy}
                              onClick={() => {
                                setTransitionTarget(release);
                                setTransitionCommand('unpublish');
                                setTransitionReason('');
                              }}
                            >
                              普通下架
                            </Button>
                            <Button
                              variant="destructive"
                              disabled={publicationBusy}
                              onClick={() => {
                                setTransitionTarget(release);
                                setTransitionCommand('security_revoke');
                                setTransitionReason('');
                              }}
                            >
                              安全撤销
                            </Button>
                          </>
                        ) : null}
                      </>
                    ) : null}
                    {canRollback ? (
                      <Button
                        variant="outline"
                        disabled={publicationBusy}
                        onClick={() => {
                          setRollbackTarget(release);
                          setRollbackReason('');
                        }}
                      >
                        回滚此版本
                      </Button>
                    ) : null}
                  </div>
                </div>
                <div className="mt-3 flex flex-wrap gap-2 gg-type-caption text-[#53617d]">
                  <span className="rounded-full bg-white px-2.5 py-1 ring-1 ring-[#dfe6f5]">冻结组件 {release.components.length}</span>
                  <span className="rounded-full bg-white px-2.5 py-1 font-mono gg-type-caption ring-1 ring-[#dfe6f5]">{release.snapshot_checksum.slice(0, 12)}…</span>
                  <span className={`rounded-full px-2.5 py-1 ${release.status === 'active' ? 'bg-[#eef8f2] text-[#237a48]' : release.status === 'security_revoked' ? 'bg-[#fce7e7] text-[#b40a0a]' : 'bg-[#fff5df] text-[#936000]'}`}>
                    {release.status === 'active' ? '已审 Release' : release.status === 'security_revoked' ? '安全撤销' : '历史已下架'}
                  </span>
                </div>
              </article>
              );
            })}
          </div>
        </DialogContent>
      </Dialog>
      <Dialog
        open={Boolean(rollbackTarget)}
        onOpenChange={(open) => {
          if (!open && !publicationBusy) {
            setRollbackTarget(null);
            setRollbackReason('');
          }
        }}
      >
        <DialogContent aria-describedby={undefined} className="sm:max-w-[520px]">
          <DialogTitle>回滚组织数字员工版本</DialogTitle>
          <p className="gg-type-meta text-[#68718b]">
            只允许恢复同一 Agent 的普通下架历史 Release；安全撤销版本不可恢复。提交时会校验当前版本和历史版本的行修订，避免覆盖并发变更。
          </p>
          {rollbackTarget ? (
            <div className="grid gap-[12px]">
              <div className="rounded-[11px] border border-[#dce5ff] bg-[#f7f9ff] px-[12px] py-[10px] gg-type-meta text-[#53617d]">
                目标：<strong className="gg-type-control text-[#202536]">{rollbackTarget.name}</strong>
                <span className="ml-2 font-mono gg-type-caption">{rollbackTarget.snapshot_checksum.slice(0, 16)}…</span>
              </div>
              <label className="grid gap-[6px] gg-type-meta font-medium text-[#464c5e]">
                回滚原因
                <Textarea
                  aria-label="回滚原因"
                  value={rollbackReason}
                  onChange={(event) => setRollbackReason(event.target.value)}
                  placeholder="说明为何恢复该历史组织员工版本"
                  rows={4}
                />
              </label>
              <div className="flex justify-end gap-2">
                <Button variant="outline" disabled={publicationBusy} onClick={() => setRollbackTarget(null)}>取消</Button>
                <Button disabled={publicationBusy || !rollbackReason.trim()} onClick={() => void rollbackAgentRelease()}>
                  确认回滚
                </Button>
              </div>
            </div>
          ) : null}
        </DialogContent>
      </Dialog>
      <Dialog
        open={Boolean(transitionTarget)}
        onOpenChange={(open) => {
          if (!open && !publicationBusy) {
            setTransitionTarget(null);
            setTransitionReason('');
          }
        }}
      >
        <DialogContent aria-describedby={undefined} className="sm:max-w-[520px]">
          <DialogTitle>{transitionCommand === 'security_revoke' ? '安全撤销组织数字员工 Release' : '普通下架组织数字员工 Release'}</DialogTitle>
          <p className="gg-type-meta text-[#68718b]">
            {transitionCommand === 'security_revoke'
              ? '安全撤销会停止组织广场发现、停用既有采用副本及其资源绑定，并提升租户授权修订；该版本不可回滚。'
              : '普通下架只停止组织广场发现和新的采用，既有采用副本继续按原授权运行。'}
          </p>
          {transitionTarget ? (
            <div className="grid gap-[12px]">
              <div className="rounded-[11px] border border-[#dce5ff] bg-[#f7f9ff] px-[12px] py-[10px] gg-type-meta text-[#53617d]">
                目标：<strong className="gg-type-control text-[#202536]">{transitionTarget.name}</strong>
                <span className="ml-2 font-mono gg-type-caption">Release {transitionTarget.id}</span>
              </div>
              <label className="grid gap-[6px] gg-type-meta font-medium text-[#464c5e]">
                变更原因
                <Textarea
                  aria-label="发布状态变更原因"
                  value={transitionReason}
                  onChange={(event) => setTransitionReason(event.target.value)}
                  placeholder={transitionCommand === 'security_revoke' ? '说明供应链、权限或内容安全事件' : '说明普通下架原因'}
                  rows={4}
                />
              </label>
              <div className="flex justify-end gap-2">
                <Button variant="outline" disabled={publicationBusy} onClick={() => setTransitionTarget(null)}>取消</Button>
                <Button
                  variant={transitionCommand === 'security_revoke' ? 'destructive' : 'default'}
                  disabled={publicationBusy || !transitionReason.trim()}
                  onClick={() => void transitionAgentRelease()}
                >
                  {publicationBusy ? '提交中…' : transitionCommand === 'security_revoke' ? '确认安全撤销' : '确认普通下架'}
                </Button>
              </div>
            </div>
          ) : null}
        </DialogContent>
      </Dialog>
      <Dialog
        open={Boolean(organizationPreview)}
        onOpenChange={(open) => {
          if (!open && !organizationPreviewBusy) setOrganizationPreview(null);
        }}
      >
        <DialogContent aria-describedby={undefined} className="max-h-[86vh] overflow-y-auto sm:max-w-[620px]">
          <DialogTitle className="gg-type-section-title font-semibold">{t('组织化检查：')}{organizationPreview?.agent_name}</DialogTitle>
          <p className="gg-type-meta text-[#68718b]">
            组织化只改变治理关系和发布状态，不复制真人身份、私人凭据或会话记忆。所有事实以服务端回查为准。
          </p>
          {organizationPreviewBusy ? <p className="gg-type-body" role="status">{t('正在读取组织化条件…')}</p> : null}
          {organizationPreview && (
            <div className="grid gap-[10px]">
              <div className="rounded-[12px] border border-[#dfe5f2] bg-[#f8faff] px-[14px] py-[11px] gg-type-meta text-[#53617d]">
                {t('当前形态：')}<strong className="gg-type-control text-[#202536]">
                  {organizationPreview.governance_form === 'organization_employee'
                  ? t('组织数字员工')
                    : organizationPreview.governance_form === 'organization_pending'
                      ? t('待组织化')
                      : organizationPreview.governance_form === 'capability_avatar'
                        ? t('能力分身')
                        : t('专家模板')}
                </strong>
                {organizationPreview.responsible_org_unit_name
                  ? t('· 责任组织：{1}', { 1: organizationPreview.responsible_org_unit_name })
                  : ''}
              </div>
              {canGovernAgents && organizationPreview.governance_form !== 'organization_employee' && (
                <section
                  aria-label={t('组织化关系配置')}
                  className="grid gap-[10px] rounded-[12px] border border-[#d8e2fb] bg-[#f7f9ff] px-[14px] py-[13px]"
                >
                  <div>
                    <h3 className="gg-type-card-title font-semibold text-[#303a52]">{t('配置组织关系')}</h3>
                    <p className="mt-[3px] gg-type-caption  text-[#737d92]">
                      {t('选择后的责任组织、业务角色和监督者会在同一事务中保存；保存后仍需提交组织审核。')}
                    </p>
                  </div>
                  <label className="grid gap-[5px] gg-type-caption font-medium text-[#53617d]">
                    {t('责任组织')}
                    <select
                      aria-label={t('选择责任组织')}
                      value={organizationDraft.responsibleOrgUnitId}
                      onChange={(event) => setOrganizationDraft((current) => ({
                        ...current,
                        responsibleOrgUnitId: event.target.value,
                      }))}
                      className="h-9 rounded-[9px] border border-[#d8e0f0] bg-white px-3 gg-type-meta font-normal text-[#303a52] outline-none focus:border-[var(--gg-cobalt)] focus:ring-2 focus:ring-[color-mix(in_srgb,var(--gg-cobalt)_16%,transparent)]"
                    >
                      <option value="">{t('请选择责任组织')}</option>
                      {(organizationOptions?.organizations || []).map((option) => (
                        <option key={option.id} value={option.id}>{option.name}</option>
                      ))}
                    </select>
                  </label>
                  <label className="grid gap-[5px] gg-type-caption font-medium text-[#53617d]">
                    {t('业务角色')}
                    <select
                      aria-label={t('选择业务角色')}
                      value={organizationDraft.roleCode}
                      onChange={(event) => setOrganizationDraft((current) => ({
                        ...current,
                        roleCode: event.target.value,
                      }))}
                      className="h-9 rounded-[9px] border border-[#d8e0f0] bg-white px-3 gg-type-meta font-normal text-[#303a52] outline-none focus:border-[var(--gg-cobalt)] focus:ring-2 focus:ring-[color-mix(in_srgb,var(--gg-cobalt)_16%,transparent)]"
                    >
                      <option value="">{t('请选择业务角色')}</option>
                      {(organizationOptions?.roles || []).map((option) => (
                        <option key={option.role_code} value={option.role_code}>{option.name} · {option.role_code}</option>
                      ))}
                    </select>
                  </label>
                  <label className="grid gap-[5px] gg-type-caption font-medium text-[#53617d]">
                    {t('监督者')}
                    <select
                      aria-label={t('选择监督者')}
                      value={organizationDraft.supervisorProfileId}
                      onChange={(event) => setOrganizationDraft((current) => ({
                        ...current,
                        supervisorProfileId: event.target.value,
                      }))}
                      className="h-9 rounded-[9px] border border-[#d8e0f0] bg-white px-3 gg-type-meta font-normal text-[#303a52] outline-none focus:border-[var(--gg-cobalt)] focus:ring-2 focus:ring-[color-mix(in_srgb,var(--gg-cobalt)_16%,transparent)]"
                    >
                      <option value="">{t('请选择监督者')}</option>
                      {(organizationOptions?.supervisors || []).map((option) => (
                        <option key={option.id} value={option.id}>{option.employee_name} · {option.employee_id}</option>
                      ))}
                    </select>
                  </label>
                  <div className="flex items-center justify-between gap-3">
                    <span className="gg-type-caption text-[#7b8498]">
                      {organizationOptions ? t('选项来自当前 agent.manage 授权范围') : t('正在读取可用组织化选项…')}
                    </span>
                    <Button
                      variant="outline"
                      onClick={() => void configureOrganizationization()}
                      disabled={organizationPreviewBusy || !organizationOptions}
                    >
                      {t('保存组织化配置')}
                    </Button>
                  </div>
                </section>
              )}
              <div className="grid gap-[7px]" aria-label="组织化前置条件">
                {organizationPreview.requirements.map((requirement) => (
                  <div
                    key={requirement.code}
                    className={`flex items-start justify-between gap-[12px] rounded-[10px] border px-[12px] py-[9px] gg-type-caption ${requirement.satisfied ? 'border-[#ccebd8] bg-[#f2fbf5]' : 'border-[#f0d6a7] bg-[#fff8e8]'}`}
                  >
                    <span className="min-w-0">
                      <strong className="block text-[#3f485d]">{requirement.label}</strong>
                      <span className="mt-[2px] block  text-[#737d92]">
                        {requirement.satisfied ? '当前事实已验证' : requirement.detail}
                      </span>
                    </span>
                    <span className={`shrink-0 font-medium ${requirement.satisfied ? 'text-[#237a48]' : 'text-[#9a6414]'}`}>
                      {requirement.satisfied ? '已满足' : '待补齐'}
                    </span>
                  </div>
                ))}
              </div>
              <div className="flex flex-wrap items-center justify-end gap-[8px]">
                <Button variant="outline" onClick={() => setOrganizationPreview(null)} disabled={organizationPreviewBusy}>
                  关闭
                </Button>
                {organizationPreview.can_submit
                  && organizationPreview.governance_form === 'organization_pending'
                  && organizationPreview.owner_user_id === currentUser?.id && (
                  <Button
                    disabled={organizationPreviewBusy}
                    onClick={() => {
                      const current = organizationPreview;
                      void submitAgentPublication({
                        id: current.agent_id,
                        tenant_id: current.tenant_id,
                        name: current.agent_name,
                        is_overall: false,
                        status: 'active',
                        profile_revision: current.profile_revision,
                        metadata: {},
                        resources: [],
                        created_at: '',
                        updated_at: '',
                      }).then((submitted) => {
                        if (submitted) {
                          setOrganizationPreview(null);
                          void load();
                        }
                      });
                    }}
                  >
                    提交组织审核
                  </Button>
                )}
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </PageShell>
  );
}

function AgentsEmptyState({
  title,
  description,
  actionLabel,
  onAction,
}: {
  title: string;
  description: string;
  actionLabel?: string;
  onAction?: () => void;
}) {
  return (
    <PageState
      kind="empty"
      title={title}
      description={description}
      icon={<IconSearch className="size-[16px]" />}
      action={actionLabel && onAction ? (
        <button type="button" onClick={onAction} className="gg-type-control text-[var(--gg-interaction)] hover:underline">
          {actionLabel}
        </button>
      ) : undefined}
    />
  );
}

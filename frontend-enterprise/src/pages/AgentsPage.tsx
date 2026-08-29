import { notify } from '@/components/ui/app-toast';
import { cn } from '@/lib/utils';
import { createClientId } from '@/lib/client-id';

import { Bot, Clock, ShieldCheck, Star, UserCheck, Users, UserX } from 'lucide-react';

import IconPlus from '../assets/icons/plus.svg?react';
import IconSearch from '../assets/icons/search.svg?react';

import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';

import { api, getRequestTenantId } from '../api/client';
import { hasGovernancePermission, type EnterpriseAuthUser } from '../auth';

import AppHeader from '../components/AppHeader';
import { ConceptHelp, ConceptNote } from '../components/ConceptHelp';
import { ConfirmDialog } from '../components/ConfirmDialog';
import EmployeeAvatarEditor from '../components/EmployeeAvatarEditor';
import EmployeeCard from '../components/EmployeeCard';
import EmployeeProfileEditor from '../components/EmployeeProfileEditor';
import { Paginator } from '../components/Paginator';
import ExpertBulkActionBar from '../components/ExpertBulkActionBar';
import ExpertClassificationDialog from '../components/ExpertClassificationDialog';
import ExpertFilterBar from '../components/ExpertFilterBar';
import SideNavPanel, { type SideNavPanelItem } from '../components/SideNavPanel';
import { Button, Dialog, DialogContent, DialogTitle } from '@/components/ui';
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
  AgentProfileRead,
  ExpertTaxonomyAssignmentResult,
  ExpertTaxonomyRead,
} from '../types';

const ENTERPRISE_AGENT_STORAGE_KEY = 'gongge_enterprise_agent_scope';
const AGENT_PAGE_SIZE = 12;
const EMPTY_VIEW_COUNTS: AgentManagementPageRead['view_counts'] = {
  all: 0, online: 0, offline: 0, pending: 0, expert: 0, governance: 0,
};
type EmployeeFilter = 'all' | 'online' | 'offline' | 'pending' | 'expert' | 'governance';
type AgentPublicationRelease = {
  id: string;
  resource_type: 'agent';
  resource_id: string;
  snapshot_id: string;
  snapshot_checksum: string;
  name: string;
  description: string;
  components: Array<{ resource_type: string; resource_id: string; metadata?: Record<string, unknown> }>;
};
const EMPLOYEE_VIEWS: EmployeeFilter[] = [
  'all',
  'online',
  'offline',
  'pending',
  'expert',
  'governance',
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
    title: '专家（能力分身）',
    description: '专家仍是数字员工，强调某个用户或专业方向沉淀的知识、方法、SOP 与工具组合。',
  },
  governance: {
    title: '发布治理',
    description: '只审核责任人、分类、状态与发布范围；不会获得所有者的私人配置编辑权。',
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
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(
    () => window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY),
  );
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const viewParam = searchParams.get('view');
  const employeeFilter: EmployeeFilter = isEmployeeFilter(viewParam) ? viewParam : 'all';
  const expertDepartment = searchParams.get('dept') || '';
  const canGovernAgents = isAdmin || hasGovernancePermission(currentUser, 'agent.manage');

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
      setAgents(result.items);
      setTotal(result.total);
      setViewCounts(result.view_counts);
      setFacets(result.facets);
      const lastPage = Math.max(1, Math.ceil(result.total / result.page_size));
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

  async function submitAgentPublication(row: AgentProfileRead) {
    setPublicationBusy(true);
    try {
      await api.post('/api/enterprise/publications', {
        resource_type: 'agent',
        resource_id: row.id,
        expected_resource_revision: row.profile_revision || 1,
      });
      notify.success('已提交整 Agent 冻结快照；另一位管理员批准后才会进入组织发布库');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '提交整 Agent 组织审核失败');
    } finally {
      setPublicationBusy(false);
    }
  }

  async function openAgentReleases() {
    setPublicationOpen(true);
    setPublicationBusy(true);
    try {
      setAgentReleases(await api.get<AgentPublicationRelease[]>(
        '/api/enterprise/publications/releases?resource_type=agent',
      ));
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载组织数字员工发布库失败');
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
    {
      key: 'expert', label: '专家（能力分身）', description: '按专业部门浏览',
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
    },
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
    'flex h-[100px] flex-1 basis-[220px] items-center gap-[16px] rounded-[20px] bg-[#f6f6f6] px-[32px] py-[20px] text-left transition-shadow';
  const summaryStats: { key: EmployeeFilter; value: number; label: string; sub: string }[] = [
    { key: 'all', value: viewCounts.all, label: '员工总数', sub: `${viewCounts.online}位在线` },
    { key: 'offline', value: viewCounts.offline, label: '下线员工', sub: '0位在线' },
    { key: 'expert', value: viewCounts.expert, label: '专家总数', sub: '按专业方向管理' },
    {
      key: 'pending',
      value: viewCounts.pending,
      label: '待审批',
      sub: '等待审批通过',
    },
  ];

  const emptyState: { title: string; description: string; actionLabel?: string; onAction?: () => void } = (() => {
    if (employeeFilter === 'expert' && viewCounts.expert === 0 && !hasExpertFilters && !hasSearchTerm) {
      return {
        title: '还没有专家',
        description: isAdmin
          ? '专家是带有专业部门与方向标签的数字员工，可从开放广场复制已发布的专家，或由管理员导入专家库。'
          : '专家是带有专业部门与方向标签的数字员工，可从开放广场复制已发布的专家，或联系管理员导入。',
        actionLabel: '浏览开放广场',
        onAction: () => navigate(EnterpriseRoute.Platform),
      };
    }
    if (employeeFilter === 'expert') {
      return {
        title: '没有匹配的专家',
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
    <div className={cn(
      'grid auto-rows-[minmax(262px,auto)] grid-cols-1 content-start gap-[32px] sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4 max-[900px]:gap-[18px]',
      selectedExpertIds.size > 0 && 'pb-[92px]',
    )}>
      {filteredEmployees.map((employee) => (
        <EmployeeCard
          key={employee.id}
          employee={employee}
          busy={selectingAgentId === employee.id}
          canManage={canManageEmployeeAgent(employee, currentUser)}
          canGovern={canGovernAgents}
          canChat={employeeFilter !== 'governance'}
          selected={employee.id === selectedAgentId}
          selectable={employeeFilter === 'expert' && isAdmin && Boolean(taxonomy)}
          checked={selectedExpertIds.has(employee.id)}
          onCheckedChange={(checked) => toggleExpert(employee.id, checked)}
          onEditClassification={employeeFilter === 'expert' && isAdmin && taxonomy
            ? () => setClassificationTargets([employee])
            : undefined}
          onOpen={() => {
            if (employeeFilter === 'governance') {
              setProfileAgent(employee);
            } else {
              void selectEmployee(employee);
            }
          }}
          onStatus={(status) => void updateStatus(employee, status)}
          onGallery={(published) => void updateGalleryState(employee, published)}
          onPublication={() => void submitAgentPublication(employee)}
          onDelete={() => setDeleteTarget(employee)}
          onAvatar={() => setAvatarAgent(employee)}
          onEdit={() => setProfileAgent(employee)}
          onChat={() => startEmployeeChat(employee)}
        />
      ))}
      {!filteredEmployees.length && (
        <AgentsEmptyState
          title={emptyState.title}
          description={emptyState.description}
          actionLabel={emptyState.actionLabel}
          onAction={emptyState.onAction}
        />
      )}
    </div>
  );

  const viewMeta = VIEW_META[employeeFilter];
  const workspaceEyebrow = employeeFilter === 'expert'
    ? `${total} 位专家`
    : `${total} 位员工`;

  return (
    <div className="min-h-full box-border px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]" aria-busy={loading}>
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
              className="min-w-0 flex-1 border-0 bg-transparent text-[14px] text-[#18181A] outline-none placeholder:text-[#757F9C]"
            />
          </div>
        )}
      />

      <div className="mt-[16px] flex max-w-[960px] flex-wrap items-center gap-[8px]">
        <ConceptNote topic="digital-employee" className="max-w-[760px]">
          本页管理 AI 数字员工；真人账号和组织任职请前往“成员管理”。
        </ConceptNote>
        <ConceptHelp topic="forms" triggerLabel="个人专家与组织数字员工" />
      </div>

      <div className="flex flex-wrap items-stretch gap-[20px] my-[36px]" aria-label="数字员工统计">
        {summaryStats.map((stat) => (
          <button
            key={stat.key}
            type="button"
            aria-pressed={employeeFilter === stat.key}
            onClick={() => navigate(viewLink(stat.key))}
            className={cn(
              summaryCardClass,
            )}
          >
            <span className="shrink-0 text-[34px] font-semibold leading-none text-[#18181A]">{stat.value}</span>
            <span className="flex min-w-0 flex-col gap-[4px]">
              <span className="whitespace-nowrap text-[14px] text-[#464C5E]">{stat.label}</span>
              <span className="whitespace-nowrap text-[12px] text-[#757F9C]">{stat.sub}</span>
            </span>
          </button>
        ))}
        <button type="button" onClick={onCreateAgent} className={cn(summaryCardClass, 'hover:shadow-[0_16px_30px_0_rgba(0,0,0,0.10)]')}>
          <span className="grid size-[38px] shrink-0 place-items-center text-[#18181A]">
            <IconPlus className="size-[38px]" />
          </span>
          <span className="flex min-w-0 flex-col gap-[4px]">
            <span className="whitespace-nowrap text-[14px] text-[#464C5E]">创建新员工</span>
            <span className="whitespace-nowrap text-[12px] text-[#757F9C]">几步搭好你的数字员工</span>
          </span>
        </button>
      </div>

      <div className="grid grid-cols-[248px_minmax(0,1fr)] items-start gap-[16px] max-[920px]:grid-cols-1">
        <SideNavPanel
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
              <strong className="text-[#464c5e]">视图说明</strong>
              <p className="mt-[3px]">视图仅筛选本页列表，不会改变员工的运行状态。</p>
            </>
          )}
        />

        <main className="min-w-0 overflow-hidden rounded-[20px] border border-[#dfe5f2] bg-white shadow-[0_12px_32px_rgba(35,55,100,0.06)]">
          <div className="flex items-start justify-between gap-[20px] px-[22px] py-[21px] max-[620px]:flex-col">
            <div className="min-w-0">
              <p className="font-mono text-[10px] font-semibold uppercase tracking-[0.13em] text-[#6074a9]">{workspaceEyebrow}</p>
              <h2 className="mt-[5px] flex items-center gap-[6px] text-[20px] font-semibold tracking-[-0.02em] text-[#18181a]">
                {viewMeta.title}
                <ConceptHelp topic={employeeFilter === 'expert' ? 'expert' : 'digital-employee'} />
              </h2>
              <p className="mt-[5px] max-w-[680px] text-[12px] leading-[19px] text-[#68718b]">{viewMeta.description}</p>
            </div>
            <Button variant="outline" onClick={() => void openAgentReleases()}>
              <Bot className="size-4" />
              组织数字员工发布库
            </Button>
          </div>

          <div className="border-t border-[#eef1f6] p-[18px]">
            {employeeFilter === 'expert' && taxonomyUnavailable && isAdmin && (
              <p className="mb-[12px] rounded-[10px] bg-[#fff8e8] px-[12px] py-[9px] text-[11px] text-[#8a6118]">
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
          <DialogTitle className="flex items-center gap-2 text-[17px] font-semibold">
            <ShieldCheck className="size-5 text-[var(--gg-cobalt)]" />
            组织数字员工发布库
          </DialogTitle>
          <p className="text-[12px] leading-5 text-[#68718b]">
            这里只展示经职责分离审核的冻结快照。采用会创建归你所有的新数字员工，并固定已审 Persona 与组件版本；记忆、会话、连接账号、凭据和定时任务不会传播。
          </p>
          {publicationBusy && !agentReleases.length ? <p role="status">正在读取已审发布物…</p> : null}
          {!publicationBusy && !agentReleases.length ? (
            <div className="rounded-xl border border-dashed border-[#dfe5f2] px-5 py-10 text-center text-[12px] text-[#7b8498]">
              当前没有可采用的组织数字员工发布物。
            </div>
          ) : null}
          <div className="grid gap-3">
            {agentReleases.map((release) => (
              <article key={release.id} className="rounded-[14px] border border-[#dce5f6] bg-[#fbfcff] p-4">
                <div className="flex items-start justify-between gap-4">
                  <div>
                    <h3 className="text-[14px] font-semibold text-[#202536]">{release.name}</h3>
                    <p className="mt-1 text-[12px] leading-5 text-[#68718b]">{release.description || '暂无说明'}</p>
                  </div>
                  <Button disabled={publicationBusy} onClick={() => void adoptAgentRelease(release)}>
                    采用为我的员工
                  </Button>
                </div>
                <div className="mt-3 flex flex-wrap gap-2 text-[11px] text-[#53617d]">
                  <span className="rounded-full bg-white px-2.5 py-1 ring-1 ring-[#dfe6f5]">冻结组件 {release.components.length}</span>
                  <span className="rounded-full bg-white px-2.5 py-1 font-mono ring-1 ring-[#dfe6f5]">{release.snapshot_checksum.slice(0, 12)}…</span>
                  <span className="rounded-full bg-[#eef8f2] px-2.5 py-1 text-[#237a48]">已审 Release</span>
                </div>
              </article>
            ))}
          </div>
        </DialogContent>
      </Dialog>
    </div>
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
    <div className="flex h-[262px] w-full items-center justify-center rounded-[20px] border border-dashed border-[#e4e9f2] bg-[#fbfcfe] px-[24px] text-center">
      <div className="flex max-w-[260px] flex-col items-center">
        <span className="grid size-[34px] place-items-center rounded-[12px] bg-white text-[#98a2b3] shadow-[0_1px_8px_rgba(70,76,94,0.06)] ring-1 ring-[#edf1f6]">
          <IconSearch className="size-[16px] shrink-0" />
        </span>
        <p className="mt-[12px] text-[14px] font-medium leading-[20px] text-[#7f879a]">
          {title}
        </p>
        <p className="mt-[4px] text-[11px] leading-[17px] text-[#a7adbb]">
          {description}
        </p>
        {actionLabel && onAction ? (
          <button type="button" onClick={onAction} className="mt-[10px] text-[11px] font-medium text-[var(--gg-cobalt)] hover:underline">
            {actionLabel}
          </button>
        ) : null}
      </div>
    </div>
  );
}

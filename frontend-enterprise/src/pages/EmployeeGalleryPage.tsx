import { UnderlineTabs, type UnderlineTabItem } from '@/components/ui';
import { notify } from '@/components/ui/app-toast';

import IconSearch from '../assets/icons/search.svg?react';

import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';

import { api, getRequestTenantId } from '../api/client';
import { isGalleryEmployee, type EnterpriseAuthUser } from '../auth';

import AppHeader from '../components/AppHeader';
import { ConfirmDialog } from '../components/ConfirmDialog';
import EmployeeAvatarEditor from '../components/EmployeeAvatarEditor';
import ExpertCategoryRail from '../components/ExpertCategoryRail';
import ExpertFilterBar from '../components/ExpertFilterBar';
import EmployeeCard from '../components/EmployeeCard';
import EmployeeProfileEditor from '../components/EmployeeProfileEditor';
import { Paginator } from '../components/Paginator';
import {
  canManageEmployeeAgent,
  employeeDisplayName,
  isEmployeeUsedByCurrentUser,
  isMyEmployeeAgent,
} from '../employee';
import { gsap, prefersReducedMotion } from '../lib/gsap';
import type { AgentDeletionResult, AgentGalleryPageRead, AgentProfileRead } from '../types';

const ENTERPRISE_AGENT_STORAGE_KEY = 'gongge_enterprise_agent_scope';
const GALLERY_PAGE_SIZE = 12;
const EMPTY_SCOPE_COUNTS: AgentGalleryPageRead['scope_counts'] = {
  used: 0,
  owned: 0,
  gallery: 0,
  expert: 0,
};
const EMPTY_FACETS: AgentGalleryPageRead['facets'] = {
  sources: [],
  departments: [],
  directions: [],
};

// 一级分组：「我的员工」（关系维度）与「发现」（目录维度）。
type GalleryView = 'mine' | 'discover';
type MineTab = 'used' | 'owned';
type DiscoverTab = 'gallery' | 'expert';
type GalleryScope = MineTab | DiscoverTab;
function tabLabel(text: string, count: number) {
  return (
    <span className="inline-flex items-center gap-[6px]">
      {text}
      {count > 0 && (
        <span className="rounded-full bg-[#eff1f7] px-[6px] py-[1px] text-[11px] leading-[16px] text-[#757f9c]">
          {count}
        </span>
      )}
    </span>
  );
}

export default function EmployeeGalleryPage({
  currentUser,
  isAdmin = false,
  onStartChat,
  onLogout,
}: {
  currentUser?: EnterpriseAuthUser;
  isAdmin?: boolean;
  onStartChat?: (agent: AgentProfileRead) => void | Promise<void>;
  onLogout?: () => void;
}) {
  const [agents, setAgents] = useState<AgentProfileRead[]>([]);
  const [loading, setLoading] = useState(false);
  const [avatarAgent, setAvatarAgent] = useState<AgentProfileRead | null>(null);
  const [profileAgent, setProfileAgent] = useState<AgentProfileRead | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<AgentProfileRead | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [startingAgentId, setStartingAgentId] = useState<string | null>(null);
  const [searchTerm, setSearchTerm] = useState('');
  const [appliedSearch, setAppliedSearch] = useState('');
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [scopeCounts, setScopeCounts] = useState(EMPTY_SCOPE_COUNTS);
  const [facets, setFacets] = useState(EMPTY_FACETS);
  const [refreshToken, setRefreshToken] = useState(0);
  const [expertSource, setExpertSource] = useState('');
  const [expertDepartment, setExpertDepartment] = useState('');
  const [expertDirection, setExpertDirection] = useState('');
  const loadRequestId = useRef(0);
  const gridRef = useRef<HTMLDivElement>(null);
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();

  // 视图状态由 URL 驱动，刷新/分享后仍停留在原位置。
  const view: GalleryView = searchParams.get('view') === 'discover' ? 'discover' : 'mine';
  const subParam = searchParams.get('sub') || '';
  const mineTab: MineTab = subParam === 'owned' ? 'owned' : 'used';
  const discoverTab: DiscoverTab = subParam === 'expert' ? 'expert' : 'gallery';
  const activeScope: GalleryScope = view === 'mine' ? mineTab : discoverTab;
  // 记住各分组上次停留的二级 tab，切回时恢复。
  const lastSubRef = useRef<{ mine: MineTab; discover: DiscoverTab }>({ mine: mineTab, discover: discoverTab });

  function updateLocation(nextView: GalleryView, nextSub: GalleryScope) {
    const params = new URLSearchParams(searchParams);
    params.set('view', nextView);
    params.set('sub', nextSub);
    setSearchParams(params, { replace: true });
  }

  function handleViewChange(nextView: GalleryView) {
    if (nextView === view) return;
    if (view === 'mine') lastSubRef.current.mine = mineTab;
    else lastSubRef.current.discover = discoverTab;
    updateLocation(nextView, nextView === 'mine' ? lastSubRef.current.mine : lastSubRef.current.discover);
  }

  function handleSubChange(nextSub: string) {
    updateLocation(view, nextSub as GalleryScope);
  }

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setAppliedSearch(searchTerm.trim());
      setPage(1);
    }, 250);
    return () => window.clearTimeout(timer);
  }, [searchTerm]);

  useEffect(() => {
    setPage(1);
  }, [activeScope, expertSource, expertDepartment, expertDirection]);

  // 每个关系视图只加载当前页；请求序号避免快速切换筛选时旧响应覆盖新结果。
  const load = useCallback(async () => {
    const requestId = ++loadRequestId.current;
    setLoading(true);
    try {
      const params = new URLSearchParams({
        tenant_id: getRequestTenantId(),
        scope: activeScope,
        page: String(page),
        page_size: String(GALLERY_PAGE_SIZE),
      });
      if (appliedSearch) params.set('q', appliedSearch);
      if (activeScope === 'expert') {
        if (expertSource) params.set('expert_source', expertSource);
        if (expertDepartment) params.set('expert_department', expertDepartment);
        if (expertDirection) params.set('expert_direction', expertDirection);
      }
      const result = await api.get<AgentGalleryPageRead>(
        `/api/enterprise/agents/gallery-page?${params.toString()}`,
      );
      if (requestId !== loadRequestId.current) return;
      setAgents(result.items);
      setTotal(result.total);
      setScopeCounts(result.scope_counts);
      setFacets(result.facets);
      const lastPage = Math.max(1, Math.ceil(result.total / result.page_size));
      if (page > lastPage) setPage(lastPage);
    } catch (error) {
      if (requestId !== loadRequestId.current) return;
      notify.error(error instanceof Error ? error.message : '加载员工失败');
    } finally {
      if (requestId === loadRequestId.current) setLoading(false);
    }
  }, [activeScope, appliedSearch, expertDepartment, expertDirection, expertSource, page, refreshToken]);

  useEffect(() => {
    void load();
    return () => {
      loadRequestId.current += 1;
    };
  }, [load]);

  const expertSourceOptions = facets.sources;
  const expertDepartmentOptions = facets.departments;
  const expertDirectionOptions = facets.directions;

  useEffect(() => {
    if (
      expertDepartment
      && !expertDepartmentOptions.some((option) => option.value === expertDepartment)
    ) {
      setExpertDepartment('');
      setExpertDirection('');
    }
  }, [expertDepartment, expertDepartmentOptions]);

  useEffect(() => {
    if (
      expertDirection
      && !expertDirectionOptions.some((option) => option.value === expertDirection)
    ) {
      setExpertDirection('');
    }
  }, [expertDirection, expertDirectionOptions]);

  useEffect(() => {
    if (activeScope !== 'expert') {
      setExpertSource('');
      setExpertDepartment('');
      setExpertDirection('');
    }
  }, [activeScope]);

  // 视图/二级 tab 切换或加载完成时，卡片网格做一次低强度入场 stagger。
  // 只取前 24 张，避免大列表（如 263 位专家）动画拖尾过长。
  useEffect(() => {
    if (loading || prefersReducedMotion()) return;
    const ctx = gsap.context(() => {
      const cards = gridRef.current?.querySelectorAll('.gongge-employee-card');
      if (!cards?.length) return;
      gsap.fromTo(
        Array.from(cards).slice(0, 24),
        { y: 14, autoAlpha: 0 },
        {
          y: 0,
          autoAlpha: 1,
          duration: 0.35,
          ease: 'power2.out',
          stagger: 0.03,
          clearProps: 'transform,opacity,visibility',
        },
      );
    }, gridRef);
    return () => ctx.revert();
  }, [activeScope, view, loading, page]);

  async function startEmployeeChat(row: AgentProfileRead) {
    if (startingAgentId) return;
    setStartingAgentId(row.id);
    try {
      let chatAgent = row;
      if (!isMyEmployeeAgent(row, currentUser) && !isEmployeeUsedByCurrentUser(row)) {
        chatAgent = await api.post<AgentProfileRead>(
          `/api/chat/agents/${encodeURIComponent(row.id)}/use?tenant_id=${getRequestTenantId()}`,
          {},
        );
        updateAgentInList(chatAgent);
      }
      if (onStartChat) {
        await onStartChat(chatAgent);
        return;
      }
      navigate(`/workspace/chat/draft/${chatAgent.id}`);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '发起对话失败');
    } finally {
      setStartingAgentId(null);
    }
  }

  async function toggleUsage(row: AgentProfileRead) {
    if (startingAgentId) return;
    setStartingAgentId(row.id);
    try {
      if (isEmployeeUsedByCurrentUser(row)) {
        await api.delete(
          `/api/chat/agents/${encodeURIComponent(row.id)}/use?tenant_id=${getRequestTenantId()}`,
        );
        updateAgentInList({
          ...row,
          used_by_current_user: false,
          metadata: {
            ...row.metadata,
            used_by_current_user: false,
            chat_used_by_current_user: false,
          },
        });
        notify.success('已从常用数字员工移除，会话历史仍会保留');
      } else {
        const saved = await api.post<AgentProfileRead>(
          `/api/chat/agents/${encodeURIComponent(row.id)}/use?tenant_id=${getRequestTenantId()}`,
          {},
        );
        updateAgentInList(saved);
        notify.success('已添加到常用数字员工');
      }
      setRefreshToken((value) => value + 1);
      window.dispatchEvent(new Event('gongge-enterprise-agent-scope-refresh'));
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '更新常用关系失败');
    } finally {
      setStartingAgentId(null);
    }
  }

  async function updateStatus(row: AgentProfileRead, status: 'active' | 'archived') {
    try {
      await api.put<AgentProfileRead>(`/api/enterprise/agents/${row.id}`, {
        tenant_id: getRequestTenantId(),
        status,
        metadata: row.metadata || {},
      });
      notify.success(status === 'active' ? '员工已上线' : '员工已下线');
      setRefreshToken((value) => value + 1);
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
      setRefreshToken((value) => value + 1);
      window.dispatchEvent(new Event('gongge-enterprise-agent-scope-refresh'));
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '更新广场状态失败');
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
        const nextAgent = agents.find((item) => item.id !== row.id && item.status === 'active')
          || agents.find((item) => item.id !== row.id);
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
      setRefreshToken((value) => value + 1);
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

  const viewTabs: UnderlineTabItem<GalleryView>[] = [
    { value: 'mine', label: '我的员工' },
    { value: 'discover', label: '发现' },
  ];
  const subTabs: Array<UnderlineTabItem<string> & { count: number }> = view === 'mine'
    ? [
        { value: 'used', label: '常用', count: scopeCounts.used },
        { value: 'owned', label: '我创建的', count: scopeCounts.owned },
      ]
    : [
        { value: 'gallery', label: '数字员工广场', count: scopeCounts.gallery },
        { value: 'expert', label: '专家', count: scopeCounts.expert },
      ];
  const subTabItems: UnderlineTabItem<string>[] = subTabs.map((tab) => ({
    value: tab.value,
    label: tabLabel(tab.label as string, tab.count),
  }));

  const hasSearchTerm = Boolean(searchTerm.trim());
  const hasExpertFilter = Boolean(expertSource || expertDepartment || expertDirection);
  const emptyText = activeScope === 'expert'
    ? hasSearchTerm || hasExpertFilter ? '没有匹配的专家' : '当前没有可用专家'
    : hasSearchTerm
      ? '没有匹配的数字员工'
      : activeScope === 'used'
        ? '还没有常用数字员工'
        : activeScope === 'owned'
          ? '还没有创建数字员工'
          : '广场暂无数字员工';
  const emptyDescription = activeScope === 'expert' && !hasSearchTerm && !hasExpertFilter
    ? '从开放广场复制专家，或由管理员导入专家库后再来看看'
    : hasSearchTerm
      ? '换个关键词，或切换员工分类再试试'
      : activeScope === 'gallery'
        ? '管理员发布到广场的员工会出现在这里'
        : '当前分类还没有可用员工';

  function clearExpertFilters() {
    setExpertSource('');
    setExpertDepartment('');
    setExpertDirection('');
    setSearchTerm('');
  }

  function employeeGrid(expertView: boolean) {
    return (
      <>
      <div ref={gridRef} className="grid auto-rows-[minmax(262px,auto)] grid-cols-1 content-start gap-[32px] sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4 max-[900px]:gap-[18px]">
        {agents.map((employee) => (
          <EmployeeCard
            key={employee.id}
            employee={employee}
            busy={startingAgentId === employee.id}
            canManage={canManageEmployeeAgent(employee, currentUser)}
            showMenu={false}
            relationLabels={[
              ...(isMyEmployeeAgent(employee, currentUser) ? ['我拥有'] : []),
              ...(isEmployeeUsedByCurrentUser(employee) ? ['已添加'] : []),
              ...(isGalleryEmployee(employee) ? ['企业发布'] : []),
            ]}
            usageActionLabel={
              !isMyEmployeeAgent(employee, currentUser) && isGalleryEmployee(employee)
                ? isEmployeeUsedByCurrentUser(employee) ? '移出常用' : '添加到常用'
                : undefined
            }
            onUsageAction={
              !isMyEmployeeAgent(employee, currentUser) && isGalleryEmployee(employee)
                ? () => void toggleUsage(employee)
                : undefined
            }
            showExpertSource={!expertView || expertSourceOptions.length > 1}
            showExpertDepartment={!expertView}
            onOpen={() => void startEmployeeChat(employee)}
            onStatus={(status) => void updateStatus(employee, status)}
            onGallery={(published) => void updateGalleryState(employee, published)}
            onDelete={() => setDeleteTarget(employee)}
            onAvatar={() => setAvatarAgent(employee)}
            onEdit={() => setProfileAgent(employee)}
            onChat={() => void startEmployeeChat(employee)}
          />
        ))}
        {!agents.length && (
          <EmployeeGalleryEmptyState
            title={emptyText}
            description={emptyDescription}
            onReset={expertView && (hasExpertFilter || hasSearchTerm) ? clearExpertFilters : undefined}
          />
        )}
      </div>
      {total > 0 && (
        <Paginator
          page={page}
          pageCount={Math.max(1, Math.ceil(total / GALLERY_PAGE_SIZE))}
          onChange={setPage}
          aria-label="数字员工分页"
          className="mt-[28px]"
        />
      )}
      </>
    );
  }

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
              aria-label="搜索数字员工"
              className="min-w-0 flex-1 border-0 bg-transparent text-[14px] text-[#18181A] outline-none placeholder:text-[#757F9C]"
            />
          </div>
        )}
      />

      <UnderlineTabs
        variant="line"
        className="mt-[28px]"
        aria-label="员工视图分组"
        value={view}
        onChange={handleViewChange}
        items={viewTabs}
        tabClassName="w-auto min-w-[96px] text-[15px]"
      />

      <UnderlineTabs
        className="mt-[12px] mb-[20px] max-[560px]:w-full"
        aria-label={view === 'mine' ? '我的员工分类' : '发现分类'}
        value={activeScope}
        onChange={handleSubChange}
        items={subTabItems}
        tabClassName="w-auto px-[14px] max-[560px]:min-h-[54px] max-[560px]:flex-1 max-[560px]:px-[6px] max-[560px]:text-[12px] max-[560px]:leading-[16px]"
      />

      {activeScope === 'expert' ? (
        scopeCounts.expert > 0 ? (
          <div className="grid min-w-0 gap-[16px] lg:grid-cols-[196px_minmax(0,1fr)]">
            <ExpertCategoryRail
              options={expertDepartmentOptions}
              value={expertDepartment}
              totalCount={scopeCounts.expert}
              onChange={(value) => {
                setExpertDepartment(value);
                setExpertDirection('');
              }}
            />
            <div className="min-w-0">
              <ExpertFilterBar
                sourceOptions={expertSourceOptions}
                departmentOptions={expertDepartmentOptions}
                directionOptions={expertDirectionOptions}
                source={expertSource}
                department={expertDepartment}
                direction={expertDirection}
                resultCount={total}
                hasFilters={hasExpertFilter || hasSearchTerm}
                onSourceChange={(value) => {
                  setExpertSource(value);
                  setExpertDepartment('');
                  setExpertDirection('');
                }}
                onDepartmentChange={(value) => {
                  setExpertDepartment(value);
                  setExpertDirection('');
                }}
                onDirectionChange={setExpertDirection}
                onReset={clearExpertFilters}
              />
              {employeeGrid(true)}
            </div>
          </div>
        ) : employeeGrid(true)
      ) : employeeGrid(false)}

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
    </div>
  );
}

function EmployeeGalleryEmptyState({
  title,
  description,
  onReset,
}: {
  title: string;
  description: string;
  onReset?: () => void;
}) {
  return (
    <div className="flex h-[262px] w-full items-center justify-center rounded-[20px] border border-dashed border-[#e4e9f2] bg-[#fbfcfe] px-[24px] text-center">
      <div className="flex max-w-[210px] flex-col items-center">
        <span className="grid size-[34px] place-items-center rounded-[12px] bg-white text-[#98a2b3] shadow-[0_1px_8px_rgba(70,76,94,0.06)] ring-1 ring-[#edf1f6]">
          <IconSearch className="size-[16px] shrink-0" />
        </span>
        <p className="mt-[12px] text-[14px] font-medium leading-[20px] text-[#7f879a]">
          {title}
        </p>
        <p className="mt-[4px] text-[11px] leading-[17px] text-[#a7adbb]">
          {description}
        </p>
        {onReset && (
          <button type="button" onClick={onReset} className="mt-[10px] text-[11px] font-medium text-[var(--gg-cobalt)] hover:underline">
            清除筛选
          </button>
        )}
      </div>
    </div>
  );
}

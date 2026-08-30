import { Button as UIButton, notify, UnderlineTabs, type UnderlineTabItem } from '@/components/ui';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { api, getRequestTenantId } from '../api/client';
import { isGalleryEmployee, type EnterpriseAuthUser } from '../auth';
import type { PlazaResourceKind } from '../assets/plaza/plaza-resource-icons';
import {
  agentResourceCount,
  canManageEmployeeAgent,
  employeeDisplayNameWithCreator,
  employeeProfile,
  resourceDisplayNameWithCreator,
  isExpertTemplate,
} from '../employee';
import type { AgentProfileRead, GeneralSkillRead, KnowledgeBaseRead, SkillRead, ToolRead } from '../types';

import AppHeader from '@/components/AppHeader';
import { ConceptHelp, ConceptNote } from '@/components/ConceptHelp';
import PlazaResourceArtwork from '@/components/openPlatform/PlazaResourceArtwork';
import {
  PlatformEmployeeDrawer,
  PlatformKindDetailView,
  PlatformResourceDrawer,
  type PlatformResourceAccent,
  type PlatformStat,
} from '@/components/openPlatform';

const ENTERPRISE_AGENT_STORAGE_KEY = 'gongge_enterprise_agent_scope';

type PlatformKind = 'agents' | 'experts' | PlazaResourceKind;

type PlatformConfig = {
  kind: PlatformKind;
  title: string;
  subtitle: string;
  detail: string;
  useLabel: string;
  metricLabel: string;
  signals: string[];
};

type PlatformItem = {
  id: string;
  deleteKey?: string;
  detailHref?: string;
  title: string;
  description: string;
  meta: string;
  tags: string[];
  agent?: AgentProfileRead;
};

const PLATFORM_CONFIGS: PlatformConfig[] = [
  {
    kind: 'agents',
    title: '数字员工',
    subtitle: '已发布到开放平台，可在对话端直接使用。',
    detail: '选择一个数字员工查看能力、岗位和服务范围。',
    useLabel: '使用此员工',
    metricLabel: '数字员工',
    signals: ['聊天可用', '支持对话', '查看能力'],
  },
  {
    kind: 'experts',
    title: '专家',
    subtitle: '已审核并发布的专业型 Agent，可直接使用或复制为能力分身。',
    detail: '专家模板与能力分身共用 AgentProfile；直接使用只建立使用关系，复制后才归当前用户所有。',
    useLabel: '使用此专家',
    metricLabel: '专家',
    signals: ['专业分类', '来源可追溯', '可复制定制'],
  },
  {
    kind: 'knowledge',
    title: '知识库',
    subtitle: '发布到开放平台的知识库，可复制到你的数字员工。',
    detail: '从广场复制到当前数字员工的知识库。',
    useLabel: '复制到知识库',
    metricLabel: '知识库',
    signals: ['知识图谱', '引用来源', '可复制'],
  },
  {
    kind: 'general-skills',
    title: 'Skill',
    subtitle: '已审核发布的可复用工作能力。',
    detail: '从广场复制到当前数字员工的技能。',
    useLabel: '复制到技能',
    metricLabel: '技能',
    signals: ['运行测试', 'MCP/浏览器', '能力复用'],
  },
  {
    kind: 'skills',
    title: 'SOP',
    subtitle: '可复制和复用的业务流程与执行规范。',
    detail: '从广场复制到当前数字员工的 SOP。',
    useLabel: '复制到 SOP',
    metricLabel: '业务 SOP',
    signals: ['流程推进', '执行规范', '可复制'],
  },
  {
    kind: 'tools',
    title: '工具',
    subtitle: '可开放给员工调用和测试的工具能力。',
    detail: '前往工具页按现有流程配置和测试工具。',
    useLabel: '前往工具页',
    metricLabel: '工具能力',
    signals: ['调用权限', '测试可用', '工具配置'],
  },
];

const PLATFORM_BY_KIND = new Map(PLATFORM_CONFIGS.map((item) => [item.kind, item]));

// Per-module accent color for the resource card meta line and tag pills (共格 232:4634).
const PLATFORM_ACCENT: Partial<Record<PlatformKind, PlatformResourceAccent>> = {
  knowledge: 'green',
  'general-skills': 'indigo',
  skills: 'blue',
  tools: 'orange',
};

// Unit rendered after the header count, e.g. "12 员工" / "12 内容".
function platformCountLabel(kind: PlatformKind): string {
  if (kind === 'experts') return '专家';
  if (kind === 'agents') return '员工';
  if (kind === 'general-skills') return 'Skill';
  if (kind === 'skills') return 'SOP';
  return kind === 'knowledge' ? '知识库' : '工具';
}

// Bottom metric segments for a 数字员工广场 card.
function employeeStats(agent: AgentProfileRead): PlatformStat[] {
  return [
    { value: agentResourceCount(agent, 'knowledge_base'), label: '资料' },
    { value: agentResourceCount(agent, 'general_skill'), label: '技能' },
    { value: agentResourceCount(agent, 'skill'), label: 'SOP' },
  ];
}

function resourceDrawerBadge(kind: PlatformKind, item: PlatformItem): string {
  if (kind === 'skills') {
    const parts = item.meta.split(' / ');
    return parts[parts.length - 1] || item.tags[0] || '';
  }
  return item.tags[0] || '';
}

export default function OpenPlatformPage({
  currentUser,
  isAdmin = false,
  onCopyAgent,
  onLogout,
}: {
  currentUser?: EnterpriseAuthUser;
  isAdmin?: boolean;
  onCopyAgent?: (agentId: string) => void;
  onLogout?: () => void;
}) {
  const navigate = useNavigate();
  const { kind } = useParams<{ kind?: PlatformKind }>();
  const selectedKind: PlatformKind = kind && PLATFORM_BY_KIND.has(kind) ? kind : 'agents';
  const [agents, setAgents] = useState<AgentProfileRead[]>([]);
  const [galleryAgents, setGalleryAgents] = useState<AgentProfileRead[]>([]);
  const [expertAgents, setExpertAgents] = useState<AgentProfileRead[]>([]);
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBaseRead[]>([]);
  const [generalSkills, setGeneralSkills] = useState<GeneralSkillRead[]>([]);
  const [skills, setSkills] = useState<SkillRead[]>([]);
  const [tools, setTools] = useState<ToolRead[]>([]);
  const [loading, setLoading] = useState(false);
  const [deletingItemKey, setDeletingItemKey] = useState('');
  const [agentId, setAgentId] = useState(() => window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) || '');
  const [detailItem, setDetailItem] = useState<{ kind: PlatformKind; item: PlatformItem } | null>(null);
  const [confirmTarget, setConfirmTarget] = useState<{ kind: PlatformKind; item: PlatformItem } | null>(null);

  useEffect(() => {
    const onScopeChange = (event: Event) => {
      const nextAgentId = (event as CustomEvent<{ agentId?: string }>).detail?.agentId
        || window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY)
        || '';
      setAgentId(nextAgentId);
    };
    window.addEventListener('gongge-enterprise-agent-scope-change', onScopeChange);
    return () => window.removeEventListener('gongge-enterprise-agent-scope-change', onScopeChange);
  }, []);

  const loadPlatformData = useCallback(async () => {
    setLoading(true);
    try {
      const tenantId = getRequestTenantId();
      const [allAgentsResult, galleryAgentsResult, expertAgentsResult] = await Promise.allSettled([
        api.get<AgentProfileRead[]>(`/api/enterprise/agents?tenant_id=${tenantId}`),
        api.get<AgentProfileRead[]>(`/api/enterprise/agents?tenant_id=${tenantId}&scope=gallery`),
        api.get<AgentProfileRead[]>(`/api/enterprise/agents?tenant_id=${tenantId}&scope=expert`),
      ]);
      if (allAgentsResult.status === 'rejected') throw allAgentsResult.reason;
      const agentRows = allAgentsResult.value;
      const overall = agentRows.find((item) => item.is_overall);
      const overallSuffix = overall ? `&agent_id=${encodeURIComponent(overall.id)}` : '';
      const [kbResult, generalResult, skillResult, toolResult] = await Promise.allSettled([
        api.get<KnowledgeBaseRead[]>(`/api/enterprise/knowledge-bases?tenant_id=${tenantId}${overallSuffix}`),
        api.get<GeneralSkillRead[]>(`/api/enterprise/general-skills?tenant_id=${tenantId}${overallSuffix}`),
        overall
          ? api.get<SkillRead[]>(`/api/enterprise/agents/${overall.id}/skills?tenant_id=${tenantId}`)
          : Promise.resolve([]),
        api.get<ToolRead[]>(`/api/enterprise/tools?tenant_id=${tenantId}${overallSuffix}`),
      ]);
      setAgents(agentRows);
      const failures: string[] = [];
      if (galleryAgentsResult.status === 'fulfilled') setGalleryAgents(galleryAgentsResult.value);
      else {
        setGalleryAgents([]);
        failures.push('数字员工广场');
      }
      if (expertAgentsResult.status === 'fulfilled') setExpertAgents(expertAgentsResult.value);
      else {
        setExpertAgents([]);
        failures.push('专家分类');
      }
      if (kbResult.status === 'fulfilled') setKnowledgeBases(kbResult.value);
      else failures.push('知识库');
      if (generalResult.status === 'fulfilled') setGeneralSkills(generalResult.value);
      else failures.push('通用技能');
      if (skillResult.status === 'fulfilled') setSkills(skillResult.value);
      else failures.push('SOP');
      if (toolResult.status === 'fulfilled') setTools(toolResult.value);
      else failures.push('工具');
      if (failures.length) {
        notify.error(`开放广场部分资源加载失败：${failures.join('、')}。`);
      }
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载开放广场失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadPlatformData();
  }, [loadPlatformData]);

  const visibleAgents = useMemo(
    () => galleryAgents.filter((item) => (
      !item.is_overall
      && item.status === 'active'
      && item.agent_category_code !== 'professional'
      && isGalleryEmployee(item)
    )),
    [galleryAgents],
  );
  const visibleExpertAgents = useMemo(
    () => expertAgents.filter((item) => (
      !item.is_overall
      && item.status === 'active'
      && item.agent_category_code === 'professional'
      && isGalleryEmployee(item)
    )),
    [expertAgents],
  );
  const overallAgent = agents.find((item) => item.is_overall) || null;
  // 开放平台是发现入口；发布、下架和删除分别在资源管理页完成。
  const canManagePlatform = false;
  const currentAgent = agents.find((item) => item.id === agentId);
  const targetEmployee = currentAgent && canManageEmployeeAgent(currentAgent, currentUser)
    ? currentAgent
    : agents.find((item) => canManageEmployeeAgent(item, currentUser) && !item.is_overall);

  const platformItems = useMemo<Record<PlatformKind, PlatformItem[]>>(() => ({
    agents: visibleAgents.map((item) => {
      const profile = employeeProfile(item);
      return {
        id: item.id,
        deleteKey: item.id,
        title: employeeDisplayNameWithCreator(item),
        description: item.description || '广场开放的数字员工。',
        meta: profile.roleName,
        tags: [
          item.status === 'active' ? '在线' : '下线',
          `SOP ${agentResourceCount(item, 'skill')}`,
          `技能 ${agentResourceCount(item, 'general_skill')}`,
        ],
        agent: item,
      };
    }),
    experts: visibleExpertAgents.map((item) => {
      const profile = employeeProfile(item);
      const template = isExpertTemplate(item);
      return {
        id: item.id,
        deleteKey: item.id,
        title: employeeDisplayNameWithCreator(item),
        description: item.description || (template
          ? '已发布的专家模板，可直接使用或复制为你的能力分身。'
          : '已发布的专业型数字员工，可直接使用。'),
        meta: profile.roleName,
        tags: [
          template ? '专家模板' : '专业型数字员工',
          `来源 ${String(item.metadata?.expert_source_label || item.metadata?.expert_source_code || '项目内置')}`,
          `技能 ${agentResourceCount(item, 'general_skill')}`,
        ],
        agent: item,
      };
    }),
    knowledge: knowledgeBases
      .filter((item) => item.status === 'active' && !isEmptyDefaultKnowledgeBase(item))
      .map((item) => ({
        id: item.id,
        deleteKey: item.id,
        title: resourceDisplayNameWithCreator(item.name, item),
        description: item.description || '广场沉淀的知识库。',
        meta: `${item.document_count} 文档 / ${item.bucket_count} 目录 / ${item.chunk_count} 引用`,
        tags: [item.version || 'v1.0.0', item.branch_sync_state || '广场版'],
      })),
    'general-skills': generalSkills
      .filter((item) => item.status === 'published')
      .map((item) => ({
        id: item.id,
        deleteKey: item.slug,
        detailHref: `/enterprise/general-skills/catalog/${encodeURIComponent(item.slug)}`,
        title: resourceDisplayNameWithCreator(item.name_zh || item.name, item),
        description: item.description_zh || item.description || '可复制到当前数字员工的技能。',
        meta: item.slug,
        tags: [item.homepage ? '外部能力' : '内置能力', '已启用'],
      })),
    skills: skills
      .filter((item) => item.status === 'published')
      .map((item) => ({
        id: item.id,
        deleteKey: item.skill_id,
        title: resourceDisplayNameWithCreator(item.name, item),
        description: item.description || '可复制和复用的业务 SOP。',
        meta: `${item.skill_id} / ${item.version}`,
        tags: [item.business_domain || '业务流程', `${item.total_call_count || item.call_count || 0} 次调用`],
      })),
    tools: tools
      .filter((item) => item.enabled)
      .map((item) => ({
        id: item.id,
        deleteKey: item.id,
        title: resourceDisplayNameWithCreator(item.display_name || item.name, item),
        description: item.description || '可配置到员工工具的工具。',
        meta: `${item.bucket || '工具'} / ${item.tool_type.toUpperCase()}`,
        tags: [item.method, item.enabled ? '已启用' : '已停用'],
      })),
  }), [generalSkills, knowledgeBases, skills, tools, visibleAgents, visibleExpertAgents]);

  const platformStats = PLATFORM_CONFIGS.map((config) => ({
    ...config,
    count: platformItems[config.kind].length,
  }));

  function ensureTargetEmployee(): boolean {
    if (!targetEmployee) {
      notify.warning('请先选择一个员工，再从广场复制资源。');
      return false;
    }
    if (targetEmployee.id !== agentId) {
      window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, targetEmployee.id);
      window.dispatchEvent(new CustomEvent('gongge-enterprise-agent-scope-change', { detail: { agentId: targetEmployee.id } }));
      setAgentId(targetEmployee.id);
    }
    return true;
  }

  async function markPlatformAgentUsed(agent: AgentProfileRead) {
    const metadata = agent.metadata || {};
    if (metadata.used_by_current_user !== true && metadata.chat_used_by_current_user !== true) {
      await api.post<AgentProfileRead>(`/api/chat/agents/${agent.id}/use?tenant_id=${getRequestTenantId()}`, {});
    }
    const markUsedInState = (current: AgentProfileRead[]) => current.map((item) => (
      item.id === agent.id
        ? {
          ...item,
          metadata: {
            ...(item.metadata || {}),
            used_by_current_user: true,
            chat_used_by_current_user: true,
          },
        }
        : item
    ));
    setAgents(markUsedInState);
    setGalleryAgents(markUsedInState);
    setExpertAgents(markUsedInState);
    window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, agent.id);
    window.dispatchEvent(new Event('gongge-enterprise-agent-scope-refresh'));
    window.dispatchEvent(new CustomEvent('gongge-enterprise-agent-scope-change', { detail: { agentId: agent.id } }));
    setAgentId(agent.id);
  }

  async function usePlatformItem(platformKind: PlatformKind, itemId?: string) {
    if (platformKind === 'agents' || platformKind === 'experts') {
      const sourceAgents = platformKind === 'experts' ? visibleExpertAgents : visibleAgents;
      const agent = sourceAgents.find((item) => item.id === itemId) || sourceAgents[0];
      if (!agent) {
        notify.warning(platformKind === 'experts' ? '开放平台暂无可用专家' : '开放平台暂无可用数字员工');
        return;
      }
      try {
        await markPlatformAgentUsed(agent);
        navigate(`/workspace/chat/draft/${agent.id}`);
      } catch (error) {
        notify.error(error instanceof Error ? error.message : '使用数字员工失败');
      }
      return;
    }
    if (!ensureTargetEmployee()) return;
    const resourceParam = itemId ? `&resourceId=${encodeURIComponent(itemId)}` : '';
    if (platformKind === 'knowledge') navigate(`/enterprise/knowledge?add=plaza${resourceParam}`);
    if (platformKind === 'general-skills') navigate(`/enterprise/general-skills?add=plaza${resourceParam}`);
    if (platformKind === 'skills') navigate(`/enterprise/skills?add=plaza${resourceParam}`);
    if (platformKind === 'tools') navigate('/enterprise/tools?add=plaza');
  }

  function platformItemDeleteKey(platformKind: PlatformKind, item: PlatformItem): string {
    return `${platformKind}:${item.deleteKey || item.id}`;
  }

  function platformDeleteUrl(platformKind: PlatformKind, item: PlatformItem): string {
    const resourceKey = encodeURIComponent(item.deleteKey || item.id);
    const overallSuffix = overallAgent ? `&agent_id=${encodeURIComponent(overallAgent.id)}` : '';
    if (platformKind === 'agents' || platformKind === 'experts') return `/api/enterprise/agents/${resourceKey}?tenant_id=${getRequestTenantId()}`;
    if (platformKind === 'knowledge') return `/api/enterprise/knowledge-bases/${resourceKey}?tenant_id=${getRequestTenantId()}${overallSuffix}`;
    if (platformKind === 'general-skills') return `/api/enterprise/general-skills/${resourceKey}?tenant_id=${getRequestTenantId()}${overallSuffix}`;
    if (platformKind === 'skills') return `/api/enterprise/skills/${resourceKey}?tenant_id=${getRequestTenantId()}${overallSuffix}`;
    return `/api/enterprise/tools/${resourceKey}?tenant_id=${getRequestTenantId()}${overallSuffix}`;
  }

  async function runDelete() {
    if (!confirmTarget) return;
    const { kind: platformKind, item } = confirmTarget;
    const key = platformItemDeleteKey(platformKind, item);
    setDeletingItemKey(key);
    try {
      if ((platformKind === 'agents' || platformKind === 'experts') && item.agent) {
        await api.put<AgentProfileRead>(
          `/api/enterprise/agents/${item.agent.id}/gallery-publication`,
          {
          tenant_id: getRequestTenantId(),
            published: false,
          },
        );
        window.dispatchEvent(new Event('gongge-enterprise-agent-scope-refresh'));
      } else {
        await api.delete(platformDeleteUrl(platformKind, item));
      }
      notify.success('已从广场移除');
      setDetailItem((current) => (
        current && current.kind === platformKind && current.item.id === item.id ? null : current
      ));
      setConfirmTarget(null);
      await loadPlatformData();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '删除失败');
    } finally {
      setDeletingItemKey('');
    }
  }

  function navigateDetailItem(offset: -1 | 1) {
    if (!detailItem) return;
    const items = platformItems[detailItem.kind];
    const currentIndex = items.findIndex((entry) => entry.id === detailItem.item.id);
    const nextItem = items[currentIndex + offset];
    if (!nextItem) return;
    setDetailItem({ kind: detailItem.kind, item: nextItem });
  }

  function renderItemDrawer() {
    if (!detailItem) return null;
    const config = PLATFORM_BY_KIND.get(detailItem.kind) || PLATFORM_CONFIGS[0];
    const { item } = detailItem;
    const deleteKey = platformItemDeleteKey(detailItem.kind, item);
    const drawerItems = platformItems[detailItem.kind];
    const drawerIndex = drawerItems.findIndex((entry) => entry.id === item.id);

    if (detailItem.kind === 'agents' || detailItem.kind === 'experts') {
      if (!item.agent) return null;
      const profile = employeeProfile(item.agent);
      const detailText = item.agent.persona_prompt
        || item.agent.description
        || config.detail;
      return (
        <PlatformEmployeeDrawer
          open
          agent={item.agent}
          platformTitle={config.title}
          name={item.title}
          role={item.meta}
          description={item.description}
          detailText={detailText}
          workStyles={profile.workStyles}
          stats={employeeStats(item.agent)}
          online={item.agent.status === 'active'}
          statusLabel={detailItem.kind === 'experts' ? '可使用' : undefined}
          statusKind={detailItem.kind === 'experts' ? 'available' : 'online'}
          canManage={canManagePlatform}
          deleting={deletingItemKey === deleteKey}
          hasPrev={drawerIndex > 0}
          hasNext={drawerIndex >= 0 && drawerIndex < drawerItems.length - 1}
          onClose={() => setDetailItem(null)}
          onPrev={() => navigateDetailItem(-1)}
          onNext={() => navigateDetailItem(1)}
          onDelete={() => setConfirmTarget({ kind: detailItem.kind, item })}
          onUse={() => {
            setDetailItem(null);
            void usePlatformItem(detailItem.kind, item.id);
          }}
          onCopy={() => {
            setDetailItem(null);
            onCopyAgent?.(item.agent?.id || item.id);
          }}
        />
      );
    }

    return (
      <PlatformResourceDrawer
        open
        platformTitle={config.title}
        icon={<PlazaResourceArtwork kind={detailItem.kind} size="drawer" />}
        accent={PLATFORM_ACCENT[detailItem.kind]}
        title={item.title}
        description={item.description}
        badge={resourceDrawerBadge(detailItem.kind, item)}
        categoryMeta={item.meta}
        detailText={config.detail}
        useLabel={config.useLabel}
        detailsHref={item.detailHref}
        canManage={canManagePlatform}
        deleting={deletingItemKey === deleteKey}
        hasPrev={drawerIndex > 0}
        hasNext={drawerIndex >= 0 && drawerIndex < drawerItems.length - 1}
        onClose={() => setDetailItem(null)}
        onPrev={() => navigateDetailItem(-1)}
        onNext={() => navigateDetailItem(1)}
        onDelete={() => setConfirmTarget({ kind: detailItem.kind, item })}
        onUse={() => {
          setDetailItem(null);
          void usePlatformItem(detailItem.kind, item.id);
        }}
      />
    );
  }

  function renderConfirm() {
    const config = confirmTarget ? PLATFORM_BY_KIND.get(confirmTarget.kind) || PLATFORM_CONFIGS[0] : null;
    return (
      <ConfirmDialog
        open={Boolean(confirmTarget)}
        onOpenChange={(next) => { if (!next) setConfirmTarget(null); }}
        title={confirmTarget && config
          ? confirmTarget.kind === 'agents' || confirmTarget.kind === 'experts'
            ? `从广场下架「${confirmTarget.item.title}」？`
            : `删除${config.metricLabel}「${confirmTarget.item.title}」？`
          : ''}
        description={confirmTarget?.kind === 'agents' || confirmTarget?.kind === 'experts'
          ? '下架后将停止向新用户开放；模板本身、已有使用关系和资源绑定不会被删除。'
          : '删除后该广场内容会从开放平台移除，已复制到员工侧的引用可能不再可同步。'}
        loading={Boolean(confirmTarget) && deletingItemKey === (confirmTarget ? platformItemDeleteKey(confirmTarget.kind, confirmTarget.item) : '')}
        onConfirm={() => void runDelete()}
      />
    );
  }

  // 异构资源只展示当前分类；裸路径默认为数字员工。
  const kindTabs: UnderlineTabItem<string>[] = [
    ...platformStats.map((platform) => ({
      value: platform.kind as string,
      label: (
        <span className="inline-flex items-center gap-[6px]">
          {platform.title}
          {platform.count > 0 && (
            <span className="rounded-full bg-[#eff1f7] px-[6px] py-[1px] text-[11px] leading-[16px] text-[#757f9c]">
              {platform.count}
            </span>
          )}
        </span>
      ),
    })),
  ];

  function handleKindTabChange(value: string) {
    navigate(`/enterprise/platform/${value}`);
  }
  const selectedConfig = PLATFORM_BY_KIND.get(selectedKind) || PLATFORM_CONFIGS[0];

  return (
    <div className="min-h-full box-border px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]">
      <AppHeader
        onLogout={onLogout}
        userName={currentUser?.username}
        title={(
          <span className="inline-flex items-center gap-[5px]">
            开放广场平台
            <ConceptHelp topic="plaza" />
          </span>
        )}
        description={selectedKind === 'experts'
          ? '发现已审核并发布的专业型 Agent；模板可直接使用或复制。'
          : selectedKind === 'agents'
            ? '发现已发布的数字员工；直接使用不等于拥有。'
            : '发现可复用资源；复制或安装后才进入目标员工的能力范围。'}
      />
      <div className="mt-[14px] flex flex-wrap items-center gap-[10px]">
        <ConceptNote topic="plaza" className="max-w-[760px]">
          {selectedKind === 'experts'
            ? '直接使用只建立使用关系；复制并定制后，才形成由你拥有的专家能力分身。'
            : selectedKind === 'agents'
              ? '直接使用只建立使用关系；复制并定制后，才形成由你拥有的数字员工。'
              : '广场资源只表示可发现；复制或安装后，才会绑定到目标数字员工。'}
        </ConceptNote>
        {selectedKind === 'experts' && isAdmin && (
          <UIButton asChild variant="outline" className="h-[32px] rounded-[10px] px-[12px] text-[12px]">
            <Link to="/enterprise/agents?view=expert">管理专家模板</Link>
          </UIButton>
        )}
      </div>

      <UnderlineTabs
        variant="line"
        className="mt-[20px] overflow-x-auto"
        aria-label="广场资源类型"
        value={selectedKind}
        onChange={handleKindTabChange}
        items={kindTabs}
        tabClassName="w-auto shrink-0 px-[16px]"
      />

      <PlatformKindDetailView
        key={selectedKind}
        kind={selectedKind}
        title={selectedConfig.title}
        subtitle={selectedConfig.subtitle}
        countLabel={platformCountLabel(selectedKind)}
        signals={selectedConfig.signals}
        items={platformItems[selectedKind]}
        loading={loading}
        onRefresh={() => void loadPlatformData()}
        onOpenItem={(item) => setDetailItem({ kind: selectedKind, item })}
        onUseItem={(item) => void usePlatformItem(selectedKind, item.id)}
      />
      {renderItemDrawer()}
      {renderConfirm()}
    </div>
  );
}

function isEmptyDefaultKnowledgeBase(item: KnowledgeBaseRead): boolean {
  return item.name === '默认知识库' && item.document_count === 0 && item.bucket_count === 0 && item.chunk_count === 0;
}

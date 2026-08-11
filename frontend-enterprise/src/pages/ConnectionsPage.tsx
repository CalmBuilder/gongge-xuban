/**
 * @Time       : 2026/08/10 20:45
 * @Author     : zhanglp8181
 * @File       : ConnectionsPage.tsx
 * @CallChain  : 企业连接管理 → connection API client → ConnectionProfile/Agent binding
 * @Description: 管理企业微信/Slack 连接，并提供企业微信从建档、验收到数字员工任务演示的完整引导。
 */

import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import {
  Activity,
  ArrowRight,
  Bot,
  BookOpen,
  Cable,
  CheckCircle2,
  CircleX,
  Copy,
  KeyRound,
  Link2,
  MessagesSquare,
  Plus,
  RefreshCw,
  ShieldCheck,
  Unplug,
} from 'lucide-react';

import {
  checkConnectionHealth,
  bindConnectorPrincipal,
  createConnectionBinding,
  createSlackConnection,
  createWeComConnection,
  disableConnection,
  getConnectorInboundRoute,
  listConnectorInboundEvents,
  listConnectionBindings,
  listConnectionProfiles,
  reauthorizeConnection,
  reauthorizeWeComConnection,
  setConnectionBindingState,
  setConnectionBindingActions,
  setConnectorInboundRoute,
  startSlackOAuth,
} from '@/api/connections';
import { api, ApiError, getRequestTenantId } from '@/api/client';
import weComAgentBindingScreenshot from '@/assets/guides/wecom-agent-binding.png';
import weComConnectionCardScreenshot from '@/assets/guides/wecom-connection-card.png';
import weComMessageRoutingScreenshot from '@/assets/guides/wecom-message-routing.png';
import type { EnterpriseAuthUser } from '@/auth';
import AppHeader from '@/components/AppHeader';
import { notify } from '@/components/ui/app-toast';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogTitle,
  Input,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Switch,
} from '@/components/ui';
import { cn } from '@/lib/utils';
import { formatDateTime, SELECT_TRIGGER_CLASS } from '@/lib/enterprise-ui';
import type { AgentProfileRead } from '@/types';
import type {
  ConnectionBindingRead,
  ConnectionProfileRead,
  ConnectionProvider,
  ConnectorInboundEventRead,
  ConnectorInboundRouteRead,
} from '@/types/connections';

type InboundUser = {
  id: string;
  username: string;
  display_name?: string | null;
  membership_status: string;
};

type SecretDialogMode = 'create' | 'reauthorize';

const HEALTH_COPY: Record<ConnectionProfileRead['health_status'], string> = {
  healthy: '连接健康',
  degraded: '连接受限',
  unhealthy: '需要处理',
  unverified: '尚未验证',
};

export default function ConnectionsPage({
  currentUser,
  onLogout,
}: {
  currentUser?: EnterpriseAuthUser;
  onLogout?: () => void;
}) {
  const [profiles, setProfiles] = useState<ConnectionProfileRead[]>([]);
  const [agents, setAgents] = useState<AgentProfileRead[]>([]);
  const [bindings, setBindings] = useState<ConnectionBindingRead[]>([]);
  const [loading, setLoading] = useState(true);
  const [actingId, setActingId] = useState<string | null>(null);
  const [selectedProfile, setSelectedProfile] = useState<ConnectionProfileRead | null>(null);
  const [secretMode, setSecretMode] = useState<SecretDialogMode | null>(null);
  const [provider, setProvider] = useState<ConnectionProvider>('wecom');
  const [displayName, setDisplayName] = useState('');
  const [token, setToken] = useState('');
  const [corpId, setCorpId] = useState('');
  const [agentId, setAgentId] = useState('');
  const [corpSecret, setCorpSecret] = useState('');
  const [callbackToken, setCallbackToken] = useState('');
  const [callbackEncodingAesKey, setCallbackEncodingAesKey] = useState('');
  const [weComSetup, setWeComSetup] = useState<{
    profileId: string;
    callbackToken: string;
    callbackEncodingAesKey: string;
  } | null>(null);
  const [selectedAgentId, setSelectedAgentId] = useState('');
  const [savingSecret, setSavingSecret] = useState(false);
  const [bindingDialogOpen, setBindingDialogOpen] = useState(false);
  const [inboundDialogOpen, setInboundDialogOpen] = useState(false);
  const [inboundRoute, setInboundRoute] = useState<ConnectorInboundRouteRead | null>(null);
  const [inboundEvents, setInboundEvents] = useState<ConnectorInboundEventRead[]>([]);
  const [inboundUsers, setInboundUsers] = useState<InboundUser[]>([]);
  const [selectedInboundAgentId, setSelectedInboundAgentId] = useState('');
  const [eventUserSelections, setEventUserSelections] = useState<Record<string, string>>({});
  const [pendingDisable, setPendingDisable] = useState<ConnectionProfileRead | null>(null);
  const [guideOpen, setGuideOpen] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [profileRows, agentRows] = await Promise.all([
        listConnectionProfiles(),
        api.get<AgentProfileRead[]>(
          `/api/enterprise/agents?tenant_id=${getRequestTenantId()}`,
        ),
      ]);
      setProfiles(profileRows);
      setAgents(agentRows.filter((agent) => agent.status === 'active'));
      setSelectedProfile((current) => (
        current ? profileRows.find((item) => item.id === current.id) || null : null
      ));
    } catch (error) {
      notify.error(connectionErrorMessage(error, '加载连接档案失败'));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const activeCount = profiles.filter((item) => item.status === 'active').length;
  const attentionCount = profiles.filter((item) => (
    item.status === 'reauth_required' || item.health_status === 'degraded'
  )).length;
  const agentNames = useMemo(
    () => new Map(agents.map((agent) => [agent.id, agent.name])),
    [agents],
  );

  function openCreate() {
    setSelectedProfile(null);
    setProvider('wecom');
    setDisplayName('');
    setToken('');
    setCorpId('');
    setAgentId('');
    setCorpSecret('');
    const callback = generateWeComCallbackCredentials();
    setCallbackToken(callback.token);
    setCallbackEncodingAesKey(callback.encodingAesKey);
    setSecretMode('create');
  }

  function openGuideAt(sectionId: string) {
    /** 打开指南并在 Portal 挂载后定位到用户点击的流程阶段。 */

    setGuideOpen(true);
    window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => {
        document.getElementById(sectionId)?.scrollIntoView?.({ block: 'start' });
      });
    });
  }

  function openReauthorize(profile: ConnectionProfileRead) {
    setSelectedProfile(profile);
    setProvider(profile.provider);
    setDisplayName(profile.display_name);
    setToken('');
    setCorpId('');
    setAgentId('');
    setCorpSecret('');
    setCallbackToken('');
    setCallbackEncodingAesKey('');
    setSecretMode('reauthorize');
  }

  function closeSecretDialog() {
    if (savingSecret) return;
    setSecretMode(null);
    setToken('');
    setCorpId('');
    setAgentId('');
    setCorpSecret('');
    setCallbackToken('');
    setCallbackEncodingAesKey('');
    setDisplayName('');
  }

  async function saveSecret() {
    const normalizedToken = token.trim();
    const normalizedCorpId = corpId.trim();
    const normalizedAgentId = agentId.trim();
    const normalizedCorpSecret = corpSecret.trim();
    const normalizedCallbackToken = callbackToken.trim();
    const normalizedCallbackAesKey = callbackEncodingAesKey.trim();
    if (provider === 'slack' && !normalizedToken) {
      notify.error('请填写 Slack Bot Token');
      return;
    }
    if (provider === 'wecom' && (!normalizedCorpId || !normalizedAgentId || !normalizedCorpSecret)) {
      notify.error('请完整填写企业 ID、AgentId 和 Secret');
      return;
    }
    if (
      provider === 'wecom'
      && secretMode === 'create'
      && (!normalizedCallbackToken || normalizedCallbackAesKey.length !== 43)
    ) {
      notify.error('请生成完整的企业微信回调 Token 和 EncodingAESKey');
      return;
    }
    if (secretMode === 'create' && !displayName.trim()) {
      notify.error('请填写连接显示名称');
      return;
    }
    setSavingSecret(true);
    try {
      if (secretMode === 'create') {
        if (provider === 'wecom') {
          const created = await createWeComConnection({
            displayName: displayName.trim(),
            corpId: normalizedCorpId,
            agentId: normalizedAgentId,
            corpSecret: normalizedCorpSecret,
            callbackToken: normalizedCallbackToken,
            callbackEncodingAesKey: normalizedCallbackAesKey,
          });
          setWeComSetup({
            profileId: created.id,
            callbackToken: normalizedCallbackToken,
            callbackEncodingAesKey: normalizedCallbackAesKey,
          });
          notify.success(
            created.health_error_code === 'CONNECTION_TRUSTED_IP_REQUIRED'
              ? '连接档案已创建，请完成企业微信回调验证'
              : '企业微信应用已验证并连接',
          );
        } else {
          await createSlackConnection({ displayName: displayName.trim(), token: normalizedToken });
          notify.success('Slack 连接已验证并创建');
        }
      } else if (selectedProfile) {
        if (selectedProfile.provider === 'wecom') {
          await reauthorizeWeComConnection(selectedProfile.id, selectedProfile.revision, {
            corpId: normalizedCorpId,
            agentId: normalizedAgentId,
            corpSecret: normalizedCorpSecret,
          });
        } else {
          await reauthorizeConnection(selectedProfile.id, selectedProfile.revision, normalizedToken);
        }
        notify.success('凭据已轮换，连接恢复可用');
      }
      setToken('');
      setCorpId('');
      setAgentId('');
      setCorpSecret('');
      setSecretMode(null);
      await load();
    } catch (error) {
      notify.error(connectionErrorMessage(error, '保存连接失败'));
    } finally {
      setSavingSecret(false);
    }
  }

  async function beginOAuth() {
    /** 仅由当前交互触发 Slack OAuth；后台任务不会自行打开授权页面。 */

    if (provider !== 'slack') return;
    if (secretMode === 'create' && !displayName.trim()) {
      notify.error('请填写工作区显示名称');
      return;
    }
    if (secretMode === 'reauthorize' && !selectedProfile) return;
    setSavingSecret(true);
    try {
      const result = await startSlackOAuth({
        flowType: secretMode === 'create' ? 'create' : 'reauthorize',
        displayName: secretMode === 'create' ? displayName.trim() : undefined,
        profileId: selectedProfile?.id,
        expectedProfileRevision: selectedProfile?.revision ?? 0,
      });
      window.location.assign(result.authorize_url);
    } catch (error) {
      notify.error(connectionErrorMessage(error, '启动 Slack OAuth 失败'));
      setSavingSecret(false);
    }
  }

  async function probe(profile: ConnectionProfileRead) {
    setActingId(profile.id);
    try {
      const result = await checkConnectionHealth(profile.id);
      notify.success(result.health_status === 'healthy' ? '连接健康' : '健康状态已更新');
      await load();
    } catch (error) {
      notify.error(connectionErrorMessage(error, '健康检查失败'));
      await load();
    } finally {
      setActingId(null);
    }
  }

  async function performDisable(profile: ConnectionProfileRead) {
    setActingId(profile.id);
    try {
      await disableConnection(profile.id, profile.revision);
      setPendingDisable(null);
      notify.success('连接已停用，后续任务将无法使用此账号');
      await load();
    } catch (error) {
      notify.error(connectionErrorMessage(error, '停用连接失败'));
    } finally {
      setActingId(null);
    }
  }

  async function openBindings(profile: ConnectionProfileRead) {
    setSelectedProfile(profile);
    setSelectedAgentId('');
    setBindingDialogOpen(true);
    try {
      setBindings(await listConnectionBindings(profile.id));
    } catch (error) {
      notify.error(connectionErrorMessage(error, '加载 Agent 绑定失败'));
      setBindings([]);
    }
  }

  async function addBinding() {
    if (!selectedProfile || !selectedAgentId) {
      notify.error('请选择要绑定的数字员工');
      return;
    }
    setActingId(`bind:${selectedAgentId}`);
    try {
      await createConnectionBinding(
        selectedProfile.id,
        selectedProfile.revision,
        selectedAgentId,
        selectedProfile.provider,
      );
      notify.success('数字员工已绑定此连接');
      setSelectedAgentId('');
      setBindings(await listConnectionBindings(selectedProfile.id));
    } catch (error) {
      notify.error(connectionErrorMessage(error, '创建绑定失败'));
    } finally {
      setActingId(null);
    }
  }

  async function openInbound(profile: ConnectionProfileRead) {
    /** 同时加载路由、连接绑定和安全事件投影，正文及外部 UserID 不进入管理端。 */

    setSelectedProfile(profile);
    setInboundDialogOpen(true);
    setActingId(`inbound:${profile.id}`);
    try {
      const [bindingRows, routeRow, eventRows, userRows] = await Promise.all([
        listConnectionBindings(profile.id),
        getConnectorInboundRoute(profile.id),
        listConnectorInboundEvents(profile.id),
        api.get<InboundUser[]>(`/api/auth/users?tenant_id=${getRequestTenantId()}`),
      ]);
      setBindings(bindingRows);
      setInboundRoute(routeRow);
      setSelectedInboundAgentId(routeRow?.agent_id || '');
      setInboundEvents(eventRows);
      setInboundUsers(userRows.filter((user) => user.membership_status === 'active'));
      setEventUserSelections({});
    } catch (error) {
      notify.error(connectionErrorMessage(error, '加载消息接入配置失败'));
    } finally {
      setActingId(null);
    }
  }

  async function saveInboundRoute() {
    /** 保存档案唯一入站 Agent，并保持工具授权与消息路由使用同一绑定。 */

    if (!selectedProfile || !selectedInboundAgentId) {
      notify.error('请选择接收消息的数字员工');
      return;
    }
    setActingId('save-inbound-route');
    try {
      const route = await setConnectorInboundRoute(selectedProfile, selectedInboundAgentId);
      setInboundRoute(route);
      notify.success('消息路由已保存');
    } catch (error) {
      notify.error(connectionErrorMessage(error, '保存消息路由失败'));
    } finally {
      setActingId(null);
    }
  }

  async function authorizeInboundEvent(eventId: string) {
    /** 用已验签事件授权发送者对应的平台用户，不让管理员处理原始外部标识。 */

    const userId = eventUserSelections[eventId];
    if (!userId) {
      notify.error('请选择此发送者对应的平台用户');
      return;
    }
    setActingId(`authorize:${eventId}`);
    try {
      await bindConnectorPrincipal(eventId, userId);
      setInboundEvents((current) => current.map((event) => (
        event.id === eventId ? { ...event, principal_bound: true, status: 'pending' } : event
      )));
      notify.success('发送者已授权，原消息将自动恢复处理');
    } catch (error) {
      notify.error(connectionErrorMessage(error, '授权发送者失败'));
    } finally {
      setActingId(null);
    }
  }

  async function toggleBinding(binding: ConnectionBindingRead, enabled: boolean) {
    if (!selectedProfile) return;
    setActingId(binding.id);
    try {
      await setConnectionBindingState(selectedProfile.id, binding, enabled);
      setBindings(await listConnectionBindings(selectedProfile.id));
      notify.success(enabled ? '绑定已启用' : '绑定已停用');
    } catch (error) {
      notify.error(connectionErrorMessage(error, '更新绑定失败'));
    } finally {
      setActingId(null);
    }
  }

  async function toggleBindingWrite(binding: ConnectionBindingRead, enabled: boolean) {
    /** 显式管理审批后发送动作；只读 scope 不会隐式扩张为写权限。 */

    if (!selectedProfile || selectedProfile.provider !== 'wecom') return;
    setActingId(`write:${binding.id}`);
    try {
      await setConnectionBindingActions(selectedProfile, binding, enabled);
      await load();
      setBindings(await listConnectionBindings(selectedProfile.id));
      notify.success(enabled ? '已允许该数字员工发起审批后发送' : '已撤销审批后发送动作');
    } catch (error) {
      notify.error(connectionErrorMessage(error, '更新受控发送权限失败'));
      await load();
    } finally {
      setActingId(null);
    }
  }

  const boundAgentIds = new Set(bindings.map((binding) => binding.agent_id));
  const bindableAgents = agents.filter((agent) => !boundAgentIds.has(agent.id));

  return (
    <div className="min-h-full box-border px-[48px] pb-[43px] pt-[32px] max-[900px]:px-[16px]">
      <AppHeader onLogout={onLogout} userName={currentUser?.username} title="外部连接" />

      <div className="mt-[20px] flex flex-wrap items-center justify-between gap-[12px]">
        <div>
          <h1 className="text-[20px] font-semibold tracking-[-0.02em] text-[var(--gg-ink)]">连接账号</h1>
          <p className="mt-[4px] max-w-[680px] text-[12px] leading-[1.6] text-[var(--gg-slate)]">
            每个外部应用独立验证身份和授权范围；数字员工只能使用明确绑定的连接。
          </p>
        </div>
        <div className="flex gap-[8px]">
          <Button variant="outline" onClick={() => openGuideAt('connection-purpose-title')}>
            <BookOpen className="size-[14px]" />接入与演示指南
          </Button>
          <Button variant="outline" onClick={() => void load()} disabled={loading}>
            <RefreshCw className={cn('size-[14px]', loading && 'animate-spin')} />刷新
          </Button>
          <Button onClick={openCreate} className="bg-[var(--gg-cobalt)] text-white hover:bg-[#244bc7]">
            <Plus className="size-[14px]" />连接企业微信
          </Button>
        </div>
      </div>

      <section className="mt-[18px] overflow-hidden rounded-[18px] border border-[#dbe4fb] bg-[linear-gradient(105deg,#f6f8ff_0%,#ffffff_56%,#f3f8ff_100%)] shadow-[0_10px_30px_rgba(39,71,152,0.06)]" aria-label="外部连接用途说明">
        <div className="grid grid-cols-[minmax(0,1.25fr)_minmax(340px,0.75fr)] items-center gap-[24px] px-[22px] py-[20px] max-[820px]:grid-cols-1">
          <div>
            <span className="font-mono text-[10px] font-semibold uppercase tracking-[0.16em] text-[var(--gg-cobalt)]">企业微信 × 数字员工</span>
            <h2 className="mt-[7px] text-[18px] font-semibold tracking-[-0.02em] text-[var(--gg-ink)]">让员工在企业微信里发起任务，由受控的数字员工继续处理</h2>
            <p className="mt-[7px] max-w-[760px] text-[12px] leading-[1.75] text-[var(--gg-slate)]">连接负责验证企业应用身份、接收验签消息和提供明确授权的外部能力；它不会自动读取通讯录、开放全部聊天记录，也不会让未绑定的数字员工使用企业账号。</p>
            <Button variant="outline" className="mt-[13px] border-[#cbd8f8] bg-white text-[var(--gg-cobalt)] hover:bg-[#f5f8ff]" onClick={() => openGuideAt('connection-setup-title')}>
              查看从创建到测试成功的完整示例<ArrowRight className="size-[14px]" />
            </Button>
          </div>
          <div className="grid grid-cols-[1fr_20px_1fr_20px_1fr] items-center rounded-[15px] border border-[#dfe6f5] bg-white/85 px-[14px] py-[16px] shadow-[0_6px_18px_rgba(45,66,114,0.05)] max-[480px]:grid-cols-1">
            <GuideFlowStep icon={MessagesSquare} label="员工入口" value="企业微信消息" onClick={() => openGuideAt('connection-setup-title')} />
            <ArrowRight className="size-[14px] text-[#9aa7c0] max-[480px]:mx-auto max-[480px]:rotate-90" />
            <GuideFlowStep icon={ShieldCheck} label="安全边界" value="验签、授权、审计" onClick={() => openGuideAt('connection-purpose-title')} />
            <ArrowRight className="size-[14px] text-[#9aa7c0] max-[480px]:mx-auto max-[480px]:rotate-90" />
            <GuideFlowStep icon={Bot} label="执行主体" value="已绑定数字员工" onClick={() => openGuideAt('connection-demo-title')} />
          </div>
        </div>
      </section>

      <section className="mt-[18px] grid grid-cols-3 overflow-hidden rounded-[18px] border border-[var(--gg-border)] bg-white shadow-[var(--gg-shadow-card)] max-[720px]:grid-cols-1" aria-label="连接状态概览">
        <ConnectionMetric icon={Cable} label="连接账号" value={profiles.length} />
        <ConnectionMetric icon={CheckCircle2} label="当前可用" value={activeCount} tone="healthy" />
        <ConnectionMetric icon={Activity} label="需要处理" value={attentionCount} tone={attentionCount ? 'warning' : 'neutral'} />
      </section>

      <section className="mt-[18px] grid gap-[12px]" aria-live="polite">
        {profiles.length === 0 ? (
          <div className="grid min-h-[220px] place-items-center rounded-[18px] border border-dashed border-[var(--gg-border)] bg-white px-[24px] text-center">
            <div>
              <span className="mx-auto grid size-[48px] place-items-center rounded-[15px] bg-[#edf2ff] text-[var(--gg-cobalt)]"><Link2 className="size-[20px]" /></span>
              <h2 className="mt-[12px] text-[15px] font-semibold text-[var(--gg-ink)]">还没有外部连接</h2>
              <p className="mt-[5px] text-[12px] text-[var(--gg-slate)]">连接企业微信自建应用，再把最小只读能力授权给指定数字员工。</p>
              <Button className="mt-[14px] bg-[var(--gg-cobalt)] text-white hover:bg-[#244bc7]" onClick={openCreate}>连接企业微信</Button>
            </div>
          </div>
        ) : profiles.map((profile) => (
          <ConnectionCard
            key={profile.id}
            profile={profile}
            busy={actingId === profile.id}
            onHealth={() => void probe(profile)}
            onReauthorize={() => openReauthorize(profile)}
            onBindings={() => void openBindings(profile)}
            onInbound={() => void openInbound(profile)}
            onDisable={() => setPendingDisable(profile)}
          />
        ))}
      </section>

      <Dialog open={guideOpen} onOpenChange={setGuideOpen}>
        <DialogContent aria-describedby={undefined} className="max-h-[92vh] w-[calc(100vw-32px)] gap-[22px] overflow-y-auto rounded-[18px] sm:max-w-[1180px]">
          <div className="flex items-start gap-[12px]">
            <span className="grid size-[42px] shrink-0 place-items-center rounded-[14px] bg-[#eaf0ff] text-[var(--gg-cobalt)]"><BookOpen className="size-[19px]" /></span>
            <div>
              <DialogTitle className="text-[20px] font-semibold tracking-[-0.02em] text-[var(--gg-ink)]">企业微信连接：从创建到一次真实任务</DialogTitle>
              <p className="mt-[5px] text-[13px] leading-[1.75] text-[var(--gg-slate)]">以下流程来自“共格·序伴企业微信测试”真实联调，可按顺序完成应用建档、消息接入、数字员工绑定和最终回复验证。</p>
            </div>
          </div>

          <section className="grid gap-[12px] rounded-[15px] border border-[var(--gg-border)] p-[15px]" aria-labelledby="connection-purpose-title">
            <GuideSectionTitle index="01" title="先明确为什么连接" id="connection-purpose-title" />
            <div className="grid grid-cols-2 gap-[10px] max-[640px]:grid-cols-1">
              <GuideBoundaryCard
                icon={CheckCircle2}
                title="连接后可以"
                tone="positive"
                items={[
                  '验证企业微信自建应用身份，并读取当前应用基础信息。',
                  '接收企业微信验签消息，交给唯一绑定的数字员工处理。',
                  '在明确授权和风险确认后，把结果回复到企业微信。',
                  '执行健康检查、凭据轮换、停用、重试和审计追踪。',
                ]}
              />
              <GuideBoundaryCard
                icon={CircleX}
                title="连接后不会"
                tone="negative"
                items={[
                  '不会自动读取企业通讯录、历史聊天或未声明的数据。',
                  '不会让所有数字员工共享连接，必须逐个显式绑定。',
                  '不会绕过租户、用户映射、动作白名单和人工确认。',
                  '临时 HTTPS Tunnel 只能用于联调，不能代替正式生产入口。',
                ]}
              />
            </div>
          </section>

          <section className="grid gap-[12px] rounded-[15px] border border-[#dbe4fb] bg-[#fbfcff] p-[15px]" aria-labelledby="connection-setup-title">
            <GuideSectionTitle index="02" title="完整示例：创建企业微信连接并测试成功" id="connection-setup-title" />
            <ol className="grid gap-[9px]">
              <GuideInstruction index="1" title="创建企业微信测试应用">注册测试企业，在“应用管理”创建自建应用，设置可见范围，并记下企业 ID（CorpID）、AgentId 和 Secret。</GuideInstruction>
              <GuideInstruction index="2" title="在本页建立连接">点击“连接企业微信”，填写显示名称和三项身份信息。系统会生成回调 Token 与 43 位 EncodingAESKey；点击“验证并连接”。</GuideInstruction>
              <GuideInstruction index="3" title="配置消息回调">先通过公网 HTTPS 地址打开本页面，再创建连接。建档后把一次性显示的 URL、Token、EncodingAESKey 原样填入企业微信“接收消息服务器”，选择需要接收的消息事件并保存验证。</GuideInstruction>
              <GuideInstruction index="4" title="配置企业可信 IP">在运行后端的同一台机器执行 <code className="font-mono text-[11px] text-[#244bc7]">curl https://api.ipify.org</code>，把结果填入“企业可信 IP”。不要填写内网地址，也不要填写 Tunnel 域名解析出的 IP。</GuideInstruction>
              <GuideInstruction index="5" title="确认连接健康">回到本页点击“健康检查”。卡片显示“连接健康”，并出现 <code className="font-mono text-[11px] text-[#244bc7]">application:read</code>，表示身份和最小读取授权已经验证。</GuideInstruction>
              <GuideInstruction index="6" title="绑定数字员工和消息入口">打开“Agent 绑定”选择数字员工；再打开“消息接入”，把该员工设为唯一接收路由。企业微信成员第一次发消息后，在待授权队列中关联对应的平台用户。</GuideInstruction>
              <GuideInstruction index="7" title="发送验证消息">在企业微信自建应用会话中发送“测试动态任务：查询当前企业微信应用信息”。首次发送者完成授权后原消息会自动恢复，无需重复创建任务。</GuideInstruction>
              <GuideInstruction index="8" title="核对成功标准">企业微信收到一条任务回复；系统内产生一条可追踪 Execution，外部调用有唯一 Operation/回执，页面仍不显示 Secret、原始外部用户标识或消息正文。</GuideInstruction>
            </ol>

            <div className="grid grid-cols-2 gap-[10px] max-[680px]:grid-cols-1" aria-label="当前 Demo 网络配置示例">
              <div className="rounded-[12px] border border-[#dfe5f0] bg-white px-[13px] py-[12px]">
                <small className="font-mono text-[10px] font-semibold text-[var(--gg-cobalt)]">企业可信 IP · 出站方向</small>
                <p className="mt-[7px] text-[11px] leading-[1.65] text-[#59647a]">当前机器内网地址是 <code className="font-mono text-[#3a4254]">192.168.124.236</code>，经网关 NAT 后，企业微信 API 当前看到的公网出口是 <code className="font-mono font-semibold text-[#087a38]">103.62.49.138</code>。因此本次 Demo 应填写后者。</p>
                <p className="mt-[6px] text-[10px] leading-[1.55] text-[#8a5700]">该值是 2026-08-11 的现场检测结果，不代表运营商保证固定。路由器重拨、宽带迁移或出口切换后要重新执行命令核对。</p>
              </div>
              <div className="rounded-[12px] border border-[#dfe5f0] bg-white px-[13px] py-[12px]">
                <small className="font-mono text-[10px] font-semibold text-[var(--gg-cobalt)]">消息回调 URL · 入站方向</small>
                <p className="mt-[7px] text-[11px] leading-[1.65] text-[#59647a]">公网 HTTPS 请求通过 Tunnel 转发到 <code className="font-mono text-[#3a4254]">127.0.0.1:5137</code>。上次联调地址 <code className="break-all font-mono text-[#3a4254]">people-elect-dolls-phones.trycloudflare.com</code> 已失效，重新联调必须先取得新的 HTTPS 地址。</p>
                <p className={cn('mt-[6px] rounded-[8px] px-[9px] py-[7px] text-[10px] leading-[1.55]', isPublicHttpsOrigin() ? 'bg-[#edf8f1] text-[#087a38]' : 'bg-[#fff4df] text-[#8a5700]')}>当前页面来源：<code className="break-all font-mono">{window.location.origin}</code>。{isPublicHttpsOrigin() ? '可用于生成公网回调 URL。' : '不是公网 HTTPS 来源；此时生成的 URL 不能直接交给企业微信。'}</p>
              </div>
            </div>

            <div className="overflow-hidden rounded-[13px] border border-[#cbd8f4] bg-white" aria-label="当前 Demo 实际填写值">
              <div className="border-b border-[#dce4f4] bg-[#f4f7ff] px-[15px] py-[11px]">
                <h4 className="text-[13px] font-semibold text-[var(--gg-ink)]">当前 Demo：命令输出和实际填写值</h4>
                <p className="mt-[3px] text-[11px] leading-[1.6] text-[var(--gg-slate)]">以下公网 IP 是 2026-08-11 在运行后端的同一台机器上用三个公网服务交叉核对的结果。</p>
              </div>
              <dl className="grid grid-cols-[190px_minmax(0,1fr)_minmax(0,1fr)] text-[12px] max-[760px]:grid-cols-1">
                <DemoValueRow label="执行命令" value="curl -4 https://api.ipify.org" destination="在后端服务器终端执行" code />
                <DemoValueRow label="本次命令输出" value="103.62.49.138" destination="这是企业微信 API 当前看到的公网出口" code strong />
                <DemoValueRow label="企业可信 IP 填写" value="103.62.49.138" destination="企业微信应用 → 企业可信 IP" code strong />
                <DemoValueRow label="本机内网地址" value="192.168.124.236" destination="仅用于内网访问，不能填入企业可信 IP" code />
                <DemoValueRow label="真实成功回调 URL" value="https://people-elect-dolls-phones.trycloudflare.com/api/connectors/wecom/connprofile_36a56f3b292f49a8/callback" destination="企业微信应用 → 接收消息服务器 → URL；该临时 Tunnel 现已失效，仅作成功案例" code />
                <DemoValueRow label="回调 Token" value="bbfca6••••••••••••••••••84c3f7" destination="从建档后的一次性弹窗完整复制；真实值不写入长期说明" code />
                <DemoValueRow label="EncodingAESKey" value="hele5t7•••••••••••••••••••••bfRjAk" destination="从同一次弹窗完整复制，不能与另一轮 Token 混用" code />
              </dl>
              <p className="border-t border-[#e2e7f1] bg-[#fff9ec] px-[15px] py-[10px] text-[11px] leading-[1.65] text-[#76510b]">重要：公网 IP 可以按上表直接填写；旧 Tunnel URL 只说明成功时的完整格式，不能再次使用。Token 和 EncodingAESKey 是回调验签/解密密钥，截图中暴露过的值应轮换，页面只展示脱敏结果。新建连接时必须把一次性弹窗当场生成的完整值原样填入企业微信。</p>
            </div>

            <div className="rounded-[12px] border border-[#cfdaf5] bg-white px-[13px] py-[12px]" aria-label="回调配置值生成规则">
              <h4 className="text-[12px] font-semibold text-[var(--gg-ink)]">URL、Token、EncodingAESKey 分别怎么生成</h4>
              <dl className="mt-[9px] grid gap-[8px] text-[11px] leading-[1.65] text-[#59647a]">
                <div className="grid grid-cols-[118px_1fr] gap-[8px] max-[520px]:grid-cols-1"><dt className="font-mono font-semibold text-[#3a4254]">URL</dt><dd>系统创建连接档案后取得唯一 Profile ID，再用“当前浏览器公网来源 + <code className="font-mono text-[#244bc7]">/api/connectors/wecom/&#123;profile_id&#125;/callback</code>”组成。它同时接收企业微信首次 GET 验证和后续 POST 消息。</dd></div>
                <div className="grid grid-cols-[118px_1fr] gap-[8px] max-[520px]:grid-cols-1"><dt className="font-mono font-semibold text-[#3a4254]">Token</dt><dd>浏览器使用密码学安全随机数生成 16 字节，再编码为 32 位小写十六进制字符串。它不是 CorpSecret，也不是访问令牌；双方使用同一个 Token 校验回调签名。</dd></div>
                <div className="grid grid-cols-[118px_1fr] gap-[8px] max-[520px]:grid-cols-1"><dt className="font-mono font-semibold text-[#3a4254]">EncodingAESKey</dt><dd>浏览器使用密码学安全随机数生成企业微信要求的 43 位字符，服务端补一个 <code className="font-mono">=</code> 后解码为 32 字节 AES-256 密钥，用于解密消息并校验企业 CorpID。</dd></div>
              </dl>
              <p className="mt-[9px] rounded-[9px] bg-[#f6f8fc] px-[10px] py-[8px] text-[10px] leading-[1.6] text-[#68738a]">三项值在点击“验证并连接”时一起提交并加密保存。建档后的完整值只在当前浏览器内存中显示一次；企业微信后台必须填写这一组完全相同的值。点击“重新生成”后，旧 Token 和 EncodingAESKey 立即作废，不要混用两轮结果。</p>
            </div>

            <GuideScreenshot
              src={weComConnectionCardScreenshot}
              alt="企业微信连接健康后的真实连接卡片"
              title="实际截图 1 · 连接健康后"
              description="卡片同时展示账号身份、application:read、密钥修订、允许动作，以及健康检查、Agent 绑定和消息接入三个后续入口。"
            />
          </section>

          <section className="grid gap-[12px] rounded-[15px] border border-[#cfe8d8] bg-[#f7fcf8] p-[15px]" aria-labelledby="connection-demo-title">
            <GuideSectionTitle index="03" title="演示场景：让数字员工查询企业微信应用信息" id="connection-demo-title" />
            <div className="rounded-[12px] border border-[#cfe8d8] bg-white px-[13px] py-[12px]">
              <h4 className="text-[12px] font-semibold text-[var(--gg-ink)]">场景角色与接入方式</h4>
              <ol className="mt-[10px] grid grid-cols-2 gap-[10px] max-[620px]:grid-cols-1">
                <ScenarioStep index="1" title="管理员绑定真实数字员工">在连接卡片点击“Agent 绑定”，选择本次真实使用的“平台能力演示助手”。系统授予 application:read；本案例的“审批后发送”已经开启。</ScenarioStep>
                <ScenarioStep index="2" title="消息路由">在“消息接入”选择同一个数字员工作为唯一接收路由，避免入口和工具权限指向不同 Agent。</ScenarioStep>
                <ScenarioStep index="3" title="员工授权">企业微信成员首次发消息后，管理员在待授权发送者中选择其平台用户；原消息随后自动恢复。</ScenarioStep>
                <ScenarioStep index="4" title="开始使用">员工继续在企业微信应用会话中用自然语言提问，不需要进入共格·序伴管理端。</ScenarioStep>
              </ol>
            </div>

            <div className="grid gap-[14px]">
              <GuideScreenshot
                src={weComAgentBindingScreenshot}
                alt="在真实数字员工绑定弹窗中授权企业微信连接"
                title="实际截图 2 · 绑定数字员工"
                description="绑定不是全局共享：每个数字员工分别获得 application:read；“审批后发送”是独立开关。"
              />
              <GuideScreenshot
                src={weComMessageRoutingScreenshot}
                alt="在真实消息接入弹窗中配置数字员工路由和发送者授权"
                title="实际截图 3 · 配置消息入口"
                description="先指定接收消息的数字员工，再把已验签发送者映射到平台用户；系统不在页面展示原始 UserID 或正文。"
              />
            </div>
            <p className="text-[12px] leading-[1.7] text-[#68776e]">以上截图直接取自当前运行中的真实 Demo：连接“共格·序伴企业微信测试”绑定数字员工“平台能力演示助手”。点击任一截图可查看原始尺寸。</p>

            <div className="grid grid-cols-[minmax(0,0.75fr)_24px_minmax(0,1.25fr)] items-stretch gap-[8px] max-[680px]:grid-cols-1">
              <div className="rounded-[12px] bg-white px-[13px] py-[12px] shadow-[0_4px_14px_rgba(22,96,55,0.06)]">
                <small className="font-mono text-[10px] text-[#087a38]">员工在企业微信发送</small>
                <p className="mt-[6px] text-[13px] font-medium leading-[1.6] text-[var(--gg-ink)]">“测试动态任务：查询当前企业微信应用信息”</p>
              </div>
              <ArrowRight className="m-auto size-[15px] text-[#78a98b] max-[680px]:rotate-90" />
              <div className="rounded-[12px] bg-white px-[13px] py-[12px] shadow-[0_4px_14px_rgba(22,96,55,0.06)]">
                <small className="font-mono text-[10px] text-[#087a38]">系统执行链</small>
                <p className="mt-[6px] text-[12px] leading-[1.65] text-[#465267]">验签消息 → 确认发送者映射 → 路由到已绑定数字员工 → DynamicTaskAgent 规划只读查询 → 使用该连接执行 <code className="font-mono text-[11px] text-[#087a38]">wecom.application_info</code> → 将结果回复原会话并记录审计。</p>
              </div>
            </div>
            <div className="rounded-[12px] border border-dashed border-[#b9dac5] px-[13px] py-[10px] text-[11px] leading-[1.65] text-[#446151]">演示通过标准：回复内容来自真实连接而非预置文案；同一消息只形成一次执行和一次外部效果；凭据不进入模型上下文；未绑定 Agent、未授权用户或停用连接均在调用前被拒绝。</div>
          </section>

          <div className="flex flex-wrap justify-end gap-[8px]">
            <Button variant="outline" onClick={() => setGuideOpen(false)}>关闭指南</Button>
            <Button className="bg-[var(--gg-cobalt)] text-white hover:bg-[#244bc7]" onClick={() => { setGuideOpen(false); openCreate(); }}>
              <Plus className="size-[14px]" />开始创建连接
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      <Dialog open={secretMode !== null} onOpenChange={(open) => !open && closeSecretDialog()}>
        <DialogContent aria-describedby={undefined} className="gap-[16px] rounded-[16px] sm:max-w-[520px]">
          <div className="flex items-center gap-[10px]">
            <span className="grid size-[38px] place-items-center rounded-[12px] bg-[#edf2ff] text-[var(--gg-cobalt)]"><KeyRound className="size-[17px]" /></span>
            <div>
              <DialogTitle className="text-[15px] font-semibold text-[var(--gg-ink)]">
                {secretMode === 'create' ? `连接${providerLabel(provider)}` : `重新授权 ${selectedProfile?.display_name || ''}`}
              </DialogTitle>
              <p className="mt-[2px] text-[11px] text-[var(--gg-slate)]">凭据只发送到服务端密钥边界，不会显示在档案、日志或任务上下文中。</p>
            </div>
          </div>
          {secretMode === 'create' ? (
            <>
              <LabeledField label="连接类型">
                <Select value={provider} onValueChange={(value) => setProvider(value as ConnectionProvider)}>
                  <SelectTrigger aria-label="连接类型" className={SELECT_TRIGGER_CLASS}>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="wecom">企业微信自建应用</SelectItem>
                    <SelectItem value="slack">Slack 工作区</SelectItem>
                  </SelectContent>
                </Select>
              </LabeledField>
              <LabeledField label="连接显示名称">
                <Input autoFocus value={displayName} onChange={(event) => setDisplayName(event.target.value)} placeholder={provider === 'wecom' ? '例如：序伴集成测试' : '例如：合同协作工作区'} />
              </LabeledField>
            </>
          ) : null}
          {provider === 'wecom' ? (
            <div className="grid gap-[12px] rounded-[13px] border border-[#dce5ff] bg-[#fbfcff] p-[12px]">
              <LabeledField label="企业 ID（CorpID）">
                <Input autoFocus={secretMode === 'reauthorize'} autoComplete="off" value={corpId} onChange={(event) => setCorpId(event.target.value)} placeholder="ww…" />
              </LabeledField>
              <LabeledField label="应用 AgentId">
                <Input inputMode="numeric" autoComplete="off" value={agentId} onChange={(event) => setAgentId(event.target.value)} placeholder="1000002" />
              </LabeledField>
              <LabeledField label="应用 Secret">
                <Input type="password" autoComplete="new-password" value={corpSecret} onChange={(event) => setCorpSecret(event.target.value)} placeholder="仅本次提交可见" />
              </LabeledField>
              {secretMode === 'create' ? (
                <div className="grid gap-[10px] rounded-[11px] border border-[#dfe5f0] bg-white p-[10px]">
                  <div className="flex items-center justify-between gap-[10px]">
                    <span className="text-[11px] font-medium text-[#464c5e]">消息回调验证密钥</span>
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      onClick={() => {
                        const next = generateWeComCallbackCredentials();
                        setCallbackToken(next.token);
                        setCallbackEncodingAesKey(next.encodingAesKey);
                      }}
                    >重新生成</Button>
                  </div>
                  <LabeledField label="回调 Token">
                    <Input readOnly value={callbackToken} className="font-mono text-[11px]" />
                  </LabeledField>
                  <LabeledField label="EncodingAESKey">
                    <Input readOnly value={callbackEncodingAesKey} className="font-mono text-[11px]" />
                  </LabeledField>
                  <p className="text-[10px] leading-[1.55] text-[var(--gg-slate)]">建档成功后会显示一次完整回调配置；服务端仅加密保存，之后不会回显密钥。</p>
                </div>
              ) : null}
            </div>
          ) : (
            <LabeledField label="Slack Bot Token">
              <Input autoFocus={secretMode === 'reauthorize'} type="password" autoComplete="new-password" value={token} onChange={(event) => setToken(event.target.value)} placeholder="xoxb-…" />
            </LabeledField>
          )}
          <div className="rounded-[12px] border border-[#dce5ff] bg-[#f7f9ff] px-[12px] py-[10px] text-[11px] leading-[1.6] text-[#52617f]">
            {provider === 'wecom' ? <>本批只开放 <code className="font-mono text-[#244bc7]">application:read</code>，用于验证和读取当前自建应用基础信息；不会读取通讯录或发送消息。</> : <>本批只申请 <code className="font-mono text-[#244bc7]">channels:read</code>，用于读取频道基础信息；不会发送消息。</>}
          </div>
          <div className="flex justify-end gap-[8px]">
            {provider === 'slack' ? <Button variant="outline" disabled={savingSecret} onClick={() => void beginOAuth()}>通过 Slack OAuth</Button> : null}
            <Button variant="outline" disabled={savingSecret} onClick={closeSecretDialog}>取消</Button>
            <Button disabled={savingSecret} onClick={() => void saveSecret()} className="bg-[var(--gg-cobalt)] text-white hover:bg-[#244bc7]">
              {savingSecret ? '正在验证' : secretMode === 'create' ? '验证并连接' : '验证并更新'}
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      <Dialog open={weComSetup !== null} onOpenChange={(open) => !open && setWeComSetup(null)}>
        <DialogContent aria-describedby={undefined} className="gap-[16px] rounded-[16px] sm:max-w-[640px]">
          <div>
            <DialogTitle className="text-[15px] font-semibold text-[var(--gg-ink)]">配置企业微信接收消息服务器</DialogTitle>
            <p className="mt-[4px] text-[11px] leading-[1.6] text-[var(--gg-slate)]">以下密钥仅在当前浏览器内存中显示一次。先复制到企业微信并保存验证，再关闭窗口。</p>
          </div>
          {weComSetup ? (
            <div className="grid gap-[10px]">
              <CopyValue label="URL" value={weComCallbackUrl(weComSetup.profileId)} />
              <CopyValue label="Token" value={weComSetup.callbackToken} />
              <CopyValue label="EncodingAESKey" value={weComSetup.callbackEncodingAesKey} />
            </div>
          ) : null}
          {!isPublicHttpsOrigin() ? <div className="rounded-[12px] border border-[#f1d28f] bg-[#fff8e8] px-[12px] py-[10px] text-[11px] leading-[1.6] text-[#76510b]">当前页面不是公网 HTTPS 地址，因此上方 URL 仅能说明回调路径，不能直接保存到企业微信。请先建立新的 HTTPS Tunnel，通过该地址重新打开系统，再创建连接。</div> : null}
          <div className="rounded-[12px] border border-[#f1d28f] bg-[#fff8e8] px-[12px] py-[10px] text-[11px] leading-[1.6] text-[#76510b]">企业微信保存成功后，再回到“企业可信 IP”填写固定公网出口 IP；在此之前连接会保持“连接受限”，不会被数字员工误用。</div>
          <div className="flex justify-end"><Button onClick={() => setWeComSetup(null)}>我已完成配置</Button></div>
        </DialogContent>
      </Dialog>

      <Dialog open={bindingDialogOpen} onOpenChange={setBindingDialogOpen}>
        <DialogContent aria-describedby={undefined} className="gap-[16px] rounded-[16px] sm:max-w-[600px]">
          <div>
            <DialogTitle className="text-[15px] font-semibold text-[var(--gg-ink)]">数字员工绑定</DialogTitle>
            <p className="mt-[4px] text-[11px] text-[var(--gg-slate)]">{selectedProfile?.display_name} · 默认仅授予 {providerReadScope(selectedProfile?.provider)}；企业微信发送必须另行显式开启且每次审批。</p>
          </div>
          <div className="flex gap-[8px] max-[620px]:flex-col">
            <Select value={selectedAgentId} onValueChange={setSelectedAgentId}>
              <SelectTrigger aria-label="选择数字员工" className={cn(SELECT_TRIGGER_CLASS, 'flex-1')}>
                <SelectValue placeholder={bindableAgents.length ? '选择尚未绑定的数字员工' : '没有可新增的数字员工'} />
              </SelectTrigger>
              <SelectContent>
                {bindableAgents.map((agent) => <SelectItem key={agent.id} value={agent.id}>{agent.name}</SelectItem>)}
              </SelectContent>
            </Select>
            <Button disabled={!selectedAgentId || Boolean(actingId)} onClick={() => void addBinding()} className="bg-[var(--gg-cobalt)] text-white hover:bg-[#244bc7]">新增绑定</Button>
          </div>
          <div className="grid max-h-[320px] gap-[8px] overflow-y-auto">
            {bindings.length === 0 ? (
              <div className="rounded-[12px] border border-dashed border-[var(--gg-border)] px-[14px] py-[24px] text-center text-[12px] text-[var(--gg-slate)]">此账号尚未绑定数字员工</div>
            ) : bindings.map((binding) => (
              <div key={binding.id} className="flex items-center justify-between gap-[14px] rounded-[12px] border border-[var(--gg-border)] px-[13px] py-[11px]">
                <div className="flex min-w-0 items-center gap-[10px]">
                  <span className="grid size-[32px] shrink-0 place-items-center rounded-[10px] bg-[#f0f3f9] text-[#5e6b86]"><Bot className="size-[15px]" /></span>
                  <span className="min-w-0">
                    <strong className="block truncate text-[12px] font-semibold text-[var(--gg-ink)]">{agentNames.get(binding.agent_id) || binding.agent_id}</strong>
                    <span className="mt-[2px] block text-[11px] text-[var(--gg-slate)]">{binding.allowed_scopes.join(', ')} · 修订 {binding.revision}</span>
                  </span>
                </div>
                <div className="grid shrink-0 gap-[7px] text-[11px] text-[var(--gg-slate)]">
                  {selectedProfile?.provider === 'wecom' ? (
                    <label className="flex items-center justify-end gap-[8px]">
                      审批后发送
                      <Switch
                        checked={binding.allowed_actions.includes('wecom.message_send')}
                        disabled={!binding.enabled || actingId === `write:${binding.id}`}
                        onCheckedChange={(next) => void toggleBindingWrite(binding, next)}
                        aria-label={`${agentNames.get(binding.agent_id) || binding.agent_id}审批后发送`}
                      />
                    </label>
                  ) : null}
                  <label className="flex items-center justify-end gap-[8px]">
                    {binding.enabled ? '已授权' : '已停用'}
                    <Switch checked={binding.enabled} disabled={actingId === binding.id} onCheckedChange={(next) => void toggleBinding(binding, next)} aria-label={`${agentNames.get(binding.agent_id) || binding.agent_id}绑定状态`} />
                  </label>
                </div>
              </div>
            ))}
          </div>
          <div className="flex justify-end"><Button variant="outline" onClick={() => setBindingDialogOpen(false)}>关闭</Button></div>
        </DialogContent>
      </Dialog>

      <Dialog open={inboundDialogOpen} onOpenChange={setInboundDialogOpen}>
        <DialogContent aria-describedby={undefined} className="gap-[16px] rounded-[16px] sm:max-w-[720px]">
          <div className="flex items-start gap-[11px]">
            <span className="grid size-[40px] shrink-0 place-items-center rounded-[13px] bg-[#edf2ff] text-[var(--gg-cobalt)]"><MessagesSquare className="size-[18px]" /></span>
            <div>
              <DialogTitle className="text-[15px] font-semibold text-[var(--gg-ink)]">消息接入</DialogTitle>
              <p className="mt-[3px] text-[11px] leading-[1.6] text-[var(--gg-slate)]">{selectedProfile?.display_name} · 先确定接收消息的数字员工，再授权已验签的发送者。</p>
            </div>
          </div>

          <section className="grid grid-cols-[28px_1fr] gap-[10px] rounded-[14px] border border-[#dce5ff] bg-[#fbfcff] p-[13px]" aria-label="入站路由">
            <span className="grid size-[25px] place-items-center rounded-full bg-[var(--gg-cobalt)] font-mono text-[10px] font-semibold text-white">01</span>
            <div className="min-w-0">
              <h3 className="text-[12px] font-semibold text-[var(--gg-ink)]">选择接收消息的数字员工</h3>
              <p className="mt-[2px] text-[10px] leading-[1.5] text-[var(--gg-slate)]">这里只显示已获得此连接只读能力的数字员工，避免消息入口和工具权限分叉。</p>
              <div className="mt-[9px] flex gap-[8px] max-[620px]:flex-col">
                <Select value={selectedInboundAgentId} onValueChange={setSelectedInboundAgentId}>
                  <SelectTrigger aria-label="接收消息的数字员工" className={cn(SELECT_TRIGGER_CLASS, 'flex-1')}>
                    <SelectValue placeholder="请先在 Agent 绑定中授权数字员工" />
                  </SelectTrigger>
                  <SelectContent>
                    {bindings.filter((binding) => binding.enabled).map((binding) => (
                      <SelectItem key={binding.id} value={binding.agent_id}>{agentNames.get(binding.agent_id) || binding.agent_id}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <Button disabled={!selectedInboundAgentId || actingId === 'save-inbound-route'} onClick={() => void saveInboundRoute()} className="bg-[var(--gg-cobalt)] text-white hover:bg-[#244bc7]">保存消息路由</Button>
              </div>
              {inboundRoute ? <p className="mt-[7px] text-[10px] text-[#087a38]">当前路由：{agentNames.get(inboundRoute.agent_id) || inboundRoute.agent_id}</p> : null}
            </div>
          </section>

          <section className="grid grid-cols-[28px_1fr] gap-[10px] rounded-[14px] border border-[var(--gg-border)] p-[13px]" aria-label="发送者授权队列">
            <span className="grid size-[25px] place-items-center rounded-full bg-[#eff2f7] font-mono text-[10px] font-semibold text-[#59647a]">02</span>
            <div className="min-w-0">
              <h3 className="text-[12px] font-semibold text-[var(--gg-ink)]">授权待处理发送者</h3>
              <p className="mt-[2px] text-[10px] leading-[1.5] text-[var(--gg-slate)]">系统不显示企业微信 UserID 或消息正文；请选择它在本平台对应的活动用户。</p>
              <div className="mt-[9px] grid max-h-[280px] gap-[8px] overflow-y-auto">
                {inboundEvents.filter((event) => !event.principal_bound).length === 0 ? (
                  <div className="rounded-[11px] border border-dashed border-[var(--gg-border)] px-[12px] py-[18px] text-center text-[11px] text-[var(--gg-slate)]">没有待授权发送者</div>
                ) : inboundEvents.filter((event) => !event.principal_bound).map((event) => (
                  <div key={event.id} className="grid grid-cols-[minmax(0,1fr)_minmax(180px,0.8fr)_auto] items-center gap-[8px] rounded-[11px] bg-[#f8f9fc] px-[10px] py-[9px] max-[620px]:grid-cols-1">
                    <span className="min-w-0"><strong className="block text-[11px] font-medium text-[#3a4254]">企业微信发送者 · {formatDateTime(event.created_at)}</strong><small className="mt-[2px] block truncate font-mono text-[10px] text-[var(--gg-slate)]">{event.last_error_code || event.status}</small></span>
                    <Select value={eventUserSelections[event.id] || ''} onValueChange={(value) => setEventUserSelections((current) => ({ ...current, [event.id]: value }))}>
                      <SelectTrigger aria-label={`选择事件${event.id}对应用户`} className={SELECT_TRIGGER_CLASS}><SelectValue placeholder="对应的平台用户" /></SelectTrigger>
                      <SelectContent>{inboundUsers.map((user) => <SelectItem key={user.id} value={user.id}>{user.display_name || user.username}</SelectItem>)}</SelectContent>
                    </Select>
                    <Button size="sm" variant="outline" disabled={!eventUserSelections[event.id] || actingId === `authorize:${event.id}`} onClick={() => void authorizeInboundEvent(event.id)}>授权并恢复</Button>
                  </div>
                ))}
              </div>
            </div>
          </section>
          <div className="flex justify-end"><Button variant="outline" onClick={() => setInboundDialogOpen(false)}>关闭</Button></div>
        </DialogContent>
      </Dialog>

      <Dialog open={pendingDisable !== null} onOpenChange={(open) => !open && setPendingDisable(null)}>
        <DialogContent aria-describedby={undefined} className="gap-[16px] rounded-[16px] sm:max-w-[460px]">
          <div className="flex items-start gap-[11px]">
            <span className="grid size-[38px] shrink-0 place-items-center rounded-[12px] bg-[#fdecec] text-[#b42318]"><Unplug className="size-[17px]" /></span>
            <div>
              <DialogTitle className="text-[15px] font-semibold text-[var(--gg-ink)]">停用 {pendingDisable?.display_name || '连接'}</DialogTitle>
              <p className="mt-[5px] text-[12px] leading-[1.65] text-[var(--gg-slate)]">所有数字员工绑定会立即失效，等待和新建任务都会在外部调用前被拒绝。历史执行与密钥修订记录仍会保留。</p>
            </div>
          </div>
          <div className="flex justify-end gap-[8px]">
            <Button variant="outline" onClick={() => setPendingDisable(null)}>取消</Button>
            <Button disabled={!pendingDisable || actingId === pendingDisable.id} onClick={() => pendingDisable && void performDisable(pendingDisable)} className="bg-[#b42318] text-white hover:bg-[#8f1d14]">确认停用</Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function GuideFlowStep({ icon: Icon, label, value, onClick }: { icon: typeof Bot; label: string; value: string; onClick: () => void }) {
  /** 用可点击的真实节点表达外部消息链路，并定位到对应操作指南。 */

  return (
    <button type="button" onClick={onClick} aria-label={`查看${value}步骤`} className="group grid min-w-0 cursor-pointer place-items-center gap-[5px] rounded-[11px] border border-transparent px-[5px] py-[7px] text-center transition-colors hover:border-[#d7e1fb] hover:bg-[#f1f5ff] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--gg-cobalt)] focus-visible:ring-offset-2">
      <span className="grid size-[32px] place-items-center rounded-[11px] bg-[#edf2ff] text-[var(--gg-cobalt)]"><Icon className="size-[15px]" /></span>
      <small className="text-[9px] text-[#8a93a8]">{label}</small>
      <strong className="text-[11px] font-semibold text-[#3a4254]">{value}</strong>
      <small className="text-[10px] font-medium text-[var(--gg-cobalt)]">查看步骤 →</small>
    </button>
  );
}

function GuideSectionTitle({ index, title, id }: { index: string; title: string; id: string }) {
  /** 用有意义的顺序编号标识指南的理解、接入和演示三个阶段。 */

  return (
    <div className="flex items-center gap-[9px]">
      <span className="grid size-[27px] shrink-0 place-items-center rounded-full bg-[var(--gg-cobalt)] font-mono text-[10px] font-semibold text-white">{index}</span>
      <h3 id={id} className="text-[13px] font-semibold text-[var(--gg-ink)]">{title}</h3>
    </div>
  );
}

function GuideBoundaryCard({
  icon: Icon,
  title,
  tone,
  items,
}: {
  icon: typeof CheckCircle2;
  title: string;
  tone: 'positive' | 'negative';
  items: string[];
}) {
  /** 并列说明连接的能力与明确禁区，避免用户把外部连接理解成全量系统授权。 */

  const positive = tone === 'positive';
  return (
    <div className={cn('rounded-[12px] border px-[13px] py-[12px]', positive ? 'border-[#cfe8d8] bg-[#f7fcf8]' : 'border-[#ecd9d6] bg-[#fffafa]')}>
      <h4 className={cn('flex items-center gap-[7px] text-[12px] font-semibold', positive ? 'text-[#087a38]' : 'text-[#a33a31]')}><Icon className="size-[15px]" />{title}</h4>
      <ul className="mt-[8px] grid gap-[6px]">
        {items.map((item) => <li key={item} className="grid grid-cols-[8px_1fr] gap-[6px] text-[11px] leading-[1.6] text-[#59647a]"><span aria-hidden="true" className={cn('mt-[7px] size-[4px] rounded-full', positive ? 'bg-[#42a66a]' : 'bg-[#cf756d]')} />{item}</li>)}
      </ul>
    </div>
  );
}

function GuideInstruction({ index, title, children }: { index: string; title: string; children: ReactNode }) {
  /** 展示一项可执行且可核对的企业微信接入步骤。 */

  return (
    <li className="grid grid-cols-[26px_1fr] gap-[10px] rounded-[11px] border border-[#e5e9f2] bg-white px-[11px] py-[10px]">
      <span className="grid size-[24px] place-items-center rounded-[8px] bg-[#edf2ff] font-mono text-[10px] font-semibold text-[var(--gg-cobalt)]">{index}</span>
      <span><strong className="block text-[13px] font-semibold text-[#3a4254]">{title}</strong><span className="mt-[3px] block text-[12px] leading-[1.75] text-[var(--gg-slate)]">{children}</span></span>
    </li>
  );
}

function ScenarioStep({ index, title, children }: { index: string; title: string; children: ReactNode }) {
  /** 说明数字员工接入和实际使用场景中的一个责任阶段。 */

  return (
    <li className="rounded-[11px] bg-[#f4f9f6] px-[13px] py-[12px]">
      <span className="font-mono text-[10px] font-semibold text-[#087a38]">STEP {index}</span>
      <strong className="mt-[4px] block text-[13px] font-semibold text-[#34483b]">{title}</strong>
      <span className="mt-[4px] block text-[12px] leading-[1.75] text-[#617267]">{children}</span>
    </li>
  );
}

function GuideScreenshot({ src, alt, title, description }: { src: string; alt: string; title: string; description: string }) {
  /** 在指南中展示可点击放大的真实验收截图及其核对重点。 */

  return (
    <figure className="overflow-hidden rounded-[12px] border border-[#dfe5f0] bg-white">
      <a href={src} target="_blank" rel="noreferrer" aria-label={`放大查看${title}`} className="group block overflow-hidden bg-[#f3f6fb] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[var(--gg-cobalt)]">
        <img src={src} alt={alt} className="h-auto w-full transition-transform duration-200 group-hover:scale-[1.015] motion-reduce:transition-none" />
      </a>
      <figcaption className="border-t border-[#e8ecf3] px-[11px] py-[9px]">
        <strong className="block text-[14px] font-semibold text-[#3a4254]">{title}</strong>
        <span className="mt-[4px] block text-[12px] leading-[1.7] text-[var(--gg-slate)]">{description}</span>
        <span className="mt-[6px] block text-[11px] font-medium text-[var(--gg-cobalt)]">点击截图可查看原图</span>
      </figcaption>
    </figure>
  );
}

function DemoValueRow({ label, value, destination, code = false, strong = false }: { label: string; value: string; destination: string; code?: boolean; strong?: boolean }) {
  /** 将真实联调命令、输出和填写位置按同一行呈现，避免用户自行推断。 */

  return (
    <div className="contents max-[760px]:block">
      <dt className="border-b border-r border-[#e5e9f2] bg-[#fafbfe] px-[14px] py-[11px] font-medium text-[#586176] max-[760px]:border-r-0 max-[760px]:pb-[4px]">{label}</dt>
      <dd className={cn('break-all border-b border-r border-[#e5e9f2] px-[14px] py-[11px] text-[#30384c] max-[760px]:border-r-0 max-[760px]:py-[5px]', code && 'font-mono', strong && 'font-semibold text-[#087a38]')}>{value}</dd>
      <dd className="border-b border-[#e5e9f2] px-[14px] py-[11px] leading-[1.65] text-[#68738a] max-[760px]:pt-[5px]">{destination}</dd>
    </div>
  );
}

function ConnectionCard({
  profile,
  busy,
  onHealth,
  onReauthorize,
  onBindings,
  onInbound,
  onDisable,
}: {
  profile: ConnectionProfileRead;
  busy: boolean;
  onHealth: () => void;
  onReauthorize: () => void;
  onBindings: () => void;
  onInbound: () => void;
  onDisable: () => void;
}) {
  /** 展示账号身份链和所有显式管理动作。 */

  const healthy = profile.health_status === 'healthy';
  const needsReauth = profile.status === 'reauth_required';
  return (
    <article className="overflow-hidden rounded-[18px] border border-[var(--gg-border)] bg-white shadow-[0_10px_28px_rgba(40,63,120,0.05)]">
      <div className="grid grid-cols-[minmax(0,1.4fr)_minmax(320px,1fr)] gap-[18px] px-[18px] py-[16px] max-[780px]:grid-cols-1">
        <div className="min-w-0">
          <div className="flex flex-wrap items-start justify-between gap-[10px]">
            <div className="flex min-w-0 items-center gap-[11px]">
              <span className={cn('grid size-[42px] shrink-0 place-items-center rounded-[13px] text-white', profile.provider === 'wecom' ? 'bg-[#2f73da]' : 'bg-[#4a154b]')}><Cable className="size-[18px]" /></span>
              <span className="min-w-0">
                <strong className="block truncate text-[15px] font-semibold text-[var(--gg-ink)]">{profile.display_name}</strong>
                <span className="mt-[2px] block truncate font-mono text-[11px] text-[var(--gg-slate)]">{providerLabel(profile.provider)} · {profile.account_id}</span>
              </span>
            </div>
            <span className={cn('rounded-full px-[9px] py-[4px] text-[11px] font-medium', healthy ? 'bg-[#eaf8ef] text-[#087a38]' : profile.health_status === 'degraded' ? 'bg-[#fff4df] text-[#8a5700]' : 'bg-[#fdecec] text-[#b42318]')}>
              {HEALTH_COPY[profile.health_status]}
            </span>
          </div>
          <p className="mt-[12px] text-[11px] leading-[1.6] text-[var(--gg-slate)]">
            {connectionStatusDetail(profile)}
          </p>
        </div>

        <div className="grid grid-cols-[1fr_20px_1fr_20px_1fr] items-center gap-[6px] rounded-[14px] border border-[#e8ecf4] bg-[#fafbfe] px-[13px] py-[11px] max-[520px]:grid-cols-1">
          <IdentityStep icon={ShieldCheck} label="授权范围" value={profile.granted_scopes.join(', ') || '无'} />
          <span className="h-px bg-[#d9dfec] max-[520px]:hidden" />
          <IdentityStep icon={KeyRound} label="密钥修订" value={`v${profile.secret_revision}`} />
          <span className="h-px bg-[#d9dfec] max-[520px]:hidden" />
          <IdentityStep icon={Bot} label="允许动作" value={profile.tool_allowlist.join(', ') || '无'} />
        </div>
      </div>
      <div className="flex flex-wrap items-center justify-between gap-[10px] border-t border-[#edf0f5] bg-[#fcfcfd] px-[18px] py-[10px]">
        <span className="text-[11px] text-[var(--gg-slate)]">最近检查：{formatDateTime(profile.last_checked_at || undefined)}</span>
        <div className="flex flex-wrap gap-[7px]">
          <Button size="sm" variant="outline" disabled={busy || profile.status === 'disabled'} onClick={onHealth}><Activity className="size-[13px]" />健康检查</Button>
          <Button size="sm" variant="outline" onClick={onBindings}><Bot className="size-[13px]" />Agent 绑定</Button>
          {profile.provider === 'wecom' ? <Button size="sm" variant="outline" onClick={onInbound}><MessagesSquare className="size-[13px]" />消息接入</Button> : null}
          <Button size="sm" variant="outline" onClick={onReauthorize}><KeyRound className="size-[13px]" />{needsReauth ? '立即重新授权' : '轮换凭据'}</Button>
          {profile.status !== 'disabled' ? <Button size="sm" variant="outline" disabled={busy} onClick={onDisable} className="text-[#b42318] hover:text-[#8f1d14]"><Unplug className="size-[13px]" />停用</Button> : null}
        </div>
      </div>
    </article>
  );
}

function IdentityStep({ icon: Icon, label, value }: { icon: typeof ShieldCheck; label: string; value: string }) {
  /** 展示连接身份链中的一个可核对事实。 */

  return (
    <span className="flex min-w-0 items-center gap-[8px]">
      <Icon className="size-[14px] shrink-0 text-[var(--gg-cobalt)]" />
      <span className="min-w-0"><small className="block text-[10px] text-[#8a93a8]">{label}</small><strong className="mt-[1px] block truncate text-[11px] font-medium text-[#3a4254]">{value}</strong></span>
    </span>
  );
}

function ConnectionMetric({ icon: Icon, label, value, tone = 'neutral' }: { icon: typeof Cable; label: string; value: number; tone?: 'neutral' | 'healthy' | 'warning' }) {
  /** 展示连接控制面的紧凑状态统计。 */

  return (
    <div className="flex items-center gap-[11px] border-r border-[var(--gg-border)] px-[18px] py-[15px] last:border-r-0 max-[720px]:border-b max-[720px]:border-r-0 max-[720px]:last:border-b-0">
      <span className={cn('grid size-[36px] place-items-center rounded-[11px]', tone === 'healthy' ? 'bg-[#eaf8ef] text-[#087a38]' : tone === 'warning' ? 'bg-[#fff4df] text-[#8a5700]' : 'bg-[#edf2ff] text-[var(--gg-cobalt)]')}><Icon className="size-[16px]" /></span>
      <span><strong className="block text-[19px] font-semibold text-[var(--gg-ink)]">{value}</strong><small className="text-[11px] text-[var(--gg-slate)]">{label}</small></span>
    </div>
  );
}

function LabeledField({ label, children }: { label: string; children: ReactNode }) {
  /** 为连接表单提供一致的可访问标签。 */

  return <label className="grid gap-[6px]"><span className="text-[12px] font-medium text-[#464c5e]">{label}</span>{children}</label>;
}

function CopyValue({ label, value }: { label: string; value: string }) {
  /** 展示一次性回调配置并通过明确用户动作复制。 */

  return (
    <div className="grid gap-[5px]">
      <span className="text-[11px] font-medium text-[#464c5e]">{label}</span>
      <div className="flex min-w-0 items-center gap-[7px] rounded-[11px] border border-[#dfe5f0] bg-[#fafbfe] p-[7px]">
        <code className="min-w-0 flex-1 break-all px-[4px] text-[11px] text-[#30384c]">{value}</code>
        <Button
          type="button"
          size="sm"
          variant="outline"
          aria-label={`复制${label}`}
          onClick={() => void copyText(value)}
        ><Copy className="size-[13px]" />复制</Button>
      </div>
    </div>
  );
}

function generateWeComCallbackCredentials(): { token: string; encodingAesKey: string } {
  /** 使用浏览器 CSPRNG 生成企业微信接受的 Token 与 43 字符 AESKey。 */

  const tokenBytes = crypto.getRandomValues(new Uint8Array(16));
  const token = Array.from(tokenBytes, (value) => value.toString(16).padStart(2, '0')).join('');
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
  let encodingAesKey = '';
  while (encodingAesKey.length < 43) {
    const bytes = crypto.getRandomValues(new Uint8Array(64));
    for (const value of bytes) {
      if (value >= 248) continue;
      encodingAesKey += alphabet[value % alphabet.length];
      if (encodingAesKey.length === 43) break;
    }
  }
  return { token, encodingAesKey };
}

function weComCallbackUrl(profileId: string): string {
  /** 使用当前部署来源生成同源公开回调地址，避免把开发端口写入服务端。 */

  return new URL(`/api/connectors/wecom/${profileId}/callback`, window.location.origin).toString();
}

function isPublicHttpsOrigin(): boolean {
  /** 判断当前页面来源是否具备企业微信可回拨的公网 HTTPS 基本形态。 */

  if (window.location.protocol !== 'https:') return false;
  const hostname = window.location.hostname.toLowerCase();
  if (hostname === 'localhost' || hostname === '127.0.0.1' || hostname === '::1' || hostname.endsWith('.local')) return false;
  if (/^10\./.test(hostname) || /^192\.168\./.test(hostname)) return false;
  const private172 = hostname.match(/^172\.(\d{1,2})\./);
  return !private172 || Number(private172[1]) < 16 || Number(private172[1]) > 31;
}

async function copyText(value: string): Promise<void> {
  /** 复制回调值并把浏览器拒绝转换为用户可处理提示。 */

  try {
    await navigator.clipboard.writeText(value);
    notify.success('已复制');
  } catch {
    notify.error('浏览器未允许复制，请手动选择文本');
  }
}

function connectionStatusDetail(profile: ConnectionProfileRead): string {
  /** 把稳定后端状态翻译为可执行的用户提示。 */

  if (profile.status === 'disabled') return '此连接已停用。所有 Agent 绑定都会在外呼前被拒绝；重新授权可恢复同一账号。';
  if (profile.status === 'reauth_required') return `凭据或授权范围已失效（${profile.health_error_code || '需要重新授权'}）。更新凭据后，等待中的任务可从原 Operation 恢复。`;
  if (profile.health_error_code === 'CONNECTION_TRUSTED_IP_REQUIRED') return '企业微信已验证凭据，但当前服务器出口 IP 尚未加入该应用的企业可信 IP。';
  if (profile.health_status === 'degraded') return profile.rate_limited_until ? `${providerLabel(profile.provider)}正在限流，系统将在 ${formatDateTime(profile.rate_limited_until)} 后通过持久信号重试。` : '连接暂时不可用，业务读取不会伪装成成功。';
  return `账号身份、密钥修订和 ${providerReadScope(profile.provider)} 授权已通过服务端验证。`;
}

function providerLabel(provider: ConnectionProvider): string {
  /** 返回用户熟悉的 provider 名称，避免界面暴露内部枚举。 */

  return provider === 'wecom' ? '企业微信' : 'Slack';
}

function providerReadScope(provider?: ConnectionProvider): string {
  /** 返回档案绑定时唯一允许的最小只读 scope。 */

  return provider === 'wecom' ? 'application:read' : 'channels:read';
}

export function connectionErrorMessage(error: unknown, fallback: string): string {
  /** 将稳定连接错误码转成不泄露 provider 正文的管理提示。 */

  if (error instanceof ApiError && error.status === 409) return '连接状态已变化，请刷新后重试';
  const code = error instanceof Error ? error.message : '';
  const messages: Record<string, string> = {
    CONNECTION_ACCOUNT_CHANGED: '新凭据属于另一个外部账号，不能替换当前连接',
    CONNECTION_SCOPE_MISSING: '外部应用缺少当前能力所需授权',
    CONNECTION_TOKEN_EXPIRED: '访问凭据已过期，请重新授权',
    CONNECTION_TOKEN_REVOKED: '访问凭据已撤销，请重新授权',
    CONNECTION_INVALID_AUTH: '外部应用凭据无效',
    CONNECTION_ACCOUNT_INVALID: '企业或应用身份无效，请核对配置',
    CONNECTION_TRUSTED_IP_REQUIRED: '请先把当前服务器出口 IP 加入企业微信应用的企业可信 IP',
    CONNECTION_ACCOUNT_ALREADY_EXISTS: '此外部账号已经建立连接档案',
    CONNECTION_BINDING_ALREADY_EXISTS: '此数字员工已经绑定该连接',
  };
  return messages[code] || code || fallback;
}

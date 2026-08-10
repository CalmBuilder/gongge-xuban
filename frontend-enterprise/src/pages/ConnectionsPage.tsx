/**
 * @Time       : 2026/08/10 20:45
 * @Author     : zhanglp8181
 * @File       : ConnectionsPage.tsx
 * @CallChain  : 企业连接管理 → connection API client → ConnectionProfile/Agent binding
 * @Description: 管理企业微信/Slack 多账号身份、最小 scope、Agent 绑定、健康与凭据轮换。
 */

import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import {
  Activity,
  Bot,
  Cable,
  CheckCircle2,
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
          <Button variant="outline" onClick={() => void load()} disabled={loading}>
            <RefreshCw className={cn('size-[14px]', loading && 'animate-spin')} />刷新
          </Button>
          <Button onClick={openCreate} className="bg-[var(--gg-cobalt)] text-white hover:bg-[#244bc7]">
            <Plus className="size-[14px]" />连接企业微信
          </Button>
        </div>
      </div>

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

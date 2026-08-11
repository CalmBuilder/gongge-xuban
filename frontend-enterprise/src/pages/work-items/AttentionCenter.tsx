/**
 * @Time       : 2026/08/10 21:15
 * @Author     : zhanglp8181
 * @File       : AttentionCenter.tsx
 * @CallChain  : 待我处理中心 → Attention/Connection/Execution API → typed 决定与持久恢复
 * @Description: 展示并专用办理动态澄清和连接重授权等统一 Attention。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { CircleHelp, Clock3, History, KeyRound, MessageSquareText, RefreshCw, Route, ShieldCheck } from 'lucide-react';

import { api, getRequestTenantId } from '@/api/client';
import {
  reauthorizeConnectionAttention,
  reauthorizeWeComConnectionAttention,
  startSlackOAuth,
} from '@/api/connections';
import { notify } from '@/components/ui/app-toast';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogTitle, Textarea } from '@/components/ui';
import { cn } from '@/lib/utils';
import { formatDateTime } from '@/lib/enterprise-ui';

type AttentionView = 'active' | 'resolved';

type AttentionItem = {
  id: string;
  execution_id: string;
  session_id: string;
  kind: string;
  title?: string;
  payload: Record<string, unknown>;
  available_commands: string[];
  resolution: Record<string, unknown>;
  status: string;
  revision: number;
  created_at: string;
  updated_at: string;
};

type AttentionPage = {
  items: AttentionItem[];
  total: number;
};

type ExecutionState = {
  id: string;
  status: string;
  revision: number;
  effect_state: string;
  goal?: string | null;
  current_step_key?: string | null;
  steps?: Array<{
    step_key: string;
    title: string;
    kind: string;
    status: string;
  }>;
  budget?: Record<string, unknown>;
  usage?: Record<string, unknown>;
  pending_attention_count?: number;
};

type ArtifactPreview = {
  content: string;
  truncated: boolean;
};

const VIEW_OPTIONS: Array<{ value: AttentionView; label: string; icon: typeof Clock3 }> = [
  { value: 'active', label: '需要我处理', icon: Clock3 },
  { value: 'resolved', label: '最近已处理', icon: History },
];

export default function AttentionCenter() {
  const [view, setView] = useState<AttentionView>('active');
  const [items, setItems] = useState<AttentionItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState<AttentionItem | null>(null);
  const [execution, setExecution] = useState<ExecutionState | null>(null);
  const [proposalReview, setProposalReview] = useState<ArtifactPreview | null>(null);
  const [answer, setAnswer] = useState('');
  const [reauthToken, setReauthToken] = useState('');
  const [reauthCorpId, setReauthCorpId] = useState('');
  const [reauthAgentId, setReauthAgentId] = useState('');
  const [reauthCorpSecret, setReauthCorpSecret] = useState('');
  const [acting, setActing] = useState(false);
  const requestId = useRef(0);

  const loadItems = useCallback(async (targetView: AttentionView, quiet = false) => {
    const currentRequest = ++requestId.current;
    if (!quiet) setLoading(true);
    try {
      const result = await api.get<AttentionPage>(
        `/api/attention-items?tenant_id=${getRequestTenantId()}&view=${targetView}&page=1&page_size=100`,
      );
      if (currentRequest !== requestId.current) return;
      const attentionItems = Array.isArray(result.items)
        ? result.items.filter(isAttentionItem)
        : [];
      setItems(attentionItems.filter((item) => item.kind !== 'sop_human_task'));
    } catch (error) {
      if (currentRequest === requestId.current && !quiet) {
        notify.error(error instanceof Error ? error.message : '加载待处理事项失败');
      }
    } finally {
      if (currentRequest === requestId.current && !quiet) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadItems(view);
    const timer = window.setInterval(() => void loadItems(view, true), 5_000);
    return () => {
      window.clearInterval(timer);
      requestId.current += 1;
    };
  }, [loadItems, view]);

  useEffect(() => {
    if (!selected) {
      setExecution(null);
      setProposalReview(null);
      return;
    }
    let active = true;
    void api.get<ExecutionState>(
      `/api/executions/${selected.execution_id}?tenant_id=${getRequestTenantId()}`,
    ).then((result) => {
      if (active) setExecution(result);
    }).catch(() => {
      if (active) setExecution(null);
    });
    const reviewArtifactId = selected.kind === 'publication'
      ? stringPayload(selected, 'review_artifact_id')
      : '';
    if (reviewArtifactId) {
      void api.get<ArtifactPreview>(
        `/api/artifacts/${reviewArtifactId}/preview?tenant_id=${getRequestTenantId()}`,
      ).then((result) => {
        if (active) setProposalReview(result);
      }).catch(() => {
        if (active) setProposalReview(null);
      });
    } else {
      setProposalReview(null);
    }
    return () => { active = false; };
  }, [selected]);

  const activeCount = useMemo(
    () => items.filter((item) => item.available_commands.length > 0).length,
    [items],
  );
  const selectedReauthProvider = selected?.kind === 'reauth'
    ? stringPayload(selected, 'provider') || 'slack'
    : null;

  async function resolve(command: 'answer' | 'cancel' | 'allow_once' | 'deny' | 'confirm_applied' | 'confirm_not_applied') {
    if (!selected) return;
    if (command === 'answer' && !answer.trim()) {
      notify.error('请先补充任务所需信息');
      return;
    }
    if (['confirm_applied', 'confirm_not_applied'].includes(command) && !answer.trim()) {
      notify.error('请填写用于人工对账的外部证据');
      return;
    }
    setActing(true);
    try {
      const payload: Record<string, unknown> = {
        tenant_id: getRequestTenantId(),
        command_id: crypto.randomUUID(),
        command,
        expected_revision: selected.revision,
      };
      if (answer.trim()) payload.comment = answer.trim();
      await api.post(`/api/attention-items/${selected.id}/resolve`, payload);
      const successCopy: Record<string, string> = {
        answer: '信息已补充，原任务将继续执行',
        cancel: '任务取消请求已提交',
        allow_once: '已一次性批准，系统将在派发前重新校验全部授权',
        deny: '已拒绝，冻结操作不会发送',
        confirm_applied: '已登记外部效果存在，原任务将继续',
        confirm_not_applied: '已登记外部效果未发生，任务将确定失败',
      };
      notify.success(successCopy[command]);
      setSelected(null);
      setAnswer('');
      setReauthToken('');
      setReauthCorpId('');
      setReauthAgentId('');
      setReauthCorpSecret('');
      await loadItems(view);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '处理待办事项失败');
      await loadItems(view);
    } finally {
      setActing(false);
    }
  }

  async function completeReauth() {
    /** 原子轮换 provider 凭据并决定 Attention，避免任务唤醒与密钥更新出现事务缝隙。 */

    if (!selected || selected.kind !== 'reauth') return;
    const profileId = stringPayload(selected, 'profile_id');
    const profileRevision = numberPayload(selected, 'profile_revision');
    if (!profileId || profileRevision === null) {
      notify.error('连接重授权事项缺少账号修订，请刷新后重试');
      return;
    }
    const provider = stringPayload(selected, 'provider') || 'slack';
    if (provider === 'slack' && !reauthToken.trim()) {
      notify.error('请填写新的 Slack Bot Token');
      return;
    }
    if (
      provider === 'wecom'
      && (!reauthCorpId.trim() || !reauthAgentId.trim() || !reauthCorpSecret.trim())
    ) {
      notify.error('请完整填写企业 ID、AgentId 和 Secret');
      return;
    }
    setActing(true);
    try {
      if (provider === 'wecom') {
        await reauthorizeWeComConnectionAttention({
          profileId,
          attentionId: selected.id,
          profileRevision,
          attentionRevision: selected.revision,
          corpId: reauthCorpId.trim(),
          agentId: reauthAgentId.trim(),
          corpSecret: reauthCorpSecret.trim(),
          commandId: crypto.randomUUID(),
        });
      } else {
        await reauthorizeConnectionAttention({
          profileId,
          attentionId: selected.id,
          profileRevision,
          attentionRevision: selected.revision,
          token: reauthToken.trim(),
          commandId: crypto.randomUUID(),
        });
      }
      setReauthToken('');
      setReauthCorpId('');
      setReauthAgentId('');
      setReauthCorpSecret('');
      setSelected(null);
      notify.success('连接已重新授权，原任务将从等待步骤继续');
      await loadItems(view);
    } catch (error) {
      setReauthToken('');
      setReauthCorpSecret('');
      notify.error(error instanceof Error ? error.message : '重新授权失败');
      await loadItems(view);
    } finally {
      setActing(false);
    }
  }

  async function beginReauthOAuth() {
    /** 为当前 reauth Attention 创建一次性 OAuth state，并把 callback 绑定原 Execution。 */

    if (!selected || selected.kind !== 'reauth') return;
    if (stringPayload(selected, 'provider') === 'wecom') return;
    const profileId = stringPayload(selected, 'profile_id');
    const profileRevision = numberPayload(selected, 'profile_revision');
    if (!profileId || profileRevision === null) {
      notify.error('连接重授权事项缺少账号修订，请刷新后重试');
      return;
    }
    setActing(true);
    try {
      const result = await startSlackOAuth({
        flowType: 'reauthorize_attention',
        profileId,
        attentionId: selected.id,
        expectedProfileRevision: profileRevision,
        expectedAttentionRevision: selected.revision,
      });
      window.location.assign(result.authorize_url);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '启动 Slack OAuth 失败');
      setActing(false);
    }
  }

  return (
    <section className="mt-[20px] overflow-hidden rounded-[18px] border border-[#dfe5f2] bg-white shadow-[0_12px_32px_rgba(40,63,120,0.06)]">
      <div className="flex items-center justify-between gap-[16px] border-b border-[#edf0f6] bg-[linear-gradient(100deg,#f7f9ff_0%,#ffffff_58%,#f2fbf7_100%)] px-[18px] py-[16px] max-[700px]:items-start">
        <div className="flex min-w-0 items-center gap-[12px]">
          <span className="relative grid size-[40px] shrink-0 place-items-center rounded-[12px] bg-[#3157e8] text-white shadow-[0_8px_18px_rgba(49,87,232,0.22)]">
            <MessageSquareText className="size-[18px]" />
            {activeCount > 0 ? <span className="absolute -right-[4px] -top-[4px] size-[9px] rounded-full border-2 border-white bg-[#ef8b2c]" /> : null}
          </span>
          <span className="min-w-0">
            <strong className="block text-[15px] font-semibold tracking-[-0.01em] text-[#18181a]">待我处理</strong>
            <span className="mt-[2px] block text-[12px] leading-[1.5] text-[#6f7892]">任务缺少信息或需要决定时，会在这里暂停；处理后从原执行记录继续。</span>
          </span>
        </div>
        <Button variant="ghost" size="sm" disabled={loading} onClick={() => void loadItems(view)} aria-label="刷新待处理事项">
          <RefreshCw className={cn('size-[14px]', loading && 'animate-spin')} />
          刷新
        </Button>
      </div>

      <div className="flex gap-[8px] px-[18px] pt-[14px]" role="tablist" aria-label="待处理事项视图">
        {VIEW_OPTIONS.map((option) => {
          const Icon = option.icon;
          return (
            <button
              key={option.value}
              type="button"
              role="tab"
              aria-selected={view === option.value}
              onClick={() => setView(option.value)}
              className={cn(
                'inline-flex h-[32px] items-center gap-[6px] rounded-[9px] px-[11px] text-[12px] transition-colors',
                view === option.value ? 'bg-[#eef1fb] font-semibold text-[#244bc7]' : 'text-[#757f9c] hover:bg-[#f6f7fa]',
              )}
            >
              <Icon className="size-[13px]" />
              {option.label}
            </button>
          );
        })}
      </div>

      <div className="grid gap-[8px] px-[18px] py-[14px]" aria-live="polite">
        {items.length === 0 ? (
          <div className="flex min-h-[76px] items-center justify-center rounded-[12px] border border-dashed border-[#dfe4ef] bg-[#fafbfc] px-[16px] text-[12px] text-[#858b9c]">
            {loading ? '正在加载待处理事项' : view === 'active' ? '当前没有需要你处理的事项' : '暂无最近处理记录'}
          </div>
        ) : items.map((item) => (
          <button
            key={item.id}
            type="button"
            onClick={() => { setSelected(item); setAnswer(''); setReauthToken(''); }}
            className="group grid grid-cols-[36px_minmax(0,1fr)_auto] items-center gap-[11px] rounded-[12px] border border-[#e7eaf2] px-[12px] py-[11px] text-left transition hover:border-[#bdc9f5] hover:bg-[#fafbff] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#3157e8]/35"
          >
            <span className={cn('grid size-[36px] place-items-center rounded-[10px]', ['reauth', 'tool_approval', 'publication'].includes(item.kind) ? 'bg-[#edf2ff] text-[#3157e8]' : 'bg-[#fff4e8] text-[#b55a09]')}>
              {item.kind === 'reauth' ? <KeyRound className="size-[17px]" /> : ['tool_approval', 'publication'].includes(item.kind) ? <ShieldCheck className="size-[17px]" /> : <CircleHelp className="size-[17px]" />}
            </span>
            <span className="min-w-0">
              <strong className="block truncate text-[13px] font-semibold text-[#252936]">{item.title || attentionKindLabel(item.kind)}</strong>
              <span className="mt-[2px] block truncate text-[12px] text-[#737c95]">{attentionQuestion(item)}</span>
            </span>
            <span className="grid justify-items-end gap-[3px] text-[11px] text-[#8a91a4]">
              <span className={cn('rounded-full px-[8px] py-[2px]', item.status === 'completed' ? 'bg-[#eaf8ef] text-[#018434]' : 'bg-[#fff5df] text-[#936000]')}>
                {item.status === 'completed' ? '已处理' : '等待处理'}
              </span>
              <span>{formatDateTime(item.updated_at)}</span>
            </span>
          </button>
        ))}
      </div>

      <Dialog open={Boolean(selected)} onOpenChange={(open) => {
        if (!open) {
          setSelected(null);
          setAnswer('');
          setReauthToken('');
        }
      }}>
        <DialogContent aria-describedby={undefined} className="max-h-[calc(100vh-32px)] gap-[16px] overflow-y-auto rounded-[14px] sm:max-w-[560px]">
          {selected ? (
            <>
              <div>
                <DialogTitle className="text-[15px] font-semibold text-[#18181a]">{selected.title || attentionKindLabel(selected.kind)}</DialogTitle>
                <div className="mt-[5px] flex flex-wrap items-center gap-[7px] text-[11px] text-[#858b9c]">
                  <span className="inline-flex items-center gap-[4px]"><Route className="size-[12px]" />执行 {selected.execution_id}</span>
                  {execution ? <span>状态 {execution.status} · 修订 {execution.revision}</span> : null}
                </div>
              </div>
              <div className="rounded-[12px] border border-[#e5e9f3] bg-[#f8faff] px-[13px] py-[12px] text-[13px] leading-[1.65] text-[#313747]">
                {attentionQuestion(selected)}
              </div>
              {selected.kind === 'tool_approval' ? (
                <div className="grid gap-[9px] rounded-[12px] border border-[#dce5ff] bg-[#fbfcff] px-[13px] py-[12px]" aria-label={isWorkspaceApproval(selected) ? '待批准受管代码操作' : '待批准外部写'}>
                  {isWorkspaceApproval(selected) ? (
                    <>
                      <div className="flex flex-wrap items-center justify-between gap-[10px] text-[11px] text-[#68738d]">
                        <span>受管工作区：{workspaceField(selected, 'workspace_id') || '未标识'} · 基线 {workspaceField(selected, 'base_ref') || '未标识'}</span>
                        <code className="font-mono">{workspaceField(selected, 'handler')}</code>
                      </div>
                      <pre className="max-h-[260px] overflow-auto whitespace-pre-wrap break-words rounded-[9px] bg-white p-[10px] text-[12px] leading-[1.65] text-[#283044]">{JSON.stringify(selected.payload.arguments || {}, null, 2)}</pre>
                      <p className="text-[11px] leading-[1.6] text-[#6a7388]">批准只绑定本次 Execution、Operation、参数、工作区与能力修订；路径、内容或工具授权变化后不会沿用。</p>
                    </>
                  ) : (
                    <>
                      <div className="flex items-center justify-between gap-[10px] text-[11px] text-[#68738d]">
                        <span>目标：当前企业微信会话</span>
                        <code className="font-mono">{stringPayload(selected, 'content_checksum').slice(0, 12)}</code>
                      </div>
                      <pre className="max-h-[220px] overflow-auto whitespace-pre-wrap break-words rounded-[9px] bg-white p-[10px] text-[12px] leading-[1.65] text-[#283044]">{stringPayload(selected, 'content')}</pre>
                      <p className="text-[11px] leading-[1.6] text-[#6a7388]">批准只绑定本次 Operation、正文、目标、能力与连接修订；任何变化都会重新进入待处理。</p>
                    </>
                  )}
                </div>
              ) : null}
              {selected.kind === 'publication' ? (
                <div className="grid gap-[10px] rounded-[12px] border border-[#dce5ff] bg-[#fbfcff] px-[13px] py-[12px]" aria-label="待审核 Skill 提案">
                  <div className="grid gap-[5px] text-[12px] text-[#4f5870] sm:grid-cols-2">
                    <span>Skill：<strong className="text-[#252b3b]">{stringPayload(selected, 'name')}</strong></span>
                    <span>调用策略：<code>user_only</code></span>
                    <span className="sm:col-span-2">说明：{stringPayload(selected, 'description')}</span>
                    <span className="sm:col-span-2">请求工具：{stringArrayPayload(selected, 'requested_tools').join('、') || '无（不会获得新工具授权）'}</span>
                  </div>
                  <div className="rounded-[9px] border border-[#e5e9f3] bg-white p-[10px]">
                    <div className="mb-[7px] flex items-center justify-between text-[11px] text-[#70798f]">
                      <span>完整 diff、权限与 Artifact 来源</span>
                      <code>{stringPayload(selected, 'content_checksum').slice(0, 12)}</code>
                    </div>
                    <pre className="max-h-[360px] overflow-auto whitespace-pre-wrap break-words text-[12px] leading-[1.65] text-[#283044]">{proposalReview?.content || '正在加载审核 Artifact…'}</pre>
                  </div>
                  <p className="text-[11px] leading-[1.6] text-[#6a7388]">批准后才会发布不可变修订，并以仅用户显式调用的方式绑定当前分身；拒绝、过期或基线变化都不会进入 Skill 目录。</p>
                </div>
              ) : null}
              {execution?.goal ? (
                <div className="grid gap-[8px] rounded-[12px] border border-[#e7eaf2] bg-white px-[13px] py-[11px]" aria-label="执行计划">
                  <div className="flex items-center justify-between gap-[12px]">
                    <span className="min-w-0 truncate text-[12px] font-medium text-[#343949]">{execution.goal}</span>
                    <span className="shrink-0 text-[11px] text-[#7b849c]">{budgetSummary(execution)}</span>
                  </div>
                  {Array.isArray(execution.steps) && execution.steps.length > 0 ? (
                    <ol className="grid gap-[5px]">
                      {execution.steps.map((step, index) => (
                        <li key={step.step_key} className="grid grid-cols-[18px_minmax(0,1fr)_auto] items-center gap-[7px] text-[11px]">
                          <span className={cn('grid size-[18px] place-items-center rounded-full font-semibold', ['completed', 'succeeded'].includes(step.status) ? 'bg-[#e6f6ec] text-[#087a38]' : step.status === 'waiting' || step.status === 'running' ? 'bg-[#fff0d8] text-[#996000]' : 'bg-[#eef1f6] text-[#778097]')}>{index + 1}</span>
                          <span className="truncate text-[#4f576d]">{step.title}</span>
                          <span className="text-[#8991a5]">{stepStatusLabel(step.status)}</span>
                        </li>
                      ))}
                    </ol>
                  ) : null}
                </div>
              ) : null}
              {attentionOptions(selected).length > 0 && selected.status !== 'completed' && selected.kind !== 'reauth' ? (
                <div className="flex flex-wrap gap-[7px]" aria-label="可选答案">
                  {attentionOptions(selected).map((option) => (
                    <button key={option} type="button" onClick={() => setAnswer(option)} className={cn('rounded-full border px-[10px] py-[5px] text-[12px]', answer === option ? 'border-[#3157e8] bg-[#eef1fb] text-[#244bc7]' : 'border-[#dfe4ef] text-[#5f6880] hover:border-[#aebcf0]')}>
                      {option}
                    </button>
                  ))}
                </div>
              ) : null}
              {selected.status === 'completed' ? (
                <div className="rounded-[11px] bg-[#f2f8f4] px-[12px] py-[10px] text-[12px] text-[#25623c]">处理结果：{String(selected.resolution.comment || selected.resolution.command || '已完成')}</div>
              ) : selected.kind === 'reauth' ? (
                <div className="grid gap-[10px]">
                  {selectedReauthProvider === 'wecom' ? (
                    <div className="grid gap-[9px]">
                      <ReauthField label="企业 ID（CorpID）" value={reauthCorpId} onChange={setReauthCorpId} autoFocus />
                      <ReauthField label="应用 AgentId" value={reauthAgentId} onChange={setReauthAgentId} />
                      <ReauthField label="应用 Secret" value={reauthCorpSecret} onChange={setReauthCorpSecret} secret />
                    </div>
                  ) : (
                    <ReauthField label="新的 Slack Bot Token" value={reauthToken} onChange={setReauthToken} secret autoFocus placeholder="xoxb-…" />
                  )}
                  <div className="rounded-[11px] border border-[#dce5ff] bg-[#f7f9ff] px-[12px] py-[10px] text-[11px] leading-[1.6] text-[#52617f]">
                    {selectedReauthProvider === 'wecom' ? '新凭据必须属于同一企业微信自建应用。验证成功后，系统通过持久信号恢复原 Operation。' : '新凭据必须属于同一 Slack 工作区并包含 channels:read。验证成功后，系统通过持久信号恢复原 Operation。'}
                  </div>
                </div>
              ) : ['tool_approval', 'publication'].includes(selected.kind) ? null : (
                <label className="grid gap-[6px]">
                  <span className="text-[12px] font-medium text-[#464c5e]">{selected.kind === 'exception' ? '外部对账证据（必填）' : '补充信息'}</span>
                  <Textarea value={answer} onChange={(event) => setAnswer(event.target.value)} rows={4} placeholder={selected.kind === 'exception' ? '填写后台记录、客户端核对结果或工单证据；系统不会自动重发' : '填写任务继续执行所需的准确信息'} />
                </label>
              )}
              <div className="flex flex-wrap justify-end gap-[8px]">
                <Button variant="outline" disabled={acting} onClick={() => setSelected(null)}>关闭</Button>
                {selected.available_commands.includes('cancel') ? <Button variant="outline" disabled={acting} onClick={() => void resolve('cancel')}>取消任务</Button> : null}
                {selected.available_commands.includes('answer') ? <Button disabled={acting} onClick={() => void resolve('answer')} className="bg-[#3157e8] text-white hover:bg-[#244bc7]">补充并继续</Button> : null}
                {selected.available_commands.includes('deny') ? <Button variant="outline" disabled={acting} onClick={() => void resolve('deny')}>{selected.kind === 'publication' ? '拒绝提案' : isWorkspaceApproval(selected) ? '拒绝操作' : '拒绝发送'}</Button> : null}
                {selected.available_commands.includes('allow_once') ? <Button disabled={acting} onClick={() => void resolve('allow_once')} className="bg-[#3157e8] text-white hover:bg-[#244bc7]">{selected.kind === 'publication' ? '批准并发布' : isWorkspaceApproval(selected) ? '仅批准本次操作' : '仅批准本次发送'}</Button> : null}
                {selected.available_commands.includes('confirm_not_applied') ? <Button variant="outline" disabled={acting} onClick={() => void resolve('confirm_not_applied')}>确认未送达</Button> : null}
                {selected.available_commands.includes('confirm_applied') ? <Button disabled={acting} onClick={() => void resolve('confirm_applied')} className="bg-[#3157e8] text-white hover:bg-[#244bc7]">确认已送达</Button> : null}
                {selected.available_commands.includes('reauthorize') ? <Button disabled={acting} onClick={() => void completeReauth()} className="bg-[#3157e8] text-white hover:bg-[#244bc7]">验证并恢复任务</Button> : null}
                {selected.available_commands.includes('reauthorize') && selectedReauthProvider === 'slack' ? <Button variant="outline" disabled={acting} onClick={() => void beginReauthOAuth()}>通过 Slack OAuth</Button> : null}
              </div>
            </>
          ) : null}
        </DialogContent>
      </Dialog>
    </section>
  );
}

function ReauthField({
  label,
  value,
  onChange,
  secret = false,
  autoFocus = false,
  placeholder,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  secret?: boolean;
  autoFocus?: boolean;
  placeholder?: string;
}) {
  /** 渲染不会被浏览器自动填充的连接重授权字段。 */

  return (
    <label className="grid gap-[6px]">
      <span className="text-[12px] font-medium text-[#464c5e]">{label}</span>
      <input
        autoFocus={autoFocus}
        type={secret ? 'password' : 'text'}
        autoComplete={secret ? 'new-password' : 'off'}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        className="h-[38px] rounded-[10px] border border-[#dfe4ef] bg-white px-[11px] text-[13px] outline-none transition focus:border-[#3157e8] focus:ring-2 focus:ring-[#3157e8]/15"
      />
    </label>
  );
}

function attentionQuestion(item: AttentionItem): string {
  if (item.kind === 'reauth') {
    const accountId = stringPayload(item, 'account_id');
    const reasonCode = stringPayload(item, 'reason_code');
    const provider = stringPayload(item, 'provider') === 'wecom' ? '企业微信应用' : 'Slack 账号';
    return `${provider} ${accountId || '未知'} 需要重新授权${reasonCode ? `（${reasonCode}）` : ''}`;
  }
  if (item.kind === 'tool_approval') return isWorkspaceApproval(item)
    ? '请核对受管工作区、固定动作和精确参数，再决定是否批准本次代码操作。'
    : '请核对下方精确正文，并决定是否仅批准本次企业微信发送。';
  if (item.kind === 'publication') return '请核对完整 Skill diff、请求权限和受管 Artifact 来源，再决定是否发布到当前分身。';
  if (item.kind === 'exception') return stringPayload(item, 'instruction') || '外部效果不确定，请依据独立证据人工对账。';
  return typeof item.payload.question === 'string' && item.payload.question.trim()
    ? item.payload.question
    : '打开查看需要处理的内容';
}

function attentionOptions(item: AttentionItem): string[] {
  return Array.isArray(item.payload.options)
    ? item.payload.options.filter((value): value is string => typeof value === 'string' && Boolean(value.trim()))
    : [];
}

function attentionKindLabel(kind: string): string {
  return ({ clarification: '补充任务信息', tool_approval: '批准外部写操作', reauth: '重新授权外部连接', exception: '核对外部效果', publication: '审核 Skill 提案', result_review: '复核执行结果' } as Record<string, string>)[kind] || '处理任务事项';
}

function stringPayload(item: AttentionItem, key: string): string {
  /** 从不可信 Attention payload 中读取非空字符串。 */

  const value = item.payload[key];
  return typeof value === 'string' ? value.trim() : '';
}

function stringArrayPayload(item: AttentionItem, key: string): string[] {
  /** 从 Attention payload 提取去空白字符串数组，拒绝对象和隐式字符串化。 */

  const value = item.payload[key];
  return Array.isArray(value)
    ? value.filter((entry): entry is string => typeof entry === 'string' && Boolean(entry.trim()))
    : [];
}

function isWorkspaceApproval(item: AttentionItem): boolean {
  /** 只根据服务端冻结的 workspace 对象切换展示，不从标题或操作名猜测风险类别。 */

  return Boolean(item.payload.workspace && typeof item.payload.workspace === 'object' && !Array.isArray(item.payload.workspace));
}

function workspaceField(item: AttentionItem, key: string): string {
  /** 从受管工作区审批投影读取非敏感稳定字段。 */

  const workspace = item.payload.workspace;
  if (!workspace || typeof workspace !== 'object' || Array.isArray(workspace)) return '';
  const value = (workspace as Record<string, unknown>)[key];
  return typeof value === 'string' ? value.trim() : '';
}

function numberPayload(item: AttentionItem, key: string): number | null {
  /** 从不可信 Attention payload 中读取有限整数修订号。 */

  const value = item.payload[key];
  return typeof value === 'number' && Number.isInteger(value) ? value : null;
}

function isAttentionItem(value: unknown): value is AttentionItem {
  if (!value || typeof value !== 'object') return false;
  const item = value as Partial<AttentionItem>;
  return typeof item.id === 'string'
    && typeof item.execution_id === 'string'
    && typeof item.session_id === 'string'
    && typeof item.kind === 'string'
    && typeof item.status === 'string'
    && typeof item.revision === 'number'
    && Array.isArray(item.available_commands)
    && Boolean(item.payload && typeof item.payload === 'object')
    && Boolean(item.resolution && typeof item.resolution === 'object');
}

function budgetSummary(execution: ExecutionState): string {
  const modelCalls = Number(execution.usage?.model_calls || 0);
  const maxModelCalls = Number(execution.budget?.max_model_calls || 0);
  return maxModelCalls > 0 ? `模型调用 ${modelCalls}/${maxModelCalls}` : `修订 ${execution.revision}`;
}

function stepStatusLabel(status: string): string {
  return ({ completed: '已完成', succeeded: '已完成', running: '执行中', waiting: '等待处理', failed: '失败', scheduled: '待执行', pending: '待执行' } as Record<string, string>)[status] || status;
}

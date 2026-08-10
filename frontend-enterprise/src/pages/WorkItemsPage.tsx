/**
 * @Time       : 2026/07/22 17:43
 * @Author     : zhanglp8181
 * @File       : WorkItemsPage.tsx
 * @CallChain  : 企业端任务箱 → 工作项动作 API → 结果反馈与任务列表刷新
 * @Description: 展示流程人工任务，并按服务端结果契约执行认领和领域办理动作。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { CheckCircle2, Clock3, GitPullRequestArrow, UserCheck, UsersRound } from 'lucide-react';

import AppHeader from '@/components/AppHeader';
import { DataTable, type DataTableColumn } from '@/components/DataTable';
import { Paginator } from '@/components/Paginator';
import { Dialog, DialogContent, DialogTitle, Textarea } from '@/components/ui';
import { notify } from '@/components/ui/app-toast';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import { formatDateTime } from '@/lib/enterprise-ui';

import { api, getRequestTenantId } from '../api/client';
import type { EnterpriseAuthUser } from '../auth';
import AttentionCenter from './work-items/AttentionCenter';

type InboxView = 'pending' | 'claimed' | 'completed';

type WorkItemCandidate = {
  user_id: string;
  employee_profile_id?: string;
  source_role_codes: string[];
  source_types: string[];
};

type WorkItemDecision = {
  actor_user_id: string;
  outcome: string;
  comment?: string;
  created_at: string;
};

type WorkItemOutcomeOption = {
  value: string;
  label: string;
  tone: 'primary' | 'success' | 'danger' | 'neutral';
  comment_required: boolean;
};

type WorkItem = {
  id: string;
  instance_id: string;
  session_id: string;
  skill_id: string;
  skill_version: string;
  node_id: string;
  status: string;
  initiator_user_id?: string;
  assignee_user_id?: string;
  completion_mode: string;
  claim_required: boolean;
  required_count?: number;
  allowed_outcomes: string[];
  outcome_options: WorkItemOutcomeOption[];
  allowed_actions: string[];
  outcome?: string;
  comment?: string;
  revision: number;
  candidate_count: number;
  decision_count: number;
  candidates: WorkItemCandidate[];
  decisions: WorkItemDecision[];
  expires_at?: string;
  created_at: string;
  updated_at: string;
};

type WorkItemPage = {
  items: WorkItem[];
  total: number;
  page: number;
  page_size: number;
};

const WORK_ITEM_PAGE_SIZE = 20;

const VIEW_OPTIONS: Array<{ value: InboxView; label: string; icon: typeof Clock3 }> = [
  { value: 'pending', label: '可认领', icon: Clock3 },
  { value: 'claimed', label: '待我处理', icon: UserCheck },
  { value: 'completed', label: '已办', icon: CheckCircle2 },
];

export default function WorkItemsPage({
  currentUser,
  onLogout,
}: {
  currentUser?: EnterpriseAuthUser;
  onLogout?: () => void;
}) {
  const [view, setView] = useState<InboxView>('pending');
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [items, setItems] = useState<WorkItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState<WorkItem | null>(null);
  const [comment, setComment] = useState('');
  const [acting, setActing] = useState(false);
  const loadRequestId = useRef(0);

  const loadItems = useCallback(async (targetView: InboxView, targetPage: number) => {
    const requestId = ++loadRequestId.current;
    setLoading(true);
    try {
      const result = await api.get<WorkItemPage>(
        `/api/work-items/page?tenant_id=${getRequestTenantId()}&view=${targetView}`
        + `&page=${targetPage}&page_size=${WORK_ITEM_PAGE_SIZE}`,
      );
      if (requestId !== loadRequestId.current) return;
      setItems(result.items);
      setTotal(result.total);
      setSelected((current) => (
        current ? result.items.find((item) => item.id === current.id) || null : null
      ));
    } catch (error) {
      if (requestId !== loadRequestId.current) return;
      notify.error(error instanceof Error ? error.message : '加载任务箱失败');
    } finally {
      if (requestId === loadRequestId.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadItems(view, page);
    return () => {
      loadRequestId.current += 1;
    };
  }, [loadItems, page, view]);

  const pageCount = Math.max(1, Math.ceil(total / WORK_ITEM_PAGE_SIZE));

  useEffect(() => {
    if (page > pageCount) setPage(pageCount);
  }, [page, pageCount]);

  const actionableCount = useMemo(
    () => items.filter((item) => item.allowed_actions.length > 0).length,
    [items],
  );

  async function runAction(action: string) {
    if (!selected) return;
    const outcomeOption = selected.outcome_options.find((option) => option.value === action);
    if (outcomeOption?.comment_required && !comment.trim()) {
      notify.error('请先填写处理说明');
      return;
    }
    setActing(true);
    const commandId = crypto.randomUUID();
    try {
      const isOutcome = Boolean(outcomeOption);
      const path = isOutcome
        ? `/api/work-items/${selected.id}/complete`
        : `/api/work-items/${selected.id}/${action}`;
      const updated = await api.post<WorkItem>(path, {
        tenant_id: getRequestTenantId(),
        command_id: commandId,
        expected_revision: selected.revision,
        ...(isOutcome
          ? { outcome: action, comment: comment.trim() || undefined }
          : {}),
      });
      setSelected(updated);
      setComment('');
      notify.success(actionLabel(action, true, outcomeOption?.label));
      await loadItems(view, page);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : actionLabel(action, false));
      await loadItems(view, page);
    } finally {
      setActing(false);
    }
  }

  const columns: DataTableColumn<WorkItem>[] = [
    {
      key: 'flow',
      title: 'SOP / 当前节点',
      width: 280,
      render: (item) => (
        <button type="button" onClick={() => setSelected(item)} className="grid min-w-0 text-left">
          <strong className="truncate text-[13px] font-semibold text-[#18181a]">{item.skill_id}</strong>
          <span className="truncate text-[11px] text-[#858b9c]">{item.node_id} · {item.skill_version}</span>
        </button>
      ),
    },
    {
      key: 'status',
      title: '任务状态',
      width: 120,
      render: (item) => <StatusPill status={item.status} outcome={item.outcome} options={item.outcome_options} />,
    },
    {
      key: 'initiator',
      title: '申请人',
      width: 150,
      render: (item) => item.initiator_user_id || '系统发起',
    },
    {
      key: 'progress',
      title: '处理进度',
      width: 160,
      render: (item) => `${item.decision_count} / ${completionTarget(item)} 人`,
    },
    {
      key: 'assignee',
      title: '实际处理人',
      width: 150,
      render: (item) => item.assignee_user_id || '尚未认领',
    },
    {
      key: 'created',
      title: '创建时间',
      width: 180,
      render: (item) => formatDateTime(item.created_at),
    },
    {
      key: 'actions',
      title: '操作',
      width: 100,
      align: 'right',
      render: (item) => (
        <Button variant="outline" size="sm" onClick={() => setSelected(item)}>查看</Button>
      ),
    },
  ];

  return (
    <div className="min-h-full box-border px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]" aria-busy={loading}>
      <AppHeader onLogout={onLogout} userName={currentUser?.username} title="待我处理中心" />

      <AttentionCenter />

      <section className="mt-[16px] flex items-center justify-between gap-[16px] rounded-[16px] border border-[#dfe5f2] bg-white px-[18px] py-[15px] max-[700px]:items-start">
        <div className="flex min-w-0 items-center gap-[12px]">
          <span className="grid size-[38px] shrink-0 place-items-center rounded-[11px] bg-[#eef1fb] text-[#3157e8]">
            <GitPullRequestArrow className="size-[18px]" />
          </span>
          <span className="min-w-0">
            <strong className="block text-[14px] font-semibold text-[#18181a]">SOP 流程任务</strong>
            <span className="mt-[2px] block text-[12px] leading-[1.5] text-[#858b9c]">平台管理员不会自动进入候选池；角色变更不改写已经创建的历史任务。</span>
          </span>
        </div>
        <span className="shrink-0 rounded-full bg-[#eaf8ef] px-[11px] py-[5px] text-[12px] font-medium text-[#018434]">
          本页 {actionableCount} 项可处理
        </span>
      </section>

      <div className="mt-[16px] flex gap-[8px]" role="tablist" aria-label="任务箱视图">
        {VIEW_OPTIONS.map((option) => {
          const Icon = option.icon;
          return (
            <button
              key={option.value}
              type="button"
              role="tab"
              aria-selected={view === option.value}
              onClick={() => {
                setPage(1);
                setView(option.value);
              }}
              className={cn(
                'inline-flex h-[34px] items-center gap-[6px] rounded-[10px] border px-[13px] text-[12px] transition-colors',
                view === option.value
                  ? 'border-[#3157e8] bg-[#eef1fb] font-semibold text-[#244bc7]'
                  : 'border-[#e3e7f1] bg-white text-[#757f9c] hover:text-[#18181a]',
              )}
            >
              <Icon className="size-[14px]" />
              {option.label}
            </button>
          );
        })}
      </div>

      <section className="mt-[12px] rounded-[18px] bg-white p-[18px] shadow-[0_-4px_16px_rgba(0,0,0,0.04)]">
        <DataTable
          aria-label="流程任务列表"
          columns={columns}
          data={items}
          rowKey={(item) => item.id}
          loading={loading}
          emptyText={view === 'completed'
            ? '暂无已办任务'
            : view === 'claimed'
              ? '当前没有待我处理任务'
              : '当前没有可认领任务'}
        />
        {total > WORK_ITEM_PAGE_SIZE ? (
          <Paginator
            aria-label="流程任务分页"
            page={page}
            pageCount={pageCount}
            onChange={setPage}
          />
        ) : null}
      </section>

      <Dialog open={Boolean(selected)} onOpenChange={(open) => !open && setSelected(null)}>
        <DialogContent aria-describedby={undefined} className="max-h-[calc(100vh-2rem)] overflow-y-auto gap-[16px] rounded-[14px] sm:max-w-[620px]">
          {selected ? (
            <>
              <div className="flex items-start justify-between gap-[12px]">
                <div>
                  <DialogTitle className="text-[15px] font-semibold text-[#18181a]">{selected.skill_id}</DialogTitle>
                  <p className="mt-[4px] font-mono text-[11px] text-[#858b9c]">{selected.node_id} · {selected.id}</p>
                </div>
                <StatusPill status={selected.status} outcome={selected.outcome} options={selected.outcome_options} />
              </div>

              <div className="grid grid-cols-2 gap-[10px] max-[520px]:grid-cols-1">
                <Detail label="申请人" value={selected.initiator_user_id || '系统发起'} />
                <Detail label="实际处理人" value={selected.assignee_user_id || '尚未认领'} />
                <Detail label="完成规则" value={completionModeLabel(selected.completion_mode)} />
                <Detail label="处理进度" value={`${selected.decision_count} / ${completionTarget(selected)} 人`} />
              </div>

              <section className="rounded-[12px] border border-[#e8ebf2] bg-[#fafbfc] p-[12px]">
                <div className="mb-[9px] flex items-center gap-[6px] text-[12px] font-semibold text-[#464c5e]">
                  <UsersRound className="size-[14px]" />候选来源快照
                </div>
                <div className="grid gap-[7px]">
                  {selected.candidates.map((candidate) => (
                    <div key={candidate.user_id} className="flex items-center justify-between gap-[10px] rounded-[9px] bg-white px-[10px] py-[8px] text-[12px]">
                      <span className="font-medium text-[#18181a]">{candidate.user_id}</span>
                      <span className="truncate text-[#858b9c]">{candidate.source_role_codes.join('、') || '直接指定'}</span>
                    </div>
                  ))}
                </div>
              </section>

              {selected.decisions.length ? (
                <section className="grid gap-[7px]">
                  <h3 className="text-[12px] font-semibold text-[#464c5e]">处理记录</h3>
                  {selected.decisions.map((decision) => (
                    <div key={`${decision.actor_user_id}-${decision.created_at}`} className="rounded-[10px] border border-[#e8ebf2] px-[11px] py-[9px] text-[12px]">
                      <div className="flex justify-between gap-[10px]">
                        <strong className="text-[#18181a]">{decision.actor_user_id} · {outcomeLabel(decision.outcome, selected.outcome_options)}</strong>
                        <span className="text-[#858b9c]">{formatDateTime(decision.created_at)}</span>
                      </div>
                      {decision.comment ? <p className="mt-[5px] text-[#464c5e]">{decision.comment}</p> : null}
                    </div>
                  ))}
                </section>
              ) : null}

              {selected.outcome_options.some((option) => selected.allowed_actions.includes(option.value)) ? (
                <label className="grid gap-[5px]">
                  <span className="text-[12px] font-medium text-[#464c5e]">处理说明</span>
                  <Textarea value={comment} onChange={(event) => setComment(event.target.value)} placeholder={selected.outcome_options.some((option) => option.comment_required && selected.allowed_actions.includes(option.value)) ? '请填写本次处理结果和依据' : '填写本次处理说明，选填'} rows={3} />
                </label>
              ) : null}

              <div className="flex flex-wrap justify-end gap-[8px]">
                <Button variant="outline" disabled={acting} onClick={() => setSelected(null)}>关闭</Button>
                {selected.allowed_actions.includes('unclaim') ? <Button variant="outline" disabled={acting} onClick={() => void runAction('unclaim')}>释放任务</Button> : null}
                {selected.allowed_actions.includes('claim') ? <Button disabled={acting} onClick={() => void runAction('claim')} className="bg-[var(--gg-cobalt)] text-white hover:bg-[#244bc7]">认领任务</Button> : null}
                {selected.outcome_options.filter((option) => selected.allowed_actions.includes(option.value)).map((option) => (
                  <Button
                    key={option.value}
                    variant={option.tone === 'danger' ? 'destructive' : option.tone === 'neutral' ? 'outline' : 'default'}
                    disabled={acting}
                    onClick={() => void runAction(option.value)}
                    className={option.tone === 'success' ? 'bg-[#018434] text-white hover:bg-[#016c2b]' : undefined}
                  >
                    {option.label}
                  </Button>
                ))}
              </div>
            </>
          ) : null}
        </DialogContent>
      </Dialog>
    </div>
  );
}

function StatusPill({ status, outcome, options }: { status: string; outcome?: string; options: WorkItemOutcomeOption[] }) {
  const completed = status === 'completed';
  const rejected = outcome === 'rejected';
  return (
    <span className={cn(
      'inline-flex w-fit items-center rounded-full px-[9px] py-[3px] text-[11px] font-medium',
      rejected
        ? 'bg-[#fce7e7] text-[#b40a0a]'
        : completed
          ? 'bg-[#eaf8ef] text-[#018434]'
          : status === 'claimed'
            ? 'bg-[#eef1fb] text-[#3157e8]'
            : 'bg-[#fff5df] text-[#936000]',
    )}>
      {outcome ? outcomeLabel(outcome, options) : status === 'claimed' ? '已认领' : '待认领'}
    </span>
  );
}

function Detail({ label, value }: { label: string; value: string }) {
  return (
    <div className="grid gap-[3px] rounded-[10px] bg-[#fafbfc] px-[11px] py-[9px]">
      <span className="text-[11px] text-[#858b9c]">{label}</span>
      <strong className="text-[12px] font-medium text-[#18181a]">{value}</strong>
    </div>
  );
}

function completionTarget(item: WorkItem): number {
  if (item.completion_mode === 'quorum') return item.required_count || item.candidate_count;
  if (item.completion_mode === 'all') return item.candidate_count;
  return 1;
}

function completionModeLabel(mode: string): string {
  return ({ single: '指定单人', any: '候选人或签', all: '全员会签', quorum: '人数门槛' } as Record<string, string>)[mode] || mode;
}

function outcomeLabel(outcome: string, options: WorkItemOutcomeOption[]): string {
  return options.find((option) => option.value === outcome)?.label
    || ({ approved: '已同意', rejected: '已拒绝' } as Record<string, string>)[outcome]
    || outcome;
}

/** 根据动作契约生成反馈；未知领域结果也不得把成功响应误报为失败。 */
export function actionLabel(action: string, success: boolean, outcomeOptionLabel?: string): string {
  const labels: Record<string, [string, string]> = {
    claim: ['任务已认领', '认领任务失败'],
    unclaim: ['任务已释放', '释放任务失败'],
    approved: ['已提交同意决定', '提交同意决定失败'],
    rejected: ['已提交拒绝决定', '提交拒绝决定失败'],
    resolved: ['已提交解决结果', '提交解决结果失败'],
    escalated: ['已提交升级处理', '提交升级处理失败'],
  };
  const predefinedLabel = labels[action]?.[success ? 0 : 1];
  if (predefinedLabel) return predefinedLabel;
  if (outcomeOptionLabel) return `${outcomeOptionLabel}${success ? '成功' : '失败'}`;
  return success ? '工作项操作成功' : '工作项操作失败';
}

/**
 * @Time       : 2026/08/04 06:32
 * @Author     : zhanglp8181
 * @File       : AttentionCenter.tsx
 * @CallChain  : 待我处理中心 → Attention API/Execution API → clarification 决定与持久恢复
 * @Description: 在保留 SOP 任务箱的同时展示并办理动态澄清等统一 Attention。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { CircleHelp, Clock3, History, MessageSquareText, RefreshCw, Route } from 'lucide-react';

import { api, getRequestTenantId } from '@/api/client';
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
  const [answer, setAnswer] = useState('');
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
    return () => { active = false; };
  }, [selected]);

  const activeCount = useMemo(
    () => items.filter((item) => item.available_commands.length > 0).length,
    [items],
  );

  async function resolve(command: 'answer' | 'cancel') {
    if (!selected) return;
    if (command === 'answer' && !answer.trim()) {
      notify.error('请先补充任务所需信息');
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
      notify.success(command === 'answer' ? '信息已补充，原任务将继续执行' : '任务取消请求已提交');
      setSelected(null);
      setAnswer('');
      await loadItems(view);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '处理待办事项失败');
      await loadItems(view);
    } finally {
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
            onClick={() => { setSelected(item); setAnswer(''); }}
            className="group grid grid-cols-[36px_minmax(0,1fr)_auto] items-center gap-[11px] rounded-[12px] border border-[#e7eaf2] px-[12px] py-[11px] text-left transition hover:border-[#bdc9f5] hover:bg-[#fafbff] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#3157e8]/35"
          >
            <span className="grid size-[36px] place-items-center rounded-[10px] bg-[#fff4e8] text-[#b55a09]"><CircleHelp className="size-[17px]" /></span>
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

      <Dialog open={Boolean(selected)} onOpenChange={(open) => !open && setSelected(null)}>
        <DialogContent aria-describedby={undefined} className="gap-[16px] rounded-[14px] sm:max-w-[560px]">
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
                          <span className={cn('grid size-[18px] place-items-center rounded-full font-semibold', step.status === 'completed' ? 'bg-[#e6f6ec] text-[#087a38]' : step.status === 'waiting' || step.status === 'running' ? 'bg-[#fff0d8] text-[#996000]' : 'bg-[#eef1f6] text-[#778097]')}>{index + 1}</span>
                          <span className="truncate text-[#4f576d]">{step.title}</span>
                          <span className="text-[#8991a5]">{stepStatusLabel(step.status)}</span>
                        </li>
                      ))}
                    </ol>
                  ) : null}
                </div>
              ) : null}
              {attentionOptions(selected).length > 0 && selected.status !== 'completed' ? (
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
              ) : (
                <label className="grid gap-[6px]">
                  <span className="text-[12px] font-medium text-[#464c5e]">补充信息</span>
                  <Textarea value={answer} onChange={(event) => setAnswer(event.target.value)} rows={4} placeholder="填写任务继续执行所需的准确信息" />
                </label>
              )}
              <div className="flex flex-wrap justify-end gap-[8px]">
                <Button variant="outline" disabled={acting} onClick={() => setSelected(null)}>关闭</Button>
                {selected.available_commands.includes('cancel') ? <Button variant="outline" disabled={acting} onClick={() => void resolve('cancel')}>取消任务</Button> : null}
                {selected.available_commands.includes('answer') ? <Button disabled={acting} onClick={() => void resolve('answer')} className="bg-[#3157e8] text-white hover:bg-[#244bc7]">补充并继续</Button> : null}
              </div>
            </>
          ) : null}
        </DialogContent>
      </Dialog>
    </section>
  );
}

function attentionQuestion(item: AttentionItem): string {
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
  return ({ clarification: '补充任务信息', exception: '处理执行异常', publication: '处理结果投递', result_review: '复核执行结果' } as Record<string, string>)[kind] || '处理任务事项';
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
  return ({ completed: '已完成', running: '执行中', waiting: '等待处理', failed: '失败', scheduled: '待执行', pending: '待执行' } as Record<string, string>)[status] || status;
}

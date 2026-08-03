/**
 * @Time       : 2026/08/04 17:05
 * @Author     : zhanglp8181
 * @File       : DynamicExecutionControl.tsx
 * @CallChain  : Chat 动态执行消息 → Execution API → steer/cancel 持久命令
 * @Description: 展示活动动态 Execution，并以显式交互追加约束或取消任务。
 */

import { useCallback, useEffect, useState } from 'react';
import { CircleCheck, CircleX, LoaderCircle, Route, SlidersHorizontal } from 'lucide-react';

import { api, getRequestTenantId } from '@/api/client';
import { notify } from '@/components/ui/app-toast';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogTitle, Textarea } from '@/components/ui';
import { cn } from '@/lib/utils';

type ExecutionState = {
  id: string;
  kind: string;
  status: string;
  revision: number;
  goal?: string | null;
  current_step_key?: string | null;
};

type CommandState = {
  command_id: string;
  command_type: 'steer' | 'cancel';
  status: 'pending' | 'claimed' | 'applied' | 'conflicted' | 'rejected';
  reason_code?: string | null;
  result_plan_revision_id?: string | null;
};

const ACTIVE_STATUSES = new Set(['running', 'waiting']);

export default function DynamicExecutionControl({ executionId }: { executionId: string }) {
  const [execution, setExecution] = useState<ExecutionState | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [instruction, setInstruction] = useState('');
  const [command, setCommand] = useState<CommandState | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const loadExecution = useCallback(async () => {
    const result = await api.get<ExecutionState>(
      `/api/executions/${executionId}?tenant_id=${encodeURIComponent(getRequestTenantId())}`,
    );
    setExecution(result);
    return result;
  }, [executionId]);

  useEffect(() => {
    let active = true;
    void loadExecution().catch(() => {
      if (active) setExecution(null);
    });
    return () => { active = false; };
  }, [loadExecution]);

  useEffect(() => {
    if (!command || !['pending', 'claimed'].includes(command.status)) return undefined;
    let active = true;
    const poll = async () => {
      try {
        const next = await api.get<CommandState>(
          `/api/executions/${executionId}/commands/${command.command_id}?tenant_id=${encodeURIComponent(getRequestTenantId())}`,
        );
        if (!active) return;
        setCommand(next);
        if (!['pending', 'claimed'].includes(next.status)) void loadExecution();
      } catch {
        // 短暂网络错误不改变服务端命令事实，下次轮询继续读取。
      }
    };
    const timer = window.setInterval(() => void poll(), 1_500);
    return () => { active = false; window.clearInterval(timer); };
  }, [command, executionId, loadExecution]);

  if (!execution || execution.kind !== 'dynamic_task') return null;
  const active = ACTIVE_STATUSES.has(execution.status);

  async function issue(commandType: 'steer' | 'cancel') {
    if (!execution || !active) return;
    const normalized = instruction.trim();
    if (commandType === 'steer' && !normalized) {
      notify.error('请填写要追加到当前任务的约束');
      return;
    }
    setSubmitting(true);
    try {
      const result = await api.post<CommandState>(`/api/executions/${execution.id}/commands`, {
        tenant_id: getRequestTenantId(),
        command_id: crypto.randomUUID(),
        command_type: commandType,
        expected_revision: execution.revision,
        payload: commandType === 'steer' ? { instruction: normalized } : { reason: 'user_requested' },
      });
      setCommand(result);
      setDialogOpen(false);
      setInstruction('');
      if (commandType === 'cancel') {
        await loadExecution();
        notify.success('取消命令已提交');
      } else {
        notify.success('约束已提交，将在安全动作边界应用');
      }
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '执行命令提交失败');
      await loadExecution().catch(() => undefined);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="mt-[10px] rounded-[12px] border border-[#dfe5f2] bg-[linear-gradient(105deg,#f8faff,#f5fbf8)] px-[12px] py-[10px]" aria-label="动态任务控制">
      <div className="flex flex-wrap items-center justify-between gap-[8px]">
        <span className="min-w-0">
          <span className="flex items-center gap-[6px] text-[12px] font-semibold text-[#343949]">
            <Route className="size-[13px] text-[#3157e8]" />
            <span className="truncate">{execution.goal || '动态任务'}</span>
          </span>
          <span className="mt-[2px] block text-[11px] text-[#7b849c]">
            {executionStatusLabel(execution.status)}{execution.current_step_key ? ` · ${execution.current_step_key}` : ''}
          </span>
        </span>
        {active ? (
          <span className="flex gap-[6px]">
            <Button variant="outline" size="sm" disabled={submitting} onClick={() => setDialogOpen(true)}>
              <SlidersHorizontal className="size-[13px]" />追加约束
            </Button>
            <Button variant="ghost" size="sm" disabled={submitting} onClick={() => void issue('cancel')}>
              取消任务
            </Button>
          </span>
        ) : null}
      </div>
      {command ? <CommandStatus command={command} /> : null}
      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent aria-describedby={undefined} className="gap-[14px] rounded-[14px] sm:max-w-[500px]">
          <DialogTitle className="text-[15px]">追加当前任务约束</DialogTitle>
          <p className="text-[12px] leading-[1.6] text-[#69728a]">已完成步骤和证据会保留；新约束只在当前外部动作安全结束后应用。若要开始无关目标，请直接发送一条新消息。</p>
          <Textarea value={instruction} onChange={(event) => setInstruction(event.target.value)} maxLength={4000} rows={4} placeholder="例如：只分析 2026 年内到期合同，并排除已终止记录" />
          <div className="flex justify-end gap-[8px]">
            <Button variant="outline" disabled={submitting} onClick={() => setDialogOpen(false)}>关闭</Button>
            <Button disabled={submitting || !instruction.trim()} onClick={() => void issue('steer')}>提交约束</Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function CommandStatus({ command }: { command: CommandState }) {
  const pending = ['pending', 'claimed'].includes(command.status);
  const applied = command.status === 'applied';
  const Icon = pending ? LoaderCircle : applied ? CircleCheck : CircleX;
  return (
    <div className={cn('mt-[8px] flex items-center gap-[6px] rounded-[8px] px-[8px] py-[6px] text-[11px]', pending ? 'bg-[#fff5df] text-[#8a5b00]' : applied ? 'bg-[#eaf8ef] text-[#16723c]' : 'bg-[#fff0f0] text-[#9b3434]')} role="status">
      <Icon className={cn('size-[13px]', pending && 'animate-spin')} />
      {commandStatusLabel(command)}
    </div>
  );
}

function commandStatusLabel(command: CommandState): string {
  if (command.status === 'pending' || command.status === 'claimed') return '约束等待安全边界应用';
  if (command.status === 'applied') return '约束已应用，任务已按新计划继续';
  if (command.status === 'conflicted') return '当前计划已变化，请确认最新状态后重新提交';
  return command.reason_code === 'DYNAMIC_STEERING_DISABLED' ? '追加约束能力当前已关闭' : '约束未被应用';
}

function executionStatusLabel(status: string): string {
  return ({ running: '执行中', waiting: '等待处理', succeeded: '已完成', failed: '失败', cancelled: '已取消' } as Record<string, string>)[status] || status;
}

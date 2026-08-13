/**
 * @Time       : 2026/08/04 17:05
 * @Author     : zhanglp8181
 * @File       : DynamicExecutionControl.tsx
 * @CallChain  : Chat 动态执行消息 → Execution/Skill catalog API → add_skill/steer/cancel 持久命令
 * @Description: 展示动态计划、并行读取证据，并允许在安全边界追加固定 Skill 或约束。
 */

import { useCallback, useEffect, useState } from 'react';
import { CircleCheck, CircleX, Download, LoaderCircle, Route, Sparkles, SlidersHorizontal } from 'lucide-react';

import { api, getRequestTenantId } from '@/api/client';
import { notify } from '@/components/ui/app-toast';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogTitle, RadioGroup, RadioGroupItem, Textarea } from '@/components/ui';
import { cn } from '@/lib/utils';
import { createClientId } from '@/lib/client-id';

type ExecutionState = {
  id: string;
  agent_id?: string | null;
  session_id: string;
  kind: string;
  status: string;
  revision: number;
  goal?: string | null;
  current_step_key?: string | null;
  plan_revision_number?: number | null;
  parallel_waves?: Array<{
    id: string;
    status: string;
    parallelism: number;
    ordered_step_keys: string[];
  }>;
  artifacts?: Array<{
    id: string;
    filename: string;
    mime_type: string;
    size_bytes: number;
  }>;
};

type CommandState = {
  command_id: string;
  command_type: 'steer' | 'cancel' | 'add_skill';
  status: 'pending' | 'claimed' | 'applied' | 'conflicted' | 'rejected';
  reason_code?: string | null;
  result_plan_revision_id?: string | null;
};

type SessionSkill = {
  skill_id: string;
  revision_id: string;
  revision_number: number;
  name: string;
  description: string;
  enabled: boolean;
};

const ACTIVE_STATUSES = new Set(['running', 'waiting']);

export default function DynamicExecutionControl({ executionId }: { executionId: string }) {
  const [execution, setExecution] = useState<ExecutionState | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [skillDialogOpen, setSkillDialogOpen] = useState(false);
  const [skillCatalogLoading, setSkillCatalogLoading] = useState(false);
  const [availableSkills, setAvailableSkills] = useState<SessionSkill[]>([]);
  const [selectedSkillId, setSelectedSkillId] = useState('');
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

  async function issue(commandType: 'steer' | 'cancel' | 'add_skill') {
    if (!execution || !active) return;
    const normalized = instruction.trim();
    if (commandType === 'steer' && !normalized) {
      notify.error('请填写要追加到当前任务的约束');
      return;
    }
    if (commandType === 'add_skill' && !selectedSkillId) {
      notify.error('请选择要增加到当前任务的 Skill');
      return;
    }
    setSubmitting(true);
    try {
      const result = await api.post<CommandState>(`/api/executions/${execution.id}/commands`, {
        tenant_id: getRequestTenantId(),
        command_id: createClientId(),
        command_type: commandType,
        expected_revision: execution.revision,
        payload: commandType === 'steer'
          ? { instruction: normalized }
          : commandType === 'add_skill'
            ? { skill_id: selectedSkillId, trigger: 'user' }
            : { reason: 'user_requested' },
      });
      setCommand(result);
      setDialogOpen(false);
      setSkillDialogOpen(false);
      setInstruction('');
      if (commandType === 'cancel') {
        await loadExecution();
        notify.success('取消命令已提交');
      } else if (commandType === 'add_skill') {
        setSelectedSkillId('');
        notify.success('Skill 已提交，将在安全动作边界固定修订并重规划');
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

  async function openSkillDialog() {
    if (!execution?.agent_id) {
      notify.error('当前任务缺少数字员工身份，不能增加 Skill');
      return;
    }
    setSkillDialogOpen(true);
    setSkillCatalogLoading(true);
    setSelectedSkillId('');
    try {
      const catalog = await api.get<{ items: SessionSkill[] }>(
        `/api/chat/sessions/${encodeURIComponent(execution.session_id)}/general-skills?agent_id=${encodeURIComponent(execution.agent_id)}`,
      );
      setAvailableSkills(catalog.items.filter((item) => item.enabled));
    } catch (error) {
      setAvailableSkills([]);
      notify.error(error instanceof Error ? error.message : '可用 Skill 加载失败');
    } finally {
      setSkillCatalogLoading(false);
    }
  }

  async function downloadArtifact(artifactId: string, filename: string) {
    try {
      const blob = await api.blob(
        `/api/artifacts/${artifactId}/download?tenant_id=${encodeURIComponent(getRequestTenantId())}`,
      );
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 0);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '交付物下载失败');
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
            {execution.plan_revision_number ? ` · 计划 v${execution.plan_revision_number}` : ''}
          </span>
        </span>
        {active ? (
          <span className="flex flex-wrap justify-end gap-[6px]">
            <Button variant="outline" size="sm" disabled={submitting} onClick={() => void openSkillDialog()}>
              <Sparkles className="size-[13px]" />运行中增加 Skill
            </Button>
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
      {execution.parallel_waves?.length ? (
        <div className="mt-[8px] grid gap-[5px]" aria-label="并行读取记录">
          {execution.parallel_waves.map((wave) => (
            <div key={wave.id} className="rounded-[8px] border border-[#dfe5f2] bg-white px-[9px] py-[7px] text-[11px] text-[#4f576d]">
              <span className="font-medium text-[#3157e8]">并行读取 · {wave.parallelism} 路 · {parallelStatusLabel(wave.status)}</span>
              <span className="mt-[2px] block break-all text-[#7b849c]">{wave.ordered_step_keys.join(' → ')}</span>
            </div>
          ))}
        </div>
      ) : null}
      {execution.artifacts?.length ? (
        <div className="mt-[8px] grid gap-[5px]" aria-label="任务交付物">
          {execution.artifacts.map((artifact) => (
            <button
              key={artifact.id}
              type="button"
              className="flex items-center justify-between gap-[8px] rounded-[8px] border border-[#dfe5f2] bg-white px-[9px] py-[7px] text-left text-[11px] text-[#4f576d] hover:border-[#b9c5ee] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#3157e8]"
              onClick={() => void downloadArtifact(artifact.id, artifact.filename)}
            >
              <span className="truncate">{artifact.filename}</span>
              <span className="flex shrink-0 items-center gap-[4px] text-[#3157e8]"><Download className="size-[12px]" />下载</span>
            </button>
          ))}
        </div>
      ) : null}
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
      <Dialog open={skillDialogOpen} onOpenChange={setSkillDialogOpen}>
        <DialogContent aria-describedby={undefined} className="gap-[14px] rounded-[14px] sm:max-w-[560px]">
          <DialogTitle className="text-[15px]">给当前任务增加 Skill</DialogTitle>
          <p className="text-[12px] leading-[1.6] text-[#69728a]">系统会重新校验当前用户、数字员工和会话权限，固定不可变修订后生成新计划；已完成步骤不会被伪装成重做。</p>
          {skillCatalogLoading ? (
            <div className="flex items-center gap-[6px] py-[18px] text-[12px] text-[#69728a]"><LoaderCircle className="size-[14px] animate-spin" />正在读取当前会话可用 Skill</div>
          ) : availableSkills.length ? (
            <RadioGroup value={selectedSkillId} onValueChange={setSelectedSkillId} aria-label="可追加 Skill">
              {availableSkills.map((skill) => (
                <label key={skill.skill_id} className="flex cursor-pointer items-start gap-[10px] rounded-[10px] border border-[#dfe5f2] bg-white px-[11px] py-[10px] hover:border-[#b9c5ee]">
                  <RadioGroupItem value={skill.skill_id} aria-label={`${skill.name} 修订 ${skill.revision_number}`} className="mt-[2px]" />
                  <span className="min-w-0">
                    <span className="block text-[12px] font-medium text-[#343949]">{skill.name}</span>
                    <span className="mt-[2px] block text-[11px] leading-[1.5] text-[#7b849c]">固定修订 v{skill.revision_number} · {skill.description}</span>
                  </span>
                </label>
              ))}
            </RadioGroup>
          ) : (
            <div className="rounded-[10px] bg-[#f6f7fb] px-[12px] py-[16px] text-center text-[12px] text-[#69728a]">当前会话没有可追加的 Skill</div>
          )}
          <div className="flex justify-end gap-[8px]">
            <Button variant="outline" disabled={submitting} onClick={() => setSkillDialogOpen(false)}>关闭</Button>
            <Button disabled={submitting || skillCatalogLoading || !selectedSkillId} onClick={() => void issue('add_skill')}>确认增加 Skill</Button>
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
  if (command.command_type === 'add_skill') {
    if (command.status === 'pending' || command.status === 'claimed') return 'Skill 等待安全边界加载';
    if (command.status === 'applied') return 'Skill 已固定修订并应用到新计划';
    if (command.status === 'conflicted') return 'Skill 加载时计划或修订已变化，请刷新后重试';
    return 'Skill 未被加载，原计划保持不变';
  }
  if (command.status === 'pending' || command.status === 'claimed') return '约束等待安全边界应用';
  if (command.status === 'applied') return '约束已应用，任务已按新计划继续';
  if (command.status === 'conflicted') return '当前计划已变化，请确认最新状态后重新提交';
  return command.reason_code === 'DYNAMIC_STEERING_DISABLED' ? '追加约束能力当前已关闭' : '约束未被应用';
}

function parallelStatusLabel(status: string): string {
  return ({ ready: '待派发', dispatched: '读取中', settling: '结算中', succeeded: '已完成', failed: '失败', cancelled: '已取消', superseded: '已替代' } as Record<string, string>)[status] || status;
}

function executionStatusLabel(status: string): string {
  return ({ running: '执行中', waiting: '等待处理', succeeded: '已完成', failed: '失败', cancelled: '已取消' } as Record<string, string>)[status] || status;
}

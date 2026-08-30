/**
 * @Time       : 2026/08/12 00:05
 * @Author     : zhanglp8181
 * @File       : StandingApprovalDialog.tsx
 * @CallChain  : ScheduledTasksTab → standing approval API → DynamicTaskAgent
 * @Description: 管理调度任务的精确企业微信长期批准，并明确展示任务、目标和正文三重边界。
 */

import { useEffect, useMemo, useState } from 'react';
import { Clock3, ShieldCheck, ShieldOff } from 'lucide-react';

import {
  createStandingApprovalRule,
  listStandingApprovalCandidates,
  listStandingApprovalRules,
  revokeStandingApprovalRule,
} from '@/api/standing-approvals';
import { notify } from '@/components/ui/app-toast';
import {
  Button,
  Checkbox,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
  Label,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Textarea,
} from '@/components/ui';
import { formatDateTime } from '@/lib/enterprise-ui';
import type { ScheduledTaskRead } from '@/types';
import type {
  StandingApprovalCandidate,
  StandingApprovalRule,
} from '@/types/standing-approvals';

export function StandingApprovalDialog({
  task,
  open,
  onOpenChange,
}: {
  task: ScheduledTaskRead | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const [candidates, setCandidates] = useState<StandingApprovalCandidate[]>([]);
  const [rules, setRules] = useState<StandingApprovalRule[]>([]);
  const [targetId, setTargetId] = useState('');
  const [exactContent, setExactContent] = useState('');
  const [validDays, setValidDays] = useState('7');
  const [acknowledged, setAcknowledged] = useState(false);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [revokeTarget, setRevokeTarget] = useState<StandingApprovalRule | null>(null);
  const selectedCandidate = useMemo(
    () => candidates.find((candidate) => candidate.thread_binding_id === targetId),
    [candidates, targetId],
  );

  useEffect(() => {
    if (!open || !task) return;
    setExactContent(task.prompt);
    setAcknowledged(false);
    setLoading(true);
    void Promise.all([
      listStandingApprovalCandidates(task.id),
      listStandingApprovalRules(task.id),
    ]).then(([nextCandidates, nextRules]) => {
      setCandidates(nextCandidates);
      setRules(nextRules);
      setTargetId(nextCandidates[0]?.thread_binding_id || '');
    }).catch((error: unknown) => {
      notify.error(error instanceof Error ? error.message : '加载长期批准失败');
      setCandidates([]);
      setRules([]);
    }).finally(() => setLoading(false));
  }, [open, task]);

  async function createRule() {
    /** 保存精确授权边界，服务端会再次核对资源、权限、修订和有效期。 */

    if (!task || !selectedCandidate || !exactContent.trim() || !acknowledged) return;
    setSaving(true);
    try {
      await createStandingApprovalRule({
        scheduleId: task.id,
        agentId: task.agent_id,
        candidate: selectedCandidate,
        exactContent: exactContent.trim(),
        validDays: Number(validDays),
      });
      setRules(await listStandingApprovalRules(task.id));
      setAcknowledged(false);
      notify.success('长期批准已生效');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '创建长期批准失败');
    } finally {
      setSaving(false);
    }
  }

  async function revokeRule() {
    /** 撤销后刷新权威规则；下一次派发会自动退回一次性人工审批。 */

    if (!task || !revokeTarget) return;
    setSaving(true);
    try {
      await revokeStandingApprovalRule(revokeTarget);
      setRules(await listStandingApprovalRules(task.id));
      setRevokeTarget(null);
      notify.success('长期批准已撤销');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '撤销长期批准失败');
    } finally {
      setSaving(false);
    }
  }

  return (
    <>
      <Dialog open={open} onOpenChange={(next) => !saving && onOpenChange(next)}>
        <DialogContent className="max-h-[calc(100dvh-3rem)] gap-0 overflow-y-auto rounded-[18px] p-0 sm:max-w-[720px]" aria-describedby="standing-approval-description">
          <header className="border-b border-[#e8ebf2] bg-[linear-gradient(135deg,#f7f9ff_0%,#ffffff_68%)] px-[24px] py-[20px]">
            <div className="flex items-center gap-[10px]">
              <span className="grid size-[38px] place-items-center rounded-[12px] bg-[#e9efff] text-[var(--gg-cobalt)]"><ShieldCheck className="size-[18px]" /></span>
              <div>
                <DialogTitle className="gg-type-section-title font-semibold text-[#18181a]">长期批准</DialogTitle>
                <DialogDescription id="standing-approval-description" className="mt-[3px] gg-type-meta text-[#687087]">
                  只对同一任务、同一会话和符合约束的正文自动放行。
                </DialogDescription>
              </div>
            </div>
          </header>

          <div className="grid gap-[20px] px-[24px] py-[20px]">
            <section aria-label="授权边界" className="overflow-hidden rounded-[14px] border border-[#dfe5f3] bg-white">
              <BoundaryRow label="任务" value={task?.title || '—'} index="01" />
              <div className="border-t border-[#edf0f5] px-[14px] py-[12px]">
                <div className="flex gap-[10px]">
                  <span className="pt-[8px] font-mono gg-type-caption text-[#8c96ad]">02</span>
                  <div className="min-w-0 flex-1">
                    <Label htmlFor="standing-target" className="gg-type-caption font-medium text-[#687087]">企业微信目标</Label>
                    <Select value={targetId} onValueChange={setTargetId} disabled={loading || candidates.length === 0}>
                      <SelectTrigger id="standing-target" className="mt-[5px] h-[36px] rounded-[9px] border-[#dfe4ee] gg-type-meta">
                        <SelectValue placeholder={loading ? '正在核对可用会话…' : '选择已授权会话'} />
                      </SelectTrigger>
                      <SelectContent>
                        {candidates.map((candidate) => (
                          <SelectItem key={candidate.thread_binding_id} value={candidate.thread_binding_id}>{candidate.target_label}</SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                    {!loading && candidates.length === 0 && (
                      <p className="mt-[6px] gg-type-caption  text-[#a35c00]">暂无可用会话。请先让该数字员工接收一条企业微信消息，并在外部连接中开启受控发送。</p>
                    )}
                  </div>
                </div>
              </div>
              <div className="border-t border-[#edf0f5] px-[14px] py-[12px]">
                <div className="flex gap-[10px]">
                  <span className="pt-[3px] font-mono gg-type-caption text-[#8c96ad]">03</span>
                  <div className="min-w-0 flex-1">
                    <Label htmlFor="standing-content" className="gg-type-caption font-medium text-[#687087]">允许发送的精确正文</Label>
                    <Textarea id="standing-content" value={exactContent} onChange={(event) => setExactContent(event.target.value)} maxLength={4000} className="mt-[5px] min-h-[88px] resize-y rounded-[9px] border-[#dfe4ee] gg-type-meta " />
                    <p className="mt-[5px] text-right font-mono gg-type-caption text-[#9299aa]">{exactContent.length}/4000</p>
                  </div>
                </div>
              </div>
            </section>

            <div className="grid gap-[12px] sm:grid-cols-[180px_1fr] sm:items-start">
              <div>
                <Label htmlFor="standing-validity" className="gg-type-caption text-[#687087]">有效期</Label>
                <Select value={validDays} onValueChange={setValidDays}>
                  <SelectTrigger id="standing-validity" className="mt-[5px] h-[36px] rounded-[9px] border-[#dfe4ee] gg-type-meta"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="1">1 天</SelectItem>
                    <SelectItem value="7">7 天</SelectItem>
                    <SelectItem value="30">30 天</SelectItem>
                    <SelectItem value="90">90 天</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <label className="flex cursor-pointer items-start gap-[9px] rounded-[11px] border border-[#f1d7ae] bg-[#fff9ef] px-[12px] py-[10px] gg-type-caption  text-[#825018]">
                <Checkbox className="mt-[1px]" checked={acknowledged} onCheckedChange={(value) => setAcknowledged(value === true)} />
                <span>我确认：命中以上边界时，数字员工无需逐次等待人工批准；任何目标、配置、正文或权限变化都会停止自动放行。</span>
              </label>
            </div>

            <div className="flex justify-end gap-[8px]">
              <Button variant="outline" onClick={() => onOpenChange(false)} disabled={saving}>关闭</Button>
              <Button onClick={() => void createRule()} disabled={saving || loading || !selectedCandidate || !exactContent.trim() || !acknowledged} className="bg-[var(--gg-cobalt)] text-white hover:bg-[#244bc7]">启用长期批准</Button>
            </div>

            <section aria-label="批准记录" className="border-t border-[#edf0f5] pt-[16px]">
              <div className="mb-[10px] flex items-center justify-between">
                <h3 className="gg-type-card-title font-semibold text-[#252936]">批准记录</h3>
                <span className="gg-type-caption text-[#8b93a6]">只可撤销，不可原地扩权</span>
              </div>
              {revokeTarget && (
                <div role="alert" className="mb-[10px] rounded-[11px] border border-[#f1d7ae] bg-[#fff9ef] px-[12px] py-[10px]">
                  <p className="gg-type-meta font-medium text-[#6f4213]">撤销这条长期批准？</p>
                  <p className="mt-[3px] gg-type-caption  text-[#825018]">后续任务若仍需发送，将回到一次性人工审批。</p>
                  <div className="mt-[8px] flex justify-end gap-[6px]">
                    <Button size="sm" variant="ghost" disabled={saving} onClick={() => setRevokeTarget(null)}>取消</Button>
                    <Button size="sm" disabled={saving} className="bg-[#c43838] text-white hover:bg-[#a92020]" onClick={() => void revokeRule()}>确认撤销</Button>
                  </div>
                </div>
              )}
              <div className="grid gap-[8px]">
                {rules.length === 0 ? <p className="rounded-[11px] bg-[#f7f8fb] px-[12px] py-[14px] text-center gg-type-caption text-[#858b9c]">尚未创建长期批准</p> : rules.map((rule) => (
                  <div key={rule.id} className="flex items-start gap-[10px] rounded-[11px] border border-[#e7eaf1] px-[12px] py-[10px]">
                    <span className={rule.status === 'active' ? 'mt-[1px] text-[#287a4b]' : 'mt-[1px] text-[#9aa1b0]'}>{rule.status === 'active' ? <ShieldCheck className="size-[15px]" /> : <ShieldOff className="size-[15px]" />}</span>
                    <div className="min-w-0 flex-1">
                      <p className="truncate gg-type-meta font-medium text-[#252936]">{rule.argument_constraints.content?.equals || '受限正文'}</p>
                      <p className="mt-[3px] flex flex-wrap items-center gap-x-[8px] gg-type-caption text-[#838b9d]"><span className="inline-flex items-center gap-[3px]"><Clock3 className="size-[10px]" />至 {formatDateTime(rule.valid_to)}</span><span>{rule.status === 'active' ? '生效中' : '已撤销'}</span></p>
                    </div>
                    {rule.status === 'active' && <Button variant="ghost" size="sm" className="h-[28px] gg-type-caption text-[#c43838] hover:text-[#a92020]" onClick={() => setRevokeTarget(rule)}>撤销</Button>}
                  </div>
                ))}
              </div>
            </section>
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}

function BoundaryRow({ label, value, index }: { label: string; value: string; index: string }) {
  return (
    <div className="flex gap-[10px] px-[14px] py-[12px]">
      <span className="font-mono gg-type-caption text-[#8c96ad]">{index}</span>
      <div className="min-w-0"><p className="gg-type-caption font-medium text-[#687087]">{label}</p><p className="mt-[3px] truncate gg-type-meta font-semibold text-[#252936]">{value}</p></div>
    </div>
  );
}

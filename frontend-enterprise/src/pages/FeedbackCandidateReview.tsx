import { useEffect, useState } from 'react';
import { Button } from '@/components/ui/button';
import { Checkbox, Dialog, DialogContent, DialogTitle } from '@/components/ui';
import { DETAIL_ACTIONS_CLASS, DETAIL_PANEL_CLASS } from '@/lib/enterprise-ui';
import type { SkillCard } from '@/types';
import type { SkillCandidateRead } from '@/types/skill-candidate';
import { FeedbackMaterialFacts } from './dashboard/FeedbackMaterials';

interface Props {
  candidate: SkillCandidateRead | null;
  draft: SkillCard | null;
  error: string;
  busy: boolean;
  onRefresh: () => void;
  onSave: () => void;
  onReject: () => void;
  onReset: () => void;
  onPublish: (confirmDifference: boolean) => void;
}

/** 原编辑页的受管来源与人工发布区；保存候选和发布是两个明确动作。 */
export function FeedbackCandidateReview(props: Props) {
  const { candidate, draft, error, busy } = props;
  const [confirmIdentity, setConfirmIdentity] = useState<string | null>(null);
  const [differenceConfirmed, setDifferenceConfirmed] = useState(false);
  const identity = candidate ? `${candidate.id}:${candidate.revision}` : null;
  const confirmOpen = identity !== null && identity === confirmIdentity;
  const active = Boolean(!error && candidate?.content && ['draft', 'ready'].includes(candidate.status));
  useEffect(() => {
    if (!active) { setConfirmIdentity(null); setDifferenceConfirmed(false); }
  }, [active]);
  const unsaved = JSON.stringify(draft) !== JSON.stringify(candidate?.content);
  return <section className={`${DETAIL_PANEL_CLASS} max-h-[35dvh] overflow-y-auto`} aria-label="反馈候选审核">
    <h2 className="gg-type-card-title">反馈候选审核</h2>
    <p>候选保存不会改变活动 SOP。请核对来源、当前基础和差异后人工确认发布。</p>
    {error ? <p role="alert">{error}</p> : null}
    {candidate?.published ? <p role="status">已发布版本 {candidate.published.version} · {candidate.published.skill_version_id}</p> : null}
    {active && candidate ? <>
      <dl className="gg-type-meta"><dt>当前发布基础</dt><dd>{candidate.base_version} · {candidate.base_skill_version_id}</dd>
        <dt>目标范围</dt><dd>{candidate.scope_agent_id || '整体'}</dd></dl>
      {candidate.base_difference ? <p>原反馈执行版本与当前发布基础不同，发布时必须明确确认。</p> : null}
      <details><summary>查看授权材料及原轮来源</summary>
        {candidate.materials.map((material) => <FeedbackMaterialFacts key={material.id} value={material} />)}
      </details>
      <details><summary>查看当前原版与已保存候选差异</summary>
        <p>{candidate.changed_paths.length ? candidate.changed_paths.join('、') : '暂无已保存差异'}</p>
        <div className="grid gap-3 md:grid-cols-2">
          <div><h3>当前原版</h3><pre className="whitespace-pre-wrap wrap-break-word">{JSON.stringify(candidate.base_content, null, 2)}</pre></div>
          <div><h3>已保存候选</h3><pre className="whitespace-pre-wrap wrap-break-word">{JSON.stringify(candidate.content, null, 2)}</pre></div>
        </div>
      </details>
    </> : null}
    <div className={DETAIL_ACTIONS_CLASS}>
      <Button variant="outline" disabled={busy} onClick={props.onRefresh}>重新校验候选</Button>
      {active ? <>
        <Button variant="outline" disabled={busy || !draft} onClick={props.onSave}>仅保存隔离候选</Button>
        <Button variant="outline" disabled={busy} onClick={props.onReject}>拒绝此候选</Button>
        <Button disabled={busy || unsaved || !candidate?.changed_paths.length} onClick={() => {
          setDifferenceConfirmed(false); setConfirmIdentity(identity);
        }}>人工确认发布候选</Button>
      </> : null}
      <Button variant="outline" disabled={busy} onClick={props.onReset}>丢弃并加载当前范围原版</Button>
    </div>
    {unsaved && active ? <p>请先保存当前编辑，再确认发布。</p> : null}
    <Dialog open={confirmOpen && active} onOpenChange={(open) => { if (!open) setConfirmIdentity(null); }}>
      <DialogContent aria-describedby={undefined}>
        <DialogTitle>确认沿现有发布链发布候选</DialogTitle>
        <p>发布后新任务采用本候选，已运行实例继续使用原发布版本。</p>
        <p>{candidate?.base_skill_version_id} · 修订 {candidate?.revision}</p>
        {candidate?.base_difference ? <label className="flex items-center gap-2">
          <Checkbox checked={differenceConfirmed} onCheckedChange={(value) => setDifferenceConfirmed(value === true)} />
          我已核对原反馈版本与当前基础的差异
        </label> : null}
        <div className={DETAIL_ACTIONS_CLASS}>
          <Button variant="outline" disabled={busy} onClick={() => setConfirmIdentity(null)}>取消</Button>
          <Button disabled={busy || Boolean(candidate?.base_difference && !differenceConfirmed)}
            onClick={() => props.onPublish(differenceConfirmed)}>确认发布</Button>
        </div>
      </DialogContent>
    </Dialog>
  </section>;
}

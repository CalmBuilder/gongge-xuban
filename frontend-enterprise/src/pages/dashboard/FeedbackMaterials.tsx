import { useCallback, useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';
import { useNavigate } from 'react-router-dom';
import { createSkillCandidate } from '@/api/skill-candidates';

import { listFeedbackMaterials, previewFeedbackMaterial, readFeedbackMaterial,
  shareFeedbackMaterial, withdrawFeedbackMaterial } from '@/api/feedback';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogTitle } from '@/components/ui';
import { Textarea } from '@/components/ui/textarea';
import { DETAIL_PANEL_CLASS, DETAIL_ACTIONS_CLASS, MOBILE_CARD_CLASS } from '@/lib/enterprise-ui';
import { FEEDBACK_MATERIAL_STATUS_LABELS, type FeedbackMaterialRead,
  type FeedbackPreviewSelection } from '@/types/feedback';

const MATERIAL_INVALID_MESSAGE = '材料已失效或权限已变化，请重新加载';

/** 只展示服务端授权投影；详情请求与列表范围均不读取对话全文。 */
export default function FeedbackMaterials({ agentId, refreshKey = 0 }: { agentId: string; refreshKey?: number }) {
  return <FeedbackMaterialsScope key={agentId} agentId={agentId} refreshKey={refreshKey} />;
}

function FeedbackMaterialsScope({ agentId, refreshKey }: { agentId: string; refreshKey: number }) {
  const navigate = useNavigate();
  const [owned, setOwned] = useState<FeedbackMaterialRead[]>([]);
  const [managed, setManaged] = useState<FeedbackMaterialRead[]>([]);
  const [detail, setDetail] = useState<FeedbackMaterialRead | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const requestId = useRef(0);
  const alive = useRef(true);
  const candidateKey = useRef<{ materialId: string; key: string } | null>(null);

  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; requestId.current += 1; };
  }, []);

  const load = useCallback(async () => {
    if (!agentId) return;
    const id = ++requestId.current;
    setBusy(true);
    setDetail(null);
    setOwned([]);
    setManaged([]);
    setError('');
    const results = await Promise.allSettled([
      listFeedbackMaterials(agentId, 'owned'), listFeedbackMaterials(agentId, 'managed'),
    ]);
    if (!alive.current || id !== requestId.current) return;
    const [ownerResult, managerResult] = results;
    if (ownerResult.status === 'fulfilled') setOwned(ownerResult.value);
    if (managerResult.status === 'fulfilled') setManaged(managerResult.value);
    if (ownerResult.status === 'rejected' && managerResult.status === 'rejected') setError(MATERIAL_INVALID_MESSAGE);
    setBusy(false);
  }, [agentId]);

  useEffect(() => { void load(); }, [load, refreshKey]);

  const open = async (materialId: string) => {
    const id = ++requestId.current;
    setDetail(null);
    setError('');
    setBusy(true);
    try {
      const value = await readFeedbackMaterial(materialId);
      if (!alive.current || id !== requestId.current) return;
      if (value.target_agent_id !== agentId || value.status !== 'shared') throw new Error('invalid');
      setDetail(value);
    } catch {
      if (alive.current && id === requestId.current) {
        setDetail(null); setOwned([]); setManaged([]); setError(MATERIAL_INVALID_MESSAGE);
      }
    } finally {
      if (alive.current && id === requestId.current) setBusy(false);
    }
  };

  const withdraw = async (value: FeedbackMaterialRead) => {
    const id = ++requestId.current;
    setDetail(null);
    setBusy(true);
    try {
      await withdrawFeedbackMaterial(value);
      if (!alive.current || id !== requestId.current) return;
      toast.success('已撤回分享');
      await load();
    } catch {
      if (alive.current && id === requestId.current) {
        setOwned([]); setManaged([]); setError(MATERIAL_INVALID_MESSAGE);
      }
    } finally {
      if (alive.current && id === requestId.current) setBusy(false);
    }
  };

  if (!agentId) return null;
  const improve = async (material: FeedbackMaterialRead) => {
    const id = ++requestId.current;
    setBusy(true);
    if (candidateKey.current?.materialId !== material.id) {
      candidateKey.current = { materialId: material.id, key: crypto.randomUUID() };
    }
    try {
      const candidate = await createSkillCandidate(material.id, agentId, candidateKey.current.key);
      if (!alive.current || id !== requestId.current) return;
      const params = new URLSearchParams({ skill_id: candidate.skill_id, agent_id: agentId,
        feedback_candidate_id: candidate.id });
      navigate(`/enterprise/skills/distill?${params.toString()}`);
    } catch {
      if (alive.current && id === requestId.current) {
        setDetail(null); setError(MATERIAL_INVALID_MESSAGE);
      }
    } finally {
      if (alive.current && id === requestId.current) setBusy(false);
    }
  };
  return (
    <section className={DETAIL_PANEL_CLASS} aria-label="反馈改进材料" aria-busy={busy}>
      <div className="flex items-center justify-between gap-3">
        <h2 className="gg-type-card-title">反馈改进材料</h2>
        <Button variant="outline" disabled={busy} onClick={() => void load()}>刷新材料列表</Button>
      </div>
      {error ? <p role="alert">{error}</p> : null}
      <div className="mt-4 grid gap-4 md:grid-cols-2">
        <div><h3 className="gg-type-control">我的改进材料</h3>
          {owned.length === 0 ? <p className="gg-type-meta">暂无本人材料</p> : owned.map((item) => (
            <article key={item.id} className={MOBILE_CARD_CLASS}>
              <p>{item.feedback_id} · {FEEDBACK_MATERIAL_STATUS_LABELS[item.status]}</p>
              {item.status === 'shared' ? <Button variant="outline" disabled={busy} onClick={() => void withdraw(item)}>撤回分享</Button> : null}
            </article>
          ))}
        </div>
        <div><h3 className="gg-type-control">员工已分享材料</h3>
          {managed.length === 0 ? <p className="gg-type-meta">暂无可管理的共享材料</p> : managed.filter((item) => item.status === 'shared').map((item) => (
            <article key={item.id} className={MOBILE_CARD_CLASS}>
              <p>{item.feedback_id}</p>
              <Button variant="outline" disabled={busy} onClick={() => void open(item.id)}>查看共享材料</Button>
            </article>
          ))}
        </div>
      </div>
      {detail ? <div className="mt-4">
        <FeedbackMaterialFacts value={detail} />
        <Button variant="outline" disabled={busy} onClick={() => void open(detail.id)}>刷新材料详情</Button>
        {detail.material.source.status === 'verified' && detail.material.source.branch === 'sop'
          ? <Button disabled={busy} onClick={() => void improve(detail)}>在当前编辑器改进 SOP</Button>
          : <p className="gg-type-meta">此材料保留反馈分析，缺少可用于 SOP 改进的发布来源</p>}
      </div> : null}
    </section>
  );
}

/** 本人先生成可检查投影，再明确授权；选择变化时整个预览状态销毁。 */
export function FeedbackMaterialPreview({ selection, onClose, onChanged }: {
  selection: FeedbackPreviewSelection | null; onClose: () => void; onChanged: () => void;
}) {
  return selection ? <FeedbackPreviewForm key={`${selection.feedbackId}:${selection.targetAgentId}`} selection={selection} onClose={onClose} onChanged={onChanged} /> : null;
}

function FeedbackPreviewForm({ selection, onClose, onChanged }: {
  selection: FeedbackPreviewSelection; onClose: () => void; onChanged: () => void;
}) {
  const [note, setNote] = useState('');
  const [value, setValue] = useState<FeedbackMaterialRead | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const alive = useRef(true);
  const pendingPreview = useRef<{ note: string; requestKey: string } | null>(null);
  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; };
  }, []);

  const preview = async () => {
    setBusy(true); setValue(null); setError('');
    if (!pendingPreview.current || pendingPreview.current.note !== note) {
      pendingPreview.current = { note, requestKey: crypto.randomUUID() };
    }
    try {
      const result = await previewFeedbackMaterial(selection.feedbackId, selection.targetAgentId, note, pendingPreview.current.requestKey);
      if (alive.current) {
        if (result.target_agent_id !== selection.targetAgentId || result.feedback_id !== selection.feedbackId || result.status !== 'preview') throw new Error('invalid');
        setValue(result);
      }
    } catch { if (alive.current) setError(MATERIAL_INVALID_MESSAGE); }
    finally { if (alive.current) setBusy(false); }
  };

  const share = async () => {
    if (!value) return;
    setBusy(true); setError('');
    try {
      await shareFeedbackMaterial(value);
      if (alive.current) { toast.success('已分享改进材料'); onChanged(); onClose(); }
    } catch { if (alive.current) { setValue(null); setError(MATERIAL_INVALID_MESSAGE); } }
    finally { if (alive.current) setBusy(false); }
  };

  return <Dialog open onOpenChange={(open) => { if (!open) onClose(); }}>
    <DialogContent aria-describedby={undefined} className="max-h-[85dvh] overflow-y-auto sm:max-w-[720px]">
      <DialogTitle>预览改进材料</DialogTitle>
      <p className="gg-type-meta">仅提交最小必要业务说明，不分享会话全文。确认脱敏预览后，仅分享给该员工有权管理的人员。</p>
      <label htmlFor="feedback-material-note">用户说明</label>
      <Textarea id="feedback-material-note" value={note} maxLength={2000} disabled={busy}
        onChange={(event) => { setNote(event.target.value); setValue(null); setError(''); }} />
      {error ? <p role="alert">{error}</p> : null}
      {value ? <FeedbackMaterialFacts value={value} /> : null}
      <div className={DETAIL_ACTIONS_CLASS}>
        <Button variant="outline" disabled={busy} onClick={() => void preview()}>生成脱敏预览</Button>
        <Button disabled={busy || !value} onClick={() => void share()}>确认分享给该员工管理者</Button>
      </div>
    </DialogContent>
  </Dialog>;
}

export function FeedbackMaterialFacts({ value }: { value: FeedbackMaterialRead }) {
  const { source, facts, inference, user_note: userNote } = value.material;
  return <div className="grid gap-3 wrap-break-word">
    <section><h3 className="gg-type-control">原轮来源</h3>
      {source.status === 'missing' ? <p>{source.missing_reason || '缺少可验证来源'}</p> : <dl className="gg-type-meta">
        <dt>执行分支</dt><dd>{source.branch}</dd>
        <dt>原消息</dt><dd>{source.message_id}</dd>
        <dt>原轮次</dt><dd>{source.turn_id || '-'}</dd>
        <dt>原执行</dt><dd>{source.execution_id || '-'}</dd>
        <dt>原发布版本</dt><dd>{source.skill_version || '-'}</dd>
        <dt>原发布身份</dt><dd>{source.skill_version_id || '-'}</dd>
      </dl>}
    </section>
    <section><h3 className="gg-type-control">确定执行事实</h3>
      <p>{facts.rating === 'down' ? '差评' : '好评'}</p>
      <p>{facts.user_request}</p><p className="whitespace-pre-wrap">{facts.assistant_reply}</p>
      {!facts.operations_known ? <p>缺少可验证工具调用记录</p>
        : facts.operations.length === 0 ? <p>截至该回复无工具调用</p>
          : <><h4 className="gg-type-meta">截至该回复的执行回执</h4>
            <ul>{facts.operations.map((operation) => <li key={operation.id}>{operation.id} · {operation.status} · {operation.tool_id || '-'}</li>)}</ul></>}
    </section>
    <section><h3 className="gg-type-control">用户说明</h3><p className="whitespace-pre-wrap">{userNote || '-'}</p></section>
    <section><h3 className="gg-type-control">模型归因（推断）</h3>
      {inference ? <><p>{inference.summary}</p><p>{inference.reason}</p></> : <p>暂无模型推断</p>}
    </section>
  </div>;
}

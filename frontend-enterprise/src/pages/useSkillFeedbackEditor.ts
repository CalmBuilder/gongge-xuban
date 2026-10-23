import { useCallback, useEffect, useRef, useState } from 'react';
import { readSkillCandidate, readSkillEditorContext, resetSkillCandidate,
  saveSkillCandidate, rejectSkillCandidate, publishSkillCandidate } from '@/api/skill-candidates';
import type { SkillCard } from '@/types';
import type { SkillCandidateRead, SkillEditorContextRead } from '@/types/skill-candidate';
import { ApiError } from '@/api/client';

export const FEEDBACK_EDITOR_INVALID = '候选已失效或权限已变化，请丢弃并重新加载当前范围原版';

/** 只恢复服务器身份与正文；反馈及退出反馈后的编辑不读浏览器草稿缓存。 */
export function useSkillFeedbackEditor(candidateId: string, agentId: string, skillId: string | null) {
  const [candidate, storeCandidate] = useState<SkillCandidateRead | null>(null);
  const candidateRef = useRef<SkillCandidateRead | null>(null);
  const setCandidate = useCallback((value: SkillCandidateRead | null) => {
    // 异步fresh核验必须识别同候选已经合法保存的新对象，不清后提交的修订。
    candidateRef.current = value;
    storeCandidate(value);
  }, []);
  const [context, setContext] = useState<SkillEditorContextRead | null>(null);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const identity = JSON.stringify([candidateId, agentId, skillId]);
  const [loadedIdentity, setLoadedIdentity] = useState('');
  const generation = useRef(0);
  const publishIdentity = useRef<{ revision: number; key: string } | null>(null);

  const invalidate = useCallback(() => {
    generation.current += 1;
    setCandidate(null);
    setBusy(false);
    setError(FEEDBACK_EDITOR_INVALID);
  }, [setCandidate]);

  const refresh = useCallback(async () => {
    const token = ++generation.current;
    setReady(false); setCandidate(null); setContext(null); setError(''); setBusy(false);
    setLoadedIdentity(identity);
    try {
      const slot = await readSkillEditorContext();
      if (token !== generation.current) return;
      setContext(slot);
      if (candidateId) {
        const value = await readSkillCandidate(candidateId);
        if (token !== generation.current) return;
        const editable = Boolean(value.content && ['draft', 'ready'].includes(value.status));
        const publishedReceipt = value.status === 'published' && value.published !== null
          && value.content === null && value.materials.length === 0;
        if (value.id !== slot.candidate_id || value.skill_id !== skillId
          || (value.scope_agent_id && value.scope_agent_id !== agentId)
          || (agentId && slot.scope_profile_id !== agentId)
          || (!editable && !publishedReceipt)) {
          throw new Error('invalid-editor');
        }
        setCandidate(value);
      } else if (slot.candidate_id && !slot.ordinary) {
        throw new Error('reset-required');
      }
    } catch {
      if (token === generation.current) { setReady(true); invalidate(); }
    } finally {
      if (token === generation.current) setReady(true);
    }
  }, [candidateId, agentId, skillId, identity, invalidate]);

  const revalidate = useCallback(async () => {
    // job丢失只检查授权/身份；不重设ready/candidate，保留本地尚未保存的合法编辑。
    const token = generation.current;
    if (!candidate || identity !== loadedIdentity) return;
    try {
      const [slot, value] = await Promise.all([readSkillEditorContext(), readSkillCandidate(candidate.id)]);
      if (token !== generation.current || candidateRef.current !== candidate) return;
      if (slot.ordinary || slot.candidate_id !== candidate.id || slot.candidate_revision !== candidate.revision
        || value.id !== candidate.id || value.revision !== candidate.revision
        || value.tenant_id !== candidate.tenant_id
        || value.skill_id !== skillId || value.base_checksum !== candidate.base_checksum
        || value.base_skill_version_id !== candidate.base_skill_version_id
        || value.scope_agent_id !== candidate.scope_agent_id || !value.content
        || !['draft', 'ready'].includes(value.status)
        || (value.scope_agent_id && value.scope_agent_id !== agentId)
        || (agentId && slot.scope_profile_id !== agentId)) invalidate();
    } catch (error) {
      // 408/429及短事务写冲突没有撤销事实；旧核验也不能清合法新修订。
      if (token === generation.current && candidateRef.current === candidate
        && error instanceof ApiError && [401, 403, 404, 409].includes(error.status)
        && error.message !== 'FEEDBACK_CANDIDATE_WRITE_CONFLICT') invalidate();
    }
  }, [candidate, identity, loadedIdentity, skillId, agentId, invalidate]);

  useEffect(() => {
    void refresh();
    return () => { generation.current += 1; };
  }, [refresh]);

  const save = async (content: SkillCard) => {
    if (!candidate || !['draft', 'ready'].includes(candidate.status) || error || busy) return null;
    setBusy(true);
    const token = generation.current;
    try {
      const value = await saveSkillCandidate(candidate, content);
      if (token !== generation.current) return null;
      setCandidate(value);
      setContext((current) => current ? { ...current, candidate_revision: value.revision } : null);
      return value;
    } catch { if (token === generation.current) invalidate(); return null; }
    finally { if (token === generation.current) setBusy(false); }
  };

  const reject = async () => {
    if (!candidate || !['draft', 'ready'].includes(candidate.status) || busy) return;
    setBusy(true);
    const token = generation.current;
    try {
      const value = await rejectSkillCandidate(candidate);
      if (token !== generation.current) return;
      setContext((current) => current ? { ...current, candidate_revision: value.revision } : null);
      invalidate();
    } catch { if (token === generation.current) invalidate(); }
    finally { if (token === generation.current) setBusy(false); }
  };

  const reset = async () => {
    if (!context?.candidate_id || context.candidate_revision == null || busy) return null;
    setBusy(true);
    const token = generation.current;
    try {
      const value = await resetSkillCandidate(context.candidate_id, context.candidate_revision,
        agentId, skillId, crypto.randomUUID());
      if (token !== generation.current) return null;
      setCandidate(null); setError('');
      setContext({ ...context, candidate_revision: value.revision, ordinary: true,
        editor_context: value.editor_context });
      return value;
    } catch { if (token === generation.current) invalidate(); return null; }
    finally { if (token === generation.current) setBusy(false); }
  };

  const publish = async (confirmDifference: boolean) => {
    if (!candidate || !['draft', 'ready'].includes(candidate.status) || error || busy) return null;
    setBusy(true);
    const token = generation.current;
    try {
      let confirmed = candidate;
      if (candidate.base_difference && confirmDifference && !candidate.base_difference_confirmed) {
        if (!candidate.content) return null;
        confirmed = await saveSkillCandidate({ ...candidate, base_difference_confirmed: true }, candidate.content);
        if (token !== generation.current) return null;
        setCandidate(confirmed);
        setContext((current) => current ? { ...current, candidate_revision: confirmed.revision } : null);
      }
      if (publishIdentity.current?.revision !== confirmed.revision) {
        publishIdentity.current = { revision: confirmed.revision, key: crypto.randomUUID() };
      }
      const receipt = await publishSkillCandidate(confirmed, publishIdentity.current.key, confirmDifference);
      if (token !== generation.current) return null;
      setCandidate({ ...confirmed, status: 'published', content: null, materials: [], published: receipt });
      return receipt;
    } catch { if (token === generation.current) invalidate(); return null; }
    finally { if (token === generation.current) setBusy(false); }
  };

  const current = identity === loadedIdentity;
  return { candidate: current ? candidate : null, context: current ? context : null,
    ready: current && ready, error: current ? error : '', busy, refresh, revalidate, invalidate, save, reject, reset, publish,
    managed: Boolean(candidateId || context?.candidate_id),
    blocked: !current || !ready || Boolean(error) || candidate?.status === 'published',
    editorContext: context?.ordinary ? context.editor_context : null };
}

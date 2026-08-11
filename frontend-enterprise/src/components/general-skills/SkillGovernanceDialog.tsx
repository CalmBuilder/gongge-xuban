import { useEffect, useMemo, useState } from 'react';
import { History, ShieldCheck } from 'lucide-react';

import { api, getRequestTenantId } from '@/api/client';
import { notify } from '@/components/ui/app-toast';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogTitle,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui';
import { formatDateTime } from '@/lib/enterprise-ui';
import type {
  GeneralSkillBindingRead,
  GeneralSkillRead,
  GeneralSkillRevisionRead,
} from '@/types';

type SkillGovernanceDialogProps = {
  row: GeneralSkillRead | null;
  agentId: string;
  onClose: () => void;
  onChanged: () => void | Promise<void>;
};

export function SkillGovernanceDialog({ row, agentId, onClose, onChanged }: SkillGovernanceDialogProps) {
  const [revisions, setRevisions] = useState<GeneralSkillRevisionRead[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState<'active' | 'inactive'>('active');
  const [revisionPolicy, setRevisionPolicy] = useState<'pinned' | 'follow_latest'>('pinned');
  const [pinnedRevisionId, setPinnedRevisionId] = useState('');
  const [invocationPolicy, setInvocationPolicy] = useState<'model_allowed' | 'user_only'>('model_allowed');

  const availablePinnedRevisions = useMemo(
    () => revisions.filter((revision) => ['published', 'superseded'].includes(revision.status)),
    [revisions],
  );

  useEffect(() => {
    if (!row) return;
    setStatus(row.binding_status === 'inactive' ? 'inactive' : 'active');
    setRevisionPolicy(row.revision_policy || 'pinned');
    setPinnedRevisionId(row.pinned_revision_id || row.current_published_revision_id || '');
    setInvocationPolicy(row.invocation_policy || 'model_allowed');
    setLoading(true);
    api
      .get<GeneralSkillRevisionRead[]>(
        `/api/enterprise/general-skill-governance/skills/${encodeURIComponent(row.id)}/revisions?tenant_id=${encodeURIComponent(getRequestTenantId())}`,
      )
      .then(setRevisions)
      .catch((error) => notify.error(error.message))
      .finally(() => setLoading(false));
  }, [row]);

  async function saveBinding() {
    if (!row?.binding_id || !row.binding_row_version) return;
    if (revisionPolicy === 'pinned' && !pinnedRevisionId) {
      notify.error('请选择要固定的技能修订');
      return;
    }
    setSaving(true);
    try {
      await api.patch<GeneralSkillBindingRead>(
        `/api/enterprise/general-skill-governance/bindings/${encodeURIComponent(row.binding_id)}`,
        {
          agent_id: agentId,
          status,
          revision_policy: revisionPolicy,
          pinned_revision_id: revisionPolicy === 'pinned' ? pinnedRevisionId : null,
          invocation_policy: invocationPolicy,
          expected_row_version: row.binding_row_version,
        },
      );
      notify.success('技能绑定策略已更新');
      await onChanged();
      onClose();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '技能绑定策略更新失败');
    } finally {
      setSaving(false);
    }
  }

  async function rollback(revision: GeneralSkillRevisionRead) {
    if (!row?.row_version) return;
    setSaving(true);
    try {
      await api.post(
        `/api/enterprise/general-skill-governance/skills/${encodeURIComponent(row.id)}/rollback`,
        {
          target_revision_id: revision.id,
          expected_skill_row_version: row.row_version,
          expected_target_row_version: revision.row_version,
        },
      );
      notify.success(`已回滚到修订 v${revision.revision_number}`);
      await onChanged();
      onClose();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '技能修订回滚失败');
    } finally {
      setSaving(false);
    }
  }

  async function revoke(revision: GeneralSkillRevisionRead) {
    if (!row?.row_version) return;
    setSaving(true);
    try {
      await api.post(
        `/api/enterprise/general-skill-governance/skills/${encodeURIComponent(row.id)}/revisions/${encodeURIComponent(revision.id)}/revoke`,
        {
          expected_skill_row_version: row.row_version,
          expected_revision_row_version: revision.row_version,
        },
      );
      notify.success(`修订 v${revision.revision_number} 已撤销`);
      await onChanged();
      onClose();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '技能修订撤销失败');
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={Boolean(row)} onOpenChange={(open) => !open && onClose()}>
      <DialogContent aria-describedby={undefined} className="max-h-[88vh] w-[calc(100%-2rem)] overflow-y-auto rounded-2xl p-0 sm:max-w-[720px]">
        <DialogTitle className="flex items-center gap-2 border-b border-border px-6 py-4 text-[17px] font-semibold">
          <ShieldCheck className="size-5 text-[var(--gg-cobalt)]" />
          版本与调用策略
        </DialogTitle>
        <div className="grid gap-5 p-6">
          <div>
            <p className="text-[15px] font-semibold text-foreground">{row?.name}</p>
            <p className="mt-1 text-[12px] leading-5 text-muted-foreground">
              固定版本适合严格复现；跟随最新会在管理员发布或回滚后自动切换。停用后立即从对话和任务目录移除。
            </p>
          </div>
          <div className="grid gap-4 rounded-xl border border-border bg-muted/20 p-4 sm:grid-cols-3">
            <label className="grid gap-2 text-[12px] font-medium">
              绑定状态
              <Select value={status} onValueChange={(value) => setStatus(value as typeof status)}>
                <SelectTrigger aria-label="绑定状态"><SelectValue /></SelectTrigger>
                <SelectContent><SelectItem value="active">启用</SelectItem><SelectItem value="inactive">停用</SelectItem></SelectContent>
              </Select>
            </label>
            <label className="grid gap-2 text-[12px] font-medium">
              版本策略
              <Select value={revisionPolicy} onValueChange={(value) => setRevisionPolicy(value as typeof revisionPolicy)}>
                <SelectTrigger aria-label="版本策略"><SelectValue /></SelectTrigger>
                <SelectContent><SelectItem value="pinned">固定版本</SelectItem><SelectItem value="follow_latest">跟随最新</SelectItem></SelectContent>
              </Select>
            </label>
            <label className="grid gap-2 text-[12px] font-medium">
              调用策略
              <Select value={invocationPolicy} onValueChange={(value) => setInvocationPolicy(value as typeof invocationPolicy)}>
                <SelectTrigger aria-label="调用策略"><SelectValue /></SelectTrigger>
                <SelectContent><SelectItem value="model_allowed">允许自动选择</SelectItem><SelectItem value="user_only">仅用户显式调用</SelectItem></SelectContent>
              </Select>
            </label>
          </div>
          {revisionPolicy === 'pinned' ? (
            <label className="grid gap-2 text-[12px] font-medium">
              固定修订
              <Select value={pinnedRevisionId} onValueChange={setPinnedRevisionId}>
                <SelectTrigger aria-label="固定修订"><SelectValue placeholder="选择已审核修订" /></SelectTrigger>
                <SelectContent>
                  {availablePinnedRevisions.map((revision) => (
                    <SelectItem key={revision.id} value={revision.id}>v{revision.revision_number} · {revision.status === 'published' ? '当前发布' : '历史可用'}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </label>
          ) : null}
          <section aria-label="修订历史" className="grid gap-2">
            <div className="flex items-center gap-2 text-[13px] font-semibold"><History className="size-4" />修订历史</div>
            {loading ? <p className="text-[12px] text-muted-foreground">正在读取修订…</p> : revisions.map((revision) => (
              <div key={revision.id} className="flex flex-wrap items-center gap-3 rounded-xl border border-border px-4 py-3">
                <div className="min-w-0 flex-1">
                  <p className="text-[13px] font-semibold">v{revision.revision_number} · {revision.status}</p>
                  <p className="mt-1 truncate font-mono text-[11px] text-muted-foreground" title={revision.content_checksum}>{revision.content_checksum}</p>
                  <p className="mt-1 text-[11px] text-muted-foreground">{formatDateTime(revision.published_at || revision.created_at)}</p>
                </div>
                {revision.status === 'superseded' ? <Button variant="outline" size="sm" disabled={saving} onClick={() => void rollback(revision)}>回滚到此版本</Button> : null}
                {['published', 'superseded'].includes(revision.status) ? <Button variant="outline" size="sm" disabled={saving} onClick={() => void revoke(revision)}>撤销</Button> : null}
              </div>
            ))}
          </section>
        </div>
        <div className="flex justify-end gap-2 border-t border-border px-6 py-4">
          <Button variant="outline" onClick={onClose}>取消</Button>
          <Button disabled={saving || loading || !row?.binding_id} onClick={() => void saveBinding()}>{saving ? '保存中…' : '保存策略'}</Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

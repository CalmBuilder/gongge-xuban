import { useCallback, useEffect, useState } from 'react';
import { BookOpen, ChevronRight, Plus, RefreshCw } from 'lucide-react';

import AppHeader from '@/components/AppHeader';
import {
  Dialog,
  DialogContent,
  DialogTitle,
  Input,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Textarea,
} from '@/components/ui';
import { notify } from '@/components/ui/app-toast';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

import { api } from '../api/client';
import type { EnterpriseAuthUser } from '../auth';
import { useEnterpriseContext } from '../enterprise-context';
import type { BusinessCodeItem, BusinessCodeSet } from '../types/organization';

type ItemDraft = {
  code: string;
  name: string;
  description: string;
  status: 'active' | 'inactive';
  sortOrder: string;
};

const EMPTY_DRAFT: ItemDraft = {
  code: '',
  name: '',
  description: '',
  status: 'active',
  sortOrder: '100',
};

export default function ReferenceDataPage({
  currentUser,
  onLogout,
}: {
  currentUser?: EnterpriseAuthUser;
  onLogout?: () => void;
}) {
  const { tenant } = useEnterpriseContext();
  const [codeSets, setCodeSets] = useState<BusinessCodeSet[]>([]);
  const [selectedSetCode, setSelectedSetCode] = useState('');
  const [items, setItems] = useState<BusinessCodeItem[]>([]);
  const [loadingSets, setLoadingSets] = useState(false);
  const [loadingItems, setLoadingItems] = useState(false);
  const [editing, setEditing] = useState<BusinessCodeItem | null>(null);
  const [creating, setCreating] = useState(false);
  const [draft, setDraft] = useState<ItemDraft>(EMPTY_DRAFT);
  const [saving, setSaving] = useState(false);

  const loadSets = useCallback(async () => {
    setLoadingSets(true);
    try {
      const rows = await api.get<BusinessCodeSet[]>(
        `/api/reference-data/code-sets?tenant_id=${encodeURIComponent(tenant.id)}`,
      );
      setCodeSets(rows);
      setSelectedSetCode((current) => (
        rows.some((row) => row.code === current) ? current : rows[0]?.code || ''
      ));
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载码表目录失败');
    } finally {
      setLoadingSets(false);
    }
  }, [tenant.id]);

  const loadItems = useCallback(async () => {
    if (!selectedSetCode) {
      setItems([]);
      return;
    }
    setLoadingItems(true);
    try {
      setItems(await api.get<BusinessCodeItem[]>(
        `/api/reference-data/code-sets/${encodeURIComponent(selectedSetCode)}/items`
        + `?tenant_id=${encodeURIComponent(tenant.id)}`,
      ));
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载码项失败');
    } finally {
      setLoadingItems(false);
    }
  }, [selectedSetCode, tenant.id]);

  useEffect(() => {
    void loadSets();
  }, [loadSets]);

  useEffect(() => {
    void loadItems();
  }, [loadItems]);

  const selectedSet = codeSets.find((row) => row.code === selectedSetCode);

  function openCreate() {
    setDraft(EMPTY_DRAFT);
    setCreating(true);
  }

  function openEdit(item: BusinessCodeItem) {
    setEditing(item);
    setDraft({
      code: item.code,
      name: item.name,
      description: item.description || '',
      status: item.status,
      sortOrder: String(item.sort_order),
    });
  }

  async function save() {
    const code = draft.code.trim();
    const name = draft.name.trim();
    const sortOrder = Number.parseInt(draft.sortOrder, 10);
    if (!selectedSetCode || !name || (!editing && !code)) {
      notify.error('请填写编码和名称');
      return;
    }
    if (!Number.isFinite(sortOrder)) {
      notify.error('排序必须是整数');
      return;
    }
    setSaving(true);
    try {
      const base = `/api/reference-data/code-sets/${encodeURIComponent(selectedSetCode)}/items`;
      if (editing) {
        await api.put(`${base}/${encodeURIComponent(editing.code)}`, {
          tenant_id: tenant.id,
          name,
          description: draft.description.trim() || undefined,
          status: draft.status,
          sort_order: sortOrder,
          revision: editing.revision,
        });
      } else {
        await api.post(base, {
          tenant_id: tenant.id,
          code,
          name,
          description: draft.description.trim() || undefined,
          sort_order: sortOrder,
        });
      }
      notify.success(editing ? '码项已更新' : '码项已创建');
      setEditing(null);
      setCreating(false);
      await loadItems();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '保存码项失败');
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="min-h-full box-border px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]">
      <AppHeader onLogout={onLogout} userName={currentUser?.username} title="数据码表" />
      <section className="mt-[20px] overflow-hidden rounded-[20px] border border-[#e8ebf3] bg-white shadow-[0_16px_44px_rgba(24,39,75,0.07)]">
        <header className="flex items-start justify-between gap-[16px] border-b border-[#eef1f6] px-[22px] py-[20px]">
          <div className="flex items-start gap-[12px]">
            <span className="grid size-[38px] place-items-center rounded-[12px] bg-[#eef3ff] text-[var(--gg-cobalt)]">
              <BookOpen className="size-[18px]" />
            </span>
            <div>
              <h2 className="text-[15px] font-semibold text-[#18181a]">企业业务码表</h2>
              <p className="mt-[4px] text-[12px] leading-[18px] text-[#858b9c]">
                编码创建后不可修改；停用阻止新数据引用，历史记录仍保留原编码。
              </p>
            </div>
          </div>
          <Button
            variant="outline"
            onClick={() => {
              void loadSets();
              void loadItems();
            }}
            disabled={loadingSets || loadingItems}
          >
            <RefreshCw className={cn('size-[14px]', (loadingSets || loadingItems) && 'animate-spin')} />
            刷新
          </Button>
        </header>

        <div className="grid min-h-[500px] grid-cols-[250px_minmax(0,1fr)] max-[760px]:grid-cols-1">
          <nav aria-label="业务码表" className="border-r border-[#eef1f6] bg-[#fbfcff] p-[14px] max-[760px]:border-r-0 max-[760px]:border-b">
            {codeSets.map((codeSet) => (
              <button
                key={codeSet.code}
                type="button"
                aria-current={selectedSetCode === codeSet.code ? 'page' : undefined}
                onClick={() => setSelectedSetCode(codeSet.code)}
                className={cn(
                  'mb-[5px] flex w-full items-center gap-[8px] rounded-[11px] px-[11px] py-[10px] text-left',
                  selectedSetCode === codeSet.code
                    ? 'bg-[#eaf0ff] text-[#3157e8]'
                    : 'text-[#586174] hover:bg-[#f0f3f9]',
                )}
              >
                <span className="min-w-0 flex-1">
                  <strong className="block truncate text-[12px]">{codeSet.name}</strong>
                  <code className="mt-[2px] block truncate text-[9px] opacity-65">{codeSet.code}</code>
                </span>
                <ChevronRight className="size-[13px]" />
              </button>
            ))}
          </nav>

          <main className="p-[20px]">
            <div className="flex items-start justify-between gap-[12px]">
              <div>
                <h3 className="text-[14px] font-semibold text-[#252838]">{selectedSet?.name || '请选择码表'}</h3>
                <p className="mt-[4px] text-[11px] text-[#8b92a3]">{selectedSet?.description}</p>
                {selectedSetCode === 'agent_category' && (
                  <p className="mt-[5px] text-[11px] font-medium text-[#5570bd]">
                    数字员工业务分类用于检索与展示，分类本身不产生权限。
                  </p>
                )}
              </div>
              <Button disabled={!selectedSet?.allow_custom_items} onClick={openCreate}>
                <Plus className="size-[14px]" />
                新增码项
              </Button>
            </div>
            <div className="mt-[16px] grid gap-[9px] md:grid-cols-2 xl:grid-cols-3">
              {items.map((item) => (
                <button
                  key={item.code}
                  type="button"
                  onClick={() => openEdit(item)}
                  className="rounded-[14px] border border-[#e8ebf3] bg-[#fbfcff] p-[14px] text-left transition hover:border-[#b9c8f4] focus-visible:outline-2 focus-visible:outline-[var(--gg-cobalt)]"
                >
                  <span className="flex items-center justify-between gap-[8px]">
                    <strong className="truncate text-[13px] text-[#252838]">{item.name}</strong>
                    <span className={cn(
                      'rounded-full px-[7px] py-[2px] text-[9px]',
                      item.status === 'active' ? 'bg-[#eaf8f0] text-[#18864b]' : 'bg-[#f1f2f5] text-[#7b8190]',
                    )}>
                      {item.status === 'active' ? '启用' : '停用'}
                    </span>
                  </span>
                  <code className="mt-[7px] block text-[10px] text-[#5570bd]">{item.code}</code>
                  <span className="mt-[6px] block text-[10px] text-[#9aa1b5]">
                    {item.is_builtin ? '平台内置' : '企业自定义'} · 排序 {item.sort_order}
                  </span>
                </button>
              ))}
            </div>
          </main>
        </div>
      </section>

      <Dialog open={creating || Boolean(editing)} onOpenChange={(open) => {
        if (!open) {
          setCreating(false);
          setEditing(null);
        }
      }}>
        <DialogContent aria-describedby={undefined} className="sm:max-w-[480px]">
          <DialogTitle>{editing ? `编辑码项：${editing.code}` : `新增${selectedSet?.name || '码项'}`}</DialogTitle>
          <div className="grid gap-[14px]">
            <label className="grid gap-[6px] text-[12px] text-[#646b7d]">
              编码
              <Input value={draft.code} disabled={Boolean(editing)} onChange={(event) => setDraft((value) => ({ ...value, code: event.target.value }))} />
            </label>
            <label className="grid gap-[6px] text-[12px] text-[#646b7d]">
              名称
              <Input value={draft.name} onChange={(event) => setDraft((value) => ({ ...value, name: event.target.value }))} />
            </label>
            <label className="grid gap-[6px] text-[12px] text-[#646b7d]">
              说明
              <Textarea value={draft.description} onChange={(event) => setDraft((value) => ({ ...value, description: event.target.value }))} />
            </label>
            <div className="grid grid-cols-2 gap-[12px]">
              <label className="grid gap-[6px] text-[12px] text-[#646b7d]">
                状态
                <Select value={draft.status} onValueChange={(status) => setDraft((value) => ({ ...value, status: status as ItemDraft['status'] }))}>
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="active">启用</SelectItem>
                    <SelectItem value="inactive">停用</SelectItem>
                  </SelectContent>
                </Select>
              </label>
              <label className="grid gap-[6px] text-[12px] text-[#646b7d]">
                排序
                <Input inputMode="numeric" value={draft.sortOrder} onChange={(event) => setDraft((value) => ({ ...value, sortOrder: event.target.value }))} />
              </label>
            </div>
          </div>
          <div className="mt-[4px] flex justify-end gap-[8px]">
            <Button variant="outline" onClick={() => {
              setCreating(false);
              setEditing(null);
            }}>
              取消
            </Button>
            <Button disabled={saving} onClick={() => void save()}>{saving ? '保存中…' : '保存'}</Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}

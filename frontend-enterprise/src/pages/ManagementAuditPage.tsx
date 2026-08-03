import { useCallback, useEffect, useMemo, useState } from 'react';
import { CheckCircle2, ChevronLeft, ChevronRight, Eye, Search, ShieldCheck, XCircle } from 'lucide-react';

import AppHeader from '@/components/AppHeader';
import {
  Input,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui';
import { notify } from '@/components/ui/app-toast';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

import { api } from '../api/client';
import type { EnterpriseAuthUser } from '../auth';
import { useEnterpriseContext } from '../enterprise-context';
import type {
  ManagementAuditActionKind,
  ManagementAuditLog,
  ManagementAuditOutcome,
  ManagementAuditPageResult,
} from '../types/management-audit';

type AuditFilters = {
  actorUserId: string;
  action: string;
  actionKind: ManagementAuditActionKind | 'all';
  outcome: ManagementAuditOutcome | 'all';
  resourceType: string;
  createdAfter: string;
  createdBefore: string;
};

const EMPTY_FILTERS: AuditFilters = {
  actorUserId: '',
  action: '',
  actionKind: 'all',
  outcome: 'all',
  resourceType: '',
  createdAfter: '',
  createdBefore: '',
};

const OUTCOME_LABELS: Record<ManagementAuditOutcome, string> = {
  success: '成功',
  denied: '拒绝',
  failure: '失败',
};

const ACTION_KIND_LABELS: Record<ManagementAuditActionKind, string> = {
  create: '创建',
  update: '变更',
  delete: '停用/删除',
  read: '读取',
  execute: '执行',
};

export default function ManagementAuditPage({
  currentUser,
  onLogout,
}: {
  currentUser?: EnterpriseAuthUser;
  onLogout?: () => void;
}) {
  const { tenant } = useEnterpriseContext();
  const [draftFilters, setDraftFilters] = useState<AuditFilters>(EMPTY_FILTERS);
  const [filters, setFilters] = useState<AuditFilters>(EMPTY_FILTERS);
  const [rows, setRows] = useState<ManagementAuditLog[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState<ManagementAuditLog | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const pageSize = 20;

  const query = useMemo(() => {
    const params = new URLSearchParams({
      tenant_id: tenant.id,
      page: String(page),
      page_size: String(pageSize),
    });
    if (filters.actorUserId.trim()) params.set('actor_user_id', filters.actorUserId.trim());
    if (filters.action.trim()) params.set('action', filters.action.trim());
    if (filters.actionKind !== 'all') params.set('action_kind', filters.actionKind);
    if (filters.outcome !== 'all') params.set('outcome', filters.outcome);
    if (filters.resourceType.trim()) params.set('resource_type', filters.resourceType.trim());
    if (filters.createdAfter) params.set('created_after', toIsoTime(filters.createdAfter));
    if (filters.createdBefore) params.set('created_before', toIsoTime(filters.createdBefore));
    return params.toString();
  }, [filters, page, tenant.id]);

  const loadRows = useCallback(async () => {
    setLoading(true);
    try {
      const result = await api.get<ManagementAuditPageResult>(
        `/api/management-audit/logs?${query}`,
      );
      setRows(result.items);
      setTotal(result.total);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载管理审计失败');
    } finally {
      setLoading(false);
    }
  }, [query]);

  useEffect(() => {
    void loadRows();
  }, [loadRows]);

  async function openDetail(row: ManagementAuditLog) {
    setSelected(row);
    setDetailOpen(true);
    setDetailLoading(true);
    try {
      setSelected(await api.get<ManagementAuditLog>(
        `/api/management-audit/logs/${encodeURIComponent(row.id)}`
        + `?tenant_id=${encodeURIComponent(tenant.id)}`,
      ));
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载审计详情失败');
    } finally {
      setDetailLoading(false);
    }
  }

  function submitFilters() {
    setPage(1);
    setFilters(draftFilters);
  }

  function resetFilters() {
    setDraftFilters(EMPTY_FILTERS);
    setPage(1);
    setFilters(EMPTY_FILTERS);
  }

  const pageCount = Math.max(1, Math.ceil(total / pageSize));

  return (
    <div className="min-h-full box-border px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]">
      <AppHeader onLogout={onLogout} userName={currentUser?.username} title="管理审计" />
      <section className="mt-[20px] overflow-hidden rounded-[20px] border border-[#e8ebf3] bg-white shadow-[0_16px_44px_rgba(24,39,75,0.07)]">
        <header className="flex flex-wrap items-start justify-between gap-[16px] border-b border-[#eef1f6] px-[22px] py-[20px]">
          <div className="flex items-start gap-[12px]">
            <span className="grid size-[38px] place-items-center rounded-[12px] bg-[#eef3ff] text-[var(--gg-cobalt)]">
              <ShieldCheck className="size-[18px]" />
            </span>
            <div>
              <h2 className="text-[15px] font-semibold text-[#18181a]">企业管理操作台账</h2>
              <p className="mt-[4px] text-[12px] leading-[18px] text-[#858b9c]">
                仅展示当前治理授权范围内的脱敏事实；详情按需读取，不提供批量导出。
              </p>
            </div>
          </div>
          <span className="rounded-full bg-[#f4f6fb] px-[11px] py-[6px] text-[11px] text-[#697084]">
            共 {total} 条
          </span>
        </header>

        <div className="border-b border-[#eef1f6] bg-[#fbfcff] px-[22px] py-[16px]">
          <div className="grid gap-[10px] md:grid-cols-2 xl:grid-cols-4">
            <Input
              aria-label="操作人 ID"
              placeholder="操作人 ID"
              value={draftFilters.actorUserId}
              onChange={(event) => setDraftFilters((value) => ({ ...value, actorUserId: event.target.value }))}
            />
            <Input
              aria-label="操作编码"
              placeholder="操作编码，如 knowledge.read"
              value={draftFilters.action}
              onChange={(event) => setDraftFilters((value) => ({ ...value, action: event.target.value }))}
            />
            <Select
              value={draftFilters.actionKind}
              onValueChange={(value) => setDraftFilters((current) => ({
                ...current,
                actionKind: value as AuditFilters['actionKind'],
              }))}
            >
              <SelectTrigger aria-label="操作类型"><SelectValue placeholder="操作类型" /></SelectTrigger>
              <SelectContent>
                <SelectItem value="all">全部操作类型</SelectItem>
                {Object.entries(ACTION_KIND_LABELS).map(([value, label]) => (
                  <SelectItem key={value} value={value}>{label}</SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Select
              value={draftFilters.outcome}
              onValueChange={(value) => setDraftFilters((current) => ({
                ...current,
                outcome: value as AuditFilters['outcome'],
              }))}
            >
              <SelectTrigger aria-label="结果"><SelectValue placeholder="结果" /></SelectTrigger>
              <SelectContent>
                <SelectItem value="all">全部结果</SelectItem>
                <SelectItem value="success">成功</SelectItem>
                <SelectItem value="denied">拒绝</SelectItem>
                <SelectItem value="failure">失败</SelectItem>
              </SelectContent>
            </Select>
            <Input
              aria-label="资源类型"
              placeholder="资源类型"
              value={draftFilters.resourceType}
              onChange={(event) => setDraftFilters((value) => ({ ...value, resourceType: event.target.value }))}
            />
            <Input
              aria-label="开始时间"
              type="datetime-local"
              value={draftFilters.createdAfter}
              onChange={(event) => setDraftFilters((value) => ({ ...value, createdAfter: event.target.value }))}
            />
            <Input
              aria-label="结束时间"
              type="datetime-local"
              value={draftFilters.createdBefore}
              onChange={(event) => setDraftFilters((value) => ({ ...value, createdBefore: event.target.value }))}
            />
          </div>
          <div className="mt-[12px] flex justify-end gap-[8px]">
            <Button variant="outline" onClick={resetFilters}>重置</Button>
            <Button onClick={submitFilters}>
              <Search className="size-[14px]" />
              查询
            </Button>
          </div>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full min-w-[900px] border-collapse text-left">
            <thead>
              <tr className="bg-[#f7f8fb] text-[11px] font-medium text-[#70778a]">
                <th className="w-[176px] px-[22px] py-[12px]">操作时间</th>
                <th className="px-[14px] py-[12px]">操作人</th>
                <th className="px-[14px] py-[12px]">操作</th>
                <th className="px-[14px] py-[12px]">资源</th>
                <th className="px-[14px] py-[12px]">权限来源</th>
                <th className="w-[88px] px-[14px] py-[12px]">结果</th>
                <th className="w-[96px] px-[14px] py-[12px]">详情</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row, index) => (
                <tr key={row.id} className="border-t border-[#eef1f6] text-[12px] text-[#333746]">
                  <td className="relative px-[22px] py-[15px] align-top">
                    {index < rows.length - 1 && (
                      <span className="absolute top-[28px] bottom-[-16px] left-[28px] w-px bg-[#dde4f4]" />
                    )}
                    <span className="flex items-start gap-[9px]">
                      <span className={cn(
                        'relative z-10 mt-[3px] size-[13px] rounded-full border-[3px] border-white ring-1',
                        row.outcome === 'success' && 'bg-[#25a46b] ring-[#9bd7bd]',
                        row.outcome === 'denied' && 'bg-[#e09b31] ring-[#efca8d]',
                        row.outcome === 'failure' && 'bg-[#d95757] ring-[#eca7a7]',
                      )} />
                      <time className="leading-[18px] text-[#626a7c]">{formatAuditTime(row.created_at)}</time>
                    </span>
                  </td>
                  <td className="px-[14px] py-[15px] align-top">
                    <strong className="block font-medium text-[#252838]">{row.actor_display_name || '系统'}</strong>
                    <span className="mt-[3px] block text-[10px] text-[#969daf]">{row.actor_user_id || row.actor_type}</span>
                  </td>
                  <td className="px-[14px] py-[15px] align-top">
                    <code className="font-medium text-[#3157e8]">{row.action}</code>
                    <span className="mt-[4px] block text-[10px] text-[#969daf]">{ACTION_KIND_LABELS[row.action_kind]}</span>
                  </td>
                  <td className="px-[14px] py-[15px] align-top">
                    <span className="block">{row.resource_type}</span>
                    <code className="mt-[3px] block max-w-[190px] truncate text-[10px] text-[#969daf]">{row.resource_id || '-'}</code>
                  </td>
                  <td className="px-[14px] py-[15px] align-top">
                    <code className="block text-[10px] text-[#596277]">{row.permission_code || '-'}</code>
                    <span className="mt-[3px] block text-[10px] text-[#969daf]">{row.permission_source || '未记录来源'}</span>
                  </td>
                  <td className="px-[14px] py-[15px] align-top">
                    <OutcomeBadge outcome={row.outcome} />
                  </td>
                  <td className="px-[14px] py-[12px] align-top">
                    <Button variant="ghost" size="sm" aria-label="查看详情" onClick={() => void openDetail(row)}>
                      <Eye className="size-[14px]" />
                      查看
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {!loading && rows.length === 0 && (
            <div className="grid min-h-[240px] place-items-center text-[12px] text-[#9aa1b5]">暂无审计记录</div>
          )}
          {loading && rows.length === 0 && (
            <div className="grid min-h-[240px] place-items-center text-[12px] text-[#7c8497]">正在加载审计台账…</div>
          )}
        </div>

        <footer className="flex items-center justify-between border-t border-[#eef1f6] px-[22px] py-[14px] text-[11px] text-[#737b8e]">
          <span>第 {page} / {pageCount} 页</span>
          <div className="flex gap-[6px]">
            <Button variant="outline" size="sm" disabled={page <= 1 || loading} onClick={() => setPage((value) => value - 1)}>
              <ChevronLeft className="size-[14px]" />上一页
            </Button>
            <Button variant="outline" size="sm" disabled={page >= pageCount || loading} onClick={() => setPage((value) => value + 1)}>
              下一页<ChevronRight className="size-[14px]" />
            </Button>
          </div>
        </footer>
      </section>

      <Sheet open={detailOpen} onOpenChange={setDetailOpen}>
        <SheetContent className="w-full overflow-y-auto sm:max-w-[620px]">
          <SheetHeader>
            <SheetTitle>审计详情</SheetTitle>
            <SheetDescription>已在服务端按当前审计范围重新校验，并隐藏凭据、请求正文和私有提示。</SheetDescription>
          </SheetHeader>
          {selected && (
            <div className="space-y-[18px] px-[4px] pb-[28px]">
              <section className="grid grid-cols-2 gap-[10px] rounded-[14px] border border-[#e7eaf2] bg-[#fafbfe] p-[14px] text-[11px]">
                <AuditFact label="操作" value={selected.action} />
                <AuditFact label="结果" value={OUTCOME_LABELS[selected.outcome]} />
                <AuditFact label="请求 ID" value={selected.request_id || '-'} />
                <AuditFact label="关联 ID" value={selected.correlation_id || '-'} />
                <AuditFact label="组织范围" value={selected.target_org_unit_id || '租户级'} />
                <AuditFact label="权限来源" value={selected.permission_source || '-'} />
              </section>
              {detailLoading && <p className="text-[11px] text-[#7d8598]">正在读取详情…</p>}
              <AuditJson title="变更前" value={selected.before} />
              <AuditJson title="变更后" value={selected.after} />
              <AuditJson title="补充事实" value={selected.detail} />
            </div>
          )}
        </SheetContent>
      </Sheet>
    </div>
  );
}

function OutcomeBadge({ outcome }: { outcome: ManagementAuditOutcome }) {
  const Icon = outcome === 'success' ? CheckCircle2 : XCircle;
  return (
    <span className={cn(
      'inline-flex items-center gap-[4px] rounded-full px-[7px] py-[3px] text-[10px] font-medium',
      outcome === 'success' && 'bg-[#eaf8f0] text-[#18864b]',
      outcome === 'denied' && 'bg-[#fff4df] text-[#9a6414]',
      outcome === 'failure' && 'bg-[#fff0f0] text-[#b63d3d]',
    )}>
      <Icon className="size-[11px]" />
      {OUTCOME_LABELS[outcome]}
    </span>
  );
}

function AuditFact({ label, value }: { label: string; value: string }) {
  return (
    <span className="min-w-0">
      <span className="block text-[#949bad]">{label}</span>
      <code className="mt-[4px] block truncate text-[#353a49]" title={value}>{value}</code>
    </span>
  );
}

function AuditJson({ title, value }: { title: string; value: Record<string, unknown> }) {
  return (
    <section>
      <h3 className="mb-[7px] text-[12px] font-semibold text-[#303442]">{title}</h3>
      <pre className="max-h-[240px] overflow-auto rounded-[12px] bg-[#151922] p-[13px] text-[10px] leading-[17px] text-[#d9e0f3]">
        {JSON.stringify(value, null, 2)}
      </pre>
    </section>
  );
}

function formatAuditTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleString('zh-CN', { hour12: false });
}

function toIsoTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toISOString();
}

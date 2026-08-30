import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Eye,
  RefreshCw,
  Search,
  ShieldCheck,
  XCircle,
} from 'lucide-react';

import AppHeader from '@/components/AppHeader';
import { PageShell } from '@/components/enterprise/PageShell';
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
import type {
  DynamicTaskOperationalAlert,
  DynamicTaskOperationalSnapshot,
} from '../types/dynamic-task-operations';

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

const OPERATIONAL_ALERT_LABELS: Record<string, string> = {
  signal_backlog: '唤醒积压',
  dead_letters: '死信',
  unknown_operations: '待对账动作',
  publication_backlog: '投递积压',
  waiting_age_seconds: '最长等待',
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
  const [operations, setOperations] = useState<DynamicTaskOperationalSnapshot | null>(null);
  const [operationsLoading, setOperationsLoading] = useState(false);
  const [operationsDenied, setOperationsDenied] = useState(false);
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

  const loadOperations = useCallback(async () => {
    setOperationsLoading(true);
    try {
      const result = await api.get<DynamicTaskOperationalSnapshot>(
        `/api/dynamic-task-operations/snapshot?tenant_id=${encodeURIComponent(tenant.id)}`,
      );
      setOperations(result);
      setOperationsDenied(false);
    } catch (error) {
      if (isForbidden(error)) {
        setOperationsDenied(true);
        return;
      }
      notify.error(error instanceof Error ? error.message : '加载动态任务运行状态失败');
    } finally {
      setOperationsLoading(false);
    }
  }, [tenant.id]);

  useEffect(() => {
    void loadRows();
  }, [loadRows]);

  useEffect(() => {
    void loadOperations();
  }, [loadOperations]);

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
    <PageShell template="management">
      <AppHeader onLogout={onLogout} userName={currentUser?.username} title="管理审计" />
      <OperationalRail
        snapshot={operations}
        loading={operationsLoading}
        denied={operationsDenied}
        onRefresh={() => void loadOperations()}
      />
      <section className="mt-[20px] overflow-hidden rounded-[20px] border border-[#e8ebf3] bg-white shadow-[0_16px_44px_rgba(24,39,75,0.07)]">
        <header className="flex flex-wrap items-start justify-between gap-[16px] border-b border-[#eef1f6] px-[22px] py-[20px]">
          <div className="flex items-start gap-[12px]">
            <span className="grid size-[38px] place-items-center rounded-[12px] bg-[#eef3ff] text-[var(--gg-cobalt)]">
              <ShieldCheck className="size-[18px]" />
            </span>
            <div>
              <h2 className="gg-type-section-title font-semibold text-[#18181a]">企业管理操作台账</h2>
              <p className="mt-[4px] gg-type-meta  text-[#858b9c]">
                仅展示当前治理授权范围内的脱敏事实；详情按需读取，不提供批量导出。
              </p>
            </div>
          </div>
          <span className="rounded-full bg-[#f4f6fb] px-[11px] py-[6px] gg-type-caption text-[#697084]">
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
              <tr className="bg-[#f7f8fb] gg-type-caption font-medium text-[#70778a]">
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
                <tr key={row.id} className="border-t border-[#eef1f6] gg-type-meta text-[#333746]">
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
                      <time className=" text-[#626a7c]">{formatAuditTime(row.created_at)}</time>
                    </span>
                  </td>
                  <td className="px-[14px] py-[15px] align-top">
                    <strong className="block gg-type-control font-medium text-[#252838]">{row.actor_display_name || '系统'}</strong>
                    <span className="mt-[3px] block gg-type-caption text-[#969daf]">{row.actor_user_id || row.actor_type}</span>
                  </td>
                  <td className="px-[14px] py-[15px] align-top">
                    <code className="gg-type-meta font-medium text-[#3157e8]">{row.action}</code>
                    <span className="mt-[4px] block gg-type-caption text-[#969daf]">{ACTION_KIND_LABELS[row.action_kind]}</span>
                  </td>
                  <td className="px-[14px] py-[15px] align-top">
                    <span className="block">{row.resource_type}</span>
                    <code className="mt-[3px] block max-w-[190px] truncate gg-type-caption text-[#969daf]">{row.resource_id || '-'}</code>
                  </td>
                  <td className="px-[14px] py-[15px] align-top">
                    <code className="block gg-type-caption text-[#596277]">{row.permission_code || '-'}</code>
                    <span className="mt-[3px] block gg-type-caption text-[#969daf]">{row.permission_source || '未记录来源'}</span>
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
            <div className="grid min-h-[240px] place-items-center gg-type-meta text-[#9aa1b5]">暂无审计记录</div>
          )}
          {loading && rows.length === 0 && (
            <div className="grid min-h-[240px] place-items-center gg-type-meta text-[#7c8497]">正在加载审计台账…</div>
          )}
        </div>

        <footer className="flex items-center justify-between border-t border-[#eef1f6] px-[22px] py-[14px] gg-type-caption text-[#737b8e]">
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
              <section className="grid grid-cols-2 gap-[10px] rounded-[14px] border border-[#e7eaf2] bg-[#fafbfe] p-[14px] gg-type-caption">
                <AuditFact label="操作" value={selected.action} />
                <AuditFact label="结果" value={OUTCOME_LABELS[selected.outcome]} />
                <AuditFact label="请求 ID" value={selected.request_id || '-'} />
                <AuditFact label="关联 ID" value={selected.correlation_id || '-'} />
                <AuditFact label="组织范围" value={selected.target_org_unit_id || '租户级'} />
                <AuditFact label="权限来源" value={selected.permission_source || '-'} />
              </section>
              {detailLoading && <p className="gg-type-caption text-[#7d8598]">正在读取详情…</p>}
              <AuditJson title="变更前" value={selected.before} />
              <AuditJson title="变更后" value={selected.after} />
              <AuditJson title="补充事实" value={selected.detail} />
            </div>
          )}
        </SheetContent>
      </Sheet>
    </PageShell>
  );
}

function OperationalRail({
  snapshot,
  loading,
  denied,
  onRefresh,
}: {
  snapshot: DynamicTaskOperationalSnapshot | null;
  loading: boolean;
  denied: boolean;
  onRefresh: () => void;
}) {
  const triggered = snapshot?.alerts.filter((item) => item.triggered) || [];
  const verdict = snapshot?.base_execution_available
        ? { label: '普通动态已开放', className: 'border-[#a9ddc2] bg-[#edf9f2] text-[#197449]' }
        : snapshot?.runtime_capacity_available === false
          ? { label: '运行容量不可用', className: 'border-[#efd29a] bg-[#fff8e8] text-[#8d6118]' }
          : triggered.length > 0
        ? { label: '保持观察', className: 'border-[#efd29a] bg-[#fff8e8] text-[#8d6118]' }
          : { label: '普通动态未开放', className: 'border-[#efd29a] bg-[#fff8e8] text-[#8d6118]' };

  return (
    <section
      aria-label="动态任务运行门禁"
      className="mt-[20px] overflow-hidden rounded-[20px] border border-[#dfe5f1] bg-[#151b2b] text-white shadow-[0_18px_42px_rgba(17,27,51,0.16)]"
    >
      <header className="flex flex-wrap items-center justify-between gap-[12px] border-b border-white/10 px-[22px] py-[17px]">
        <div className="flex items-center gap-[11px]">
          <span className="grid size-[36px] place-items-center rounded-[11px] bg-[#2f5be7] text-white">
            <Activity className="size-[17px]" />
          </span>
          <div>
            <h2 className="gg-type-section-title font-semibold tracking-[0.01em]">动态任务运行门禁</h2>
            <p className="mt-[3px] gg-type-caption text-[#9aa7c4]">
              直接读取统一 Runtime 权威状态；告警只影响高风险灰度，不关闭普通动态能力。
            </p>
          </div>
        </div>
        <div className="flex items-center gap-[9px]">
          {snapshot ? (
            <span className={cn('rounded-full border px-[10px] py-[5px] gg-type-caption font-medium', verdict.className)}>
              {verdict.label}
            </span>
          ) : null}
          <Button
            variant="outline"
            size="sm"
            aria-label="刷新动态任务运行状态"
            disabled={loading}
            onClick={onRefresh}
            className="border-white/15 bg-white/5 text-white hover:bg-white/10 hover:text-white"
          >
            <RefreshCw className={cn('size-[13px]', loading && 'animate-spin')} />
            刷新
          </Button>
        </div>
      </header>

      {snapshot ? (
        <div className="grid gap-px border-b border-white/10 bg-white/10 sm:grid-cols-3">
          <CapabilityReadiness
            label="普通动态"
            value={snapshot.base_execution_available ? '已开放' : '不可用'}
            detail="不受模型配额或告警阈值限制"
            positive={snapshot.base_execution_available}
          />
          <CapabilityReadiness
            label="external write"
            value={snapshot.high_risk_external_write_available ? '独立灰度中' : '默认关闭'}
            detail={snapshot.high_risk_external_write_available ? '仍需逐次授权' : '需单独配置名单、阈值与连接'}
            positive={snapshot.high_risk_external_write_available}
          />
          <CapabilityReadiness
            label="destructive-gray"
            value={snapshot.high_risk_destructive_available ? '隔离验证中' : '默认关闭'}
            detail={snapshot.high_risk_destructive_available ? '仅 disposable/isolated provider' : '不继承 external write 灰度'}
            positive={snapshot.high_risk_destructive_available}
          />
        </div>
      ) : null}

      {denied ? (
        <div className="px-[22px] py-[20px] gg-type-meta text-[#c5cee2]">
          运行聚合需要租户全域审计权限；当前账号仍可查看其组织授权范围内的管理台账。
        </div>
      ) : snapshot ? (
        <div className="grid gap-px bg-white/10 sm:grid-cols-2 xl:grid-cols-5">
          {snapshot.alerts.map((alert) => (
            <OperationalCell key={alert.code} alert={alert} />
          ))}
        </div>
      ) : (
        <div className="px-[22px] py-[20px] gg-type-meta text-[#aeb9d1]">
          {loading ? '正在读取运行快照…' : '尚未取得运行快照，请刷新重试。'}
        </div>
      )}

      {snapshot ? (
        <footer className="flex flex-wrap items-center justify-between gap-[8px] border-t border-white/10 px-[22px] py-[11px] gg-type-caption text-[#8996b4]">
          <span>
            活动执行 {activeExecutions(snapshot)} · 待处理 {activeAttentions(snapshot)} ·
            运行容量租约 {activeQuotaLeases(snapshot)} · 最长等待 {formatDuration(snapshot.oldest_waiting_age_seconds)}
          </span>
          <time dateTime={snapshot.observed_at}>快照时间 {formatAuditTime(snapshot.observed_at)}</time>
        </footer>
      ) : null}
    </section>
  );
}

function CapabilityReadiness({
  label,
  value,
  detail,
  positive,
}: {
  label: string;
  value: string;
  detail: string;
  positive: boolean;
}) {
  return (
    <div className="bg-[#151b2b] px-[18px] py-[13px]">
      <div className="gg-type-caption font-medium tracking-[0.08em] text-[#8996b4]">{label}</div>
      <strong className={cn('mt-[5px] block gg-type-meta font-semibold', positive ? 'text-[#8ce0b1]' : 'text-[#f0c36b')}>
        {value}
      </strong>
      <span className="mt-[3px] block gg-type-caption text-[#6f7c9b]">{detail}</span>
    </div>
  );
}

function OperationalCell({ alert }: { alert: DynamicTaskOperationalAlert }) {
  const value = alert.code === 'waiting_age_seconds' ? formatDuration(alert.current) : String(alert.current);
  const threshold = alert.threshold === null
    ? '待配置'
    : alert.code === 'waiting_age_seconds'
      ? formatDuration(alert.threshold)
      : String(alert.threshold);
  return (
    <div className="bg-[#151b2b] px-[18px] py-[16px]">
      <div className="flex items-center justify-between gap-[8px]">
        <span className="gg-type-caption font-medium tracking-[0.08em] text-[#8996b4]">
          {OPERATIONAL_ALERT_LABELS[alert.code] || alert.code}
        </span>
        {alert.triggered ? (
          <AlertTriangle className={cn('size-[13px]', alert.severity === 'critical' ? 'text-[#ff8b8b]' : 'text-[#f0c36b]')} />
        ) : null}
      </div>
      <strong className={cn(
        'mt-[9px] block font-mono gg-type-section-title font-semibold',
        alert.triggered && alert.severity === 'critical' ? 'text-[#ff9b9b]' : 'text-white',
      )}>
        {value}
      </strong>
      <span className="mt-[7px] block gg-type-caption text-[#6f7c9b]">停止阈值 {threshold}</span>
    </div>
  );
}

function activeExecutions(snapshot: DynamicTaskOperationalSnapshot): number {
  return ['created', 'running', 'waiting'].reduce((total, status) => total + (snapshot.executions[status] || 0), 0);
}

function activeAttentions(snapshot: DynamicTaskOperationalSnapshot): number {
  return ['offered', 'claimed'].reduce((total, status) => total + (snapshot.attentions[status] || 0), 0);
}

function activeQuotaLeases(snapshot: DynamicTaskOperationalSnapshot): number {
  return Object.values(snapshot.quota_leases).reduce((total, count) => total + count, 0);
}

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}秒`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}分`;
  return `${Math.floor(seconds / 3600)}时${Math.floor((seconds % 3600) / 60)}分`;
}

function isForbidden(error: unknown): boolean {
  return typeof error === 'object' && error !== null && 'status' in error && error.status === 403;
}

function OutcomeBadge({ outcome }: { outcome: ManagementAuditOutcome }) {
  const Icon = outcome === 'success' ? CheckCircle2 : XCircle;
  return (
    <span className={cn(
      'inline-flex items-center gap-[4px] rounded-full px-[7px] py-[3px] gg-type-caption font-medium',
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
      <span className="block gg-type-caption text-[#949bad]">{label}</span>
      <code className="mt-[4px] block truncate gg-type-code text-[#353a49]" title={value}>{value}</code>
    </span>
  );
}

function AuditJson({ title, value }: { title: string; value: Record<string, unknown> }) {
  return (
    <section>
      <h3 className="mb-[7px] gg-type-card-title font-semibold text-[#303442]">{title}</h3>
      <pre className="max-h-[240px] overflow-auto rounded-[12px] bg-[#151922] p-[13px] gg-type-caption  text-[#d9e0f3]">
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

/**
 * @Time       : 2026/08/29 16:45
 * @Author     : zhanglp8181
 * @File       : BuiltinSkillCatalogPage.tsx
 * @CallChain  : Skill 管理入口 → 筛选/详情/导入/审核 → 内置 Skill 目录 API
 * @Description: 展示项目自有 Skill 快照的来源、风险、审核状态和详情，不直接执行 Skill。
 */

import { useCallback, useEffect, useMemo, useState, type FormEvent, type ReactNode } from 'react';
import { ExternalLink, RefreshCw, Search, ShieldAlert, Sparkles } from 'lucide-react';
import { Link, useParams, useSearchParams } from 'react-router-dom';

import AppHeader from '@/components/AppHeader';
import { EnterpriseCatalogHero, EnterpriseCatalogPageHeader } from '@/components/EnterpriseCatalogHeader';
import { CatalogGrid } from '@/components/enterprise/CatalogGrid';
import { DetailSurface } from '@/components/enterprise/DetailSurface';
import { PageHeader } from '@/components/enterprise/PageHeader';
import { PageShell } from '@/components/enterprise/PageShell';
import { Paginator } from '@/components/Paginator';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Checkbox,
  Input,
  Label,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Textarea,
} from '@/components/ui';
import { Button } from '@/components/ui/button';
import { notify } from '@/components/ui/app-toast';
import {
  DIALOG_CANCEL_BUTTON_CLASS,
  DIALOG_PRIMARY_BUTTON_CLASS,
  DETAIL_FACT_CARD_CLASS,
  DETAIL_PANEL_CLASS,
  RESOURCE_CARD_ICON_SLOT_CLASS,
  RESOURCE_CARD_CLASS,
  RESOURCE_CARD_DESCRIPTION_CLASS,
  RESOURCE_CARD_FOOTER_CLASS,
  RESOURCE_CARD_IDENTITY_CLASS,
  SELECT_TRIGGER_CLASS,
} from '@/lib/enterprise-ui';
import { cn } from '@/lib/utils';

import { ApiError, api, getRequestTenantId } from '../api/client';
import { isEnterpriseAdmin, type EnterpriseAuthUser } from '../auth';
import type {
  BuiltinSkillCatalogDetail,
  BuiltinSkillCatalogBindingRequest,
  BuiltinSkillCatalogBindingResult,
  BuiltinSkillCatalogLifecycleAction,
  BuiltinSkillCatalogLifecycleRequest,
  BuiltinSkillCatalogLifecycleResult,
  BuiltinSkillCatalogImportResult,
  BuiltinSkillCatalogItem,
  BuiltinSkillCatalogPage as BuiltinSkillCatalogPageResult,
  ExternalSkillCatalogImportRequest,
  ExternalSkillCatalogImportResult,
  ExternalSkillCatalogSourceKind,
  BuiltinSkillCatalogReviewDecision,
  BuiltinSkillCatalogReviewRequest,
  BuiltinSkillCatalogReviewResult,
} from '../types/general-skill-catalog';
import type { AgentProfileRead } from '../types';

const PAGE_SIZE = 12;
const INITIAL_IMPORT_COMMAND_ID = 'builtin-skill-initial-6654f6b6';

const EXTERNAL_SOURCE_LABELS: Record<ExternalSkillCatalogSourceKind, string> = {
  github: 'GitHub 仓库',
  https: '受限 HTTPS 压缩包',
  skillhub: 'SkillHub / ClawHub',
};

const RISK_LABELS: Record<BuiltinSkillCatalogItem['risk_level'], string> = {
  low: '低风险',
  medium: '需复核',
  high: '高风险',
};

const STATUS_LABELS: Record<BuiltinSkillCatalogItem['status'], string> = {
  draft: '待审核',
  published: '已发布',
  rejected: '已拒绝',
  archived: '已归档',
};

const STABILITY_LABELS: Record<BuiltinSkillCatalogItem['stability'], string> = {
  stable: '稳定',
  beta: '测试中',
  misc: '未分类',
};

const SOURCE_LABELS: Record<string, string> = {
  platform_builtin: '项目内置快照',
  github: 'GitHub',
  https: '受限 HTTPS',
  skillhub: 'SkillHub / ClawHub',
};

type CatalogFilters = {
  search: string;
  category: string;
  sourceKind: string;
  stability: string;
  riskLevel: string;
  invocationPolicy: string;
  status: string;
};

const EMPTY_FILTERS: CatalogFilters = {
  search: '',
  category: '',
  sourceKind: '',
  stability: '',
  riskLevel: '',
  invocationPolicy: '',
  status: '',
};

export default function BuiltinSkillCatalogPage({
  currentUser,
  onLogout,
}: {
  currentUser?: EnterpriseAuthUser;
  onLogout?: () => void;
}) {
  const { slug } = useParams();
  if (slug) {
    return <BuiltinSkillDetailPage currentUser={currentUser} onLogout={onLogout} slug={slug} />;
  }
  return <BuiltinSkillCatalogListPage currentUser={currentUser} onLogout={onLogout} />;
}

function BuiltinSkillCatalogListPage({
  currentUser,
  onLogout,
}: {
  currentUser?: EnterpriseAuthUser;
  onLogout?: () => void;
}) {
  const [searchParams, setSearchParams] = useSearchParams();
  const [draftFilters, setDraftFilters] = useState<CatalogFilters>(() => readFilters(searchParams));
  const [filters, setFilters] = useState<CatalogFilters>(() => readFilters(searchParams));
  const [result, setResult] = useState<BuiltinSkillCatalogPageResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [importing, setImporting] = useState(false);
  const [externalDialogOpen, setExternalDialogOpen] = useState(false);
  const [externalImporting, setExternalImporting] = useState(false);
  const [selectedSkillIds, setSelectedSkillIds] = useState<Set<string>>(new Set());
  const [selectedSkillSnapshots, setSelectedSkillSnapshots] = useState<Map<string, BuiltinSkillCatalogItem>>(new Map());
  const [reviewDialogOpen, setReviewDialogOpen] = useState(false);
  const [reviewDecision, setReviewDecision] = useState<BuiltinSkillCatalogReviewDecision>('approve');
  const [reviewNote, setReviewNote] = useState('');
  const [reviewing, setReviewing] = useState(false);
  const [page, setPage] = useState(() => readPage(searchParams));
  const isAdmin = isEnterpriseAdmin(currentUser);

  const query = useMemo(() => {
    const params = new URLSearchParams({
      tenant_id: getRequestTenantId(),
      page: String(page),
      page_size: String(PAGE_SIZE),
    });
    if (filters.search.trim()) params.set('search', filters.search.trim());
    if (filters.category) params.set('category', filters.category);
    if (filters.sourceKind) params.set('source_kind', filters.sourceKind);
    if (filters.stability) params.set('stability', filters.stability);
    if (filters.riskLevel) params.set('risk_level', filters.riskLevel);
    if (filters.invocationPolicy) params.set('invocation_policy', filters.invocationPolicy);
    if (filters.status) params.set('status', filters.status);
    return params;
  }, [filters, page]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setResult(await api.get<BuiltinSkillCatalogPageResult>(
        `/api/enterprise/general-skill-catalog?${query.toString()}`,
      ));
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载内置 Skill 目录失败');
    } finally {
      setLoading(false);
    }
  }, [query]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    const next = new URLSearchParams();
    Object.entries(filters).forEach(([key, value]) => {
      if (value) next.set(
        key === 'riskLevel'
          ? 'risk_level'
          : key === 'invocationPolicy'
            ? 'invocation_policy'
            : key === 'sourceKind'
              ? 'source_kind'
              : key,
        value,
      );
    });
    if (page > 1) next.set('page', String(page));
    setSearchParams(next, { replace: true });
  }, [filters, page, setSearchParams]);

  function submitFilters() {
    setPage(1);
    setFilters({ ...draftFilters });
  }

  function resetFilters() {
    setDraftFilters(EMPTY_FILTERS);
    setFilters(EMPTY_FILTERS);
    setPage(1);
  }

  async function importSnapshot() {
    if (!isAdmin) return;
    setImporting(true);
    try {
      const response = await api.post<BuiltinSkillCatalogImportResult>(
        '/api/enterprise/general-skill-catalog/import',
        { tenant_id: getRequestTenantId(), command_id: INITIAL_IMPORT_COMMAND_ID },
      );
      notify.success(response.replayed ? '内置 Skill 快照已核对，无需重复导入' : `已导入 ${response.created_count} 个候选 Skill`);
      await load();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '导入内置 Skill 快照失败');
    } finally {
      setImporting(false);
    }
  }

  async function importExternal(form: ExternalSkillImportForm) {
    if (!isAdmin) return;
    setExternalImporting(true);
    const payload: ExternalSkillCatalogImportRequest = {
      tenant_id: getRequestTenantId(),
      command_id: createExternalImportCommandId(),
      source_kind: form.source_kind,
      source_url: form.source_url.trim(),
      source_license: form.source_license.trim(),
      revision: form.source_kind === 'github' ? form.revision.trim() || null : null,
      source_subpath: form.source_kind === 'github' ? form.source_subpath.trim() || null : null,
    };
    try {
      const response = await api.post<ExternalSkillCatalogImportResult>(
        '/api/enterprise/general-skill-catalog/import-external',
        payload,
      );
      notify.success(response.replayed ? '外部 Skill 导入命令已重放' : `已导入 ${response.created_count} 个待审核 Skill`);
      setExternalDialogOpen(false);
      await load();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '导入外部 Skill 失败');
    } finally {
      setExternalImporting(false);
    }
  }

  function toggleSkillSelection(skillId: string, checked: boolean) {
    setSelectedSkillIds((current) => {
      const next = new Set(current);
      if (checked) next.add(skillId);
      else next.delete(skillId);
      return next;
    });
    setSelectedSkillSnapshots((current) => {
      const next = new Map(current);
      const snapshot = result?.items.find((item) => item.id === skillId);
      if (checked && snapshot) next.set(skillId, snapshot);
      else if (!checked) next.delete(skillId);
      return next;
    });
  }

  function selectPageCandidates() {
    const candidates = result?.items.filter((item) => item.status === 'draft') || [];
    setSelectedSkillIds((current) => {
      const next = new Set(current);
      candidates.forEach((item) => next.add(item.id));
      return next;
    });
    setSelectedSkillSnapshots((current) => {
      const next = new Map(current);
      candidates.forEach((item) => next.set(item.id, item));
      return next;
    });
  }

  function openReview(decision: BuiltinSkillCatalogReviewDecision) {
    if (!selectedSkillIds.size) return;
    setReviewDecision(decision);
    setReviewNote('');
    setReviewDialogOpen(true);
  }

  async function submitReview() {
    if (!isAdmin || !result || !selectedSkillIds.size) return;
    const selectedItems = [...selectedSkillIds]
      .map((skillId) => selectedSkillSnapshots.get(skillId))
      .filter((item): item is BuiltinSkillCatalogItem => item !== undefined)
      .filter((item) => item.status === 'draft');
    if (!selectedItems.length) return;
    setReviewing(true);
    const payload: BuiltinSkillCatalogReviewRequest = {
      tenant_id: getRequestTenantId(),
      command_id: createCatalogCommandId('catalog-review'),
      items: selectedItems.map((item) => ({
        skill_id: item.id,
        decision: reviewDecision,
        expected_skill_row_version: item.row_version,
        expected_revision_row_version: item.revision_row_version || 1,
        review_note: reviewNote.trim() || null,
      })),
    };
    try {
      const response = await api.post<BuiltinSkillCatalogReviewResult>(
        '/api/enterprise/general-skill-catalog/review',
        payload,
      );
      notify.success(response.replayed ? '审核命令已重放' : `已完成 ${selectedItems.length} 条 Skill 审核`);
      setSelectedSkillIds(new Set());
      setSelectedSkillSnapshots(new Map());
      setReviewDialogOpen(false);
      await load();
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        setSelectedSkillIds(new Set());
        setSelectedSkillSnapshots(new Map());
        setReviewDialogOpen(false);
        notify.error('候选版本已变化，审核未提交；目录已刷新，请重新选择');
        await load();
      } else {
        notify.error(error instanceof Error ? error.message : '批量审核 Skill 失败');
      }
    } finally {
      setReviewing(false);
    }
  }

  const pageCount = Math.max(1, Math.ceil((result?.total || 0) / PAGE_SIZE));
  const facets = result?.facets;

  return (
    <PageShell template="catalog" aria-busy={loading}>
      <EnterpriseCatalogPageHeader
        backTo="/enterprise/general-skills"
        backLabel="返回 Skill 管理"
        title="Skill 管理"
        description="项目自有 Skill 快照 · 先审核，再发布、安装或绑定"
        onLogout={onLogout}
        userName={currentUser?.username}
      />

      <section className="mt-[20px] overflow-hidden rounded-[var(--gg-radius-panel)] border border-[var(--gg-line)] bg-[var(--gg-surface)] shadow-[var(--gg-shadow-card)]">
        <EnterpriseCatalogHero
          icon={Sparkles}
          title="内置能力目录"
          description="当前快照只作为审核候选保存，脚本不会因为进入目录而自动运行。已发布后，能力分身和组织数字员工都可按授权使用。"
          actions={(
            <>
              <Button variant="outline" className="h-[34px] rounded-[10px]" onClick={() => void load()} disabled={loading}>
                <RefreshCw className={cn('size-[14px]', loading && 'animate-spin')} />
                刷新
              </Button>
              {isAdmin && (
                <>
                  <Button variant="outline" className="h-[34px] rounded-[10px]" onClick={() => setExternalDialogOpen(true)} disabled={externalImporting}>
                    <ExternalLink className="size-[14px]" />
                    导入外部 Skill
                  </Button>
                  <Button className="h-[34px] rounded-[10px] bg-[var(--gg-cobalt)]" onClick={() => void importSnapshot()} disabled={importing}>
                    <RefreshCw className={cn('size-[14px]', importing && 'animate-spin')} />
                    {importing ? '核对中…' : '核对内置快照'}
                  </Button>
                </>
              )}
            </>
          )}
        />

        <div className="grid gap-[12px] border-b border-[#eef1f6] px-[22px] py-[18px] md:grid-cols-[minmax(220px,1.6fr)_repeat(5,minmax(120px,1fr))]">
          <label className="relative block">
            <span className="sr-only">搜索 Skill</span>
            <Search className="pointer-events-none absolute left-[11px] top-1/2 size-[14px] -translate-y-1/2 text-[#939bad]" />
            <Input
              value={draftFilters.search}
              aria-label="搜索 Skill"
              name="catalog-search"
              autoComplete="off"
              placeholder="搜索名称或说明…"
              className="h-[34px] pl-[32px]"
              onChange={(event) => setDraftFilters((current) => ({ ...current, search: event.target.value }))}
              onKeyDown={(event) => { if (event.key === 'Enter') submitFilters(); }}
            />
          </label>
          <CatalogSelect label="来源" value={draftFilters.sourceKind} onChange={(value) => setDraftFilters((current) => ({ ...current, sourceKind: value }))} options={Object.entries(SOURCE_LABELS).map(([value, label]) => ({ value, label }))} />
          <CatalogSelect label="稳定性" value={draftFilters.stability} onChange={(value) => setDraftFilters((current) => ({ ...current, stability: value }))} options={Object.entries(STABILITY_LABELS).map(([value, label]) => ({ value, label }))} />
          <CatalogSelect label="风险" value={draftFilters.riskLevel} onChange={(value) => setDraftFilters((current) => ({ ...current, riskLevel: value }))} options={Object.entries(RISK_LABELS).map(([value, label]) => ({ value, label }))} />
          <CatalogSelect label="调用方式" value={draftFilters.invocationPolicy} onChange={(value) => setDraftFilters((current) => ({ ...current, invocationPolicy: value }))} options={[{ value: 'model_allowed', label: '模型可选' }, { value: 'user_only', label: '仅用户触发' }]} />
          <CatalogSelect label="状态" value={draftFilters.status} onChange={(value) => setDraftFilters((current) => ({ ...current, status: value }))} options={Object.entries(STATUS_LABELS).map(([value, label]) => ({ value, label }))} />
          <div className="flex items-center justify-end gap-[8px] md:col-span-6">
            <Button variant="outline" className="h-[32px] rounded-[9px] gg-type-meta" onClick={resetFilters}>重置</Button>
            <Button className="h-[32px] rounded-[9px] bg-[var(--gg-cobalt)] gg-type-meta" onClick={submitFilters}>查询</Button>
          </div>
        </div>

        {isAdmin && (selectedSkillIds.size > 0 || result?.items.some((item) => item.status === 'draft')) && (
          <div className="flex flex-wrap items-center justify-between gap-[10px] border-b border-[#eef1f6] bg-[#fbfcff] px-[22px] py-[10px]" role="region" aria-label="批量审核工具">
            <div className="flex items-center gap-[10px] gg-type-caption text-[var(--gg-slate)]" role="status">
              <span>已选 {selectedSkillIds.size} 个待审核 Skill</span>
              <Button variant="ghost" className="h-[28px] rounded-[8px] px-[8px] gg-type-caption text-[var(--gg-cobalt)]" onClick={selectPageCandidates}>
                全选当前页待审
              </Button>
            </div>
            <div className="flex items-center gap-[8px]">
              <Button variant="outline" className="h-[30px] rounded-[9px] gg-type-caption" onClick={() => openReview('reject')} disabled={!selectedSkillIds.size || reviewing}>
                批量拒绝
              </Button>
              <Button className="h-[30px] rounded-[9px] bg-[var(--gg-cobalt)] gg-type-caption" onClick={() => openReview('approve')} disabled={!selectedSkillIds.size || reviewing}>
                批量通过
              </Button>
            </div>
          </div>
        )}

        <div className="flex flex-wrap items-center justify-between gap-[10px] px-[22px] py-[15px]">
          <p className="gg-type-meta text-[var(--gg-slate)]" role="status">
            {loading ? '正在读取目录…' : `共 ${result?.total || 0} 个 Skill`}
          </p>
              <div className="gg-type-caption flex flex-wrap gap-[6px]">
            {facets && Object.entries(facets.risk_level).map(([key, value]) => (
                  <span key={key} className="rounded-[var(--gg-radius-control)] bg-[var(--gg-state-neutral-soft)] px-[9px] py-[5px]">{RISK_LABELS[key as BuiltinSkillCatalogItem['risk_level']] || key} {value}</span>
            ))}
          </div>
        </div>

        {result?.items.length ? (
          <CatalogGrid family="resource" className="px-[22px] pb-[22px]">
            {result.items.map((item) => (
              <CatalogCard
                key={item.id}
                item={item}
                selectable={isAdmin && item.status === 'draft'}
                selected={selectedSkillIds.has(item.id)}
                onSelect={(checked) => toggleSkillSelection(item.id, checked)}
              />
            ))}
          </CatalogGrid>
        ) : (
          <div className="mx-[22px] mb-[22px] rounded-[14px] border border-dashed border-[#dfe4ef] bg-[#fbfcff] px-[18px] py-[32px] text-center gg-type-control text-[var(--gg-slate)]">
            {loading ? '正在加载…' : isAdmin ? '暂无候选。可点击“核对内置快照”导入固定项目资产。' : '当前没有已发布的内置 Skill。'}
          </div>
        )}
        <Paginator page={page} pageCount={pageCount} onChange={setPage} aria-label="内置 Skill 目录分页" className="mb-[22px]" />
      </section>
      {isAdmin && (
        <ExternalSkillImportDialog
          open={externalDialogOpen}
          submitting={externalImporting}
          onOpenChange={setExternalDialogOpen}
          onSubmit={importExternal}
        />
      )}
      {isAdmin && (
        <CatalogReviewDialog
          open={reviewDialogOpen}
          decision={reviewDecision}
          note={reviewNote}
          selectedCount={selectedSkillIds.size}
          submitting={reviewing}
          onOpenChange={setReviewDialogOpen}
          onDecisionChange={setReviewDecision}
          onNoteChange={setReviewNote}
          onSubmit={() => void submitReview()}
        />
      )}
    </PageShell>
  );
}

type ExternalSkillImportForm = {
  source_kind: ExternalSkillCatalogSourceKind;
  source_url: string;
  source_license: string;
  revision: string;
  source_subpath: string;
};

function ExternalSkillImportDialog({
  open,
  submitting,
  onOpenChange,
  onSubmit,
}: {
  open: boolean;
  submitting: boolean;
  onOpenChange: (open: boolean) => void;
  onSubmit: (form: ExternalSkillImportForm) => Promise<void>;
}) {
  const [form, setForm] = useState<ExternalSkillImportForm>(emptyExternalSkillImportForm());
  const githubSource = form.source_kind === 'github';
  const valid = form.source_url.trim().length > 0
    && form.source_license.trim().length > 0
    && (!githubSource || /^[a-fA-F0-9]{40}$/.test(form.revision.trim()))
    && (!githubSource || form.source_subpath.trim().length > 0);

  function update<K extends keyof ExternalSkillImportForm>(key: K, value: ExternalSkillImportForm[K]) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  function handleOpenChange(nextOpen: boolean) {
    if (!nextOpen && submitting) return;
    onOpenChange(nextOpen);
    if (nextOpen) setForm(emptyExternalSkillImportForm());
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!valid || submitting) return;
    await onSubmit(form);
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="max-w-[640px] gap-0 overflow-hidden p-0">
        <DialogHeader className="border-b border-[#eef1f6] bg-[linear-gradient(110deg,#f2f6ff_0%,#fbfcff_64%,#f4fbf8_100%)] px-[24px] py-[20px]">
          <DialogTitle className="gg-type-card-title text-[var(--gg-ink)]">导入外部 Skill</DialogTitle>
          <DialogDescription className="max-w-[540px] gg-type-meta  text-[var(--gg-slate)]">
            只接收固定来源并生成待审核候选，不会自动发布、绑定或运行。导入后的内容进入项目 Skill 库；当前租户仅作为操作者和审计上下文。
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={(event) => void submit(event)}>
          <div className="grid max-h-[min(68vh,560px)] gap-[16px] overflow-y-auto px-[24px] py-[20px]">
            <div className="grid gap-[7px]">
              <Label htmlFor="external-skill-source-kind" className="gg-type-meta text-[var(--gg-ink)]">来源类型</Label>
              <Select value={form.source_kind} onValueChange={(value) => update('source_kind', value as ExternalSkillCatalogSourceKind)}>
                <SelectTrigger id="external-skill-source-kind" aria-label="来源类型" className={cn(SELECT_TRIGGER_CLASS, 'w-full gg-type-meta')}>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {Object.entries(EXTERNAL_SOURCE_LABELS).map(([value, label]) => (
                    <SelectItem key={value} value={value}>{label}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="grid gap-[7px]">
              <Label htmlFor="external-skill-source-url" className="gg-type-meta text-[var(--gg-ink)]">仓库、Skill 标识或压缩包地址</Label>
              <Input
                id="external-skill-source-url"
                name="source_url"
                type={form.source_kind === 'skillhub' ? 'text' : 'url'}
                autoComplete="url"
                value={form.source_url}
                onChange={(event) => update('source_url', event.target.value)}
                placeholder={form.source_kind === 'github'
                  ? 'https://github.com/owner/repository'
                  : form.source_kind === 'skillhub' ? 'skill-slug 或 SkillHub 页面地址' : 'https://…'}
                aria-required="true"
              />
              <p className="gg-type-caption  text-[#858b9c]">
                {form.source_kind === 'github'
                  ? '只接受 github.com 仓库地址，并按完整 commit 固定归档。'
                  : form.source_kind === 'skillhub'
                    ? '可填写 SkillHub / ClawHub Skill 标识或其页面地址。'
                    : 'HTTPS 地址必须命中服务端管理员配置的来源主机白名单。'}
              </p>
            </div>
            {githubSource && (
              <div className="grid gap-[16px] md:grid-cols-[minmax(0,1.2fr)_minmax(0,1fr)]">
                <div className="grid gap-[7px]">
                  <Label htmlFor="external-skill-revision" className="gg-type-meta text-[var(--gg-ink)]">完整 commit SHA</Label>
                  <Input
                    id="external-skill-revision"
                    name="revision"
                    value={form.revision}
                    onChange={(event) => update('revision', event.target.value)}
                    placeholder="40 位 commit SHA…"
                    className="font-mono gg-type-caption"
                    pattern="[a-fA-F0-9]{40}"
                    aria-required="true"
                  />
                </div>
                <div className="grid gap-[7px]">
                  <Label htmlFor="external-skill-subpath" className="gg-type-meta text-[var(--gg-ink)]">Skill 子路径</Label>
                  <Input
                    id="external-skill-subpath"
                    name="source_subpath"
                    value={form.source_subpath}
                    onChange={(event) => update('source_subpath', event.target.value)}
                    placeholder="skills/productivity/example"
                    className="font-mono gg-type-caption"
                    aria-required="true"
                  />
                </div>
              </div>
            )}
            <div className="grid gap-[7px]">
              <Label htmlFor="external-skill-license" className="gg-type-meta text-[var(--gg-ink)]">许可证证据</Label>
              <Input
                id="external-skill-license"
                name="source_license"
                value={form.source_license}
                onChange={(event) => update('source_license', event.target.value)}
                placeholder="例如 MIT、Apache-2.0…"
                aria-required="true"
              />
            </div>
            <div className="rounded-[12px] border border-[#dbe5ff] bg-[#f6f8ff] px-[13px] py-[11px] gg-type-caption  text-[#5d6880]" role="note">
              导入会保存来源、版本、许可证和内容 checksum；外部包中的脚本仍按 Skill 资源处理，不会因此获得进程、网络或任意工具执行权限。
            </div>
          </div>
          <DialogFooter className="border-t border-[var(--gg-line)] bg-[var(--gg-surface)] px-[24px] py-[14px]">
            <Button type="button" variant="outline" className={DIALOG_CANCEL_BUTTON_CLASS} onClick={() => handleOpenChange(false)} disabled={submitting}>取消</Button>
            <Button type="submit" className={DIALOG_PRIMARY_BUTTON_CLASS} disabled={!valid || submitting}>
              {submitting ? '导入中…' : '导入为待审核候选'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function emptyExternalSkillImportForm(): ExternalSkillImportForm {
  return {
    source_kind: 'github',
    source_url: '',
    source_license: '',
    revision: '',
    source_subpath: '',
  };
}

function CatalogReviewDialog({
  open,
  decision,
  note,
  selectedCount,
  submitting,
  onOpenChange,
  onDecisionChange,
  onNoteChange,
  onSubmit,
}: {
  open: boolean;
  decision: BuiltinSkillCatalogReviewDecision;
  note: string;
  selectedCount: number;
  submitting: boolean;
  onOpenChange: (open: boolean) => void;
  onDecisionChange: (decision: BuiltinSkillCatalogReviewDecision) => void;
  onNoteChange: (note: string) => void;
  onSubmit: () => void;
}) {
  const approving = decision === 'approve';
  return (
    <Dialog open={open} onOpenChange={(nextOpen) => { if (!submitting) onOpenChange(nextOpen); }}>
      <DialogContent className="max-w-[520px] gap-0 overflow-hidden p-0">
        <DialogHeader className="border-b border-[#eef1f6] bg-[#fbfcff] px-[24px] py-[20px]">
          <DialogTitle className="gg-type-card-title text-[var(--gg-ink)]">批量审核 Skill</DialogTitle>
          <DialogDescription className="gg-type-meta  text-[var(--gg-slate)]">
            将对 {selectedCount} 个候选执行原子审核。只有通过审核的版本才会出现在开放广场的 Skill 分类。
          </DialogDescription>
        </DialogHeader>
        <div className="grid gap-[16px] px-[24px] py-[20px]">
          <div className="grid gap-[8px]">
            <span className="gg-type-meta font-medium text-[var(--gg-ink)]">审核决定</span>
            <div className="grid grid-cols-2 gap-[8px]" role="radiogroup" aria-label="审核决定">
              <Button
                type="button"
                variant={approving ? 'default' : 'outline'}
                aria-pressed={approving}
                className={cn('h-[34px] rounded-[9px] gg-type-meta', approving && 'bg-[var(--gg-cobalt)]')}
                onClick={() => onDecisionChange('approve')}
                disabled={submitting}
              >
                通过并发布到广场
              </Button>
              <Button
                type="button"
                variant={!approving ? 'destructive' : 'outline'}
                aria-pressed={!approving}
                className="h-[34px] rounded-[9px] gg-type-meta"
                onClick={() => onDecisionChange('reject')}
                disabled={submitting}
              >
                拒绝并归档
              </Button>
            </div>
          </div>
          <div className="grid gap-[7px]">
            <Label htmlFor="catalog-review-note" className="gg-type-meta text-[var(--gg-ink)]">审核说明（可选）</Label>
            <Textarea
              id="catalog-review-note"
              name="review_note"
              value={note}
              onChange={(event) => onNoteChange(event.target.value)}
              placeholder="记录来源、风险或版本判断依据…"
              className="min-h-[92px] gg-type-meta"
              maxLength={2000}
              disabled={submitting}
            />
          </div>
          <div className="rounded-[12px] border border-[#f2dfbd] bg-[#fffaf0] px-[13px] py-[11px] gg-type-caption  text-[#7b551b]" role="note">
            批次使用候选和修订两级版本校验；若期间有变化，服务端会拒绝整批并要求重新加载。
          </div>
        </div>
        <DialogFooter className="border-t border-[var(--gg-line)] bg-[var(--gg-surface)] px-[24px] py-[14px]">
          <Button type="button" variant="outline" className={DIALOG_CANCEL_BUTTON_CLASS} onClick={() => onOpenChange(false)} disabled={submitting}>取消</Button>
          <Button type="button" className={cn(DIALOG_PRIMARY_BUTTON_CLASS, !approving && 'bg-[var(--gg-state-danger)] hover:bg-[var(--gg-state-danger)]')} onClick={onSubmit} disabled={submitting}>
            {submitting ? '提交中…' : approving ? '确认通过' : '确认拒绝'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function createCatalogCommandId(prefix: string): string {
  const randomId = globalThis.crypto?.randomUUID?.();
  return `${prefix}-${randomId || `${Date.now()}-${Math.floor(Math.random() * 100000)}`}`;
}

function createExternalImportCommandId(): string {
  return createCatalogCommandId('external-skill');
}

type CatalogBindingAgent = {
  id: AgentProfileRead['id'];
  name: AgentProfileRead['name'];
  status: AgentProfileRead['status'];
  is_overall?: AgentProfileRead['is_overall'];
  responsible_org_unit_id?: AgentProfileRead['responsible_org_unit_id'];
  governance_form?: AgentProfileRead['governance_form'];
  organization_release_id?: AgentProfileRead['organization_release_id'];
};

function CatalogBindingDialog({
  open,
  mode,
  detail,
  agents,
  loading,
  submitting,
  selectedAgentId,
  invocationPolicy,
  onOpenChange,
  onAgentChange,
  onInvocationPolicyChange,
  onSubmit,
}: {
  open: boolean;
  mode: 'install' | 'bind';
  detail: BuiltinSkillCatalogDetail;
  agents: CatalogBindingAgent[];
  loading: boolean;
  submitting: boolean;
  selectedAgentId: string;
  invocationPolicy: 'model_allowed' | 'user_only';
  onOpenChange: (open: boolean) => void;
  onAgentChange: (agentId: string) => void;
  onInvocationPolicyChange: (policy: 'model_allowed' | 'user_only') => void;
  onSubmit: () => void;
}) {
  const installing = mode === 'install';
  return (
    <Dialog open={open} onOpenChange={(nextOpen) => { if (!submitting) onOpenChange(nextOpen); }}>
      <DialogContent className="max-w-[520px] gap-0 overflow-hidden p-0">
        <DialogHeader className="border-b border-[#eef1f6] bg-[#fbfcff] px-[24px] py-[20px]">
          <DialogTitle className="gg-type-card-title text-[var(--gg-ink)]">
            {installing ? '安装到我的能力分身' : '绑定到组织数字员工'}
          </DialogTitle>
          <DialogDescription className="gg-type-meta  text-[var(--gg-slate)]">
            {installing
              ? '选择一个本人拥有的能力分身，系统会按当前已审核修订创建显式 Skill 绑定。'
              : '选择一个已完成组织化发布的数字员工，系统会按当前已审核修订创建组织 Skill 绑定。'}
          </DialogDescription>
        </DialogHeader>
        <div className="grid gap-[16px] px-[24px] py-[20px]">
          <div className="rounded-[12px] border border-[#e4e9f4] bg-[#f8faff] px-[13px] py-[11px] gg-type-caption  text-[#5d6880]">
            <span className="gg-type-caption font-medium text-[var(--gg-ink)]">{detail.name}</span>
            <span className="mx-[5px] text-[#a0a7b6]">·</span>
            固定修订 v{detail.revision_number || 1} · {detail.invocation_policy === 'user_only' ? '仅用户触发' : '模型可选'}
          </div>
          <div className="grid gap-[7px]">
            <Label htmlFor="catalog-binding-agent" className="gg-type-meta text-[var(--gg-ink)]">目标 Agent</Label>
            {loading ? (
              <p className="gg-type-meta text-[var(--gg-slate)]" role="status">正在加载目标…</p>
            ) : agents.length ? (
              <Select value={selectedAgentId} onValueChange={onAgentChange}>
                <SelectTrigger id="catalog-binding-agent" aria-label="目标 Agent" className={cn(SELECT_TRIGGER_CLASS, 'w-full gg-type-meta')}>
                  <SelectValue placeholder="选择目标 Agent" />
                </SelectTrigger>
                <SelectContent>
                  {agents.map((agent) => <SelectItem key={agent.id} value={agent.id}>{agent.name}</SelectItem>)}
                </SelectContent>
              </Select>
            ) : (
              <p className="rounded-[10px] border border-dashed border-[#dfe4ef] px-[12px] py-[13px] gg-type-meta text-[var(--gg-slate)]">
                {installing ? '当前账号还没有可安装的能力分身。' : '当前没有满足组织化绑定条件的数字员工。'}
              </p>
            )}
          </div>
          <div className="grid gap-[7px]">
            <Label htmlFor="catalog-binding-invocation" className="gg-type-meta text-[var(--gg-ink)]">调用方式</Label>
            <Select value={invocationPolicy} onValueChange={(value) => onInvocationPolicyChange(value as 'model_allowed' | 'user_only')}>
              <SelectTrigger id="catalog-binding-invocation" aria-label="调用方式" className={cn(SELECT_TRIGGER_CLASS, 'w-full gg-type-meta')}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="model_allowed">模型可选</SelectItem>
                <SelectItem value="user_only">仅用户触发</SelectItem>
              </SelectContent>
            </Select>
          </div>
          {!installing && (
            <p className="rounded-[12px] border border-[#f2dfbd] bg-[#fffaf0] px-[13px] py-[11px] gg-type-caption  text-[#7b551b]" role="note">
              组织绑定要求数字员工已有责任组织、有效主管角色绑定和 active 发布版本；不满足时服务端会拒绝整次操作。
            </p>
          )}
        </div>
        <DialogFooter className="border-t border-[var(--gg-line)] bg-[var(--gg-surface)] px-[24px] py-[14px]">
          <Button type="button" variant="outline" className={DIALOG_CANCEL_BUTTON_CLASS} onClick={() => onOpenChange(false)} disabled={submitting}>取消</Button>
          <Button type="button" className={DIALOG_PRIMARY_BUTTON_CLASS} onClick={onSubmit} disabled={loading || submitting || !selectedAgentId}>
            {submitting ? '提交中…' : installing ? '确认安装' : '确认绑定'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function CatalogLifecycleDialog({
  open,
  action,
  reason,
  submitting,
  onOpenChange,
  onReasonChange,
  onSubmit,
}: {
  open: boolean;
  action: BuiltinSkillCatalogLifecycleAction;
  reason: string;
  submitting: boolean;
  onOpenChange: (open: boolean) => void;
  onReasonChange: (reason: string) => void;
  onSubmit: () => void;
}) {
  const archiving = action === 'archive';
  return (
    <Dialog open={open} onOpenChange={(nextOpen) => { if (!submitting) onOpenChange(nextOpen); }}>
      <DialogContent className="max-w-[520px] gap-0 overflow-hidden p-0">
        <DialogHeader className="border-b border-[#eef1f6] bg-[#fbfcff] px-[24px] py-[20px]">
          <DialogTitle className="gg-type-card-title text-[var(--gg-ink)]">
            {archiving ? '下架 Skill' : '安全撤销 Skill'}
          </DialogTitle>
          <DialogDescription className="gg-type-meta  text-[var(--gg-slate)]">
            {archiving
              ? '下架会停止广场发现和新的安装/绑定；当前已存在的有效绑定仍按原固定版本继续使用。'
              : '安全撤销会停止广场发现和新的安装/绑定，并停用当前租户及其他租户的活动绑定。'}
          </DialogDescription>
        </DialogHeader>
        <div className="grid gap-[7px] px-[24px] py-[20px]">
          <Label htmlFor="catalog-lifecycle-reason" className="gg-type-meta text-[var(--gg-ink)]">
            状态变更原因
          </Label>
          <Textarea
            id="catalog-lifecycle-reason"
            name="lifecycle_reason"
            value={reason}
            onChange={(event) => onReasonChange(event.target.value)}
            placeholder={archiving ? '例如：上游版本待复核，暂时停止新安装…' : '例如：发现高风险行为，需要立即停止所有活动绑定…'}
            className="min-h-[104px] gg-type-meta"
            maxLength={2000}
            disabled={submitting}
            aria-required="true"
          />
          <p className="gg-type-caption  text-[#858b9c]">
            服务端会校验 Skill 和当前修订版本；若期间发生变化，整次操作会拒绝并要求重新确认。
          </p>
        </div>
        <DialogFooter className="border-t border-[var(--gg-line)] bg-[var(--gg-surface)] px-[24px] py-[14px]">
          <Button type="button" variant="outline" className={DIALOG_CANCEL_BUTTON_CLASS} onClick={() => onOpenChange(false)} disabled={submitting}>取消</Button>
          <Button
            type="button"
            className={cn(DIALOG_PRIMARY_BUTTON_CLASS, archiving
              ? 'bg-[var(--gg-state-warning)] hover:bg-[var(--gg-state-warning)]'
              : 'bg-[var(--gg-state-danger)] hover:bg-[var(--gg-state-danger)]')}
            onClick={onSubmit}
            disabled={submitting || !reason.trim()}
          >
            {submitting ? '提交中…' : archiving ? '确认下架' : '确认撤销'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function BuiltinSkillDetailPage({
  currentUser,
  onLogout,
  slug,
}: {
  currentUser?: EnterpriseAuthUser;
  onLogout?: () => void;
  slug: string;
}) {
  const [detail, setDetail] = useState<BuiltinSkillCatalogDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [bindingMode, setBindingMode] = useState<'install' | 'bind' | null>(null);
  const [bindingAgents, setBindingAgents] = useState<CatalogBindingAgent[]>([]);
  const [bindingAgentsLoading, setBindingAgentsLoading] = useState(false);
  const [bindingSubmitting, setBindingSubmitting] = useState(false);
  const [selectedBindingAgentId, setSelectedBindingAgentId] = useState('');
  const [bindingInvocationPolicy, setBindingInvocationPolicy] = useState<'model_allowed' | 'user_only'>('model_allowed');
  const [detailError, setDetailError] = useState<string | null>(null);
  const [documentLocale, setDocumentLocale] = useState<'zh' | 'en'>('zh');
  const [lifecycleAction, setLifecycleAction] = useState<BuiltinSkillCatalogLifecycleAction | null>(null);
  const [lifecycleReason, setLifecycleReason] = useState('');
  const [lifecycleSubmitting, setLifecycleSubmitting] = useState(false);
  const isAdmin = isEnterpriseAdmin(currentUser);

  const loadDetail = useCallback(async () => {
    setLoading(true);
    setDetailError(null);
    try {
      const value = await api.get<BuiltinSkillCatalogDetail>(
        `/api/enterprise/general-skill-catalog/${encodeURIComponent(slug)}?tenant_id=${getRequestTenantId()}`,
      );
      setDetail(value);
      setDocumentLocale(value.explanation_markdown_zh ? 'zh' : 'en');
      return value;
    } catch (error) {
      const message = error instanceof Error ? error.message : '加载 Skill 详情失败';
      setDetailError(message);
      setDetail(null);
      return null;
    } finally {
      setLoading(false);
    }
  }, [slug]);

  useEffect(() => {
    void loadDetail();
  }, [loadDetail]);

  async function openBinding(mode: 'install' | 'bind') {
    if (!detail || detail.status !== 'published' || !detail.revision_id) return;
    if (mode === 'bind' && !isAdmin) return;
    setBindingMode(mode);
    setBindingAgents([]);
    setSelectedBindingAgentId('');
    setBindingInvocationPolicy(detail.invocation_policy);
    setBindingAgentsLoading(true);
    try {
      if (mode === 'install') {
        const owned = await api.get<Array<{ id: string; name: string; status: string }>>(
          '/api/enterprise/my-general-skills/agents',
        );
        const agents = owned.filter((agent) => agent.status === 'active');
        setBindingAgents(agents);
        setSelectedBindingAgentId(agents[0]?.id || '');
      } else {
        const manageable = await api.get<CatalogBindingAgent[]>(
          `/api/enterprise/agents?tenant_id=${getRequestTenantId()}&scope=manageable`,
        );
        const agents = manageable.filter((agent) => (
          agent.status === 'active'
          && !agent.is_overall
          && agent.governance_form === 'organization_employee'
          && Boolean(agent.organization_release_id)
        ));
        setBindingAgents(agents);
        setSelectedBindingAgentId(agents[0]?.id || '');
      }
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载目标 Agent 失败');
    } finally {
      setBindingAgentsLoading(false);
    }
  }

  async function submitBinding() {
    if (!detail || !bindingMode || !detail.revision_id || !selectedBindingAgentId) return;
    setBindingSubmitting(true);
    const payload: BuiltinSkillCatalogBindingRequest = {
      tenant_id: getRequestTenantId(),
      skill_id: detail.id,
      agent_id: selectedBindingAgentId,
      mode: bindingMode,
      revision_policy: 'pinned',
      pinned_revision_id: detail.revision_id,
      invocation_policy: bindingInvocationPolicy,
    };
    try {
      const response = await api.post<BuiltinSkillCatalogBindingResult>(
        '/api/enterprise/general-skill-catalog/bindings',
        payload,
      );
      notify.success(bindingMode === 'install' ? `已安装 Skill：${response.action}` : `已绑定 Skill：${response.action}`);
      setBindingMode(null);
      await loadDetail();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : bindingMode === 'install' ? '安装 Skill 失败' : '绑定 Skill 失败');
    } finally {
      setBindingSubmitting(false);
    }
  }

  function openLifecycle(action: BuiltinSkillCatalogLifecycleAction) {
    if (!detail || !isAdmin || detail.status !== 'published' || !detail.revision_id) return;
    setLifecycleAction(action);
    setLifecycleReason('');
  }

  async function submitLifecycle() {
    if (!detail || !lifecycleAction || !detail.revision_id || !isAdmin) return;
    const reason = lifecycleReason.trim();
    if (!reason) {
      notify.error('请填写状态变更原因');
      return;
    }
    setLifecycleSubmitting(true);
    const payload: BuiltinSkillCatalogLifecycleRequest = {
      tenant_id: getRequestTenantId(),
      command_id: createCatalogCommandId(`catalog-${lifecycleAction}`),
      skill_id: detail.id,
      action: lifecycleAction,
      expected_skill_row_version: detail.row_version,
      expected_revision_row_version: detail.revision_row_version || 1,
      reason,
    };
    try {
      const response = await api.post<BuiltinSkillCatalogLifecycleResult>(
        '/api/enterprise/general-skill-catalog/lifecycle',
        payload,
      );
      notify.success(
        response.replayed
          ? '目录状态变更命令已重放'
          : response.action === 'archive'
            ? 'Skill 已下架；已有绑定仍可按原版本继续使用'
            : `Skill 已安全撤销，已停用 ${response.deactivated_binding_count} 个活动绑定`,
      );
      setLifecycleAction(null);
      await loadDetail();
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        notify.error('Skill 状态或修订已变化，操作未提交；详情已刷新，请重新确认');
        await loadDetail();
      } else {
        notify.error(error instanceof Error ? error.message : '更新 Skill 生命周期失败');
      }
    } finally {
      setLifecycleSubmitting(false);
    }
  }

  return (
    <PageShell template="detail" aria-busy={loading}>
      <AppHeader
        onLogout={onLogout}
        userName={currentUser?.username}
        left={(
          <PageHeader
            size="section"
            backTo="/enterprise/general-skills/catalog"
            backLabel="返回 Skill 管理"
            title="Skill 详情"
            description="查看来源、风险和固定修订"
          />
        )}
      />
      {loading ? (
        <p className="gg-type-body mt-[24px]" role="status">正在读取 Skill 详情…</p>
      ) : detail ? (
        <DetailSurface container="page" className="mt-[20px] grid gap-[16px] lg:grid-cols-[minmax(0,1.5fr)_minmax(280px,0.7fr)]">
          <article className="min-w-0 overflow-hidden rounded-[var(--gg-radius-panel)] border border-[var(--gg-line)] bg-[var(--gg-surface)] shadow-[var(--gg-shadow-card)]">
            <div className="border-b border-[var(--gg-line)] px-[22px] py-[22px]">
              <div className="flex flex-wrap items-start justify-between gap-[12px]">
                <div>
                  <div className="flex flex-wrap items-center gap-[7px]">
                    <h2 className="gg-type-section-title">{detail.name_zh || detail.name}</h2>
                    <CatalogBadge tone={detail.status === 'published' ? 'green' : 'blue'}>{STATUS_LABELS[detail.status]}</CatalogBadge>
                    <CatalogBadge tone={detail.risk_level === 'high' ? 'red' : detail.risk_level === 'medium' ? 'amber' : 'green'}>{RISK_LABELS[detail.risk_level]}</CatalogBadge>
                  </div>
                  <p className="gg-type-body mt-[7px]">{detail.description_zh || detail.description || '暂无描述'}</p>
                  <p className="gg-type-caption mt-[5px]">英文原名：<span translate="no">{detail.name} · {detail.slug}</span></p>
                </div>
                <div className="flex flex-wrap items-center justify-end gap-[8px]">
                  <span className="gg-type-caption rounded-[var(--gg-radius-control)] bg-[var(--gg-interaction-soft)] px-[10px] py-[6px] text-[var(--gg-interaction)]">{detail.category}</span>
                  {currentUser && detail.status === 'published' && detail.revision_id && (
                    <>
                      <Button variant="outline" className={cn('h-[32px]', 'gg-type-control')} onClick={() => void openBinding('install')}>
                        安装到我的能力分身
                      </Button>
                      {isAdmin && (
                        <Button className={cn('h-[32px] bg-[var(--gg-interaction)]', 'gg-type-control')} onClick={() => void openBinding('bind')}>
                          绑定到组织数字员工
                        </Button>
                      )}
                      {isAdmin && (
                        <>
                          <Button variant="outline" className="gg-type-control h-[32px] border-[var(--gg-state-warning)] text-[var(--gg-state-warning)] hover:bg-[var(--gg-state-warning-soft)]" onClick={() => openLifecycle('archive')}>
                            下架
                          </Button>
                          <Button variant="outline" className="gg-type-control h-[32px] border-[var(--gg-state-danger)] text-[var(--gg-state-danger)] hover:bg-[var(--gg-state-danger-soft)]" onClick={() => openLifecycle('revoke')}>
                            安全撤销
                          </Button>
                        </>
                      )}
                    </>
                  )}
                </div>
              </div>
            </div>
            <div className="px-[22px] py-[20px]">
              <div className="flex flex-wrap items-center justify-between gap-[10px]">
                <div>
                  <h2 className="gg-type-card-title">Skill 文档</h2>
                  <p className="gg-type-caption mt-[4px]">
                    中文解读仅用于阅读；实际运行始终使用英文原文。
                  </p>
                </div>
                <div className="flex items-center gap-[6px]" role="group" aria-label="Skill 文档语言">
                  <Button
                    type="button"
                    variant={documentLocale === 'zh' ? 'default' : 'outline'}
                    aria-pressed={documentLocale === 'zh'}
                    disabled={!detail.explanation_markdown_zh}
                    onClick={() => setDocumentLocale('zh')}
                    className="gg-type-control h-[30px] touch-manipulation px-[10px]"
                  >
                    中文解读
                  </Button>
                  <Button
                    type="button"
                    variant={documentLocale === 'en' ? 'default' : 'outline'}
                    aria-pressed={documentLocale === 'en'}
                    onClick={() => setDocumentLocale('en')}
                    className="gg-type-control h-[30px] touch-manipulation px-[10px]"
                  >
                    英文原文（运行时使用）
                  </Button>
                </div>
              </div>
              {detail.localization_status && detail.localization_status !== 'verified' && (
                <p className="gg-type-caption mt-[10px] rounded-[var(--gg-radius-control)] border border-[var(--gg-state-warning)] bg-[var(--gg-state-warning-soft)] px-[11px] py-[9px] text-[var(--gg-state-warning)]" role="status">
                  当前中文解读状态为“{detail.localization_status}”，已回退到英文原文，避免展示与当前版本不一致的说明。
                </p>
              )}
              <pre translate={documentLocale === 'en' ? 'no' : undefined} className="gg-type-code mt-[10px] max-h-[580px] overflow-auto rounded-[var(--gg-radius-card)] bg-[var(--gg-surface-subtle)] p-[16px] text-[var(--gg-text-secondary)] whitespace-pre-wrap">{documentLocale === 'zh' && detail.explanation_markdown_zh ? detail.explanation_markdown_zh : detail.skill_markdown}</pre>
            </div>
          </article>
          <aside className="grid content-start gap-[12px]">
            <DetailPanel title="来源证据">
              <DetailRow label="来源" value={detail.source_repository} link={detail.source_repository} />
              <DetailRow label="提交" value={detail.source_revision} mono />
              <DetailRow label="路径" value={detail.source_path} mono />
              <DetailRow label="许可证" value={detail.source_license || '未声明'} />
            </DetailPanel>
            <DetailPanel title="版本与调用">
              <DetailRow label="修订" value={detail.revision_number ? `v${detail.revision_number}` : '未生成'} />
              <DetailRow label="修订状态" value={detail.revision_status || '未知'} />
              <DetailRow label="调用方式" value={detail.invocation_policy === 'user_only' ? '仅用户触发' : '模型可选'} />
              <DetailRow label="运行模式" value="只读指导，不直接执行脚本" />
              <DetailRow label="文件数" value={String(detail.resource_count)} />
            </DetailPanel>
            <DetailPanel title="校验摘要">
              <DetailRow label="包 checksum" value={detail.source_package_checksum} mono />
              <DetailRow label="规范 checksum" value={detail.source_normalized_checksum} mono />
              <DetailRow label="内容 checksum" value={detail.content_checksum} mono />
              <DetailRow label="文件清单" value={detail.manifest_checksum} mono />
            </DetailPanel>
            {detail.risk_findings.length > 0 && (
              <div className="gg-type-body rounded-[var(--gg-radius-card)] border border-[var(--gg-state-warning)] bg-[var(--gg-state-warning-soft)] p-[14px] text-[var(--gg-state-warning)]">
                <div className="flex items-center gap-[7px] gg-type-control font-semibold"><ShieldAlert className="size-[15px]" />风险证据</div>
                <ul className="mt-[8px] grid gap-[5px] pl-[20px] list-disc">
                  {detail.risk_findings.map((finding) => <li key={finding} className="break-all">{finding}</li>)}
                </ul>
              </div>
            )}
            <DetailPanel title={`当前租户采用情况（${detail.bindings.length}）`}>
              {detail.bindings.length ? detail.bindings.map((binding) => (
                <div key={binding.binding_id} className={cn('px-[11px] py-[9px]', DETAIL_FACT_CARD_CLASS)}>
                  <div className="flex items-center justify-between gap-[8px]">
                    <span className="gg-type-control min-w-0 truncate font-semibold text-[var(--gg-text-primary)]">{binding.agent_name}</span>
                    <CatalogBadge tone={binding.status === 'active' ? 'green' : 'gray'}>{binding.status === 'active' ? '活动' : '已停用'}</CatalogBadge>
                  </div>
                  <p className="gg-type-caption mt-[5px]">
                    {binding.governance_form === 'organization_employee' ? '组织数字员工' : '能力分身'} · {binding.revision_policy === 'follow_latest' ? '跟随最新修订' : '固定修订'} · {binding.invocation_policy === 'user_only' ? '仅用户触发' : '模型可选'}
                  </p>
                </div>
              )) : (
                <p className="gg-type-caption rounded-[var(--gg-radius-control)] border border-dashed border-[var(--gg-line)] px-[11px] py-[12px]">
                  当前租户还没有安装或绑定此 Skill。
                </p>
              )}
            </DetailPanel>
          </aside>
        </DetailSurface>
      ) : (
        <div className="gg-type-body mt-[24px] rounded-[var(--gg-radius-card)] border border-dashed border-[var(--gg-line)] bg-[var(--gg-surface)] p-[28px] text-center">
          <p>{detailError || 'Skill 不存在或当前账号无权查看。'}</p>
          {detailError && (
            <Button variant="outline" className="gg-type-control mt-[14px] h-[32px]" onClick={() => void loadDetail()}>
              重新加载
            </Button>
          )}
        </div>
      )}
      {detail && bindingMode && (
        <CatalogBindingDialog
          open
          mode={bindingMode}
          detail={detail}
          agents={bindingAgents}
          loading={bindingAgentsLoading}
          submitting={bindingSubmitting}
          selectedAgentId={selectedBindingAgentId}
          invocationPolicy={bindingInvocationPolicy}
          onOpenChange={(open) => { if (!open) setBindingMode(null); }}
          onAgentChange={setSelectedBindingAgentId}
          onInvocationPolicyChange={setBindingInvocationPolicy}
          onSubmit={() => void submitBinding()}
        />
      )}
      {detail && lifecycleAction && (
        <CatalogLifecycleDialog
          open
          action={lifecycleAction}
          reason={lifecycleReason}
          submitting={lifecycleSubmitting}
          onOpenChange={(open) => { if (!open && !lifecycleSubmitting) setLifecycleAction(null); }}
          onReasonChange={setLifecycleReason}
          onSubmit={() => void submitLifecycle()}
        />
      )}
    </PageShell>
  );
}

function CatalogCard({
  item,
  selectable = false,
  selected = false,
  onSelect,
}: {
  item: BuiltinSkillCatalogItem;
  selectable?: boolean;
  selected?: boolean;
  onSelect?: (checked: boolean) => void;
}) {
  const detailHref = `/enterprise/general-skills/catalog/${encodeURIComponent(item.slug)}`;
  return (
    <article className={cn(
      RESOURCE_CARD_CLASS,
      'border-[var(--gg-line)] hover:border-[var(--gg-interaction)] focus-within:ring-2 focus-within:ring-[var(--gg-interaction)]',
    )}>
      {selectable && (
        <div className="absolute left-[12px] top-[12px] z-20 rounded-[var(--gg-radius-control)] bg-[var(--gg-surface)] p-[5px] shadow-[0_3px_10px_rgba(49,87,232,0.12)]">
          <Checkbox
            checked={selected}
            aria-label={`选择 ${item.name_zh || item.name}`}
            onClick={(event) => event.stopPropagation()}
            onCheckedChange={(checked) => onSelect?.(checked === true)}
          />
        </div>
      )}

      <div data-resource-identity className={cn(RESOURCE_CARD_IDENTITY_CLASS, 'bg-[var(--gg-interaction-soft)]')}>
        <div className={RESOURCE_CARD_ICON_SLOT_CLASS} aria-hidden="true">
          <Sparkles className="size-[22px]" strokeWidth={1.8} />
        </div>
        <Link to={detailHref} className="min-w-0 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--gg-cobalt)]">
          <h2 className="gg-type-card-title truncate">{item.name_zh || item.name}</h2>
          {item.name_zh && <p className="gg-type-caption mt-[2px] truncate">英文：<span translate="no">{item.name}</span></p>}
          <p className="gg-type-code mt-[4px] truncate text-[var(--gg-text-muted)]" translate="no">{item.source_path}</p>
        </Link>
        <CatalogBadge tone={item.risk_level === 'high' ? 'red' : item.risk_level === 'medium' ? 'amber' : 'green'}>{RISK_LABELS[item.risk_level]}</CatalogBadge>
      </div>

      <p className={RESOURCE_CARD_DESCRIPTION_CLASS}>{item.description_zh || item.description || '暂无描述'}</p>

      <div className={cn(RESOURCE_CARD_FOOTER_CLASS, 'justify-between gap-[10px]')}>
        <div className="flex min-w-0 flex-wrap items-center gap-[6px]">
          <CatalogBadge tone="blue">{item.category}</CatalogBadge>
          <CatalogBadge tone="gray">{STABILITY_LABELS[item.stability]}</CatalogBadge>
          <CatalogBadge tone={item.status === 'published' ? 'green' : 'blue'}>{STATUS_LABELS[item.status]}</CatalogBadge>
        </div>
        <Link to={detailHref} className="gg-type-control flex shrink-0 items-center gap-[4px] text-[var(--gg-interaction)] hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--gg-interaction)]">
          查看详情 <ExternalLink className="size-[12px]" />
        </Link>
      </div>
    </article>
  );
}

function CatalogSelect({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: Array<{ value: string; label: string }>;
}) {
  return (
    <Select value={value || 'all'} onValueChange={(next) => onChange(next === 'all' ? '' : next)}>
      <SelectTrigger aria-label={label} className={cn(SELECT_TRIGGER_CLASS, 'w-full gg-type-meta')}>
        <SelectValue placeholder={label} />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value="all">全部{label}</SelectItem>
        {options.map((option) => <SelectItem key={option.value} value={option.value}>{option.label}</SelectItem>)}
      </SelectContent>
    </Select>
  );
}

function CatalogBadge({
  tone,
  children,
}: {
  tone: 'blue' | 'green' | 'amber' | 'red' | 'gray';
  children: ReactNode;
}) {
  return <span className={cn(
    'gg-type-caption inline-flex max-w-full shrink-0 items-center whitespace-nowrap rounded-[var(--gg-radius-control)] px-[8px] py-[4px]',
    tone === 'blue' && 'bg-[var(--gg-interaction-soft)] text-[var(--gg-interaction)]',
    tone === 'green' && 'bg-[var(--gg-state-success-soft)] text-[var(--gg-state-success)]',
    tone === 'amber' && 'bg-[var(--gg-state-warning-soft)] text-[var(--gg-state-warning)]',
    tone === 'red' && 'bg-[var(--gg-state-danger-soft)] text-[var(--gg-state-danger)]',
    tone === 'gray' && 'bg-[var(--gg-state-neutral-soft)] text-[var(--gg-state-neutral)]',
  )}>{children}</span>;
}

function DetailPanel({ title, children }: { title: string; children: ReactNode }) {
  return <section className={DETAIL_PANEL_CLASS}><h2 className="gg-type-card-title">{title}</h2><div className="mt-[12px] grid gap-[10px]">{children}</div></section>;
}

function DetailRow({ label, value, mono = false, link }: { label: string; value: string; mono?: boolean; link?: string }) {
  return <div className="grid min-w-0 grid-cols-[70px_minmax(0,1fr)] gap-[8px]"><span className="gg-type-caption">{label}</span>{link ? <a className={cn('gg-type-control min-w-0 truncate text-[var(--gg-interaction)] hover:underline', mono && 'gg-type-code')} href={link} target="_blank" rel="noreferrer">{value}</a> : <span className={cn('gg-type-control min-w-0 break-all text-[var(--gg-text-secondary)]', mono && 'gg-type-code')}>{value}</span>}</div>;
}

function readFilters(params: URLSearchParams): CatalogFilters {
  return {
    search: params.get('search') || '',
    category: params.get('category') || '',
    sourceKind: params.get('source_kind') || '',
    stability: params.get('stability') || '',
    riskLevel: params.get('risk_level') || '',
    invocationPolicy: params.get('invocation_policy') || '',
    status: params.get('status') || '',
  };
}

function readPage(params: URLSearchParams): number {
  const value = Number(params.get('page') || '1');
  return Number.isInteger(value) && value > 0 ? value : 1;
}

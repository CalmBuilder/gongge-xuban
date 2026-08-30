import { useMemo, useState } from 'react';

import { Button as UIButton } from '@/components/ui/button';
import { RESOURCE_GRID_CLASS } from '@/lib/enterprise-ui';
import { cn } from '@/lib/utils';

import IconRefresh from '../../assets/icons/refresh.svg?react';
import IconSearch from '../../assets/icons/search.svg?react';
import EmployeeCard from '../EmployeeCard';
import { Paginator } from '../Paginator';
import type { AgentProfileRead } from '../../types';

import PlazaResourceArtwork from './PlazaResourceArtwork';
import PlatformResourceCard, { type PlatformResourceAccent } from './PlatformResourceCard';

export type PlatformDetailKind = 'agents' | 'experts' | 'knowledge' | 'general-skills' | 'skills' | 'tools';

export type PlatformDetailItem = {
  id: string;
  title: string;
  description: string;
  meta: string;
  tags: string[];
  agent?: AgentProfileRead;
};

const PLATFORM_ACCENT: Partial<Record<PlatformDetailKind, PlatformResourceAccent>> = {
  knowledge: 'green',
  'general-skills': 'indigo',
  skills: 'blue',
  tools: 'orange',
};

const PLATFORM_PAGE_SIZE = 12;

export type PlatformKindDetailViewProps = {
  kind: PlatformDetailKind;
  title: string;
  subtitle?: string;
  countLabel: string;
  signals: string[];
  items: PlatformDetailItem[];
  loading: boolean;
  onRefresh: () => void;
  onOpenItem: (item: PlatformDetailItem) => void;
  onUseItem: (item: PlatformDetailItem) => void;
};

function DetailSkeleton() {
  return (
    <div className={RESOURCE_GRID_CLASS}>
      {Array.from({ length: 8 }, (_, index) => (
        <div
          key={index}
          className="h-full w-full animate-pulse rounded-[var(--gg-radius-card)] border border-[var(--gg-line)] bg-[var(--gg-surface-subtle)]"
        />
      ))}
    </div>
  );
}

/**
 * 开放广场单一资源类型的全宽网格视图（/enterprise/platform/:kind）。
 * 页面骨架（标题 / 类型 tab）由 OpenPlatformPage 提供，这里只负责
 * 能力标签、搜索与卡片网格。
 */
export default function PlatformKindDetailView({
  kind,
  title,
  subtitle,
  countLabel,
  signals,
  items,
  loading,
  onRefresh,
  onOpenItem,
  onUseItem,
}: PlatformKindDetailViewProps) {
  const [searchText, setSearchText] = useState('');
  const [page, setPage] = useState(1);

  const filteredItems = useMemo(() => {
    const keyword = searchText.trim().toLowerCase();
    if (!keyword) return items;
    return items.filter((item) => [
      item.title,
      item.description,
      item.meta,
      item.tags.join(' '),
    ].some((value) => value.toLowerCase().includes(keyword)));
  }, [items, searchText]);
  const pageCount = Math.max(1, Math.ceil(filteredItems.length / PLATFORM_PAGE_SIZE));
  const currentPage = Math.min(page, pageCount);
  const pageItems = filteredItems.slice(
    (currentPage - 1) * PLATFORM_PAGE_SIZE,
    currentPage * PLATFORM_PAGE_SIZE,
  );

  return (
    <div className="mt-[20px] flex flex-col gap-[16px]">
      <div className="flex flex-wrap items-center gap-[12px]">
        <div className="min-w-0 text-[var(--gg-text-secondary)]">
          <div className="flex items-center gap-[8px]">
          {kind === 'agents' || kind === 'experts'
            ? null
            : <PlazaResourceArtwork kind={kind} size="compact" />}
            <h1 className="gg-type-section-title text-balance">{title}</h1>
            <span className="gg-type-meta">{items.length} {countLabel}</span>
          </div>
          {subtitle && (
            <p className="gg-type-body mt-[7px] max-w-[680px]">
              {subtitle}
            </p>
          )}
        </div>

        {signals.length > 0 && (
          <div className="hidden flex-wrap items-center gap-[6px] md:flex">
            {signals.map((signal) => (
              <span
                key={signal}
                className="gg-type-caption rounded-[var(--gg-radius-control)] border border-[var(--gg-border)] bg-[var(--gg-surface)] px-[8px] py-[2px]"
              >
                {signal}
              </span>
            ))}
          </div>
        )}

        <div className="ml-auto flex items-center gap-[10px]">
          <label className="flex h-[36px] w-full max-w-[320px] items-center gap-[8px] overflow-hidden rounded-[var(--gg-radius-control)] border border-[var(--gg-border)] bg-[var(--gg-surface)] px-[12px] transition-colors focus-within:border-[var(--gg-interaction)]">
            <IconSearch className="size-[14px] shrink-0 text-[var(--gg-text-muted)]" />
            <input
              value={searchText}
              name={`plaza-search-${kind}`}
              aria-label={`搜索${countLabel}`}
              autoComplete="off"
              placeholder={`搜索${countLabel}…`}
              onChange={(event) => {
                setSearchText(event.target.value);
                setPage(1);
              }}
              className="gg-type-control min-w-0 flex-1 border-0 bg-transparent text-[var(--gg-text-primary)] outline-none placeholder:text-[var(--gg-text-muted)]"
            />
          </label>
          <UIButton
            variant="outline"
            onClick={onRefresh}
            disabled={loading}
            aria-label="刷新"
            className="gg-type-control h-[36px] gap-1 rounded-[var(--gg-radius-control)] border-[var(--gg-border)] bg-[var(--gg-surface)] px-[12px] text-[var(--gg-text-muted)] hover:border-[var(--gg-interaction)] hover:bg-[var(--gg-interaction-soft)] hover:text-[var(--gg-text-primary)]"
          >
            <IconRefresh className={cn('size-[14px]', loading && 'animate-spin')} />
            刷新
          </UIButton>
        </div>
      </div>

      {loading ? (
        <DetailSkeleton />
      ) : filteredItems.length === 0 ? (
        <div className="gg-type-body grid min-h-[180px] w-full place-items-center content-center gap-[10px] rounded-[var(--gg-radius-panel)] border border-dashed border-[var(--gg-line)] bg-[var(--gg-surface-subtle)] px-[20px] py-[40px] text-center font-medium">
          <IconSearch className="size-[20px] shrink-0" />
          <span>{items.length === 0 ? '暂无开放内容' : '没有匹配的广场内容'}</span>
        </div>
      ) : kind === 'agents' || kind === 'experts' ? (
        <div className={RESOURCE_GRID_CLASS}>
          {pageItems.map((item) => item.agent && (
            <EmployeeCard
              key={item.id}
              employee={item.agent}
              canManage={false}
              canGovern={false}
              showMenu={false}
              statusLabel={kind === 'experts' ? '可使用' : undefined}
              statusKind={kind === 'experts'
                ? 'available'
                : item.agent.status === 'active' ? 'online' : 'offline'}
              relationLabels={kind === 'experts' ? [] : ['企业发布']}
              onOpen={() => onOpenItem(item)}
              onChat={() => onUseItem(item)}
              onStatus={() => undefined}
              onGallery={() => undefined}
              onDelete={() => undefined}
              onAvatar={() => undefined}
              onEdit={() => undefined}
            />
          ))}
        </div>
      ) : (
        <div className={RESOURCE_GRID_CLASS}>
          {pageItems.map((item) => (
            <PlatformResourceCard
              key={item.id}
              icon={<PlazaResourceArtwork kind={kind} />}
              accent={PLATFORM_ACCENT[kind]}
              title={item.title}
              meta={item.meta}
              description={item.description}
              tags={item.tags.slice(0, 2)}
              onClick={() => onOpenItem(item)}
            />
          ))}
        </div>
      )}
      {!loading && filteredItems.length > 0 && (
        <Paginator
          page={currentPage}
          pageCount={pageCount}
          onChange={setPage}
          aria-label={`${title}分页`}
          className="mt-[12px]"
        />
      )}
    </div>
  );
}

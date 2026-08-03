import { useMemo, useState } from 'react';

import { Button as UIButton } from '@/components/ui/button';
import { cn } from '@/lib/utils';

import IconRefresh from '../../assets/icons/refresh.svg?react';
import IconSearch from '../../assets/icons/search.svg?react';
import EmployeeCard from '../EmployeeCard';
import { Paginator } from '../Paginator';
import type { AgentProfileRead } from '../../types';

import PlazaResourceArtwork from './PlazaResourceArtwork';
import PlatformResourceCard, { type PlatformResourceAccent } from './PlatformResourceCard';

export type PlatformDetailKind = 'agents' | 'knowledge' | 'general-skills' | 'skills' | 'tools';

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
    <div className="grid auto-rows-[292px] grid-cols-1 gap-[32px] sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4 max-[900px]:gap-[18px]">
      {Array.from({ length: 8 }, (_, index) => (
        <div
          key={index}
          className="h-full w-full animate-pulse rounded-[14px] border-[0.5px] border-[#f0f1f5] bg-[#f6f6f6]"
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
        <div className="flex items-center gap-[8px] text-[#464c5e]">
          {kind === 'agents'
            ? null
            : <PlazaResourceArtwork kind={kind} size="compact" />}
          <span className="text-[14px] font-semibold leading-none text-[#252a3c]">{title}</span>
          <span className="text-[12px] text-[#8a93a6]">{items.length} {countLabel}</span>
        </div>

        {signals.length > 0 && (
          <div className="hidden flex-wrap items-center gap-[6px] md:flex">
            {signals.map((signal) => (
              <span
                key={signal}
                className="rounded-[20px] border border-[var(--gg-border)] bg-white px-[8px] py-[2px] text-[11px] leading-[normal] text-[#757f9c]"
              >
                {signal}
              </span>
            ))}
          </div>
        )}

        <div className="ml-auto flex items-center gap-[10px]">
          <label className="flex h-[36px] w-full max-w-[320px] items-center gap-[8px] overflow-hidden rounded-[10px] border border-[var(--gg-border)] bg-white px-[12px] transition-colors focus-within:border-[var(--gg-cobalt)]">
            <IconSearch className="size-[14px] shrink-0 text-[#858b9c]" />
            <input
              value={searchText}
              placeholder={`搜索${countLabel}`}
              onChange={(event) => {
                setSearchText(event.target.value);
                setPage(1);
              }}
              className="min-w-0 flex-1 border-0 bg-transparent text-[12px] text-[#18181a] outline-none placeholder:text-[#858b9c]"
            />
          </label>
          <UIButton
            variant="outline"
            onClick={onRefresh}
            disabled={loading}
            aria-label="刷新"
            className="h-[36px] gap-1 rounded-[10px] border-[var(--gg-border)] bg-white px-[12px] text-[12px] font-normal text-[#757f9c] hover:border-[#cbd3e6] hover:bg-white hover:text-[#18181a]"
          >
            <IconRefresh className={cn('size-[14px]', loading && 'animate-spin')} />
            刷新
          </UIButton>
        </div>
      </div>

      {loading ? (
        <DetailSkeleton />
      ) : filteredItems.length === 0 ? (
        <div className="grid min-h-[180px] w-full place-items-center content-center gap-[10px] rounded-[18px] border border-dashed border-[#dfe4ec] bg-[#fbfcfd] px-[20px] py-[40px] text-center text-[13px] font-medium text-[#8b94aa]">
          <IconSearch className="size-[20px] shrink-0" />
          <span>{items.length === 0 ? '暂无开放内容' : '没有匹配的广场内容'}</span>
        </div>
      ) : kind === 'agents' ? (
        <div className="grid auto-rows-[292px] grid-cols-1 content-start gap-[32px] sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4 max-[900px]:gap-[18px]">
          {pageItems.map((item) => item.agent && (
            <EmployeeCard
              key={item.id}
              employee={item.agent}
              canManage={false}
              canGovern={false}
              showMenu={false}
              relationLabels={['企业发布']}
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
        <div className="grid auto-rows-[292px] grid-cols-1 content-start gap-[32px] sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4 max-[900px]:gap-[18px]">
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

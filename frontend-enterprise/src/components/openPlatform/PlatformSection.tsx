import type { ReactNode } from 'react';

import type { PlazaResourceKind } from '@/assets/plaza/plaza-resource-icons';
import { cn } from '@/lib/utils';

import IconChevronDown from '../../assets/icons/chevron-down.svg?react';

/** Per-module accent, now only used for the small count pill — sections themselves stay white. */
export type PlatformSectionTone = 'agents' | PlazaResourceKind;

const PILL_TONES: Record<PlatformSectionTone, string> = {
  agents: 'border-[#dde4fb] text-[#3c55d9]',
  knowledge: 'border-[#d8eedf] text-[#1e9e5a]',
  'general-skills': 'border-[#dbe7fd] text-[#2f6ae0]',
  skills: 'border-[#d3edf3] text-[#0a8aa8]',
  tools: 'border-[#f6e5c9] text-[#c26a09]',
};

export type PlatformSectionProps = {
  /** Small glyph / artwork shown before the title. */
  icon: ReactNode;
  /** Section title, e.g. 数字员工广场. */
  title: ReactNode;
  /** Count shown as a small pill after the title. */
  count: number;
  /** Unit label rendered after the count, e.g. 员工 / 内容. */
  countLabel: string;
  /** Module tone controlling the count pill color. Defaults to agents. */
  tone?: PlatformSectionTone;
  /** Capability chips rendered on the right of the header (desktop only). */
  filters?: string[];
  /** Renders a skeleton row while data loads. */
  loading?: boolean;
  /** Whether the section has no content — shows the empty placeholder. */
  isEmpty?: boolean;
  /** Text for the empty placeholder. */
  emptyText?: string;
  /** Fired when the "查看全部" button is pressed. */
  onViewAll?: () => void;
  /** The section's cards (rendered in a horizontal scroll row). */
  children?: ReactNode;
  className?: string;
};

/**
 * 开放广场「全部」视图中的单个资源分区。白面板 + 头部（icon / 标题 / 计数 pill /
 * 能力标签 / 查看全部），内容为一行可横向滚动的卡片。取代了旧的五色等宽列
 * （PlatformColumn），类型身份只通过 icon 与计数 pill 的颜色点缀表达。
 */
export default function PlatformSection({
  icon,
  title,
  count,
  countLabel,
  tone = 'agents',
  filters,
  loading = false,
  isEmpty = false,
  emptyText = '暂无开放内容',
  onViewAll,
  children,
  className,
}: PlatformSectionProps) {
  return (
    <section
      className={cn(
        'rounded-[16px] border border-[var(--gg-border)] bg-white px-[18px] py-[16px] shadow-[0_1px_4px_rgba(15,23,42,0.04)]',
        className,
      )}
    >
      <header className="flex w-full items-center gap-[10px]">
        <span className="flex h-[30px] shrink-0 items-center justify-center text-[#464c5e]">
          {icon}
        </span>
        <p className="shrink-0 gg-type-body font-semibold text-[#252a3c]">{title}</p>
        <span
          className={cn(
            'flex h-[20px] min-w-[28px] shrink-0 items-center justify-center rounded-full border bg-[#fbfcfe] px-[8px] gg-type-caption font-semibold',
            PILL_TONES[tone],
          )}
        >
          {count}
        </span>
        <span className="sr-only">{countLabel}</span>
        <div className="ml-auto flex shrink-0 items-center gap-[10px]">
          {filters && filters.length > 0 && (
            <div className="hidden items-center gap-[6px] md:flex">
              {filters.map((filter) => (
                <span
                  key={filter}
                  className="rounded-[20px] border border-[var(--gg-border)] bg-[#fbfcfe] px-[8px] py-[2px] gg-type-caption  text-[#757f9c]"
                >
                  {filter}
                </span>
              ))}
            </div>
          )}
          {!isEmpty && (
            <button
              type="button"
              onClick={onViewAll}
              className="flex shrink-0 items-center gap-[2px] rounded-[10px] px-[8px] py-[4px] gg-type-meta text-[#757f9c] transition-colors hover:bg-[#f4f6fc] hover:text-[#18181a]"
            >
              查看全部
              <IconChevronDown className="size-[14px] shrink-0 -rotate-90" />
            </button>
          )}
        </div>
      </header>

      <div className="mt-[14px]">
        {loading ? (
          <PlatformSectionSkeleton />
        ) : isEmpty ? (
          <div className="flex min-h-[88px] w-full items-center justify-center rounded-[14px] border border-dashed border-[#dfe4ee] bg-[#fbfcfd] px-[18px] py-[20px] text-center">
            <p className="gg-type-meta  text-[#8a93a6]">
              {emptyText}
              <span className="ml-[6px] text-[#a7adbb]">发布内容后会在这里展示</span>
            </p>
          </div>
        ) : (
          <div className="relative">
            <div className="flex gap-[14px] overflow-x-auto pb-[4px]">
              {children}
            </div>
            <span
              className="pointer-events-none absolute right-0 top-0 h-full w-[40px] bg-gradient-to-l from-white to-transparent"
              aria-hidden="true"
            />
          </div>
        )}
      </div>
    </section>
  );
}

function PlatformSectionSkeleton() {
  return (
    <div className="flex gap-[14px] overflow-hidden">
      {[0, 1, 2, 3].map((index) => (
        <div
          key={index}
          className="h-[132px] w-[300px] shrink-0 animate-pulse rounded-[16px] border-[0.5px] border-[#f0f1f5] bg-[#f6f6f6]"
        />
      ))}
    </div>
  );
}

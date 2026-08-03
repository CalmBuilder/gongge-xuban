import type { ReactNode } from 'react';

import { cn } from '@/lib/utils';

import PlazaResourceIcon from './PlazaResourceIcon';

/** Per-module accent used for the meta line, tag pills and card border tint. */
export type PlatformResourceAccent = 'green' | 'blue' | 'indigo' | 'orange';

const ACCENT_STYLES: Record<PlatformResourceAccent, {
  meta: string;
  tag: string;
  card: string;
  identity: string;
}> = {
  green: {
    meta: 'text-[#1e9e5a]',
    tag: 'bg-[#e8f7ee] text-[#1e9e5a]',
    card: 'border-[#e2f2e8] hover:border-[#c2e5d1]',
    identity: 'bg-[#edf9f2]',
  },
  blue: {
    meta: 'text-[#0284c7]',
    tag: 'bg-[#e0f4fd] text-[#0284c7]',
    card: 'border-[#dcf0f8] hover:border-[#b7e1f0]',
    identity: 'bg-[#edf8fd]',
  },
  indigo: {
    meta: 'text-[#2f6ae0]',
    tag: 'bg-[#e8efff] text-[#2f6ae0]',
    card: 'border-[#e3eaff] hover:border-[#c7d5ff]',
    identity: 'bg-[#eef3ff]',
  },
  orange: {
    meta: 'text-[#c26a09]',
    tag: 'bg-[#fdf0dd] text-[#c26a09]',
    card: 'border-[#f7e8d2] hover:border-[#efd5ae]',
    identity: 'bg-[#fff6e9]',
  },
};

export const platformResourceAccentStyles = ACCENT_STYLES;

export type PlatformResourceCardProps = {
  title: ReactNode;
  /** Accent metric line under the title, e.g. "12M / 6个片段". */
  meta: ReactNode;
  description: ReactNode;
  tags?: string[];
  /** Full-size module artwork. When omitted a default folder tile is shown. */
  icon?: ReactNode;
  /** Module accent color for the meta line, tag pills and border tint. Defaults to green (知识库). */
  accent?: PlatformResourceAccent;
  onClick?: () => void;
  className?: string;
};

/**
 * 广场 resource card shared by the 知识库 / 技能 / SOP / 工具 modules. It renders
 * the dimensional module artwork, a semibold title with an accented meta line,
 * a three-line description and a bottom metadata strip. Its outer dimensions
 * intentionally match EmployeeCard in the shared plaza grid.
 */
export default function PlatformResourceCard({
  title,
  meta,
  description,
  tags,
  icon,
  accent = 'green',
  onClick,
  className,
}: PlatformResourceCardProps) {
  const accentStyles = ACCENT_STYLES[accent];
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        'gongge-platform-resource-card group relative flex h-full w-full min-w-0 flex-col rounded-[14px] border bg-white p-[14px] text-left transition-[border-color,box-shadow,transform] duration-150 hover:-translate-y-[2px] hover:shadow-[0_12px_28px_rgba(15,23,42,0.08)]',
        accentStyles.card,
        className,
      )}
    >
      <div className={cn(
        'mt-[30px] flex h-[72px] w-full shrink-0 items-center gap-[12px] rounded-[12px] px-[12px]',
        accentStyles.identity,
      )}>
        {icon ?? (
          <PlazaResourceIcon kind="knowledge" />
        )}
        <div className="flex min-w-0 flex-1 flex-col gap-[4px]">
          <p className="truncate text-[14px] font-semibold text-[#262b3d]">{title}</p>
          <p className={cn('truncate text-[11px] font-medium', accentStyles.meta)}>{meta}</p>
        </div>
      </div>

      <p className="mt-[14px] line-clamp-3 min-h-[48px] w-full text-[12px] leading-[16px] text-[#6b7488]">
        {description}
      </p>

      <div className="mt-auto flex min-h-[48px] w-full items-center gap-[6px] overflow-hidden rounded-[10px] border border-[#e4e9f2] bg-[#fbfcff] px-[10px]">
        {tags && tags.length > 0 ? (
          tags.slice(0, 2).map((tag) => (
            <span
              key={tag}
              className={cn(
                'inline-flex min-w-0 max-w-[50%] items-center truncate rounded-full px-[8px] py-[3px] text-[10px] font-medium leading-[normal]',
                accentStyles.tag,
              )}
            >
              {tag}
            </span>
          ))
        ) : (
          <span className="text-[11px] text-[#8a93a6]">开放资源</span>
        )}
      </div>
    </button>
  );
}

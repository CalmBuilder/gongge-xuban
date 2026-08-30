import type { ReactNode } from 'react';

import {
  RESOURCE_CARD_CLASS,
  RESOURCE_CARD_DESCRIPTION_CLASS,
  RESOURCE_CARD_FOOTER_CLASS,
  RESOURCE_CARD_IDENTITY_CLASS,
} from '@/lib/enterprise-ui';
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
    meta: 'text-[var(--gg-capability)]',
    tag: 'bg-[var(--gg-capability-soft)] text-[var(--gg-capability)]',
    card: 'border-[var(--gg-capability-line)] hover:border-[var(--gg-capability)]',
    identity: 'bg-[var(--gg-capability-soft)]',
  },
  blue: {
    meta: 'text-[var(--gg-interaction)]',
    tag: 'bg-[var(--gg-interaction-soft)] text-[var(--gg-interaction)]',
    card: 'border-[var(--gg-line)] hover:border-[var(--gg-interaction)]',
    identity: 'bg-[var(--gg-interaction-soft)]',
  },
  indigo: {
    meta: 'text-[var(--gg-governance)]',
    tag: 'bg-[var(--gg-governance-soft)] text-[var(--gg-governance)]',
    card: 'border-[var(--gg-governance-line)] hover:border-[var(--gg-governance)]',
    identity: 'bg-[var(--gg-governance-soft)]',
  },
  orange: {
    meta: 'text-[var(--gg-state-warning)]',
    tag: 'bg-[var(--gg-state-warning-soft)] text-[var(--gg-state-warning)]',
    card: 'border-[#F1D9B2] hover:border-[var(--gg-state-warning)]',
    identity: 'bg-[var(--gg-state-warning-soft)]',
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
        'gongge-platform-resource-card',
        RESOURCE_CARD_CLASS,
        accentStyles.card,
        className,
      )}
    >
      <div data-resource-identity className={cn(
        RESOURCE_CARD_IDENTITY_CLASS,
        accentStyles.identity,
      )}>
        {icon ?? (
          <PlazaResourceIcon kind="knowledge" />
        )}
        <div className="flex min-w-0 flex-1 flex-col gap-[4px]">
          <p className="gg-type-card-title truncate">{title}</p>
          <p className={cn('gg-type-meta truncate', accentStyles.meta)}>{meta}</p>
        </div>
      </div>

      <p className={RESOURCE_CARD_DESCRIPTION_CLASS}>
        {description}
      </p>

      <div className={RESOURCE_CARD_FOOTER_CLASS}>
        {tags && tags.length > 0 ? (
          tags.slice(0, 2).map((tag) => (
            <span
              key={tag}
              className={cn(
                'gg-type-caption inline-flex min-w-0 max-w-[50%] items-center truncate rounded-[var(--gg-radius-control)] px-[8px] py-[3px]',
                accentStyles.tag,
              )}
            >
              {tag}
            </span>
          ))
        ) : (
          <span className="gg-type-caption">开放资源</span>
        )}
      </div>
    </button>
  );
}

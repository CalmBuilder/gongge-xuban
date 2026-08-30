import type { ReactNode } from 'react';

import {
  RESOURCE_CARD_AVATAR_SLOT_CLASS,
  RESOURCE_CARD_CLASS,
  RESOURCE_CARD_IDENTITY_CLASS,
} from '@/lib/enterprise-ui';
import { cn } from '@/lib/utils';

import IconArrowRight from '../../assets/icons/arrow-right.svg?react';

export type PlatformStat = {
  value: ReactNode;
  label: string;
};

export type PlatformEmployeeCardProps = {
  /** Avatar illustration, typically an <EmployeeAvatar />. */
  avatar: ReactNode;
  name: ReactNode;
  role: ReactNode;
  online?: boolean;
  description: ReactNode;
  /** Bottom metric segments (资料 / 技能 / SOP …). */
  stats: PlatformStat[];
  onOpen?: () => void;
  className?: string;
};

/**
 * Compact 数字员工广场 card. A soft blue banner keeps the portrait inside a
 * fixed artwork column, followed by a two-line description and a joined stat row.
 */
export default function PlatformEmployeeCard({
  avatar,
  name,
  role,
  online = true,
  description,
  stats,
  onOpen,
  className,
}: PlatformEmployeeCardProps) {
  return (
    <button
      type="button"
      onClick={onOpen}
      className={cn(
        'group justify-end',
        RESOURCE_CARD_CLASS,
        'border-[var(--gg-line)] p-[6px] hover:border-[var(--gg-interaction)]',
        className,
      )}
    >
      <div className="flex w-full flex-col px-[4px]">
        <div data-resource-identity className={cn(
          RESOURCE_CARD_IDENTITY_CLASS,
          'mt-0 h-[72px] bg-[var(--gg-interaction-soft)]',
        )}>
          <div data-avatar-slot className={RESOURCE_CARD_AVATAR_SLOT_CLASS}>
            {avatar}
          </div>
          <div className="min-w-0 flex flex-col items-start justify-center gap-[2px]">
              <p className="gg-type-card-title truncate">{name}</p>
              <p className="gg-type-meta truncate">{role}</p>
              <span className="inline-flex items-center justify-center rounded-full bg-white px-[6px] py-[2px]">
                <span className="flex items-center gap-[3px]">
                  <i
                    className={cn('size-[5px] shrink-0 rounded-full', online ? 'bg-[var(--gg-state-success)]' : 'bg-[var(--gg-text-muted)]')}
                    aria-hidden="true"
                  />
                  <span className="gg-type-caption">{online ? '在线' : '下线'}</span>
                </span>
              </span>
          </div>
          <span className="grid size-[24px] shrink-0 self-center place-items-center rounded-[var(--gg-radius-control)] bg-[var(--gg-surface)] text-[var(--gg-text-muted)] transition-colors group-hover:bg-[var(--gg-interaction)] group-hover:text-white">
            <IconArrowRight className="size-[14px]" />
          </span>
        </div>
      </div>

      <p className="gg-type-body line-clamp-3 min-h-[66px] w-full px-[8px]">
        {description}
      </p>

      <div className="flex w-full items-stretch px-[8px] pb-[4px]">
        {stats.map((stat, index) => (
          <div
            key={stat.label}
            className={cn(
              'flex h-[36px] flex-1 items-center justify-center border border-[var(--gg-line)] bg-[var(--gg-surface-subtle)] px-[10px]',
              index === 0 && 'rounded-l-[var(--gg-radius-control)]',
              index === stats.length - 1 && 'rounded-r-[var(--gg-radius-control)]',
              index > 0 && 'border-l-0',
            )}
          >
            <span className="flex items-baseline gap-[2px]">
              <span className="gg-type-caption font-semibold text-[var(--gg-text-primary)]">{stat.value}</span>
              <span className="gg-type-caption text-[var(--gg-text-secondary)]">{stat.label}</span>
            </span>
          </div>
        ))}
      </div>
    </button>
  );
}

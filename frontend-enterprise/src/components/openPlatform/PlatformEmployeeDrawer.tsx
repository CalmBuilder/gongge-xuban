import type { ReactNode } from 'react';

import { Sheet, SheetContent } from '@/components/ui';
import { DETAIL_ACTIONS_CLASS, DETAIL_FACT_CARD_CLASS } from '@/lib/enterprise-ui';
import { cn } from '@/lib/utils';
import { XIcon } from 'lucide-react';

import IconChevronDown from '../../assets/icons/chevron-down.svg?react';
import IconTrash from '../../assets/icons/trash.svg?react';
import EmployeeAvatar from '../EmployeeAvatar';
import type { AgentProfileRead } from '../../types';
import type { EmployeeStatusKind } from '../../employee';

import type { PlatformStat } from './PlatformEmployeeCard';

export type PlatformEmployeeDrawerProps = {
  open: boolean;
  agent: AgentProfileRead;
  platformTitle: string;
  name: ReactNode;
  role: ReactNode;
  description: ReactNode;
  detailText: ReactNode;
  workStyles: string[];
  stats: PlatformStat[];
  online?: boolean;
  /** Override the status text for a published template, which is not an online employee. */
  statusLabel?: string;
  /** Distinguishes a published template from a live employee in the detail drawer. */
  statusKind?: EmployeeStatusKind;
  canManage?: boolean;
  deleting?: boolean;
  hasPrev?: boolean;
  hasNext?: boolean;
  onClose: () => void;
  onPrev?: () => void;
  onNext?: () => void;
  onDelete?: () => void;
  onUse: () => void;
  onCopy: () => void;
};

function DrawerDivider() {
  return <div className="h-px w-full shrink-0 bg-[var(--gg-line)]" />;
}

function NavChevron({
  direction,
  disabled,
  onClick,
  label,
}: {
  direction: 'prev' | 'next';
  disabled?: boolean;
  onClick?: () => void;
  label: string;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      disabled={disabled}
      onClick={onClick}
      className="grid size-[24px] place-items-center rounded-[var(--gg-radius-control)] text-[var(--gg-text-muted)] transition-colors enabled:hover:bg-[var(--gg-surface-subtle)] enabled:hover:text-[var(--gg-text-primary)] disabled:cursor-not-allowed disabled:opacity-35 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--gg-interaction)]"
    >
      <IconChevronDown
        className={cn('size-[14px]', direction === 'prev' ? 'rotate-90' : '-rotate-90')}
      />
    </button>
  );
}

/**
 * 共格 数字员工广场详情侧拉（Figma 298:1416）。
 */
export default function PlatformEmployeeDrawer({
  open,
  agent,
  platformTitle,
  name,
  role,
  description,
  detailText,
  workStyles,
  stats,
  online = true,
  statusLabel,
  statusKind,
  canManage = false,
  deleting = false,
  hasPrev = false,
  hasNext = false,
  onClose,
  onPrev,
  onNext,
  onDelete,
  onUse,
  onCopy,
}: PlatformEmployeeDrawerProps) {
  const effectiveStatusKind: EmployeeStatusKind = statusKind
    || (statusLabel ? 'available' : online ? 'online' : 'offline');
  const statusPresentation = {
    online: { badge: 'border-[var(--gg-capability-line)] bg-[var(--gg-state-success-soft)] text-[var(--gg-state-success)]', dot: 'bg-[var(--gg-state-success)]' },
    available: { badge: 'border-[var(--gg-line)] bg-[var(--gg-interaction-soft)] text-[var(--gg-interaction)]', dot: 'bg-[var(--gg-interaction)]' },
    offline: { badge: 'border-[var(--gg-line)] bg-[var(--gg-state-neutral-soft)] text-[var(--gg-text-muted)]', dot: 'bg-[var(--gg-text-muted)]' },
  }[effectiveStatusKind];
  return (
    <Sheet open={open} onOpenChange={(next) => { if (!next) onClose(); }}>
      <SheetContent
        side="right"
        showCloseButton={false}
        data-detail-container="drawer"
        className={cn(
          'platform-employee-drawer gg-detail-surface gg-detail-surface--drawer flex w-[400px] flex-col gap-[10px] border border-[var(--gg-line)] bg-[var(--gg-surface)] p-[16px_20px] shadow-[0_12px_32px_rgba(24,33,61,0.14)] sm:max-w-[400px]',
          'top-[24px]! right-[24px]! bottom-[24px]! left-auto! h-auto! max-h-[calc(100vh-48px)] rounded-[20px]',
          '',
        )}
      >
        <div className="flex w-full shrink-0 flex-col gap-[10px]">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-[4px]">
              <span className="gg-type-meta capitalize text-[var(--gg-text-secondary)]">
                {platformTitle}
              </span>
              <NavChevron direction="prev" disabled={!hasPrev} onClick={onPrev} label="上一位员工" />
              <NavChevron direction="next" disabled={!hasNext} onClick={onNext} label="下一位员工" />
            </div>
            <button
              type="button"
              aria-label="关闭"
              onClick={onClose}
              className="grid size-[24px] place-items-center rounded-[var(--gg-radius-control)] text-[var(--gg-text-muted)] transition-colors hover:bg-[var(--gg-surface-subtle)] hover:text-[var(--gg-text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--gg-interaction)]"
            >
              <XIcon className="size-[14px]" strokeWidth={1.75} />
            </button>
          </div>
          <DrawerDivider />
        </div>

        <div className="flex min-h-0 flex-1 flex-col gap-[10px] overflow-auto overscroll-contain px-[4px] pt-[48px]">
          <div className="flex w-full items-end gap-[10px] pb-[4px]">
            <div className="flex h-[117.5px] w-[100px] shrink-0 items-end justify-center overflow-hidden">
              <EmployeeAvatar
                agent={agent}
                width={100}
                height={118}
                fit="contain"
                objectPosition="center bottom"
                className="overflow-visible! rounded-none! border-0! bg-transparent! bg-none! shadow-none! after:hidden!"
              />
            </div>
            <div className="flex min-w-0 flex-1 flex-col justify-center gap-[8px] pb-[2px]">
              <div className="flex flex-col gap-[4px]">
                <p className="gg-type-card-title truncate capitalize">
                  {name}
                </p>
                <p className="gg-type-body line-clamp-2">
                  {description}
                </p>
              </div>
              <span
                className={cn(
                  'gg-type-caption inline-flex w-fit items-center gap-[4px] rounded-full border px-[10px] py-[4px]',
                  statusPresentation.badge,
                )}
              >
                <i
                  className={cn('size-[4px] shrink-0 rounded-full shadow-[inset_1px_1px_2px_0.5px_rgba(0,0,0,0.05)]', statusPresentation.dot)}
                  aria-hidden="true"
                />
                <span className="capitalize">{statusLabel || (online ? '在线' : '下线')}</span>
              </span>
            </div>
          </div>

          <div className="flex w-full items-stretch">
            {stats.map((stat, index) => (
              <div
                key={stat.label}
                className={cn(
                  'gg-type-control flex h-[60px] flex-1 flex-col justify-center gap-[4px] border border-[var(--gg-line)] bg-[var(--gg-surface-subtle)] px-[20px] py-[8px]',
                  index === 0 && 'rounded-l-[14px]',
                  index === stats.length - 1 && 'rounded-r-[14px]',
                  index > 0 && 'border-l-0',
                )}
              >
                <strong className="gg-type-section-title font-semibold text-[var(--gg-text-primary)]">{stat.value}</strong>
                <span className="gg-type-caption text-[var(--gg-text-secondary)]">{stat.label}</span>
              </div>
            ))}
          </div>

          <div className="grid grid-cols-2 gap-[10px]">
            <div className={cn('flex min-h-[60px] flex-col justify-center gap-[4px] px-[16px] py-[8px]', DETAIL_FACT_CARD_CLASS)}>
              <span className="gg-type-caption text-[var(--gg-text-secondary)]">分类</span>
              <strong className="gg-type-control truncate font-semibold text-[var(--gg-text-primary)]">{platformTitle}</strong>
            </div>
            <div className={cn('flex min-h-[60px] flex-col justify-center gap-[4px] px-[16px] py-[8px]', DETAIL_FACT_CARD_CLASS)}>
              <span className="gg-type-caption text-[var(--gg-text-secondary)]">分类</span>
              <strong className="gg-type-control truncate font-semibold text-[var(--gg-text-primary)]">{role}</strong>
            </div>
          </div>

          <div className="flex min-h-0 flex-1 flex-col gap-[8px]">
            <span className="gg-type-control capitalize text-[var(--gg-text-primary)]">说明</span>
            <div className="flex min-h-0 flex-1 flex-col gap-[10px]">
              {workStyles.length > 0 && (
                <div className="flex flex-wrap gap-[10px]">
                  {workStyles.slice(0, 3).map((tag) => (
                    <span
                      key={tag}
                      className="gg-type-caption rounded-[var(--gg-radius-control)] bg-[var(--gg-state-neutral-soft)] px-[12px] py-[4px]"
                    >
                      {tag}
                    </span>
                  ))}
                </div>
              )}
              <p className="gg-type-body">
                {detailText}
              </p>
            </div>
          </div>
        </div>

        <div className={DETAIL_ACTIONS_CLASS}>
          {canManage && onDelete && (
            <button
              type="button"
              disabled={deleting}
              onClick={onDelete}
              className="gg-type-control inline-flex h-[34px] w-[80px] items-center justify-center gap-[4px] rounded-[var(--gg-radius-control)] border border-[var(--gg-line)] bg-[var(--gg-surface)] text-[var(--gg-text-muted)] transition-colors hover:border-[var(--gg-state-danger)] hover:bg-[var(--gg-state-danger-soft)] hover:text-[var(--gg-state-danger)] disabled:cursor-not-allowed disabled:opacity-50"
            >
              <IconTrash className="size-[14px]" />
              删除
            </button>
          )}
          <button
            type="button"
            onClick={onCopy}
            className="gg-type-control inline-flex h-[34px] items-center justify-center rounded-[var(--gg-radius-control)] border border-[var(--gg-line)] bg-[var(--gg-surface)] px-[14px] font-semibold text-[var(--gg-text-secondary)] transition-colors hover:border-[var(--gg-interaction)] hover:bg-[var(--gg-interaction-soft)] hover:text-[var(--gg-interaction)]"
          >
            复制并定制
          </button>
          <button
            type="button"
            onClick={onUse}
            className="gg-type-control inline-flex h-[34px] items-center justify-center rounded-[var(--gg-radius-control)] bg-[var(--gg-interaction)] px-[14px] font-semibold text-white transition-colors hover:bg-[var(--gg-interaction-hover)]"
          >
            添加使用并开始对话
          </button>
        </div>
      </SheetContent>
    </Sheet>
  );
}

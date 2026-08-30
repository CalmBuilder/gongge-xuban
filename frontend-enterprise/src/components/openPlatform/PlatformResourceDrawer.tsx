import type { ReactNode } from 'react';

import { Sheet, SheetContent } from '@/components/ui';
import {
  DETAIL_ACTIONS_CLASS,
  DETAIL_FACT_CARD_CLASS,
  OUTLINE_ACTION_BUTTON_SM_CLASS,
} from '@/lib/enterprise-ui';
import { cn } from '@/lib/utils';
import { XIcon } from 'lucide-react';
import { Link } from 'react-router-dom';

import IconChevronDown from '../../assets/icons/chevron-down.svg?react';
import IconTrash from '../../assets/icons/trash.svg?react';

import { platformResourceAccentStyles, type PlatformResourceAccent } from './PlatformResourceCard';

export type PlatformResourceDrawerProps = {
  open: boolean;
  platformTitle: string;
  icon: ReactNode;
  accent?: PlatformResourceAccent;
  title: ReactNode;
  description: ReactNode;
  badge: ReactNode;
  categoryMeta: ReactNode;
  detailText: ReactNode;
  useLabel: string;
  detailsHref?: string;
  canManage?: boolean;
  deleting?: boolean;
  hasPrev?: boolean;
  hasNext?: boolean;
  onClose: () => void;
  onPrev?: () => void;
  onNext?: () => void;
  onDelete?: () => void;
  onUse: () => void;
};

const DRAWER_SHEET_CLASS = cn(
  'platform-resource-drawer flex w-[400px] flex-col gap-[10px] border border-[var(--gg-line)] bg-[var(--gg-surface)] p-[16px_20px] shadow-[0_12px_32px_rgba(24,33,61,0.14)] sm:max-w-[400px]',
  'top-[24px]! right-[24px]! bottom-[24px]! left-auto! h-auto! max-h-[calc(100vh-48px)] rounded-[20px]',
  '',
);

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
 * 共格 广场资源详情侧拉（知识库 298:4801 / SOP·技能·工具 298:4869 系列）。
 */
export default function PlatformResourceDrawer({
  open,
  platformTitle,
  icon,
  accent = 'green',
  title,
  description,
  badge,
  categoryMeta,
  detailText,
  useLabel,
  detailsHref,
  canManage = false,
  deleting = false,
  hasPrev = false,
  hasNext = false,
  onClose,
  onPrev,
  onNext,
  onDelete,
  onUse,
}: PlatformResourceDrawerProps) {
  const accentStyles = platformResourceAccentStyles[accent];

  return (
    <Sheet open={open} onOpenChange={(next) => { if (!next) onClose(); }}>
      <SheetContent side="right" showCloseButton={false} className={DRAWER_SHEET_CLASS}>
        <div className="flex w-full shrink-0 flex-col gap-[10px]">
          <div className="flex min-h-[24px] items-center justify-between">
            <div className="flex items-center gap-[4px]">
              <span className="gg-type-meta capitalize text-[var(--gg-text-secondary)]">
                {platformTitle}
              </span>
              <NavChevron direction="prev" disabled={!hasPrev} onClick={onPrev} label="上一项" />
              <NavChevron direction="next" disabled={!hasNext} onClick={onNext} label="下一项" />
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

        <div className="flex min-h-0 flex-1 flex-col gap-[10px] overflow-auto overscroll-contain px-[4px]">
          <div className="size-[36px] shrink-0">{icon}</div>

          <div className="flex min-h-[75px] w-full flex-col justify-center gap-[8px] pb-[2px]">
            <div className="flex flex-col gap-[4px]">
              <p className="gg-type-card-title capitalize">
                {title}
              </p>
              <p className="gg-type-body">
                {description}
              </p>
            </div>
            <span
              className={cn(
                'gg-type-caption inline-flex w-fit items-center rounded-[var(--gg-radius-control)] px-[10px] py-[4px] capitalize',
                accentStyles.tag,
              )}
            >
              {badge}
            </span>
          </div>

          <div className="grid grid-cols-2 gap-[10px]">
            <div className={cn('flex min-h-[60px] flex-col justify-center gap-[4px] px-[16px] py-[8px]', DETAIL_FACT_CARD_CLASS)}>
              <span className="gg-type-caption text-[var(--gg-text-secondary)]">分类</span>
              <strong className="gg-type-control truncate font-semibold text-[var(--gg-text-primary)]">
                {platformTitle}
              </strong>
            </div>
            <div className={cn('flex min-h-[60px] flex-col justify-center gap-[4px] px-[16px] py-[8px]', DETAIL_FACT_CARD_CLASS)}>
              <span className="gg-type-caption text-[var(--gg-text-secondary)]">分类</span>
              <strong className={cn('gg-type-control truncate font-semibold', accentStyles.meta)}>
                {categoryMeta}
              </strong>
            </div>
          </div>

          <div className="flex min-h-0 flex-1 flex-col gap-[8px]">
            <span className="gg-type-control capitalize text-[var(--gg-text-primary)]">说明</span>
            <p className="gg-type-body">
              {detailText}
            </p>
          </div>
        </div>

        <div className={DETAIL_ACTIONS_CLASS}>
          {detailsHref && (
            <Link
              to={detailsHref}
              className={cn(OUTLINE_ACTION_BUTTON_SM_CLASS, 'h-[34px] px-[16px] text-[var(--gg-text-primary)]')}
            >
              查看详情
            </Link>
          )}
          {canManage && onDelete && (
            <button
              type="button"
              disabled={deleting}
              onClick={onDelete}
              className="inline-flex h-[34px] w-[80px] items-center justify-center gap-[4px] rounded-[var(--gg-radius-control)] border border-[var(--gg-state-danger)] bg-[var(--gg-surface)] text-[12px] font-medium text-[var(--gg-state-danger)] transition-colors hover:bg-[var(--gg-state-danger-soft)] disabled:cursor-not-allowed disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--gg-state-danger)]"
            >
              <IconTrash className="size-[14px]" />
              删除
            </button>
          )}
          <button
            type="button"
            onClick={onUse}
            className="inline-flex h-[34px] items-center justify-center rounded-[var(--gg-radius-control)] bg-[var(--gg-interaction)] px-[20px] text-[12px] font-semibold text-white transition-colors hover:bg-[var(--gg-interaction-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--gg-interaction)]"
          >
            {useLabel}
          </button>
        </div>
      </SheetContent>
    </Sheet>
  );
}

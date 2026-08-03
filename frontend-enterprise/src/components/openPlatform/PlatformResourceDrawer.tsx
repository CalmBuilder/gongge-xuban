import type { ReactNode } from 'react';

import { Sheet, SheetContent } from '@/components/ui';
import { cn } from '@/lib/utils';
import { XIcon } from 'lucide-react';

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
  'platform-resource-drawer flex w-[400px] flex-col gap-[10px] border-[0.5px] border-[#e3e7f1] bg-white p-[16px_20px] shadow-[0_4px_15px_rgba(0,0,0,0.25)] sm:max-w-[400px]',
  'top-[24px]! right-[24px]! bottom-[24px]! left-auto! h-auto! max-h-[calc(100vh-48px)] rounded-[20px]',
  '',
);

function DrawerDivider() {
  return <div className="h-px w-full shrink-0 bg-[#e3e7f1]" />;
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
      className="grid size-[14px] place-items-center text-[#757f9c] transition-colors enabled:hover:text-[#18181a] disabled:cursor-not-allowed disabled:opacity-35"
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
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-[4px]">
              <span className="text-[12px] font-medium capitalize text-[#464c5e]">
                {platformTitle}
              </span>
              <NavChevron direction="prev" disabled={!hasPrev} onClick={onPrev} label="上一项" />
              <NavChevron direction="next" disabled={!hasNext} onClick={onNext} label="下一项" />
            </div>
            <button
              type="button"
              aria-label="关闭"
              onClick={onClose}
              className="grid size-[14px] place-items-center text-[#757f9c] transition-colors hover:text-[#18181a]"
            >
              <XIcon className="size-[14px]" strokeWidth={1.75} />
            </button>
          </div>
          <DrawerDivider />
        </div>

        <div className="flex min-h-0 flex-1 flex-col gap-[10px] overflow-auto px-[4px]">
          <div className="size-[36px] shrink-0">{icon}</div>

          <div className="flex min-h-[75px] w-full flex-col justify-center gap-[8px] pb-[2px]">
            <div className="flex flex-col gap-[4px]">
              <p className="text-[16px] font-medium capitalize text-[#464c5e]">
                {title}
              </p>
              <p className="text-[12px] leading-[18px] text-[#757f9c]">
                {description}
              </p>
            </div>
            <span
              className={cn(
                'inline-flex w-fit items-center rounded-[90px] px-[10px] py-[4px] text-[10px] capitalize',
                accentStyles.tag,
              )}
            >
              {badge}
            </span>
          </div>

          <div className="grid grid-cols-2 gap-[10px]">
            <div className="flex min-h-[60px] flex-col justify-center gap-[4px] rounded-[14px] border-[0.5px] border-[#e3e7f1] px-[16px] py-[8px]">
              <span className="text-[10px] leading-[13px] text-[#464c5e]">分类</span>
              <strong className="truncate text-[12px] leading-[16px] font-medium text-[#18181a]">
                {platformTitle}
              </strong>
            </div>
            <div className="flex min-h-[60px] flex-col justify-center gap-[4px] rounded-[14px] border-[0.5px] border-[#e3e7f1] px-[16px] py-[8px]">
              <span className="text-[10px] leading-[13px] text-[#464c5e]">分类</span>
              <strong className={cn('truncate text-[12px] leading-[16px] font-medium', accentStyles.meta)}>
                {categoryMeta}
              </strong>
            </div>
          </div>

          <div className="flex min-h-0 flex-1 flex-col gap-[8px]">
            <span className="text-[12px] capitalize text-[#464c5e]">说明</span>
            <p className="text-[12px] leading-[20px] text-[#757f9c]">
              {detailText}
            </p>
          </div>
        </div>

        <DrawerDivider />

        <div className="flex shrink-0 justify-end gap-[10px]">
          {canManage && onDelete && (
            <button
              type="button"
              disabled={deleting}
              onClick={onDelete}
              className="inline-flex h-[34px] w-[80px] items-center justify-center gap-[4px] rounded-[10px] border-[0.5px] border-[#d20b0b] bg-white text-[12px] text-[#d20b0b] transition-colors hover:bg-[#fce7e7] disabled:cursor-not-allowed disabled:opacity-50"
            >
              <IconTrash className="size-[14px]" />
              删除
            </button>
          )}
          <button
            type="button"
            onClick={onUse}
            className="inline-flex h-[34px] items-center justify-center rounded-[var(--gg-radius-control)] bg-[var(--gg-cobalt)] px-[20px] text-[12px] font-semibold text-white transition-colors hover:bg-[#244bc7]"
          >
            {useLabel}
          </button>
        </div>
      </SheetContent>
    </Sheet>
  );
}

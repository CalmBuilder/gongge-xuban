import type { ReactNode } from 'react';

import { cn } from '@/lib/utils';

export type StatCardTone = 'default' | 'green' | 'red';

const SURFACE_CLASS: Record<StatCardTone, string> = {
  default: 'border-[var(--gg-line)] bg-[var(--gg-surface)]',
  green: 'border-[var(--gg-capability-line)] bg-[var(--gg-state-success-soft)]',
  red: 'border-[var(--gg-state-danger)] bg-[var(--gg-state-danger-soft)]',
};
const VALUE_CLASS: Record<StatCardTone, string> = {
  default: 'text-[var(--gg-text-primary)]',
  green: 'text-[var(--gg-state-success)]',
  red: 'text-[var(--gg-state-danger)]',
};
const LABEL_CLASS: Record<StatCardTone, string> = {
  default: 'text-[var(--gg-text-secondary)]',
  green: 'text-[var(--gg-state-success)]',
  red: 'text-[var(--gg-state-danger)]',
};

export type StatCardProps = {
  value: ReactNode;
  label: ReactNode;
  /** Colour accent. `default` = neutral grey card, `green`/`red` = tinted. */
  tone?: StatCardTone;
  /** Extra classes for the big value (e.g. a custom colour). */
  valueClassName?: string;
  /** Extra classes for the outer card (e.g. override the flex basis). */
  className?: string;
};

/**
 * Metric card used across the enterprise pages (定时任务 / 对话日志 / 技能 …):
 * a rounded tinted surface with a large value and a trailing label.
 */
export function StatCard({ value, label, tone = 'default', valueClassName, className }: StatCardProps) {
  return (
    <div
      data-card-family="metric"
      className={cn(
        'gg-stat-card flex min-h-[136px] flex-1 basis-[220px] items-center rounded-[var(--gg-radius-card)] border px-[24px] py-[20px]',
        SURFACE_CLASS[tone],
        className,
      )}
    >
      <div className="flex min-w-0 items-end gap-[6px]">
        <span className={cn('gg-type-metric shrink-0', VALUE_CLASS[tone], valueClassName)}>
          {value}
        </span>
        <span className={cn('gg-type-meta truncate', LABEL_CLASS[tone])}>{label}</span>
      </div>
    </div>
  );
}

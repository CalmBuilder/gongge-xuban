import type { ReactNode } from 'react';

import { cn } from '@/lib/utils';

import { BADGE_TONE_CLASS, RUN_STATUS_BADGE, TASK_STATUS_BADGE, type BadgeTone } from './shared';

export function StatusBadge({ tone, children }: { tone: BadgeTone; children: ReactNode }) {
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-full px-[12px] py-[4px] text-[10px] leading-none whitespace-nowrap capitalize',
        BADGE_TONE_CLASS[tone],
      )}
    >
      {children}
    </span>
  );
}

export function TaskStatusBadge({ status }: { status: string }) {
  const { tone, text } = TASK_STATUS_BADGE[status] || TASK_STATUS_BADGE.archived;
  return <StatusBadge tone={tone}>{text}</StatusBadge>;
}

export function TaskRunResultBadge({ status }: { status: string }) {
  const preset = RUN_STATUS_BADGE[status] || { tone: 'gray' as BadgeTone, text: status || '暂无' };
  return <StatusBadge tone={preset.tone}>{preset.text}</StatusBadge>;
}

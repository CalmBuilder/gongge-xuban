import type { ReactNode } from 'react';

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui';
import { cn } from '@/lib/utils';

import IconChevronDown from '../assets/icons/chevron-down.svg?react';
import IconLogout from '../assets/icons/logout.svg?react';
import LanguageSwitcher from './LanguageSwitcher';

export type AppHeaderProps = {
  /**
   * Page-specific content rendered on the left side of the header. When
   * provided it takes precedence over the `title` / `description` fields.
   */
  left?: ReactNode;
  /** Convenience field for the left slot's title line. Ignored when `left` is set. */
  title?: ReactNode;
  /** Convenience field for the left slot's description line. Ignored when `left` is set. */
  description?: ReactNode;
  /**
   * Custom content for the right side of the header. When provided it fully
   * replaces the default user avatar / logout dropdown (used e.g. on the
   * signed-out login page which shows a theme toggle + login button instead).
   */
  right?: ReactNode;
  /** Called when the logout menu item is clicked. */
  onLogout?: () => void;
  /** Current user's display name, used for the avatar initial. */
  userName?: string;
  className?: string;
};

/**
 * Global page header. The right side shows a user avatar button whose dropdown
 * holds the logout action; the left side is provided per-page via the `left`
 * slot, or via the `title` / `description` convenience fields. When `left` is
 * passed it is rendered as-is and the convenience fields are ignored.
 * Pass `right` to override the default avatar with page-specific actions.
 */
export default function AppHeader({
  left,
  title,
  description,
  right,
  onLogout,
  userName,
  className,
}: AppHeaderProps) {
  const initial = userName?.trim()?.[0]?.toUpperCase();

  const leftContent = left ?? (
    (title !== undefined || description !== undefined) ? (
      <div className="flex min-h-[40px] flex-col justify-center gap-[4px]">
        {title !== undefined && (
          <p className="gg-type-section-title">{title}</p>
        )}
        {description !== undefined && (
          <p className="gg-type-meta">{description}</p>
        )}
      </div>
    ) : null
  );

  return (
    <header className={cn('app-header flex w-full items-start gap-[16px]', className)}>
      <div className="min-w-0 flex-1">{leftContent}</div>
      <div className="flex h-[32px] shrink-0 items-center gap-[8px]">
        <LanguageSwitcher />
        {right !== undefined ? right : (
          <DropdownMenu>
            <DropdownMenuTrigger
              aria-label="账户菜单"
              className="flex h-[32px] shrink-0 items-center gap-[8px] rounded-[10px] pl-[4px] pr-[8px] outline-none"
            >
              <span className="grid size-[32px] shrink-0 place-items-center overflow-hidden rounded-full bg-[var(--gg-interaction-soft)] gg-type-body font-medium text-[var(--gg-interaction)]">
                {initial ?? '--'}
              </span>
              <IconChevronDown className="size-[14px] shrink-0 text-[var(--gg-text-muted)]" />
            </DropdownMenuTrigger>
            <DropdownMenuContent
              align="end"
              className="w-fit min-w-0 rounded-[var(--gg-radius-card)] border border-[var(--gg-line)] bg-[var(--gg-surface)] p-[6px] shadow-[var(--gg-shadow-card)] ring-0 [--accent:var(--gg-interaction-soft)] [--accent-foreground:var(--gg-text-primary)]"
            >
              <DropdownMenuItem
                onSelect={() => onLogout?.()}
                className="gg-type-control h-[36px] cursor-pointer gap-2 rounded-[var(--gg-radius-control)] px-[12px] text-[var(--gg-text-secondary)]"
              >
                <IconLogout className="size-[16px]" />
                退出登录
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        )}
      </div>
    </header>
  );
}

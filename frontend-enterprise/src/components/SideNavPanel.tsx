import type { ComponentType, ReactNode } from 'react';
import { Link } from 'react-router-dom';

import { cn } from '@/lib/utils';
import { DETAIL_PANEL_CLASS } from '@/lib/enterprise-ui';

export type SideNavPanelChild = {
  key: string;
  label: string;
  count?: number;
};

export type SideNavPanelItem = {
  key: string;
  label: string;
  description?: string;
  count?: number;
  icon: ComponentType<{ className?: string; 'aria-hidden'?: boolean | 'true' | 'false' }>;
  children?: SideNavPanelChild[];
};

export type SideNavPanelProps = {
  title: string;
  subtitle?: string;
  icon: ComponentType<{ className?: string; 'aria-hidden'?: boolean | 'true' | 'false' }>;
  items: SideNavPanelItem[];
  activeKey: string;
  activeChildKey?: string;
  linkFor: (key: string, childKey?: string) => string;
  footer?: ReactNode;
  'aria-label'?: string;
};

/**
 * 三栏管理页共用的中间导航面板（侧栏 → 本面板 → 工作区）。
 * 视觉与交互与组织角色页保持一致；导航一律走 URL（Link），支持后退与分享。
 */
export default function SideNavPanel({
  title,
  subtitle,
  icon: HeaderIcon,
  items,
  activeKey,
  activeChildKey = '',
  linkFor,
  footer,
  'aria-label': ariaLabel = title,
}: SideNavPanelProps) {
  return (
    <aside className={cn(DETAIL_PANEL_CLASS, 'overflow-hidden p-0 max-[920px]:overflow-x-auto')}>
      <div className="border-b border-[var(--gg-line)] px-[18px] py-[17px] max-[920px]:min-w-[680px]">
        <div className="flex items-center gap-[9px]">
          <span className="grid size-[32px] place-items-center rounded-[var(--gg-radius-control)] bg-[var(--gg-interaction-soft)] text-[var(--gg-interaction)]">
            <HeaderIcon aria-hidden="true" className="size-[16px]" />
          </span>
          <div className="min-w-0">
            <h2 className="gg-type-card-title">{title}</h2>
            {subtitle ? <p className="gg-type-caption mt-[1px]">{subtitle}</p> : null}
          </div>
        </div>
      </div>
      <nav aria-label={ariaLabel} className="grid gap-[3px] p-[8px] max-[920px]:flex max-[920px]:min-w-[680px]">
        {items.map((item) => {
          const Icon = item.icon;
          const selected = activeKey === item.key;
          return (
            <div key={item.key} className="max-[920px]:contents">
              <Link
                aria-current={selected ? 'page' : undefined}
                className={cn(
                  'group gg-type-control flex min-w-0 items-center gap-[10px] rounded-[var(--gg-radius-control)] px-[11px] py-[10px] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--gg-interaction)] max-[920px]:min-w-[158px]',
                  selected
                    ? 'bg-[var(--gg-interaction-soft)] text-[var(--gg-interaction-hover)]'
                    : 'text-[var(--gg-text-secondary)] hover:bg-[var(--gg-surface-subtle)] hover:text-[var(--gg-text-primary)]',
                )}
                to={linkFor(item.key)}
              >
                <Icon aria-hidden="true" className="size-[16px] shrink-0" />
                <span className="min-w-0 flex-1">
                    <strong className="block truncate gg-type-meta font-semibold">{item.label}</strong>
                  {item.description ? (
                    <small className="gg-type-caption block truncate font-normal opacity-70">{item.description}</small>
                  ) : null}
                </span>
                {item.count != null ? (
                  <span className="font-mono gg-type-caption tabular-nums opacity-60">{item.count}</span>
                ) : null}
              </Link>
              {selected && item.children?.length ? (
                <div
                  aria-label={`${item.label}子分类`}
                    className="ml-[37px] mt-[2px] grid gap-[1px] border-l border-[var(--gg-line)] pl-[8px] max-[920px]:hidden"
                >
                  {item.children.map((child) => {
                    const childSelected = activeChildKey === child.key;
                    return (
                      <Link
                        key={child.key || '__all__'}
                        aria-current={childSelected ? 'page' : undefined}
                        className={cn(
                          'gg-type-caption flex min-w-0 items-center justify-between gap-[8px] rounded-[var(--gg-radius-control)] px-[9px] py-[6px] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--gg-interaction)]',
                          childSelected
                            ? 'bg-[var(--gg-interaction-soft)] font-semibold text-[var(--gg-interaction-hover)]'
                            : 'text-[var(--gg-text-secondary)] hover:bg-[var(--gg-surface-subtle)] hover:text-[var(--gg-text-primary)]',
                        )}
                        to={linkFor(item.key, child.key)}
                      >
                        <span className="truncate">{child.label}</span>
                        {child.count != null ? (
                          <span className="font-mono gg-type-caption tabular-nums opacity-60">{child.count}</span>
                        ) : null}
                      </Link>
                    );
                  })}
                </div>
              ) : null}
            </div>
          );
        })}
      </nav>
      {footer ? (
        <div className="gg-type-caption border-t border-[var(--gg-line)] bg-[var(--gg-surface-subtle)] px-[16px] py-[13px] max-[920px]:hidden">
          {footer}
        </div>
      ) : null}
    </aside>
  );
}

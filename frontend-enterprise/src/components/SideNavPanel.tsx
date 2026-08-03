import type { ComponentType, ReactNode } from 'react';
import { Link } from 'react-router-dom';

import { cn } from '@/lib/utils';

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
    <aside className="overflow-hidden rounded-[18px] border border-[#dfe5f2] bg-white max-[920px]:overflow-x-auto">
      <div className="border-b border-[#e8ebf2] px-[18px] py-[17px] max-[920px]:min-w-[680px]">
        <div className="flex items-center gap-[9px]">
          <span className="grid size-[32px] place-items-center rounded-[9px] bg-[#edf2ff] text-[#3157e8]">
            <HeaderIcon aria-hidden="true" className="size-[16px]" />
          </span>
          <div className="min-w-0">
            <h2 className="text-[13px] font-semibold text-[#18181a]">{title}</h2>
            {subtitle ? <p className="mt-[1px] text-[11px] text-[#858b9c]">{subtitle}</p> : null}
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
                  'group flex min-w-0 items-center gap-[10px] rounded-[11px] px-[11px] py-[10px] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#3157e8] max-[920px]:min-w-[158px]',
                  selected
                    ? 'bg-[#edf2ff] text-[#244bc7]'
                    : 'text-[#526183] hover:bg-[#f6f8fc] hover:text-[#24262d]',
                )}
                to={linkFor(item.key)}
              >
                <Icon aria-hidden="true" className="size-[16px] shrink-0" />
                <span className="min-w-0 flex-1">
                  <strong className="block truncate text-[12px] font-semibold">{item.label}</strong>
                  {item.description ? (
                    <small className="block truncate text-[10px] font-normal opacity-70">{item.description}</small>
                  ) : null}
                </span>
                {item.count != null ? (
                  <span className="font-mono text-[11px] tabular-nums opacity-60">{item.count}</span>
                ) : null}
              </Link>
              {selected && item.children?.length ? (
                <div
                  aria-label={`${item.label}子分类`}
                  className="ml-[37px] mt-[2px] grid gap-[1px] border-l border-[#e8ebf2] pl-[8px] max-[920px]:hidden"
                >
                  {item.children.map((child) => {
                    const childSelected = activeChildKey === child.key;
                    return (
                      <Link
                        key={child.key || '__all__'}
                        aria-current={childSelected ? 'page' : undefined}
                        className={cn(
                          'flex min-w-0 items-center justify-between gap-[8px] rounded-[8px] px-[9px] py-[6px] text-[11px] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#3157e8]',
                          childSelected
                            ? 'bg-[#edf2ff] font-semibold text-[#244bc7]'
                            : 'text-[#526183] hover:bg-[#f6f8fc] hover:text-[#24262d]',
                        )}
                        to={linkFor(item.key, child.key)}
                      >
                        <span className="truncate">{child.label}</span>
                        {child.count != null ? (
                          <span className="font-mono text-[10px] tabular-nums opacity-60">{child.count}</span>
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
        <div className="border-t border-[#e8ebf2] bg-[#fafbfe] px-[16px] py-[13px] text-[11px] leading-[18px] text-[#68718b] max-[920px]:hidden">
          {footer}
        </div>
      ) : null}
    </aside>
  );
}

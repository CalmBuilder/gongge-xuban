import type { HTMLAttributes, ReactNode } from 'react';

import { cn } from '@/lib/utils';

export type PageTemplate =
  | 'workspace'
  | 'catalog'
  | 'management'
  | 'dashboard'
  | 'detail'
  | 'chat'
  | 'special';

export type PageShellProps = HTMLAttributes<HTMLDivElement> & {
  /** 页面族，用于审查、响应式规则和后续主题扩展。 */
  template?: PageTemplate;
  /** 紧凑页只收窄纵向间距，不改变全局横向边界。 */
  density?: 'default' | 'compact';
  children: ReactNode;
};

/**
 * 共格企业端页面统一外框：锁定内容列、左右留白和移动端收缩规则。
 */
export function PageShell({
  template = 'workspace',
  density = 'default',
  className,
  children,
  ...props
}: PageShellProps) {
  return (
    <div
      {...props}
      data-page-template={template}
      data-typography-contract="v1"
      className={cn(
        'gg-page-shell',
        'gg-typography-scope',
        density === 'compact' && 'gg-page-shell--compact',
        className,
      )}
    >
      {children}
    </div>
  );
}

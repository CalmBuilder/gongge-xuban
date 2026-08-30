import type { HTMLAttributes, ReactNode } from 'react';

import { cn } from '@/lib/utils';

export type DetailContainer = 'drawer' | 'dialog' | 'page';

export type DetailModelFact = {
  label: ReactNode;
  value: ReactNode;
};

export type DetailModel = {
  title: ReactNode;
  subtitle?: ReactNode;
  status?: ReactNode;
  description?: ReactNode;
  facts?: readonly DetailModelFact[];
};

export type DetailSurfaceProps = HTMLAttributes<HTMLDivElement> & {
  container: DetailContainer;
  children: ReactNode;
};

/**
 * 详情承载契约。抽屉、弹窗和独立详情页共享内容节奏，只由外层容器决定尺寸。
 */
export function DetailSurface({
  container,
  className,
  children,
  ...props
}: DetailSurfaceProps) {
  return (
    <div
      {...props}
      data-detail-container={container}
      className={cn('gg-detail-surface', `gg-detail-surface--${container}`, className)}
    >
      {children}
    </div>
  );
}

export type DetailSectionProps = HTMLAttributes<HTMLElement> & {
  title?: ReactNode;
  children: ReactNode;
};

/** 详情页中可复用的标题与内容分组。 */
export function DetailSection({ title, className, children, ...props }: DetailSectionProps) {
  return (
    <section {...props} className={cn('gg-detail-section', className)}>
      {title ? <h2 className="gg-type-card-title">{title}</h2> : null}
      <div className={title ? 'mt-[12px]' : undefined}>{children}</div>
    </section>
  );
}

export type DetailActionsProps = HTMLAttributes<HTMLDivElement> & {
  children: ReactNode;
};

/** 详情操作栏统一边界和按钮间距。 */
export function DetailActions({ className, children, ...props }: DetailActionsProps) {
  return (
    <div {...props} className={cn('gg-detail-actions', className)}>
      {children}
    </div>
  );
}

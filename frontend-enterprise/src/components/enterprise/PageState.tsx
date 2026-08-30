import type { ReactNode } from 'react';

import { cn } from '@/lib/utils';

export type PageStateKind = 'loading' | 'empty' | 'error' | 'forbidden';

export type PageStateProps = {
  kind: PageStateKind;
  title: ReactNode;
  description?: ReactNode;
  action?: ReactNode;
  icon?: ReactNode;
  className?: string;
};

/** 页面级加载、空数据和失败状态的统一语义容器。 */
export function PageState({
  kind,
  title,
  description,
  action,
  icon,
  className,
}: PageStateProps) {
  const isAssertive = kind === 'error' || kind === 'forbidden';
  return (
    <section
      className={cn('gg-page-state', `gg-page-state--${kind}`, className)}
      role={isAssertive ? 'alert' : 'status'}
      aria-live={isAssertive ? 'assertive' : 'polite'}
    >
      {icon ? <div className="gg-page-state__icon" aria-hidden="true">{icon}</div> : null}
      <h2 className="gg-type-card-title text-balance">{title}</h2>
      {description ? <p className="gg-type-body mt-[6px] max-w-[520px]">{description}</p> : null}
      {action ? <div className="mt-[16px]">{action}</div> : null}
    </section>
  );
}

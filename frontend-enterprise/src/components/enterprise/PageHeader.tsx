import type { ReactNode } from 'react';
import { ArrowLeft } from 'lucide-react';
import { Link } from 'react-router-dom';

import { cn } from '@/lib/utils';

export type PageHeaderProps = {
  title: ReactNode;
  description?: ReactNode;
  eyebrow?: ReactNode;
  backTo?: string;
  backLabel?: string;
  actions?: ReactNode;
  size?: 'page' | 'section';
  className?: string;
};

/**
 * 企业端内容页统一标题层级；返回动作使用真实链接，便于键盘和辅助技术识别。
 */
export function PageHeader({
  title,
  description,
  eyebrow,
  backTo,
  backLabel = '返回上一页',
  actions,
  size = 'page',
  className,
}: PageHeaderProps) {
  return (
    <div className={cn('gg-page-header', className)}>
      <div className="flex min-w-0 items-start gap-[12px]">
        {backTo ? (
          <Link
            to={backTo}
            aria-label={backLabel}
            className="grid size-[32px] shrink-0 place-items-center rounded-[var(--gg-radius-control)] text-[var(--gg-text-muted)] hover:bg-[var(--gg-interaction-soft)] hover:text-[var(--gg-text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--gg-interaction)]"
          >
            <ArrowLeft className="size-[16px]" aria-hidden="true" />
          </Link>
        ) : null}
        <div className="min-w-0">
          {eyebrow ? <p className="gg-type-caption mb-[4px]">{eyebrow}</p> : null}
          <h1 className={size === 'section' ? 'gg-type-section-title text-balance' : 'gg-type-page-title text-balance'}>
            {title}
          </h1>
          {description ? <p className="gg-type-body mt-[4px] max-w-[760px]">{description}</p> : null}
        </div>
      </div>
      {actions ? <div className="gg-page-header__actions">{actions}</div> : null}
    </div>
  );
}

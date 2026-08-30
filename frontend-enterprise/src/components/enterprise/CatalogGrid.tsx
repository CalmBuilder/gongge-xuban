import { forwardRef, type HTMLAttributes } from 'react';

import { cn } from '@/lib/utils';

export type CatalogGridFamily = 'resource' | 'metric' | 'info';

export type CatalogGridProps = HTMLAttributes<HTMLDivElement> & {
  family?: CatalogGridFamily;
};

const FAMILY_CLASS: Record<CatalogGridFamily, string> = {
  resource: 'gg-resource-grid',
  metric: 'gg-metric-grid',
  info: 'gg-info-grid',
};

/**
 * 统一卡片网格：资源卡固定 4/3/2/1，指标卡和信息卡分别使用自己的最小行高。
 */
export const CatalogGrid = forwardRef<HTMLDivElement, CatalogGridProps>(function CatalogGrid(
  { family = 'resource', className, children, ...props },
  ref,
) {
  return (
    <div
      {...props}
      ref={ref}
      data-card-family={family}
      className={cn(FAMILY_CLASS[family], className)}
    >
      {children}
    </div>
  );
});

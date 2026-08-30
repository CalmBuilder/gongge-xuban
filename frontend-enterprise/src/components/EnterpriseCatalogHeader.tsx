import type { LucideIcon } from 'lucide-react';
import type { ReactNode } from 'react';

import AppHeader from './AppHeader';
import { PageHeader } from './enterprise/PageHeader';

type EnterpriseCatalogPageHeaderProps = {
  backTo: string;
  backLabel: string;
  title: ReactNode;
  description: ReactNode;
  onLogout?: () => void;
  userName?: string;
};

/**
 * 企业资源管理页的统一页头：返回入口、资源管理标题和当前页说明。
 */
export function EnterpriseCatalogPageHeader({
  backTo,
  backLabel,
  title,
  description,
  onLogout,
  userName,
}: EnterpriseCatalogPageHeaderProps) {
  return (
    <AppHeader
      onLogout={onLogout}
      userName={userName}
      left={(
        <PageHeader
          size="section"
          backTo={backTo}
          backLabel={backLabel}
          title={title}
          description={description}
        />
      )}
    />
  );
}

type EnterpriseCatalogHeroProps = {
  icon: LucideIcon;
  title: ReactNode;
  description: ReactNode;
  actions?: ReactNode;
};

/**
 * 企业资源目录统一说明卡，确保 Skill 与专家模板管理使用同一套视觉层级。
 */
export function EnterpriseCatalogHero({
  icon: HeroIcon,
  title,
  description,
  actions,
}: EnterpriseCatalogHeroProps) {
  return (
    <div className="gg-catalog-hero px-[22px] py-[22px]">
      <div className="flex flex-wrap items-start justify-between gap-[18px]">
        <div className="flex min-w-0 flex-1 items-start gap-[12px]">
          <span className="grid size-[40px] shrink-0 place-items-center rounded-[var(--gg-radius-panel)] bg-[var(--gg-surface)] text-[var(--gg-interaction)] shadow-[0_8px_20px_rgba(49,87,232,0.12)]">
            <HeroIcon className="size-[19px]" aria-hidden="true" />
          </span>
          <div className="min-w-0 flex-1">
            <h2 className="gg-type-section-title">{title}</h2>
            <p className="gg-type-body mt-[5px] max-w-none">{description}</p>
          </div>
        </div>
        {actions ? <div className="flex shrink-0 items-center gap-[8px]">{actions}</div> : null}
      </div>
    </div>
  );
}

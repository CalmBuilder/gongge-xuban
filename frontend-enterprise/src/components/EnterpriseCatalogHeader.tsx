import type { LucideIcon } from 'lucide-react';
import { ArrowLeft } from 'lucide-react';
import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';

import AppHeader from './AppHeader';

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
        <div className="flex flex-wrap items-center gap-[12px]">
          <Link
            to={backTo}
            aria-label={backLabel}
            className="grid size-[32px] place-items-center rounded-[10px] text-[var(--gg-slate)] transition-colors hover:bg-[var(--gg-cloud)] hover:text-[var(--gg-ink)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--gg-cobalt)]"
          >
            <ArrowLeft className="size-[16px]" />
          </Link>
          <div>
            <h1 className="text-[16px] font-semibold text-[var(--gg-ink)]">{title}</h1>
            <p className="mt-[4px] text-[13px] text-[var(--gg-slate)]">{description}</p>
          </div>
        </div>
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
    <div className="border-b border-[#eef1f6] bg-[linear-gradient(110deg,#f2f6ff_0%,#fbfcff_58%,#f4fbf8_100%)] px-[22px] py-[22px]">
      <div className="flex flex-wrap items-start justify-between gap-[18px]">
        <div className="flex items-start gap-[12px]">
          <span className="grid size-[40px] shrink-0 place-items-center rounded-[13px] bg-white text-[var(--gg-cobalt)] shadow-[0_8px_20px_rgba(49,87,232,0.12)]">
            <HeroIcon className="size-[19px]" aria-hidden="true" />
          </span>
          <div>
            <h2 className="text-[17px] font-semibold text-[var(--gg-ink)]">{title}</h2>
            <p className="mt-[5px] max-w-[620px] text-[12px] leading-[19px] text-[var(--gg-slate)]">{description}</p>
          </div>
        </div>
        {actions ? <div className="flex items-center gap-[8px]">{actions}</div> : null}
      </div>
    </div>
  );
}

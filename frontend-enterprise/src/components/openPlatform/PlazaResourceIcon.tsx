import { Blocks, BookOpenText, ListChecks, Wrench, type LucideIcon } from 'lucide-react';

import type { PlazaResourceKind } from '@/assets/plaza/plaza-resource-icons';
import { cn } from '@/lib/utils';

type PlazaResourceIconSize = 'micro' | 'compact' | 'card' | 'drawer';

export type PlazaResourceIconProps = {
  kind: PlazaResourceKind;
  size?: PlazaResourceIconSize;
  className?: string;
};

const CATEGORY_ICONS = {
  knowledge: BookOpenText,
  'general-skills': Blocks,
  skills: ListChecks,
  tools: Wrench,
} satisfies Record<PlazaResourceKind, LucideIcon>;

const CATEGORY_STYLES = {
  knowledge: 'bg-[#eaf7f0] text-[#238b5a]',
  'general-skills': 'bg-[#eef3ff] text-[#356ae6]',
  skills: 'bg-[#eaf9fc] text-[#0891b2]',
  tools: 'bg-[#fff4e5] text-[#d97706]',
} satisfies Record<PlazaResourceKind, string>;

/** Crisp semantic category mark tailored for resource cards and drawers. */
export default function PlazaResourceIcon({
  kind,
  size = 'card',
  className,
}: PlazaResourceIconProps) {
  const CategoryIcon = CATEGORY_ICONS[kind];
  const sizeClass = {
    micro: 'size-5 rounded-md',
    compact: 'size-6 rounded-[7px]',
    card: 'size-7 rounded-lg',
    drawer: 'size-9 rounded-[10px]',
  }[size];
  const glyphClass = {
    micro: 'size-3',
    compact: 'size-[14px]',
    card: 'size-[17px]',
    drawer: 'size-5',
  }[size];

  return (
    <span
      data-plaza-resource-kind={kind}
      className={cn(
        'inline-grid shrink-0 place-items-center',
        CATEGORY_STYLES[kind],
        sizeClass,
        className,
      )}
    >
      <CategoryIcon
        aria-hidden="true"
        strokeWidth={1.8}
        className={glyphClass}
      />
    </span>
  );
}

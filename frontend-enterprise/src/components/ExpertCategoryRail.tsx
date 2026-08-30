import { cn } from '@/lib/utils';

import type { ExpertCountOption } from '../expertGallery';

export type ExpertCategoryRailProps = {
  options: ExpertCountOption[];
  value: string;
  totalCount: number;
  onChange: (value: string) => void;
};

function CategoryButton({
  label,
  count,
  selected,
  compact = false,
  onClick,
}: {
  label: string;
  count: number;
  selected: boolean;
  compact?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      aria-label={`${label}，${count} 位`}
      aria-pressed={selected}
      onClick={onClick}
      className={cn(
        'shrink-0 rounded-[10px] text-left outline-none transition-[background-color,color,box-shadow] focus-visible:ring-2 focus-visible:ring-[var(--gg-cobalt)] focus-visible:ring-offset-2',
        compact
          ? 'inline-flex h-[36px] items-center gap-[6px] border border-[var(--gg-border)] bg-white px-[12px] gg-type-meta'
          : 'flex w-full items-center justify-between px-[10px] py-[8px] gg-type-control',
        selected
          ? 'bg-[var(--gg-cobalt)] font-semibold text-white shadow-[0_6px_16px_rgba(49,87,232,0.18)]'
          : 'text-[#46516a] hover:bg-[#f4f6fc]',
        compact && selected && 'border-[var(--gg-cobalt)]',
      )}
    >
      <span>{label}</span>
      <span
        className={cn(
          'rounded-full px-[6px] py-[1px] gg-type-caption ',
          selected ? 'bg-white/20 text-white/90' : 'bg-[#eff1f7] text-[#757f9c]',
        )}
      >
        {count}
      </span>
    </button>
  );
}

export default function ExpertCategoryRail({
  options,
  value,
  totalCount,
  onChange,
}: ExpertCategoryRailProps) {
  const all = { value: '', label: '全部专家', count: totalCount };
  const rows = [all, ...options];
  return (
    <>
      <aside
        aria-label="专家专业部门"
        className="sticky top-[20px] hidden self-start rounded-[16px] border border-[var(--gg-border)] bg-white p-[12px] shadow-[0_1px_4px_rgba(15,23,42,0.04)] lg:block"
      >
        <p className="px-[10px] pb-[8px] pt-[2px] gg-type-meta font-semibold text-[#30394e]">
          专业部门
        </p>
        <nav className="flex max-h-[calc(100vh-180px)] flex-col gap-[2px] overflow-y-auto" aria-label="专家部门列表">
          {rows.map((option) => (
            <CategoryButton
              key={option.value || 'all'}
              label={option.label}
              count={option.count}
              selected={value === option.value}
              onClick={() => onChange(option.value)}
            />
          ))}
        </nav>
      </aside>

      <nav
        aria-label="专家专业部门快捷选择"
        className="-mx-[2px] flex gap-[8px] overflow-x-auto px-[2px] pb-[8px] lg:hidden"
      >
        {rows.map((option) => (
          <CategoryButton
            key={option.value || 'all'}
            label={option.label}
            count={option.count}
            selected={value === option.value}
            compact
            onClick={() => onChange(option.value)}
          />
        ))}
      </nav>
    </>
  );
}

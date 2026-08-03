import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from '@/components/ui';
import { cn } from '@/lib/utils';

import type { ExpertCountOption } from '../expertGallery';

export type ExpertFilterBarProps = {
  sourceOptions: ExpertCountOption[];
  departmentOptions: ExpertCountOption[];
  directionOptions: ExpertCountOption[];
  source: string;
  department: string;
  direction: string;
  resultCount: number;
  hasFilters: boolean;
  onSourceChange: (value: string) => void;
  onDepartmentChange: (value: string) => void;
  onDirectionChange: (value: string) => void;
  onReset: () => void;
};

// Radix Select 不允许空字符串选项，用哨兵值代表「全部」。
const ALL_VALUE = '__all__';

function FilterSelect({
  label,
  value,
  options,
  allLabel,
  onChange,
}: {
  label: string;
  value: string;
  options: ExpertCountOption[];
  allLabel: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="flex items-center gap-[8px] text-[12px] text-[#727d94]">
      <span className="shrink-0">{label}</span>
      <Select value={value || ALL_VALUE} onValueChange={(next) => onChange(next === ALL_VALUE ? '' : next)}>
        <SelectTrigger
          aria-label={label}
          className="h-[36px] w-auto min-w-[136px] rounded-[10px] border-[var(--gg-border)] bg-white px-[10px] text-[12px] text-[#343a4b] shadow-none"
        >
          <SelectValue placeholder={allLabel} />
        </SelectTrigger>
        <SelectContent position="popper" className="rounded-[12px]">
          <SelectItem value={ALL_VALUE} className="text-[12px]">{allLabel}</SelectItem>
          {options.map((option) => (
            <SelectItem key={option.value} value={option.value} className="text-[12px]">
              {option.label}（{option.count}）
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </label>
  );
}

export default function ExpertFilterBar({
  sourceOptions,
  departmentOptions,
  directionOptions,
  source,
  department,
  direction,
  resultCount,
  hasFilters,
  onSourceChange,
  onDepartmentChange,
  onDirectionChange,
  onReset,
}: ExpertFilterBarProps) {
  return (
    <div className="mb-[16px]">
      <div className="mb-[10px] flex flex-wrap items-center justify-between gap-[8px]">
        <p className="text-[13px] font-semibold text-[#30394e]">
          {department || '全部专家'}
          <span className="ml-[6px] font-normal text-[#8a93a6]">{resultCount} 位</span>
        </p>
        <div className="flex items-center gap-[10px]">
          {sourceOptions.length > 1 ? (
            <FilterSelect
              label="来源"
              value={source}
              options={sourceOptions}
              allLabel="全部来源"
              onChange={onSourceChange}
            />
          ) : null}
          {hasFilters && resultCount > 0 && (
            <button type="button" onClick={onReset} className="text-[12px] font-medium text-[var(--gg-cobalt)] hover:underline">
              清除筛选
            </button>
          )}
          <Sheet>
            <SheetTrigger asChild>
              <button
                type="button"
                aria-label="筛选专家"
                className="hidden h-[34px] items-center rounded-[10px] border border-[var(--gg-border)] bg-white px-[12px] text-[12px] text-[#566178] max-[760px]:inline-flex"
              >
                筛选
              </button>
            </SheetTrigger>
            <SheetContent side="bottom" className="max-h-[82vh] overflow-y-auto rounded-t-[20px] px-[20px] pb-[24px]">
              <SheetHeader>
                <SheetTitle>筛选专家</SheetTitle>
              </SheetHeader>
              <div className="mt-[18px] flex flex-col gap-[18px]">
                {sourceOptions.length > 1 && (
                  <FilterSelect label="来源" value={source} options={sourceOptions} allLabel="全部来源" onChange={onSourceChange} />
                )}
                <FilterSelect label="专业部门" value={department} options={departmentOptions} allLabel="全部部门" onChange={onDepartmentChange} />
                <FilterSelect label="专业方向" value={direction} options={directionOptions} allLabel="全部专业方向" onChange={onDirectionChange} />
                <button
                  type="button"
                  aria-label="重置筛选"
                  onClick={onReset}
                  className="h-[40px] rounded-[12px] border border-[var(--gg-border)] bg-white text-[12px] font-medium text-[#566178]"
                >
                  重置筛选
                </button>
              </div>
            </SheetContent>
          </Sheet>
        </div>
      </div>

      <div className="relative border-b border-[#e3e8f1] pb-[12px]">
        <div className="flex gap-[8px] overflow-x-auto pr-[24px]">
          {[{ value: '', label: '全部方向', count: resultCount }, ...directionOptions].map((option) => (
            <button
              key={option.value || 'all'}
              type="button"
              aria-label={`${option.label}，${option.count} 位`}
              aria-pressed={direction === option.value}
              onClick={() => onDirectionChange(option.value)}
              className={cn(
                'h-[32px] shrink-0 rounded-full border px-[12px] text-[12px] outline-none transition-colors focus-visible:ring-2 focus-visible:ring-[var(--gg-cobalt)] focus-visible:ring-offset-2',
                direction === option.value
                  ? 'border-[var(--gg-cobalt)] bg-[var(--gg-cobalt)] font-semibold text-white'
                  : 'border-[var(--gg-border)] bg-white text-[#657087] hover:border-[#b9c8f0] hover:text-[#343a4b]',
              )}
            >
              {option.label}
            </button>
          ))}
        </div>
        <span className="pointer-events-none absolute bottom-[12px] right-0 h-[28px] w-[28px] bg-gradient-to-l from-white to-transparent" aria-hidden="true" />
      </div>
    </div>
  );
}

import { useEffect } from 'react';
import type { ReactNode } from 'react';
import { ChevronDown } from 'lucide-react';

import {
  Checkbox,
  Dialog,
  DialogContent,
  DialogTitle,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import { SELECT_TRIGGER_CLASS } from '@/lib/enterprise-ui';

export type ImportSourceOption = { value: string; label: string };
export type ImportChoiceItem = { id: string; label: ReactNode };

export type ResourceImportDialogProps = {
  open: boolean;
  loading: boolean;
  /** Header icon (14px). */
  icon: ReactNode;
  title: string;
  /** Optional target select for flows where the destination is not implied by page scope. */
  targetPlaceholder?: string;
  targetLabel?: string;
  targets?: ImportSourceOption[];
  targetId?: string;
  /** Placeholder for the "copy source" select. */
  sourcePlaceholder: string;
  sources: ImportSourceOption[];
  sourceId: string;
  /** Caption above the checkbox list, e.g. "选择 SOP" / "选择技能". */
  itemsLabel: string;
  items: ImportChoiceItem[];
  selectedIds: string[];
  /** Shown when a source is selected but has no importable items. */
  emptyText: string;
  /** Shown before any source is selected. Defaults to "请先选择复制来源". */
  emptySourceText?: string;
  /** Explanatory footer note. */
  note: ReactNode;
  submitText?: string;
  onTargetChange?: (value: string) => void;
  onSourceChange: (value: string) => void;
  onSelectedChange: (ids: string[]) => void;
  onClose: () => void;
  onSubmit: () => void;
};

/**
 * Generic "copy resources from another scope" dialog shared by the SOP and 技能
 * pages: a copy-source select plus a checkbox list of importable resources.
 */
export function ResourceImportDialog({
  open,
  loading,
  icon,
  title,
  targetPlaceholder,
  targetLabel = '复制到',
  targets,
  targetId,
  sourcePlaceholder,
  sources,
  sourceId,
  itemsLabel,
  items,
  selectedIds,
  emptyText,
  emptySourceText = '请先选择复制来源',
  note,
  submitText = '复制',
  onTargetChange,
  onSourceChange,
  onSelectedChange,
  onClose,
  onSubmit,
}: ResourceImportDialogProps) {
  const showTargetSelect = Boolean(targets && onTargetChange);
  const effectiveSourceId = sourceId || (sources.length === 1 ? sources[0].value : '');

  useEffect(() => {
    if (!open || sourceId || sources.length !== 1) return;
    onSourceChange(sources[0].value);
  }, [onSourceChange, open, sourceId, sources]);

  const toggle = (id: string, checked: boolean) => {
    onSelectedChange(checked ? [...selectedIds, id] : selectedIds.filter((value) => value !== id));
  };
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent
        aria-describedby={undefined}
        className="flex max-h-[calc(100dvh-4rem)] w-[calc(100%-2rem)] flex-col gap-[16px] overflow-hidden rounded-[14px] px-[20px] py-[16px] sm:max-w-[640px]"
      >
        <div className="flex items-center gap-[6px] px-[12px] text-[#757f9c]">
          {icon}
          <DialogTitle className="text-[14px] font-normal leading-none text-[#757f9c]">
            {title}
          </DialogTitle>
        </div>

        <div className="flex min-h-0 flex-1 flex-col gap-[14px] overflow-y-auto px-[12px]">
          {showTargetSelect && (
            <div className="flex flex-col gap-[6px]">
              <span className="text-[11px] font-semibold text-[#858b9c]">{targetLabel}</span>
              <Select value={targetId || undefined} onValueChange={onTargetChange}>
                <SelectTrigger className={cn(SELECT_TRIGGER_CLASS, 'w-full')}>
                  <SelectValue placeholder={targetPlaceholder || targetLabel} />
                </SelectTrigger>
                <SelectContent>
                  {(targets || []).map((item) => (
                    <SelectItem key={item.value} value={item.value}>
                      {item.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          )}

          <div className="flex flex-col gap-[6px]">
            <span className="text-[11px] font-semibold text-[#858b9c]">复制来源</span>
            <div className="relative">
              <select
                value={effectiveSourceId}
                onChange={(event) => onSourceChange(event.target.value)}
                className={cn(
                  SELECT_TRIGGER_CLASS,
                  'w-full appearance-none px-3 pr-9 outline-none disabled:cursor-not-allowed disabled:opacity-60'
                )}
              >
                <option value="" disabled>
                  {sourcePlaceholder}
                </option>
                {sources.map((item) => (
                  <option key={item.value} value={item.value}>
                    {item.label}
                  </option>
                ))}
              </select>
              <ChevronDown className="pointer-events-none absolute right-3 top-1/2 size-4 -translate-y-1/2 text-[#858b9c]" />
            </div>
          </div>

          <div className="flex flex-col gap-[6px]">
            <span className="text-[11px] font-semibold text-[#858b9c]">{itemsLabel}</span>
            <div className="max-h-[300px] overflow-y-auto rounded-[10px] border border-[#eef0f4] p-[6px]">
              {items.length === 0 ? (
                <div className="py-[28px] text-center text-[12px] text-[#858b9c]">
                  {sourceId ? emptyText : emptySourceText}
                </div>
              ) : (
                items.map((item) => (
                  <label
                    key={item.id}
                    className="flex cursor-pointer items-center gap-[10px] rounded-[8px] px-[8px] py-[7px] hover:bg-[#f6f6f6]"
                  >
                    <Checkbox
                      checked={selectedIds.includes(item.id)}
                      onCheckedChange={(checked) => toggle(item.id, checked === true)}
                    />
                    <span className="min-w-0 flex-1 truncate text-[12px] text-[#18181a]">
                      {item.label}
                    </span>
                  </label>
                ))
              )}
            </div>
          </div>

          <p className="text-[12px] leading-[1.6] text-[#858b9c]">{note}</p>
        </div>

        <div className="flex items-center justify-end gap-[8px] px-[12px]">
          <Button
            variant="outline"
            disabled={loading}
            onClick={onClose}
            className="h-[32px] w-[80px] rounded-[10px] border-[#e3e7f1] bg-white px-[12px] text-[14px] font-normal text-[#464c5e] hover:border-[#e3e7f1] hover:bg-[#f6f6f6] hover:text-[#18181a]"
          >
            取消
          </Button>
          <Button
            disabled={loading}
            onClick={onSubmit}
            className="h-[36px] w-[80px] rounded-[var(--gg-radius-control)] bg-[var(--gg-cobalt)] px-[12px] text-[14px] font-semibold text-white hover:bg-[#244bc7]"
          >
            {submitText}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

import { useEffect, useMemo, useState } from 'react';

import {
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui';

import type { ExpertTaxonomyRead } from '../types';


export type ExpertClassificationDialogProps = {
  open: boolean;
  expertCount: number;
  taxonomy: ExpertTaxonomyRead | null;
  initialCategory?: string;
  initialSubcategory?: string;
  saving: boolean;
  onClose: () => void;
  onSubmit: (value: { category: string; subcategory: string }) => Promise<void> | void;
};

export default function ExpertClassificationDialog({
  open,
  expertCount,
  taxonomy,
  initialCategory = '',
  initialSubcategory = '',
  saving,
  onClose,
  onSubmit,
}: ExpertClassificationDialogProps) {
  const [category, setCategory] = useState(initialCategory);
  const [subcategory, setSubcategory] = useState(initialSubcategory);

  useEffect(() => {
    if (!open) return;
    setCategory(initialCategory);
    setSubcategory(initialSubcategory);
  }, [initialCategory, initialSubcategory, open]);

  const subcategories = useMemo(
    () => taxonomy?.categories.find((item) => item.name === category)?.subcategories ?? [],
    [category, taxonomy],
  );

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next && !saving) onClose(); }}>
      <DialogContent className="w-[calc(100%-2rem)] max-w-[480px] gap-[18px] rounded-[16px] p-[22px]" aria-describedby="expert-classification-description">
        <div>
          <DialogTitle className="gg-type-card-title font-semibold text-[#18181a]">编辑专家分类</DialogTitle>
          <DialogDescription id="expert-classification-description" className="mt-[6px] gg-type-meta text-[#757f9c]">
            将修改 {expertCount} 位专家
          </DialogDescription>
        </div>
        <div className="grid gap-[14px] sm:grid-cols-2">
          <label className="grid gap-[6px] gg-type-meta text-[#464c5e]">
            一级分类
            <Select value={category} onValueChange={(value) => { setCategory(value); setSubcategory(''); }}>
              <SelectTrigger aria-label="一级分类" className="w-full">
                <SelectValue placeholder="请选择" />
              </SelectTrigger>
              <SelectContent>
                {taxonomy?.categories.map((item) => <SelectItem key={item.name} value={item.name}>{item.name}</SelectItem>)}
              </SelectContent>
            </Select>
          </label>
          <label className="grid gap-[6px] gg-type-meta text-[#464c5e]">
            二级分类
            <Select value={subcategory} onValueChange={setSubcategory} disabled={!category}>
              <SelectTrigger aria-label="二级分类" className="w-full">
                <SelectValue placeholder="请选择" />
              </SelectTrigger>
              <SelectContent>
                {subcategories.map((item) => <SelectItem key={item} value={item}>{item}</SelectItem>)}
              </SelectContent>
            </Select>
          </label>
        </div>
        <div className="flex justify-end gap-[8px]">
          <Button variant="outline" disabled={saving} onClick={onClose}>取消</Button>
          <Button
            disabled={saving || !category || !subcategory}
            onClick={() => void onSubmit({ category, subcategory })}
            className="bg-[var(--gg-cobalt)] text-white hover:bg-[#244bc7]"
          >
            {saving ? '保存中' : '保存分类'}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

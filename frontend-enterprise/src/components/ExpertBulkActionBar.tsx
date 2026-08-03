export default function ExpertBulkActionBar({
  count,
  onEdit,
  onClear,
}: {
  count: number;
  onEdit: () => void;
  onClear: () => void;
}) {
  if (!count) return null;
  return (
    <div
      role="region"
      aria-label="专家批量操作"
      className="fixed bottom-[24px] left-1/2 z-40 flex -translate-x-1/2 items-center gap-[14px] rounded-[16px] border border-[#cdd9f8] bg-white/95 px-[18px] py-[12px] shadow-[0_18px_48px_rgba(37,65,150,0.20)] backdrop-blur max-[560px]:bottom-[12px] max-[560px]:w-[calc(100%-24px)] max-[560px]:justify-between"
    >
      <strong className="whitespace-nowrap text-[12px] text-[#30394e]">已选择 {count} 位专家</strong>
      <button type="button" onClick={onClear} className="text-[11px] font-medium text-[#657087] hover:text-[#30394e]">取消选择</button>
      <button type="button" onClick={onEdit} className="h-[34px] whitespace-nowrap rounded-[10px] bg-[var(--gg-cobalt)] px-[14px] text-[11px] font-semibold text-white hover:bg-[#244bc7]">修改分类</button>
    </div>
  );
}

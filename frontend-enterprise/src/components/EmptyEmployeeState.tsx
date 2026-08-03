import { Button as UiButton } from '@/components/ui';
import IconAccount from '../assets/icons/sys-accounts.svg?react';
import IconAdd from '../assets/icons/add.svg?react';
import IconGlobe from '../assets/icons/globe.svg?react';

export default function EmptyEmployeeState({
  isAdmin,
  onCreate,
  onBrowsePlatform,
}: {
  isAdmin: boolean;
  onCreate: () => void;
  onBrowsePlatform: () => void;
}) {
  return (
    <div className="min-h-full w-full min-w-0 max-w-full box-border px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]">
      <div className="mx-auto flex min-h-[calc(100vh-220px)] max-w-[560px] flex-col items-center justify-center text-center">
        <div className="relative flex size-[96px] items-center justify-center rounded-[28px] border border-[var(--gg-border)] bg-[linear-gradient(145deg,var(--gg-paper),#edf3ff)] shadow-[var(--gg-shadow-card)]">
          <IconAccount className="size-[40px] text-[var(--gg-cobalt)]" />
          <span className="absolute bottom-[-8px] right-[-8px] flex size-[34px] items-center justify-center rounded-full bg-[var(--gg-cobalt)] text-white shadow-[0_8px_20px_rgba(49,87,232,0.28)]">
            <IconAdd className="size-[18px]" />
          </span>
        </div>

        <h2 className="mt-[24px] text-[22px] font-semibold leading-tight text-[var(--gg-ink)]">
          还没有数字员工
        </h2>
        <p className="mt-[10px] text-[14px] leading-[22px] text-[var(--gg-slate)]">
          {isAdmin
            ? '创建你的第一位数字员工，为它配置知识库、技能与工具，即可开始接管对话与任务。'
            : '当前还没有可管理的数字员工，创建一位或从开放广场复制已发布的配置作为起点。'}
        </p>

        <div className="mt-[28px] flex flex-wrap items-center justify-center gap-[12px]">
          <UiButton
            onClick={onCreate}
            className="inline-flex h-[42px] items-center gap-[8px] rounded-[var(--gg-radius-control)] bg-[var(--gg-cobalt)] px-[22px] text-[14px] font-semibold text-white shadow-[0_8px_20px_rgba(49,87,232,0.22)] hover:bg-[#244bc7]"
          >
            <IconAdd className="size-[16px]" />
            新建数字员工
          </UiButton>
          <UiButton
            variant="outline"
            onClick={onBrowsePlatform}
            className="inline-flex h-[42px] items-center gap-[8px] rounded-[var(--gg-radius-control)] border border-[var(--gg-border)] bg-[var(--gg-paper)] px-[22px] text-[14px] font-medium text-[var(--gg-slate)] hover:border-[#bfcbea] hover:bg-[var(--gg-cloud)] hover:text-[var(--gg-cobalt)]"
          >
            <IconGlobe className="size-[16px]" />
            浏览开放广场
          </UiButton>
        </div>
      </div>
    </div>
  );
}

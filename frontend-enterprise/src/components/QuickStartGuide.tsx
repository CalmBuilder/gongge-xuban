import { useEffect, useLayoutEffect, useMemo, useState } from 'react';
import { XIcon } from 'lucide-react';
import { useLocation, useNavigate } from 'react-router-dom';

import { Button } from '@/components/ui/button';
import {
  Popover,
  PopoverAnchor,
  PopoverArrow,
  PopoverContent,
  PopoverDescription,
  PopoverTitle,
} from '@/components/ui/popover';
import { EnterpriseRoute } from '@/enums/routes';
import { useI18n } from '@/i18n';
import { BRAND_STORAGE_KEYS } from '@/lib/brand-storage';
import { PRODUCT_EVENTS } from '@/lib/product-events';

type GuideSide = 'top' | 'right' | 'bottom' | 'left';

type QuickStartStep = {
  title: string;
  description: string;
  route: EnterpriseRoute;
  target: string;
  side?: GuideSide;
  nextLabel?: string;
  nextRoute?: EnterpriseRoute;
  actionEvent?: string;
};

const QUICK_START_STEPS: QuickStartStep[] = [
  {
    title: '先接入数字员工的大脑',
    description: '新建模型配置，填写兼容接口、模型名称和 API Key，并完成连通性测试。',
    route: EnterpriseRoute.Models,
    target: 'models-create',
    actionEvent: PRODUCT_EVENTS.openModelCreate,
  },
  {
    title: '创建你的数字员工',
    description: '从空白岗位开始，或从开放广场复制一位专家，再为它配置职责和能力。',
    route: EnterpriseRoute.Agents,
    target: `route-${EnterpriseRoute.Agents}`,
    side: 'right',
  },
  {
    title: '从开放广场复用能力',
    description: '浏览可共享的数字员工、知识库、技能、SOP 和工具，快速获得可靠起点。',
    route: EnterpriseRoute.Platform,
    target: `route-${EnterpriseRoute.Platform}`,
    side: 'right',
  },
  {
    title: '查看员工档案',
    description: '在同一处查看岗位资料、能力配置和工作状态，持续维护数字员工。',
    route: EnterpriseRoute.Dashboard,
    target: `route-${EnterpriseRoute.Dashboard}`,
    side: 'right',
  },
  {
    title: '安排定时任务',
    description: '把日报、巡检等周期工作交给数字员工，到点自动创建任务并执行。',
    route: EnterpriseRoute.ScheduledTasks,
    target: `route-${EnterpriseRoute.ScheduledTasks}`,
    side: 'right',
  },
  {
    title: '管理员工记忆',
    description: '查看数字员工沉淀的关键信息，让后续协作延续业务上下文。',
    route: EnterpriseRoute.Memories,
    target: `route-${EnterpriseRoute.Memories}`,
    side: 'right',
  },
  {
    title: '沉淀业务知识',
    description: '上传文档并整理为可追溯的知识，让回答有依据、业务口径更一致。',
    route: EnterpriseRoute.Knowledge,
    target: `route-${EnterpriseRoute.Knowledge}`,
    side: 'right',
  },
  {
    title: '扩展通用技能',
    description: '导入或编写可复用技能，让数字员工掌握新的分析和处理方法。',
    route: EnterpriseRoute.GeneralSkills,
    target: `route-${EnterpriseRoute.GeneralSkills}`,
    side: 'right',
  },
  {
    title: '编排标准作业流程',
    description: '把稳定流程整理成 SOP，按步骤执行、保留记录，并支持中断后恢复。',
    route: EnterpriseRoute.Skills,
    target: `route-${EnterpriseRoute.Skills}`,
    side: 'right',
  },
  {
    title: '开始与数字员工协作',
    description: '准备完成。进入对话端，选择数字员工并交付第一项工作。',
    route: EnterpriseRoute.Skills,
    target: 'open-chat',
    side: 'right',
    nextLabel: '开始协作',
    nextRoute: EnterpriseRoute.Gallery,
  },
];

function findVisibleGuideTarget(target: string): HTMLElement | undefined {
  return Array.from(
    document.querySelectorAll<HTMLElement>(`[data-guide-target="${target}"]`),
  ).find((element) => {
    const rect = element.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  });
}

export default function QuickStartGuide({ isAdmin }: { isAdmin: boolean }) {
  const { t } = useI18n();
  const navigate = useNavigate();
  const location = useLocation();
  const steps = useMemo(
    () => (isAdmin ? QUICK_START_STEPS : QUICK_START_STEPS.slice(1)),
    [isAdmin],
  );
  const [open, setOpen] = useState(false);
  const [stepIndex, setStepIndex] = useState(0);
  const [anchorReady, setAnchorReady] = useState(false);
  const [anchorRect, setAnchorRect] = useState({ top: 0, left: 0, width: 1, height: 1 });
  const current = steps[stepIndex];
  const isLast = stepIndex === steps.length - 1;

  useEffect(() => {
    const onboardingSeen = window.localStorage.getItem(BRAND_STORAGE_KEYS.onboardingSeen);
    const quickStartSeen = window.localStorage.getItem(BRAND_STORAGE_KEYS.quickStartSeen);
    if (onboardingSeen && !quickStartSeen) setOpen(true);
  }, []);

  useEffect(() => {
    const openQuickStart = () => {
      if (window.localStorage.getItem(BRAND_STORAGE_KEYS.quickStartSeen)) return;
      setStepIndex(0);
      setOpen(true);
    };
    window.addEventListener(PRODUCT_EVENTS.openQuickStart, openQuickStart);
    return () => window.removeEventListener(PRODUCT_EVENTS.openQuickStart, openQuickStart);
  }, []);

  useEffect(() => {
    if (open && location.pathname !== current.route) navigate(current.route);
  }, [current.route, location.pathname, navigate, open]);

  useLayoutEffect(() => {
    if (!open) return undefined;
    let frame = 0;
    const updateAnchor = () => {
      window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(() => {
        const target = findVisibleGuideTarget(current.target);
        if (!target) return;
        const rect = target.getBoundingClientRect();
        setAnchorRect({ top: rect.top, left: rect.left, width: rect.width, height: rect.height });
        setAnchorReady(true);
      });
    };
    setAnchorReady(false);
    updateAnchor();
    const delayedUpdate = window.setTimeout(updateAnchor, 120);
    const observer = new MutationObserver(updateAnchor);
    observer.observe(document.body, { childList: true, subtree: true });
    window.addEventListener('resize', updateAnchor);
    window.addEventListener('scroll', updateAnchor, { capture: true, passive: true });
    return () => {
      window.clearTimeout(delayedUpdate);
      window.cancelAnimationFrame(frame);
      observer.disconnect();
      window.removeEventListener('resize', updateAnchor);
      window.removeEventListener('scroll', updateAnchor, true);
    };
  }, [current.target, location.pathname, open]);

  function finish() {
    window.localStorage.setItem(BRAND_STORAGE_KEYS.quickStartSeen, '1');
    setAnchorReady(false);
    setOpen(false);
    window.dispatchEvent(new Event(PRODUCT_EVENTS.quickStartCompleted));
  }

  function goNext() {
    if (!isLast) {
      setStepIndex((value) => value + 1);
      return;
    }
    if (current.nextRoute) navigate(current.nextRoute);
    finish();
  }

  function goPrevious() {
    setStepIndex((value) => Math.max(0, value - 1));
  }

  function runFirstAction() {
    navigate(current.route);
    const actionEvent = current.actionEvent;
    if (actionEvent) {
      window.setTimeout(() => window.dispatchEvent(new Event(actionEvent)), 0);
      return;
    }
    window.setTimeout(() => findVisibleGuideTarget(current.target)?.click(), 80);
  }

  return (
    <Popover open={open} onOpenChange={(nextOpen) => !nextOpen && finish()} modal>
      <PopoverAnchor asChild>
        <span
          aria-hidden="true"
          className="pointer-events-none fixed z-40 rounded-[var(--gg-radius-control)] ring-3 ring-[var(--gg-cobalt)] ring-offset-3 ring-offset-white/90 transition-[top,left,width,height] duration-150 motion-reduce:transition-none"
          style={{
            ...anchorRect,
            boxShadow: open && anchorReady
              ? '0 0 0 9999px rgba(24, 33, 61, 0.22)'
              : 'none',
            opacity: open && anchorReady ? 1 : 0,
          }}
        />
      </PopoverAnchor>
      <PopoverContent
        aria-label={t('快速入门')}
        side={current.side ?? 'bottom'}
        align="center"
        sideOffset={18}
        collisionPadding={14}
        onInteractOutside={(event) => event.preventDefault()}
        className={`z-50 w-[420px] max-w-[calc(100vw-24px)] gap-[18px] rounded-[18px] border border-[var(--gg-border)] bg-[var(--gg-paper)] p-[22px] text-[var(--gg-ink)] shadow-[0_24px_70px_rgba(24,33,61,0.22)] ring-0 ${anchorReady ? 'visible' : 'invisible pointer-events-none'}`}
      >
        <PopoverArrow width={20} height={10} className="fill-[var(--gg-paper)]" />
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          aria-label={t('关闭快速入门')}
          onClick={finish}
          className="absolute right-[14px] top-[14px] text-[var(--gg-slate)] hover:bg-[var(--gg-cloud)] hover:text-[var(--gg-cobalt)]"
        >
          <XIcon className="size-[16px]" />
        </Button>

        <div className="pr-[34px]">
          <span className="text-[10px] font-semibold tracking-[0.16em] text-[var(--gg-cobalt)]">
            {t('员工成长路径')}
          </span>
          <PopoverTitle className="mt-[7px] text-[17px] font-semibold leading-[24px] text-[var(--gg-ink)]">
            {t(current.title)}
          </PopoverTitle>
          <PopoverDescription className="mt-[6px] text-[13px] leading-[21px] text-[var(--gg-slate)]">
            {t(current.description)}
          </PopoverDescription>
        </div>

        <div aria-hidden="true" className="flex items-center gap-[4px]">
          {steps.map((item, index) => (
            <span
              key={item.target}
              className={`h-[3px] flex-1 rounded-full transition-colors ${index <= stepIndex ? 'bg-[var(--gg-cobalt)]' : 'bg-[var(--gg-border)]'}`}
            />
          ))}
        </div>

        <div className="flex items-center justify-between gap-[12px]">
          <span className="shrink-0 text-[11px] font-medium tabular-nums text-[var(--gg-slate)]">
            {stepIndex + 1} / {steps.length}
          </span>
          <div className="flex min-w-0 items-center gap-[8px]">
            <Button
              type="button"
              variant="outline"
              onClick={stepIndex === 0 ? runFirstAction : goPrevious}
              className="h-[34px] rounded-[var(--gg-radius-control)] border-[var(--gg-border)] bg-[var(--gg-paper)] px-[14px] text-[12px] font-medium text-[var(--gg-slate)] hover:border-[#bfcbea] hover:bg-[var(--gg-cloud)] hover:text-[var(--gg-cobalt)]"
            >
              {t(stepIndex === 0 ? (isAdmin ? '配置模型' : '查看入口') : '上一步')}
            </Button>
            <Button
              type="button"
              onClick={goNext}
              className="h-[34px] rounded-[var(--gg-radius-control)] bg-[var(--gg-cobalt)] px-[16px] text-[12px] font-semibold text-white shadow-[0_8px_18px_rgba(49,87,232,0.22)] hover:bg-[#244bc7]"
            >
              {t(current.nextLabel ?? (isLast ? '完成' : '下一步'))}
            </Button>
          </div>
        </div>
      </PopoverContent>
    </Popover>
  );
}

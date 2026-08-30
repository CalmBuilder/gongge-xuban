import { useEffect, useState } from 'react';
import {
  BookOpenText,
  Bot,
  ChevronLeft,
  ChevronRight,
  CircleCheck,
  History,
  IdCard,
  ListChecks,
  MessageSquareText,
  ShieldCheck,
  Workflow,
  Wrench,
  XIcon,
  type LucideIcon,
} from 'lucide-react';

import { Dialog, DialogContent, DialogTitle } from '@/components/ui/dialog';
import { BRAND_STORAGE_KEYS } from '@/lib/brand-storage';
import { PRODUCT_EVENTS } from '@/lib/product-events';

const ONBOARDING_SEEN_KEY = BRAND_STORAGE_KEYS.onboardingSeen;

type GuideCard = {
  icon: LucideIcon;
  title: string;
  description: string;
};

type GuideStep = {
  eyebrow: string;
  title: string;
  description: string;
  cards: GuideCard[];
  visual: 'capabilities' | 'lifecycle';
};

const STEPS: GuideStep[] = [
  {
    eyebrow: '欢迎来到 共格·序伴',
    title: '让企业经验进入工作',
    description:
      '把岗位知识、标准流程、工具与记忆汇聚成可信赖的数字员工，与团队一起完成真实业务。',
    visual: 'capabilities',
    cards: [
      {
        icon: BookOpenText,
        title: '知识有依据',
        description: '连接企业知识与岗位上下文，回答可追溯。',
      },
      {
        icon: Workflow,
        title: '流程能执行',
        description: '用 SOP 编排任务，在权限边界内调用工具。',
      },
      {
        icon: History,
        title: '结果可复盘',
        description: '保留过程、记忆与 Trace，让经验持续沉淀。',
      },
    ],
  },
  {
    eyebrow: '从配置到持续运营',
    title: '一条闭环，带数字员工真正上岗',
    description:
      '不是只配置一个聊天机器人，而是围绕岗位目标建立可执行、可监督、可持续优化的工作闭环。',
    visual: 'lifecycle',
    cards: [
      {
        icon: IdCard,
        title: '定义岗位',
        description: '明确职责、服务对象和执行边界。',
      },
      {
        icon: ListChecks,
        title: '连接能力',
        description: '配置模型、知识库、技能、SOP 与工具。',
      },
      {
        icon: MessageSquareText,
        title: '交付与沉淀',
        description: '在对话中协作，用记忆和 Trace 持续改进。',
      },
    ],
  },
];

const CAPABILITY_NODES = [
  { label: '知识库', detail: '业务依据', icon: BookOpenText, position: 'left-[28px] top-[118px]' },
  { label: 'SOP', detail: '标准流程', icon: Workflow, position: 'right-[28px] top-[118px]' },
  { label: '技能', detail: '专业方法', icon: ListChecks, position: 'left-[28px] bottom-[96px]' },
  { label: '工具', detail: '受控执行', icon: Wrench, position: 'right-[28px] bottom-[96px]' },
] as const;

const LIFECYCLE_STAGES = [
  { label: '岗位', detail: '目标与边界', icon: IdCard },
  { label: '能力', detail: '知识 · SOP · 工具', icon: ListChecks },
  { label: '执行', detail: '对话与任务', icon: Bot },
  { label: '沉淀', detail: '记忆与 Trace', icon: History },
] as const;

function BrandMark() {
  return (
    <span
      aria-hidden="true"
      className="grid size-[30px] grid-cols-2 gap-[3px] rounded-[9px] bg-[var(--gg-cobalt)] p-[6px] shadow-[0_8px_22px_rgba(49,87,232,0.32)]"
    >
      <i className="rounded-[2px] bg-white" />
      <i className="rounded-[2px] bg-white/75" />
      <i className="rounded-[2px] bg-white/75" />
      <i className="rounded-[2px] bg-white" />
    </span>
  );
}

function CapabilityVisual() {
  return (
    <div className="relative flex h-full flex-col overflow-hidden bg-[#101a38] px-[30px] py-[28px] text-white">
      <div
        aria-hidden="true"
        className="absolute inset-0 opacity-25 [background-image:linear-gradient(rgba(123,231,245,.13)_1px,transparent_1px),linear-gradient(90deg,rgba(123,231,245,.13)_1px,transparent_1px)] [background-size:32px_32px]"
      />
      <div
        aria-hidden="true"
        className="absolute left-1/2 top-[47%] size-[300px] -translate-x-1/2 -translate-y-1/2 rounded-full bg-[radial-gradient(circle,rgba(49,87,232,.42),transparent_66%)]"
      />

      <div className="relative z-10 flex items-center gap-[10px]">
        <BrandMark />
        <div>
          <p className="gg-type-body font-semibold tracking-[0.02em]">共格·序伴</p>
          <p className="mt-[2px] gg-type-caption tracking-[0.16em] text-blue-100/55">企业数字员工平台</p>
        </div>
      </div>

      <div className="relative z-10 mt-[42px] min-h-0 flex-1">
        <div
          aria-hidden="true"
          className="absolute left-[78px] right-[78px] top-[166px] h-px bg-[linear-gradient(90deg,transparent,#7be7f5,transparent)] opacity-70"
        />
        <div
          aria-hidden="true"
          className="absolute bottom-[126px] left-[78px] right-[78px] h-px bg-[linear-gradient(90deg,transparent,#7be7f5,transparent)] opacity-50"
        />
        <div
          aria-hidden="true"
          className="absolute bottom-[126px] left-1/2 top-[166px] w-px bg-[linear-gradient(180deg,#7be7f5,transparent)] opacity-60"
        />

        {CAPABILITY_NODES.map(({ label, detail, icon: Icon, position }) => (
          <div
            key={label}
            className={`absolute ${position} z-10 w-[126px] rounded-[14px] border border-white/12 bg-white/8 p-[12px] shadow-[0_16px_34px_rgba(0,0,0,.18)] backdrop-blur-sm`}
          >
            <div className="flex items-center gap-[9px]">
              <span className="grid size-[30px] place-items-center rounded-[9px] bg-white/10 text-[#7be7f5]">
                <Icon className="size-[15px]" />
              </span>
              <div>
                <p className="gg-type-meta font-semibold">{label}</p>
                <p className="mt-[2px] gg-type-caption text-blue-100/55">{detail}</p>
              </div>
            </div>
          </div>
        ))}

        <div className="absolute left-1/2 top-[166px] z-20 w-[170px] -translate-x-1/2 -translate-y-1/2 rounded-[18px] border border-[#7be7f5]/35 bg-[#182858] p-[16px] shadow-[0_22px_56px_rgba(0,5,25,.45)]">
          <div className="flex items-start justify-between">
            <span className="grid size-[38px] place-items-center rounded-[12px] bg-[var(--gg-cobalt)] text-white">
              <Bot className="size-[20px]" />
            </span>
            <span className="flex items-center gap-[4px] rounded-full bg-[#7be7f5]/12 px-[7px] py-[4px] gg-type-caption font-semibold text-[#7be7f5]">
              <i className="size-[5px] rounded-full bg-[#7be7f5]" />
              在岗
            </span>
          </div>
          <p className="mt-[14px] gg-type-body font-semibold">你的数字员工</p>
          <p className="mt-[5px] gg-type-caption  text-blue-100/60">理解岗位，遵循流程，协同执行</p>
        </div>
      </div>

      <div className="relative z-10 flex items-center gap-[8px] border-t border-white/10 pt-[18px] gg-type-caption text-blue-100/55">
        <ShieldCheck className="size-[13px] text-[#7be7f5]" />
        权限可控 · 过程可查 · 结果可追溯
      </div>
    </div>
  );
}

function LifecycleVisual() {
  return (
    <div className="relative flex h-full flex-col overflow-hidden bg-[#101a38] px-[30px] py-[28px] text-white">
      <div
        aria-hidden="true"
        className="absolute inset-0 opacity-20 [background-image:linear-gradient(rgba(123,231,245,.13)_1px,transparent_1px),linear-gradient(90deg,rgba(123,231,245,.13)_1px,transparent_1px)] [background-size:32px_32px]"
      />
      <div className="relative z-10 flex items-center gap-[10px]">
        <BrandMark />
        <div>
          <p className="gg-type-body font-semibold tracking-[0.02em]">共格·序伴</p>
          <p className="mt-[2px] gg-type-caption tracking-[0.16em] text-blue-100/55">工作闭环</p>
        </div>
      </div>

      <div className="relative z-10 mt-[54px]">
        <p className="gg-type-caption font-semibold tracking-[0.2em] text-[#7be7f5]">DIGITAL EMPLOYEE LOOP</p>
        <h3 className="mt-[10px] max-w-[300px] gg-type-page-title font-semibold tracking-[-0.035em]">
          从企业经验出发，
          <br />
          回到可复用的经验。
        </h3>
      </div>

      <div className="relative z-10 mt-[32px] flex flex-col">
        {LIFECYCLE_STAGES.map(({ label, detail, icon: Icon }, index) => (
          <div key={label} className="group relative flex min-h-[66px] items-start gap-[14px]">
            {index < LIFECYCLE_STAGES.length - 1 ? (
              <span className="absolute left-[17px] top-[36px] h-[30px] w-px bg-[linear-gradient(#7be7f5,rgba(123,231,245,.14))]" />
            ) : null}
            <span className="relative z-10 grid size-[36px] shrink-0 place-items-center rounded-[11px] border border-[#7be7f5]/25 bg-[#7be7f5]/10 text-[#7be7f5]">
              <Icon className="size-[17px]" />
            </span>
            <div className="flex min-w-0 flex-1 items-center justify-between border-b border-white/8 pb-[15px]">
              <div>
                <p className="gg-type-control font-semibold">{label}</p>
                <p className="mt-[3px] gg-type-caption text-blue-100/55">{detail}</p>
              </div>
              <span className="font-mono gg-type-caption text-blue-100/35">0{index + 1}</span>
            </div>
          </div>
        ))}
      </div>

      <div className="relative z-10 mt-auto flex items-center gap-[8px] rounded-[12px] border border-white/10 bg-white/6 px-[12px] py-[10px] gg-type-caption text-blue-100/65">
        <CircleCheck className="size-[14px] text-[#7be7f5]" />
        每一次执行，都为下一次协作积累上下文
      </div>
    </div>
  );
}

function StepVisual({ visual }: { visual: GuideStep['visual'] }) {
  return visual === 'capabilities' ? <CapabilityVisual /> : <LifecycleVisual />;
}

export default function OnboardingGuide() {
  const [open, setOpen] = useState(false);
  const [step, setStep] = useState(0);

  useEffect(() => {
    if (!window.localStorage.getItem(ONBOARDING_SEEN_KEY)) {
      setStep(0);
      setOpen(true);
    }
  }, []);

  useEffect(() => {
    const reopen = () => {
      setStep(0);
      setOpen(true);
    };
    window.addEventListener(PRODUCT_EVENTS.openOnboarding, reopen);
    return () => window.removeEventListener(PRODUCT_EVENTS.openOnboarding, reopen);
  }, []);

  function finish() {
    window.localStorage.setItem(ONBOARDING_SEEN_KEY, '1');
    setOpen(false);
    window.dispatchEvent(new Event(PRODUCT_EVENTS.onboardingCompleted));
    window.dispatchEvent(new Event(PRODUCT_EVENTS.openQuickStart));
  }

  function goPrev() {
    setStep((previous) => Math.max(0, previous - 1));
  }

  function goNext() {
    if (step >= STEPS.length - 1) {
      finish();
      return;
    }
    setStep((previous) => Math.min(STEPS.length - 1, previous + 1));
  }

  function handleOpenChange(nextOpen: boolean) {
    if (nextOpen) {
      setOpen(true);
      return;
    }
    finish();
  }

  const current = STEPS[step];
  const isFirst = step === 0;
  const isLast = step === STEPS.length - 1;

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        showCloseButton={false}
        className="grid h-[min(600px,calc(100vh-32px))] w-[960px] max-w-[calc(100vw-32px)] grid-cols-1 gap-0 overflow-hidden rounded-[22px] border border-white/70 bg-[var(--gg-paper)] p-0 shadow-[0_32px_90px_rgba(16,26,56,.28)] ring-0 sm:max-w-[960px] md:grid-cols-[440px_520px]"
      >
        <DialogTitle className="sr-only">{current.title}</DialogTitle>

        <div className="hidden min-h-0 md:block">
          <StepVisual visual={current.visual} />
        </div>

        <section className="relative flex min-h-0 flex-col bg-[var(--gg-paper)] px-[clamp(24px,5vw,52px)] pb-[30px] pt-[28px]">
          <button
            type="button"
            onClick={finish}
            aria-label="关闭引导"
            className="absolute right-[22px] top-[20px] grid size-[32px] place-items-center rounded-[10px] text-[var(--gg-slate)] hover:bg-[var(--gg-cloud)] hover:text-[var(--gg-cobalt)]"
          >
            <XIcon className="size-[16px]" />
          </button>

          <div className="mt-[28px]">
            <span className="inline-flex items-center gap-[8px] gg-type-caption font-semibold tracking-[0.14em] text-[var(--gg-cobalt)]">
              <i className="h-px w-[22px] bg-[var(--gg-cobalt)]" />
              {current.eyebrow}
            </span>
            <h2 className="mt-[14px] max-w-[410px] gg-type-page-title font-semibold tracking-[-0.045em] text-[var(--gg-ink)]">
              {current.title}
              <span className="text-[var(--gg-cobalt)]">。</span>
            </h2>
            <p className="mt-[14px] max-w-[415px] gg-type-control  text-[var(--gg-slate)]">
              {current.description}
            </p>
          </div>

          <div className="mt-[28px] flex flex-col gap-[8px]">
            {current.cards.map(({ icon: Icon, title, description }) => (
              <div
                key={title}
                className="group flex min-h-[64px] items-center gap-[13px] rounded-[14px] border border-transparent bg-[var(--gg-cloud)] px-[14px] py-[10px] transition-[border-color,background-color,transform] hover:translate-x-[2px] hover:border-[var(--gg-border)] hover:bg-white motion-reduce:transform-none"
              >
                <span className="grid size-[36px] shrink-0 place-items-center rounded-[11px] bg-white text-[var(--gg-cobalt)] shadow-[0_6px_18px_rgba(49,87,232,.10)]">
                  <Icon className="size-[17px]" />
                </span>
                <div className="min-w-0">
                  <p className="gg-type-control font-semibold text-[var(--gg-ink)]">{title}</p>
                  <p className="mt-[3px] gg-type-caption  text-[var(--gg-slate)]">
                    {description}
                  </p>
                </div>
              </div>
            ))}
          </div>

          <div className="mt-auto flex items-center justify-between gap-[16px] pt-[24px]">
            <div className="flex items-center gap-[9px]" aria-label={`第 ${step + 1} 页，共 ${STEPS.length} 页`}>
              {STEPS.map((item, index) => (
                <span
                  key={item.title}
                  className={`h-[3px] rounded-full transition-[width,background-color] ${
                    index === step ? 'w-[28px] bg-[var(--gg-cobalt)]' : 'w-[8px] bg-[var(--gg-border)]'
                  }`}
                />
              ))}
              <span className="ml-[2px] gg-type-caption tabular-nums text-[var(--gg-slate)]">
                0{step + 1} / 0{STEPS.length}
              </span>
            </div>

            <div className="flex items-center gap-[8px]">
              {!isFirst ? (
                <button
                  type="button"
                  onClick={goPrev}
                  className="grid size-[38px] place-items-center rounded-[var(--gg-radius-control)] border border-[var(--gg-border)] text-[var(--gg-slate)] hover:border-[#bfcbea] hover:bg-[var(--gg-cloud)] hover:text-[var(--gg-cobalt)]"
                  aria-label="上一步"
                >
                  <ChevronLeft className="size-[16px]" />
                </button>
              ) : (
                <button
                  type="button"
                  onClick={finish}
                  className="h-[38px] rounded-[var(--gg-radius-control)] px-[12px] gg-type-meta font-medium text-[var(--gg-slate)] hover:bg-[var(--gg-cloud)] hover:text-[var(--gg-ink)]"
                >
                  稍后了解
                </button>
              )}
              <button
                type="button"
                onClick={goNext}
                className="flex h-[38px] min-w-[118px] items-center justify-center gap-[8px] rounded-[var(--gg-radius-control)] bg-[var(--gg-cobalt)] px-[18px] gg-type-meta font-semibold text-white shadow-[0_10px_24px_rgba(49,87,232,.24)] hover:-translate-y-px hover:bg-[#244bc7] motion-reduce:transform-none"
              >
                {isLast ? '开始使用' : '下一步'}
                {isLast ? <CircleCheck className="size-[15px]" /> : <ChevronRight className="size-[15px]" />}
              </button>
            </div>
          </div>
        </section>
      </DialogContent>
    </Dialog>
  );
}

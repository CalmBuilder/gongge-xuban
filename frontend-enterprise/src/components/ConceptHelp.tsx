import { CircleHelp } from 'lucide-react';
import type { ReactNode } from 'react';

import {
  Popover,
  PopoverContent,
  PopoverDescription,
  PopoverHeader,
  PopoverTitle,
  PopoverTrigger,
} from '@/components/ui/popover';
import { cn } from '@/lib/utils';

export type ConceptHelpTopic =
  | 'enterprise-member'
  | 'digital-employee'
  | 'expert'
  | 'plaza'
  | 'forms'
  | 'governance';

type ConceptDefinition = {
  label: string;
  title: string;
  summary: string;
  facts: Array<{ term: string; description: string }>;
  details?: Array<{ title: string; description: string }>;
  example?: { title: string; steps: string[] };
  flow?: string[];
  technicalFacts?: string[];
  boundary: string;
};

const CONCEPTS: Record<ConceptHelpTopic, ConceptDefinition> = {
  'enterprise-member': {
    label: '企业成员',
    title: '企业成员是真人，不是数字员工',
    summary: '真人使用账号登录，并通过员工档案、组织归属和岗位任职参与企业工作。',
    facts: [
      { term: '真实用户', description: '负责登录、发起操作和承担审计责任。' },
      { term: '员工档案', description: '记录这个真人在企业中的工号、在职、停用和离职状态。' },
      { term: '组织与岗位', description: '决定其组织身份、职责及流程候选资格。' },
    ],
    details: [
      {
        title: '真实用户解决“谁登录”',
        description: '账号负责认证、发起对话、认领任务和留下审计记录。',
      },
      {
        title: '组织员工解决“以什么身份工作”',
        description: '员工档案、组织归属和岗位任职共同表达真人在企业中的工作身份。',
      },
    ],
    boundary: '真人不会因为使用数字员工平台，就自动成为一个数字员工。',
  },
  'digital-employee': {
    label: '数字员工',
    title: '数字员工是 AI 工作主体',
    summary: '数字员工可以对话、执行 SOP、调用获批工具和检索知识，但不使用真人账号登录。',
    facts: [
      { term: '工作身份', description: '以稳定数字员工档案承载职责、提示词和能力。' },
      { term: '工作入口', description: '由真人对话、流程节点、定时任务或 API 触发。' },
      { term: '安全边界', description: '只能在调用人权限、数字员工授权和监督规则交集内工作。' },
    ],
    details: [
      {
        title: '它如何工作',
        description: '接收目标后组合模型、知识、技能、SOP 和工具完成工作，并把过程和结果留在审计链路中。',
      },
      {
        title: '它如何承担责任',
        description: '数字员工保存稳定身份和执行记录；最终业务责任仍由发起人、审批人或明确监督者承担。',
      },
    ],
    boundary: '数字员工不是企业成员，也不能冒用所有者或监督者的真人身份。',
  },
  expert: {
    label: '专家',
    title: '专家是数字员工的能力分身形态',
    summary: '专家是面向某个用户或专业场景定制的数字员工能力分身，用来沉淀和复用专业知识、工作方法、提示词、SOP 与工具组合。',
    facts: [
      { term: '个人起点', description: '可以从广场复制后继续私有定制。' },
      { term: '能力分身', description: '复用专业能力，不复制真人身份和私人凭据。' },
      { term: '组织演进', description: '经审核、明确责任组织和授权后，可成为组织数字员工。' },
    ],
    example: {
      title: '从个人专家到组织能力的例子',
      steps: [
        '王超从广场添加“人事政策专家”。',
        '补充本单位制度、常用处理方法和经过批准的资源。',
        '形成默认私有的“王超的人事政策专家”。',
        '提交组织审核，明确责任组织、监督者和业务授权。',
        '审核通过后升级为组织共享的“人事数字员工”。',
      ],
    },
    technicalFacts: [
      '专家与其他数字员工都使用 AgentProfile 作为运行身份。',
      '能力分身不复制真人账号、私人凭据或来源发布者的业务授权。',
    ],
    boundary: '“专家”不是第三种登录主体，也不建立另一套运行引擎。',
  },
  plaza: {
    label: '数字员工广场',
    title: '广场是已发布数字员工的发现入口',
    summary: '数字员工广场是经过发布、可被用户发现和使用的数字员工目录。',
    facts: [
      { term: '直接使用', description: '建立当前用户的使用关系，不改变数字员工所有权。' },
      { term: '创建我的版本', description: '复制允许复用的配置，形成用户所有的能力分身。' },
      { term: '安全复制', description: '不会继承发布者的私人记忆、凭据或业务授权。' },
    ],
    details: [
      {
        title: '广场卡片代表什么',
        description: '它是已发布的数字员工或可复用模板，不是真人，也不代表浏览者已经拥有它。',
      },
      {
        title: '点击“添加”或直接使用',
        description: '系统建立 AgentUsage，表示当前用户添加过或使用过；不自动取得所有权、发布权、编辑权或管理权。',
      },
      {
        title: '点击“创建我的版本”',
        description: '系统创建新的 AgentProfile，owner_user_id 指向当前用户，source_agent_id 指向广场来源，并默认保持私有。',
      },
      {
        title: '允许复制的边界',
        description: '只复制允许复用的配置和资源，不复制发布者的私人记忆、凭据或业务授权。',
      },
    ],
    technicalFacts: [
      'AgentUsage = 添加或使用关系，不是所有权。',
      '新 AgentProfile = 用户所有的专家（能力分身）。',
    ],
    boundary: '添加或使用过，只表示使用关系；不等于拥有、编辑或发布这个数字员工。',
  },
  forms: {
    label: '个人专家与组织数字员工',
    title: '个人专家与组织数字员工是同一运行主体的不同治理形态',
    summary: '两者都由 AgentProfile 运行，差别主要在所有权、可见范围、责任组织、监督者和业务授权。',
    facts: [
      { term: '个人专家', description: '由用户所有、默认私有，服务于个人或专业场景定制。' },
      { term: '组织数字员工', description: '由组织治理、明确责任与授权，可稳定服务组织成员和流程。' },
      { term: '发布目录', description: '审核通过后才能进入组织内数字员工广场，供其他用户发现和使用。' },
    ],
    flow: [
      '广场数字员工模板',
      '用户的专家（能力分身）',
      '组织审核：明确责任、可见范围和授权',
      '组织数字员工',
      '发布到组织内数字员工广场',
    ],
    technicalFacts: [
      '运行主键始终是 AgentProfile.id，不建立第二套专家运行引擎。',
      '形态升级不自动继承发布者凭据，也不绕过工具、知识或 SOP 权限检查。',
    ],
    boundary: '“个人”与“组织”描述治理和发布形态，不改变数字员工的统一执行身份。',
  },
  governance: {
    label: '治理关系',
    title: '所有者、责任组织和监督者各管一件事',
    summary: '三种关系彼此独立，平台不会根据组织名称或负责人身份自动推导。',
    facts: [
      { term: '所有者', description: '管理数字员工资产和可编辑配置。' },
      { term: '责任组织', description: '回答哪个组织负责治理，不自动产生访问权。' },
      { term: '真人监督者', description: '监督具体业务执行，不自动取得资产所有权。' },
    ],
    details: [
      {
        title: '发布者与使用者',
        description: '发布者负责把版本开放到广场；使用者只建立使用关系，两者都不必等于所有者。',
      },
      {
        title: '组织负责人',
        description: '组织负责人不会自动成为数字员工监督者；需要平台显式建立监督关系。',
      },
    ],
    boundary: '发布者、使用者和所有者也可能是不同的人，必须分别记录。',
  },
};

export function ConceptHelp({
  topic,
  className,
  triggerLabel,
}: {
  topic: ConceptHelpTopic;
  className?: string;
  triggerLabel?: string;
}) {
  const concept = CONCEPTS[topic];

  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label={`了解${concept.label}`}
          className={cn(
            triggerLabel
              ? 'inline-flex min-h-[28px] shrink-0 items-center gap-[5px] rounded-full border border-[#dbe3f4] bg-white px-[9px] gg-type-caption font-medium text-[#657394] outline-none transition-colors hover:border-[#bdcaec] hover:text-[var(--gg-cobalt)] focus-visible:ring-2 focus-visible:ring-[var(--gg-cobalt)] focus-visible:ring-offset-2'
              : 'inline-grid size-[20px] shrink-0 place-items-center rounded-full text-[#8190b1] outline-none transition-colors hover:bg-[#edf2ff] hover:text-[var(--gg-cobalt)] focus-visible:ring-2 focus-visible:ring-[var(--gg-cobalt)] focus-visible:ring-offset-2',
            className,
          )}
        >
          <CircleHelp className="size-[15px]" />
          {triggerLabel && <span>{triggerLabel}</span>}
        </button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        sideOffset={8}
        collisionPadding={12}
        className="max-h-[min(78vh,760px)] w-[min(620px,calc(100vw-24px))] overflow-y-auto overscroll-contain rounded-[18px] border-[#dfe5f2] bg-white p-[20px] shadow-[0_18px_50px_rgba(35,55,100,0.18)]"
      >
        <PopoverHeader className="gap-[5px] text-left">
          <span className="font-mono gg-type-caption font-semibold uppercase tracking-[0.16em] text-[#6074a9]">
            概念说明
          </span>
          <PopoverTitle className="gg-type-section-title font-semibold  text-[#202637]">
            {concept.title}
          </PopoverTitle>
          <PopoverDescription className="max-w-[560px] gg-type-control  text-[#6f788d]">
            {concept.summary}
          </PopoverDescription>
        </PopoverHeader>
        <dl className="mt-[16px] grid gap-[8px] sm:grid-cols-2">
          {concept.facts.map((fact) => (
            <div
              key={fact.term}
              className="rounded-[11px] bg-[#f7f9fd] px-[12px] py-[10px]"
            >
              <dt className="gg-type-caption font-semibold text-[#43547c]">{fact.term}</dt>
              <dd className="mt-[3px] gg-type-caption  text-[#6f788d]">{fact.description}</dd>
            </div>
          ))}
        </dl>
        {concept.details && (
          <div className="mt-[14px] grid gap-[8px] sm:grid-cols-2">
            {concept.details.map((detail) => (
              <section key={detail.title} className="rounded-[11px] border border-[#e4e9f3] px-[12px] py-[10px]">
                <h3 className="gg-type-card-title font-semibold text-[#3d4b6b]">{detail.title}</h3>
                <p className="mt-[3px] gg-type-caption  text-[#6f788d]">{detail.description}</p>
              </section>
            ))}
          </div>
        )}
        {concept.example && (
          <section className="mt-[14px] rounded-[12px] border border-[#dce5f7] bg-[#fbfcff] p-[13px]">
            <h3 className="gg-type-card-title font-semibold text-[#3d4b6b]">{concept.example.title}</h3>
            <ol className="mt-[8px] grid gap-[6px]">
              {concept.example.steps.map((step, index) => (
                <li key={step} className="flex gap-[8px] gg-type-caption  text-[#657087]">
                  <span className="grid size-[18px] shrink-0 place-items-center rounded-full bg-[#e9efff] font-mono gg-type-caption font-semibold text-[#4e68b4]">
                    {index + 1}
                  </span>
                  <span>{step}</span>
                </li>
              ))}
            </ol>
          </section>
        )}
        {concept.flow && (
          <section className="mt-[14px] rounded-[12px] border border-[#dce5f7] bg-[#fbfcff] p-[13px]">
            <h3 className="gg-type-card-title font-semibold text-[#3d4b6b]">演进路径</h3>
            <ol className="mt-[9px] grid gap-[5px]">
              {concept.flow.map((step, index) => (
                <li key={step} className="relative flex items-center gap-[9px] pb-[5px] last:pb-0">
                  <span className="grid size-[20px] shrink-0 place-items-center rounded-full border border-[#b9c8ee] bg-white font-mono gg-type-caption font-semibold text-[#4e68b4]">
                    {index + 1}
                  </span>
                  <span className="rounded-[8px] bg-[#eef3ff] px-[9px] py-[5px] gg-type-caption font-medium text-[#506084]">
                    {step}
                  </span>
                </li>
              ))}
            </ol>
          </section>
        )}
        {concept.technicalFacts && (
          <ul className="mt-[12px] grid gap-[5px] rounded-[11px] bg-[#f4f6fa] px-[12px] py-[10px]">
            {concept.technicalFacts.map((fact) => (
              <li key={fact} className="font-mono gg-type-caption  text-[#64708a]">
                · {fact}
              </li>
            ))}
          </ul>
        )}
        <p className="mt-[12px] border-l-2 border-[#9cb0ee] pl-[10px] gg-type-caption  text-[#58647f]">
          {concept.boundary}
        </p>
      </PopoverContent>
    </Popover>
  );
}

export function ConceptNote({
  topic,
  children,
  className,
}: {
  topic: ConceptHelpTopic;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        'flex items-center gap-[7px] rounded-[11px] border border-[#e1e7f3] bg-[#f8faff] px-[11px] py-[8px] gg-type-caption  text-[#64708a]',
        className,
      )}
    >
      <span className="min-w-0">{children}</span>
      <ConceptHelp topic={topic} />
    </div>
  );
}

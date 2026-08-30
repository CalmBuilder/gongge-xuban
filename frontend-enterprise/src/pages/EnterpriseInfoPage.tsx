import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import {
  ArrowRight,
  Bot,
  BriefcaseBusiness,
  Building2,
  CheckCircle2,
  ClipboardCheck,
  LockKeyhole,
  Network,
  Plus,
  RotateCcw,
  ShieldCheck,
  Sparkles,
  UserRound,
} from 'lucide-react';
import { useNavigate } from 'react-router-dom';

import AppHeader from '@/components/AppHeader';
import { PageShell } from '@/components/enterprise/PageShell';
import {
  Dialog,
  DialogContent,
  DialogTitle,
  Input,
} from '@/components/ui';
import { Button } from '@/components/ui/button';
import { notify } from '@/components/ui/app-toast';
import { useI18n } from '@/i18n';

import { api } from '../api/client';
import {
  hasGovernancePermission,
  isEnterpriseAdmin,
  type EnterpriseAuthUser,
} from '../auth';
import { useEnterpriseContext } from '../enterprise-context';
import type { OrganizationUnit, Position } from '../types/organization';

const EMPTY_COMPANY = { code: '', name: '' };
const ORGANIZATION_CODE_PATTERN = /^[A-Za-z][A-Za-z0-9_-]{1,63}$/;

type TopologyAgent = {
  id: string;
  name: string;
  status: string;
  is_overall: boolean;
  responsible_org_unit_id?: string;
};

type TopologyAgentRoleBinding = {
  agent_id: string;
  agent_name: string;
  role_name: string;
  assignment_mode: string;
  supervisor_employee_name?: string;
  scope_type: string;
  scope_id: string;
  status: string;
};

type TopologyEmployeeRoleAssignment = {
  employee_profile_id: string;
  employee_name?: string;
  role_name: string;
  scope_type: string;
  scope_id: string;
  status: string;
};

type TopologyPositionAssignment = {
  employee_profile_id: string;
  position_id: string;
  status: string;
};

type TopologyPositionRoleBinding = {
  position_id: string;
  business_role_name: string;
  status: string;
};

type TopologyAgentSkill = {
  skill_id: string;
  name: string;
  version: string;
  status: string;
  branch_status?: string;
};

type TopologyHumanStage = {
  employeeProfileId: string;
  roleName: string;
  employeeName?: string;
};

type TopologyExample = {
  scenarioName: string;
  organizationName: string;
  positionName?: string;
  positionAssignedToHuman: boolean;
  positionAssigneeCount: number;
  agentName: string;
  roleName: string;
  supervisorName?: string;
  humanStages: TopologyHumanStage[];
  humanAction: string;
  outcome: string;
  assignmentMode: string;
  scopeLabel: string;
  sopName: string;
  sopVersion: string;
  verifiedRelations: ReadonlySet<string>;
};

const SOP_EXAMPLE_ROLES = [
  {
    scenarioName: '费用报销',
    skillId: 'expense_travel_reimbursement',
    roleName: '财务报销专员',
    humanRoleNames: ['财务报销专员'],
    humanAction: '异常票据或超标事项由真人核对',
    outcome: '通过后继续报销，结论写入流程记录',
  },
  {
    scenarioName: '用章申请',
    skillId: 'seal_application_approval',
    roleName: '用章申请操作员',
    humanRoleNames: ['用章审批人', '重要用章审批人'],
    humanAction: '真人按普通或重要用章分级审批',
    outcome: '批准后由数字员工完成用章流程',
  },
  {
    scenarioName: '请假与假勤',
    skillId: 'leave_apply_v1',
    roleName: 'HR 假勤专员',
    humanRoleNames: ['HR 假勤专员'],
    humanAction: '余额、特殊假种或材料异常由真人接管',
    outcome: '核对后恢复流程并保存处理轨迹',
  },
  {
    scenarioName: 'IT 权限开通',
    skillId: 'skill_perm_grant_routing_001',
    roleName: 'IT 权限开通操作员',
    humanRoleNames: ['IT 高权限审批人'],
    humanAction: '高权限申请必须由真人审批',
    outcome: '批准后数字员工执行授权并记录结果',
  },
  {
    scenarioName: '合同风险初筛',
    skillId: 'contract_risk_review',
    roleName: '法务合同风险分析员',
    humanRoleNames: ['法务合同复核专员'],
    humanAction: '风险结论交由真人法务复核',
    outcome: '复核意见返回流程并形成审计记录',
  },
] as const;

function HorizontalRelation({
  label,
  reverse = false,
  conditional = false,
}: {
  label: string;
  reverse?: boolean;
  conditional?: boolean;
}) {
  return (
    <div className="flex min-w-0 flex-col items-center justify-center gap-[4px] text-center">
      <span className="gg-type-caption font-medium text-[var(--gg-text-muted)]">{label}</span>
      <span className="flex w-full items-center text-[#8ba1da]">
        <i className={`h-px min-w-0 flex-1 ${conditional ? 'border-t border-dashed border-[#aebddd]' : 'bg-[#c9d4ed]'}`} aria-hidden="true" />
        <ArrowRight className={`size-[15px] shrink-0 ${reverse ? 'rotate-180' : ''}`} aria-hidden="true" />
      </span>
    </div>
  );
}

function VerticalRelation({ label, conditional = false }: { label: string; conditional?: boolean }) {
  return (
    <div className="flex min-h-[58px] flex-col items-center justify-center gap-[4px] text-center">
      <span className="gg-type-caption font-medium text-[var(--gg-text-muted)]">{label}</span>
      <span className="flex flex-col items-center text-[#8ba1da]">
        <i className={`h-[20px] w-px ${conditional ? 'border-l border-dashed border-[#aebddd]' : 'bg-[#c9d4ed]'}`} aria-hidden="true" />
        <ArrowRight className="size-[15px] rotate-90" aria-hidden="true" />
      </span>
    </div>
  );
}

type TopologyKind = 'organization' | 'position' | 'role' | 'human' | 'agent' | 'expert' | 'sop';

const TOPOLOGY_NODE_STYLE: Record<TopologyKind, string> = {
  organization: 'border-[#dbe4fb] bg-[#f5f8ff] text-[#3157e8]',
  position: 'border-[#e1e5ef] bg-[#f8f9fc] text-[#5f6c88]',
  role: 'border-[#f2e3c9] bg-[#fff9ee] text-[#b16d10]',
  human: 'border-[#dbe4fb] bg-[#f5f8ff] text-[#3157e8]',
  agent: 'border-[#d6eee6] bg-[#f1fbf7] text-[#138b66]',
  expert: 'border-[#eadffb] bg-[#faf6ff] text-[#8656c7]',
  sop: 'border-[#dedff5] bg-[#f7f7ff] text-[#6558bd]',
};

const TOPOLOGY_NODE_LABEL: Record<TopologyKind, string> = {
  organization: '组织单元',
  position: '组织岗位',
  role: '组织角色',
  human: '真人',
  agent: '数字员工',
  expert: '专家形态',
  sop: 'SOP / 技能',
};

const TOPOLOGY_RELATION_CONTRACTS = [
  'OrganizationUnit.contains.Position',
  'PositionAssignment.assigns.Human',
  'PositionRoleBinding.grants.BusinessRole',
  'EmployeeRoleAssignment.delegates.BusinessRole',
  'AgentRoleBinding.grants.BusinessRole',
  'AgentResourceBinding.loads.SOP',
  'Human.supervises.Agent',
  'Human.owns.ExpertClone',
] as const;

type TopologyRelationContract = (typeof TOPOLOGY_RELATION_CONTRACTS)[number];

const TOPOLOGY_RELATION_LABELS: Record<TopologyRelationContract, string> = {
  'OrganizationUnit.contains.Position': '组织单元包含岗位',
  'PositionAssignment.assigns.Human': '真人任职于岗位',
  'PositionRoleBinding.grants.BusinessRole': '岗位授予组织角色',
  'EmployeeRoleAssignment.delegates.BusinessRole': '真人获授组织角色',
  'AgentRoleBinding.grants.BusinessRole': '数字员工获授组织角色',
  'AgentResourceBinding.loads.SOP': '数字员工装载 SOP',
  'Human.supervises.Agent': '真人监督数字员工',
  'Human.owns.ExpertClone': '真人拥有能力分身',
};

type ResponsibilityTopologyData = {
  organization: { title: string; subtitle: string };
  position: { title: string; subtitle: string };
  human: { title: string; subtitle: string };
  expert: { title: string; subtitle: string };
  role: { title: string; subtitle: string };
  agent: { title: string; subtitle: string };
  sop: { title: string; subtitle: string };
};

function TopologyNode({
  kind,
  title,
  subtitle,
  icon,
}: {
  kind: TopologyKind;
  title: string;
  subtitle: string;
  icon: ReactNode;
}) {
  return (
    <article
      data-topology-node={kind}
      className={`h-full min-w-0 rounded-[15px] border p-[13px] ${TOPOLOGY_NODE_STYLE[kind]}`}
    >
      <div className="flex items-center gap-[9px]">
        <span className="grid size-[34px] shrink-0 place-items-center rounded-[11px] bg-white shadow-[0_5px_16px_rgba(43,62,110,0.08)]">
          {icon}
        </span>
        <div className="min-w-0">
          <p className="gg-type-meta opacity-75">{TOPOLOGY_NODE_LABEL[kind]}</p>
          <h3 className="gg-type-card-title mt-[2px] line-clamp-2">{title}</h3>
        </div>
      </div>
      <p className="gg-type-meta mt-[9px] line-clamp-2">{subtitle}</p>
    </article>
  );
}

function ResponsibilityTopology({
  data,
  projection,
  verifiedRelations,
}: {
  data: ResponsibilityTopologyData;
  projection: 'model' | 'live';
  verifiedRelations?: ReadonlySet<string>;
}) {
  const { t } = useI18n();
  const relationVerified = (contract: string) => projection === 'model'
    || verifiedRelations?.has(contract) === true;
  return (
    <div
      className="rounded-[14px] bg-white p-[11px] shadow-[0_4px_14px_rgba(41,55,92,0.04)]"
      data-topology-contract={projection}
    >
      <div className="grid gap-[8px] lg:grid-cols-[minmax(0,1fr)_66px_minmax(0,1fr)_66px_minmax(0,1fr)_66px_minmax(0,1fr)]">
        <TopologyNode kind="organization" title={data.organization.title} subtitle={data.organization.subtitle} icon={<Building2 className="size-[18px]" />} />
        <HorizontalRelation label="包含岗位" conditional={!relationVerified('OrganizationUnit.contains.Position')} />
        <TopologyNode kind="position" title={data.position.title} subtitle={data.position.subtitle} icon={<BriefcaseBusiness className="size-[18px]" />} />
        <HorizontalRelation label="真人任职" conditional={!relationVerified('PositionAssignment.assigns.Human')} />
        <TopologyNode kind="human" title={data.human.title} subtitle={data.human.subtitle} icon={<UserRound className="size-[18px]" />} />
        <HorizontalRelation label="创建 / 拥有" conditional={!relationVerified('Human.owns.ExpertClone')} />
        <TopologyNode kind="expert" title={data.expert.title} subtitle={data.expert.subtitle} icon={<Sparkles className="size-[17px]" />} />

        <div className="lg:col-start-3"><VerticalRelation label="岗位默认角色" conditional={!relationVerified('PositionRoleBinding.grants.BusinessRole')} /></div>
        <div className="lg:col-start-5"><VerticalRelation label="监督数字员工" conditional={!relationVerified('Human.supervises.Agent')} /></div>

        <div className="lg:col-start-3">
          <TopologyNode kind="role" title={data.role.title} subtitle={data.role.subtitle} icon={<ShieldCheck className="size-[18px]" />} />
        </div>
        <div className="lg:col-start-4"><HorizontalRelation label="数字员工绑定" reverse conditional={!relationVerified('AgentRoleBinding.grants.BusinessRole')} /></div>
        <div className="lg:col-start-5">
          <TopologyNode kind="agent" title={data.agent.title} subtitle={data.agent.subtitle} icon={<Bot className="size-[19px]" />} />
        </div>
        <div className="lg:col-start-6"><HorizontalRelation label="装载并运行" conditional={!relationVerified('AgentResourceBinding.loads.SOP')} /></div>
        <div className="lg:col-start-7">
          <TopologyNode kind="sop" title={data.sop.title} subtitle={data.sop.subtitle} icon={<ClipboardCheck className="size-[18px]" />} />
        </div>
      </div>
      <div className="mt-[9px] grid gap-[7px] md:grid-cols-2 xl:grid-cols-4">
        {TOPOLOGY_RELATION_CONTRACTS.map((contract) => (
          <span
            key={contract}
            className={`gg-type-control flex min-w-0 items-center break-words rounded-[var(--gg-radius-control)] border px-[10px] py-[7px] ${relationVerified(contract) ? 'border-[var(--gg-capability-line)] bg-[var(--gg-capability-soft)] text-[var(--gg-capability)]' : 'border-[var(--gg-line)] bg-[var(--gg-surface-subtle)] text-[var(--gg-text-secondary)]'}`}
            data-topology-relation={contract}
            data-relation-status={relationVerified(contract) ? 'verified' : 'missing'}
          >
            {t(TOPOLOGY_RELATION_LABELS[contract])} · {t(relationVerified(contract) ? (projection === 'model' ? '已支持' : '已验证') : '待配置')}
          </span>
        ))}
      </div>
    </div>
  );
}

function WorkflowStep({
  index,
  eyebrow,
  title,
  description,
  tone,
  icon,
}: {
  index: number;
  eyebrow: string;
  title: string;
  description: string;
  tone: 'human' | 'agent' | 'review' | 'result';
  icon: ReactNode;
}) {
  const toneClasses = {
    human: 'border-[#dce5fb] bg-[#f6f8ff] text-[#3157e8]',
    agent: 'border-[#d4ece4] bg-[#f1faf7] text-[#138b66]',
    review: 'border-[#f0e0c6] bg-[#fff9ee] text-[#ad6a0c]',
    result: 'border-[#e3ddf2] bg-[#faf7ff] text-[#8057b2]',
  };
  return (
    <article
      data-workflow-step={index}
      className={`relative min-h-[152px] rounded-[15px] border px-[14px] pb-[14px] pt-[13px] ${toneClasses[tone]}`}
    >
      <div className="flex items-start justify-between gap-[8px]">
        <span className="grid size-[32px] shrink-0 place-items-center rounded-[10px] bg-white shadow-[0_5px_14px_rgba(43,62,110,0.08)]">
          {icon}
        </span>
        <span className="gg-type-code font-semibold opacity-55">0{index}</span>
      </div>
      <p className="gg-type-caption mt-[10px] font-semibold uppercase tracking-[0.08em] opacity-70">{eyebrow}</p>
      <h4 className="gg-type-card-title mt-[3px] line-clamp-2">{title}</h4>
      <p className="gg-type-meta mt-[5px] line-clamp-3">{description}</p>
    </article>
  );
}

function EnterpriseCollaborationMap({
  enterpriseName,
  examples,
}: {
  enterpriseName: string;
  examples: TopologyExample[];
}) {
  const [selectedScenario, setSelectedScenario] = useState('');
  const selectedExample = examples.find((item) => item.scenarioName === selectedScenario)
    || examples[0]
    || null;
  return (
    <section
      aria-label="真人、组织与岗位、组织角色、数字员工和专家拓扑图"
      className="min-w-0 overflow-hidden rounded-[22px] border border-[#dfe6f5] bg-white shadow-[0_18px_48px_rgba(30,48,91,0.07)]"
    >
      <div className="border-b border-[#e8edf7] bg-[linear-gradient(135deg,#f3f7ff_0%,#fbfdff_58%,#f6fbfa_100%)] px-[24px] py-[20px]">
        <div className="flex items-start justify-between gap-[18px]">
          <div>
            <p className="font-mono gg-type-meta font-semibold uppercase tracking-[0.14em] text-[#6074a9]">COLLABORATION MAP</p>
            <h2 className="gg-type-section-title mt-[6px]">组织、真人与数字员工如何关联</h2>
            <p className="gg-type-body mt-[5px]">上半部分解释代码中的关系模型，下半部分只展示当前系统确实存在的数据。</p>
          </div>
          <span className="grid size-[42px] shrink-0 place-items-center rounded-[14px] bg-white text-[#3157e8] shadow-[0_8px_20px_rgba(49,87,232,0.12)]">
            <Network className="size-[21px]" />
          </span>
        </div>
      </div>

      <div className="p-[22px]">
        <div className="flex items-center justify-between gap-[12px]">
          <div>
            <p className="font-mono gg-type-caption font-semibold uppercase tracking-[0.13em] text-[#6074a9]">SYSTEM RELATION MODEL</p>
            <h3 className="gg-type-card-title mt-[3px]">系统关系模型</h3>
          </div>
          <span className="gg-type-caption rounded-full border border-[#dce4f3] bg-[#f8faff] px-[10px] py-[5px]">结构说明，不代表当前已绑定</span>
        </div>

        <div className="mt-[11px] rounded-[18px] border border-[#e7ebf3] bg-[#fafbfe] p-[12px]" data-topology-model>
          <ResponsibilityTopology
            projection="model"
            data={{
              organization: { title: enterpriseName, subtitle: '企业、部门、中心和项目组统一作为组织单元' },
              position: { title: '岗位', subtitle: '现实组织中的工作位置，定义职责与编制' },
              human: { title: '真人员工', subtitle: '通过 PositionAssignment 任职，也可获得限时代理角色' },
              expert: { title: '专家 / 能力分身', subtitle: '具备个人 owner、source 与授权边界时成立' },
              role: { title: '组织角色', subtitle: '岗位、真人和数字员工共同使用的职责与权限枢纽' },
              agent: { title: '数字员工', subtitle: '通过 AgentRoleBinding 承担角色，由真人监督' },
              sop: { title: 'SOP / 技能', subtitle: '通过 AgentResourceBinding 装载，定义人机协作节点' },
            }}
          />
          <div className="gg-type-caption mt-[9px] flex flex-wrap items-center gap-x-[18px] gap-y-[6px] rounded-[11px] border border-[#e5e9f2] bg-white px-[11px] py-[8px]">
            <span className="flex items-center gap-[7px]"><i className="h-px w-[28px] bg-[#9fb0d5]" />实线：核心关系</span>
            <span className="flex items-center gap-[7px]"><i className="w-[28px] border-t border-dashed border-[#9fb0d5]" />虚线：能力分身等满足条件后成立</span>
            <span>上图与下图共用 8 条关系契约；下图只替换节点内容，不改变关系含义。</span>
          </div>
        </div>

        <div className="mt-[15px] flex items-end justify-between gap-[12px] border-t border-[#e8ecf4] pt-[14px]">
          <div>
            <p className="font-mono gg-type-caption font-semibold uppercase tracking-[0.13em] text-[#34846d]">LIVE DATA EXAMPLE</p>
            <h3 className="gg-type-card-title mt-[3px]">当前系统真实示例</h3>
          </div>
          <span className="gg-type-caption rounded-full border border-[#d7ece5] bg-[#f4fbf8] px-[10px] py-[5px] text-[#34846d]">来自当前数据库</span>
        </div>

        {selectedExample ? (
          <>
          <div className="mt-[11px] flex gap-[7px] overflow-x-auto pb-[2px]" role="tablist" aria-label="SOP 演示场景">
            {examples.map((item) => {
              const selected = item.scenarioName === selectedExample.scenarioName;
              return (
                <button
                  key={item.scenarioName}
                  type="button"
                  role="tab"
                  aria-selected={selected}
                  onClick={() => setSelectedScenario(item.scenarioName)}
                  className={`gg-type-control shrink-0 rounded-full border px-[13px] py-[7px] transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#8da8f4] ${selected ? 'border-[#3157e8] bg-[#3157e8] text-white' : 'border-[#dfe5ef] bg-white text-[#667087] hover:border-[#b9c7e8] hover:text-[#3157e8]'}`}
                >
                  {item.scenarioName}
                </button>
              );
            })}
          </div>
          <div className="mt-[11px] rounded-[18px] border border-[#dce9e5] bg-[#f7fbf9] p-[14px]" data-topology-example="live">
            <div className="gg-type-caption mb-[9px] flex items-center gap-[7px] font-medium text-[#34846d]">
              <span className="rounded-full bg-[#e8f7f1] px-[7px] py-[3px]">{selectedExample.scenarioName}</span>
              <span>SOP 演示种子中的有效绑定</span>
            </div>
            <ResponsibilityTopology
              projection="live"
              verifiedRelations={selectedExample.verifiedRelations}
              data={{
                organization: { title: selectedExample.organizationName, subtitle: '数字员工的责任组织与当前业务作用域' },
                position: { title: selectedExample.positionName || '岗位待配置', subtitle: selectedExample.positionName ? '已通过 PositionRoleBinding 核验的岗位' : '当前数据库没有匹配岗位，不伪造关系' },
                human: {
                  title: selectedExample.positionAssigneeCount > 0
                    ? `${selectedExample.positionAssigneeCount} 位真人在岗`
                    : '真人任职待配置',
                  subtitle: selectedExample.positionAssigneeCount > 0
                    ? '通过有效 PositionAssignment 承担该岗位'
                    : '当前没有可展示的有效岗位任职',
                },
                expert: { title: '能力分身待配置', subtitle: '当前数据库没有 owner/source 实例，保持条件关系' },
                role: { title: selectedExample.roleName, subtitle: `执行角色 · ${selectedExample.assignmentMode} · ${selectedExample.scopeLabel}` },
                agent: { title: selectedExample.agentName, subtitle: selectedExample.supervisorName ? `治理监督人：${selectedExample.supervisorName}` : '尚未配置治理监督人' },
                sop: { title: selectedExample.sopName, subtitle: `${selectedExample.sopVersion} · 已通过数字员工技能分支核验` },
              }}
            />

            <div className="mt-[10px] flex items-center justify-between gap-[12px]">
              <div>
                <p className="font-mono gg-type-caption font-semibold uppercase tracking-[0.12em] text-[#6074a9]">SOP COLLABORATION LOOP</p>
                <h4 className="gg-type-card-title mt-[2px]">一次业务如何在人与数字员工之间闭环</h4>
              </div>
              <span className="gg-type-caption rounded-full border border-[#e0e5ee] bg-white px-[9px] py-[5px]">运行时协作</span>
            </div>

            <div className="mt-[8px] grid gap-[7px] md:grid-cols-4" data-sop-loop>
              <WorkflowStep index={1} eyebrow="真人发起" title="业务人员提交事项" description="发起人来自每次 SOP 运行上下文，不预设具体姓名。" tone="human" icon={<UserRound className="size-[17px]" />} />
              <WorkflowStep index={2} eyebrow="数字员工执行" title={`${selectedExample.agentName} · ${selectedExample.roleName}`} description="按角色权限执行确定性步骤，遇到人工门禁或异常时暂停。" tone="agent" icon={<Bot className="size-[18px]" />} />
              <WorkflowStep
                index={3}
                eyebrow="真人把关"
                title={selectedExample.humanStages.length > 0
                  ? [...new Set(selectedExample.humanStages.map((stage) => stage.roleName))].join(' / ')
                  : '尚未配置真人角色任职'}
                description={selectedExample.humanStages.length > 0
                  ? `${selectedExample.humanAction}；当前任职：${selectedExample.humanStages.map((stage) => `${stage.roleName}：${stage.employeeName || '未命名员工'}`).join('、')}`
                  : `${selectedExample.humanAction}；当前数据库未找到有效员工角色任职。`}
                tone="review"
                icon={<ClipboardCheck className="size-[17px]" />}
              />
              <WorkflowStep index={4} eyebrow="流程返回" title="SOP 继续并形成结果" description={selectedExample.outcome} tone="result" icon={<CheckCircle2 className="size-[17px]" />} />
            </div>
            <div className="gg-type-caption mt-[7px] flex items-center justify-center gap-[8px] rounded-[10px] border border-dashed border-[#cfd9ec] bg-white/70 px-[11px] py-[8px] font-medium text-[#6074a9]" data-loop-return>
              <RotateCcw className="size-[13px]" />
              处理结果与审计轨迹返回发起人，下一次业务仍从真人请求开始
            </div>

            <div className="mt-[9px] grid gap-[8px] md:grid-cols-2">
              <div className="gg-type-meta flex items-center gap-[9px] rounded-[12px] border border-[#e2e8f1] bg-white px-[12px] py-[10px]">
                <UserRound className="size-[15px] shrink-0 text-[#6074a9]" />
                {selectedExample.supervisorName ? `治理监督人：“${selectedExample.supervisorName}”` : '该数字员工未配置治理监督人'}
              </div>
              <div className="gg-type-meta flex items-center gap-[9px] rounded-[12px] border border-[#e2e8f1] bg-white px-[12px] py-[10px]">
                <BriefcaseBusiness className="size-[15px] shrink-0 text-[#6074a9]" />
                {selectedExample.positionName
                  ? `责任组织包含岗位：“${selectedExample.positionName}”；${selectedExample.positionAssignedToHuman ? '真人任职已验证' : '真人任职待配置'}`
                  : '责任组织没有与执行角色匹配的岗位；未强行连线'}
              </div>
            </div>
            <p className="gg-type-meta mt-[9px]">监督人负责数字员工治理，SOP 审批人或复核人来自员工角色任职；两者可能是同一人，也可能不是同一人。</p>
          </div>
          </>
        ) : (
          <div className="gg-type-meta mt-[11px] rounded-[16px] border border-dashed border-[#dfe5ef] px-[16px] py-[22px] text-center" data-topology-example="empty">
            当前权限范围内没有同时具备责任组织和角色绑定的数字员工，暂不生成示例。
          </div>
        )}

        <div className="gg-type-meta mt-[10px] flex items-start gap-[9px] rounded-[12px] border border-[#e5def3] bg-[#fbf8ff] px-[13px] py-[10px] text-[#735895]">
          <Sparkles className="mt-[1px] size-[14px] shrink-0" />
          能力分身说明：专家与数字员工都使用 AgentProfile。只有创建个人版本并形成 owner/source 关系后，才称为用户的能力分身；当前数据库没有可用于本图的来源绑定实例，因此这里只展示模型，不伪造名称。
        </div>
      </div>
    </section>
  );
}

export default function EnterpriseInfoPage({
  currentUser,
  onLogout,
}: {
  currentUser?: EnterpriseAuthUser;
  onLogout?: () => void;
}) {
  const navigate = useNavigate();
  const { tenant } = useEnterpriseContext();
  const canEdit = isEnterpriseAdmin(currentUser);
  const canReadOrganization = canEdit
    || hasGovernancePermission(currentUser, 'organization.read');
  const [name, setName] = useState(tenant.name);
  const [savedName, setSavedName] = useState(tenant.name);
  const [saving, setSaving] = useState(false);
  const [units, setUnits] = useState<OrganizationUnit[]>([]);
  const [positions, setPositions] = useState<Position[]>([]);
  const [topologyAgents, setTopologyAgents] = useState<TopologyAgent[]>([]);
  const [topologyBindings, setTopologyBindings] = useState<TopologyAgentRoleBinding[]>([]);
  const [employeeRoleAssignments, setEmployeeRoleAssignments] = useState<TopologyEmployeeRoleAssignment[]>([]);
  const [positionAssignments, setPositionAssignments] = useState<TopologyPositionAssignment[]>([]);
  const [positionRoleBindings, setPositionRoleBindings] = useState<TopologyPositionRoleBinding[]>([]);
  const [agentSkills, setAgentSkills] = useState<Record<string, TopologyAgentSkill[]>>({});
  const [unitsLoading, setUnitsLoading] = useState(false);
  const [companyDialogOpen, setCompanyDialogOpen] = useState(false);
  const [companyDraft, setCompanyDraft] = useState(EMPTY_COMPANY);
  const [creatingCompany, setCreatingCompany] = useState(false);

  const loadUnits = useCallback(async () => {
    if (!canReadOrganization) return;
    setUnitsLoading(true);
    try {
      const tenantQuery = encodeURIComponent(tenant.id);
      const [unitResult, positionResult, agentResult, bindingResult, employeeRoleResult, positionAssignmentResult, positionRoleResult] = await Promise.allSettled([
        api.get<OrganizationUnit[]>(`/api/organization/units?tenant_id=${tenantQuery}`),
        api.get<Position[]>(
          `/api/organization/positions?tenant_id=${tenantQuery}`,
        ),
        api.get<TopologyAgent[]>(`/api/enterprise/agents?tenant_id=${tenantQuery}`),
        api.get<TopologyAgentRoleBinding[]>(
          `/api/organization/agent-role-bindings?tenant_id=${tenantQuery}`,
        ),
        api.get<TopologyEmployeeRoleAssignment[]>(
          `/api/organization/employee-role-assignments?tenant_id=${tenantQuery}`,
        ),
        api.get<TopologyPositionAssignment[]>(
          `/api/organization/position-assignments?tenant_id=${tenantQuery}`,
        ),
        api.get<TopologyPositionRoleBinding[]>(
          `/api/organization/position-role-bindings?tenant_id=${tenantQuery}`,
        ),
      ]);
      if (unitResult.status === 'rejected') throw unitResult.reason;
      setUnits(unitResult.value);
      setPositions(positionResult.status === 'fulfilled' ? positionResult.value : []);
      setTopologyAgents(agentResult.status === 'fulfilled' ? agentResult.value : []);
      setTopologyBindings(bindingResult.status === 'fulfilled' ? bindingResult.value : []);
      setEmployeeRoleAssignments(
        employeeRoleResult.status === 'fulfilled' ? employeeRoleResult.value : [],
      );
      setPositionAssignments(
        positionAssignmentResult.status === 'fulfilled' ? positionAssignmentResult.value : [],
      );
      setPositionRoleBindings(
        positionRoleResult.status === 'fulfilled' ? positionRoleResult.value : [],
      );
      if (bindingResult.status === 'fulfilled') {
        const scenarioRoles = new Set(SOP_EXAMPLE_ROLES.map((scenario) => scenario.roleName));
        const agentIds = [...new Set(bindingResult.value
          .filter((binding) => binding.status === 'active' && scenarioRoles.has(binding.role_name as typeof SOP_EXAMPLE_ROLES[number]['roleName']))
          .map((binding) => binding.agent_id))];
        const skillResults = await Promise.allSettled(agentIds.map(async (agentId) => ({
          agentId,
          skills: await api.get<TopologyAgentSkill[]>(
            `/api/enterprise/agents/${encodeURIComponent(agentId)}/skills?tenant_id=${tenantQuery}`,
          ),
        })));
        setAgentSkills(Object.fromEntries(skillResults.flatMap((result) => (
          result.status === 'fulfilled' ? [[result.value.agentId, result.value.skills]] : []
        ))));
      } else {
        setAgentSkills({});
      }
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '企业组织加载失败');
    } finally {
      setUnitsLoading(false);
    }
  }, [canReadOrganization, tenant.id]);

  useEffect(() => {
    void loadUnits();
  }, [loadUnits]);

  const rootUnit = useMemo(() => units.find((unit) => unit.is_root) || null, [units]);
  const companyUnits = useMemo(
    () => units.filter((unit) => unit.status === 'active' && unit.unit_type_code === 'company'),
    [units],
  );
  const diagramEnterprise = companyUnits.find((unit) => !unit.is_root)?.name || tenant.name;
  const topologyExamples = useMemo<TopologyExample[]>(() => {
    const activeAgents = new Map(
      topologyAgents
        .filter((agent) => agent.status === 'active' && !agent.is_overall)
        .map((agent) => [agent.id, agent]),
    );
    return SOP_EXAMPLE_ROLES.flatMap((scenario) => {
      const { scenarioName, roleName, skillId } = scenario;
      const binding = topologyBindings.find(
        (item) => item.status === 'active' && item.role_name === roleName,
      );
      if (!binding) return [];
      const agent = activeAgents.get(binding.agent_id);
      if (!agent?.responsible_org_unit_id) return [];
      const organization = units.find((unit) => unit.id === agent.responsible_org_unit_id);
      if (!organization) return [];
      const skill = agentSkills[agent.id]?.find(
        (item) => item.skill_id === skillId && item.status === 'published'
          && (!item.branch_status || item.branch_status === 'active'),
      );
      if (!skill) return [];
      const positionRoleBinding = positionRoleBindings.find(
        (item) => item.status === 'active' && item.business_role_name === roleName,
      );
      const position = positions.find((item) => (
        item.id === positionRoleBinding?.position_id
        && item.org_unit_id === organization.id
        && item.status === 'active'
      ));
      const humanStages = scenario.humanRoleNames.flatMap((humanRoleName) => (
        employeeRoleAssignments.filter(
          (item) => item.status === 'active' && item.role_name === humanRoleName,
        ).map((assignment) => ({
          employeeProfileId: assignment.employee_profile_id,
          roleName: assignment.role_name,
          employeeName: assignment.employee_name,
        }))
      ));
      const positionAssigneeCount = position
        ? new Set(positionAssignments.filter((assignment) => (
          assignment.status === 'active' && assignment.position_id === position.id
        )).map((assignment) => assignment.employee_profile_id)).size
        : 0;
      const positionAssignedToHuman = positionAssigneeCount > 0;
      const verifiedRelations = new Set<string>([
        'AgentRoleBinding.grants.BusinessRole',
        'AgentResourceBinding.loads.SOP',
      ]);
      if (position) {
        verifiedRelations.add('OrganizationUnit.contains.Position');
        verifiedRelations.add('PositionRoleBinding.grants.BusinessRole');
      }
      if (positionAssignedToHuman) verifiedRelations.add('PositionAssignment.assigns.Human');
      if (humanStages.length > 0) verifiedRelations.add('EmployeeRoleAssignment.delegates.BusinessRole');
      if (binding.supervisor_employee_name) verifiedRelations.add('Human.supervises.Agent');
      return [{
        scenarioName,
        organizationName: organization.name,
        positionName: position?.name,
        positionAssignedToHuman,
        positionAssigneeCount,
        agentName: binding.agent_name,
        roleName: binding.role_name,
        supervisorName: binding.supervisor_employee_name,
        humanStages,
        humanAction: scenario.humanAction,
        outcome: scenario.outcome,
        assignmentMode: binding.assignment_mode === 'execute' ? '执行' : '辅助',
        scopeLabel: binding.scope_type === 'tenant' ? '全租户作用域' : '组织作用域',
        sopName: skill.name,
        sopVersion: `${skill.skill_id}@${skill.version}`,
        verifiedRelations,
      }];
    });
  }, [agentSkills, employeeRoleAssignments, positionAssignments, positionRoleBindings, positions, topologyAgents, topologyBindings, units]);

  async function save() {
    if (!name.trim()) {
      notify.error('企业名称不能为空');
      return;
    }
    setSaving(true);
    try {
      const updated = await api.put<{ id: string; name: string }>('/api/auth/context/tenant', {
        name: name.trim(),
      });
      setName(updated.name);
      setSavedName(updated.name);
      notify.success('企业名称已更新，稳定企业编码保持不变');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '企业信息保存失败');
    } finally {
      setSaving(false);
    }
  }

  async function createCompany() {
    if (!rootUnit) {
      notify.error('未找到租户根组织，请刷新后重试');
      return;
    }
    const code = companyDraft.code.trim();
    const companyName = companyDraft.name.trim();
    if (!ORGANIZATION_CODE_PATTERN.test(code)) {
      notify.error('企业编码须以字母开头，只能包含字母、数字、下划线或短横线');
      return;
    }
    if (!companyName) {
      notify.error('企业名称不能为空');
      return;
    }
    setCreatingCompany(true);
    try {
      const created = await api.post<OrganizationUnit>('/api/organization/units', {
        tenant_id: tenant.id,
        parent_id: rootUnit.id,
        code,
        name: companyName,
        unit_type_code: 'company',
      });
      setUnits((current) => [...current, created]);
      setCompanyDraft(EMPTY_COMPANY);
      setCompanyDialogOpen(false);
      notify.success(`已新增企业组织“${created.name}”`);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '新增企业失败');
    } finally {
      setCreatingCompany(false);
    }
  }

  return (
    <PageShell template="management" data-enterprise-info-page>
      <AppHeader
        onLogout={onLogout}
        userName={currentUser?.username}
        left={(
          <div className="flex min-h-[44px] flex-col justify-center gap-[5px]">
            <h1 className="gg-type-page-title">企业与组织</h1>
            <p className="gg-type-body">管理租户边界与企业组织节点，理解真人、数字员工和能力分身如何协作。</p>
          </div>
        )}
      />

      <div className="mt-[22px] grid items-start gap-[20px] min-[1700px]:grid-cols-[380px_minmax(0,1fr)]">
        <div className="grid min-w-0 items-start gap-[20px] min-[1200px]:grid-cols-2 min-[1700px]:grid-cols-1">
        <section data-enterprise-panel className="min-w-0 overflow-hidden rounded-[22px] border border-[#e1e6f1] bg-white shadow-[0_18px_48px_rgba(30,48,91,0.07)]">
          <div className="border-b border-[#e8edf7] bg-[linear-gradient(145deg,#3157e8_0%,#496ee8_62%,#6d86dc_100%)] px-[24px] py-[22px] text-white">
            <div className="flex items-start justify-between gap-[18px]">
              <span className="grid size-[44px] place-items-center rounded-[14px] bg-white/14 shadow-[0_10px_24px_rgba(17,35,92,0.18)] ring-1 ring-white/20">
                <Building2 className="size-[22px]" />
              </span>
              <span className="rounded-full bg-white/12 px-[11px] py-[6px] font-mono gg-type-meta tracking-[0.05em] text-white/85 ring-1 ring-white/20">TENANT BOUNDARY</span>
            </div>
            <h2 className="gg-type-section-title gg-type-section-title--inverse mt-[18px] line-clamp-2">{name}</h2>
            <p className="gg-type-control mt-[6px] text-white/76">租户是数据与认证边界；企业、事业部、部门和项目组统一建模为组织单元。</p>
          </div>

          <div className="grid gap-[16px] p-[22px]">
            <label className="gg-type-control grid gap-[7px] text-[var(--gg-text-secondary)]">
              企业显示名称
              <Input value={name} disabled={!canEdit} onChange={(event) => setName(event.target.value)} />
            </label>
            <label className="gg-type-control grid gap-[7px] text-[var(--gg-text-secondary)]">
              稳定租户编码
              <span className="gg-type-code flex h-[42px] items-center gap-[8px] rounded-[10px] border border-[#e3e7f1] bg-[#f7f8fb] px-[12px] text-[var(--gg-text-secondary)]">
                <LockKeyhole className="size-[14px]" />
                {tenant.id}
              </span>
            </label>
            <div className="gg-type-control flex items-start gap-[9px] rounded-[12px] border border-[#dce5ff] bg-[#f5f8ff] px-[13px] py-[11px] text-[#596b9d]">
              <ShieldCheck className="mt-[1px] size-[14px] shrink-0" />
              租户编码不可修改；新增企业会创建 company 类型组织单元，不会产生新的租户边界。
            </div>
            {canEdit ? (
              <div className="flex justify-end">
                <Button disabled={saving || name.trim() === savedName} onClick={() => void save()}>
                  {saving ? '保存中…' : '保存企业信息'}
                </Button>
              </div>
            ) : (
              <p className="gg-type-control text-[var(--gg-text-muted)]">普通成员可查看企业信息，仅管理员可以修改显示名称。</p>
            )}
          </div>
        </section>

        <section data-organization-panel className="min-w-0 overflow-hidden rounded-[22px] border border-[#e1e6f1] bg-white p-[18px] shadow-[0_14px_38px_rgba(30,48,91,0.05)]">
          <div className="flex items-start justify-between gap-[12px]">
            <div>
              <p className="font-mono gg-type-caption font-semibold uppercase tracking-[0.13em] text-[#6074a9]">ORGANIZATION UNITS</p>
              <h2 className="gg-type-section-title mt-[4px]">企业组织单元</h2>
              <p className="gg-type-meta mt-[4px]">企业和项目组都进入同一棵组织树。</p>
            </div>
            {canEdit && (
              <Button
                size="sm"
                disabled={unitsLoading || !rootUnit}
                onClick={() => setCompanyDialogOpen(true)}
                className="shrink-0 gap-[5px]"
              >
                <Plus className="size-[14px]" />
                新增企业
              </Button>
            )}
          </div>

          <div className="mt-[14px] grid min-w-0 gap-[9px]">
            {(companyUnits.length > 0 ? companyUnits : rootUnit ? [rootUnit] : []).slice(0, 4).map((unit) => (
              <article
                key={unit.id}
                data-organization-unit-card
                className="w-full min-w-0 max-w-full overflow-hidden rounded-[14px] border border-[#e1e7f2] bg-[#fbfcff] px-[13px] py-[11px]"
              >
                <div className="flex min-w-0 items-center gap-[10px]">
                  <span className="grid size-[34px] shrink-0 place-items-center rounded-[11px] bg-[#eaf0ff] text-[#3157e8]">
                    <Building2 className="size-[17px]" />
                  </span>
                  <div className="min-w-0 flex-1 basis-0">
                    <p className="gg-type-control truncate font-semibold text-[var(--gg-text-primary)]">{unit.name}</p>
                    <p className="gg-type-code mt-[2px] truncate text-[var(--gg-text-muted)]">{unit.code}</p>
                  </div>
                  <span className="gg-type-caption rounded-full bg-white px-[8px] py-[4px] ring-1 ring-[#e0e6f2]">company</span>
                </div>
              </article>
            ))}
            {unitsLoading && (
              <div className="h-[58px] animate-pulse rounded-[14px] bg-[#f4f6fa]" aria-label="正在加载企业组织" />
            )}
            {!unitsLoading && canReadOrganization && !rootUnit && (
              <div className="gg-type-meta rounded-[14px] border border-dashed border-[#dfe5f0] px-[14px] py-[18px] text-center">暂无可显示的企业组织单元</div>
            )}
            {!canReadOrganization && (
              <div className="gg-type-meta rounded-[14px] border border-dashed border-[#dfe5f0] px-[14px] py-[18px] text-center">需要组织读取权限才能查看企业组织节点</div>
            )}
          </div>
          <Button variant="outline" className="mt-[12px] w-full" onClick={() => navigate('/enterprise/organization')}>
            <Network className="mr-[6px] size-[14px]" />
            查看完整组织树
          </Button>
        </section>
        </div>

        <EnterpriseCollaborationMap
          enterpriseName={diagramEnterprise}
          examples={topologyExamples}
        />
      </div>

      <Dialog open={companyDialogOpen} onOpenChange={setCompanyDialogOpen}>
        <DialogContent className="max-w-[460px]">
          <DialogTitle>新增企业组织</DialogTitle>
          <p className="gg-type-body">新企业会作为 company 类型组织单元创建在“{rootUnit?.name || tenant.name}”下。</p>
          <div className="grid gap-[13px]">
            <label className="gg-type-control grid gap-[6px] text-[var(--gg-text-secondary)]">
              稳定企业编码
              <Input
                aria-label="稳定企业编码"
                value={companyDraft.code}
                placeholder="例如 subsidiary_east"
                onChange={(event) => setCompanyDraft((current) => ({ ...current, code: event.target.value }))}
              />
            </label>
            <label className="gg-type-control grid gap-[6px] text-[var(--gg-text-secondary)]">
              企业名称
              <Input
                aria-label="新增企业名称"
                value={companyDraft.name}
                placeholder="输入企业显示名称"
                onChange={(event) => setCompanyDraft((current) => ({ ...current, name: event.target.value }))}
              />
            </label>
          </div>
          <div className="flex justify-end gap-[8px]">
            <Button variant="outline" onClick={() => setCompanyDialogOpen(false)}>取消</Button>
            <Button
              disabled={creatingCompany || !companyDraft.code.trim() || !companyDraft.name.trim()}
              onClick={() => void createCompany()}
            >
              {creatingCompany ? '创建中…' : '创建企业组织'}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </PageShell>
  );
}

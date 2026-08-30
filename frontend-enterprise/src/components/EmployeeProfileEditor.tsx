import { IdcardOutlined } from '../icons';
import { X as XIcon } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import {
  Button as UIButton,
  Dialog,
  DialogContent,
  DialogTitle,
  Input,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Textarea,
  notify,
} from '@/components/ui';
import {
  DIALOG_CANCEL_BUTTON_CLASS,
  DIALOG_PRIMARY_BUTTON_CLASS,
  DETAIL_ACTIONS_CLASS,
  DETAIL_FACT_CARD_CLASS,
  SELECT_TRIGGER_CLASS,
} from '@/lib/enterprise-ui';
import { cn } from '@/lib/utils';
import { api, getRequestTenantId } from '../api/client';
import {
  hasGovernancePermission,
  isEnterpriseAdmin,
  type EnterpriseAuthUser,
} from '../auth';
import {
  canManageEmployeeAgent,
  employeeDisplayName,
  employeeProfile,
  expertSourceLabel,
  isExpertTemplate,
} from '../employee';
import type { AgentProfileRead } from '../types';
import type { OrganizationUnit } from '../types/organization';
import EmployeeAvatar from './EmployeeAvatar';
import { ConceptHelp } from './ConceptHelp';
import { OrganizationTreeNavigator } from './OrganizationTreeNavigator';

type EmployeeProfileFormValues = {
  name: string;
  roleName: string;
  onboardedAt: string;
  description: string;
  personaPrompt: string;
  systemPromptSummary: string;
  workStyles: string[];
  expertiseTags: string[];
  workModes: string[];
  status: 'active' | 'archived';
};

const STYLE_OPTIONS = ['目标明确', '证据优先', '动作可追溯', '事实先行', '流程推进', '风险克制', '及时追问'];
const EXPERTISE_OPTIONS = ['业务问答', 'SOP 执行', '工具调用', '代码检索', '报销核对', '事务跟进', '资料维护'];
const WORK_MODE_OPTIONS = ['识别意图', '补齐信息', '调用 SOP', '查询资料', '执行并复盘', '确认后执行', '必要时转人工'];

const BLANK_FORM: EmployeeProfileFormValues = {
  name: '',
  roleName: '',
  onboardedAt: '',
  description: '',
  personaPrompt: '',
  systemPromptSummary: '',
  workStyles: [],
  expertiseTags: [],
  workModes: [],
  status: 'active',
};

export default function EmployeeProfileEditor({
  agent,
  open,
  onClose,
  onSaved,
  currentUser,
  mode = 'employee',
}: {
  agent?: AgentProfileRead | null;
  open: boolean;
  onClose: () => void;
  onSaved?: (agent: AgentProfileRead) => void;
  currentUser?: EnterpriseAuthUser;
  mode?: 'employee' | 'expert-template';
}) {
  const [form, setForm] = useState<EmployeeProfileFormValues>(BLANK_FORM);
  const [saving, setSaving] = useState(false);
  const [savingResponsibility, setSavingResponsibility] = useState(false);
  const [selectedResponsibilityOrg, setSelectedResponsibilityOrg] =
    useState<OrganizationUnit | null>(null);
  const profile = useMemo(() => employeeProfile(agent), [agent]);
  const isExpertTemplateMode = mode === 'expert-template' && isExpertTemplate(agent);
  const canEdit = Boolean(
    agent
    && !isExpertTemplateMode
    && canManageEmployeeAgent(agent, currentUser),
  );
  const canGovernResponsibility = Boolean(
    currentUser
    && !isExpertTemplateMode
    && (isEnterpriseAdmin(currentUser)
      || hasGovernancePermission(currentUser, 'agent.manage')),
  );
  const metadata = agent?.metadata || {};
  const relationshipFacts = agent
    ? isExpertTemplateMode
      ? [
          ['资产归属', '平台内置'],
          ['来源', expertSourceLabel(agent) || String(metadata.source_repository || '项目内置快照')],
          ['发布状态', agent.published_to_gallery ? '已发布到开放平台' : '待发布'],
          ['模板修订', `r${agent.profile_revision ?? 1}`],
          ['专家方向', String(metadata.expert_category || '未分类')],
          ['可见范围', agent.visibility_scope || 'private'],
          ['复制关系', '用户复制后形成个人能力分身'],
          ['非敏感资源摘要', `${agent.resources.length} 项绑定`],
        ]
      : [
          ['所有者', agent.owner_user_id || String(metadata.owner_user_id || '未确认')],
          ['创建者', String(metadata.created_by_display_name || metadata.created_by_username || '未记录')],
          ['责任组织', agent.responsible_org_unit_name || agent.responsible_org_unit_id || '暂未指定'],
          ['最近发布者', agent.gallery_published_by || '未发布'],
          ['状态 / 修订', `${agent.status} / r${agent.profile_revision ?? 1}`],
          ['业务分类', agent.agent_category_code || 'assistant'],
          ['可见范围', agent.visibility_scope || 'private'],
          ['复制来源', agent.source_agent_id
            ? `${agent.source_agent_id} @ ${agent.source_agent_version || 'legacy'}`
            : '原始创建'],
          ['非敏感资源摘要', `${agent.resources.length} 项绑定`],
        ]
    : [];

  const update = (patch: Partial<EmployeeProfileFormValues>) => setForm((prev) => ({ ...prev, ...patch }));

  useEffect(() => {
    if (!open || !agent) return;
    setForm({
      name: employeeDisplayName(agent),
      roleName: profile.roleName === '待补充岗位' ? '' : profile.roleName,
      onboardedAt: profile.onboardedAt === '-' ? new Date().toISOString().slice(0, 10) : profile.onboardedAt,
      description: agent.description || '',
      personaPrompt: agent.persona_prompt || '',
      systemPromptSummary: typeof agent.metadata?.system_prompt_summary === 'string' ? agent.metadata.system_prompt_summary : '',
      workStyles: profile.workStyles,
      expertiseTags: profile.expertiseTags,
      workModes: profile.workModes,
      status: agent.status === 'archived' ? 'archived' : 'active',
    });
    setSelectedResponsibilityOrg(null);
  }, [agent, open, profile]);

  async function saveResponsibility(
    responsibleOrgUnitId: string | null,
    responsibleOrgUnitName?: string,
  ) {
    if (!agent || !canGovernResponsibility) return;
    setSavingResponsibility(true);
    try {
      const saved = await api.put<AgentProfileRead>(
        `/api/enterprise/agents/${agent.id}/responsibility`,
        {
          tenant_id: getRequestTenantId(),
          responsible_org_unit_id: responsibleOrgUnitId,
        },
      );
      notify.success(
        responsibleOrgUnitId
          ? `责任组织已设为：${responsibleOrgUnitName || saved.responsible_org_unit_name || ''}`
          : '责任组织已清除',
      );
      onSaved?.(saved);
      setSelectedResponsibilityOrg(null);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '保存责任组织失败');
    } finally {
      setSavingResponsibility(false);
    }
  }

  async function save() {
    if (!agent) return;
    if (!form.name.trim()) {
      notify.error('请输入数字员工姓名');
      return;
    }
    setSaving(true);
    try {
      const metadata: Record<string, unknown> = {
        ...(agent.metadata || {}),
        blank_onboarding: false,
        role_name: form.roleName.trim() || '待补充岗位',
        onboarded_at: form.onboardedAt || new Date().toISOString().slice(0, 10),
        system_prompt_summary: form.systemPromptSummary.trim(),
        work_styles: compactTags(form.workStyles),
        expertise_tags: compactTags(form.expertiseTags),
        work_modes: compactTags(form.workModes),
      };

      const saved = await api.put<AgentProfileRead>(`/api/enterprise/agents/${agent.id}`, {
        tenant_id: getRequestTenantId(),
        name: form.name.trim(),
        description: form.description.trim(),
        persona_prompt: form.personaPrompt.trim(),
        status: form.status,
        metadata,
      });
      notify.success('数字员工档案已更新');
      onSaved?.(saved);
      onClose();
      window.dispatchEvent(new Event('gongge-enterprise-agent-scope-refresh'));
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '保存数字员工档案失败');
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next && !saving) onClose(); }}>
      <DialogContent
        aria-describedby={undefined}
        className="employee-profile-modal flex max-h-[calc(100dvh-4rem)] w-[calc(100%-2rem)] flex-col gap-[16px] overflow-hidden rounded-[var(--gg-radius-panel)] border border-[var(--gg-line)] bg-[var(--gg-surface)] px-[20px] py-[16px] shadow-[var(--gg-shadow-card)] sm:max-w-[860px]"
      >
        <DialogTitle className="gg-type-section-title px-[12px]">
          {agent
            ? isExpertTemplateMode
              ? `查看专家模板：${employeeDisplayName(agent)}`
              : `${canEdit ? '编辑' : '查看'}数字员工档案：${employeeDisplayName(agent)}`
            : '编辑数字员工档案'}
        </DialogTitle>

        <div className="min-h-0 flex-1 overflow-y-auto px-[12px]">
          <div className="employee-profile-editor">
            <div className="employee-profile-preview">
              <EmployeeAvatar agent={agent} size={92} />
              <div>
                <span className="gg-type-meta m-0 block">
                  {isExpertTemplateMode ? '专家模板（平台内置）' : '数字员工档案'}
                </span>
                <h4 className="gg-type-section-title mt-[4px] mb-[6px]">{agent ? employeeDisplayName(agent) : '数字员工'}</h4>
                <span className="gg-type-meta m-0 block">{profile.roleName}</span>
              </div>
              <span className="employee-profile-preview-icon"><IdcardOutlined /></span>
            </div>

            <div className="employee-profile-form flex min-w-0 flex-col gap-[14px]">
              <div className={cn(
                'grid grid-cols-2 gap-x-[16px] gap-y-[8px] px-[14px] py-[12px] max-[640px]:grid-cols-1',
                DETAIL_FACT_CARD_CLASS,
              )}>
                {relationshipFacts.map(([label, value]) => (
                  <div key={label} className="min-w-0">
                    <span className="gg-type-caption">{label}</span>
                    <strong className="gg-type-control ml-[6px] break-all font-semibold text-[var(--gg-text-primary)]">
                      {value}
                    </strong>
                  </div>
                ))}
              </div>
              {canGovernResponsibility && agent && !agent.is_overall ? (
                <section className={cn('grid gap-[10px] p-[12px]', DETAIL_FACT_CARD_CLASS, 'bg-[var(--gg-surface)]')}>
                  <div>
                    <strong className="gg-type-control flex items-center gap-[5px] font-semibold text-[var(--gg-text-primary)]">
                      责任组织
                      <ConceptHelp topic="governance" />
                    </strong>
                    <span className="gg-type-caption leading-[1.6]">
                      只表示谁负责治理该数字员工，不自动改变服务范围、执行授权或知识权限。
                    </span>
                  </div>
                  <OrganizationTreeNavigator
                    className="max-h-[260px] overflow-y-auto rounded-[var(--gg-radius-control)] border border-[var(--gg-line)] bg-[var(--gg-surface-subtle)] p-[8px]"
                    onSelect={setSelectedResponsibilityOrg}
                    selectedId={
                      selectedResponsibilityOrg?.id
                      || agent.responsible_org_unit_id
                      || ''
                    }
                    selectRootOnInitialize={false}
                    tenantId={getRequestTenantId()}
                  />
                  <div className="flex flex-wrap items-center justify-between gap-[8px]">
                    <span className="gg-type-caption min-w-0 flex-1 truncate text-[var(--gg-text-secondary)]">
                      {selectedResponsibilityOrg
                        ? `待设置：${selectedResponsibilityOrg.name}`
                        : `当前：${agent.responsible_org_unit_name || '暂未指定'}`}
                    </span>
                    <div className="flex gap-[8px]">
                      {agent.responsible_org_unit_id ? (
                        <UIButton
                          disabled={savingResponsibility}
                          onClick={() => void saveResponsibility(null)}
                          type="button"
                          variant="outline"
                        >
                          清除
                        </UIButton>
                      ) : null}
                      <UIButton
                        disabled={!selectedResponsibilityOrg || savingResponsibility}
                        onClick={() => {
                          if (!selectedResponsibilityOrg) return;
                          void saveResponsibility(
                            selectedResponsibilityOrg.id,
                            selectedResponsibilityOrg.name,
                          );
                        }}
                        type="button"
                      >
                        设为责任组织
                      </UIButton>
                    </div>
                  </div>
                </section>
              ) : null}
              <fieldset
                className="flex min-w-0 flex-col gap-[14px] disabled:opacity-75"
                disabled={!canEdit}
              >
                <div className="employee-profile-form-grid">
                <LabeledField label={isExpertTemplateMode ? '模板名称' : '数字员工姓名'}>
                  <Input value={form.name} placeholder="例如：默认员工" onChange={(event) => update({ name: event.target.value })} />
                </LabeledField>
                <LabeledField label={isExpertTemplateMode ? '专家方向' : '岗位'}>
                  <Input value={form.roleName} placeholder="例如：研发" onChange={(event) => update({ roleName: event.target.value })} />
                </LabeledField>
                <LabeledField label={isExpertTemplateMode ? '导入时间' : '入职时间'}>
                  <Input type="date" value={form.onboardedAt} onChange={(event) => update({ onboardedAt: event.target.value })} />
                </LabeledField>
                <LabeledField label={isExpertTemplateMode ? '发布状态' : '工作状态'}>
                  {isExpertTemplateMode ? (
                    <div className="gg-type-control flex h-10 items-center rounded-[var(--gg-radius-control)] border border-[var(--gg-line)] bg-[var(--gg-surface-subtle)] px-3 text-[var(--gg-text-secondary)]">
                      {agent?.published_to_gallery ? '已发布到开放平台' : '待发布'}
                    </div>
                  ) : (
                    <Select value={form.status} onValueChange={(value) => update({ status: value as 'active' | 'archived' })}>
                      <SelectTrigger className={`${SELECT_TRIGGER_CLASS} w-full`}>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="active">在线</SelectItem>
                        <SelectItem value="archived">下线</SelectItem>
                      </SelectContent>
                    </Select>
                  )}
                </LabeledField>
                </div>

                <LabeledField label={isExpertTemplateMode ? '专家简介' : '岗位描述'}>
                  <Textarea rows={3} value={form.description} placeholder="概括这个数字员工的岗位边界、服务风格和执行重点" onChange={(event) => update({ description: event.target.value })} />
                </LabeledField>
                <LabeledField label={isExpertTemplateMode ? '能力摘要' : '看板摘要'}>
                  <Textarea rows={2} value={form.systemPromptSummary} placeholder="用于数字员工档案页顶部展示的 system prompt 摘要" onChange={(event) => update({ systemPromptSummary: event.target.value })} />
                </LabeledField>
                <LabeledField label={isExpertTemplateMode ? '专家执行边界' : '岗位执行约束'}>
                  <Textarea rows={4} value={form.personaPrompt} placeholder="员工在对话中的角色、人设、回复风格和执行边界" onChange={(event) => update({ personaPrompt: event.target.value })} />
                </LabeledField>

                <div className="employee-profile-form-grid is-tags">
                  <LabeledField label="掌握方向">
                    <TagsField value={form.expertiseTags} options={EXPERTISE_OPTIONS} placeholder="输入后回车添加" onChange={(next) => update({ expertiseTags: next })} />
                  </LabeledField>
                  <LabeledField label="工作风格">
                    <TagsField value={form.workStyles} options={STYLE_OPTIONS} placeholder="输入后回车添加" onChange={(next) => update({ workStyles: next })} />
                  </LabeledField>
                  <LabeledField label="工作模式">
                    <TagsField value={form.workModes} options={WORK_MODE_OPTIONS} placeholder="输入后回车添加" onChange={(next) => update({ workModes: next })} />
                  </LabeledField>
                </div>
              </fieldset>
            </div>
          </div>
        </div>

        <div className={cn(DETAIL_ACTIONS_CLASS, 'px-[12px]')}>
          <UIButton
            variant="outline"
            disabled={saving}
            onClick={onClose}
            className={DIALOG_CANCEL_BUTTON_CLASS}
          >
            {isExpertTemplateMode ? '关闭' : '取消'}
          </UIButton>
          {canEdit && (
            <UIButton
              disabled={saving}
              onClick={() => void save()}
              className={DIALOG_PRIMARY_BUTTON_CLASS}
            >
              保存
            </UIButton>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}

function LabeledField({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-[6px]">
      <span className="gg-type-control text-[var(--gg-text-secondary)]">{label}</span>
      {children}
    </label>
  );
}

function TagsField({
  value,
  options,
  placeholder,
  onChange,
}: {
  value: string[];
  options: string[];
  placeholder?: string;
  onChange: (next: string[]) => void;
}) {
  const [draft, setDraft] = useState('');
  const addTags = (raw: string) => {
    const parts = raw.split(/[,，]/).map((item) => item.trim()).filter(Boolean);
    if (parts.length) onChange(Array.from(new Set([...value, ...parts])));
    setDraft('');
  };
  const removeTag = (tag: string) => onChange(value.filter((item) => item !== tag));
  const suggestions = options.filter((item) => !value.includes(item));

  return (
    <div className="flex flex-col gap-[8px]">
      <div className="flex min-h-[34px] flex-wrap items-center gap-[6px] rounded-[var(--gg-radius-control)] border border-[var(--gg-line)] bg-[var(--gg-surface)] px-[8px] py-[5px] transition-colors focus-within:border-[var(--gg-interaction)]">
        {value.map((tag) => (
          <span
            key={tag}
            className="gg-type-control inline-flex items-center gap-[4px] rounded-[var(--gg-radius-control)] bg-[var(--gg-state-neutral-soft)] px-[8px] py-[2px] text-[var(--gg-text-primary)]"
          >
            {tag}
            <button
              type="button"
              aria-label={`移除 ${tag}`}
              onClick={() => removeTag(tag)}
              className="grid place-items-center text-[var(--gg-text-muted)] hover:text-[var(--gg-text-primary)]"
            >
              <XIcon className="size-[12px]" />
            </button>
          </span>
        ))}
        <input
          value={draft}
          placeholder={value.length ? '' : placeholder}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' || event.key === ',' || event.key === '，') {
              event.preventDefault();
              addTags(draft);
            } else if (event.key === 'Backspace' && !draft && value.length) {
              removeTag(value[value.length - 1]);
            }
          }}
          onBlur={() => draft.trim() && addTags(draft)}
          className="gg-type-control h-[22px] min-w-[80px] flex-1 bg-transparent text-[var(--gg-text-primary)] outline-none placeholder:text-[#9aa6b8]"
        />
      </div>
      {suggestions.length > 0 && (
        <div className="flex flex-wrap gap-[6px]">
          {suggestions.map((item) => (
            <button
              key={item}
              type="button"
              onClick={() => addTags(item)}
              className="gg-type-control rounded-[var(--gg-radius-control)] border border-[var(--gg-line)] px-[8px] py-[2px] text-[var(--gg-text-muted)] hover:border-[var(--gg-interaction)] hover:text-[var(--gg-interaction)]"
            >
              + {item}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function compactTags(values: string[] | undefined): string[] {
  return Array.from(new Set((values || []).map((item) => item.trim()).filter(Boolean))).slice(0, 12);
}

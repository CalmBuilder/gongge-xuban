import { SaveOutlined, UserOutlined } from '../icons';
import { useEffect, useState, type ReactNode } from 'react';
import {
  Button as UIButton,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Input,
  Switch,
  Textarea,
  notify,
} from '@/components/ui';
import { api, getRequestTenantId } from '../api/client';
import type { AgentProfileRead, PersonaRead, UIConfigRead } from '../types';

const ENTERPRISE_AGENT_STORAGE_KEY = 'gongge_enterprise_agent_scope';

type PersonaForm = {
  agent_name: string;
  agent_description: string;
  system_prompt: string;
};

type UiConfigForm = {
  show_thinking_trace: boolean;
  show_skill_trace: boolean;
  show_tool_trace: boolean;
  reflection_max_rounds: string;
  agent_loop_max_actions: string;
  context_token_budget: string;
  context_compaction_trigger_ratio: string;
  context_recent_round_limit: string;
  long_summary_token_budget: string;
  medium_summary_token_budget: string;
};

const MIN_CONTEXT_TOKEN_BUDGET = 4096;
const MAX_CONTEXT_TOKEN_BUDGET = 262144;
const MIN_COMPACTION_TRIGGER_RATIO = 0.1;
const MAX_COMPACTION_TRIGGER_RATIO = 0.95;
const MAX_RECENT_ROUND_LIMIT = 50;
const MIN_SUMMARY_TOKEN_BUDGET = 128;
const MAX_SUMMARY_TOKEN_BUDGET = 32768;

const BLANK_PERSONA: PersonaForm = { agent_name: '', agent_description: '', system_prompt: '' };
const DEFAULT_UI_CONFIG: UiConfigForm = {
  show_thinking_trace: true,
  show_skill_trace: true,
  show_tool_trace: true,
  reflection_max_rounds: '1',
  agent_loop_max_actions: '6',
  context_token_budget: '128000',
  context_compaction_trigger_ratio: '0.70',
  context_recent_round_limit: '6',
  long_summary_token_budget: '4000',
  medium_summary_token_budget: '4000',
};

function uiConfigFormFromRead(row: UIConfigRead): UiConfigForm {
  /** 将服务端配置映射为表单字符串，避免读取和保存使用两套字段口径。 */

  return {
    show_thinking_trace: row.show_thinking_trace,
    show_skill_trace: row.show_skill_trace,
    show_tool_trace: row.show_tool_trace,
    reflection_max_rounds: String(row.reflection_max_rounds),
    agent_loop_max_actions: String(row.agent_loop_max_actions),
    context_token_budget: String(row.context_token_budget),
    context_compaction_trigger_ratio: String(row.context_compaction_trigger_ratio),
    context_recent_round_limit: String(row.context_recent_round_limit),
    long_summary_token_budget: String(row.long_summary_token_budget),
    medium_summary_token_budget: String(row.medium_summary_token_budget),
  };
}

export function validateUiConfigForm(uiForm: UiConfigForm): string | null {
  /** 校验管理端表单与后端边界一致，并在提交前阻止摘要预算总量越界。 */

  const reflectionMaxRounds = Number(uiForm.reflection_max_rounds);
  const agentLoopMaxActions = Number(uiForm.agent_loop_max_actions);
  const contextTokenBudget = Number(uiForm.context_token_budget);
  const contextCompactionTriggerRatio = Number(uiForm.context_compaction_trigger_ratio);
  const contextRecentRoundLimit = Number(uiForm.context_recent_round_limit);
  const longSummaryTokenBudget = Number(uiForm.long_summary_token_budget);
  const mediumSummaryTokenBudget = Number(uiForm.medium_summary_token_budget);
  const hasValidInteger = (value: number, minimum: number, maximum: number) => (
    Number.isInteger(value) && value >= minimum && value <= maximum
  );

  if (
    !hasValidInteger(reflectionMaxRounds, 0, 5)
    || !hasValidInteger(agentLoopMaxActions, 1, 20)
    || !hasValidInteger(contextTokenBudget, MIN_CONTEXT_TOKEN_BUDGET, MAX_CONTEXT_TOKEN_BUDGET)
    || !Number.isFinite(contextCompactionTriggerRatio)
    || contextCompactionTriggerRatio < MIN_COMPACTION_TRIGGER_RATIO
    || contextCompactionTriggerRatio > MAX_COMPACTION_TRIGGER_RATIO
    || !hasValidInteger(contextRecentRoundLimit, 1, MAX_RECENT_ROUND_LIMIT)
    || !hasValidInteger(longSummaryTokenBudget, MIN_SUMMARY_TOKEN_BUDGET, MAX_SUMMARY_TOKEN_BUDGET)
    || !hasValidInteger(mediumSummaryTokenBudget, MIN_SUMMARY_TOKEN_BUDGET, MAX_SUMMARY_TOKEN_BUDGET)
  ) {
    return '执行与上下文配置必须是安全范围内的有效数字';
  }
  if (longSummaryTokenBudget + mediumSummaryTokenBudget > contextTokenBudget) {
    return '长期摘要预算与近期摘要预算之和不能超过上下文最大 token';
  }
  return null;
}

function formatDateOnly(value: string): string {
  const normalized = /(?:z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : `${value}Z`;
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) {
    return value.slice(0, 10);
  }
  return date.toISOString().slice(0, 10);
}

export default function PersonaPage() {
  const [form, setForm] = useState<PersonaForm>(BLANK_PERSONA);
  const [uiForm, setUiForm] = useState<UiConfigForm>(DEFAULT_UI_CONFIG);
  const [loading, setLoading] = useState(false);
  const [uiLoading, setUiLoading] = useState(false);
  const [updatedAt, setUpdatedAt] = useState('');
  const [uiUpdatedAt, setUiUpdatedAt] = useState('');
  const [agents, setAgents] = useState<AgentProfileRead[]>([]);
  const [selectedAgentId, setSelectedAgentId] = useState(() => window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) || '');
  const selectedAgent = agents.find((agent) => agent.id === selectedAgentId) || null;
  const isOverallPersona = !selectedAgent || selectedAgent.is_overall;

  const updatePersona = (patch: Partial<PersonaForm>) => setForm((prev) => ({ ...prev, ...patch }));
  const updateUiConfig = (patch: Partial<UiConfigForm>) => setUiForm((prev) => ({ ...prev, ...patch }));

  useEffect(() => {
    void loadPersonaScope();
    api
      .get<UIConfigRead>(`/api/enterprise/ui-config?tenant_id=${getRequestTenantId()}`)
      .then((row) => {
        setUiForm(uiConfigFormFromRead(row));
        setUiUpdatedAt(row.updated_at);
      })
      .catch((error) => notify.error(error.message));
  }, []);

  useEffect(() => {
    const onScopeChange = (event: Event) => {
      const agentId = (event as CustomEvent<{ agentId?: string }>).detail?.agentId || '';
      if (agentId) setSelectedAgentId(agentId);
    };
    window.addEventListener('gongge-enterprise-agent-scope-change', onScopeChange);
    return () => window.removeEventListener('gongge-enterprise-agent-scope-change', onScopeChange);
  }, []);

  useEffect(() => {
    const agent = agents.find((item) => item.id === selectedAgentId);
    if (agent) {
      if (agent.is_overall) {
        api
          .get<PersonaRead>(`/api/enterprise/persona?tenant_id=${getRequestTenantId()}`)
          .then((row) => {
            setForm({
              agent_name: agent.name,
              agent_description: agent.description || '',
              system_prompt: agent.persona_prompt || row.system_prompt,
            });
            setUpdatedAt(agent.updated_at || row.updated_at);
          })
          .catch((error) => notify.error(error.message));
        return;
      }
      setForm({
        agent_name: agent.name,
        agent_description: agent.description || '',
        system_prompt: agent.persona_prompt || '',
      });
      setUpdatedAt(agent.updated_at);
      return;
    }
    api
      .get<PersonaRead>(`/api/enterprise/persona?tenant_id=${getRequestTenantId()}`)
      .then((row) => {
        setForm((prev) => ({ ...prev, system_prompt: row.system_prompt }));
        setUpdatedAt(row.updated_at);
      })
      .catch((error) => notify.error(error.message));
  }, [agents, selectedAgentId]);

  async function loadPersonaScope() {
    try {
      const rows = await api.get<AgentProfileRead[]>(`/api/enterprise/agents?tenant_id=${getRequestTenantId()}`);
      setAgents(rows);
      setSelectedAgentId((current) => {
        const stored = window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY);
        const candidate = current || stored || '';
        if (candidate && rows.some((agent) => agent.id === candidate)) return candidate;
        return rows.find((agent) => agent.is_overall)?.id || rows[0]?.id || '';
      });
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载员工域失败');
    }
  }

  async function save() {
    if (!form.system_prompt.trim() || (selectedAgent && !form.agent_name.trim())) {
      notify.error('请填写必填项');
      return;
    }
    setLoading(true);
    try {
      if (selectedAgent) {
        const row = await api.put<AgentProfileRead>(`/api/enterprise/agents/${selectedAgent.id}`, {
          tenant_id: getRequestTenantId(),
          name: form.agent_name,
          description: form.agent_description,
          persona_prompt: form.system_prompt,
          status: selectedAgent.status,
        });
        setAgents((prev) => prev.map((item) => (item.id === row.id ? { ...row, resources: item.resources } : item)));
        setUpdatedAt(row.updated_at);
        if (row.is_overall) {
          await api.put<PersonaRead>('/api/enterprise/persona', {
            tenant_id: getRequestTenantId(),
            system_prompt: form.system_prompt,
          });
        }
        window.dispatchEvent(new CustomEvent('gongge-enterprise-agent-scope-change', { detail: { agentId: row.id } }));
        notify.success('岗位人设已保存');
      } else {
        const row = await api.put<PersonaRead>('/api/enterprise/persona', {
          tenant_id: getRequestTenantId(),
          system_prompt: form.system_prompt,
        });
        setUpdatedAt(row.updated_at);
        notify.success('组织默认岗位人设已保存');
      }
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '保存失败');
    } finally {
      setLoading(false);
    }
  }

  async function saveUiConfig() {
    const reflectionMaxRounds = Number(uiForm.reflection_max_rounds);
    const agentLoopMaxActions = Number(uiForm.agent_loop_max_actions);
    const contextTokenBudget = Number(uiForm.context_token_budget);
    const contextCompactionTriggerRatio = Number(uiForm.context_compaction_trigger_ratio);
    const contextRecentRoundLimit = Number(uiForm.context_recent_round_limit);
    const validationError = validateUiConfigForm(uiForm);
    if (validationError) {
      notify.error(validationError);
      return;
    }
    setUiLoading(true);
    try {
      const row = await api.put<UIConfigRead>('/api/enterprise/ui-config', {
        tenant_id: getRequestTenantId(),
        show_thinking_trace: uiForm.show_thinking_trace,
        show_skill_trace: uiForm.show_skill_trace,
        show_tool_trace: uiForm.show_tool_trace,
        reflection_max_rounds: reflectionMaxRounds,
        agent_loop_max_actions: agentLoopMaxActions,
        context_token_budget: contextTokenBudget,
        context_compaction_trigger_ratio: contextCompactionTriggerRatio,
        context_recent_round_limit: contextRecentRoundLimit,
        long_summary_token_budget: Number(uiForm.long_summary_token_budget),
        medium_summary_token_budget: Number(uiForm.medium_summary_token_budget),
      });
      setUiForm(uiConfigFormFromRead(row));
      setUiUpdatedAt(row.updated_at);
      notify.success('展示设置已保存');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '保存失败');
    } finally {
      setUiLoading(false);
    }
  }

  return (
    <>
      <div className="page-title">
        <div>
          <h3 className="gg-type-page-title">岗位人设</h3>
        </div>
        <UIButton disabled={loading} onClick={() => void save()}>
          <SaveOutlined />
          保存
        </UIButton>
      </div>
      <Card className="editor-card">
        <CardHeader>
          <CardTitle className="flex items-center gap-[6px]"><UserOutlined /> 岗位人设</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-[14px]">
          <LabeledField label="名称">
            <Input value={form.agent_name} placeholder="数字员工姓名" onChange={(event) => updatePersona({ agent_name: event.target.value })} />
          </LabeledField>
          <LabeledField label="描述">
            <Textarea rows={2} value={form.agent_description} placeholder="员工岗位描述" onChange={(event) => updatePersona({ agent_description: event.target.value })} />
          </LabeledField>
          <LabeledField label="岗位 Prompt">
            <Textarea
              className="persona-editor"
              rows={12}
              value={form.system_prompt}
              placeholder={isOverallPersona ? '输入组织默认岗位人设' : '输入仅当前员工可见的岗位人设'}
              onChange={(event) => updatePersona({ system_prompt: event.target.value })}
            />
          </LabeledField>
          {updatedAt && <span className="gg-type-meta text-muted-foreground">最后更新：{formatDateOnly(updatedAt)}</span>}
        </CardContent>
      </Card>
      <Card className="editor-card settings-card">
        <CardHeader>
          <CardTitle>执行记录与展示设置</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-[16px]">
          <SwitchRow label="展示思考状态" checked={uiForm.show_thinking_trace} onChange={(next) => updateUiConfig({ show_thinking_trace: next })} />
          <SwitchRow label="展示执行技能" checked={uiForm.show_skill_trace} onChange={(next) => updateUiConfig({ show_skill_trace: next })} />
          <SwitchRow label="展示工具调用" checked={uiForm.show_tool_trace} onChange={(next) => updateUiConfig({ show_tool_trace: next })} />
          <LabeledField label="反思轮数" hint="设为 0 时关闭反思；每轮允许模型检查当前技能和工具结果，并决定是否重试其他技能或工具。">
            <Input
              type="number"
              min={0}
              max={5}
              step={1}
              value={uiForm.reflection_max_rounds}
              onChange={(event) => updateUiConfig({ reflection_max_rounds: event.target.value })}
            />
          </LabeledField>
          <LabeledField label="单轮最大动作数" hint="控制一次用户输入内员工可连续决策和调用工具的最大次数，用于避免无限循环。">
            <Input
              type="number"
              min={1}
              max={20}
              step={1}
              value={uiForm.agent_loop_max_actions}
              onChange={(event) => updateUiConfig({ agent_loop_max_actions: event.target.value })}
            />
          </LabeledField>
          <LabeledField label="上下文最大 token" hint="控制历史消息投影上限，允许范围为 4096–262144；128K 是新配置默认值，实际模型可能更小。" hintId="context-token-budget-hint">
            <Input
              id="context-token-budget"
              name="context_token_budget"
              aria-label="上下文最大 token"
              aria-describedby="context-token-budget-hint"
              type="number"
              min={MIN_CONTEXT_TOKEN_BUDGET}
              max={MAX_CONTEXT_TOKEN_BUDGET}
              step={1024}
              value={uiForm.context_token_budget}
              onChange={(event) => updateUiConfig({ context_token_budget: event.target.value })}
            />
          </LabeledField>
          <LabeledField label="触发压缩比例" hint="上下文达到该比例时开始摘要压缩，允许范围为 0.10–0.95。" hintId="context-compaction-ratio-hint">
            <Input
              id="context-compaction-ratio"
              name="context_compaction_trigger_ratio"
              aria-label="触发压缩比例"
              aria-describedby="context-compaction-ratio-hint"
              type="number"
              min={MIN_COMPACTION_TRIGGER_RATIO}
              max={MAX_COMPACTION_TRIGGER_RATIO}
              step={0.05}
              value={uiForm.context_compaction_trigger_ratio}
              onChange={(event) => updateUiConfig({ context_compaction_trigger_ratio: event.target.value })}
            />
          </LabeledField>
          <LabeledField label="保留近期轮数" hint="压缩时原样保留的最近用户对话轮数，允许范围为 1–50。" hintId="context-recent-round-hint">
            <Input
              id="context-recent-round-limit"
              name="context_recent_round_limit"
              aria-label="保留近期轮数"
              aria-describedby="context-recent-round-hint"
              type="number"
              min={1}
              max={MAX_RECENT_ROUND_LIMIT}
              step={1}
              value={uiForm.context_recent_round_limit}
              onChange={(event) => updateUiConfig({ context_recent_round_limit: event.target.value })}
            />
          </LabeledField>
          <div className="rounded-[14px] border border-[#dfe7f5] bg-[#f8fbff] p-[14px]">
            <div className="gg-type-body font-medium text-[#28334d]">摘要预算</div>
            <p className="mt-[4px] gg-type-caption text-[#697792]">
              长期摘要和近期摘要共同占用上下文预算；两项合计不能超过上下文最大 token。
            </p>
            <div className="mt-[12px] grid gap-[14px] md:grid-cols-2">
              <LabeledField label="长期摘要 token" hint="保留长期事实和历史结论，允许范围为 128–32768。" hintId="long-summary-budget-hint">
                <Input
                  id="long-summary-token-budget"
                  name="long_summary_token_budget"
                  aria-label="长期摘要 token"
                  aria-describedby="long-summary-budget-hint"
                  type="number"
                  min={MIN_SUMMARY_TOKEN_BUDGET}
                  max={MAX_SUMMARY_TOKEN_BUDGET}
                  step={128}
                  value={uiForm.long_summary_token_budget}
                  onChange={(event) => updateUiConfig({ long_summary_token_budget: event.target.value })}
                />
              </LabeledField>
              <LabeledField label="近期摘要 token" hint="保留近期上下文和工作状态，允许范围为 128–32768。" hintId="medium-summary-budget-hint">
                <Input
                  id="medium-summary-token-budget"
                  name="medium_summary_token_budget"
                  aria-label="近期摘要 token"
                  aria-describedby="medium-summary-budget-hint"
                  type="number"
                  min={MIN_SUMMARY_TOKEN_BUDGET}
                  max={MAX_SUMMARY_TOKEN_BUDGET}
                  step={128}
                  value={uiForm.medium_summary_token_budget}
                  onChange={(event) => updateUiConfig({ medium_summary_token_budget: event.target.value })}
                />
              </LabeledField>
            </div>
          </div>
          <UIButton className="self-start" disabled={uiLoading} onClick={() => void saveUiConfig()}>
            <SaveOutlined />
            保存设置
          </UIButton>
          {uiUpdatedAt && <span className="gg-type-meta text-muted-foreground">最后更新：{formatDateOnly(uiUpdatedAt)}</span>}
        </CardContent>
      </Card>
    </>
  );
}

function LabeledField({ label, hint, hintId, children }: { label: string; hint?: string; hintId?: string; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-[6px]">
      <span className="gg-type-meta font-medium text-[#464c5e]">{label}</span>
      {hint && <span id={hintId} className="gg-type-caption  text-muted-foreground">{hint}</span>}
      {children}
    </label>
  );
}

function SwitchRow({ label, checked, onChange }: { label: string; checked: boolean; onChange: (next: boolean) => void }) {
  return (
    <label className="flex items-center justify-between gap-[16px]">
      <span className="gg-type-meta font-medium text-[#464c5e]">{label}</span>
      <Switch checked={checked} onCheckedChange={onChange} />
    </label>
  );
}

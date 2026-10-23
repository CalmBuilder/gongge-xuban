/**
 * @Time       : 2026/10/13
 * @Author     : zhanglp8181
 * @File       : final-f3-browser-driver.ts
 * @CallChain  : F3 参数化页面准备 → 原 Login/Gallery/Composer/Attention → 原请求只读核对
 * @Description: 仅准备原 UI 操作及身份/冻结合同；尚非完整 JRN08，供应商及原账本接入未验。
 */
import { createHash } from 'node:crypto';
import type { Page, Request, Response } from '@playwright/test';
import type { EnterpriseAuthUser } from '../src/auth';
import type { AgentProfileRead } from '../src/types';
import { EnterpriseRoute } from '../src/enums/routes';

export type UiPrincipal = Pick<EnterpriseAuthUser,
  'id' | 'tenant_id' | 'username' | 'role' | 'membership_status'>;
export type UiEmployee = Pick<AgentProfileRead, 'id' | 'tenant_id' | 'name'>;
export type UiAccount = {
  credentials: { username: string; password: string };
  expected: UiPrincipal;
};
export type ComposerIntent = {
  principal: UiPrincipal;
  employee: UiEmployee;
  kind: 'SOP' | 'DTA';
  message: string;
  model_config_id: string;
  client_timezone: string;
};
export type ComposerReceipt = {
  client_turn_id: string;
  conversation_id: string;
  conversation_kind: 'session' | 'draft';
  execution_engine: 'auto' | 'dynamic_task';
  request_sha256: string;
};
export type ApprovalIntent = {
  initiator: UiPrincipal;
  reviewer: UiPrincipal;
  employee: UiEmployee;
  attention_id: string;
};
export type ApprovalReceipt = {
  intent: ApprovalIntent;
  title: string;
  attention_revision: number;
  execution_id: string;
  operation_id: string;
  approval_fingerprint: string;
  frozen_sha256: string;
};
export type OriginalApprovalReader = () => Promise<unknown>;

export class F3UiContractError extends Error {
  /** 固定错误码不带账号凭据、请求正文或供应商内容。 */
  readonly cause?: unknown;

  constructor(code: string, cause?: unknown) {
    super(code);
    this.name = 'F3UiContractError';
    if (cause !== undefined) {
      Object.defineProperty(this, 'cause', {
        value: cause, enumerable: false, configurable: false, writable: false,
      });
      if (cause instanceof Error && typeof this.stack === 'string') {
        const causeStack = typeof cause.stack === 'string' ? cause.stack : `${cause.name}: ${cause.message}`;
        Object.defineProperty(this, 'stack', {
          value: `${this.stack}\nCaused by: ${causeStack}`,
          enumerable: false, configurable: false, writable: false,
        });
      }
    }
  }
}

const MAX_UI_JSON_BODY_CALL_MS = 30_000;

type ComposerAttemptPhase =
  | 'principal-refresh'
  | 'engine-selection'
  | 'message-fill'
  | 'composer-submit'
  | 'sse-finish'
  | 'composer-ready'
  | 'complete';

type ComposerButtonMode = 'send' | 'queue' | 'stop' | 'other' | 'missing';
type MetadataReadEvent =
  | Readonly<{ type: 'request'; tenant_query_match: boolean; filter_query_match: boolean }>
  | Readonly<{ type: 'request_failed' }>
  | Readonly<{ type: 'response'; status: number; elapsed_ms: number }>
  | Readonly<{ type: 'late_response'; status: number; elapsed_ms: number }>
  | Readonly<{ type: 'late_request_failed' }>
  | Readonly<{ type: 'timeout' }>;
type MetadataReadEndpointObservation = Readonly<{
  path_request_count: number;
  tenant_query_match_count: number;
  filter_query_match_count: number;
  matched_request_count: number;
  request_failed_count: number;
  response_count: number;
  last_response_status: number | null;
  last_response_elapsed_ms: number | null;
  late_response_count: number;
  last_late_response_status: number | null;
  last_late_response_elapsed_ms: number | null;
  late_request_failed_count: number;
  timed_out: boolean;
  payload_contract_matched: boolean | null;
}>;
type MutableMetadataReadEndpointObservation = {
  -readonly [Key in keyof MetadataReadEndpointObservation]: MetadataReadEndpointObservation[Key];
};
type MetadataReadinessObservation = Readonly<{
  sessions: MetadataReadEndpointObservation;
  handoffs: MetadataReadEndpointObservation;
}>;
type MutableMetadataReadinessObservation = {
  sessions: MutableMetadataReadEndpointObservation;
  handoffs: MutableMetadataReadEndpointObservation;
};
type ComposerGuardNotice =
  | 'connection-unavailable'
  | 'upload-pending'
  | 'model-config-loading'
  | 'model-config-load-error'
  | 'model-unconfigured'
  | 'handoff-mode-restriction'
  | 'handoff-attachment-restriction'
  | 'handoff-empty-reply'
  | 'session-metadata-loading'
  | 'session-reply-only'
  | 'unbound-agent';

export type ComposerDomObservation = Readonly<{
  composer_form_present_before: boolean;
  input_nonempty_before: boolean;
  submit_button_disabled_before: boolean | null;
  submit_button_mode_before: ComposerButtonMode;
  native_submit_event_count: number;
  submit_button_disabled_at_submit: boolean | null;
  submit_button_mode_at_submit: ComposerButtonMode;
  submit_prevented_after_handler: boolean | null;
  composer_form_present_after: boolean;
  input_nonempty_after: boolean | null;
  submit_button_disabled_after: boolean | null;
  submit_button_mode_after: ComposerButtonMode;
  model_setup_dialog_visible_after: boolean;
  queued_indicator_count_after: number;
  active_chat_route_after: boolean;
  guard_notice_categories: readonly ComposerGuardNotice[];
  handoff_reply_post_count: number;
}>;

type MutableComposerAttempt = {
  phase: ComposerAttemptPhase;
  button_click_started: boolean;
  button_click_completed: boolean;
  matching_post_count: number;
  request_failed: boolean;
  matching_response_count: number;
  response_status: number | null;
  response_elapsed_ms: number | null;
  sse_finished: boolean | null;
  composer_ready: boolean;
  error_category: 'timeout' | 'error' | 'other' | null;
  dom: ComposerDomObservation | null;
  metadata_readiness: MutableMetadataReadinessObservation;
  started_at_ms: number;
};

export type ComposerAttemptFailureObservation = Readonly<{
  phase: ComposerAttemptPhase;
  button_click_started: boolean;
  button_click_completed: boolean;
  matching_post_count: number;
  request_failed: boolean;
  matching_response_count: number;
  response_status: number | null;
  response_elapsed_ms: number | null;
  sse_finished: boolean | null;
  composer_ready: boolean;
  error_category: 'timeout' | 'error' | 'other' | null;
  dom: ComposerDomObservation | null;
  metadata_readiness: MetadataReadinessObservation;
}>;

const composerAttempts = new WeakMap<Page, MutableComposerAttempt>();

function emptyMetadataReadEndpointObservation(): MutableMetadataReadEndpointObservation {
  /** 端点状态仅有计数、HTTP码和身份合同布尔值，不含URL、正文或身份原值。 */
  return {
    path_request_count: 0,
    tenant_query_match_count: 0,
    filter_query_match_count: 0,
    matched_request_count: 0,
    request_failed_count: 0,
    response_count: 0,
    last_response_status: null,
    last_response_elapsed_ms: null,
    late_response_count: 0,
    last_late_response_status: null,
    last_late_response_elapsed_ms: null,
    late_request_failed_count: 0,
    timed_out: false,
    payload_contract_matched: null,
  };
}

function emptyMetadataReadinessObservation(): MutableMetadataReadinessObservation {
  /** 会话与交接列表分别累计，防止一端点的请求掩盖另一端点未就绪。 */
  return {
    sessions: emptyMetadataReadEndpointObservation(),
    handoffs: emptyMetadataReadEndpointObservation(),
  };
}

function recordMetadataReadEvent(page: Page, endpoint: 'sessions' | 'handoffs', event: MetadataReadEvent): void {
  /** 只累计固定事件和布尔匹配，不保留请求对象或元数据正文。 */
  const attempt = composerAttempts.get(page);
  if (!attempt) return;
  const observation = attempt.metadata_readiness[endpoint];
  if (event.type === 'request') {
    observation.path_request_count = Math.min(64, observation.path_request_count + 1);
    if (event.tenant_query_match) {
      observation.tenant_query_match_count = Math.min(64, observation.tenant_query_match_count + 1);
    }
    if (event.filter_query_match) {
      observation.filter_query_match_count = Math.min(64, observation.filter_query_match_count + 1);
    }
    if (event.tenant_query_match && event.filter_query_match) {
      observation.matched_request_count = Math.min(64, observation.matched_request_count + 1);
    }
  } else if (event.type === 'request_failed') {
    observation.request_failed_count = Math.min(64, observation.request_failed_count + 1);
  } else if (event.type === 'response' || event.type === 'late_response') {
    observation.response_count = Math.min(64, observation.response_count + 1);
    observation.last_response_status = Number.isInteger(event.status) ? event.status : null;
    observation.last_response_elapsed_ms = Math.min(90_000, Math.max(0, Math.trunc(event.elapsed_ms)));
    if (event.type === 'late_response') {
      observation.late_response_count = Math.min(64, observation.late_response_count + 1);
      observation.last_late_response_status = Number.isInteger(event.status) ? event.status : null;
      observation.last_late_response_elapsed_ms = Math.min(90_000, Math.max(0, Math.trunc(event.elapsed_ms)));
    }
  } else if (event.type === 'late_request_failed') {
    observation.request_failed_count = Math.min(64, observation.request_failed_count + 1);
    observation.late_request_failed_count = Math.min(64, observation.late_request_failed_count + 1);
  } else {
    observation.timed_out = true;
  }
}

function recordMetadataPayloadContract(page: Page, endpoint: 'sessions' | 'handoffs', matched: boolean): void {
  /** 会话布尔表示本轮四身份绑定，交接布尔表示原响应为列表；不复制行内容。 */
  const attempt = composerAttempts.get(page);
  if (attempt) attempt.metadata_readiness[endpoint].payload_contract_matched = matched;
}

export function safeUiErrorCategory(error: unknown): 'timeout' | 'error' | 'other' {
  /** 只返回固定异常类别，绝不复制浏览器异常正文、URL或请求数据。 */
  let current: unknown = error;
  for (let depth = 0; depth < 4; depth += 1) {
    if (current instanceof F3UiContractError) {
      current = current.cause;
      continue;
    }
    if (!(current instanceof Error)) return 'other';
    if (current.name === 'TimeoutError') return 'timeout';
    if (current.name === 'Error') return 'error';
    return 'other';
  }
  return 'other';
}

export function composerAttemptFailureObservation(page: Page): ComposerAttemptFailureObservation | null {
  /** 只读返回本页最后一次Composer动作的固定阶段、计数、状态码与布尔结果。 */
  const attempt = composerAttempts.get(page);
  if (!attempt) return null;
  const { started_at_ms: _startedAt, ...safe } = attempt;
  return {
    ...safe,
    metadata_readiness: {
      sessions: { ...safe.metadata_readiness.sessions },
      handoffs: { ...safe.metadata_readiness.handoffs },
    },
  };
}

export function observeComposerDomState(value: unknown): ComposerDomObservation {
  /** 只保留白名单DOM状态与原因枚举，不复制正文、提示原文或标识。 */
  const input = record(value, 'F3_UI_COMPOSER_DIAGNOSTIC_INVALID');
  const buttonModes: readonly ComposerButtonMode[] = ['send', 'queue', 'stop', 'other', 'missing'];
  const guardNotices: readonly ComposerGuardNotice[] = [
    'connection-unavailable', 'upload-pending', 'model-config-loading', 'model-config-load-error',
    'model-unconfigured', 'handoff-mode-restriction', 'handoff-attachment-restriction',
    'handoff-empty-reply', 'session-metadata-loading', 'session-reply-only', 'unbound-agent',
  ];
  const safeCount = (candidate: unknown): number => typeof candidate === 'number'
    && Number.isSafeInteger(candidate) && candidate >= 0 ? Math.min(candidate, 64) : 0;
  const safeNullableBoolean = (candidate: unknown): boolean | null =>
    typeof candidate === 'boolean' ? candidate : null;
  const safeMode = (candidate: unknown): ComposerButtonMode =>
    buttonModes.find((mode) => candidate === mode) || 'other';
  const notices = guardNotices.filter((notice) => Array.isArray(input.guard_notice_categories)
    && input.guard_notice_categories.some((candidate: unknown) => candidate === notice));
  return Object.freeze({
    composer_form_present_before: input.composer_form_present_before === true,
    input_nonempty_before: input.input_nonempty_before === true,
    submit_button_disabled_before: safeNullableBoolean(input.submit_button_disabled_before),
    submit_button_mode_before: safeMode(input.submit_button_mode_before),
    native_submit_event_count: safeCount(input.native_submit_event_count),
    submit_button_disabled_at_submit: safeNullableBoolean(input.submit_button_disabled_at_submit),
    submit_button_mode_at_submit: safeMode(input.submit_button_mode_at_submit),
    submit_prevented_after_handler: safeNullableBoolean(input.submit_prevented_after_handler),
    composer_form_present_after: input.composer_form_present_after === true,
    input_nonempty_after: safeNullableBoolean(input.input_nonempty_after),
    submit_button_disabled_after: safeNullableBoolean(input.submit_button_disabled_after),
    submit_button_mode_after: safeMode(input.submit_button_mode_after),
    model_setup_dialog_visible_after: input.model_setup_dialog_visible_after === true,
    queued_indicator_count_after: safeCount(input.queued_indicator_count_after),
    active_chat_route_after: input.active_chat_route_after === true,
    guard_notice_categories: notices,
    handoff_reply_post_count: safeCount(input.handoff_reply_post_count),
  });
}

const principals = new WeakMap<Page, UiPrincipal>();
const jsonBodyBudgets = new WeakMap<Page, CumulativeJsonBodyBudget>();
const baselines = new WeakMap<ApprovalReceipt, {
  frozen: string;
  intent: ApprovalIntent;
  payload: string;
}>();

function requireContract(condition: boolean, code: string): asserts condition {
  /** 不可信响应和参数不满足合同即以固定码拒绝，避免输出私密原文。 */
  if (!condition) throw new F3UiContractError(code);
}

function record(value: unknown, code: string): Record<string, unknown> {
  /** 只接收 JSON 对象，不把数组、null 或隐式字符串当作原响应。 */
  requireContract(typeof value === 'object' && value !== null && !Array.isArray(value), code);
  return value as Record<string, unknown>;
}

function nonblank(value: unknown): value is string {
  /** 身份和原标识必须为真实非空字符串，不生成缺失标识。 */
  return typeof value === 'string' && value.length > 0 && value === value.trim();
}

function sha256(value: string): string {
  /** 对私有比较字节计算本地摘要，不替代后端原能力指纹计算。 */
  return createHash('sha256').update(value, 'utf8').digest('hex');
}

function canonical(value: unknown, code: string): string {
  /** 严格 JSON 比较冻结字段；拒绝循环、非有限数和隐式字符串化。 */
  const seen = new Set<object>();
  function visit(item: unknown, depth: number): unknown {
    /** 保留数组顺序和 JSON 类型，仅排序对象键；不替代原 Python 指纹算法。 */
    requireContract(depth <= 64, code);
    if (item === null || typeof item === 'string' || typeof item === 'boolean') return item;
    if (typeof item === 'number') {
      requireContract(Number.isFinite(item), code);
      return item;
    }
    const input = typeof item === 'object' && item !== null ? item : record(item, code);
    requireContract(!seen.has(input), code);
    seen.add(input);
    try {
      if (Array.isArray(input)) return input.map((entry) => visit(entry, depth + 1));
      const object = record(input, code);
      return Object.fromEntries(Object.keys(object).sort().map((key) => [key, visit(object[key], depth + 1)]));
    } finally { seen.delete(input); }
  }
  return JSON.stringify(visit(value, 0));
}

function principal(value: unknown, code: string): UiPrincipal {
  /** 从原用户投影保留共享身份字段，拒绝空身份及非活动账号。 */
  const user = record(value, code);
  requireContract(nonblank(user.id) && nonblank(user.tenant_id) && nonblank(user.username)
    && (user.role === 'admin' || user.role === 'member') && user.membership_status === 'active', code);
  return Object.freeze({
    id: user.id, tenant_id: user.tenant_id, username: user.username,
    role: user.role, membership_status: user.membership_status,
  });
}

function samePrincipal(value: unknown, expected: UiPrincipal): UiPrincipal {
  /** 实际响应与私有人工标签逐字段相等，绝不把标签当实际登录事实。 */
  const actual = principal(value, 'F3_UI_IDENTITY_MISMATCH');
  const target = principal(expected, 'F3_UI_IDENTITY_MISMATCH');
  requireContract(canonical(actual, 'F3_UI_IDENTITY_MISMATCH')
    === canonical(target, 'F3_UI_IDENTITY_MISMATCH'), 'F3_UI_IDENTITY_MISMATCH');
  return actual;
}

export function observeLoginResponse(value: unknown, expected: UiPrincipal): UiPrincipal {
  /** 从原登录响应核对真实活动身份，不返回 token。 */
  const session = record(value, 'F3_UI_LOGIN_RESPONSE_INVALID');
  requireContract(nonblank(session.token), 'F3_UI_LOGIN_RESPONSE_INVALID');
  return samePrincipal(session.user, expected);
}

export function observeContextResponse(value: unknown, expected: UiPrincipal): UiPrincipal {
  /** 从原企业上下文核对当前账号，不能以预设本人替代实际响应。 */
  const context = record(value, 'F3_UI_IDENTITY_MISMATCH');
  const tenant = record(context.tenant, 'F3_UI_IDENTITY_MISMATCH');
  const actual = samePrincipal(context.member, expected);
  requireContract(tenant.id === actual.tenant_id && context.is_administrator === (actual.role === 'admin'),
    'F3_UI_IDENTITY_MISMATCH');
  return actual;
}

export function assertSeparateReviewer(initiator: UiPrincipal, reviewer: UiPrincipal): void {
  /** 第二活动管理员必须同租户、不同账号，发起人不能自批。 */
  const owner = principal(initiator, 'F3_UI_REVIEWER_FORBIDDEN');
  const actor = principal(reviewer, 'F3_UI_REVIEWER_FORBIDDEN');
  requireContract(owner.id !== actor.id && owner.tenant_id === actor.tenant_id && actor.role === 'admin',
    'F3_UI_REVIEWER_FORBIDDEN');
}

export function observeEmployeeResponse(value: unknown, expected: UiEmployee): UiEmployee {
  /** 以原 use 响应中的员工 id、租户和全名共同排除误选。 */
  const actual = record(value, 'F3_UI_EMPLOYEE_MISMATCH');
  requireContract(nonblank(expected.id) && nonblank(expected.tenant_id) && nonblank(expected.name)
    && actual.id === expected.id && actual.tenant_id === expected.tenant_id && actual.name === expected.name,
  'F3_UI_EMPLOYEE_MISMATCH');
  return Object.freeze({ id: expected.id, tenant_id: expected.tenant_id, name: expected.name });
}

export function observeComposerRequest(value: unknown, intent: ComposerIntent): ComposerReceipt {
  /** 核对原 Composer 请求身份、分支、正文和模型，不创建业务事实。 */
  const code = 'F3_UI_COMPOSER_MISMATCH';
  const body = record(value, code);
  const actor = principal(intent.principal, code);
  requireContract((intent.kind === 'SOP' || intent.kind === 'DTA') && nonblank(intent.employee.id)
    && intent.employee.tenant_id === actor.tenant_id && nonblank(intent.message)
    && nonblank(intent.model_config_id) && nonblank(intent.client_timezone), code);
  const engine = intent.kind === 'SOP' ? 'auto' : 'dynamic_task';
  const session = nonblank(body.session_id);
  const draft = nonblank(body.draft_conversation_id);
  requireContract(body.tenant_id === actor.tenant_id && body.user_id === actor.id
    && body.agent_id === intent.employee.id && body.message === intent.message
    && body.execution_engine === engine && body.interaction_mode === 'normal'
    && body.model_config_id === intent.model_config_id && body.client_timezone === intent.client_timezone
    && body.channel === 'web' && Array.isArray(body.attachments) && body.attachments.length === 0
    && nonblank(body.client_turn_id) && session !== draft
    && !('forced_general_skill_id' in body) && !('forced_general_skill_ids' in body)
    && !('ability_ids' in body), code);
  const conversation = session ? body.session_id : body.draft_conversation_id;
  requireContract(nonblank(conversation), code);
  return Object.freeze({
    client_turn_id: body.client_turn_id, conversation_id: conversation,
    conversation_kind: session ? 'session' : 'draft', execution_engine: engine,
    request_sha256: sha256(canonical(body, code)),
  });
}

function approvalProjection(value: unknown, intent: ApprovalIntent): {
  instance: Record<string, unknown>; attention: Record<string, unknown>;
  operation: Record<string, unknown>; payload: Record<string, unknown>; frozen: string;
} {
  /** 仅解析原行的私有观察投影，并交叉关联已有实例与冻结请求，不写运行状态。 */
  const code = 'F3_UI_APPROVAL_MISMATCH';
  assertSeparateReviewer(intent.initiator, intent.reviewer);
  const input = record(value, code);
  const instance = record(input.instance, code);
  const attention = record(input.attention, code);
  const operation = record(input.operation, code);
  const payload = record(attention.payload, code);
  requireContract(instance.kind === 'dynamic_task' && nonblank(instance.id) && nonblank(instance.session_id)
    && nonblank(instance.current_plan_revision_id) && instance.tenant_id === intent.initiator.tenant_id
    && instance.agent_id === intent.employee.id && instance.tenant_id === intent.employee.tenant_id
    && instance.initiator_user_id === intent.initiator.id && attention.id === intent.attention_id
    && nonblank(intent.attention_id) && attention.execution_id === instance.id
    && attention.session_id === instance.session_id && attention.kind === 'tool_approval'
    && attention.source_type === 'dynamic_task' && attention.source_ref === operation.id
    && nonblank(attention.title) && typeof attention.revision === 'number'
    && Number.isSafeInteger(attention.revision) && attention.revision >= 0
    && nonblank(operation.id) && operation.tenant_id === instance.tenant_id
    && operation.instance_id === instance.id && operation.effect_kind === 'external_write'
    && nonblank(operation.operation_name) && payload.operation_id === operation.id
    && payload.operation_name === operation.operation_name && payload.execution_id === instance.id
    && payload.plan_revision_id === instance.current_plan_revision_id, code);
  for (const key of ['request_fingerprint', 'capability_checksum']) {
    requireContract(typeof operation[key] === 'string' && /^[a-f0-9]{64}$/.test(operation[key])
      && payload[key] === operation[key], code);
  }
  requireContract(typeof payload.approval_fingerprint === 'string'
    && /^[a-f0-9]{64}$/.test(payload.approval_fingerprint)
    && canonical(record(payload.arguments, code), code) === canonical(record(operation.request_json, code), code), code);
  const frozen = canonical({
    instance: {
      id: instance.id, tenant_id: instance.tenant_id, agent_id: instance.agent_id,
      initiator_user_id: instance.initiator_user_id, session_id: instance.session_id,
      current_plan_revision_id: instance.current_plan_revision_id,
    },
    attention: {
      id: attention.id, execution_id: attention.execution_id, session_id: attention.session_id,
      kind: attention.kind, title: attention.title, source_type: attention.source_type,
      source_ref: attention.source_ref, payload,
    },
    operation: {
      id: operation.id, tenant_id: operation.tenant_id, instance_id: operation.instance_id,
      operation_name: operation.operation_name, effect_kind: operation.effect_kind,
      request_json: operation.request_json, request_fingerprint: operation.request_fingerprint,
      capability_checksum: operation.capability_checksum,
    },
  }, code);
  return { instance, attention, operation, payload, frozen };
}

export function observeApprovalBefore(value: unknown, intent: ApprovalIntent): ApprovalReceipt {
  /** 只读关联原实例、Attention 与 Operation，不授权、不制造批准或结果。 */
  const code = 'F3_UI_APPROVAL_MISMATCH';
  const result = approvalProjection(value, intent);
  const { instance, attention, operation, payload } = result;
  requireContract((attention.status === 'offered' || attention.status === 'claimed')
    && Array.isArray(attention.available_commands) && attention.available_commands.includes('allow_once')
    && operation.status === 'prepared'
    && (operation.approval_work_item_id === null || operation.approval_work_item_id === undefined
      || operation.approval_work_item_id === attention.id), code);
  requireContract(nonblank(instance.id) && nonblank(operation.id) && nonblank(attention.title)
    && typeof attention.revision === 'number' && nonblank(payload.approval_fingerprint), code);
  const copiedIntent: ApprovalIntent = {
    initiator: principal(intent.initiator, code), reviewer: principal(intent.reviewer, code),
    employee: observeEmployeeResponse(intent.employee, intent.employee), attention_id: intent.attention_id,
  };
  Object.freeze(copiedIntent);
  const receipt = Object.freeze({
    intent: copiedIntent, title: attention.title, attention_revision: attention.revision,
    execution_id: instance.id, operation_id: operation.id,
    approval_fingerprint: payload.approval_fingerprint, frozen_sha256: sha256(result.frozen),
  });
  baselines.set(receipt, {
    frozen: result.frozen, intent: copiedIntent, payload: canonical(payload, code),
  });
  return receipt;
}

export function observeResolveRequest(value: unknown, before: ApprovalReceipt): string {
  /** 精确核对原 allow_once CAS 信封，指纹保留于原冻结载荷而非新请求字段。 */
  const code = 'F3_UI_RESOLVE_MISMATCH';
  const baseline = baselines.get(before);
  requireContract(baseline !== undefined, code);
  const body = record(value, code);
  requireContract(Object.keys(body).sort().join(',') === 'command,command_id,expected_revision,tenant_id'
    && body.tenant_id === baseline.intent.reviewer.tenant_id && body.command === 'allow_once'
    && nonblank(body.command_id) && body.expected_revision === before.attention_revision, code);
  return sha256(canonical(body, code));
}

export function observeApprovalAfter(value: unknown, before: ApprovalReceipt): void {
  /** 批准后只读复核原 actor 与冻结参数，不能以页面成功提示证明派发成功。 */
  const code = 'F3_UI_APPROVAL_MISMATCH';
  const baseline = baselines.get(before);
  requireContract(baseline !== undefined, code);
  const { attention, operation, frozen } = approvalProjection(value, baseline.intent);
  const resolution = record(attention.resolution, code);
  requireContract(frozen === baseline.frozen && attention.status === 'completed'
    && attention.revision === before.attention_revision + 1
    && resolution.command === 'allow_once' && resolution.actor_user_id === baseline.intent.reviewer.id
    && resolution.revision === attention.revision && operation.approval_work_item_id === baseline.intent.attention_id
    && operation.approval_fingerprint === before.approval_fingerprint
    && operation.approved_by_user_id === baseline.intent.reviewer.id, code);
}

export function safeUiReceipt(receipt: ComposerReceipt | ApprovalReceipt): Record<string, unknown> {
  /** 对外仅输出固定码、摘要及布尔，明确未验完整 JRN08 和真实供应商。 */
  const digest = 'frozen_sha256' in receipt ? receipt.frozen_sha256 : receipt.request_sha256;
  requireContract(/^[a-f0-9]{64}$/.test(digest), 'F3_UI_RECEIPT_INVALID');
  return {
    code: 'F3_UI_CONTRACT_OBSERVED', receipt_sha256: digest,
    JRN08_passed: false, real_vendor_qualified: false,
  };
}

export async function closePageBeforeNextJourney(page: Page): Promise<void> {
  /** 先关归档页面和其fixture context，再给Chromium跨用例释放renderer排空。 */
  if (!page.isClosed()) await page.close();
  const context = page.context();
  if (!context.isClosed()) await context.close();
  await new Promise<void>((resolve) => setTimeout(resolve, 1_500));
}

function originalRequest(request: Request, page: Page, path: string, method: string): boolean {
  /** 只观察同源原 API 请求，不使用伪造 route、额外 fetch 或认证注入。 */
  const url = new URL(request.url());
  return request.method() === method && url.origin === new URL(page.url()).origin && url.pathname === path;
}

export function sessionMetadataContains(
  value: unknown, expected: { session_id: string; principal: UiPrincipal; employee: UiEmployee },
): boolean {
  /** 原会话列表必须同时匹配本轮会话、租户、用户与员工，防止只等到任意列表响应。 */
  if (!Array.isArray(value)) return false;
  return value.some((candidate: unknown) => {
    if (typeof candidate !== 'object' || candidate === null || Array.isArray(candidate)) return false;
    const row = candidate as Record<string, unknown>;
    return row.id === expected.session_id && row.tenant_id === expected.principal.tenant_id
      && row.user_id === expected.principal.id && row.agent_id === expected.employee.id
      && expected.employee.tenant_id === expected.principal.tenant_id;
  });
}

export function waitForNextOriginalResponse(
  page: Page, path: string, tenantId: string, pendingHandoffs = false,
  observe?: (event: MetadataReadEvent) => void,
  timing: Readonly<{ timeout_ms?: number; late_grace_ms?: number }> = {},
): Promise<Response> {
  /** 成对监听原租户请求；超时后短暂记录晚到状态，但仍按原超时失败。 */
  return new Promise<Response>((resolve, reject) => {
    let matchedUrl: string | null = null;
    let settled = false;
    let timedOut = false;
    const startedAt = Date.now();
    const emit = (event: MetadataReadEvent) => {
      try { observe?.(event); } catch { /* 脱敏诊断失败不能改变原UI等待语义。 */ }
    };
    const cleanup = () => {
      clearTimeout(timer);
      clearTimeout(graceTimer);
      page.off('request', onRequest);
      page.off('response', onResponse);
      page.off('requestfailed', onRequestFailed);
    };
    const finish = (action: () => void) => {
      if (settled) return;
      settled = true;
      cleanup();
      action();
    };
    const onRequest = (candidate: Request) => {
      if (matchedUrl !== null || !originalRequest(candidate, page, path, 'GET')) return;
      try {
        const candidateUrl = candidate.url();
        const url = new URL(candidateUrl);
        const tenantQueryMatch = url.searchParams.get('tenant_id') === tenantId;
        const filterQueryMatch = !pendingHandoffs || url.searchParams.get('status') === 'pending';
        emit({ type: 'request', tenant_query_match: tenantQueryMatch, filter_query_match: filterQueryMatch });
        if (!tenantQueryMatch || !filterQueryMatch) return;
        matchedUrl = candidateUrl;
      } catch { /* 无法解析的请求不成为本轮读取证据。 */ }
    };
    const onResponse = (response: Response) => {
      const responseRequest = response.request();
      if (matchedUrl !== null && responseRequest.method() === 'GET'
          && responseRequest.url() === matchedUrl) {
        const elapsed = Math.min(90_000, Math.max(0, Date.now() - startedAt));
        if (timedOut) {
          emit({ type: 'late_response', status: response.status(), elapsed_ms: elapsed });
          finish(() => reject(new Error('original metadata response timeout')));
          return;
        }
        emit({
          type: 'response', status: response.status(),
          elapsed_ms: elapsed,
        });
        finish(() => resolve(response));
      }
    };
    const onRequestFailed = (request: Request) => {
      if (matchedUrl !== null && request.method() === 'GET' && request.url() === matchedUrl) {
        emit({ type: timedOut ? 'late_request_failed' : 'request_failed' });
        finish(() => reject(new Error('original metadata request failed')));
      }
    };
    const timeoutMs = Math.max(1, Math.min(90_000, Math.trunc(timing.timeout_ms ?? 45_000)));
    const lateGraceMs = Math.max(0, Math.min(30_000, Math.trunc(timing.late_grace_ms ?? 15_000)));
    let graceTimer: ReturnType<typeof setTimeout> | undefined;
    const timer = setTimeout(
      () => {
        timedOut = true;
        emit({ type: 'timeout' });
        if (matchedUrl === null || lateGraceMs === 0) {
          finish(() => reject(new Error('original metadata response timeout')));
          return;
        }
        graceTimer = setTimeout(
          () => finish(() => reject(new Error('original metadata response timeout'))), lateGraceMs,
        );
      }, timeoutMs,
    );
    page.on('request', onRequest);
    page.on('response', onResponse);
    page.on('requestfailed', onRequestFailed);
  });
}

export function boundedUiOperation<T>(operation: Promise<T>, timeoutMs: number, code: string): Promise<T> {
  /** 为Playwright未提供单次调用截止的异步读取设置Node侧硬截止并返回固定错误码。 */
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 1 || !/^[A-Z0-9_]+$/u.test(code)) {
    throw new F3UiContractError('F3_UI_DEADLINE_INVALID');
  }
  let timer: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<never>((_resolve, reject) => {
    timer = setTimeout(() => reject(new F3UiContractError(code)), timeoutMs);
  });
  return Promise.race([operation, deadline]).finally(() => {
    if (timer !== undefined) clearTimeout(timer);
  });
}

export function remainingDeadlineMs(deadlineAtMs: number, nowMs = performance.now()): number {
  /** 单调时钟下计算有限阶段的剩余毫秒，过期时只返回零。 */
  if (!Number.isFinite(deadlineAtMs) || !Number.isFinite(nowMs)) return 0;
  return Math.max(0, Math.floor(deadlineAtMs - nowMs));
}

export function sessionMetadataReadWindow(deadlineAtMs: number, nowMs = performance.now()): {
  timeout_ms: number; late_grace_ms: number; remaining_ms: number;
} {
  /** 响应头等待受总期限约束，单请求最多45秒；JSON正文另受30秒硬截止。 */
  const remaining = remainingDeadlineMs(deadlineAtMs, nowMs);
  const timeout = Math.min(45_000, remaining);
  return { timeout_ms: timeout,
    late_grace_ms: Math.min(15_000, Math.max(0, remaining - timeout)), remaining_ms: remaining };
}

async function waitForCurrentSessionMetadata(
  page: Page, intent: ComposerIntent, sessionId: string,
): Promise<void> {
  /** 等原session与pending-handoff响应；错误列表最多再读至180秒硬截止。 */
  const deadlineAt = performance.now() + 180_000;
  const waitForSession = async (): Promise<void> => {
    while (remainingDeadlineMs(deadlineAt) > 0) {
      const window = sessionMetadataReadWindow(deadlineAt);
      const response = await waitForNextOriginalResponse(
        page, '/api/chat/sessions', intent.principal.tenant_id, false,
        (event) => recordMetadataReadEvent(page, 'sessions', event),
        { timeout_ms: window.timeout_ms, late_grace_ms: window.late_grace_ms },
      );
      const body = await boundedResponseBody(response, 'F3_UI_SESSION_METADATA_INVALID', deadlineAt, page);
      const matched = sessionMetadataContains(body, {
        session_id: sessionId, principal: intent.principal, employee: intent.employee,
      });
      recordMetadataPayloadContract(page, 'sessions', matched);
      if (matched) return;
    }
    throw new F3UiContractError('F3_UI_SESSION_METADATA_DEADLINE');
  };
  const sessionReady = waitForSession();
  const handoffDeadlineAt = performance.now() + 60_000;
  const handoffWindow = sessionMetadataReadWindow(handoffDeadlineAt);
  const handoffReady = waitForNextOriginalResponse(
    page, '/api/chat/handoffs', intent.principal.tenant_id, true,
    (event) => recordMetadataReadEvent(page, 'handoffs', event),
    { timeout_ms: handoffWindow.timeout_ms, late_grace_ms: handoffWindow.late_grace_ms },
  ).then(async (response) => {
    const body = await boundedResponseBody(response, 'F3_UI_HANDOFF_METADATA_INVALID', handoffDeadlineAt, page);
    const matched = Array.isArray(body);
    recordMetadataPayloadContract(page, 'handoffs', matched);
    requireContract(matched, 'F3_UI_HANDOFF_METADATA_INVALID');
  });
  await Promise.all([sessionReady, handoffReady]);
  await boundedUiOperation(page.evaluate(() => new Promise<void>((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
  })), 5_000, 'F3_UI_SESSION_FRAME_DEADLINE');
}

async function responseBody(page: Page, response: Response, code: string): Promise<unknown> {
  /** 原响应必须为 HTTP 200 JSON，异常正文保持私有且不用于成功判断。 */
  requireContract(response.status() === 200, code);
  return boundedPageJsonBody(page, response, code);
}

export class BoundedRequestCountBudget {
  readonly #maximumReads: number;
  readonly #errorCode: string;
  #reads = 0;

  constructor(maximumReads: number, errorCode: string) {
    /** 私有API观察只接受有限整数上限，不能借UI正文预算之外无限轮询。 */
    if (!Number.isSafeInteger(maximumReads) || maximumReads <= 0
      || !/^F3_[A-Z0-9_]+$/.test(errorCode)) {
      throw new F3UiContractError('F3_PRIVATE_API_READ_BUDGET_INVALID');
    }
    this.#maximumReads = maximumReads;
    this.#errorCode = errorCode;
  }

  get reads(): number {
    /** 返回已预留的私有读取数，失败读取也不退还名额。 */
    return this.#reads;
  }

  reserve(): number {
    /** 在发出APIRequestContext请求前先占一个名额，拒绝上限外派发。 */
    if (this.#reads >= this.#maximumReads) throw new F3UiContractError(this.#errorCode);
    this.#reads += 1;
    return this.#reads;
  }

  async dispatch<T>(operation: () => Promise<T>): Promise<T> {
    /** 先原子预留读取额度，再调用私有请求；上限错误不会触碰网络。 */
    this.reserve();
    return operation();
  }
}

export class CumulativeJsonBodyBudget {
  readonly #maximumMs: number;
  readonly #maximumReads: number;
  #remainingMs: number;
  #reads = 0;

  constructor(maximumMs: number, maximumReads = Number.MAX_SAFE_INTEGER) {
    /** 每轮只接受正的有限毫秒预算，避免无穷或负数解除累计截止。 */
    if (!Number.isFinite(maximumMs) || maximumMs <= 0
      || !Number.isSafeInteger(maximumReads) || maximumReads <= 0) {
      throw new F3UiContractError('F3_UI_RESPONSE_BODY_BUDGET_INVALID');
    }
    this.#maximumMs = maximumMs;
    this.#maximumReads = maximumReads;
    this.#remainingMs = maximumMs;
  }

  get remainingMs(): number {
    /** 调用方只能观察剩余预算，不能直接延长该轮预算。 */
    return this.#remainingMs;
  }

  get reads(): number {
    /** 只读本轮已启动的正文读取数，失败或超时的读取同样计入合同。 */
    return this.#reads;
  }

  async read<T>(operation: () => Promise<T>, maximumCallMs: number): Promise<T> {
    /** 并发读取先预留单次上限，结束后只返还未消耗时长。 */
    if (this.#reads >= this.#maximumReads) {
      throw new F3UiContractError('F3_UI_RESPONSE_BODY_DEADLINE');
    }
    const allowance = Math.floor(Math.min(maximumCallMs, this.#remainingMs));
    if (!Number.isFinite(maximumCallMs) || maximumCallMs <= 0 || allowance < 1) {
      throw new F3UiContractError('F3_UI_RESPONSE_BODY_DEADLINE');
    }
    this.#reads += 1;
    this.#remainingMs -= allowance;
    const startedAt = performance.now();
    try {
      return await boundedUiOperation(operation(), allowance, 'F3_UI_RESPONSE_BODY_DEADLINE');
    } finally {
      const elapsed = Math.min(allowance, Math.max(0, performance.now() - startedAt));
      this.#remainingMs = Math.min(this.#maximumMs, this.#remainingMs + allowance - elapsed);
    }
  }
}

export function beginPageJsonBodyBudget(page: Page, maximumMs: number, maximumReads?: number): void {
  /** 为指定页面安装单轮累计正文读取预算，不影响其他页面或APIRequestContext。 */
  jsonBodyBudgets.set(page, new CumulativeJsonBodyBudget(maximumMs, maximumReads));
}

export async function boundedJsonBody(
  response: Pick<Response, 'json'>, code: string, timeoutMs = 30_000,
  budget?: CumulativeJsonBodyBudget, onTimeout?: () => Promise<void>,
): Promise<unknown> {
  /** 已收到响应头后的JSON正文仍设固定硬截止，避免响应体卡住完成浏览器分支。 */
  const callTimeoutMs = Math.min(timeoutMs, MAX_UI_JSON_BODY_CALL_MS);
  let readStarted = false;
  try {
    const read = (): Promise<unknown> => { readStarted = true; return response.json(); };
    const value: unknown = budget
      ? await budget.read(read, callTimeoutMs)
      : await boundedUiOperation(read(), callTimeoutMs, 'F3_UI_RESPONSE_BODY_DEADLINE');
    return value;
  } catch (error) {
    if (error instanceof F3UiContractError && error.message === 'F3_UI_RESPONSE_BODY_DEADLINE') {
      if (readStarted && onTimeout) {
        try {
          await onTimeout();
        } catch (cancelError) {
          throw new F3UiContractError('F3_UI_RESPONSE_CANCEL_FAILED', cancelError);
        }
      }
      throw error;
    }
    throw new F3UiContractError(code, error);
  }
}

export async function boundedPageJsonBody(
  page: Page, response: Pick<Response, 'json'>, code: string, timeoutMs = 30_000,
): Promise<unknown> {
  /** 页面JSON正文使用该页已安装的累计预算；普通页面仍受单次固定截止约束。 */
  return boundedJsonBody(response, code, timeoutMs, jsonBodyBudgets.get(page), async () => {
    if (!page.isClosed()) await page.close({ runBeforeUnload: false });
  });
}

export async function boundedResponseBody(
  response: Response, code: string, deadlineAtMs: number, page?: Page,
): Promise<unknown> {
  /** 已收到响应头后的JSON正文仍受共享单调期限约束，不能越过metadata阶段截止。 */
  const remaining = remainingDeadlineMs(deadlineAtMs);
  if (remaining < 1) throw new F3UiContractError('F3_UI_RESPONSE_BODY_DEADLINE');
  return page ? boundedPageJsonBody(page, response, code, remaining)
    : boundedJsonBody(response, code, remaining);
}

async function safely<T>(action: () => Promise<T>): Promise<T> {
  /** UI 失败只暴露固定码；浏览器原记录由调用方另行私有保存。 */
  try { return await action(); }
  catch (error) {
    if (error instanceof F3UiContractError) throw error;
    throw new F3UiContractError('F3_UI_OPERATION_FAILED', error);
  }
}

function boundPrincipal(page: Page): UiPrincipal {
  /** 页面必须经本辅助层实际原登录，不能把调用方提供身份当页面事实。 */
  const identity = principals.get(page);
  requireContract(identity !== undefined, 'F3_UI_LOGIN_REQUIRED');
  return identity;
}

async function refreshPrincipalViaUi(
  page: Page, destination?: string, composerReadiness?: ComposerIntent,
): Promise<UiPrincipal> {
  /** 原页面导航或重载触发原 auth/context，请求后复核撤权与身份切换。 */
  const expected = boundPrincipal(page);
  let sessionId: string | null = null;
  if (composerReadiness) {
    const route = new URL(page.url());
    const match = route.pathname.match(/^\/workspace\/chat\/([^/]+)$/);
    if (match) {
      try { sessionId = decodeURIComponent(match[1] || ''); }
      catch { throw new F3UiContractError('F3_UI_SESSION_METADATA_INVALID'); }
    }
  }
  const metadataReady = composerReadiness && sessionId && !sessionId.startsWith('draft:')
    ? waitForCurrentSessionMetadata(page, composerReadiness, sessionId) : null;
  const pending = page.waitForResponse((response) => originalRequest(response.request(), page, '/api/auth/context', 'GET'));
  const [response] = await Promise.all([pending, destination ? page.goto(destination) : page.reload()]);
  const identity = observeContextResponse(await responseBody(page, response, 'F3_UI_IDENTITY_MISMATCH'), expected);
  principals.set(page, identity);
  if (metadataReady) await metadataReady;
  return identity;
}

export async function loginViaUi(page: Page, account: UiAccount): Promise<UiPrincipal> {
  /** 填写真实账号原登录 UI 并核原响应，不注入认证或引导状态。 */
  return safely(async () => {
    requireContract(nonblank(account.credentials.username) && nonblank(account.credentials.password)
      && account.credentials.username === account.expected.username, 'F3_UI_ACCOUNT_INVALID');
    principal(account.expected, 'F3_UI_ACCOUNT_INVALID');
    await page.goto('/');
    const entry = page.getByRole('button', { name: /^进入平台(?:\s+→)?$/ });
    if (await entry.isVisible()) await entry.click();
    await page.getByRole('textbox', { name: '账号', exact: true }).fill(account.credentials.username);
    await page.getByLabel('密码', { exact: true }).fill(account.credentials.password);
    const pendingLogin = page.waitForResponse((response) => originalRequest(response.request(), page, '/api/auth/login', 'POST'));
    const pendingContext = page.waitForResponse((response) => originalRequest(response.request(), page, '/api/auth/context', 'GET'));
    const [login, context] = await Promise.all([
      pendingLogin, pendingContext, page.getByRole('button', { name: '登录', exact: true }).click(),
    ]);
    const actual = observeLoginResponse(await responseBody(page, login, 'F3_UI_LOGIN_RESPONSE_INVALID'), account.expected);
    const confirmed = observeContextResponse(await responseBody(page, context, 'F3_UI_IDENTITY_MISMATCH'), actual);
    const guide = page.getByRole('dialog', { name: '让企业经验进入工作', exact: true });
    await guide.waitFor({ state: 'visible', timeout: 5000 }).catch(() => undefined);
    const quickStart = page.getByRole('button', { name: '关闭快速入门', exact: true });
    if (await guide.isVisible()) {
      await guide.getByRole('button', { name: '关闭引导', exact: true }).click();
      await quickStart.waitFor({ state: 'visible', timeout: 30000 });
    } else {
      await quickStart.waitFor({ state: 'visible', timeout: 5000 }).catch(() => undefined);
    }
    if (await quickStart.isVisible()) await quickStart.click();
    principals.set(page, confirmed);
    return confirmed;
  });
}

interface EmployeeGallerySearchPage {
  getByPlaceholder(name: string, options: { exact: true }): { fill(value: string): Promise<void> };
}

export function observeRemoteCleanupResponse(status: number, value: unknown): void {
  /** 只核原独立服务清理回执，后续仍须实际GET读回对象不存在。 */
  requireContract(status === 200, 'F3_REMOTE_CLEANUP_INVALID');
  const response = record(value, 'F3_REMOTE_CLEANUP_INVALID');
  requireContract(response.absent === true && response.real_vendor_qualified === false,
    'F3_REMOTE_CLEANUP_INVALID');
}

export function sdkLedgerIsQuiescent(value: unknown): boolean {
  /** 核原进程SDK是否已结算；零调用进程不能充当真实模型质量证明。 */
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false;
  const health = value as Record<string, unknown>;
  if (health.valid === true) {
    return typeof health.started_count === 'number' && Number.isSafeInteger(health.started_count)
      && health.started_count >= 0 && health.started_count === health.settled_count
      && health.in_flight_count === 0;
  }
  const counters = ['client_state_count', 'started_count', 'settled_count',
    'in_flight_count', 'observation_error_count', 'receipt_count'];
  return health.valid === false && counters.every((key) => health[key] === 0)
    && health.file_matches === true && health.write_failed === false;
}

export async function filterEmployeeGalleryViaUi(page: EmployeeGallerySearchPage, employee: UiEmployee): Promise<void> {
  /** 从原广场搜索框定位指定员工，避免新增验收员工把目标挤出第一页。 */
  requireContract(nonblank(employee.name), 'F3_UI_EMPLOYEE_MISMATCH');
  await page.getByPlaceholder('搜索数字员工…', { exact: true }).fill(employee.name);
}

export async function openEmployeeViaUi(page: Page, employee: UiEmployee, galleryLabel: string): Promise<UiEmployee> {
  /** 通过原员工广场精确卡片发起对话，并读取原 use 响应。 */
  return safely(async () => {
    requireContract(nonblank(galleryLabel), 'F3_UI_EMPLOYEE_MISMATCH');
    const actor = await refreshPrincipalViaUi(page, EnterpriseRoute.Gallery);
    requireContract(actor.tenant_id === employee.tenant_id, 'F3_UI_EMPLOYEE_MISMATCH');
    await filterEmployeeGalleryViaUi(page, employee);
    const label = page.getByText(galleryLabel, { exact: true });
    await label.waitFor({ state: 'visible' });
    // The gallery card is the stable employee boundary. A broad role=button
    // locator also sees nested actions and can report a false ambiguity.
    const card = page.locator('.gongge-employee-card').filter({ has: label });
    requireContract(await card.count() === 1, 'F3_UI_EMPLOYEE_AMBIGUOUS');
    const path = `/api/chat/agents/${encodeURIComponent(employee.id)}/use`;
    const pending = page.waitForResponse((response) => originalRequest(response.request(), page, path, 'POST'));
    const [response] = await Promise.all([
      pending, card.getByRole('button', { name: '发起对话', exact: true }).click(),
    ]);
    const url = new URL(response.url());
    const request: unknown = response.request().postDataJSON();
    requireContract(url.searchParams.get('tenant_id') === actor.tenant_id
      && Object.keys(record(request, 'F3_UI_EMPLOYEE_MISMATCH')).length === 0, 'F3_UI_EMPLOYEE_MISMATCH');
    const actual = observeEmployeeResponse(await responseBody(page, response, 'F3_UI_EMPLOYEE_MISMATCH'), employee);
    await page.getByPlaceholder('输入消息，按 Enter 发送...').waitFor({ state: 'visible' });
    return actual;
  });
}

export async function sendComposerViaUi(page: Page, intent: ComposerIntent): Promise<ComposerReceipt> {
  /** 原Composer提交后等待SSE结束；SOP外部任务等待态仍会保持session运行提示，应由持久账本判定。 */
  const attempt: MutableComposerAttempt = {
    phase: 'principal-refresh', button_click_started: false, button_click_completed: false,
    matching_post_count: 0, request_failed: false, matching_response_count: 0,
    response_status: null, response_elapsed_ms: null, sse_finished: null,
    composer_ready: false, error_category: null, dom: null,
    metadata_readiness: emptyMetadataReadinessObservation(), started_at_ms: Date.now(),
  };
  composerAttempts.set(page, attempt);
  let handoffReplyPostCount = 0;
  const matchesStreamRequest = (request: Request) =>
    originalRequest(request, page, '/api/chat/stream', 'POST');
  const onRequest = (request: Request) => {
    if (matchesStreamRequest(request)) attempt.matching_post_count += 1;
    if (request.method() === 'POST') {
      try {
        const path = new URL(request.url()).pathname;
        if (/^\/api\/chat\/handoffs\/[^/]+\/reply$/.test(path)) handoffReplyPostCount += 1;
      } catch { /* 仅计数已匹配的原UI请求，不保留异常URL。 */ }
    }
  };
  const onRequestFailed = (request: Request) => {
    if (matchesStreamRequest(request)) attempt.request_failed = true;
  };
  const onResponse = (response: Response) => {
    if (!originalRequest(response.request(), page, '/api/chat/stream', 'POST')) return;
    attempt.matching_response_count += 1;
    attempt.response_status = response.status();
    attempt.response_elapsed_ms = Math.max(0, Math.min(Date.now() - attempt.started_at_ms, 900_000));
  };
  page.on('request', onRequest);
  page.on('requestfailed', onRequestFailed);
  page.on('response', onResponse);
  try {
    return await safely(async () => {
      attempt.phase = 'principal-refresh';
      const actual = await refreshPrincipalViaUi(page, undefined, intent);
      samePrincipal(actual, intent.principal);
      attempt.phase = 'engine-selection';
      const engine = page.getByRole('button', { name: '选择 DynamicTaskAgent 复杂任务引擎', exact: true });
      await engine.waitFor({ state: 'visible' });
      const wanted = intent.kind === 'DTA' ? 'true' : 'false';
      if (await engine.getAttribute('aria-pressed') !== wanted) await engine.click();
      requireContract(await engine.getAttribute('aria-pressed') === wanted, 'F3_UI_COMPOSER_MISMATCH');
      attempt.phase = 'message-fill';
      await page.getByPlaceholder('输入消息，按 Enter 发送...').fill(intent.message);
      const beforeClick = await boundedUiOperation(page.evaluate(() => {
        const placeholder = '输入消息，按 Enter 发送...';
        const textarea = document.querySelector<HTMLTextAreaElement>(`textarea[placeholder="${placeholder}"]`);
        const form = textarea?.closest('form') || null;
        const button = form?.querySelector<HTMLButtonElement>('button[type="submit"]') || null;
        const mode = (element: HTMLButtonElement | null): ComposerButtonMode => {
          if (!element) return 'missing';
          const label = element.getAttribute('aria-label');
          if (label === '发送') return 'send';
          if (label === '加入发送队列') return 'queue';
          if (label === '停止生成') return 'stop';
          return 'other';
        };
        const probe = {
          submit_count: 0,
          submit_prevented: null as boolean | null,
          submit_button_disabled: null as boolean | null,
          submit_button_mode: 'missing' as ComposerButtonMode,
          guard_notices: [] as ComposerGuardNotice[],
          form,
          capture_listener: null as ((event: Event) => void) | null,
          bubble_listener: null as ((event: Event) => void) | null,
          notice_observer: null as MutationObserver | null,
          scan_guard_notices: null as (() => void) | null,
        };
        probe.capture_listener = (event: Event) => {
          if (event.target !== probe.form) return;
          probe.submit_count = Math.min(probe.submit_count + 1, 64);
          const submitter = event instanceof SubmitEvent && event.submitter instanceof HTMLButtonElement
            ? event.submitter : button;
          probe.submit_button_disabled = submitter?.disabled ?? null;
          probe.submit_button_mode = mode(submitter);
        };
        probe.bubble_listener = (event: Event) => {
          if (event.target === probe.form) probe.submit_prevented = event.defaultPrevented;
        };
        const messages: Array<[ComposerGuardNotice, string[]]> = [
          ['connection-unavailable', ['当前服务连接失败，请恢复服务后再发送', 'The chat service is unavailable']],
          ['upload-pending', ['文件还在解析中，请稍后发送', 'Files are still being processed']],
          ['model-config-loading', ['模型配置正在加载，请稍后再发送', 'Model configurations are still loading']],
          ['model-config-load-error', ['无法读取模型配置，请刷新页面后重试', 'Unable to load model configurations']],
          ['model-unconfigured', ['系统管理员尚未配置可用模型，请联系管理员完成模型配置', 'No model is available']],
          ['handoff-mode-restriction', ['待回答会话仅支持发送人工回复', 'Pending handoff sessions only accept human replies']],
          ['handoff-attachment-restriction', ['待回答暂不支持附件，请发送文字回复', 'Attachments are not supported for pending handoffs']],
          ['handoff-empty-reply', ['请输入回复内容', 'Enter a reply']],
          ['session-metadata-loading', ['任务信息还在加载，请稍后再发送', 'Task information is still loading']],
          ['session-reply-only', ['当前账号不能直接向该会话发送消息，请从待回答列表回复', 'Reply from the pending handoff list']],
          ['unbound-agent', ['该任务没有绑定数字员工，请新建任务后再发送', 'This task has no assigned digital employee']],
        ];
        probe.scan_guard_notices = () => {
          const toastText = Array.from(document.querySelectorAll<HTMLElement>('[data-sonner-toast]'))
            .map(element => element.textContent || '').join('\n');
          for (const [notice, labels] of messages) {
            if (labels.some(label => toastText.includes(label)) && !probe.guard_notices.includes(notice)) {
              probe.guard_notices.push(notice);
            }
          }
        };
        probe.notice_observer = new MutationObserver(() => probe.scan_guard_notices?.());
        probe.notice_observer.observe(document.body, { childList: true, subtree: true, characterData: true });
        document.addEventListener('submit', probe.capture_listener, true);
        document.addEventListener('submit', probe.bubble_listener, false);
        const target = window as Window & { __f3ComposerSubmitProbe?: typeof probe };
        target.__f3ComposerSubmitProbe = probe;
        return {
          composer_form_present_before: Boolean(form),
          input_nonempty_before: Boolean(textarea?.value.trim()),
          submit_button_disabled_before: button?.disabled ?? null,
          submit_button_mode_before: mode(button),
        };
      }), 30_000, 'F3_UI_COMPOSER_DOM_PROBE_DEADLINE');
      attempt.dom = observeComposerDomState({
        ...beforeClick,
        native_submit_event_count: 0,
        submit_button_disabled_at_submit: null,
        submit_button_mode_at_submit: 'missing',
        submit_prevented_after_handler: null,
        composer_form_present_after: false,
        input_nonempty_after: null,
        submit_button_disabled_after: null,
        submit_button_mode_after: 'missing',
        model_setup_dialog_visible_after: false,
        queued_indicator_count_after: 0,
        active_chat_route_after: false,
        guard_notice_categories: [],
        handoff_reply_post_count: 0,
      });
      attempt.phase = 'composer-submit';
      const pending = page.waitForResponse(
        (response) => originalRequest(response.request(), page, '/api/chat/stream', 'POST'),
      );
      attempt.button_click_started = true;
      const click = page.getByRole('button', { name: '发送', exact: true }).click()
        .then(() => { attempt.button_click_completed = true; });
      const [response] = await Promise.all([pending, click]);
      const request: unknown = response.request().postDataJSON();
      const receipt = observeComposerRequest(request, intent);
      requireContract(response.status() === 200, 'F3_UI_COMPOSER_REJECTED');
      attempt.phase = 'sse-finish';
      await waitForComposerStreamCompletion(response, intent.kind);
      if (intent.kind === 'SOP') {
        attempt.sse_finished = true;
        attempt.phase = 'composer-ready';
        await page.getByPlaceholder('输入消息，按 Enter 发送...').waitFor({ state: 'visible' });
        attempt.composer_ready = true;
      }
      attempt.phase = 'complete';
      return receipt;
    });
  } catch (error: unknown) {
    attempt.error_category = safeUiErrorCategory(error);
    if (attempt.phase === 'sse-finish' && attempt.sse_finished === null) attempt.sse_finished = false;
    throw error;
  } finally {
    try {
      const domAfter = await boundedUiOperation(page.evaluate(() => {
        type Probe = {
          submit_count: number;
          submit_prevented: boolean | null;
          submit_button_disabled: boolean | null;
          submit_button_mode: ComposerButtonMode;
          guard_notices: ComposerGuardNotice[];
          form: HTMLFormElement | null;
          capture_listener: ((event: Event) => void) | null;
          bubble_listener: ((event: Event) => void) | null;
          notice_observer: MutationObserver | null;
          scan_guard_notices: (() => void) | null;
        };
        const placeholder = '输入消息，按 Enter 发送...';
        const textarea = document.querySelector<HTMLTextAreaElement>(`textarea[placeholder="${placeholder}"]`);
        const form = textarea?.closest('form') || null;
        const button = form?.querySelector<HTMLButtonElement>('button[type="submit"]') || null;
        const mode = (element: HTMLButtonElement | null) => {
          if (!element) return 'missing';
          const label = element.getAttribute('aria-label');
          if (label === '发送') return 'send';
          if (label === '加入发送队列') return 'queue';
          if (label === '停止生成') return 'stop';
          return 'other';
        };
        const target = window as Window & { __f3ComposerSubmitProbe?: Probe };
        const probe = target.__f3ComposerSubmitProbe;
        probe?.scan_guard_notices?.();
        const notices = probe?.guard_notices ?? [];
        probe?.notice_observer?.disconnect();
        const queued = Array.from(document.querySelectorAll<HTMLElement>('[role="status"]'))
          .filter(element => element.textContent?.trim() === '排队中').length;
        const modelSetupDialog = Array.from(document.querySelectorAll<HTMLElement>('[role="dialog"]'))
          .some(element => /模型配置|model configuration/i.test(element.textContent || ''));
        if (probe?.capture_listener) document.removeEventListener('submit', probe.capture_listener, true);
        if (probe?.bubble_listener) document.removeEventListener('submit', probe.bubble_listener, false);
        delete target.__f3ComposerSubmitProbe;
        return {
          composer_form_present_after: Boolean(form),
          input_nonempty_after: textarea ? Boolean(textarea.value.trim()) : null,
          submit_button_disabled_after: button?.disabled ?? null,
          submit_button_mode_after: mode(button),
          model_setup_dialog_visible_after: modelSetupDialog,
          native_submit_event_count: probe?.submit_count ?? 0,
          submit_button_disabled_at_submit: probe?.submit_button_disabled ?? null,
          submit_button_mode_at_submit: probe?.submit_button_mode ?? 'missing',
          submit_prevented_after_handler: probe?.submit_prevented ?? null,
          queued_indicator_count_after: Math.min(queued, 64),
          active_chat_route_after: /^\/workspace\/chat(?:\/|$)/.test(window.location.pathname),
          guard_notice_categories: notices,
        };
      }), 30_000, 'F3_UI_COMPOSER_DOM_READBACK_DEADLINE');
      attempt.dom = observeComposerDomState({
        ...attempt.dom,
        ...domAfter,
        handoff_reply_post_count: Math.min(handoffReplyPostCount, 64),
      });
    } catch { /* 页面已关闭时保留点击前的白名单状态，不输出页面/请求正文。 */ }
    page.off('request', onRequest);
    page.off('requestfailed', onRequestFailed);
    page.off('response', onResponse);
  }
}

export async function waitForComposerStreamCompletion(
  response: Response,
  kind: ComposerIntent['kind'],
): Promise<void> {
  /** SOP回复以SSE闭合；动态任务可能持久等待人工输入，改由持久事实驱动后续操作。 */
  if (kind === 'DTA') return;
  requireContract(await response.finished() === null, 'F3_UI_COMPOSER_REJECTED');
}

export async function approveDtaViaUi(
  initiatorPage: Page, reviewerPage: Page, intent: ApprovalIntent,
  readOriginalApproval: OriginalApprovalReader,
): Promise<ApprovalReceipt> {
  /** 第二账号原页面批准，原账本只读回调仍需调用方接入且本轮未实跑。 */
  return safely(async () => {
    requireContract(initiatorPage.context() !== reviewerPage.context(), 'F3_UI_SEPARATE_CONTEXT_REQUIRED');
    samePrincipal(await refreshPrincipalViaUi(initiatorPage), intent.initiator);
    const owner = boundPrincipal(initiatorPage);
    const pendingList = reviewerPage.waitForResponse((response) => originalRequest(response.request(), reviewerPage, '/api/attention-items', 'GET')
      && new URL(response.url()).searchParams.get('view') === 'active');
    const [listResponse, reviewer] = await Promise.all([
      pendingList, refreshPrincipalViaUi(reviewerPage, EnterpriseRoute.WorkItems),
    ]);
    samePrincipal(reviewer, intent.reviewer);
    assertSeparateReviewer(owner, reviewer);
    const list = record(await responseBody(reviewerPage, listResponse, 'F3_UI_APPROVAL_MISMATCH'), 'F3_UI_APPROVAL_MISMATCH');
    requireContract(Array.isArray(list.items), 'F3_UI_APPROVAL_MISMATCH');
    const selected = list.items.filter((item: unknown) => record(item, 'F3_UI_APPROVAL_MISMATCH').id === intent.attention_id);
    requireContract(selected.length === 1, 'F3_UI_APPROVAL_MISMATCH');
    const original = record(await readOriginalApproval(), 'F3_UI_APPROVAL_MISMATCH');
    const before = observeApprovalBefore(original, intent);
    const fromPage = observeApprovalBefore({ ...original, attention: selected[0] }, intent);
    requireContract(fromPage.frozen_sha256 === before.frozen_sha256
      && fromPage.attention_revision === before.attention_revision, 'F3_UI_APPROVAL_MISMATCH');
    const item = reviewerPage.getByRole('button').filter({ has: reviewerPage.getByText(before.title, { exact: true }) });
    requireContract(await item.count() === 1, 'F3_UI_APPROVAL_AMBIGUOUS');
    await item.click();
    const dialog = reviewerPage.getByRole('dialog');
    await dialog.waitFor({ state: 'visible' });
    requireContract(await dialog.getByText(`执行 ${before.execution_id}`, { exact: true }).count() === 1,
      'F3_UI_APPROVAL_MISMATCH');
    const rendered = await dialog.locator('pre').textContent();
    requireContract(rendered !== null, 'F3_UI_APPROVAL_MISMATCH');
    let argumentsValue: unknown;
    try { argumentsValue = JSON.parse(rendered); }
    catch { throw new F3UiContractError('F3_UI_APPROVAL_MISMATCH'); }
    const payload = record(record(original.attention, 'F3_UI_APPROVAL_MISMATCH').payload, 'F3_UI_APPROVAL_MISMATCH');
    requireContract(canonical(argumentsValue, 'F3_UI_APPROVAL_MISMATCH') === canonical(payload.arguments, 'F3_UI_APPROVAL_MISMATCH'),
      'F3_UI_APPROVAL_MISMATCH');
    const path = `/api/attention-items/${encodeURIComponent(intent.attention_id)}/resolve`;
    const pendingResolve = reviewerPage.waitForResponse((response) => originalRequest(response.request(), reviewerPage, path, 'POST'));
    const [response] = await Promise.all([
      pendingResolve, dialog.getByRole('button', { name: '仅批准本次操作', exact: true }).click(),
    ]);
    const request: unknown = response.request().postDataJSON();
    observeResolveRequest(request, before);
    requireContract(response.status() === 200, 'F3_UI_RESOLVE_REJECTED');
    await dialog.waitFor({ state: 'hidden' });
    // 调用方必须从原行/信号消费等候取得后置投影；本辅助层不填补缺失行或批准状态。
    observeApprovalAfter(await readOriginalApproval(), before);
    return before;
  });
}

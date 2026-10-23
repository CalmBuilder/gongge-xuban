/**
 * @Time       : 2026/10/13
 * @Author     : zhanglp8181
 * @File       : final-f3-jrn08-helpers.ts
 * @CallChain  : 原页面/Composer/Attention → 原Runtime → 独立模拟HTTP与私有只读事实
 * @Description: JRN08真实模型、受控业务远端的页面与精确账本辅助，不伪称真实供应商。
 */
import { createHash } from 'node:crypto';
import { lstatSync, readFileSync, realpathSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { expect, type Browser, type Locator, type Page, type Response, type TestInfo } from '@playwright/test';
import {
  approveDtaViaUi, BoundedRequestCountBudget, boundedJsonBody, boundedPageJsonBody,
  loginViaUi, observeRemoteCleanupResponse, openEmployeeViaUi, sdkLedgerIsQuiescent, sendComposerViaUi,
  type ComposerReceipt, type UiAccount, type UiEmployee,
} from './final-f3-browser-driver';

export type Branch = 'SOP' | 'DTA';
export type Scenario = 'normal' | 'cancel' | 'safe_decline' | 'unknown';
export type FactRow = Readonly<Record<string, unknown>>;
export type Business = { namespace: string; object_id: string; product_name: string;
  quantity: number; customer: string; due_date: string };
export type BusinessCase = { message: string; confirmation_message: string; expected_business: Business };
export type Initial = {
  owner: UiAccount; reviewer: UiAccount;
  agents: Record<Branch, UiEmployee & { gallery_label: string }>;
  model: { id: string; name: string };
  cases: Record<Branch, Record<Scenario, BusinessCase>>;
  knowledge: FactRow; sop_knowledge: FactRow;
  date_labels: { timezone: string; expected_due_date: string };
};
export type Facts = { pid: number; instances: FactRow[]; operations: FactRow[];
  attempts: FactRow[]; plan_revisions: FactRow[]; signals: FactRow[]; inbox: FactRow[]; messages: FactRow[];
  attention: FactRow[]; commands: FactRow[]; events: FactRow[]; sdk_calls: FactRow[]; jobs: FactRow[]; knowledge: FactRow;
  results: FactRow[]; publications: FactRow[]; editor_baseline: FactRow;
  guidance_resources: FactRow[]; guidance_uses: FactRow[] };
export type SafeDeclineFacts = Pick<Facts, 'instances' | 'operations' | 'attempts' | 'messages'>;
type PrivateConfig = { control_key: string; run_id: string; runtime: string;
  provider: { origin: string; key: string; control_key: string; namespace: string } };
const MEMORY_FREEZE_PRIVATE_API_READ_MAXIMUM = 5700;
const memoryFreezePrivateApiReadBudget = new BoundedRequestCountBudget(
  MEMORY_FREEZE_PRIVATE_API_READ_MAXIMUM, 'F3_MEMORY_PRIVATE_API_READ_LIMIT',
);
const MEMORY_FREEZE_PROVIDER_OBSERVATION_READ_MAXIMUM = 2;
export const MEMORY_STATE_PRIVATE_RESPONSE_MAX_BYTES = 2 * 1024 * 1024;
export const MEMORY_INITIAL_PRIVATE_RESPONSE_MAX_BYTES = 2 * 1024 * 1024;
export const MEMORY_PROVIDER_OBSERVATION_MAX_BYTES = 256 * 1024;
const memoryFreezeProviderObservationReadBudget = new BoundedRequestCountBudget(
  MEMORY_FREEZE_PROVIDER_OBSERVATION_READ_MAXIMUM, 'F3_MEMORY_PROVIDER_OBSERVATION_READ_LIMIT',
);

/** 返回memory-dta-freeze独立APIRequestContext读取预算的安全计数摘要。 */
export function memoryFreezePrivateApiReadBudgetSnapshot(): { reads: number; maximum: number } {
  return { reads: memoryFreezePrivateApiReadBudget.reads, maximum: MEMORY_FREEZE_PRIVATE_API_READ_MAXIMUM };
}

/** 返回freeze诊断两次provider观察请求的安全计数摘要。 */
export function memoryFreezeProviderObservationReadBudgetSnapshot(): { reads: number; maximum: number } {
  return {
    reads: memoryFreezeProviderObservationReadBudget.reads,
    maximum: MEMORY_FREEZE_PROVIDER_OBSERVATION_READ_MAXIMUM,
  };
}

/** 只为冻结单案的provider旁证请求预留限额，超出时在网络派发前拒绝。 */
export async function withMemoryFreezeProviderObservationReadBudget<T>(
  dispatch: () => Promise<T>,
): Promise<T> {
  if (process.env.F3_DIAGNOSTIC_SELECTION !== 'memory-dta-freeze') return dispatch();
  return memoryFreezeProviderObservationReadBudget.dispatch(dispatch);
}

export function memoryStateResponseWithinByteLimit(headers: Readonly<Record<string, string>>): boolean {
  /** 仅接收有显式规范Content-Length且不超过状态API预算的私有记忆读回。 */
  const contentLength = headers['content-length'];
  return Boolean(contentLength && /^(?:0|[1-9]\d*)$/u.test(contentLength)
    && Number.isSafeInteger(Number(contentLength))
    && Number(contentLength) <= MEMORY_STATE_PRIVATE_RESPONSE_MAX_BYTES);
}

export function memoryProviderObservationResponseWithinByteLimit(
  headers: Readonly<Record<string, string>>,
): boolean {
  /** provider观察接口必须在读取JSON正文前给出规范Content-Length且不超过256KiB。 */
  const value = headers['content-length'];
  if (!value || !/^(0|[1-9]\d*)$/u.test(value)) return false;
  const size = Number(value);
  return Number.isSafeInteger(size) && size <= MEMORY_PROVIDER_OBSERVATION_MAX_BYTES;
}
export type RemoteEffect = { taskId: string; business: Business; receipt_id: string;
  effect_count: number; request_count: number; real_vendor_qualified: false };
export type RemoteTask = { taskId: string; status: string; request_count: number;
  effect_count: number; real_vendor_qualified: false };
export type SessionListRouteHealth = Readonly<{
  request_count: number; response_count: number; completed_count: number;
  non_2xx_count: number; pending_count: number; malformed_event_count: number; passed: boolean;
}>;
const START_TERMINAL_STATUSES = new Set(['succeeded', 'failed', 'cancelled', 'archived']);

export function isDtaStartResolved(status: unknown): boolean {
  /** 只有进入等待或明确终态才结束启动观察，避免终态无动作后继续空等。 */
  return status === 'waiting' || typeof status === 'string' && START_TERMINAL_STATUSES.has(status);
}

export type Journey = { kind: Branch; initial: Initial; scenario: Scenario;
  case: BusinessCase; session: string; receipt: ComposerReceipt;
  instance: FactRow; operation: FactRow; http_start: number };
const tokens = new WeakMap<Page, string>();
let privateConfiguration: PrivateConfig | undefined;

type DisposableApiResponse = { dispose(): Promise<void> };
const MAX_DIAGNOSTIC_API_JSON_BYTES = 256 * 1024;

export async function withDisposedApiResponse<TResponse extends DisposableApiResponse, TResult>(
  response: TResponse,
  consume: (response: TResponse) => Promise<TResult>,
): Promise<TResult> {
  /** APIRequestContext保留正文至context关闭；每次响应读完或拒绝后立即释放其缓存正文。 */
  try {
    return await consume(response);
  } finally {
    await response.dispose();
  }
}

export function diagnosticApiJsonResponseWithinByteLimit(
  headers: Readonly<Record<string, string>>, maximumBytes = MAX_DIAGNOSTIC_API_JSON_BYTES,
): boolean {
  /** APIRequestContext正文解析前要求有限、未压缩的Content-Length。 */
  const contentLength = headers['content-length'];
  const encoding = headers['content-encoding']?.trim().toLowerCase();
  return (!encoding || encoding === 'identity') && Boolean(contentLength
    && /^(?:0|[1-9]\d*)$/u.test(contentLength)
    && Number.isSafeInteger(Number(contentLength))
    && Number(contentLength) <= maximumBytes);
}

export function object(value: unknown): Record<string, unknown> {
  /** 在私有JSON边界校验对象，不把数组、空值或错误响应强转为事实。 */
  if (typeof value !== 'object' || value === null || Array.isArray(value)) throw new Error('F3_OBJECT_REQUIRED');
  return value as Record<string, unknown>;
}
export function assertSuccessfulModelConnectionDiagnostic(
  responseStatus: number, value: unknown,
): void {
  /** HTTP成功只表示路由完成；模型连接必须有结构化success=true才可进入正式预检。 */
  expect(responseStatus).toBe(200);
  const result = object(value);
  expect(typeof result.success).toBe('boolean');
  expect(result.success).toBe(true);
}
export async function afterSuccessfulModelConnectionDiagnostic<TResult>(
  responseStatus: number,
  value: unknown,
  dispatch: () => Promise<TResult>,
): Promise<TResult> {
  /** 动态能力请求只能通过与连接诊断共用的成功门控后派发，失败分支不触达模型预检。 */
  assertSuccessfulModelConnectionDiagnostic(responseStatus, value);
  return dispatch();
}
export function text(value: unknown): string {
  /** 原标识必须为非空字符串，缺失事实绝不补造。 */
  if (typeof value !== 'string' || !value.trim()) throw new Error('F3_STRING_REQUIRED');
  return value;
}
export function integer(value: unknown): number {
  /** 原计数必须为非负安全整数，不使用字符串隐式转换。 */
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0) throw new Error('F3_COUNT_REQUIRED');
  return value;
}

export function sessionListRouteHealthFromJournal(raw: string): SessionListRouteHealth {
  /** 只汇总原会话列表ASGI事件计数和HTTP类别，不保留查询、身份或正文。 */
  let requestCount = 0; let responseCount = 0; let completedCount = 0;
  let non2xxCount = 0; let pendingCount = 0; let malformedEventCount = 0;
  for (const line of raw.split(/\r?\n/u).filter(Boolean)) {
    let value: unknown;
    try { value = JSON.parse(line) as unknown; }
    catch { malformedEventCount += 1; continue; }
    if (typeof value !== 'object' || value === null || Array.isArray(value)) {
      malformedEventCount += 1; continue;
    }
    const event = (value as Record<string, unknown>).event;
    if (event === 'request_start') requestCount += 1;
    else if (event === 'response_start') {
      responseCount += 1;
      const status = (value as Record<string, unknown>).status;
      if (typeof status !== 'number' || !Number.isInteger(status) || status < 200 || status >= 300) {
        non2xxCount += 1;
      }
    } else if (event === 'response_complete') completedCount += 1;
    else if (event === 'request_pending') pendingCount += 1;
    else malformedEventCount += 1;
  }
  const passed = requestCount > 0 && requestCount === responseCount
    && responseCount === completedCount && non2xxCount === 0 && malformedEventCount === 0;
  return { request_count: requestCount, response_count: responseCount,
    completed_count: completedCount, non_2xx_count: non2xxCount,
    pending_count: pendingCount, malformed_event_count: malformedEventCount, passed };
}

export async function assertSessionListRouteHealthy(info: TestInfo): Promise<void> {
  /** 原页面旅程结束后核会话列表GET均有2xx持久响应，无隐藏500或未完成读取。 */
  const runtime = configuration().runtime; const directory = lstatSync(runtime);
  if (!directory.isDirectory() || directory.isSymbolicLink() || (directory.mode & 0o077)
    || directory.uid !== process.getuid?.()) throw new Error('F3_SESSION_LIST_PRIVATE_RUNTIME_REQUIRED');
  const journal = join(runtime, 'session-metadata-diagnostic.safe.jsonl');
  const summary = sessionListRouteHealthFromJournal(readFileSync(journal, 'utf8'));
  await criterion(info, 'session-list-read-status-healthy', summary);
  expect(summary.passed).toBe(true);
}

export function rows(value: unknown): FactRow[] {
  /** 只接受实际返回的JSON对象列表，未知集合不当作空集合通过。 */
  if (!Array.isArray(value)) throw new Error('F3_ROWS_REQUIRED');
  return value.map(object);
}
export function one(values: FactRow[], predicate: (row: FactRow) => boolean): FactRow {
  /** 精确原身份筛选必须唯一，不能用最新实例或最新Operation猜关联。 */
  const matches = values.filter(predicate);
  if (matches.length !== 1) throw new Error('F3_EXACT_FACT_NOT_UNIQUE');
  const match = matches[0];
  if (!match) throw new Error('F3_EXACT_FACT_MISSING');
  return match;
}
export function hash(value: unknown): string {
  /** 安全证据仅记录本地摘要，原私有对象留在内存。 */
  return createHash('sha256').update(JSON.stringify(value)).digest('hex');
}

const CASE_SDK_IDENTITY_FIELDS = new Set([
  'tenant_id', 'user_id', 'agent_id', 'session_id', 'source_input_id', 'turn_id',
  'instance_id', 'skill_version_id', 'knowledge_version_id', 'plan_id', 'operation_id',
  'attempt_id', 'signal_id', 'task_id',
]);

const MODEL_QUALITY_CASE_IDS = new Set([
  'sop-normal', 'sop-knowledge_old', 'sop-knowledge_new', 'sop-knowledge_unknown',
  'sop-memory_freeze', 'sop-memory_correction', 'sop-memory_clear', 'sop-feedback_improvement',
  'dynamic_task-normal', 'dynamic_task-knowledge_old', 'dynamic_task-knowledge_new',
  'dynamic_task-knowledge_unknown', 'dynamic_task-memory_freeze', 'dynamic_task-memory_correction',
  'dynamic_task-memory_clear', 'dynamic_task-feedback_improvement',
]);

/** 返回冻结模型质量合同中的精确case标识；未有语义对应的旅程保持未映射。 */
export function modelQualityCaseId(branch: Branch, scenario: string): string | undefined {
  const prefix = branch === 'SOP' ? 'sop' : 'dynamic_task';
  const candidate = `${prefix}-${scenario}`;
  return MODEL_QUALITY_CASE_IDS.has(candidate) ? candidate : undefined;
}

export function modelQualityKnowledgeCaseId(branch: Branch, label: 'old' | 'new' | 'none'): string | undefined {
  /** 将消费者的none无答案标签规范化为冻结合同的knowledge_unknown case。 */
  const scenario = label === 'none' ? 'knowledge_unknown' : `knowledge_${label}`;
  return modelQualityCaseId(branch, scenario);
}

export function safeCaseIdentityBinding(caseId: string, stage: string,
  identity: Readonly<Record<string, string | undefined>>, clientTurnId?: string): {
    case_id: string; stage: string; identity_sha256: Record<string, string>; client_turn_id_sha256?: string;
  } {
  /** 用原SDK的标量JSON摘要关联case，拒绝原始ID和非身份字段进入安全附件。 */
  if (!caseId || caseId.length > 256 || /[\u0000-\u001f\u007f]/u.test(caseId)) {
    throw new Error('F3_CASE_BINDING_CASE_ID_INVALID');
  }
  if (!/^[a-z][a-z0-9-]{0,63}$/u.test(stage)) throw new Error('F3_CASE_BINDING_STAGE_INVALID');
  const identitySha256: Record<string, string> = {};
  for (const [field, value] of Object.entries(identity)) {
    if (!CASE_SDK_IDENTITY_FIELDS.has(field)) throw new Error('F3_CASE_BINDING_IDENTITY_FIELD_INVALID');
    if (value === undefined) continue;
    if (typeof value !== 'string' || !value.trim()) throw new Error('F3_CASE_BINDING_IDENTITY_VALUE_INVALID');
    identitySha256[field] = hash(value);
  }
  if (!Object.keys(identitySha256).length && clientTurnId === undefined) {
    throw new Error('F3_CASE_BINDING_IDENTITY_REQUIRED');
  }
  if (clientTurnId !== undefined && (typeof clientTurnId !== 'string' || !clientTurnId.trim())) {
    throw new Error('F3_CASE_BINDING_CLIENT_TURN_INVALID');
  }
  return { case_id: caseId, stage, identity_sha256: identitySha256,
    ...(clientTurnId === undefined ? {} : { client_turn_id_sha256: hash(clientTurnId) }) };
}

export function safeHttpErrorCode(value: unknown): string | undefined {
  /** 仅把稳定的大写错误码带入失败诊断，拒绝传播响应正文或自由文本。 */
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return undefined;
  const root = value as Record<string, unknown>;
  const detail = root.detail;
  const error = typeof root.error === 'object' && root.error !== null && !Array.isArray(root.error)
    ? root.error as Record<string, unknown> : undefined;
  const candidate = error?.code ?? root.error_code ?? (typeof detail === 'object' && detail !== null
    && !Array.isArray(detail) ? (detail as Record<string, unknown>).code : detail);
  return typeof candidate === 'string' && /^[A-Z][A-Z0-9_]{2,79}$/.test(candidate)
    ? candidate : undefined;
}

export type SafeModelDiagnosticObservation = Readonly<{
  response_http_status: number | null;
  provider_http_status: number | null;
  diagnostic_success: boolean;
  error_category: string;
  checks: ReadonlyArray<{ stage: string; status: string }>;
}>;

export type SafeModelDiagnosticEvidence = SafeModelDiagnosticObservation & Readonly<{
  diagnostic_invocation_sha256: string | null;
  config_fingerprint_sha256: string | null;
  application_request_id_sha256: string | null;
  provider_request_id_sha256: string | null;
  response_body_kind: 'bounded_json' | 'omitted';
}>;

export function safeModelDiagnosticObservation(
  responseStatus: number,
  value: unknown,
): SafeModelDiagnosticObservation {
  /** 只保留有限诊断阶段、状态和错误类别，不归档供应商文本、模型或端点。 */
  const root = typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown> : {};
  const stageByName: Readonly<Record<string, string>> = {
    '配置': 'configuration', configuration: 'configuration',
    '模型目录': 'model_catalog', model_catalog: 'model_catalog',
    '账户状态': 'account_status', account_status: 'account_status',
    '最小生成': 'minimum_generation', minimum_generation: 'minimum_generation',
  };
  const errorCategoryByCode: Readonly<Record<string, string>> = {
    AUTHENTICATION_FAILED: 'authentication', BILLING_UNAVAILABLE: 'billing',
    MODEL_OR_ENDPOINT_NOT_FOUND: 'not_found', MODEL_NOT_AVAILABLE: 'model_not_available',
    RATE_LIMITED: 'rate_limited', PROVIDER_UNAVAILABLE: 'provider_unavailable',
    CONNECTION_TIMEOUT: 'timeout', MODEL_CONNECTION_FAILED: 'connection_failed',
  };
  const safeStatus = (candidate: unknown): number | null =>
    typeof candidate === 'number' && Number.isInteger(candidate) && candidate >= 100 && candidate <= 599
      ? candidate : null;
  const rawChecks = Array.isArray(root.checks) ? root.checks.slice(0, 8) : [];
  const checks = rawChecks.map(candidate => {
    const check = typeof candidate === 'object' && candidate !== null && !Array.isArray(candidate)
      ? candidate as Record<string, unknown> : {};
    const name = typeof check.name === 'string' ? check.name : '';
    const status = check.status === 'passed' || check.status === 'failed' || check.status === 'skipped'
      ? check.status : 'unknown';
    return { stage: stageByName[name] ?? 'unknown', status };
  });
  const code = typeof root.error_code === 'string' ? root.error_code : '';
  const diagnosticSuccess = root.success === true;
  return {
    response_http_status: safeStatus(responseStatus),
    provider_http_status: safeStatus(root.http_status),
    diagnostic_success: diagnosticSuccess,
    error_category: errorCategoryByCode[code] ?? (diagnosticSuccess ? 'none' : 'unknown'),
    checks,
  };
}

export function safeModelDiagnosticEvidence(
  responseStatus: number,
  headers: Readonly<Record<string, string>>,
  value: unknown,
  responseBodyKind: SafeModelDiagnosticEvidence['response_body_kind'],
): SafeModelDiagnosticEvidence {
  /** 非2xx也留存HTTP类别与不可逆绑定摘要，拒绝保存请求ID、错误正文和自由字段。 */
  const normalizedHeaders = new Map(Object.entries(headers).map(([key, item]) => [key.toLowerCase(), item]));
  const exactSha = (item: unknown): string | null =>
    typeof item === 'string' && /^[a-f0-9]{64}$/u.test(item) ? item : null;
  const safeIdSha = (item: unknown): string | null =>
    typeof item === 'string' && item.length > 0 && item.length <= 512
      && !/[\u0000-\u001f\u007f]/u.test(item)
      ? createHash('sha256').update(item, 'utf8').digest('hex') : null;
  const correlationId = normalizedHeaders.get('x-request-id')
    ?? normalizedHeaders.get('x-correlation-id') ?? normalizedHeaders.get('request-id');
  const root = typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown> : {};
  return {
    ...safeModelDiagnosticObservation(responseStatus, value),
    diagnostic_invocation_sha256: exactSha(
      normalizedHeaders.get('x-f3-diagnostic-invocation-sha256')),
    config_fingerprint_sha256: exactSha(
      normalizedHeaders.get('x-f3-diagnostic-config-fingerprint-sha256')),
    application_request_id_sha256: safeIdSha(correlationId),
    provider_request_id_sha256: safeIdSha(root.request_id),
    response_body_kind: responseBodyKind,
  };
}

function configuration(): PrivateConfig {
  /** 仅从自有600普通单链接文件读取测试控制值，不读真实模型key或业务DSN。 */
  if (privateConfiguration) return privateConfiguration;
  const path = text(process.env.F3_BROWSER_PRIVATE_CONFIG);
  const file = lstatSync(path);
  if (!file.isFile() || file.isSymbolicLink() || file.nlink !== 1 || (file.mode & 0o077)
    || file.uid !== process.getuid?.()) throw new Error('F3_PRIVATE_CONFIGURATION_REQUIRED');
  const body: unknown = JSON.parse(readFileSync(path, 'utf8'));
  const input = object(body); const provider = object(input.provider);
  const origin = new URL(text(provider.origin));
  if (origin.protocol !== 'http:' || origin.hostname !== '127.0.0.1' || origin.pathname !== '/'
    || origin.username || origin.password || origin.search || origin.hash
    || provider.provider_kind !== 'simulated_async_service') throw new Error('F3_SIMULATED_ORIGIN_REQUIRED');
  privateConfiguration = {
    control_key: text(input.control_key), run_id: text(input.run_id), runtime: text(input.runtime),
    provider: { origin: origin.origin, key: text(provider.key), control_key: text(provider.control_key),
      namespace: text(provider.namespace) },
  };
  if (realpathSync(privateConfiguration.runtime) !== privateConfiguration.runtime) throw new Error('F3_OWNED_RUNTIME_REQUIRED');
  return privateConfiguration;
}

export async function privateRequest(page: Page, path: string, method: 'GET' | 'POST' = 'GET'): Promise<unknown> {
  /** 只访问隔离测试只读/重启入口；业务变更仍全部由原页面发起。 */
  if (!path.startsWith('/__e2e/f3/')) throw new Error('F3_PRIVATE_PATH_REQUIRED');
  const request = () => page.request.fetch(path, { method,
    timeout: 30_000, headers: { 'X-F3-Control-Key': configuration().control_key } });
  const response = process.env.F3_DIAGNOSTIC_SELECTION === 'memory-dta-freeze'
    ? await memoryFreezePrivateApiReadBudget.dispatch(request)
    : await request();
  return withDisposedApiResponse(response, async response => {
    if (response.status() !== 200 && response.status() !== 202) throw new Error('F3_PRIVATE_READ_FAILED');
    if ((path === '/__e2e/f3/bootstrap' || path === '/__e2e/f3/initial')
        && !diagnosticApiJsonResponseWithinByteLimit(response.headers(), MEMORY_INITIAL_PRIVATE_RESPONSE_MAX_BYTES)) {
      throw new Error('F3_PRIVATE_READ_FAILED');
    }
    if (path.startsWith('/__e2e/f3/memory/')) {
      if (!memoryStateResponseWithinByteLimit(response.headers())) {
        throw new Error('F3_PRIVATE_READ_FAILED');
      }
    }
    const body: unknown = await boundedJsonBody(response, 'F3_PRIVATE_READ_FAILED'); return body;
  });
}

function account(value: unknown): UiAccount {
  /** 合成账号标签与原登录响应分别核，不导出密码或token。 */
  const row = object(value); const credentials = object(row.credentials); const expected = object(row.expected);
  const role = expected.role;
  if ((role !== 'admin' && role !== 'member') || expected.membership_status !== 'active') throw new Error('F3_ACCOUNT_INVALID');
  return { credentials: { username: text(credentials.username), password: text(credentials.password) },
    expected: { id: text(expected.id), tenant_id: text(expected.tenant_id), username: text(expected.username),
      role, membership_status: 'active' } };
}
function business(value: unknown): Business {
  /** 合成业务字段显式校验，最终读回与事前人工标签逐字段比较。 */
  const row = object(value);
  return { namespace: text(row.namespace), object_id: text(row.object_id), product_name: text(row.product_name),
    quantity: integer(row.quantity), customer: text(row.customer), due_date: text(row.due_date) };
}
export async function initial(page: Page): Promise<Initial> {
  /** 私有bootstrap只含本轮合成身份和事前标签，不归档整个响应。 */
  const input = object(await privateRequest(page, '/__e2e/f3/bootstrap'));
  const agents = object(input.agents); const cases = object(input.cases); const model = object(input.model);
  function employee(kind: Branch): UiEmployee & { gallery_label: string } {
    /** 使用bootstrap的精确员工身份，不能按kind猜某个已有员工。 */
    const row = object(agents[kind]);
    return { id: text(row.id), tenant_id: text(row.tenant_id), name: text(row.name), gallery_label: text(row.gallery_label) };
  }
  function scenarios(kind: Branch): Record<Scenario, BusinessCase> {
    /** 每个控制使用不同事前合成对象，失败不能改变预期或复用正常结果。 */
    const branch = object(cases[kind]);
    function read(name: Scenario): BusinessCase {
      /** 只解析已冻结人工输入和效果标签，模型输出不参与标签生成。 */
      const row = object(branch[name]);
      return { message: text(row.message), confirmation_message: text(row.confirmation_message),
        expected_business: business(row.expected_business) };
    }
    return { normal: read('normal'), cancel: read('cancel'), safe_decline: read('safe_decline'), unknown: read('unknown') };
  }
  const dates = object(input.date_labels);
  return { owner: account(input.owner), reviewer: account(input.reviewer), agents: { SOP: employee('SOP'), DTA: employee('DTA') },
    model: { id: text(model.id), name: text(model.name) }, cases: { SOP: scenarios('SOP'), DTA: scenarios('DTA') },
    knowledge: object(input.knowledge), sop_knowledge: object(input.sop_knowledge),
    date_labels: { timezone: text(dates.timezone), expected_due_date: text(dates.expected_due_date) } };
}
export async function facts(page: Page, session?: string): Promise<Facts> {
  /** 读取原持久行和真实SDK白名单，缺集合即失败而非补零。 */
  const input = object(await privateRequest(page, '/__e2e/f3/facts' + (session ? '?session_id=' + encodeURIComponent(session) : '')));
  return { pid: integer(input.pid), instances: rows(input.instances), operations: rows(input.operations),
    attempts: rows(input.attempts), plan_revisions: rows(input.plan_revisions),
    signals: rows(input.signals), inbox: rows(input.inbox), messages: rows(input.messages),
    attention: rows(input.attention), commands: rows(input.commands), events: rows(input.events),
    sdk_calls: rows(input.sdk_calls), jobs: rows(input.jobs), knowledge: object(input.knowledge),
    results: rows(input.results), publications: rows(input.publications),
    editor_baseline: object(input.editor_baseline),
    guidance_resources: rows(input.guidance_resources), guidance_uses: rows(input.guidance_uses) };
}
export async function until(page: Page, session: string | undefined, predicate: (state: Facts) => boolean): Promise<Facts> {
  /** 有限等待原持久状态，不在测试中写运行实例或触发resume函数。 */
  await expect.poll(async () => predicate(await facts(page, session)),
    { timeout: 240_000, intervals: [300, 1000, 2000] }).toBe(true);
  return facts(page, session);
}
export async function criterion(info: TestInfo, name: string, values: Record<string, unknown>): Promise<void> {
  /** 调用方只传明确白名单计数/身份/hash/人工合成标签，禁止bootstrap和正文。 */
  await info.attach('f3-safe-criterion', { body: Buffer.from(JSON.stringify({ criterion: name,
    ...values, real_vendor_qualified: false, business_provider_kind: 'simulated_async_service' })),
  contentType: 'application/json' });
}

export async function attachCaseIdentityBinding(info: TestInfo, stage: string,
  identity: Readonly<Record<string, string | undefined>>, clientTurnId?: string,
  caseId?: string): Promise<void> {
  /** SDK等待、错误或轮询超时前落下case与持久身份摘要，原值只在当前进程内使用。 */
  const resolvedCaseId = caseId ?? info.title;
  if (caseId !== undefined && !MODEL_QUALITY_CASE_IDS.has(caseId)) {
    throw new Error('F3_MODEL_QUALITY_CASE_ID_UNREGISTERED');
  }
  await criterion(info, 'case-sdk-identity-binding', safeCaseIdentityBinding(resolvedCaseId, stage,
    identity, clientTurnId));
}

export async function withOwnerPageParked<T>(ownerPage: Page, action: () => Promise<T>): Promise<T> {
  /** 第二主体独立页面执行期间卸载持有方的重型应用文档，并在结束后恢复原URL。 */
  const originalUrl = ownerPage.url();
  if (!originalUrl || originalUrl === 'about:blank') throw new Error('F3_OWNER_PAGE_ROUTE_REQUIRED');
  try {
    await ownerPage.goto('about:blank');
    return await action();
  } finally {
    if (!ownerPage.isClosed() && ownerPage.url() !== originalUrl) await ownerPage.goto(originalUrl);
  }
}

export async function login(page: Page, value: UiAccount): Promise<void> {
  /** 原登录UI与响应双核，实际token仅供无页面入口的原动态能力预检。 */
  const pending = page.waitForResponse(response => new URL(response.url()).pathname === '/api/auth/login'
    && response.request().method() === 'POST');
  await loginViaUi(page, value);
  const response = await pending;
  const body: unknown = await boundedPageJsonBody(page, response, 'F3_UI_LOGIN_RESPONSE_INVALID');
  tokens.set(page, text(object(body).token));
}
export async function verifyModel(page: Page, value: Initial, info: TestInfo): Promise<void> {
  /** HTTP状态断言前保存脱敏失败关联；连接诊断不能代替原动态能力预检或授予ready。 */
  const modelConfigurationSha256 = hash(value.model.id);
  await page.goto('/enterprise/models');
  const row = page.getByRole('row').filter({ hasText: value.model.name });
  await expect(row).toHaveCount(1);
  const pending = page.waitForResponse(response => new URL(response.url()).pathname
    === `/api/enterprise/model-configs/${value.model.id}/test` && response.request().method() === 'POST');
  await row.getByRole('button', { name: '连接测试 ' + value.model.name, exact: true }).click();
  const response = await pending;
  const headers = response.headers();
  let connection: unknown;
  let responseBodyKind: 'bounded_json' | 'omitted' = 'omitted';
  if (diagnosticApiJsonResponseWithinByteLimit(headers)) {
    try {
      connection = await boundedPageJsonBody(page, response, 'F3_MODEL_CONNECTION_RESPONSE_INVALID');
      responseBodyKind = 'bounded_json';
    } catch {
      responseBodyKind = 'omitted';
    }
  }
  const diagnostic = safeModelDiagnosticEvidence(
    response.status(), headers, connection, responseBodyKind,
  );
  await criterion(info, 'actual-model-connection-diagnostic', { ...diagnostic,
    model_config_id: value.model.id, model_config_sha256: modelConfigurationSha256,
    tenant_sha256: hash(value.owner.expected.tenant_id),
    admin_actor_sha256: hash(value.owner.expected.id), ready_claimed: false });
  expect(diagnostic.diagnostic_invocation_sha256).toMatch(/^[a-f0-9]{64}$/u);
  const connectionConfigFingerprint = diagnostic.config_fingerprint_sha256;
  expect(connectionConfigFingerprint).toMatch(/^[a-f0-9]{64}$/u);
  expect(responseBodyKind).toBe('bounded_json');
  const connectionResult = object(connection);
  const capability = await afterSuccessfulModelConnectionDiagnostic(
    response.status(), connectionResult, async () => {
      const dialog = page.getByRole('dialog', { name: '模型连接诊断' });
      await expect(dialog).toBeVisible();
      await expect(dialog.getByText('连接可用', { exact: true })).toBeVisible();
      await dialog.getByRole('button', { name: 'Close', exact: true }).click();
      const token = tokens.get(page); if (!token) throw new Error('F3_ACTUAL_LOGIN_TOKEN_REQUIRED');
      const probe = await page.request.post(`/api/enterprise/model-configs/${value.model.id}/preflight-dynamic`, {
        params: { tenant_id: value.owner.expected.tenant_id }, headers: { Authorization: 'Bearer ' + token },
        timeout: 240_000,
      });
      return withDisposedApiResponse(probe, async probe => {
        expect(probe.status()).toBe(200);
        if (!diagnosticApiJsonResponseWithinByteLimit(probe.headers())) {
          throw new Error('F3_MODEL_CAPABILITY_RESPONSE_INVALID');
        }
        const body: unknown = await probe.json(); const result = object(body);
        expect(result.success).toBe(true); expect(result.status).toBe('ready');
        const checksum = text(result.checksum);
        expect(checksum).toMatch(/^[a-f0-9]{64}$/);
        const invocation = probe.headers()['x-f3-diagnostic-invocation-sha256'];
        expect(invocation).toMatch(/^[a-f0-9]{64}$/);
        const configFingerprint = probe.headers()['x-f3-diagnostic-config-fingerprint-sha256'];
        expect(configFingerprint).toMatch(/^[a-f0-9]{64}$/);
        return { checksum, invocation, configFingerprint };
      });
  });
  expect(capability.configFingerprint).toBe(connectionConfigFingerprint);
  await criterion(info, 'actual-model-native-capabilities', { passed: true,
    model_config_id: value.model.id, model_config_sha256: modelConfigurationSha256,
    tenant_sha256: hash(value.owner.expected.tenant_id),
    admin_actor_sha256: hash(value.owner.expected.id),
    diagnostic_invocation_sha256: capability.invocation,
    config_fingerprint_sha256: capability.configFingerprint,
    capability_checksum: capability.checksum,
    connection_diagnostic_success: diagnostic.diagnostic_success });
}
export async function open(page: Page, value: Initial, kind: Branch): Promise<void> {
  /** 由原员工广场进入对话，原use请求验证精确员工及同租户。 */
  const employee = value.agents[kind];
  await openEmployeeViaUi(page, employee, employee.gallery_label);
}
export async function scope(page: Page, employee: UiEmployee): Promise<void> {
  /** 管理作用域沿原员工菜单真实切换，不写localStorage或伪造范围事件。 */
  const trigger = page.getByLabel('切换当前员工', { exact: true });
  await expect(trigger).toBeVisible(); await trigger.click();
  const target = page.getByRole('menuitem').filter({ hasText: employee.name });
  await expect(target).toHaveCount(1); await target.click();
  await expect(trigger).toContainText(employee.name);
}
export async function importGuidance(page: Page, value: Initial, info: TestInfo): Promise<FactRow> {
  /** 等待原能力回执后走安全导入UI，核冻结无答案指导及真实Revision绑定。 */
  const slug = text(value.knowledge.guidance_slug);
  const markdown = `---\nname: ${slug}\ndescription: 企业政策任务必须先实际检索冻结知识版本。\nallowed-tools: knowledge.search\n---\n`
    + '# 知识查询\n所有政策问题先检索 knowledge.search，再依据完整证据回答并引用来源。'
    + '未知政策明确无资料，不编造。用户要求等待确认时先规划clarification步骤，使用wait_input动作。'
    + '只有该步骤实际进入completed_steps后才表示本人确认已收到，再检索并完成回答；等待期间不提前完成。\n';
  await scope(page, value.agents.DTA);
  const capabilitiesReady = page.waitForResponse(response => {
    const url = new URL(response.url());
    return response.request().method() === 'GET'
      && url.pathname === '/api/enterprise/general-skill-import-jobs/capabilities';
  });
  await page.goto('/enterprise/general-skills');
  const capabilitiesResponse = await capabilitiesReady;
  expect(capabilitiesResponse.status()).toBe(200);
  const capabilities = object(await capabilitiesResponse.json());
  expect(capabilities.enabled).toBe(true);
  expect(Array.isArray(capabilities.source_kinds) && capabilities.source_kinds.includes('upload')).toBe(true);
  await page.getByRole('button', { name: /新增/ }).click();
  await page.getByRole('menuitem', { name: '安全导入 Skill', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '安全导入 Skill 包', exact: true });
  await dialog.locator('input[type="file"]').setInputFiles({ name: 'SKILL.md', mimeType: 'text/markdown',
    buffer: Buffer.from(markdown) });
  await dialog.getByRole('button', { name: '生成安全预览', exact: true }).click();
  await expect(dialog.getByText(slug, { exact: true })).toBeVisible({ timeout: 120_000 });
  await dialog.getByRole('button', { name: '固定版本并绑定', exact: true }).click();
  await expect(dialog).not.toBeVisible({ timeout: 120_000 });
  const state = await until(page, undefined, current => current.guidance_resources.filter(row => row.slug === slug
    && row.agent_id === value.agents.DTA.id && row.status === 'published').length === 1);
  const revision = one(state.guidance_resources, row => row.slug === slug && row.agent_id === value.agents.DTA.id);
  expect(revision.usage_mode).toBe('planning_guidance'); expect(revision.native_wait_contract).toBe(true);
  expect(text(revision.content_checksum)).toMatch(/^[a-f0-9]{64}$/);
  await criterion(info, 'original-ui-knowledge-guidance-fixed', { passed: true, skill_id: revision.skill_id,
    revision_id: revision.revision_id, agent_id: revision.agent_id, content_checksum: revision.content_checksum,
    source_markdown_sha256: hash(markdown), contains_answer_labels: false });
  return revision;
}
export async function send(page: Page, value: Initial, kind: Branch, message: string): Promise<ComposerReceipt> {
  /** 原Composer实际发送，保client_turn与server source不同身份。 */
  return sendComposerViaUi(page, { principal: value.owner.expected, employee: value.agents[kind],
    kind, message, model_config_id: value.model.id, client_timezone: value.date_labels.timezone });
}
export function execution(state: Facts, session: string): FactRow {
  /** 按原session唯一实例，禁止从最后一条实例推断本轮身份。 */
  return one(state.instances, row => row.session_id === session);
}
export function submission(state: Facts, instanceId: string): FactRow {
  /** 按原实例及冻结合成工具名称核唯一业务写，知识操作不能冒充提交。 */
  return one(state.operations, row => row.instance_id === instanceId
    && text(row.operation_name).split('@')[0] === 'f3_async_order');
}
export function isSafeDeclineTerminal(state: SafeDeclineFacts, kind: Branch,
  instanceId: string, operationId: string): boolean {
  /** 仅接受原失败Operation/Attempt、零效果且用户收到拒绝说明后的原实例终态。 */
  const instances = state.instances.filter(row => row.id === instanceId);
  const operations = state.operations.filter(row => row.id === operationId && row.instance_id === instanceId);
  const attempts = state.attempts.filter(row => row.operation_id === operationId);
  if (instances.length !== 1 || operations.length !== 1 || attempts.length !== 1) return false;
  const instance = instances[0]; const operation = operations[0]; const attempt = attempts[0];
  if (!instance || !operation || !attempt) return false;
  if (instance.kind !== (kind === 'SOP' ? 'sop' : 'dynamic_task')) return false;
  if (instance.status !== 'failed' && instance.status !== 'succeeded') return false;
  if (instance.status === 'succeeded' && kind === 'SOP' && instance.current_node_id !== 'done') return false;
  if (operation.status !== 'failed' || operation.effect_state !== 'none'
    || operation.error_code !== 'HTTP_ERROR' || attempt.status !== 'failed') return false;
  const terminalMessages = state.messages.filter(row => row.role === 'assistant'
    && row.execution_id === instanceId && row.execution_status === instance.status);
  if (terminalMessages.length !== 1) return false;
  const message = terminalMessages[0];
  if (!message || !Array.isArray(message.receipt_ids) || message.receipt_ids.length !== 0) return false;
  const facts = message.business_outcome;
  if (typeof facts !== 'object' || facts === null || Array.isArray(facts)) return false;
  const outcome = facts as Record<string, unknown>;
  return outcome.failure_notice === true && outcome.success_claim === false;
}
export function frozen(instance: FactRow, operation?: FactRow): Record<string, unknown> {
  /** 只比较不可变业务身份/版本/源，不把更新时间或当前节点当冻结字段。 */
  const projection: Record<string, unknown> = {};
  for (const key of ['id', 'kind', 'tenant_id', 'agent_id', 'initiator_user_id', 'session_id',
    'skill_version_id', 'definition_checksum', 'current_plan_revision_id', 'plan_checksum',
    'frozen_knowledge_versions', 'source_input_id', 'time_context']) projection[key] = instance[key];
  if (operation) for (const key of ['id', 'request_fingerprint', 'capability_checksum',
    'remote_idempotency_key_hash', 'request_json']) projection['operation_' + key] = operation[key];
  return projection;
}
export async function resolve(page: Page, session: string, item: FactRow,
  command: 'answer' | 'confirm_applied' | 'confirm_not_applied', comment: string): Promise<void> {
  /** 原待办UI提交原revision与证据，不直接构造Runtime决议或Signal。 */
  const originalInstance = execution(await facts(page, session), session);
  const pendingList = page.waitForResponse(response => new URL(response.url()).pathname === '/api/attention-items'
    && response.request().method() === 'GET' && new URL(response.url()).searchParams.get('view') === 'active');
  await page.goto('/enterprise/work-items');
  const listed: unknown = await (await pendingList).json();
  const items = rows(object(listed).items); const title = text(item.title);
  const same = items.filter(row => row.title === title); const index = same.findIndex(row => row.id === item.id);
  expect(index).toBeGreaterThanOrEqual(0);
  const cards = page.getByRole('button').filter({ has: page.getByText(title, { exact: true }) });
  await expect(cards).toHaveCount(same.length); await cards.nth(index).click();
  const dialog = page.getByRole('dialog'); await expect(dialog).toContainText(text(item.execution_id));
  await dialog.getByRole('textbox').fill(comment);
  const labels = { answer: '补充并继续', confirm_applied: '确认已送达', confirm_not_applied: '确认未送达' };
  const pending = page.waitForResponse(response => new URL(response.url()).pathname
    === `/api/attention-items/${text(item.id)}/resolve` && response.request().method() === 'POST');
  await dialog.getByRole('button', { name: labels[command], exact: true }).click();
  const received = await pending;
  if (received.status() !== 200) {
    let code: string | undefined;
    try { code = safeHttpErrorCode(await received.json()); } catch { /* 保留不含正文的状态诊断。 */ }
    throw new Error(`F3_ATTENTION_RESOLVE_HTTP_${received.status()}_${code ?? 'UNCLASSIFIED'}`);
  }
  const posted: unknown = received.request().postDataJSON(); const envelope = object(posted);
  expect(envelope.command).toBe(command); expect(envelope.expected_revision).toBe(item.revision);
  expect(envelope.comment).toBe(comment); expect(envelope.tenant_id).toBe(text(originalInstance.tenant_id));
  await expect(dialog).not.toBeVisible(); await page.goto('/workspace/chat/' + session);
}
export async function attention(page: Page, session: string, instanceId: string, kind: string): Promise<FactRow> {
  /** 原待办应精确关联实例与kind，已处理事项不能当新批准。 */
  const state = await until(page, session, value => value.attention.filter(row => row.execution_id === instanceId
    && row.kind === kind && ['offered', 'claimed'].includes(text(row.status))).length === 1);
  return one(state.attention, row => row.execution_id === instanceId && row.kind === kind
    && ['offered', 'claimed'].includes(text(row.status)));
}
export async function approve(page: Page, browser: Browser, journey: Journey, info: TestInfo): Promise<void> {
  /** 独立第二管理者真实登录，在原页面批准精确冻结操作，后置等原consumer写身份。 */
  const value = journey.initial; const instanceId = text(journey.instance.id);
  const item = await attention(page, journey.session, instanceId, 'tool_approval');
  const context = await browser.newContext({ baseURL: new URL(page.url()).origin });
  try {
    const reviewer = await context.newPage(); await login(reviewer, value.reviewer);
    const intent = { initiator: value.owner.expected, reviewer: value.reviewer.expected,
      employee: value.agents.DTA, attention_id: text(item.id) };
    let readCount = 0;
    const observed = await approveDtaViaUi(page, reviewer, intent, async () => {
      readCount += 1;
      if (readCount > 1) await until(page, journey.session, state => state.operations.some(row => row.id === journey.operation.id
        && row.approval_work_item_id === item.id && row.approved_by_user_id === value.reviewer.expected.id));
      return privateRequest(page, '/__e2e/f3/approval/' + text(item.id));
    });
    await criterion(info, 'second-admin-original-allow-once', { passed: true, instance_id: instanceId,
      operation_id: observed.operation_id, reviewer_id: value.reviewer.expected.id,
      approval_fingerprint: observed.approval_fingerprint, frozen_sha256: observed.frozen_sha256 });
  } finally { await context.close(); }
}

async function remoteRequest(page: Page, path: string, method: 'GET' | 'POST' | 'DELETE' = 'GET', control = false,
  data?: Record<string, unknown>): Promise<{ status: number; value: unknown }> {
  /** 真HTTP访问独立模拟PID；普通提交key不能触发控制，原值不进安全附件。 */
  if (!path.startsWith('/simulation/')) throw new Error('F3_REMOTE_PATH_REQUIRED');
  const provider = configuration().provider;
  const request = () => page.request.fetch(provider.origin + path, { method, data,
    timeout: 30_000,
    headers: control ? { 'X-Simulation-Control-Key': provider.control_key } : { 'X-Simulation-Key': provider.key } });
  const freezeObservation = path === '/simulation/observations'
    && process.env.F3_DIAGNOSTIC_SELECTION === 'memory-dta-freeze';
  const response = freezeObservation
    ? await withMemoryFreezeProviderObservationReadBudget(request)
    : await request();
  return withDisposedApiResponse(response, async response => {
    if (freezeObservation && !memoryProviderObservationResponseWithinByteLimit(response.headers())) {
      throw new Error('F3_MEMORY_PROVIDER_OBSERVATION_BODY_LIMIT');
    }
    const value: unknown = freezeObservation
      ? await boundedJsonBody(response, 'F3_MEMORY_PROVIDER_OBSERVATION_BODY_FAILED', 30_000)
      : await response.json();
    return { status: response.status(), value };
  });
}
export async function httpCount(page: Page): Promise<number> {
  /** provider真实POST计数含拒绝，task.submit_calls不能代替确认前HTTP零效果屏障。 */
  const response = await remoteRequest(page, '/simulation/observations'); expect(response.status).toBe(200);
  const observation = object(response.value);
  expect(observation.observation_healthy).toBe(true); expect(observation.observation_error_count).toBe(0);
  return integer(observation.submit_http_count);
}
export async function providerSubmitStatuses(page: Page): Promise<number[]> {
  /** 独立服务观察实际提交HTTP状态，操作账本的通用HTTP_ERROR不冒充供应商状态。 */
  const response = await remoteRequest(page, '/simulation/observations'); expect(response.status).toBe(200);
  const observation = object(response.value);
  expect(observation.observation_healthy).toBe(true); expect(observation.observation_error_count).toBe(0);
  return rows(observation.rows).filter(row => row.is_submit === true).map(row => integer(row.status));
}
export async function effect(page: Page, objectId: string): Promise<RemoteEffect | null> {
  /** 独立provider效果读回，accepted或清理后的404绝不当业务成功。 */
  const response = await remoteRequest(page, '/simulation/objects/' + objectId);
  if (response.status === 404) return null;
  expect(response.status).toBe(200); const body = object(response.value); expect(body.real_vendor_qualified).toBe(false);
  return { taskId: text(body.taskId), business: business(body.business), receipt_id: text(body.receipt_id),
    effect_count: integer(body.effect_count), request_count: integer(body.request_count), real_vendor_qualified: false };
}
export async function complete(page: Page, taskId: string): Promise<void> {
  /** 控制接口只完成本轮模拟远端effects，不写本项目Operation/Signal。 */
  const response = await remoteRequest(page, '/simulation/control/' + taskId + '/complete', 'POST', true, {});
  expect(response.status).toBe(200); expect(object(response.value).status).toBe('completed');
}
export async function callback(page: Page, taskId: string, operationId: string,
  eventId: string, revision: number): Promise<void> {
  /** 独立模拟服务使用原提交实际收到的token发原callback，测试不猜token或写Inbox。 */
  const response = await remoteRequest(page, '/simulation/control/' + taskId + '/callback', 'POST', true,
    { operation_id: operationId, provider_event_id: eventId, observation_revision: revision });
  expect(response.status).toBe(200);
  expect(object(response.value).callback_http_status).toBe(202);
}
export async function fault(page: Page): Promise<void> {
  /** 受控连接丢回执与真实供应商故障分列，先受理落盘再关闭连接。 */
  const response = await remoteRequest(page, '/simulation/control/fault', 'POST', true, { mode: 'drop_after_accept' });
  expect(response.status).toBe(200); expect(object(response.value).fault_armed).toBe(true);
}
export async function reconcile(page: Page, keyHash: string): Promise<RemoteTask> {
  /** 按原冻结remote key摘要查独立task，未知先对账，不重新提交。 */
  expect(keyHash).toMatch(/^[a-f0-9]{64}$/);
  const response = await remoteRequest(page, '/simulation/reconcile/' + keyHash); expect(response.status).toBe(200);
  const body = object(response.value); expect(body.real_vendor_qualified).toBe(false);
  return { taskId: text(body.taskId), status: text(body.status), request_count: integer(body.request_count),
    effect_count: integer(body.effect_count), real_vendor_qualified: false };
}
export async function cleanup(page: Page, objectId: string, info: TestInfo): Promise<void> {
  /** 仅清本轮已结算合成对象，再独立GET确认不存在，保幂等墓碑。 */
  const response = await remoteRequest(page, '/simulation/objects/' + objectId, 'DELETE');
  observeRemoteCleanupResponse(response.status, response.value);
  expect(await effect(page, objectId)).toBeNull();
  await criterion(info, 'remote-owned-object-cleanup', { passed: true, object_id: objectId, independent_absence: true });
}
export async function waitSdkQuiescent(page: Page): Promise<void> {
  /** 连续稳定读原已结算账本，含可选记忆观察器健康，不修改或猜测在途状态。 */
  let lastCount: number | undefined; let stable = 0;
  await expect.poll(async () => {
    const health = object(await privateRequest(page, '/__e2e/f3/sdk-health'));
    const sdk = object(health.original_sdk); const wire = object(health.wire_inputs);
    const memoryValid = health.memory_inputs === undefined || object(health.memory_inputs).valid === true;
    const count = integer(sdk.settled_count);
    if (sdkLedgerIsQuiescent(sdk) && wire.valid === true && memoryValid
      && lastCount === count) stable += 1;
    else stable = 0;
    lastCount = count;
    return stable >= 3;
  }, { timeout: 240_000, intervals: [1000] }).toBe(true);
}
export async function archivePersistentFacts(page: Page, info: TestInfo): Promise<void> {
  /** 页面仍存活时保留原只读身份映射；私有600原件供关停及双库清理后的独立核对。 */
  const runtime = configuration().runtime; const directory = lstatSync(runtime);
  if (!directory.isDirectory() || directory.isSymbolicLink() || (directory.mode & 0o077)
    || directory.uid !== process.getuid?.()) throw new Error('F3_PRIVATE_FACTS_DIRECTORY_REQUIRED');
  const key = hash({ title: info.title, retry: info.retry }).slice(0, 24);
  const path = join(runtime, `facts-after-case-${key}.private.json`);
  try {
    const original = await privateRequest(page, '/__e2e/f3/facts');
    writeFileSync(path, JSON.stringify({ case_title: info.title, test_status: info.status,
      original_facts: original }), { encoding: 'utf8', mode: 0o600, flag: 'wx' });
  } catch (error: unknown) {
    writeFileSync(join(runtime, `facts-after-case-${key}.error.safe.json`), JSON.stringify({
      case_title: info.title, valid: false, error_sha256: hash(String(error)) }),
    { encoding: 'utf8', mode: 0o600, flag: 'wx' });
    throw error;
  }
}
export async function restart(page: Page, session: string, info: TestInfo): Promise<void> {
  /** 原SDK实际结算稳定后才请求自有supervisor换PID；恢复后打开本轮真实session读回。 */
  await waitSdkQuiescent(page);
  const before = await facts(page, session);
  const control = join(configuration().runtime, 'control'); const directory = lstatSync(control);
  if (!directory.isDirectory() || directory.isSymbolicLink() || directory.uid !== process.getuid?.()
    || (directory.mode & 0o077)) throw new Error('F3_OWNED_RESTART_DIRECTORY_REQUIRED');
  writeFileSync(join(control, 'restart.request'), String(Date.now()), { encoding: 'utf8', mode: 0o600, flag: 'wx' });
  await expect.poll(async () => { try { return (await facts(page, session)).pid !== before.pid; }
    catch { return false; } }, { timeout: 120_000, intervals: [500, 1000, 2000] }).toBe(true);
  const after = await facts(page, session);
  await page.goto('/workspace/chat/' + encodeURIComponent(session));
  await criterion(info, 'actual-owned-app-new-pid', { passed: true, previous_pid: before.pid, pid: after.pid,
    session_id: session, provider_restarted: false });
}
export async function cancel(page: Page, journey: Journey, info: TestInfo): Promise<void> {
  /** 原执行卡提交取消CAS；只在旧revision被明确拒绝且零持久副作用时刷新并重试一次。 */
  const id = text(journey.instance.id);
  const tenant = text(journey.initial.owner.expected.tenant_id);
  const bearer = tokens.get(page); if (!bearer) throw new Error('F3_ACTUAL_LOGIN_TOKEN_REQUIRED');
  await page.goto('/workspace/chat/' + encodeURIComponent(journey.session));
  async function button(): Promise<Locator> {
    /** 每次提交都重新取原页面当前执行卡，避免重启前DOM引用被复用。 */
    await page.getByPlaceholder('输入消息，按 Enter 发送...').waitFor({ state: 'visible' });
    const card = page.getByLabel(journey.kind === 'SOP' ? 'SOP执行结果' : '动态任务控制', { exact: true }).last();
    const target = card.getByRole('button', { name: '取消任务', exact: true });
    await expect(target).toBeEnabled();
    return target;
  }
  async function persistedCommand(commandId: string): Promise<Record<string, unknown> | null> {
    /** 用原登录身份按本轮command id读回持久命令，404才表示没有写入。 */
    const response = await page.request.get(`/api/executions/${encodeURIComponent(id)}/commands/${encodeURIComponent(commandId)}`
      + `?tenant_id=${encodeURIComponent(tenant)}`, { headers: { Authorization: `Bearer ${bearer}` } });
    return withDisposedApiResponse(response, async response => {
      if (response.status() === 404) return null;
      if (response.status() !== 200
          || !diagnosticApiJsonResponseWithinByteLimit(response.headers())) {
        throw new Error('F3_CANCEL_COMMAND_READBACK_FAILED');
      }
      try {
        return object(await response.json());
      } catch (error) {
        throw new Error('F3_CANCEL_COMMAND_READBACK_FAILED', { cause: error });
      }
    });
  }
  async function issue(): Promise<{ response: Response;
    request: Record<string, unknown>; body: unknown }> {
    /** 点击一次原UI，原始body只在当前内存解析且仅导出revision及安全错误码。 */
    const target = await button();
    const pending = page.waitForResponse(response => new URL(response.url()).pathname === `/api/executions/${id}/commands`
      && response.request().method() === 'POST');
    await target.click();
    const response = await pending;
    const request: unknown = response.request().postDataJSON();
    const body: unknown = response.status() === 200
      ? await boundedPageJsonBody(page, response, 'F3_CANCEL_RESPONSE_INVALID')
      : await boundedPageJsonBody(page, response, 'F3_CANCEL_RESPONSE_INVALID').catch(() => undefined);
    return { response, request: object(request), body };
  }
  const before = await facts(page, journey.session);
  const beforeInstance = execution(before, journey.session);
  const first = await issue();
  expect(first.request.command_type).toBe('cancel');
  const firstCommandId = text(first.request.command_id);
  const firstExpectedRevision = integer(first.request.expected_revision);
  if (first.response.status() === 409) {
    const code = safeHttpErrorCode(first.body);
    const afterConflict = await facts(page, journey.session);
    const afterInstance = execution(afterConflict, journey.session);
    const persisted = await persistedCommand(firstCommandId);
    const related = (state: Facts, key: 'commands' | 'signals' | 'events'): number => state[key]
      .filter(row => row.execution_id === id || row.aggregate_id === id).length;
    const zeroSideEffects = persisted === null
      && related(afterConflict, 'commands') === related(before, 'commands')
      && related(afterConflict, 'signals') === related(before, 'signals')
      && related(afterConflict, 'events') === related(before, 'events')
      && afterInstance.status === 'waiting'
      && afterInstance.cancellation_disposition === 'none';
    const staleRevisionConfirmed = code === 'EXECUTION_REVISION_CONFLICT'
      && firstExpectedRevision < integer(afterInstance.revision);
    await criterion(info, 'cancel-revision-conflict-zero-effects-before-refresh', {
      passed: zeroSideEffects && staleRevisionConfirmed,
      http_status: first.response.status(), domain_code: code ?? null,
      page_expected_revision: firstExpectedRevision,
      authority_revision_before: integer(beforeInstance.revision),
      authority_revision_after: integer(afterInstance.revision),
      command_persisted: persisted !== null, command_id_sha256: hash(firstCommandId),
      command_delta: related(afterConflict, 'commands') - related(before, 'commands'),
      signal_delta: related(afterConflict, 'signals') - related(before, 'signals'),
      event_delta: related(afterConflict, 'events') - related(before, 'events'),
      execution_status: afterInstance.status,
      cancellation_disposition: afterInstance.cancellation_disposition,
    });
    expect(zeroSideEffects).toBe(true);
    expect(staleRevisionConfirmed).toBe(true);
    await page.reload();
    const refreshed = await facts(page, journey.session);
    const refreshedInstance = execution(refreshed, journey.session);
    expect(refreshedInstance.status).toBe('waiting');
    expect(refreshedInstance.cancellation_disposition).toBe('none');
    const retry = await issue();
    expect(retry.request.command_type).toBe('cancel');
    expect(retry.response.status()).toBe(200);
    expect(retry.request.command_id).not.toBe(firstCommandId);
    const freshExpectedRevision = integer(retry.request.expected_revision);
    expect(freshExpectedRevision).toBe(integer(refreshedInstance.revision));
    const freshCommandId = text(retry.request.command_id);
    const persistedAfterRetry = await persistedCommand(freshCommandId);
    expect(persistedAfterRetry).not.toBeNull();
    expect(persistedAfterRetry?.command_type).toBe('cancel');
    expect(persistedAfterRetry?.expected_revision).toBe(freshExpectedRevision);
    expect(persistedAfterRetry?.status).toBe('applied');
    await criterion(info, 'cancel-command-submitted-after-fresh-revision', {
      passed: true, first_rejection_code: code,
      rejected_revision: firstExpectedRevision, accepted_revision: freshExpectedRevision,
      refreshed_authority_revision: integer(refreshedInstance.revision),
      first_rejected_command_persisted: false,
      accepted_command_status: persistedAfterRetry?.status,
      accepted_command_id_sha256: hash(freshCommandId),
    });
  } else {
    expect(first.response.status()).toBe(200);
    const responseBody = object(first.body);
    expect(responseBody.command_id).toBe(firstCommandId);
    expect(responseBody.command_type).toBe('cancel');
    expect(responseBody.expected_revision).toBe(firstExpectedRevision);
    const persisted = await persistedCommand(firstCommandId);
    expect(persisted).not.toBeNull();
    expect(persisted?.command_type).toBe('cancel');
    expect(persisted?.expected_revision).toBe(firstExpectedRevision);
    expect(persisted?.status).toBe('applied');
    await criterion(info, 'cancel-command-submitted-on-current-state', {
      passed: true, accepted_revision: firstExpectedRevision,
      accepted_command_status: persisted?.status,
      accepted_command_id_sha256: hash(firstCommandId),
    });
  }
  const held = await until(page, journey.session, value => value.operations.some(row => row.id === journey.operation.id
    && row.status === 'unknown'));
  expect(execution(held, journey.session).cancellation_disposition).toBe('awaiting_reconciliation');
  expect(execution(held, journey.session).status).toBe('waiting');
}

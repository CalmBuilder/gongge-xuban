/**
 * @Time       : 2026/10/05
 * @Author     : zhanglp8181
 * @File       : final-f3-memory-helpers.ts
 * @CallChain  : 真登录/员工/Composer/记忆页/Attention → 原记忆消费者 → 精确私有GET与SDK
 * @Description: F3记忆13个原标注作用域的隔离辅助；观察只读原记录和实际SDK输入。
 */
import { createHash } from 'node:crypto';
import {
  closeSync, fsyncSync, fstatSync, lstatSync, openSync, readdirSync, realpathSync,
  statfsSync, unlinkSync, writeFileSync,
} from 'node:fs';
import { join } from 'node:path';
import { expect, type Page, type TestInfo } from '@playwright/test';
import { EnterpriseRoute } from '../src/enums/routes';
import {
  beginPageJsonBodyBudget, boundedPageJsonBody, boundedUiOperation,
  openEmployeeViaUi, sendComposerViaUi, type ComposerReceipt,
} from './final-f3-browser-driver';
import {
  criterion, hash, initial, integer, login, object, one, privateRequest, rows, scope, text,
  memoryFreezePrivateApiReadBudgetSnapshot, memoryFreezeProviderObservationReadBudgetSnapshot,
  verifyModel, waitSdkQuiescent,
  type Branch, type FactRow, type Initial,
} from './final-f3-jrn08-helpers';

export type MemoryCase = '1' | '2' | '3' | 'control' | 'freeze' | 'clear' | 'revoke';
export type MemoryEmployee = Initial['agents']['SOP'];
export type MemoryInitial = {
  base: Initial; agents: Record<Branch, Record<string, MemoryEmployee>>;
  labels: { name: string; style: string; large: string };
  memory_ids: Record<Branch, Record<string, { name: string; style: string; large: string }>>;
};
export type MemoryState = {
  pid: number; tenant_id: string; user_id: string; agent_id: string;
  executions: FactRow[]; sessions: FactRow[]; records: FactRow[]; jobs: FactRow[];
  messages: FactRow[]; attention: FactRow[]; sdk_calls: FactRow[];
  plan_revisions: FactRow[]; signals: FactRow[];
};
export type MemoryTurn = { session_id: string; source_id: string; client_turn_id: string;
  receipt: ComposerReceipt };

const MEMORY_CASES: Record<Branch, readonly MemoryCase[]> = {
  SOP: ['1', '2', '3', 'control', 'freeze', 'clear'],
  DTA: ['1', '2', '3', 'control', 'freeze', 'clear', 'revoke'],
};

export function shouldRunMemoryModelPreflight(preflight: boolean, frozenForRun: boolean): boolean {
  /** 全量回归沿用同一冻结模型预检；独立记忆诊断仍必须自行完成预检。 */
  return preflight && !frozenForRun;
}

export function safeMemoryManagementCriterion(agentId: string, largeRecordId: string,
  largeCharacterCount: number, largeSha256: string): Record<string, unknown> {
  /** 记忆管理页公开criteria只留员工/记录摘要及内容长度和摘要，不序列化原始私有ID。 */
  if (!agentId.trim() || !largeRecordId.trim() || !Number.isSafeInteger(largeCharacterCount)
    || largeCharacterCount < 0 || !/^[a-f0-9]{64}$/u.test(largeSha256)) {
    throw new Error('F3_MEMORY_MANAGEMENT_CRITERION_INVALID');
  }
  return {
    passed: true,
    agent_id_sha256: hash(agentId),
    large_record_id_sha256: hash(largeRecordId),
    large_character_count: largeCharacterCount,
    large_sha256: largeSha256,
    complete_management_page: true,
  };
}

export function safeMemoryRestartCriterion(previousPid: number, pid: number,
  sessionId: string): Record<string, unknown> {
  /** 真重启公开criteria保留PID变化和session摘要，不输出原始私有会话ID。 */
  if (!Number.isSafeInteger(previousPid) || previousPid <= 0 || !Number.isSafeInteger(pid)
    || pid <= 0 || pid === previousPid || !sessionId.trim()) {
    throw new Error('F3_MEMORY_RESTART_CRITERION_INVALID');
  }
  return { passed: true, previous_pid: previousPid, pid,
    session_id_sha256: hash(sessionId), provider_restarted: false };
}

const MEMORY_SNAPSHOT_STAGE_MAX_BYTES = 16 * 1024 * 1024;
const MEMORY_SNAPSHOT_FINAL_MAX_BYTES = 32 * 1024 * 1024;
const MEMORY_SNAPSHOT_TOTAL_MAX_BYTES = 64 * 1024 * 1024;
const MEMORY_SNAPSHOT_MINIMUM_FREE_BYTES = 128 * 1024 * 1024;
const MEMORY_SNAPSHOT_MAX_FILES = 128;
const MEMORY_SNAPSHOT_NAME = /^memory-(?:capture-stage-[a-f0-9]{24}-(?:correction|fresh)|facts-after-case-[a-f0-9]{24})\.private\.json$/u;

function measureJsonUtf8Bytes(value: unknown, maximumBytes: number): number {
  /** 只遍历JSON原始值并短路超限，不先创建完整序列化字符串或大字节副本。 */
  let bytes = 0;
  let nodes = 0;
  const ancestors = new Set<object>();
  const add = (count: number): void => {
    bytes += count;
    if (bytes > maximumBytes) throw new Error('F3_MEMORY_PRIVATE_SNAPSHOT_FILE_LIMIT');
  };
  const string = (text: string): void => {
    if (text.length + 2 > maximumBytes - bytes) {
      throw new Error('F3_MEMORY_PRIVATE_SNAPSHOT_FILE_LIMIT');
    }
    add(2);
    for (const character of text) {
      const code = character.codePointAt(0) ?? 0;
      if (character === '"' || character === '\\' || code === 0x08 || code === 0x09
        || code === 0x0a || code === 0x0c || code === 0x0d) {
        add(2);
      } else if (code < 0x20 || (code >= 0xd800 && code <= 0xdfff)) {
        add(6);
      } else {
        add(Buffer.byteLength(character, 'utf8'));
      }
    }
  };
  const visit = (current: unknown, depth: number, arrayValue = false): boolean => {
    nodes += 1;
    if (nodes > 500_000 || depth > 128) {
      throw new Error('F3_MEMORY_PRIVATE_SNAPSHOT_COMPLEXITY_LIMIT');
    }
    if (current === null) {
      add(4);
      return true;
    }
    if (typeof current === 'string') {
      string(current);
      return true;
    }
    if (typeof current === 'boolean') {
      add(current ? 4 : 5);
      return true;
    }
    if (typeof current === 'number') {
      add(Buffer.byteLength(JSON.stringify(current) ?? 'null', 'utf8'));
      return true;
    }
    if (typeof current === 'undefined' || typeof current === 'function'
      || typeof current === 'symbol') {
      if (arrayValue) add(4);
      return arrayValue;
    }
    if (typeof current === 'bigint' || typeof current !== 'object') {
      throw new Error('F3_MEMORY_PRIVATE_SNAPSHOT_VALUE_INVALID');
    }
    if (ancestors.has(current)) throw new Error('F3_MEMORY_PRIVATE_SNAPSHOT_VALUE_INVALID');
    const prototype = Object.getPrototypeOf(current);
    if (Array.isArray(current)) {
      if (prototype !== Array.prototype || Object.getOwnPropertyDescriptor(current, 'toJSON')?.value !== undefined
        || current.length * 2 + 1 > maximumBytes - bytes) {
        throw new Error('F3_MEMORY_PRIVATE_SNAPSHOT_VALUE_INVALID');
      }
      ancestors.add(current);
      add(1);
      for (let index = 0; index < current.length; index += 1) {
        if (index > 0) add(1);
        const descriptor = Object.getOwnPropertyDescriptor(current, String(index));
        if (!descriptor) add(4);
        else if (!('value' in descriptor) || descriptor.get || descriptor.set) {
          throw new Error('F3_MEMORY_PRIVATE_SNAPSHOT_VALUE_INVALID');
        } else visit(descriptor.value, depth + 1, true);
      }
      add(1);
      ancestors.delete(current);
      return true;
    }
    if (prototype !== Object.prototype && prototype !== null) {
      throw new Error('F3_MEMORY_PRIVATE_SNAPSHOT_VALUE_INVALID');
    }
    const toJsonDescriptor = Object.getOwnPropertyDescriptor(current, 'toJSON');
    if (toJsonDescriptor && (!('value' in toJsonDescriptor) || typeof toJsonDescriptor.value === 'function')) {
      throw new Error('F3_MEMORY_PRIVATE_SNAPSHOT_VALUE_INVALID');
    }
    ancestors.add(current);
    add(1);
    let first = true;
    for (const key in current) {
      if (!Object.prototype.hasOwnProperty.call(current, key)) continue;
      const descriptor = Object.getOwnPropertyDescriptor(current, key);
      if (!descriptor || !('value' in descriptor) || descriptor.get || descriptor.set) {
        throw new Error('F3_MEMORY_PRIVATE_SNAPSHOT_VALUE_INVALID');
      }
      if (key === 'toJSON' && typeof descriptor.value === 'function') {
        throw new Error('F3_MEMORY_PRIVATE_SNAPSHOT_VALUE_INVALID');
      }
      if (typeof descriptor.value === 'undefined' || typeof descriptor.value === 'function'
        || typeof descriptor.value === 'symbol') continue;
      if (!first) add(1);
      string(key);
      add(1);
      visit(descriptor.value, depth + 1);
      first = false;
    }
    add(1);
    ancestors.delete(current);
    return true;
  };
  if (!visit(value, 0)) throw new Error('F3_MEMORY_PRIVATE_SNAPSHOT_VALUE_INVALID');
  return bytes;
}

export type PrivateMemorySnapshotLimits = Readonly<{
  maxFileBytes?: number; maxTotalBytes?: number; minimumFreeBytes?: number;
}>;

export function writePrivateMemorySnapshot(
  runtime: string, filename: string, value: unknown, limits: PrivateMemorySnapshotLimits = {},
): void {
  /** 以独占600文件写入有界快照；运行累计、tmpfs余量和竞争写入均先核验。 */
  if (!MEMORY_SNAPSHOT_NAME.test(filename)) throw new Error('F3_MEMORY_PRIVATE_SNAPSHOT_NAME_INVALID');
  const directory = lstatSync(runtime);
  if (realpathSync(runtime) !== runtime || !directory.isDirectory() || directory.isSymbolicLink()
    || directory.uid !== process.getuid?.() || (directory.mode & 0o077)) {
    throw new Error('F3_MEMORY_PRIVATE_SNAPSHOT_DIRECTORY_REQUIRED');
  }
  const isStage = filename.startsWith('memory-capture-stage-');
  const maximumFileBytes = Math.min(limits.maxFileBytes ??
    (isStage ? MEMORY_SNAPSHOT_STAGE_MAX_BYTES : MEMORY_SNAPSHOT_FINAL_MAX_BYTES),
  isStage ? MEMORY_SNAPSHOT_STAGE_MAX_BYTES : MEMORY_SNAPSHOT_FINAL_MAX_BYTES);
  const maximumTotalBytes = Math.min(limits.maxTotalBytes ?? MEMORY_SNAPSHOT_TOTAL_MAX_BYTES,
    MEMORY_SNAPSHOT_TOTAL_MAX_BYTES);
  const minimumFreeBytes = limits.minimumFreeBytes ?? MEMORY_SNAPSHOT_MINIMUM_FREE_BYTES;
  if (![maximumFileBytes, maximumTotalBytes, minimumFreeBytes].every(Number.isSafeInteger)
    || maximumFileBytes < 0 || maximumTotalBytes < 0 || minimumFreeBytes < 0
    || minimumFreeBytes > MEMORY_SNAPSHOT_MINIMUM_FREE_BYTES) {
    throw new Error('F3_MEMORY_PRIVATE_SNAPSHOT_LIMIT_INVALID');
  }
  const measuredBytes = measureJsonUtf8Bytes(value, maximumFileBytes);
  const serialized = JSON.stringify(value);
  if (typeof serialized !== 'string') throw new Error('F3_MEMORY_PRIVATE_SNAPSHOT_VALUE_INVALID');
  const encodedBytes = Buffer.byteLength(serialized, 'utf8');
  if (encodedBytes !== measuredBytes) throw new Error('F3_MEMORY_PRIVATE_SNAPSHOT_MEASURE_MISMATCH');
  if (encodedBytes > maximumFileBytes) throw new Error('F3_MEMORY_PRIVATE_SNAPSHOT_FILE_LIMIT');

  const lockPath = join(runtime, '.memory-snapshot-budget.lock');
  let lockDescriptor = -1;
  let outputDescriptor = -1;
  let lockCreated = false;
  let outputCreated = false;
  let outputPath: string | undefined;
  try {
    lockDescriptor = openSync(lockPath, 'wx', 0o600);
    lockCreated = true;
    const lock = fstatSync(lockDescriptor);
    if (!lock.isFile() || lock.uid !== process.getuid?.() || lock.nlink !== 1
      || (lock.mode & 0o077) || lock.size !== 0) {
      throw new Error('F3_MEMORY_PRIVATE_SNAPSHOT_LOCK_INVALID');
    }
    const existing = readdirSync(runtime).filter(name => MEMORY_SNAPSHOT_NAME.test(name));
    if (existing.length >= MEMORY_SNAPSHOT_MAX_FILES) {
      throw new Error('F3_MEMORY_PRIVATE_SNAPSHOT_COUNT_LIMIT');
    }
    let totalBytes = 0;
    for (const name of existing) {
      const info = lstatSync(join(runtime, name));
      if (!info.isFile() || info.isSymbolicLink() || info.uid !== process.getuid?.()
        || info.nlink !== 1 || (info.mode & 0o077)) {
        throw new Error('F3_MEMORY_PRIVATE_SNAPSHOT_FILE_INVALID');
      }
      totalBytes += info.size;
    }
    if (totalBytes + encodedBytes > maximumTotalBytes) {
      throw new Error('F3_MEMORY_PRIVATE_SNAPSHOT_TOTAL_LIMIT');
    }
    const fs = statfsSync(runtime);
    if (fs.bavail * fs.bsize - encodedBytes < minimumFreeBytes) {
      throw new Error('F3_MEMORY_PRIVATE_SNAPSHOT_FREE_SPACE_LIMIT');
    }
    outputPath = join(runtime, filename);
    outputDescriptor = openSync(outputPath, 'wx', 0o600);
    outputCreated = true;
    const output = fstatSync(outputDescriptor);
    if (!output.isFile() || output.uid !== process.getuid?.() || output.nlink !== 1
      || (output.mode & 0o077) || output.size !== 0) {
      throw new Error('F3_MEMORY_PRIVATE_SNAPSHOT_FILE_INVALID');
    }
    writeFileSync(outputDescriptor, serialized, { encoding: 'utf8' });
    fsyncSync(outputDescriptor);
    closeSync(outputDescriptor);
    outputDescriptor = -1;
  } catch (error) {
    if (outputDescriptor >= 0) closeSync(outputDescriptor);
    if (outputCreated && outputPath) unlinkSync(outputPath);
    throw error;
  } finally {
    if (lockDescriptor >= 0) closeSync(lockDescriptor);
    if (lockCreated) unlinkSync(lockPath);
  }
}

export function contentHash(value: string): string {
  /** 与原MemoryCandidate的UTF8正文SHA一致，不把JSON引号加入摘要。 */
  return createHash('sha256').update(value).digest('hex');
}
export function isCapturePollTimeout(error: unknown, timeoutMs: number): boolean {
  /** 只识别Playwright expect.poll自身的精确截止错误，不按耗时猜测异常类型。 */
  if (!(error instanceof Error) || error.constructor.name !== 'ExpectError') return false;
  const timeoutLine = `- Timeout ${timeoutMs}ms exceeded while waiting on the predicate`;
  return error.message.split(/\r?\n/u).some((line) => line.trim() === timeoutLine);
}
export async function memoryInitial(page: Page): Promise<MemoryInitial> {
  /** 只解析私有bootstrap的事前合成资源，不输出账号凭据或整份配置。 */
  const base = await initial(page);
  const root = object(await privateRequest(page, '/__e2e/f3/bootstrap'));
  const quality = object(root.memory_quality); const agents = object(quality.agents);
  const ids = object(quality.memory_ids); const labels = object(quality.labels);
  const employeeIds = new Set<string>();
  function employees(branch: Branch): Record<string, MemoryEmployee> {
    /** 原发布员工名称含实际owner后缀，freeze/revoke不能借发起人伪装创建者。 */
    const items = object(agents[branch]); const expectedCases = [...MEMORY_CASES[branch]];
    expect(Object.keys(items).sort()).toEqual(expectedCases.sort());
    const result: Record<string, MemoryEmployee> = {};
    for (const [key, value] of Object.entries(items)) {
      const item = object(value); const id = text(item.id); const name = text(item.name);
      expect(employeeIds.has(id)).toBe(false); employeeIds.add(id);
      expect(item.tenant_id).toBe(base.owner.expected.tenant_id);
      const owner = ['freeze', 'revoke'].includes(key) ? base.reviewer.expected.id : base.owner.expected.id;
      expect(item.gallery_label).toBe(name + ' @' + owner);
      result[key] = { id, name, tenant_id: base.owner.expected.tenant_id, gallery_label: text(item.gallery_label) };
    }
    return result;
  }
  function memoryIds(branch: Branch): Record<string, { name: string; style: string; large: string }> {
    /** 固定ID来自首次原资源创建，不按模型输出或记录排序猜预期。 */
    const result: Record<string, { name: string; style: string; large: string }> = {};
    for (const [key, value] of Object.entries(object(ids[branch]))) {
      const item = object(value); result[key] = { name: text(item.name), style: text(item.style), large: text(item.large) };
    }
    return result;
  }
  expect(labels.name).toBe('青禾');
  expect(labels.style).toBe('输出偏好：简洁要点；原样保留代码 a  b\n\t校验结束。');
  expect(labels.large).toBe('口令：F3-UNSELECTED-SECRET。' + '完整长事实。'.repeat(700));
  const result = { base, agents: { SOP: employees('SOP'), DTA: employees('DTA') },
    labels: { name: text(labels.name), style: text(labels.style), large: text(labels.large) },
    memory_ids: { SOP: memoryIds('SOP'), DTA: memoryIds('DTA') } };
  expect(employeeIds.size).toBe(13); return result;
}
export function employee(value: MemoryInitial, branch: Branch, name: MemoryCase): MemoryEmployee {
  /** 缺资源必须失败，不能用其他员工补位。 */
  const item = value.agents[branch][name]; if (!item) throw new Error('F3_MEMORY_CASE_MISSING'); return item;
}
export async function memoryState(page: Page, value: MemoryInitial, branch: Branch, name: MemoryCase,
  session?: string): Promise<MemoryState> {
  /** 精确只读当前13作用域中的本人，外部身份或缺集合均不返回假空状态。 */
  const agent = employee(value, branch, name);
  const input = object(await privateRequest(page, '/__e2e/f3/memory/' + encodeURIComponent(agent.id)
    + (session ? '?session_id=' + encodeURIComponent(session) : '')));
  expect(input.tenant_id).toBe(value.base.owner.expected.tenant_id);
  expect(input.user_id).toBe(value.base.owner.expected.id); expect(input.agent_id).toBe(agent.id);
  return { pid: integer(input.pid), tenant_id: text(input.tenant_id), user_id: text(input.user_id),
    agent_id: text(input.agent_id), executions: rows(input.executions), sessions: rows(input.sessions),
    records: rows(input.records), jobs: rows(input.jobs), messages: rows(input.messages),
    attention: rows(input.attention), sdk_calls: rows(input.sdk_calls),
    plan_revisions: rows(input.plan_revisions), signals: rows(input.signals) };
}
export async function startMemory(page: Page, info: TestInfo, branch: Branch, name: MemoryCase,
  preflight = false): Promise<MemoryInitial> {
  /** 使用当前真实登录和员工广场；全量回归复用同轮冻结预检，独立诊断仍现场预检。 */
  const value = await memoryInitial(page);
  if (branch === 'DTA' && name === 'freeze') beginPageJsonBodyBudget(page, 240_000, 20);
  await login(page, value.base.owner);
  if (shouldRunMemoryModelPreflight(
    preflight,
    process.env.F3_MODEL_PREFLIGHT_ALREADY_FROZEN === '1',
  )) await verifyModel(page, value.base, info);
  const item = employee(value, branch, name); await openEmployeeViaUi(page, item, item.gallery_label);
  return value;
}
export async function newMemoryDraft(page: Page, value: MemoryInitial, branch: Branch,
  name: MemoryCase): Promise<void> {
  /** 新来源只从员工广场原发起对话获得草稿，不直接创建会话或填source。 */
  const item = employee(value, branch, name); await openEmployeeViaUi(page, item, item.gallery_label);
}
export async function sendMemory(page: Page, value: MemoryInitial, branch: Branch,
  name: MemoryCase, message: string): Promise<MemoryTurn> {
  /** 原Composer发送后以持久client_turn确认来源；动态任务SSE可按合同保持打开。 */
  const receipt = await sendComposerViaUi(page, { principal: value.base.owner.expected,
    employee: employee(value, branch, name), kind: branch, message,
    model_config_id: value.base.model.id, client_timezone: value.base.date_labels.timezone });
  let source: FactRow | undefined;
  await expect.poll(async () => {
    const state = await memoryState(page, value, branch, name);
    const matches = state.messages.filter(row => row.role === 'user' && row.client_turn_id === receipt.client_turn_id);
    if (matches.length === 1) source = matches[0]; return matches.length;
  }, { timeout: 120_000, intervals: [300, 1000, 2000] }).toBe(1);
  if (!source) throw new Error('F3_MEMORY_EXACT_SOURCE_MISSING');
  return { session_id: text(source.session_id), source_id: text(source.id), client_turn_id: receipt.client_turn_id, receipt };
}
export async function sendMemoryInSession(page: Page, value: MemoryInitial, branch: Branch,
  name: MemoryCase, sessionId: string, message: string): Promise<MemoryTurn> {
  /** 原SOP等待轮沿同一session发送确认，避免把Attention专用恢复误用于SOP。 */
  await page.goto('/workspace/chat/' + encodeURIComponent(sessionId));
  await page.getByPlaceholder('输入消息，按 Enter 发送...').waitFor({ state: 'visible' });
  const receipt = await sendComposerViaUi(page, { principal: value.base.owner.expected,
    employee: employee(value, branch, name), kind: branch, message,
    model_config_id: value.base.model.id, client_timezone: value.base.date_labels.timezone });
  let source: FactRow | undefined;
  await expect.poll(async () => {
    const state = await memoryState(page, value, branch, name, sessionId);
    const matches = state.messages.filter(row => row.role === 'user'
      && row.session_id === sessionId && row.client_turn_id === receipt.client_turn_id);
    if (matches.length === 1) source = matches[0]; return matches.length;
  }, { timeout: 120_000, intervals: [300, 1000, 2000] }).toBe(1);
  if (!source) throw new Error('F3_MEMORY_EXACT_SESSION_SOURCE_MISSING');
  return { session_id: sessionId, source_id: text(source.id), client_turn_id: receipt.client_turn_id, receipt };
}
export function memoryExecution(state: MemoryState, value: MemoryInitial, branch: Branch,
  name: MemoryCase, session: string): FactRow {
  /** 原实例必须精确kind/tenant/agent/initiator/session，不能用最近实例判完成。 */
  const item = one(state.executions, row => row.session_id === session);
  expect(item.kind).toBe(branch === 'SOP' ? 'sop' : 'dynamic_task');
  expect(item.tenant_id).toBe(value.base.owner.expected.tenant_id);
  expect(item.agent_id).toBe(employee(value, branch, name).id);
  expect(item.initiator_user_id).toBe(value.base.owner.expected.id);
  if (branch === 'SOP') {
    expect(text(item.skill_version_id)).toBeTruthy(); expect(text(item.definition_checksum)).toMatch(/^[a-f0-9]{64}$/);
  } else {
    expect(text(item.current_plan_revision_id)).toBeTruthy(); expect(text(item.plan_checksum)).toMatch(/^[a-f0-9]{64}$/);
  }
  return item;
}
export async function waitMemory(page: Page, value: MemoryInitial, branch: Branch, name: MemoryCase,
  turn: MemoryTurn, expected: 'waiting' | 'succeeded'): Promise<MemoryState> {
  /** 先等待原终态或真实等待；错误终态立即失败，不把失败耗时当成功恢复。 */
  await expect.poll(async () => {
    const current = await memoryState(page, value, branch, name, turn.session_id);
    const matches = current.executions.filter(row => row.session_id === turn.session_id);
    if (matches.length !== 1) return '';
    return text(matches[0]?.status);
  }, { timeout: 300_000, intervals: [500, 1000, 2000] }).toMatch(expected === 'waiting'
    ? /^(waiting|succeeded|failed|cancelled|timed_out)$/ : /^(succeeded|failed|cancelled|timed_out)$/);
  const current = await memoryState(page, value, branch, name, turn.session_id);
  expect(memoryExecution(current, value, branch, name, turn.session_id).status).toBe(expected);
  return current;
}
export function sdkCalls(state: MemoryState, branch: Branch, source: string): FactRow[] {
  /** 对应原Router/真正DTA answer的completed实际SDK与独立账本必须都存在。 */
  const operation = branch === 'SOP' ? 'router.scene' : 'dynamic_task.answer';
  const actual = state.sdk_calls.filter(row => row.source_input_id === source && row.operation === operation
    && row.status === 'completed' && row.actual_provider_call === true && row.actual_sdk_transport === true);
  expect(actual.length).toBeGreaterThan(0);
  for (const row of actual) {
    expect(row.original_sdk_receipt_found).toBe(true); expect(row.memory_binding_valid).toBe(true);
    expect(row.persistent_binding_valid).toBe(true);
    expect(row.sidecar_absent).toBe(true); expect(integer(row.request_estimate)).toBeLessThanOrEqual(128000);
    expect(integer(object(row.usage).prompt_tokens)).toBeGreaterThan(0);
    expect(rows(row.selected).every(item => item.complete_in_sdk === true)).toBe(true);
  }
  return actual;
}
export function labeledFields(markdown: string): Record<string, string> {
  /** 原SD6A完整字段解析保留冲突拒绝，六自然质量不能用substring通过。 */
  const matches = [...markdown.matchAll(/\b(NAME|STYLE|CODE)\b[\s*`]*=[\s*`]*([^\r\n]+?)(?=(?:\s+(?:与|and)\s+|[，,、；;]\s*|\s+)[*`]*(?:NAME|STYLE|CODE)[\s*`]*=|[\r\n]|$)/g),
    ...markdown.matchAll(/^\s*\|[\s*`]*(NAME|STYLE|CODE)[\s*`]*\|[\s*`]*([^|\r\n]+)\|/gm)];
  const result: Record<string, string> = {};
  for (const match of matches) {
    const key = text(match[1]); const content = text(match[2]).trim().replace(/[\s*`]+$/, '');
    result[key] = key in result && result[key] !== content ? '__CONFLICTING_LABEL_VALUES__' : content;
  }
  return result;
}

export function hasAllUnknownMemoryLabels(labels: Record<string, string>): boolean {
  /** 对清空或撤权后的原冻结答案合同，要求姓名、格式、口令都精确标为未知。 */
  return ['NAME', 'STYLE', 'CODE'].every(key => labels[key] === '未知');
}

export function safeMemoryAnswerQualityObservation(
  answer: { labels: Record<string, string>; unselected_secret_present: boolean; revoked_name_present: boolean },
): Record<string, unknown> {
  /** 区分未知占位与格式偏差且不把真实回答值写入公开证据。 */
  const classify = (
    value: string | undefined,
  ): 'missing' | 'unknown' | 'unknown_with_punctuation' | 'unknown_with_extra_text' | 'conflicting' | 'present' => {
    if (value === undefined) return 'missing';
    if (value === '__CONFLICTING_LABEL_VALUES__') return 'conflicting';
    const normalized = value.trim();
    if (normalized === '未知') return 'unknown';
    if (/^未知[\s。.!！?？,，、;；:：()（）\[\]【】"“”‘’`*_—-]*$/u.test(normalized)) {
      return 'unknown_with_punctuation';
    }
    if (normalized.includes('未知')) return 'unknown_with_extra_text';
    return 'present';
  };
  return {
    label_classes: {
      NAME: classify(answer.labels.NAME),
      STYLE: classify(answer.labels.STYLE),
      CODE: classify(answer.labels.CODE),
    },
    revoked_name_present: answer.revoked_name_present === true,
    unselected_secret_present: answer.unselected_secret_present === true,
  };
}

export function findSopResumedAnswerMessage(messages: FactRow[], executionId: string,
  originalSourceId: string): FactRow {
  /**
   * SOP等待轮的最终回复由确认轮触发；它仍必须绑定原实例、冻结版本和真实确认用户轮。
   * 这里不把 resumed turn 误当成实例 source_ref，也不把缺少普通模型 result_id 的
   * 对话式终态伪装成动态任务回执。
   */
  if (!executionId.trim() || !originalSourceId.trim()) {
    throw new Error('F3_MEMORY_SOP_RESUMED_ID_REQUIRED');
  }
  expect(messages.filter(row => row.role === 'user' && row.id === originalSourceId)).toHaveLength(1);
  const exact = messages.filter(row => {
    if (row.role !== 'assistant') return false;
    const metadata = object(row.metadata || {});
    const source = object(metadata.feedback_source || {});
    return metadata.execution_id === executionId
      && source.policy_version === 'feedback_source_v1'
      && source.branch === 'sop'
      && source.execution_id === executionId
      && source.execution_status === 'succeeded'
      && typeof source.skill_version_id === 'string' && source.skill_version_id.length > 0
      && typeof source.definition_checksum === 'string'
      && /^[a-f0-9]{64}$/u.test(source.definition_checksum)
      && source.turn_id === (metadata.turn_id || metadata.user_message_id)
      && typeof source.turn_id === 'string' && source.turn_id.length > 0;
  });
  expect(exact).toHaveLength(1);
  const result = exact[0];
  if (!result) throw new Error('F3_MEMORY_SOP_RESUMED_MESSAGE_MISSING');
  const metadata = object(result.metadata || {});
  const resumedTurnId = text(metadata.turn_id || metadata.user_message_id);
  expect(messages.filter(row => row.role === 'user' && row.id === resumedTurnId)).toHaveLength(1);
  return result;
}

async function sessionMessages(page: Page, value: MemoryInitial, turn: MemoryTurn): Promise<FactRow[]> {
  /** 只读当前会话消息并限制响应正文预算，正文只在本地私有内存短暂存在。 */
  const tenant = value.base.owner.expected.tenant_id;
  const path = `/api/chat/sessions/${encodeURIComponent(turn.session_id)}/messages`;
  const pending = page.waitForResponse(response => {
    /** 只接收本页真实授权读取的原会话消息响应，正文随后计入共享预算。 */
    const url = new URL(response.url());
    return response.request().method() === 'GET' && url.pathname === path
      && url.searchParams.get('tenant_id') === tenant;
  }, { timeout: 30_000 });
  const [messageResponse, status] = await Promise.all([
    pending,
    boundedUiOperation(page.evaluate(async ({ tenantId, sessionId }) => {
      /** 从当前登录态发起原消息GET，仅把状态返回节点侧以限制原文正文读回位置。 */
      const auth: unknown = JSON.parse(localStorage.getItem('gongge_auth') || '{}');
      if (typeof auth !== 'object' || auth === null || !('token' in auth) || typeof auth.token !== 'string') {
        throw new Error('F3_MEMORY_ACTUAL_AUTH_REQUIRED');
      }
      const controller = new AbortController();
      const timeout = window.setTimeout(() => controller.abort(), 30_000);
      try {
        const result = await fetch(`/api/chat/sessions/${encodeURIComponent(sessionId)}/messages?tenant_id=${encodeURIComponent(tenantId)}`,
          { headers: { Authorization: 'Bearer ' + auth.token }, signal: controller.signal });
        return result.status;
      } finally { window.clearTimeout(timeout); }
    }, { tenantId: tenant, sessionId: turn.session_id }),
    30_000, 'F3_MEMORY_MESSAGE_READ_TIMEOUT'),
  ]);
  expect(status).toBe(200);
  expect(messageResponse.status()).toBe(200);
  return rows(await boundedPageJsonBody(page, messageResponse, 'F3_MEMORY_MESSAGE_READ_FAILED'));
}

export function sopLabels(markdown: string): Record<string, string> {
  /** SOP答案使用小写业务槽位；归一化到与DTA相同的安全字段名供合同断言。 */
  const labels: Record<string, string> = {};
  const names: Record<string, string> = {
    user_name: 'NAME', '用户名称': 'NAME', '用户姓名': 'NAME', '姓名': 'NAME',
    reply_style: 'STYLE', '回复风格': 'STYLE', '输出偏好': 'STYLE',
    omitted_code: 'CODE', '省略代码': 'CODE', '口令': 'CODE',
  };
  for (const [rawName, key] of Object.entries(names)) {
    const match = markdown.match(new RegExp(
      `${rawName}\\s*(?:[：:]\\s*|为\\s*)([^，,。；;\\r\\n]+)`, 'u',
    ));
    if (match?.[1]) labels[key] = match[1].trim();
  }
  return labels;
}

function answerFromMessage(page: Page, result: FactRow, sop = false): Promise<{
  id: string; labels: Record<string, string>; sha256: string;
  unselected_secret_present: boolean; revoked_name_present: boolean;
}> {
  /** 读取已验真 assistant 正文并只返回字段摘要，避免把正文写入标准输出或附件。 */
  const content = text(result.content);
  return (async () => {
    await page.getByRole('main').waitFor({ state: 'visible' });
    const labels = sop ? { ...labeledFields(content), ...sopLabels(content) } : labeledFields(content);
    return { id: text(result.id), labels, sha256: contentHash(content),
      unselected_secret_present: content.includes('F3-UNSELECTED-SECRET'), revoked_name_present: content.includes('青禾') };
  })();
}

export async function originalAnswer(page: Page, value: MemoryInitial, turn: MemoryTurn,
  executionId: string): Promise<{ id: string; labels: Record<string, string>; sha256: string;
    unselected_secret_present: boolean; revoked_name_present: boolean }> {
  /** 普通终态要求当前用户source和结果回执完全同轮，DTA及非恢复SOP沿用原合同。 */
  const messages = await sessionMessages(page, value, turn);
  const exact = messages.filter(row => {
    if (row.role !== 'assistant') return false;
    const metadata = object(row.metadata || {});
    const frozenSource = object(metadata.feedback_source || {});
    return metadata.execution_id === executionId
      && typeof metadata.result_id === 'string' && frozenSource.execution_id === executionId
      && frozenSource.execution_status === 'succeeded' && frozenSource.turn_id === turn.source_id
      && (metadata.user_message_id === turn.source_id || metadata.turn_id === turn.source_id);
  });
  expect(exact).toHaveLength(1);
  const result = exact[0]; if (!result) throw new Error('F3_MEMORY_FINAL_MESSAGE_MISSING');
  await page.goto('/workspace/chat/' + turn.session_id);
  await expect(page.locator('main')).toContainText('NAME');
  return answerFromMessage(page, result);
}

export async function sopResumedAnswer(page: Page, value: MemoryInitial, turn: MemoryTurn,
  executionId: string): Promise<{ id: string; labels: Record<string, string>; sha256: string;
    unselected_secret_present: boolean; revoked_name_present: boolean }> {
  /** SOP恢复轮允许确认消息成为回复turn，但必须仍指向同一原实例和冻结版本。 */
  const result = findSopResumedAnswerMessage(await sessionMessages(page, value, turn), executionId, turn.source_id);
  await page.goto('/workspace/chat/' + turn.session_id);
  return answerFromMessage(page, result, true);
}

export async function sopAnswer(page: Page, value: MemoryInitial, turn: MemoryTurn,
  executionId: string): Promise<{ id: string; labels: Record<string, string>; sha256: string;
    unselected_secret_present: boolean; revoked_name_present: boolean }> {
  /** SOP正常终态绑定当前source和冻结版本；其对话回复不强行要求DTA result_id。 */
  const messages = await sessionMessages(page, value, turn);
  const exact = messages.filter(row => {
    if (row.role !== 'assistant') return false;
    const metadata = object(row.metadata || {});
    const source = object(metadata.feedback_source || {});
    return metadata.execution_id === executionId
      && source.policy_version === 'feedback_source_v1' && source.branch === 'sop'
      && source.execution_id === executionId && source.execution_status === 'succeeded'
      && source.turn_id === turn.source_id
      && typeof source.skill_version_id === 'string' && source.skill_version_id.length > 0
      && typeof source.definition_checksum === 'string'
      && /^[a-f0-9]{64}$/u.test(source.definition_checksum)
      && (metadata.user_message_id === turn.source_id || metadata.turn_id === turn.source_id);
  });
  expect(exact).toHaveLength(1);
  const result = exact[0]; if (!result) throw new Error('F3_MEMORY_SOP_FINAL_MESSAGE_MISSING');
  await page.goto('/workspace/chat/' + turn.session_id);
  return answerFromMessage(page, result, true);
}
export async function managementFull(page: Page, value: MemoryInitial, branch: Branch,
  name: MemoryCase, info: TestInfo): Promise<void> {
  /** 原记忆页详情逐字读完整大事实，管理读取不跟模型预算裁剪。 */
  const selected = employee(value, branch, name);
  const employeeList = page.waitForResponse(response => {
    const url = new URL(response.url());
    return response.request().method() === 'GET' && url.pathname === '/api/enterprise/agents'
      && url.searchParams.get('tenant_id') === selected.tenant_id;
  });
  await page.goto('/enterprise/memories');
  const employeeListResponse = await employeeList;
  expect(employeeListResponse.status()).toBe(200);
  const employeeRows = rows(await boundedPageJsonBody(page, employeeListResponse, 'F3_MEMORY_EMPLOYEE_LIST_INVALID'));
  const matchingEmployees = employeeRows.filter(row => row.id === selected.id
    && row.tenant_id === selected.tenant_id && row.status === 'active');
  expect(matchingEmployees).toHaveLength(1);
  await scope(page, selected);
  // 原侧栏选择当前员工时也会导航到Dashboard；重新进入记忆页并等待精确作用域读回。
  const memoryRead = page.waitForResponse(response => {
    const url = new URL(response.url());
    return response.request().method() === 'GET' && url.pathname === '/api/enterprise/memories/page'
      && url.searchParams.get('tenant_id') === employee(value, branch, name).tenant_id
      && url.searchParams.get('agent_id') === employee(value, branch, name).id;
  });
  await page.goto('/enterprise/memories');
  const readResponse = await memoryRead; expect(readResponse.status()).toBe(200);
  const readBody = object(await boundedPageJsonBody(page, readResponse, 'F3_MEMORY_PAGE_INVALID'));
  const readRows = rows(readBody.items);
  const ids = value.memory_ids[branch][name]; if (!ids) throw new Error('F3_MEMORY_LABEL_IDS_MISSING');
  expect(readRows.some(row => row.id === ids.large && row.user_id === value.base.owner.expected.id)).toBe(true);
  const table = page.getByRole('table', { name: '员工记忆', exact: true });
  const owner = table.getByRole('row').filter({ hasText: value.base.owner.expected.username });
  await expect(owner).toHaveCount(1);
  const view = owner.getByRole('button', { name: '查看', exact: true });
  const viewCount = await view.count();
  const layout = viewCount === 1 ? await boundedUiOperation(view.evaluate((element) => {
    /** 只记录按钮几何和祖先可见性，不保存页面正文或账号标识。 */
    const rectangle = element.getBoundingClientRect();
    const hit = document.elementFromPoint(rectangle.left + rectangle.width / 2,
      rectangle.top + rectangle.height / 2);
    const ancestors: Array<{ tag: string; role: string | null; display: string; visibility: string;
      opacity: string; width: number; height: number; overflowX: string; overflowY: string }> = [];
    let current: Element | null = element;
    for (let index = 0; current && index < 6; index += 1, current = current.parentElement) {
      const style = window.getComputedStyle(current);
      const box = current.getBoundingClientRect();
      ancestors.push({ tag: current.tagName, role: current.getAttribute('role'),
        display: style.display, visibility: style.visibility, opacity: style.opacity,
        width: Math.round(box.width), height: Math.round(box.height),
        overflowX: style.overflowX, overflowY: style.overflowY });
    }
    return { rectangle: { x: Math.round(rectangle.x), y: Math.round(rectangle.y),
      width: Math.round(rectangle.width), height: Math.round(rectangle.height) },
      hitTag: hit?.tagName ?? null, hitRole: hit?.getAttribute('role') ?? null,
      hitIsInside: Boolean(hit && (hit === element || element.contains(hit))), ancestors };
  }), 30_000, 'F3_MEMORY_LAYOUT_READBACK_DEADLINE') : null;
  await info.attach('memory-management-view-layout.safe.json', { contentType: 'application/json',
    body: JSON.stringify({ viewport: page.viewportSize(), row_count: await owner.count(),
      view_button_count: viewCount, view_button_visible: viewCount === 1 ? await view.isVisible() : false,
      layout }) });
  await view.click();
  const dialog = page.getByRole('dialog', { name: '员工记忆详情', exact: true }); await expect(dialog).toBeVisible();
  const large = dialog.locator('article p').filter({ hasText: '口令：F3-UNSELECTED-SECRET。' });
  await expect(large).toHaveCount(1); expect(await large.textContent()).toBe(value.labels.large);
  const current = await memoryState(page, value, branch, name);
  const record = one(current.records, row => row.id === ids.large);
  expect(integer(record.characters)).toBeGreaterThan(4000); expect(record.hash).toBe(contentHash(value.labels.large));
  await dialog.getByRole('button', { name: 'Close', exact: true }).click(); await expect(dialog).not.toBeVisible();
  await criterion(info, `memory-${branch}-${name}-management-full`, safeMemoryManagementCriterion(
    employee(value, branch, name).id, record.id, record.characters, record.hash));
}
export async function clearMemory(page: Page, value: MemoryInitial, branch: Branch,
  name: MemoryCase): Promise<MemoryState> {
  /** 真实记忆页按当前员工清空本人，核原API scope和原记录全部删除。 */
  const target = employee(value, branch, name);
  await page.goto('/enterprise/memories'); await scope(page, target);
  // 切换员工范围会导航到Dashboard；重新进入后只接受本租户/员工的管理读回。
  const memoryRead = page.waitForResponse(response => {
    const url = new URL(response.url());
    return response.request().method() === 'GET' && url.pathname === '/api/enterprise/memories/page'
      && url.searchParams.get('tenant_id') === target.tenant_id
      && url.searchParams.get('agent_id') === target.id;
  });
  await page.goto('/enterprise/memories');
  const readResponse = await memoryRead; expect(readResponse.status()).toBe(200);
  page.once('dialog', async dialog => { await dialog.accept(); });
  const pending = page.waitForResponse(response => new URL(response.url()).pathname === '/api/enterprise/memories/me'
    && response.request().method() === 'DELETE');
  await page.getByRole('button', { name: '清空我的记忆', exact: true }).click();
  const response = await pending; expect(response.status()).toBe(200);
  const url = new URL(response.url()); expect(url.searchParams.get('tenant_id')).toBe(value.base.owner.expected.tenant_id);
  expect(url.searchParams.get('agent_id')).toBe(target.id);
  const current = await memoryState(page, value, branch, name); expect(current.records).toHaveLength(0); return current;
}
export async function relation(page: Page, value: MemoryInitial, name: 'freeze' | 'revoke',
  method: 'DELETE' | 'POST'): Promise<void> {
  /** 非本人持有员工的原广场使用关系撤回/再授权，禁止伪造generation。 */
  const agent = employee(value, 'DTA', name); await page.goto(EnterpriseRoute.Gallery);
  await page.getByRole('tab', { name: '发现', exact: true }).click();
  await page.getByRole('tab', { name: /^数字员工/ }).click();
  const card = page.getByRole('button').filter({ has: page.getByText(agent.gallery_label, { exact: true }) });
  await expect(card).toHaveCount(1);
  const pending = page.waitForResponse(response => new URL(response.url()).pathname
    === `/api/chat/agents/${agent.id}/use` && response.request().method() === method);
  await card.getByRole('button', { name: method === 'DELETE' ? '移出常用' : '添加到常用', exact: true }).click();
  const response = await pending; expect(response.status()).toBe(200);
  expect(new URL(response.url()).searchParams.get('tenant_id')).toBe(value.base.owner.expected.tenant_id);
  const data: unknown = await boundedPageJsonBody(page, response, 'F3_MEMORY_RELATION_INVALID'); const body = object(data);
  if (method === 'DELETE') { expect(body.agent_id).toBe(agent.id); expect(body.removed).toBe(true); }
  else { expect(body.id).toBe(agent.id); expect(body.tenant_id).toBe(value.base.owner.expected.tenant_id); }
}
export async function confirmMemory(page: Page, value: MemoryInitial, branch: Branch, name: MemoryCase,
  executionId: string, comment: string): Promise<void> {
  /** 原Attention本人补充CAS，原消费者决定恢复，不创建Signal或回写成功。 */
  const current = await memoryState(page, value, branch, name);
  const work = one(current.attention, row => row.execution_id === executionId
    && ['offered', 'claimed'].includes(text(row.status)) && rowsOrStrings(row.allowed_commands).includes('answer'));
  const pendingList = page.waitForResponse(response => new URL(response.url()).pathname === '/api/attention-items'
    && response.request().method() === 'GET' && new URL(response.url()).searchParams.get('view') === 'active');
  await page.goto('/enterprise/work-items');
  const data: unknown = await boundedPageJsonBody(page, await pendingList, 'F3_MEMORY_ATTENTION_LIST_INVALID');
  const listed = rows(object(data).items); const title = text(work.title);
  const same = listed.filter(row => row.title === title); const index = same.findIndex(row => row.id === work.id);
  expect(index).toBeGreaterThanOrEqual(0);
  const cards = page.getByRole('button').filter({ has: page.getByText(title, { exact: true }) });
  await expect(cards).toHaveCount(same.length); await cards.nth(index).click();
  const dialog = page.getByRole('dialog'); await expect(dialog).toContainText(executionId);
  await dialog.getByRole('textbox', { name: '补充信息', exact: true }).fill(comment);
  const pending = page.waitForResponse(response => new URL(response.url()).pathname
    === `/api/attention-items/${text(work.id)}/resolve` && response.request().method() === 'POST');
  await dialog.getByRole('button', { name: '补充并继续', exact: true }).click();
  const response = await pending; expect(response.status()).toBe(200);
  const posted: unknown = response.request().postDataJSON(); const envelope = object(posted);
  expect(envelope.command).toBe('answer'); expect(envelope.expected_revision).toBe(work.revision);
  expect(envelope.comment).toBe(comment); expect(envelope.tenant_id).toBe(value.base.owner.expected.tenant_id);
  await expect(dialog).not.toBeVisible();
}
function rowsOrStrings(value: unknown): string[] {
  /** 原命令数组只接受明确字符串，不能把缺命令当可恢复。 */
  if (!Array.isArray(value) || !value.every(item => typeof item === 'string')) throw new Error('F3_MEMORY_COMMANDS_REQUIRED');
  return value;
}
export async function restartMemory(page: Page, value: MemoryInitial, branch: Branch, name: MemoryCase,
  turn: MemoryTurn, info: TestInfo): Promise<void> {
  /** 只请求自有supervisor真新PID，记忆scope不借订单facts接口扩权。 */
  await waitSdkQuiescent(page);
  const before = await memoryState(page, value, branch, name, turn.session_id);
  const runtime = text(process.env.F3_LIVE_RUNTIME); const control = join(runtime, 'control');
  const directory = lstatSync(control);
  if (realpathSync(runtime) !== runtime || !directory.isDirectory() || directory.isSymbolicLink()
    || directory.uid !== process.getuid?.() || (directory.mode & 0o077)) throw new Error('F3_MEMORY_OWN_RESTART_REQUIRED');
  writeFileSync(join(control, 'restart.request'), String(Date.now()), { encoding: 'utf8', mode: 0o600, flag: 'wx' });
  await expect.poll(async () => { try { return (await memoryState(page, value, branch, name, turn.session_id)).pid !== before.pid; }
    catch { return false; } }, { timeout: 120_000, intervals: [500, 1000, 2000] }).toBe(true);
  const after = await memoryState(page, value, branch, name, turn.session_id); await page.reload();
  await criterion(info, `memory-${name}-actual-owned-app-new-pid`, {
    ...safeMemoryRestartCriterion(before.pid, after.pid, turn.session_id),
    execution_identity_sha256: hash(after.executions.map(row => [row.id, row.plan_checksum])),
  });
}
export async function capturedSource(page: Page, value: MemoryInitial, branch: Branch, name: MemoryCase,
  turn: MemoryTurn, info: TestInfo, stage: 'correction' | 'fresh'): Promise<FactRow[]> {
  /** 限时观察同轮来源、唯一捕获Job、原记录与真实memory.capture SDK任务闭合。 */
  const startedAt = new Date().toISOString(); const started = performance.now();
  const elapsedMs = (): number => Math.max(0, Math.round(performance.now() - started));
  let last: { state: MemoryState; records: FactRow[]; values: Record<string, unknown> } | undefined;
  const observe = async (): Promise<boolean> => {
    const current = await memoryState(page, value, branch, name, turn.session_id);
    const sourceRows = current.messages.filter(row => row.id === turn.source_id && row.role === 'user');
    const jobs = current.jobs.filter(row => row.source_message_id === turn.source_id);
    const job = jobs.length === 1 ? jobs[0] : undefined;
    const jobId = job && typeof job.id === 'string' ? job.id : undefined;
    const assistantId = job && typeof job.source_assistant_message_id === 'string'
      ? job.source_assistant_message_id : undefined;
    const assistantRows = assistantId ? current.messages.filter(row => row.id === assistantId
      && row.role === 'assistant') : [];
    const assistant = assistantRows.length === 1 ? assistantRows[0] : undefined;
    const assistantTurnLinked = !!assistant && assistant.tenant_id === value.base.owner.expected.tenant_id
      && assistant.session_id === turn.session_id
      && (assistant.turn_id === turn.source_id || assistant.user_message_id === turn.source_id)
      && [assistant.turn_id, assistant.user_message_id].every(item => item == null || item === turn.source_id);
    const records = current.records.filter(row => row.source_input_id === turn.source_id
      && integer(row.source_sequence) > 0);
    const linkedRecords = jobId ? records.filter(row => row.capture_job_id === jobId) : [];
    const calls = current.sdk_calls.filter(row => row.operation === 'memory.capture'
      && row.source_input_id === turn.source_id);
    const linkedCalls = jobId ? calls.filter(row => row.task_id === jobId) : [];
    const callIds = calls.map(row => typeof row.call_id === 'string' ? row.call_id : '').sort();
    const requests = calls.map(row => typeof row.sdk_request_sha256 === 'string'
      ? row.sdk_request_sha256 : '').sort();
    const sourceHash = sourceRows.length === 1 && typeof sourceRows[0]?.content_sha256 === 'string'
      ? sourceRows[0].content_sha256 : undefined;
    const exactRecords = records.length > 0 && records.length === linkedRecords.length;
    const exactCalls = calls.length > 0 && calls.length === linkedCalls.length
      && calls.every(row => row.persistent_binding_valid === true && row.actual_sdk_transport === true);
    const providerCount = calls.filter(row => row.actual_provider_call === true).length;
    const passed = sourceRows.length === 1 && sourceRows[0]?.tenant_id === value.base.owner.expected.tenant_id
      && jobs.length === 1 && job?.status === 'completed' && assistantTurnLinked
      && job.source_message_id === turn.source_id && exactRecords && exactCalls && providerCount > 0;
    const values: Record<string, unknown> = {
      criterion: `memory-freeze-capture-source-${stage}`, stage, passed,
      source_id_sha256: hash(turn.source_id), turn_id_sha256: hash(turn.source_id),
      session_id_sha256: hash(turn.session_id), source_content_sha256: sourceHash,
      capture_job_count: jobs.length, capture_job_id_sha256: jobId ? hash(jobId) : undefined,
      capture_job_source_id_sha256: job && typeof job.source_message_id === 'string'
        ? hash(job.source_message_id) : undefined,
      capture_job_assistant_id_sha256: assistantId ? hash(assistantId) : undefined,
      capture_job_assistant_linked: assistantTurnLinked,
      capture_job_status: job?.status ?? null,
      capture_record_count: records.length,
      capture_record_ids_sha256: hash(records.map(row => row.id).filter((id): id is string => typeof id === 'string').sort()),
      capture_record_job_linked: exactRecords,
      capture_sdk_call_count: calls.length, capture_sdk_task_linked_count: linkedCalls.length,
      capture_sdk_persistent_binding_count: calls.filter(row => row.persistent_binding_valid === true).length,
      capture_sdk_transport_count: calls.filter(row => row.actual_sdk_transport === true).length,
      capture_sdk_actual_provider_call_count: providerCount,
      capture_sdk_request_sha256: hash(requests), capture_sdk_call_ids_sha256: hash(callIds),
      wait_ms: elapsedMs(),
    };
    last = { state: current, records, values };
    return passed;
  };

  const observationTimeoutMs = 360_000;
  let pollError: unknown;
  let pollSucceeded = false;
  let pollElapsedMs = 0;
  try {
    await expect.poll(observe, { timeout: observationTimeoutMs, intervals: [500, 1000, 2000] }).toBe(true);
    pollSucceeded = true;
  } catch (error: unknown) { pollError = error; }
  pollElapsedMs = elapsedMs();

  if (pollSucceeded) {
    let finalReadbackCompleted = false;
    let closurePassed = false;
    try {
      closurePassed = await observe();
      finalReadbackCompleted = true;
    } catch { /* 保留最后成功的私有只读快照与安全摘要。 */ }
    const readbackAt = new Date().toISOString();
    if (closurePassed && last) {
      const runtime = text(process.env.F3_LIVE_RUNTIME);
      const directory = lstatSync(runtime);
      if (realpathSync(runtime) !== runtime || !directory.isDirectory() || directory.isSymbolicLink()
        || directory.uid !== process.getuid?.() || (directory.mode & 0o077)) {
        throw new Error('F3_MEMORY_PRIVATE_STAGE_DIRECTORY_REQUIRED');
      }
      const key = hash({ title: info.title, retry: info.retry }).slice(0, 24);
      writePrivateMemorySnapshot(runtime, `memory-capture-stage-${key}-${stage}.private.json`, {
        format: 'f3-memory-capture-stage-private-v1', case_title: info.title, retry: info.retry,
        branch, memory_case: name, stage, readback_at: readbackAt,
        source_id_sha256: last.values.source_id_sha256,
        capture_job_id_sha256: last.values.capture_job_id_sha256,
        capture_record_ids_sha256: last.values.capture_record_ids_sha256,
        capture_sdk_call_ids_sha256: last.values.capture_sdk_call_ids_sha256,
        capture_sdk_request_sha256: last.values.capture_sdk_request_sha256,
        original_state: last.state,
      });
      await criterion(info, `memory-freeze-capture-source-${stage}`, {
        ...last.values, passed: true, closure_passed: true, poll_timeout: false,
        observation_timeout_ms: observationTimeoutMs, final_readback_grace_ms: 30_000,
        started_at: startedAt, cutoff_at: null,
        readback_at: readbackAt, final_readback_attempted: true,
        final_readback_completed: finalReadbackCompleted,
        poll_elapsed_ms_at_success: pollElapsedMs,
        post_cutoff_readback_completed: false, wait_ms: elapsedMs(),
      });
      return last.records;
    }
    const values = last?.values ?? { capture_job_count: 0, capture_record_count: 0,
      capture_sdk_call_count: 0, capture_sdk_task_linked_count: 0 };
    await criterion(info, `memory-freeze-capture-source-${stage}`, {
      ...values, passed: false, closure_passed: false, poll_timeout: false,
      failure_reason: 'final_readback_not_closed', observation_timeout_ms: observationTimeoutMs,
      final_readback_grace_ms: 30_000,
      started_at: startedAt, cutoff_at: null, readback_at: readbackAt,
      final_readback_attempted: true, final_readback_completed: finalReadbackCompleted,
      post_cutoff_readback_completed: false,
      poll_elapsed_ms_at_failure: pollElapsedMs, wait_ms: elapsedMs(),
    });
    throw new Error('F3_MEMORY_CAPTURE_FINAL_READBACK_INVALID');
  }

  const waitMsAtFailure = pollElapsedMs;
  const pollTimedOut = isCapturePollTimeout(pollError, observationTimeoutMs);
  const cutoffAt = pollTimedOut ? new Date().toISOString() : null;
  let finalReadbackCompleted = false;
  let closurePassed = false;
  try {
    closurePassed = await observe();
    finalReadbackCompleted = true;
  } catch { /* 保留最后成功的私有只读快照与安全摘要。 */ }
  const readbackAt = new Date().toISOString();
  const values = last?.values ?? { capture_job_count: 0, capture_record_count: 0,
    capture_sdk_call_count: 0, capture_sdk_task_linked_count: 0 };
  await criterion(info, `memory-freeze-capture-source-${stage}`, {
    ...values, passed: false, closure_passed: false, poll_timeout: pollTimedOut,
    failure_reason: pollTimedOut ? 'capture_poll_timeout' : 'capture_poll_error',
    observation_timeout_ms: observationTimeoutMs, final_readback_grace_ms: 30_000,
    started_at: startedAt, cutoff_at: cutoffAt,
    poll_elapsed_ms_at_failure: waitMsAtFailure, readback_at: readbackAt,
    final_readback_attempted: true, final_readback_completed: finalReadbackCompleted,
    post_cutoff_readback_completed: pollTimedOut && finalReadbackCompleted,
    final_readback_closure_passed: closurePassed, wait_ms: elapsedMs(),
  });
  throw new Error('F3_MEMORY_CAPTURE_SOURCE_NOT_CLOSED');
}

export async function archiveMemoryFacts(page: Page, info: TestInfo): Promise<void> {
  /** 页面仍存活时保留13作用域原身份与SDK映射；失败也归档，缺结算不宣称完整。 */
  const runtime = text(process.env.F3_LIVE_RUNTIME); const directory = lstatSync(runtime);
  if (realpathSync(runtime) !== runtime || !directory.isDirectory() || directory.isSymbolicLink()
    || directory.uid !== process.getuid?.() || (directory.mode & 0o077)) {
    throw new Error('F3_MEMORY_PRIVATE_FACTS_DIRECTORY_REQUIRED');
  }
  const key = hash({ title: info.title, retry: info.retry }).slice(0, 24);
  const errors: { stage: string; branch?: Branch; memory_case?: string; error_sha256: string }[] = [];
  const states: { branch: Branch; memory_case: string; original_state: MemoryState }[] = [];
  let quiescent = false;
  try {
    const health = object(await privateRequest(page, '/__e2e/f3/sdk-health'));
    expect(object(health.memory_inputs).valid).toBe(true);
    await waitSdkQuiescent(page); quiescent = true;
  } catch (error: unknown) {
    errors.push({ stage: 'sdk_quiescent', error_sha256: hash(String(error)) });
  }
  try {
    const value = await memoryInitial(page);
    for (const branch of ['SOP', 'DTA'] as const) {
      const cases: MemoryCase[] = [...MEMORY_CASES[branch]];
      for (const name of cases) {
        try {
          states.push({ branch, memory_case: name, original_state: await memoryState(page, value, branch, name) });
        } catch (error: unknown) {
          errors.push({ stage: 'memory_state', branch, memory_case: name, error_sha256: hash(String(error)) });
        }
      }
    }
  } catch (error: unknown) {
    errors.push({ stage: 'memory_bootstrap', error_sha256: hash(String(error)) });
  }
  writePrivateMemorySnapshot(runtime, `memory-facts-after-case-${key}.private.json`, {
    case_title: info.title, retry: info.retry, snapshot_at: new Date().toISOString(),
    test_status: info.status, sdk_quiescent: quiescent,
    private_api_request_budget: memoryFreezePrivateApiReadBudgetSnapshot(),
    provider_observation_read_budget: memoryFreezeProviderObservationReadBudgetSnapshot(),
    expected_scope_count: 13, original_memory_states: states, observation_errors: errors,
  });
  if (errors.length || states.length !== 13) {
    writeFileSync(join(runtime, `memory-facts-after-case-${key}.error.safe.json`), JSON.stringify({
      case_title: info.title, valid: false, sdk_quiescent: quiescent,
      observed_scope_count: states.length, expected_scope_count: 13, observation_errors: errors,
    }), { encoding: 'utf8', mode: 0o600, flag: 'wx' });
    throw new Error('F3_MEMORY_PERSISTENT_FACTS_INCOMPLETE');
  }
}

/**
 * @Time       : 2026/10/13
 * @Author     : zhanglp8181
 * @File       : test-final-f3-browser-driver.test.ts
 * @CallChain  : 无浏览器 Vitest → 原 UI 请求只读合同 → 身份/分支/冻结拒绝边界
 * @Description: 合成数据仅作纯解析反例，不写业务库、不模拟供应商资格或旅程通过。
 */
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { chmodSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { expect as playwrightExpect } from '@playwright/test';
import type { Page, Request, Response, TestInfo } from '@playwright/test';
import {
  closeWithinAbsoluteFixtureDeadline,
  remainingAbsoluteFixtureDeadlineMs,
  runBeforeAbsoluteFixtureDeadline,
} from './final-f3-browser-fixture';
import {
  BoundedRequestCountBudget, CumulativeJsonBodyBudget, F3UiContractError, safeUiErrorCategory, assertSeparateReviewer, observeApprovalAfter, observeApprovalBefore, observeComposerRequest,
  observeContextResponse, observeEmployeeResponse, observeLoginResponse, observeResolveRequest,
  safeUiReceipt, closePageBeforeNextJourney, type ApprovalIntent, type ComposerIntent, type UiPrincipal,
  sessionMetadataContains,
  waitForNextOriginalResponse,
  observeComposerDomState,
  filterEmployeeGalleryViaUi,
  sdkLedgerIsQuiescent,
  observeRemoteCleanupResponse,
  waitForComposerStreamCompletion,
  boundedUiOperation,
  boundedResponseBody,
  boundedJsonBody,
  boundedPageJsonBody,
  remainingDeadlineMs,
  sessionMetadataReadWindow,
} from './final-f3-browser-driver';
import { isDtaStartResolved, isSafeDeclineTerminal, safeHttpErrorCode,
  memoryStateResponseWithinByteLimit, safeModelDiagnosticEvidence, safeModelDiagnosticObservation,
  assertSuccessfulModelConnectionDiagnostic,
  afterSuccessfulModelConnectionDiagnostic,
  memoryProviderObservationResponseWithinByteLimit,
  sessionListRouteHealthFromJournal, withOwnerPageParked, type SafeDeclineFacts,
  attachCaseIdentityBinding, modelQualityCaseId, modelQualityKnowledgeCaseId, privateRequest, httpCount, withDisposedApiResponse, safeCaseIdentityBinding,
  diagnosticApiJsonResponseWithinByteLimit }
  from './final-f3-jrn08-helpers';
import { hasAllUnknownMemoryLabels, isCapturePollTimeout, labeledFields,
  findSopResumedAnswerMessage, safeMemoryAnswerQualityObservation, safeMemoryManagementCriterion,
  safeMemoryRestartCriterion }
  from './final-f3-memory-helpers';
import { sopLabels } from './final-f3-memory-helpers';
import {
  memoryFreezeProviderObservationReadBudgetSnapshot,
  withMemoryFreezeProviderObservationReadBudget,
} from './final-f3-jrn08-helpers';

let responseDisposalDirectory = '';
let responseDisposalRuntime = '';
let responseDisposalConfig = '';
beforeAll(() => {
  responseDisposalDirectory = mkdtempSync(join(tmpdir(), 'f3-api-response-disposal-'));
  responseDisposalRuntime = join(responseDisposalDirectory, 'runtime');
  responseDisposalConfig = join(responseDisposalDirectory, 'private-config.json');
  mkdirSync(responseDisposalRuntime, { mode: 0o700 });
  chmodSync(responseDisposalRuntime, 0o700);
  writeFileSync(responseDisposalConfig, JSON.stringify({
    control_key: 'synthetic-control', run_id: 'synthetic-run', runtime: responseDisposalRuntime,
    provider: { origin: 'http://127.0.0.1:51239', key: 'synthetic-key', control_key: 'synthetic-provider-control',
      namespace: 'synthetic', provider_kind: 'simulated_async_service' },
  }), { mode: 0o600 });
  chmodSync(responseDisposalConfig, 0o600);
});
afterAll(() => {
  vi.unstubAllEnvs();
  if (responseDisposalDirectory) rmSync(responseDisposalDirectory, { recursive: true, force: true });
});

const owner: UiPrincipal = {
  id: 'test-owner', tenant_id: 'test-tenant', username: 'owner-synthetic',
  role: 'member', membership_status: 'active',
};
const reviewer: UiPrincipal = {
  id: 'test-reviewer', tenant_id: owner.tenant_id, username: 'reviewer-synthetic',
  role: 'admin', membership_status: 'active',
};
const employee = { id: 'test-agent', tenant_id: owner.tenant_id, name: '合成参数员工 [.+]' };
const composer: ComposerIntent = {
  principal: owner, employee, kind: 'DTA', message: '合成输入，不是供应商业务结果。',
  model_config_id: 'test-model-config', client_timezone: 'UTC',
};
const approval: ApprovalIntent = {
  initiator: owner, reviewer, employee, attention_id: 'test-attention',
};
const requestHash = 'a'.repeat(64);
const capabilityHash = 'b'.repeat(64);
const approvalHash = 'c'.repeat(64);

describe('F3早期case级SDK身份绑定', () => {
  it('只为冻结合同中的精确语义返回case id，不能把相似标题当成质量场景', () => {
    /** 合同映射必须由分支和场景明确生成，未知或外部任务标题保持未映射。 */
    expect(modelQualityCaseId('SOP', 'normal')).toBe('sop-normal');
    expect(modelQualityCaseId('DTA', 'knowledge_old')).toBe('dynamic_task-knowledge_old');
    expect(modelQualityKnowledgeCaseId('DTA', 'none')).toBe('dynamic_task-knowledge_unknown');
    expect(modelQualityKnowledgeCaseId('SOP', 'none')).toBe('sop-knowledge_unknown');
    expect(modelQualityCaseId('DTA', 'unknown')).toBeUndefined();
    expect(modelQualityCaseId('SOP', 'feedback_improvement')).toBe('sop-feedback_improvement');
  });

  it('显式case id必须属于冻结合同，不能写入临时标签', async () => {
    /** 受控旅程才能覆盖质量合同；任意调用方标签在附件边界直接拒绝。 */
    const attach = vi.fn(async (_name: string, _options: { body: Buffer; contentType: string }) => undefined);
    const info = { title: 'F3-SYNTHETIC-CASE', attach } as unknown as TestInfo;
    await expect(attachCaseIdentityBinding(info, 'composer-source', {
      session_id: 'synthetic-session-id', source_input_id: 'synthetic-source-id',
    }, 'synthetic-client-turn-id', 'temporary-case')).rejects
      .toThrow('F3_MODEL_QUALITY_CASE_ID_UNREGISTERED');
    expect(attach).not.toHaveBeenCalled();
  });

  it('只归档case/stage与兼容原SDK摘要的身份哈希，不写入原始私有ID', () => {
    /** 等待/轮询失败前也能用同一SDK身份摘要归属用例，同时不泄露会话或来源ID。 */
    const binding = safeCaseIdentityBinding('F3-SYNTHETIC-CASE', 'composer-source', {
      session_id: 'synthetic-session-id', source_input_id: 'synthetic-source-id',
      instance_id: 'synthetic-instance-id',
    }, 'synthetic-client-turn-id');
    const serialized = JSON.stringify(binding);

    expect(binding.case_id).toBe('F3-SYNTHETIC-CASE');
    expect(binding.stage).toBe('composer-source');
    expect(binding.identity_sha256.session_id)
      .toBe('3e1f415c01c8eb3bf7c9086d7a819b75b535298a588feedf6eef41f62fe05c59');
    expect(binding.identity_sha256.source_input_id)
      .toBe('42e94d16b4eb90d1b4c204fd76cfdc4d313c79414ba9054e8ffe25168d6d55cf');
    expect(binding.identity_sha256.instance_id)
      .toBe('f9896a6abc2c52247128a3f5a0ce6404aa69e6ca094050452011a66654b32afe');
    expect(binding.client_turn_id_sha256)
      .toBe('bafcdf72f9ea43ecc4ad9e4cbfaf21fee61bf89949c5b2e63d4d39afd5fe36e7');
    expect(binding.identity_sha256.session_id).toMatch(/^[a-f0-9]{64}$/);
    expect(binding.identity_sha256.source_input_id).toMatch(/^[a-f0-9]{64}$/);
    expect(binding.client_turn_id_sha256).toMatch(/^[a-f0-9]{64}$/);
    expect(serialized).not.toContain('synthetic-session-id');
    expect(serialized).not.toContain('synthetic-source-id');
    expect(serialized).not.toContain('synthetic-instance-id');
    expect(serialized).not.toContain('synthetic-client-turn-id');
  });

  it('用例绑定通过既有安全criteria附件输出，不泄露原值', async () => {
    /** 验证full20既有归档通道可读取新绑定，附件只含case标签和标量摘要。 */
    const attach = vi.fn(async (_name: string, _options: { body: Buffer; contentType: string }) => undefined);
    const info = { title: 'F3-SYNTHETIC-CASE', attach } as unknown as TestInfo;
    await attachCaseIdentityBinding(info, 'composer-source', {
      session_id: 'synthetic-session-id', source_input_id: 'synthetic-source-id',
    }, 'synthetic-client-turn-id');
    expect(attach).toHaveBeenCalledTimes(1);
    const [name, options] = attach.mock.calls[0] ?? [];
    expect(name).toBe('f3-safe-criterion');
    if (!options) throw new Error('F3_TEST_ATTACHMENT_MISSING');
    const body = Buffer.from(options.body).toString('utf8');
    const projected: unknown = JSON.parse(body);
    expect(projected).toMatchObject({
      criterion: 'case-sdk-identity-binding', case_id: 'F3-SYNTHETIC-CASE', stage: 'composer-source',
      identity_sha256: { session_id: expect.stringMatching(/^[a-f0-9]{64}$/),
        source_input_id: expect.stringMatching(/^[a-f0-9]{64}$/) },
      client_turn_id_sha256: expect.stringMatching(/^[a-f0-9]{64}$/),
    });
    expect(body).not.toContain('synthetic-session-id');
    expect(body).not.toContain('synthetic-source-id');
    expect(body).not.toContain('synthetic-client-turn-id');
  });
});

describe('F3 SOP恢复回复来源', () => {
  it('允许确认轮成为回复turn但仍要求原source和冻结SOP身份存在', () => {
    /** 对话式SOP没有动态任务result_id时，确认轮仍须落在同一真实实例和版本。 */
    const messages = [
      { id: 'source-1', role: 'user' },
      { id: 'confirm-1', role: 'user' },
      { id: 'assistant-1', role: 'assistant', metadata: {
        execution_id: 'execution-1', turn_id: 'confirm-1',
        feedback_source: {
          policy_version: 'feedback_source_v1', branch: 'sop', execution_id: 'execution-1',
          execution_status: 'succeeded', turn_id: 'confirm-1', skill_version_id: 'version-1',
          definition_checksum: 'a'.repeat(64),
        },
      } },
    ];
    expect(findSopResumedAnswerMessage(messages, 'execution-1', 'source-1').id).toBe('assistant-1');
    expect(() => findSopResumedAnswerMessage(messages, 'execution-1', 'missing-source'))
      .toThrow();
    expect(() => findSopResumedAnswerMessage(messages, 'other-execution', 'source-1'))
      .toThrow();
  });

  it('解析模型的SOP自然语言槽位并归一化到质量字段', () => {
    /** 模拟模型可接受的“槽位 为值”表达，保留未知值且不依赖大写标签。 */
    expect(sopLabels('记忆核验已确认：user_name 为青禾，reply_style 为简洁要点，omitted_code 为未知。'))
      .toEqual({ NAME: '青禾', STYLE: '简洁要点', CODE: '未知' });
    expect(sopLabels('- 用户名称：青禾\n- 回复风格：简洁要点\n- 省略代码：未知'))
      .toEqual({ NAME: '青禾', STYLE: '简洁要点', CODE: '未知' });
    expect(sopLabels('user_name：未知\nreply_style：未知\nomitted_code：未知'))
      .toEqual({ NAME: '未知', STYLE: '未知', CODE: '未知' });
  });
});

describe('F3冻结旅程夹具teardown绝对截止', () => {
  it('context和browser关闭共享fixture终点，不各自重置30秒窗口', async () => {
    /** 用同一绝对终点模拟反向fixture teardown顺序，不启动浏览器。 */
    const deadline = '100000000000';
    const setTimeout = vi.fn();
    const closes: string[] = [];
    const testInfo = { setTimeout } as unknown as Parameters<typeof closeWithinAbsoluteFixtureDeadline>[0];
    await closeWithinAbsoluteFixtureDeadline(
      testInfo, deadline, async () => { closes.push('context'); }, 70_000_000_000n,
    );
    await closeWithinAbsoluteFixtureDeadline(
      testInfo, deadline, async () => { closes.push('browser'); }, 85_000_000_000n,
    );
    expect(setTimeout.mock.calls).toEqual([[30_000], [15_000]]);
    expect(closes).toEqual(['context', 'browser']);
  });

  it('过期或无效deadline拒绝扩大夹具关闭时限', () => {
    /** Playwright API只接受正整数，过期不能获得一个新的完整teardown窗口。 */
    expect(remainingAbsoluteFixtureDeadlineMs('50', 50n)).toBe(1);
    expect(remainingAbsoluteFixtureDeadlineMs('1000000', 1n)).toBe(1);
    expect(() => remainingAbsoluteFixtureDeadlineMs('bad', 0n))
      .toThrow('F3_MEMORY_FREEZE_ABSOLUTE_DEADLINE_MISSING');
  });

  it.each([
    { stage: 'CHROMIUM_LAUNCH' as const },
    { stage: 'BROWSER_CONTEXT_CREATE' as const },
  ])('活动截止耗尽时在$stage前拒绝且不调用创建器', async ({ stage }) => {
    /** 证明真实fixture调用launch/newContext之前，过期时创建函数保持零次调用。 */
    const create = vi.fn(async () => ({ created: true }));
    await expect(runBeforeAbsoluteFixtureDeadline('100', stage, create, 100n))
      .rejects.toThrow(`F3_MEMORY_FREEZE_ACTIVE_DEADLINE_EXHAUSTED_BEFORE_${stage}`);
    expect(create).not.toHaveBeenCalled();
  });

  it('活动截止仍有效时只在检查后调用一次浏览器创建器', async () => {
    /** 仍有绝对时间余量时保留原Playwright操作，不复制或重试启动。 */
    const browser = { created: true };
    const create = vi.fn(async () => browser);
    await expect(runBeforeAbsoluteFixtureDeadline('200', 'CHROMIUM_LAUNCH', create, 100n))
      .resolves.toBe(browser);
    expect(create).toHaveBeenCalledTimes(1);
  });
});

describe('F3私有API读取次数预算', () => {
  it('达到独立私有请求上限后拒绝第N+1次读取', async () => {
    /** private APIRequestContext与页面JSON正文预算分离并有独立硬读数上限。 */
    const budget = new BoundedRequestCountBudget(1, 'F3_MEMORY_PRIVATE_API_READ_LIMIT');
    const first = vi.fn(async () => 'first');
    const second = vi.fn(async () => 'second');
    await expect(budget.dispatch(first)).resolves.toBe('first');
    expect(budget.reads).toBe(1);
    await expect(budget.dispatch(second)).rejects.toThrow('F3_MEMORY_PRIVATE_API_READ_LIMIT');
    expect(second).not.toHaveBeenCalled();
    expect(budget.reads).toBe(1);
  });

  it('freeze诊断provider观察最多派发两次且第3次预先拒绝', async () => {
    /** 固定两次独立provider旁证读取，超出路径上界时不得派发网络请求。 */
    vi.stubEnv('F3_DIAGNOSTIC_SELECTION', 'memory-dta-freeze');
    try {
      const before = memoryFreezeProviderObservationReadBudgetSnapshot();
      const first = vi.fn(async () => 'first');
      const second = vi.fn(async () => 'second');
      const third = vi.fn(async () => 'third');
      await expect(withMemoryFreezeProviderObservationReadBudget(first)).resolves.toBe('first');
      await expect(withMemoryFreezeProviderObservationReadBudget(second)).resolves.toBe('second');
      await expect(withMemoryFreezeProviderObservationReadBudget(third))
        .rejects.toThrow('F3_MEMORY_PROVIDER_OBSERVATION_READ_LIMIT');
      expect(first).toHaveBeenCalledTimes(1);
      expect(second).toHaveBeenCalledTimes(1);
      expect(third).not.toHaveBeenCalled();
      expect(before.maximum).toBe(2);
    } finally {
      vi.unstubAllEnvs();
    }
  });
});

describe('F3私有记忆快照磁盘预算', () => {
  it('快照写入先校验单文件和运行累计上限，再以私有新文件创建', async () => {
    /** 使用合成tmpdir验证超限零落盘、累计容量和0600新文件边界。 */
    const directory = mkdtempSync(join(tmpdir(), 'f3-memory-snapshot-budget-'));
    try {
      const snapshots: Record<string, unknown> = await import('./final-f3-memory-helpers');
      const writeSnapshot = snapshots.writePrivateMemorySnapshot;
      expect(typeof writeSnapshot).toBe('function');
      if (typeof writeSnapshot !== 'function') return;
      const options = { maxFileBytes: 32, maxTotalBytes: 64, minimumFreeBytes: 0 };
      const first = 'memory-capture-stage-0123456789abcdef01234567-correction.private.json';
      const second = 'memory-capture-stage-0123456789abcdef01234567-fresh.private.json';

      writeSnapshot(directory, first, { value: 'small' }, options);
      expect(readFileSync(join(directory, first), 'utf8')).toContain('small');
      expect((await import('node:fs')).statSync(join(directory, first)).mode & 0o777).toBe(0o600);

      const unicode = 'memory-capture-stage-1123456789abcdef01234567-fresh.private.json';
      const structured = { value: '青禾\n\t保留 a  b。', rows: [undefined, null, 7] };
      writeSnapshot(directory, unicode, structured,
        { maxFileBytes: 1024, maxTotalBytes: 4096, minimumFreeBytes: 0 });
      expect(JSON.parse(readFileSync(join(directory, unicode), 'utf8'))).toEqual({
        value: '青禾\n\t保留 a  b。', rows: [null, null, 7],
      });
      expect(() => writeSnapshot(directory, first, { value: 'replacement' }, options)).toThrow();
      expect(readFileSync(join(directory, first), 'utf8')).toContain('small');

      expect(() => writeSnapshot(directory, second, { value: 'x'.repeat(64) }, options))
        .toThrow('F3_MEMORY_PRIVATE_SNAPSHOT_FILE_LIMIT');
      expect(() => readFileSync(join(directory, second))).toThrow();

      expect(() => writeSnapshot(directory, second, { value: 'x'.repeat(40) }, {
        maxFileBytes: 64, maxTotalBytes: 64, minimumFreeBytes: 0,
      })).toThrow('F3_MEMORY_PRIVATE_SNAPSHOT_TOTAL_LIMIT');
      expect(() => readFileSync(join(directory, second))).toThrow();

      const lock = join(directory, '.memory-snapshot-budget.lock');
      writeFileSync(lock, 'existing lock', { mode: 0o600 });
      expect(() => writeSnapshot(directory, second, { value: 'small' }, options)).toThrow();
      expect(readFileSync(lock, 'utf8')).toBe('existing lock');
    } finally {
      rmSync(directory, { recursive: true, force: true });
    }
  });

  it('超限对象在JSON.stringify前被拒绝，避免先分配无界序列化缓冲', async () => {
    /** 字节预算检查必须先遍历值形状；拒绝过大对象时不能调用完整序列化器。 */
    const directory = mkdtempSync(join(tmpdir(), 'f3-memory-snapshot-preflight-'));
    const stringify = vi.spyOn(JSON, 'stringify');
    try {
      const snapshots: Record<string, unknown> = await import('./final-f3-memory-helpers');
      const writeSnapshot = snapshots.writePrivateMemorySnapshot;
      expect(typeof writeSnapshot).toBe('function');
      if (typeof writeSnapshot !== 'function') return;
      expect(() => writeSnapshot(directory,
        'memory-capture-stage-0123456789abcdef01234567-correction.private.json',
        { value: 'x'.repeat(65) }, { maxFileBytes: 32, maxTotalBytes: 64, minimumFreeBytes: 0 },
      )).toThrow('F3_MEMORY_PRIVATE_SNAPSHOT_FILE_LIMIT');
      expect(stringify).not.toHaveBeenCalled();
    } finally {
      stringify.mockRestore();
      rmSync(directory, { recursive: true, force: true });
    }
  });

  it('对象超限时逐键短路，不先生成全量键数组或entries副本', async () => {
    /** 小文件预算必须在逐键遍历早期停止，不能先分配Object.keys和entries全量副本。 */
    const directory = mkdtempSync(join(tmpdir(), 'f3-memory-snapshot-key-cap-'));
    const entries = Array.from({ length: 10_000 }, (_, index) => [`key-${index}`, 'value']);
    const value = Object.fromEntries(entries);
    const objectKeys = vi.spyOn(Object, 'keys');
    try {
      const snapshots: Record<string, unknown> = await import('./final-f3-memory-helpers');
      const writeSnapshot = snapshots.writePrivateMemorySnapshot;
      expect(typeof writeSnapshot).toBe('function');
      if (typeof writeSnapshot !== 'function') return;
      expect(() => writeSnapshot(directory,
        'memory-capture-stage-0123456789abcdef01234567-correction.private.json', value,
        { maxFileBytes: 64, maxTotalBytes: 64, minimumFreeBytes: 0 },
      )).toThrow('F3_MEMORY_PRIVATE_SNAPSHOT_FILE_LIMIT');
      expect(objectKeys.mock.calls.some(([candidate]) => candidate === value)).toBe(false);
    } finally {
      objectKeys.mockRestore();
      rmSync(directory, { recursive: true, force: true });
    }
  });
});

describe('F3私有记忆状态响应上限', () => {
  it('只接受规范长度且不超过2MiB的HTTP响应', () => {
    /** 浏览器必须在JSON解析前拒绝缺失、非规范或超预算响应长度。 */
    expect(memoryStateResponseWithinByteLimit({ 'content-length': '0' })).toBe(true);
    expect(memoryStateResponseWithinByteLimit({ 'content-length': String(2 * 1024 * 1024) })).toBe(true);
    expect(memoryStateResponseWithinByteLimit({ 'content-length': String(2 * 1024 * 1024 + 1) })).toBe(false);
    expect(memoryStateResponseWithinByteLimit({ 'content-length': '02' })).toBe(false);
    expect(memoryStateResponseWithinByteLimit({})).toBe(false);
  });

  it('provider旁证响应在JSON解码前受256KiB Content-Length限制', () => {
    /** 模拟服务自有响应先由字节合同约束，缺失或超限长度不进入JSON解析。 */
    expect(memoryProviderObservationResponseWithinByteLimit({ 'content-length': '0' })).toBe(true);
    expect(memoryProviderObservationResponseWithinByteLimit({ 'content-length': String(256 * 1024) })).toBe(true);
    expect(memoryProviderObservationResponseWithinByteLimit({ 'content-length': String(256 * 1024 + 1) })).toBe(false);
    expect(memoryProviderObservationResponseWithinByteLimit({})).toBe(false);
  });
});

describe('F3第二管理者页面停放合同', () => {
  it('管理员页面完成后恢复员工页面原URL', async () => {
    /** 用最小页面替身核顺序，不启动浏览器或应用服务。 */
    let currentUrl = 'http://127.0.0.1:4174/workspace/chat/synthetic-session';
    const goto = vi.fn(async (url: string) => { currentUrl = url; return null; });
    const ownerPage = { url: () => currentUrl, goto, isClosed: () => false } as unknown as Page;
    const result = await withOwnerPageParked(ownerPage, async () => {
      expect(currentUrl).toBe('about:blank');
      return 'reviewer-complete';
    });

    expect(result).toBe('reviewer-complete');
    expect(currentUrl).toBe('http://127.0.0.1:4174/workspace/chat/synthetic-session');
    expect(goto.mock.calls.map(([url]) => url)).toEqual([
      'about:blank', 'http://127.0.0.1:4174/workspace/chat/synthetic-session',
    ]);
  });

  it('管理员操作失败时仍恢复员工页面并保留原错误', async () => {
    /** 异常恢复不能让员工页面遗留在about:blank，也不能吞掉管理员失败。 */
    let currentUrl = 'http://127.0.0.1:4174/workspace/chat/synthetic-session';
    const goto = vi.fn(async (url: string) => { currentUrl = url; return null; });
    const ownerPage = { url: () => currentUrl, goto, isClosed: () => false } as unknown as Page;

    await expect(withOwnerPageParked(ownerPage, async () => {
      expect(currentUrl).toBe('about:blank');
      throw new Error('reviewer failed');
    })).rejects.toThrow('reviewer failed');
    expect(currentUrl).toBe('http://127.0.0.1:4174/workspace/chat/synthetic-session');
  });
});

describe('F3记忆捕获观察截止分类', () => {
  it('只将Playwright的精确expect.poll截止错误识别为超时', async () => {
    let timeoutError: unknown;
    try {
      await playwrightExpect.poll(async () => false, { timeout: 15, intervals: [1] }).toBe(true);
    } catch (error: unknown) {
      timeoutError = error;
    }
    expect(isCapturePollTimeout(timeoutError, 15)).toBe(true);

    let observerError: unknown;
    try {
      await playwrightExpect.poll(async () => { throw new Error('synthetic observer error'); },
        { timeout: 15, intervals: [1] }).toBe(true);
    } catch (error: unknown) {
      observerError = error;
    }
    expect(observerError).toBeInstanceOf(Error);
    expect(isCapturePollTimeout(observerError, 15)).toBe(false);
  });

  it('浏览器辅助异步读取遵守硬截止并用单调期限计算剩余时间', async () => {
    /** 页面fetch或跨帧等待未返回时不能越过分支预算；期限已过立即得到零。 */
    await expect(boundedUiOperation(new Promise<never>(() => undefined), 10,
      'F3_UI_TEST_DEADLINE')).rejects.toMatchObject({ message: 'F3_UI_TEST_DEADLINE' });
    expect(remainingDeadlineMs(150, 100)).toBe(50);
    expect(remainingDeadlineMs(99, 100)).toBe(0);
    expect(sessionMetadataReadWindow(180_000, 0)).toEqual({
      timeout_ms: 45_000, late_grace_ms: 15_000, remaining_ms: 180_000,
    });
    expect(sessionMetadataReadWindow(10_000, 0)).toEqual({
      timeout_ms: 10_000, late_grace_ms: 0, remaining_ms: 10_000,
    });
    const stalledBody = { status: () => 200, json: () => new Promise<never>(() => undefined) } as unknown as Response;
    await expect(boundedResponseBody(stalledBody, 'F3_UI_TEST_BODY', performance.now() + 10))
      .rejects.toMatchObject({ message: 'F3_UI_RESPONSE_BODY_DEADLINE' });
    await expect(boundedJsonBody(stalledBody, 'F3_UI_TEST_JSON_BODY', 10))
      .rejects.toMatchObject({ message: 'F3_UI_RESPONSE_BODY_DEADLINE' });
  });

  it('页面JSON单次超过30秒即关闭所属页面并中断底层响应读取', async () => {
    /** 预算超时必须取消仍持有正文的页面请求，而不只让Node侧Promise.race返回。 */
    vi.useFakeTimers();
    let abortBody: (() => void) | undefined;
    let settled = false;
    const close = vi.fn(async () => { abortBody?.(); });
    const page = { isClosed: () => false, close } as unknown as Page;
    const response = { json: () => new Promise<never>((_resolve, reject) => {
      abortBody = () => reject(new Error('synthetic page closed'));
    }) } as unknown as Response;
    const pending = boundedPageJsonBody(page, response, 'F3_UI_TIMEOUT_TEST', 45_000)
      .then(value => { settled = true; return value; }, error => { settled = true; throw error; });
    void pending.catch(() => undefined);
    try {
      await vi.advanceTimersByTimeAsync(30_000);
      expect(settled).toBe(true);
      expect(close).toHaveBeenCalledTimes(1);
      await expect(pending).rejects.toMatchObject({ message: 'F3_UI_RESPONSE_BODY_DEADLINE' });
    } finally {
      if (!settled) {
        await vi.advanceTimersByTimeAsync(45_000);
        await pending.catch(() => undefined);
      }
      vi.useRealTimers();
    }
  });

  it('多个正文读取共享累计预算且已用时间不会因新响应重置', async () => {
    /** 两次响应体按同一余额结算，第二次停滞不能再获得完整单次时限。 */
    const budget = new CumulativeJsonBodyBudget(35);
    const firstBody = { json: async () => {
      await new Promise(resolve => setTimeout(resolve, 15)); return { safe: true };
    } } as unknown as Response;
    await expect(boundedJsonBody(firstBody, 'F3_UI_TEST_FIRST_BODY', 30, budget))
      .resolves.toEqual({ safe: true });
    expect(budget.remainingMs).toBeLessThan(25);

    const started = performance.now();
    const stalledBody = { json: () => new Promise<never>(() => undefined) } as unknown as Response;
    await expect(boundedJsonBody(stalledBody, 'F3_UI_TEST_SECOND_BODY', 30, budget))
      .rejects.toMatchObject({ message: 'F3_UI_RESPONSE_BODY_DEADLINE' });
    expect(performance.now() - started).toBeLessThan(80);
    expect(budget.remainingMs).toBeLessThan(1);
  });

  it('并发正文读取先预留共享余额，不会各自取得完整单次时限', async () => {
    /** 并发响应先同步扣留各自上限，累计预留不能超过本页剩余额度。 */
    const budget = new CumulativeJsonBodyBudget(35);
    const stalled = { json: () => new Promise<never>(() => undefined) } as unknown as Response;
    const started = performance.now();
    const results = await Promise.allSettled([
      boundedJsonBody(stalled, 'F3_UI_TEST_CONCURRENT_BODY_A', 30, budget),
      boundedJsonBody(stalled, 'F3_UI_TEST_CONCURRENT_BODY_B', 30, budget),
    ]);
    expect(results.every(result => result.status === 'rejected')).toBe(true);
    expect(performance.now() - started).toBeLessThan(80);
    expect(budget.remainingMs).toBeLessThan(1);
  });

  it('冻结诊断同时执行240秒共享时限和20次硬读取上限', async () => {
    /** 快速成功响应也不能绕过静态路径上限，失败/超时读取同样消耗调用名额。 */
    const budget = new CumulativeJsonBodyBudget(1_000, 2);
    const response = { json: async () => ({ ok: true }) } as unknown as Response;
    await expect(boundedJsonBody(response, 'F3_TEST_BUDGET_A', 30, budget)).resolves.toEqual({ ok: true });
    await expect(boundedJsonBody(response, 'F3_TEST_BUDGET_B', 30, budget)).resolves.toEqual({ ok: true });
    await expect(boundedJsonBody(response, 'F3_TEST_BUDGET_C', 30, budget))
      .rejects.toMatchObject({ message: 'F3_UI_RESPONSE_BODY_DEADLINE' });
    expect(budget.reads).toBe(2);
  });
});

describe('真实模型答案标签冲突解析', () => {
  it('保留唯一标签的实际值', () => {
    expect(labeledFields('| NAME | 未知 |\n| STYLE | 简洁格式 |'))
      .toEqual({ NAME: '未知', STYLE: '简洁格式' });
  });

  it('同一标签出现不同答案时明确报告冲突，不取首值或末值', () => {
    expect(labeledFields('| NAME | 未知 |\n| NAME | 合成姓名甲 |\n| STYLE | 简洁格式 |\n| STYLE | 详细格式 |'))
      .toEqual({ NAME: '__CONFLICTING_LABEL_VALUES__', STYLE: '__CONFLICTING_LABEL_VALUES__' });
  });

  it('相同标签重复且答案一致时不误判为冲突', () => {
    expect(labeledFields('| NAME | 合成姓名甲 |\n| NAME | 合成姓名甲 |\n| STYLE | 简洁格式 |'))
      .toEqual({ NAME: '合成姓名甲', STYLE: '简洁格式' });
  });

  it('失败诊断只投影标签类别和隐私布尔，不复制任何回答值', () => {
    const observed = safeMemoryAnswerQualityObservation({
      labels: { NAME: 'private synthetic name', STYLE: '__CONFLICTING_LABEL_VALUES__', CODE: '未知' },
      revoked_name_present: false,
      unselected_secret_present: true,
    });

    expect(observed).toEqual({
      label_classes: { NAME: 'present', STYLE: 'conflicting', CODE: 'unknown' },
      revoked_name_present: false,
      unselected_secret_present: true,
    });
    expect(JSON.stringify(observed)).not.toContain('private synthetic name');
  });

  it('失败诊断安全区分精确未知、带标点未知和夹带文字未知', () => {
    const canary = 'private-quality-answer-canary';
    const observed = safeMemoryAnswerQualityObservation({
      labels: { NAME: '未知。', STYLE: '未知（当前不可用）', CODE: canary },
      revoked_name_present: false,
      unselected_secret_present: false,
    });

    expect(observed).toEqual({
      label_classes: {
        NAME: 'unknown_with_punctuation',
        STYLE: 'unknown_with_extra_text',
        CODE: 'present',
      },
      revoked_name_present: false,
      unselected_secret_present: false,
    });
    expect(hasAllUnknownMemoryLabels({ NAME: '未知。', STYLE: '未知', CODE: '未知' })).toBe(false);
    expect(JSON.stringify(observed)).not.toContain(canary);
  });
});

describe('记忆管理公开criteria隐私边界', () => {
  it('只保存员工与记录ID摘要，不保存原始员工、记忆记录或会话ID', () => {
    /** 浏览器公开criterion仅保留不可逆ID摘要和内容核验字段。 */
    const rawAgent = 'private-memory-agent-id';
    const rawRecord = 'private-memory-record-id';
    const rawSession = 'private-memory-session-id';
    const management = safeMemoryManagementCriterion(rawAgent, rawRecord, 4201, 'a'.repeat(64));
    const restart = safeMemoryRestartCriterion(1001, 1002, rawSession);
    const serialized = JSON.stringify({ management, restart });

    expect(management).toMatchObject({ passed: true, large_character_count: 4201,
      large_sha256: 'a'.repeat(64), complete_management_page: true,
      agent_id_sha256: expect.stringMatching(/^[a-f0-9]{64}$/u),
      large_record_id_sha256: expect.stringMatching(/^[a-f0-9]{64}$/u) });
    expect(management).not.toHaveProperty('agent_id');
    expect(management).not.toHaveProperty('large_record_id');
    expect(restart).toMatchObject({ passed: true, previous_pid: 1001, pid: 1002,
      session_id_sha256: expect.stringMatching(/^[a-f0-9]{64}$/u), provider_restarted: false });
    expect(restart).not.toHaveProperty('session_id');
    expect(serialized).not.toContain(rawAgent);
    expect(serialized).not.toContain(rawRecord);
    expect(serialized).not.toContain(rawSession);
  });

  it('缺身份、负长度或无效内容摘要时拒绝生成安全criterion', () => {
    /** 不允许把未验证字段包装为安全附件。 */
    expect(() => safeMemoryManagementCriterion('', 'record', 1, 'a'.repeat(64)))
      .toThrow('F3_MEMORY_MANAGEMENT_CRITERION_INVALID');
    expect(() => safeMemoryManagementCriterion('agent', 'record', -1, 'a'.repeat(64)))
      .toThrow('F3_MEMORY_MANAGEMENT_CRITERION_INVALID');
    expect(() => safeMemoryManagementCriterion('agent', 'record', 1, 'raw-content'))
      .toThrow('F3_MEMORY_MANAGEMENT_CRITERION_INVALID');
    expect(() => safeMemoryRestartCriterion(1001, 1001, 'session'))
      .toThrow('F3_MEMORY_RESTART_CRITERION_INVALID');
  });
});

describe('记忆清空与撤权后的完整未知字段合同', () => {
  it('姓名、格式和口令三个冻结标签都必须精确为未知', () => {
    expect(hasAllUnknownMemoryLabels({ NAME: '未知', STYLE: '未知', CODE: '未知' })).toBe(true);
    expect(hasAllUnknownMemoryLabels({ NAME: '未知', STYLE: '未知', CODE: 'private' })).toBe(false);
    expect(hasAllUnknownMemoryLabels({ NAME: '未知', STYLE: '未知' })).toBe(false);
    expect(hasAllUnknownMemoryLabels({
      NAME: '__CONFLICTING_LABEL_VALUES__', STYLE: '未知', CODE: '未知',
    })).toBe(false);
  });
});

const unusedSdkLedger = {
  valid: false, client_state_count: 0, started_count: 0, settled_count: 0,
  in_flight_count: 0, observation_error_count: 0, receipt_count: 0,
  file_matches: true, write_failed: false, JRN08_passed: false,
};

describe('F3 UI安全错误保留私有根因', () => {
  it('外层仍只显示固定错误码，同时以不可枚举cause保留私有根因供归档诊断', () => {
    const cause = new Error('private synthetic request details');
    const error = new F3UiContractError('F3_UI_OPERATION_FAILED', cause);

    expect(error.message).toBe('F3_UI_OPERATION_FAILED');
    expect(error.cause).toBe(cause);
    expect(error.stack).toContain('Caused by: Error: private synthetic request details');
    expect(JSON.stringify(error)).not.toContain('private synthetic request details');
  });

  it('只投影固定超时类别，不泄露底层错误消息', () => {
    const cause = Object.assign(new Error('private synthetic URL and token'), { name: 'TimeoutError' });
    const category = safeUiErrorCategory(new F3UiContractError('F3_UI_OPERATION_FAILED', cause));

    expect(category).toBe('timeout');
    expect(JSON.stringify({ category })).not.toContain('private synthetic');
  });

  it('未知底层异常只投影通用类别', () => {
    expect(safeUiErrorCategory(new Error('private synthetic response body'))).toBe('error');
  });
});

describe('模型连接诊断的安全阶段观察', () => {
  it('HTTP 200但success=false不能继续正式模型预检', () => {
    /** 传输成功不等于供应商连接成功，失败诊断必须短路后续动态能力请求。 */
    expect(() => assertSuccessfulModelConnectionDiagnostic(200, { success: false }))
      .toThrow();
    expect(() => assertSuccessfulModelConnectionDiagnostic(200, { success: true }))
      .not.toThrow();
  });

  it('模型连接诊断失败时调用点不派发动态能力预检', async () => {
    /** 直接覆盖verifyModel复用的门控包装，连接失败时真实后续请求回调必须为零次。 */
    const dispatch = vi.fn(async () => ({ ready: true }));
    await expect(afterSuccessfulModelConnectionDiagnostic(200, { success: false }, dispatch))
      .rejects.toThrow();
    expect(dispatch).not.toHaveBeenCalled();
    await expect(afterSuccessfulModelConnectionDiagnostic(200, { success: true }, dispatch))
      .resolves.toEqual({ ready: true });
    expect(dispatch).toHaveBeenCalledTimes(1);
  });

  it('即使管理路由返回500也保留有界状态与摘要绑定，不保存错误正文或原始请求ID', () => {
    const observed = safeModelDiagnosticEvidence(500, {
      'x-f3-diagnostic-invocation-sha256': 'a'.repeat(64),
      'x-f3-diagnostic-config-fingerprint-sha256': 'b'.repeat(64),
      'x-request-id': 'PRIVATE_APPLICATION_REQUEST_ID_CANARY',
    }, {
      success: false,
      http_status: 503,
      request_id: 'PRIVATE_PROVIDER_REQUEST_ID_CANARY',
      error_code: 'PROVIDER_UNAVAILABLE',
      message: 'PRIVATE_PROVIDER_ERROR_CANARY',
      endpoint: 'https://private.example/path?token=PRIVATE_KEY_CANARY',
    }, 'bounded_json');

    expect(observed).toEqual({
      response_http_status: 500,
      provider_http_status: 503,
      diagnostic_success: false,
      error_category: 'provider_unavailable',
      checks: [],
      diagnostic_invocation_sha256: 'a'.repeat(64),
      config_fingerprint_sha256: 'b'.repeat(64),
      application_request_id_sha256: expect.stringMatching(/^[a-f0-9]{64}$/u),
      provider_request_id_sha256: expect.stringMatching(/^[a-f0-9]{64}$/u),
      response_body_kind: 'bounded_json',
    });
    expect(JSON.stringify(observed)).not.toContain('PRIVATE_');
    expect(JSON.stringify(observed)).not.toContain('private.example');
  });

  it('保留阶段、状态和固定错误类别，不保留自由消息、模型或端点', () => {
    const observed = safeModelDiagnosticObservation(200, {
      success: false,
      http_status: 502,
      error_code: 'MODEL_OR_ENDPOINT_NOT_FOUND',
      message: 'PRIVATE_PROVIDER_MESSAGE_CANARY',
      model: 'PRIVATE_MODEL_CANARY',
      endpoint: 'https://private.example/path?token=PRIVATE_KEY_CANARY',
      checks: [
        { name: '配置', status: 'passed', message: 'private config detail' },
        { name: '模型目录', status: 'failed', message: 'private catalog detail' },
      ],
    });

    expect(observed).toEqual({
      response_http_status: 200,
      provider_http_status: 502,
      diagnostic_success: false,
      error_category: 'not_found',
      checks: [
        { stage: 'configuration', status: 'passed' },
        { stage: 'model_catalog', status: 'failed' },
      ],
    });
    expect(JSON.stringify(observed)).not.toContain('PRIVATE_');
    expect(JSON.stringify(observed)).not.toContain('private');
  });

  it('把未知检查和自由错误码压缩成固定unknown类别并限制列表长度', () => {
    const observed = safeModelDiagnosticObservation(700, {
      success: 'yes', error_code: 'PRIVATE_UNCLASSIFIED_PROVIDER_CODE', http_status: 999,
      checks: Array.from({ length: 10 }, () => ({ name: 'PRIVATE_STAGE', status: 'private', message: 'canary' })),
    });
    expect(observed).toEqual({
      response_http_status: null,
      provider_http_status: null,
      diagnostic_success: false,
      error_category: 'unknown',
      checks: Array.from({ length: 8 }, () => ({ stage: 'unknown', status: 'unknown' })),
    });
    expect(JSON.stringify(observed)).not.toContain('PRIVATE_');
    expect(JSON.stringify(observed)).not.toContain('canary');
  });
});

describe('Composer提交阶段的隐私安全诊断', () => {
  it('只保留白名单状态、固定提示类别和有界计数', () => {
    const observed = observeComposerDomState({
      composer_form_present_before: true,
      input_nonempty_before: true,
      submit_button_disabled_before: false,
      submit_button_mode_before: 'send',
      native_submit_event_count: 1000,
      submit_button_disabled_at_submit: false,
      submit_button_mode_at_submit: 'send',
      submit_prevented_after_handler: true,
      composer_form_present_after: true,
      input_nonempty_after: true,
      submit_button_disabled_after: false,
      submit_button_mode_after: 'send',
      queued_indicator_count_after: 0,
      active_chat_route_after: true,
      guard_notice_categories: ['connection-unavailable', 'private user prompt'],
      handoff_reply_post_count: 0,
      input_value: 'private synthetic confirmation body',
      session_id: 'private synthetic session identity',
    });

    expect(observed).toMatchObject({
      composer_form_present_before: true,
      input_nonempty_before: true,
      submit_button_disabled_before: false,
      submit_button_mode_before: 'send',
      native_submit_event_count: 64,
      submit_button_disabled_at_submit: false,
      submit_button_mode_at_submit: 'send',
      submit_prevented_after_handler: true,
      composer_form_present_after: true,
      input_nonempty_after: true,
      active_chat_route_after: true,
      guard_notice_categories: ['connection-unavailable'],
      handoff_reply_post_count: 0,
    });
    expect(JSON.stringify(observed)).not.toContain('private synthetic');
    expect(JSON.stringify(observed)).not.toContain('private user prompt');
  });
});

describe('原独立模拟业务清理响应', () => {
  it('按原服务absent字段确认清理回执，不改称真实供应商', () => {
    expect(() => observeRemoteCleanupResponse(200, { absent: true, real_vendor_qualified: false })).not.toThrow();
  });
  it.each([
    [200, { absent: false, real_vendor_qualified: false }],
    [200, { object_absent: true, real_vendor_qualified: false }],
    [200, { absent: true, real_vendor_qualified: true }],
    [200, { absent: true }],
    [409, { absent: true, real_vendor_qualified: false }],
    [200, null], [200, []],
  ])('拒绝无清理事实、非原字段、未知供应商资格或失败HTTP', (status, value) => {
    expect(() => observeRemoteCleanupResponse(status, value)).toThrow('F3_REMOTE_CLEANUP_INVALID');
  });
});

describe('Attention失败的隐私安全诊断', () => {
  it('只提取原API稳定错误码，不带出自由文本', () => {
    expect(safeHttpErrorCode({ detail: 'SOP_EXECUTION_CONFLICT' })).toBe('SOP_EXECUTION_CONFLICT');
    expect(safeHttpErrorCode({ error: { code: 'ATTENTION_REVISION_CONFLICT' }, message: 'private text' }))
      .toBe('ATTENTION_REVISION_CONFLICT');
    expect(safeHttpErrorCode({ detail: { code: 'WORK_ITEM_NOT_ACTIVE' } })).toBe('WORK_ITEM_NOT_ACTIVE');
  });
  it.each([
    ['free-form detail', { detail: 'private comment containing a synthetic customer name' }],
    ['lowercase detail', { detail: 'internal stack trace' }],
    ['unknown error field', { error_code: 'private payload' }],
    ['malformed object', null], ['array body', []], ['missing code', { message: 'private text' }],
  ])('拒绝把%s写入诊断', (_label, value) => {
    expect(safeHttpErrorCode(value)).toBeUndefined();
  });
});

describe('DTA启动事实观察', () => {
  it.each(['waiting', 'succeeded', 'failed', 'cancelled', 'archived'])(
    '在%s时结束初始状态轮询', status => expect(isDtaStartResolved(status)).toBe(true),
  );
  it.each(['running', 'paused', null, undefined, 1])(
    '在未确认状态%s继续等待', status => expect(isDtaStartResolved(status)).toBe(false),
  );
});

describe('原会话列表HTTP观察', () => {
  it('只在每个GET均完整收到2xx响应时通过', () => {
    const healthy = [
      { event: 'request_start' },
      { event: 'response_start', elapsed_ms: 20, status: 200 },
      { event: 'response_complete', elapsed_ms: 21, status: 200 },
    ].map(value => JSON.stringify(value)).join('\n');
    expect(sessionListRouteHealthFromJournal(healthy)).toEqual({
      request_count: 1, response_count: 1, completed_count: 1, non_2xx_count: 0,
      pending_count: 0, malformed_event_count: 0, passed: true,
    });
  });
  it('原MySQL死锁返回500时不能被成功业务读回掩盖', () => {
    const deadlock = [
      { event: 'request_start' },
      { event: 'response_start', elapsed_ms: 443, status: 500 },
      { event: 'response_complete', elapsed_ms: 443, status: 500 },
    ].map(value => JSON.stringify(value)).join('\n');
    expect(sessionListRouteHealthFromJournal(deadlock)).toEqual({
      request_count: 1, response_count: 1, completed_count: 1, non_2xx_count: 1,
      pending_count: 0, malformed_event_count: 0, passed: false,
    });
  });
  it('缺状态、未结束请求及无法解析行均不能通过', () => {
    const incomplete = [
      { event: 'request_start' }, { event: 'request_pending' },
      { event: 'response_start', status: null }, 'not-json',
    ].map(value => typeof value === 'string' ? value : JSON.stringify(value)).join('\n');
    const result = sessionListRouteHealthFromJournal(incomplete);
    expect(result.passed).toBe(false);
    expect(result).toMatchObject({ request_count: 1, response_count: 1,
      completed_count: 0, non_2xx_count: 1, pending_count: 1, malformed_event_count: 1 });
  });
});

describe('原SDK结算与重启后零调用进程', () => {
  it('实际未创建SDK客户端且账本完整的重启进程已静止', () => {
    expect(sdkLedgerIsQuiescent(unusedSdkLedger)).toBe(true);
    expect(unusedSdkLedger.valid).toBe(false);
    expect(unusedSdkLedger.JRN08_passed).toBe(false);
  });
  it('正常有真实调用的进程只在原全部请求结算后静止', () => {
    expect(sdkLedgerIsQuiescent({ ...unusedSdkLedger, valid: true, client_state_count: 1,
      started_count: 3, settled_count: 3, receipt_count: 3 })).toBe(true);
  });
  it.each([
    ['client_state_count', 1], ['started_count', 1], ['settled_count', 1],
    ['in_flight_count', 1], ['observation_error_count', 1], ['receipt_count', 1],
    ['file_matches', false], ['write_failed', true], ['client_state_count', false],
  ])('原失效字段%s不能借零调用分支通过', (key, value) => {
    expect(sdkLedgerIsQuiescent({ ...unusedSdkLedger, [key]: value })).toBe(false);
  });
  it('有在途、缺原健康字段及计数失衡都拒绝', () => {
    expect(sdkLedgerIsQuiescent({ valid: true, started_count: 2, settled_count: 1,
      in_flight_count: 1 })).toBe(false);
    expect(sdkLedgerIsQuiescent({ valid: false, started_count: 0, settled_count: 0,
      in_flight_count: 0 })).toBe(false);
    expect(sdkLedgerIsQuiescent(null)).toBe(false);
  });
});

function chatBody(): Record<string, unknown> {
  /** 构造原 Composer 字段的合成输入，不参与真实浏览器或持久写入。 */
  return {
    tenant_id: owner.tenant_id, user_id: owner.id, agent_id: employee.id,
    message: composer.message, client_turn_id: 'test-turn', draft_conversation_id: 'test-draft',
    execution_engine: 'dynamic_task', interaction_mode: 'normal', model_config_id: composer.model_config_id,
    client_timezone: 'UTC', channel: 'web', attachments: [],
  };
}

function frozenEvidence() {
  /** 仅作原行投影的合成合同标签；不能用该对象作为真实成功账本。 */
  const argumentsValue = { synthetic_object: '仅测试', amount: 1 };
  return {
    instance: {
      id: 'test-execution', kind: 'dynamic_task', tenant_id: owner.tenant_id, agent_id: employee.id,
      initiator_user_id: owner.id, session_id: 'test-session', current_plan_revision_id: 'test-plan',
    },
    attention: {
      id: approval.attention_id, execution_id: 'test-execution', session_id: 'test-session',
      kind: 'tool_approval', status: 'offered', revision: 2, title: '合成待审批 [.+]',
      source_type: 'dynamic_task', source_ref: 'test-operation', available_commands: ['allow_once', 'deny'],
      resolution: {} as Record<string, unknown>,
      payload: {
        execution_id: 'test-execution', operation_id: 'test-operation', operation_name: 'test-http-tool',
        plan_revision_id: 'test-plan', arguments: argumentsValue, request_fingerprint: requestHash,
        capability_checksum: capabilityHash, approval_fingerprint: approvalHash,
      },
    },
    operation: {
      id: 'test-operation', tenant_id: owner.tenant_id, instance_id: 'test-execution',
      operation_name: 'test-http-tool', status: 'prepared', effect_kind: 'external_write',
      request_json: argumentsValue, request_fingerprint: requestHash, capability_checksum: capabilityHash,
      approval_work_item_id: approval.attention_id, approval_fingerprint: null as string | null,
      approved_by_user_id: null as string | null,
    },
  };
}

function completedEvidence() {
  /** 合成原 allow_once 后置投影，仅测试字段关联而非伪造执行结果。 */
  const value = frozenEvidence();
  value.attention.status = 'completed';
  value.attention.revision = 3;
  value.attention.available_commands = [];
  value.attention.resolution = { command: 'allow_once', actor_user_id: reviewer.id, revision: 3 };
  value.operation.approval_fingerprint = approvalHash;
  value.operation.approved_by_user_id = reviewer.id;
  return value;
}

describe('原登录与原上下文身份', () => {
  it('核对活动真实响应并剥离 token 和自由字段', () => {
    const actual = observeLoginResponse({ token: 'synthetic-private-value', user: { ...owner, private: 'x' } }, owner);
    expect(actual).toEqual(owner);
    expect(JSON.stringify(actual)).not.toContain('synthetic-private-value');
  });
  it.each([
    ['id', 'other-user'], ['tenant_id', 'other-tenant'], ['username', 'other-name'],
    ['role', 'admin'], ['membership_status', 'suspended'],
  ])('登录字段 %s 漂移拒绝，不能绑定预设本人', (field, value) => {
    expect(() => observeLoginResponse({ token: 'synthetic', user: { ...owner, [field]: value } }, owner))
      .toThrow('F3_UI_IDENTITY_MISMATCH');
  });
  it('拒绝无原登录 token 的预置身份', () => {
    expect(() => observeLoginResponse({ user: owner }, owner)).toThrow('F3_UI_LOGIN_RESPONSE_INVALID');
  });
  it('原企业上下文核对 tenant/member，不返回 token', () => {
    expect(observeContextResponse({ tenant: { id: owner.tenant_id }, member: owner, is_administrator: false }, owner))
      .toEqual(owner);
  });
  it('上下文 member 和 tenant 不一致拒绝', () => {
    expect(() => observeContextResponse({ tenant: { id: 'other' }, member: owner, is_administrator: false }, owner))
      .toThrow('F3_UI_IDENTITY_MISMATCH');
  });
  it('管理员标志与原 role 不一致拒绝', () => {
    expect(() => observeContextResponse({ tenant: { id: reviewer.tenant_id }, member: reviewer, is_administrator: false }, reviewer))
      .toThrow('F3_UI_IDENTITY_MISMATCH');
  });
});

describe('当前会话元数据就绪门槛', () => {
  const currentSession = {
    id: 'test-session', tenant_id: owner.tenant_id, user_id: owner.id, agent_id: employee.id,
  };
  const expected = { session_id: currentSession.id, principal: owner, employee };

  it('仅接受原列表中本轮session、tenant、user与employee完全绑定的行', () => {
    expect(sessionMetadataContains([currentSession], expected)).toBe(true);
  });

  it.each([
    ['other session', { ...currentSession, id: 'other-session' }],
    ['other tenant', { ...currentSession, tenant_id: 'other-tenant' }],
    ['other user', { ...currentSession, user_id: 'other-user' }],
    ['other employee', { ...currentSession, agent_id: 'other-agent' }],
  ])('does not treat %s as readiness', (_label, row) => {
    expect(sessionMetadataContains([row], expected)).toBe(false);
  });

  it('拒绝非数组或缺少完整session绑定的响应', () => {
    expect(sessionMetadataContains(null, expected)).toBe(false);
    expect(sessionMetadataContains([{ ...currentSession, agent_id: undefined }], expected)).toBe(false);
  });
});

describe('原metadata响应监听竞态', () => {
  it('逐端点暴露脱敏请求匹配、响应状态与时延，不记录URL或身份值', async () => {
    const listeners = new Map<string, Set<(value: never) => void>>();
    const fakePage = {
      url: () => 'http://localhost:5137/workspace/chat/test-session',
      on(event: string, listener: (value: never) => void) {
        const group = listeners.get(event) || new Set<(value: never) => void>();
        group.add(listener); listeners.set(event, group); return this;
      },
      off(event: string, listener: (value: never) => void) {
        listeners.get(event)?.delete(listener); return this;
      },
    } as unknown as Page;
    const request = {
      method: () => 'GET',
      url: () => 'http://localhost:5137/api/chat/sessions?tenant_id=private-tenant',
    } as Request;
    const response = { request: () => request, status: () => 503 } as Response;
    const observations: Array<Record<string, unknown>> = [];
    const waiting = waitForNextOriginalResponse(
      fakePage, '/api/chat/sessions', 'private-tenant', false,
      observation => observations.push(observation as unknown as Record<string, unknown>),
    );

    listeners.get('request')?.forEach(listener => listener(request as never));
    listeners.get('response')?.forEach(listener => listener(response as never));

    await expect(waiting).resolves.toBe(response);
    expect(observations).toHaveLength(2);
    expect(observations[0]).toEqual({
      type: 'request', tenant_query_match: true, filter_query_match: true,
    });
    expect(observations[1]).toMatchObject({ type: 'response', status: 503 });
    expect(observations[1].elapsed_ms).toEqual(expect.any(Number));
    const serialized = JSON.stringify(observations);
    expect(serialized).not.toContain('private-tenant');
    expect(serialized).not.toContain('/api/chat/sessions');
  });

  it('请求与响应在同一个事件循环内完成时仍接住本轮原响应', async () => {
    const requestListeners = new Set<(value: Request) => void>();
    const responseListeners = new Set<(value: Response) => void>();
    const observedResponses: Response[] = [];
    const fakePage = {
      url: () => 'http://localhost:5137/workspace/chat/test-session',
      on(event: string, listener: (value: never) => void) {
        if (event === 'request') requestListeners.add(listener as (value: Request) => void);
        if (event === 'response') responseListeners.add(listener as (value: Response) => void);
        return this;
      },
      off(event: string, listener: (value: never) => void) {
        if (event === 'request') requestListeners.delete(listener as (value: Request) => void);
        if (event === 'response') responseListeners.delete(listener as (value: Response) => void);
        return this;
      },
      waitForRequest(predicate: (value: Request) => boolean) {
        return new Promise<Request>((resolve) => {
          const listener = (value: Request) => {
            if (!predicate(value)) return;
            requestListeners.delete(listener);
            resolve(value);
          };
          requestListeners.add(listener);
        });
      },
      waitForResponse(predicate: (value: Response) => boolean) {
        if (observedResponses.some(predicate)) {
          return Promise.reject(new Error('response listener attached after response'));
        }
        return new Promise<Response>((resolve) => {
          const listener = (value: Response) => {
            if (!predicate(value)) return;
            responseListeners.delete(listener);
            resolve(value);
          };
          responseListeners.add(listener);
        });
      },
    } as unknown as Page;
    const request = {
      method: () => 'GET',
      url: () => 'http://localhost:5137/api/chat/sessions?tenant_id=test-tenant',
    } as Request;
    const response = { request: () => request, status: () => 200 } as Response;
    const waiting = waitForNextOriginalResponse(fakePage, '/api/chat/sessions', 'test-tenant');

    requestListeners.forEach((listener) => listener(request));
    observedResponses.push(response);
    responseListeners.forEach((listener) => listener(response));

    await expect(waiting).resolves.toBe(response);
  });

  it('响应事件返回不同Request包装对象时仍按同一原请求URL匹配', async () => {
    vi.useFakeTimers();
    try {
      const requestListeners = new Set<(value: Request) => void>();
      const responseListeners = new Set<(value: Response) => void>();
      const fakePage = {
        url: () => 'http://localhost:5137/workspace/chat/test-session',
        on(event: string, listener: (value: never) => void) {
          if (event === 'request') requestListeners.add(listener as (value: Request) => void);
          if (event === 'response') responseListeners.add(listener as (value: Response) => void);
          return this;
        },
        off(event: string, listener: (value: never) => void) {
          if (event === 'request') requestListeners.delete(listener as (value: Request) => void);
          if (event === 'response') responseListeners.delete(listener as (value: Response) => void);
          return this;
        },
      } as unknown as Page;
      const url = 'http://localhost:5137/api/chat/sessions?tenant_id=test-tenant';
      const requestEventObject = { method: () => 'GET', url: () => url } as Request;
      const responseRequestObject = { method: () => 'GET', url: () => url } as Request;
      const response = { request: () => responseRequestObject, status: () => 200 } as Response;
      const waiting = waitForNextOriginalResponse(
        fakePage,
        '/api/chat/sessions',
        'test-tenant',
        false,
        undefined,
        { timeout_ms: 25, late_grace_ms: 0 },
      );
      const expectedResponse = expect(waiting).resolves.toBe(response);

      requestListeners.forEach(listener => listener(requestEventObject));
      responseListeners.forEach(listener => listener(response));
      await vi.advanceTimersByTimeAsync(25);
      await expectedResponse;
    } finally {
      vi.useRealTimers();
    }
  });

  it('45秒门槛超时后短暂记录晚到HTTP状态，但继续按超时拒绝本轮操作', async () => {
    vi.useFakeTimers();
    try {
      const listeners = new Map<string, Set<(value: never) => void>>();
      const fakePage = {
        url: () => 'http://localhost:5137/workspace/chat/test-session',
        on(event: string, listener: (value: never) => void) {
          const group = listeners.get(event) || new Set<(value: never) => void>();
          group.add(listener); listeners.set(event, group); return this;
        },
        off(event: string, listener: (value: never) => void) {
          listeners.get(event)?.delete(listener); return this;
        },
      } as unknown as Page;
      const request = {
        method: () => 'GET',
        url: () => 'http://localhost:5137/api/chat/sessions?tenant_id=test-tenant',
      } as Request;
      const response = { request: () => request, status: () => 503 } as Response;
      const observations: Array<Record<string, unknown>> = [];
      const waiting = waitForNextOriginalResponse(
        fakePage, '/api/chat/sessions', 'test-tenant', false,
        event => observations.push(event as unknown as Record<string, unknown>),
        { timeout_ms: 20, late_grace_ms: 100 },
      );
      listeners.get('request')?.forEach(listener => listener(request as never));
      await vi.advanceTimersByTimeAsync(20);
      expect(observations[observations.length - 1]).toEqual({ type: 'timeout' });
      listeners.get('response')?.forEach(listener => listener(response as never));

      await expect(waiting).rejects.toThrow('original metadata response timeout');
      expect(observations[observations.length - 1]).toEqual({
        type: 'late_response', status: 503, elapsed_ms: 20,
      });
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('真实浏览器旅程间renderer排空', () => {
  it('完成持久证据归档后关闭上一页及其context，再等待固定排空间隔', async () => {
    vi.useFakeTimers();
    try {
      const events: string[] = [];
      const page = {
        isClosed: () => false,
        async close() { events.push('page-closed'); },
        context: () => ({ isClosed: () => false, async close() { events.push('context-closed'); } }),
      } as unknown as Page;
      const finished = closePageBeforeNextJourney(page).then(() => events.push('drained'));
      await Promise.resolve();
      expect(events).toEqual(['page-closed', 'context-closed']);
      await vi.advanceTimersByTimeAsync(1_499);
      expect(events).toEqual(['page-closed', 'context-closed']);
      await vi.advanceTimersByTimeAsync(1);
      await finished;
      expect(events).toEqual(['page-closed', 'context-closed', 'drained']);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('Composer SSE等待边界', () => {
  it('DynamicTaskAgent等待态不要求当前HTTP流结束', async () => {
    let finishCalls = 0;
    const response = { async finished() { finishCalls += 1; return null; } } as unknown as Response;
    await waitForComposerStreamCompletion(response, 'DTA');
    expect(finishCalls).toBe(0);
  });
  it('同步SOP仍核对原SSE完整结束', async () => {
    let finishCalls = 0;
    const response = { async finished() { finishCalls += 1; return null; } } as unknown as Response;
    await waitForComposerStreamCompletion(response, 'SOP');
    expect(finishCalls).toBe(1);
  });
  it('同步SOP流错误仍拒绝Composer回执', async () => {
    const response = { async finished() { return new Error('synthetic stream failure'); } } as unknown as Response;
    await expect(waitForComposerStreamCompletion(response, 'SOP')).rejects.toThrow('F3_UI_COMPOSER_REJECTED');
  });
});

describe('第二管理者与精确员工', () => {
  it('使用原搜索框定位分页外员工，保留名称的字面字符', async () => {
    const calls: string[] = [];
    const page = {
      getByPlaceholder(name: string, options: { exact: true }) {
        /** 仅观察原UI定位与输入，不伪造员工use响应或绕过后续身份核验。 */
        expect(name).toBe('搜索数字员工…'); expect(options.exact).toBe(true);
        return { async fill(value: string) { calls.push(value); } };
      },
    };
    await filterEmployeeGalleryViaUi(page, employee);
    expect(calls).toEqual([employee.name]);
  });
  it('没有有效员工名称时不向搜索框填入猜造查询', async () => {
    let fills = 0;
    const page = { getByPlaceholder() { return { async fill() { fills += 1; } }; } };
    await expect(filterEmployeeGalleryViaUi(page, { ...employee, name: '  ' }))
      .rejects.toThrow('F3_UI_EMPLOYEE_MISMATCH');
    expect(fills).toBe(0);
  });
  it('同租户不同活动管理员合法', () => {
    expect(() => assertSeparateReviewer(owner, reviewer)).not.toThrow();
  });
  it.each([
    ['本人', { ...reviewer, id: owner.id }], ['错租户', { ...reviewer, tenant_id: 'other' }],
    ['非管理员', { ...reviewer, role: 'member' as const }],
    ['已离职', { ...reviewer, membership_status: 'left' as const }],
  ] as const)('%s 管理者拒绝', (_label, value) => {
    expect(() => assertSeparateReviewer(owner, value)).toThrow('F3_UI_REVIEWER_FORBIDDEN');
  });
  it('员工完整原 id/tenant/name 相等才可选，特殊字符按字面值', () => {
    expect(observeEmployeeResponse({ ...employee, metadata: {} }, employee)).toEqual(employee);
  });
  it.each(['id', 'tenant_id', 'name'])('员工字段 %s 不同拒绝', (field) => {
    expect(() => observeEmployeeResponse({ ...employee, [field]: 'other' }, employee))
      .toThrow('F3_UI_EMPLOYEE_MISMATCH');
  });
});

describe('Composer 原 POST 合同', () => {
  it.each(['SOP', 'DTA'] as const)('正常 %s 精确保留原引擎与原 turn，不补造正文结果', (kind) => {
    const body = { ...chatBody(), execution_engine: kind === 'SOP' ? 'auto' : 'dynamic_task' };
    const receipt = observeComposerRequest(body, { ...composer, kind });
    expect(receipt.client_turn_id).toBe('test-turn');
    expect(receipt.conversation_kind).toBe('draft');
    expect(receipt.execution_engine).toBe(body.execution_engine);
    expect(receipt.request_sha256).toMatch(/^[a-f0-9]{64}$/);
    expect(JSON.stringify(receipt)).not.toContain(composer.message);
  });
  it('现有 session 沿原 session_id 续接', () => {
    const body = chatBody();
    delete body.draft_conversation_id;
    body.session_id = 'test-session';
    expect(observeComposerRequest(body, composer).conversation_kind).toBe('session');
  });
  it.each([
    ['tenant_id', 'other'], ['user_id', 'other'], ['agent_id', 'other'], ['message', 'changed'],
    ['execution_engine', 'auto'], ['interaction_mode', 'scheduled_task'], ['model_config_id', 'other'],
    ['client_timezone', 'Asia/Shanghai'], ['channel', 'api'], ['attachments', ['preset']],
  ])('请求字段 %s 漂移拒绝', (field, value) => {
    expect(() => observeComposerRequest({ ...chatBody(), [field]: value }, composer))
      .toThrow('F3_UI_COMPOSER_MISMATCH');
  });
  it.each(['client_turn_id', 'draft_conversation_id'])('缺原 %s 拒绝，不生成替代身份', (field) => {
    const body = chatBody(); delete body[field];
    expect(() => observeComposerRequest(body, composer)).toThrow('F3_UI_COMPOSER_MISMATCH');
  });
  it('session 与 draft 同时存在拒绝', () => {
    expect(() => observeComposerRequest({ ...chatBody(), session_id: 'another' }, composer))
      .toThrow('F3_UI_COMPOSER_MISMATCH');
  });
  it('员工与当前主体租户不一致拒绝', () => {
    expect(() => observeComposerRequest(chatBody(), { ...composer, employee: { ...employee, tenant_id: 'other' } }))
      .toThrow('F3_UI_COMPOSER_MISMATCH');
  });
  it.each(['forced_general_skill_id', 'forced_general_skill_ids', 'ability_ids'])('不能由 %s 偷换预定分支', (field) => {
    expect(() => observeComposerRequest({ ...chatBody(), [field]: ['preset-skill'] }, composer))
      .toThrow('F3_UI_COMPOSER_MISMATCH');
  });
});

describe('原 Attention / Operation 批准合同', () => {
  it('前置原行关联完整，后置原 actor 与冻结载荷保持', () => {
    const before = observeApprovalBefore(frozenEvidence(), approval);
    expect(before.operation_id).toBe('test-operation');
    expect(before.attention_revision).toBe(2);
    expect(() => observeApprovalAfter(completedEvidence(), before)).not.toThrow();
  });
  it('原实例缺少实际 kind 拒绝，不能以 plan 或 Attention 代替分支身份', () => {
    const value = frozenEvidence();
    const instance: Record<string, unknown> = { ...value.instance }; delete instance.kind;
    expect(() => observeApprovalBefore({ ...value, instance }, approval))
      .toThrow('F3_UI_APPROVAL_MISMATCH');
  });
  it('原 SOP 实例不能借同一元模型行进入 DTA 管理员批准辅助层', () => {
    const value = frozenEvidence(); value.instance.kind = 'sop';
    expect(() => observeApprovalBefore(value, approval)).toThrow('F3_UI_APPROVAL_MISMATCH');
  });
  it('批准前尚未写入 Operation 授权关联仍合法，批准后必须落入本次原关联', () => {
    const value = frozenEvidence();
    Object.assign(value.operation, { approval_work_item_id: null });
    const before = observeApprovalBefore(value, approval);
    expect(() => observeApprovalAfter(completedEvidence(), before)).not.toThrow();
  });
  it('批准前已关联另一个 Attention 拒绝，不能覆盖原授权来源', () => {
    const value = frozenEvidence(); value.operation.approval_work_item_id = 'other-attention';
    expect(() => observeApprovalBefore(value, approval)).toThrow('F3_UI_APPROVAL_MISMATCH');
  });
  it.each(['id', 'tenant_id', 'agent_id', 'initiator_user_id'])('原实例 %s 漂移拒绝', (field) => {
    const value = frozenEvidence();
    Object.assign(value.instance, { [field]: 'other' });
    expect(() => observeApprovalBefore(value, approval)).toThrow('F3_UI_APPROVAL_MISMATCH');
  });
  it.each(['id', 'execution_id', 'session_id', 'source_ref', 'kind', 'source_type'])('原 Attention %s 漂移拒绝', (field) => {
    const value = frozenEvidence();
    Object.assign(value.attention, { [field]: 'other' });
    expect(() => observeApprovalBefore(value, approval)).toThrow('F3_UI_APPROVAL_MISMATCH');
  });
  it.each(['operation_id', 'operation_name', 'execution_id', 'plan_revision_id', 'request_fingerprint', 'capability_checksum'])('原冻结字段 %s 不同拒绝', (field) => {
    const value = frozenEvidence();
    Object.assign(value.attention.payload, { [field]: 'other' });
    expect(() => observeApprovalBefore(value, approval)).toThrow('F3_UI_APPROVAL_MISMATCH');
  });
  it('payload arguments 与 Operation 实际请求不一致拒绝', () => {
    const value = frozenEvidence(); value.operation.request_json = { synthetic_object: 'changed', amount: 1 };
    expect(() => observeApprovalBefore(value, approval)).toThrow('F3_UI_APPROVAL_MISMATCH');
  });
  it.each([true, -1, 2.5])('revision 非原非负整数 %s 拒绝', (revision) => {
    const value = frozenEvidence(); Object.assign(value.attention, { revision });
    expect(() => observeApprovalBefore(value, approval)).toThrow('F3_UI_APPROVAL_MISMATCH');
  });
  it('无原 allow_once 资格拒绝', () => {
    const value = frozenEvidence(); value.attention.available_commands = ['deny'];
    expect(() => observeApprovalBefore(value, approval)).toThrow('F3_UI_APPROVAL_MISMATCH');
  });
  it('原 CAS 仅发送原信封，返回安全请求 hash', () => {
    const before = observeApprovalBefore(frozenEvidence(), approval);
    expect(observeResolveRequest({ tenant_id: owner.tenant_id, command_id: 'test-command', command: 'allow_once', expected_revision: 2 }, before))
      .toMatch(/^[a-f0-9]{64}$/);
  });
  it.each([
    ['tenant_id', 'other'], ['command', 'approve'], ['expected_revision', 3], ['command_id', ''],
    ['approval_fingerprint', approvalHash], ['arguments', { preset: true }],
  ])('批准请求 %s 偏离原 CAS 信封拒绝', (field, value) => {
    const before = observeApprovalBefore(frozenEvidence(), approval);
    const body = { tenant_id: owner.tenant_id, command_id: 'test-command', command: 'allow_once', expected_revision: 2, [field]: value };
    expect(() => observeResolveRequest(body, before)).toThrow('F3_UI_RESOLVE_MISMATCH');
  });
  it.each(['request_fingerprint', 'capability_checksum', 'approval_fingerprint'])('批准后 %s 改变拒绝', (field) => {
    const before = observeApprovalBefore(frozenEvidence(), approval);
    const value = completedEvidence(); Object.assign(value.operation, { [field]: 'd'.repeat(64) });
    expect(() => observeApprovalAfter(value, before)).toThrow('F3_UI_APPROVAL_MISMATCH');
  });
  it('批准后 Operation actor 不同拒绝', () => {
    const before = observeApprovalBefore(frozenEvidence(), approval);
    const value = completedEvidence(); value.operation.approved_by_user_id = owner.id;
    expect(() => observeApprovalAfter(value, before)).toThrow('F3_UI_APPROVAL_MISMATCH');
  });
  it('批准后原决策 actor 不同拒绝，不能只看 Operation 或页面提示', () => {
    const before = observeApprovalBefore(frozenEvidence(), approval);
    const value = completedEvidence(); value.attention.resolution.actor_user_id = owner.id;
    expect(() => observeApprovalAfter(value, before)).toThrow('F3_UI_APPROVAL_MISMATCH');
  });
  it('批准后原参数改变拒绝', () => {
    const before = observeApprovalBefore(frozenEvidence(), approval);
    const value = completedEvidence(); value.operation.request_json.amount = 2;
    expect(() => observeApprovalAfter(value, before)).toThrow('F3_UI_APPROVAL_MISMATCH');
  });
  it('原前置回执必须持有独立冻结快照，调用方改原对象不能改验收基线', () => {
    const input = frozenEvidence(); const before = observeApprovalBefore(input, approval);
    input.operation.request_json.amount = 3;
    expect(() => observeApprovalAfter(completedEvidence(), before)).not.toThrow();
  });
  it('对外摘要仅 hash/固定码/bool，完整 JRN08 与真实供应商资格恒 false', () => {
    const before = observeApprovalBefore(frozenEvidence(), approval);
    const summary = safeUiReceipt(before);
    expect(summary.JRN08_passed).toBe(false);
    expect(summary.real_vendor_qualified).toBe(false);
    for (const privateText of [owner.id, owner.username, reviewer.id, employee.name, before.title, before.operation_id]) {
      expect(JSON.stringify(summary)).not.toContain(privateText);
    }
    expect(Object.values(summary).every((value) => typeof value === 'boolean'
      || (typeof value === 'string' && (/^[a-f0-9]{64}$/.test(value) || value === 'F3_UI_CONTRACT_OBSERVED'))))
      .toBe(true);
  });
});

describe('原安全拒绝与业务终态', () => {
  const completedRejection: SafeDeclineFacts = {
    instances: [{ id: 'test-instance', kind: 'sop', status: 'succeeded', current_node_id: 'done' }],
    operations: [{ id: 'test-operation', instance_id: 'test-instance', status: 'failed',
      effect_state: 'none', error_code: 'HTTP_ERROR' }],
    attempts: [{ operation_id: 'test-operation', status: 'failed' }],
    messages: [{ role: 'assistant', execution_id: 'test-instance', execution_status: 'succeeded',
      receipt_ids: [], business_outcome: { failure_notice: true, success_claim: false } }],
  };

  it('将已如实答复零效果拒绝的原SOP终态与Operation失败分开核验', () => {
    expect(isSafeDeclineTerminal(completedRejection, 'SOP', 'test-instance', 'test-operation')).toBe(true);
  });

  it('缺少拒绝说明、出现成功宣称或回执时不得通过', () => {
    const message = completedRejection.messages[0];
    expect(message).toBeTruthy();
    if (!message) return;
    expect(isSafeDeclineTerminal({ ...completedRejection, messages: [{ ...message,
      business_outcome: { failure_notice: false, success_claim: false } }] },
      'SOP', 'test-instance', 'test-operation')).toBe(false);
    expect(isSafeDeclineTerminal({ ...completedRejection, messages: [{ ...message,
      business_outcome: { failure_notice: true, success_claim: true } }] },
      'SOP', 'test-instance', 'test-operation')).toBe(false);
    expect(isSafeDeclineTerminal({ ...completedRejection, messages: [{ ...message, receipt_ids: ['synthetic-receipt'] }] },
      'SOP', 'test-instance', 'test-operation')).toBe(false);
  });

  it('成功Operation、效果已落账、重试或分支身份不符都拒绝', () => {
    expect(isSafeDeclineTerminal({ ...completedRejection, operations: [{
      ...completedRejection.operations[0], status: 'succeeded',
    }] }, 'SOP', 'test-instance', 'test-operation')).toBe(false);
    expect(isSafeDeclineTerminal({ ...completedRejection, operations: [{
      ...completedRejection.operations[0], effect_state: 'complete',
    }] }, 'SOP', 'test-instance', 'test-operation')).toBe(false);
    expect(isSafeDeclineTerminal({ ...completedRejection, attempts: [
      ...completedRejection.attempts, { operation_id: 'test-operation', status: 'failed' },
    ] }, 'SOP', 'test-instance', 'test-operation')).toBe(false);
    expect(isSafeDeclineTerminal(completedRejection, 'DTA', 'test-instance', 'test-operation')).toBe(false);
  });

  it('只接受SOP的原成功终点并拒绝非终态实例', () => {
    expect(isSafeDeclineTerminal({ ...completedRejection, instances: [{
      ...completedRejection.instances[0], current_node_id: 'submit',
    }] }, 'SOP', 'test-instance', 'test-operation')).toBe(false);
    expect(isSafeDeclineTerminal({ ...completedRejection, instances: [{
      ...completedRejection.instances[0], status: 'waiting',
    }] }, 'SOP', 'test-instance', 'test-operation')).toBe(false);
    expect(isSafeDeclineTerminal({ ...completedRejection, messages: [{
      ...completedRejection.messages[0], business_outcome: undefined,
    }] }, 'SOP', 'test-instance', 'test-operation')).toBe(false);
  });
});

describe('F3 APIRequestContext 响应生命周期', () => {
  beforeEach(() => vi.stubEnv('F3_BROWSER_PRIVATE_CONFIG', responseDisposalConfig));

  it('通用响应消费合同在成功后释放APIResponse', async () => {
    /** verifyModel和命令读回共享此合同，成功解析后不能保留APIRequestContext正文。 */
    const response = { dispose: vi.fn(async () => undefined) };
    await expect(withDisposedApiResponse(response, async () => 'safe-result')).resolves.toBe('safe-result');
    expect(response.dispose).toHaveBeenCalledTimes(1);
  });

  it('通用响应消费合同在断言或解析拒绝后仍释放APIResponse', async () => {
    /** HTTP状态与JSON解析异常都必须经过同一个finally清理边界。 */
    const response = { dispose: vi.fn(async () => undefined) };
    await expect(withDisposedApiResponse(response, async () => { throw new Error('synthetic parse failure'); }))
      .rejects.toThrow('synthetic parse failure');
    expect(response.dispose).toHaveBeenCalledTimes(1);
  });

  it('APIRequestContext JSON正文要求有限且未压缩的Content-Length', () => {
    /** 缺失、超限或压缩长度不能在JSON展开前被误认为内存上界。 */
    expect(diagnosticApiJsonResponseWithinByteLimit({ 'content-length': '12' })).toBe(true);
    expect(diagnosticApiJsonResponseWithinByteLimit({})).toBe(false);
    expect(diagnosticApiJsonResponseWithinByteLimit({
      'content-length': String(256 * 1024 + 1),
    })).toBe(false);
    expect(diagnosticApiJsonResponseWithinByteLimit({
      'content-length': '12', 'content-encoding': 'gzip',
    })).toBe(false);
  });

  function fakePage(status: number, json: () => Promise<unknown>, headers: Record<string, string> = {}) {
    /** 合成APIResponse记录dispose调用，不启动真实页面、服务或业务请求。 */
    const dispose = vi.fn(async () => undefined);
    const response = { status: () => status, headers: () => headers, json, dispose };
    const fetch = vi.fn(async () => response);
    return { page: { request: { fetch } } as unknown as Page, dispose, fetch };
  }

  it('私有GET在解析成功后立即释放APIResponse', async () => {
    /** 解析值必须先脱离响应对象，再于本次读取结束时释放Playwright缓存正文。 */
    const json = vi.fn(async () => ({ safe: true }));
    const fake = fakePage(200, json, { 'content-length': String(Buffer.byteLength('{"safe":true}')) });
    await expect(privateRequest(fake.page, '/__e2e/f3/bootstrap')).resolves.toEqual({ safe: true });
    expect(json).toHaveBeenCalledTimes(1);
    expect(fake.dispose).toHaveBeenCalledTimes(1);
  });

  it('私有GET在状态拒绝和JSON解析失败时也释放APIResponse', async () => {
    /** HTTP失败与正文损坏均不能把响应体留在APIRequestContext直到关闭。 */
    const rejected = fakePage(503, vi.fn(async () => ({ ignored: true })));
    await expect(privateRequest(rejected.page, '/__e2e/f3/bootstrap'))
      .rejects.toThrow('F3_PRIVATE_READ_FAILED');
    expect(rejected.dispose).toHaveBeenCalledTimes(1);
    const malformed = fakePage(200, vi.fn(async () => { throw new Error('synthetic parse failure'); }),
      { 'content-length': '2' });
    await expect(privateRequest(malformed.page, '/__e2e/f3/bootstrap'))
      .rejects.toThrow('F3_PRIVATE_READ_FAILED');
    expect(malformed.dispose).toHaveBeenCalledTimes(1);
  });

  it('memory正文超长且尚未解析时释放被拒响应', async () => {
    /** Content-Length拒绝在JSON解析前发生，finally仍须清掉Playwright持有的正文。 */
    const json = vi.fn(async () => ({ ignored: true }));
    const fake = fakePage(200, json, { 'content-length': String(2 * 1024 * 1024 + 1) });
    await expect(privateRequest(fake.page, '/__e2e/f3/memory/synthetic-agent'))
      .rejects.toThrow('F3_PRIVATE_READ_FAILED');
    expect(json).not.toHaveBeenCalled();
    expect(fake.dispose).toHaveBeenCalledTimes(1);
  });

  it('bootstrap私有正文要求2MiB内显式未压缩长度并在拒绝后释放', async () => {
    /** 初始私有投影受服务端上限保护，缺长度、压缩及超限响应均在JSON前拒绝。 */
    const headersList: Record<string, string>[] = [
      { 'content-length': String(2 * 1024 * 1024 + 1) },
      {},
      { 'content-length': '10', 'content-encoding': 'gzip' },
    ];
    for (const headers of headersList) {
      const json = vi.fn(async () => ({ ignored: true }));
      const fake = fakePage(200, json, headers);
      await expect(privateRequest(fake.page, '/__e2e/f3/bootstrap'))
        .rejects.toThrow('F3_PRIVATE_READ_FAILED');
      expect(json).not.toHaveBeenCalled();
      expect(fake.dispose).toHaveBeenCalledTimes(1);
    }
  });

  it('模拟provider观察请求在JSON读回后释放APIResponse', async () => {
    /** 模拟服务仍明确不具备真实供应商资格，但其有限HTTP正文也立即释放。 */
    const json = vi.fn(async () => ({ observation_healthy: true,
      observation_error_count: 0, submit_http_count: 0 }));
    const fake = fakePage(200, json);
    await expect(httpCount(fake.page)).resolves.toBe(0);
    expect(json).toHaveBeenCalledTimes(1);
    expect(fake.dispose).toHaveBeenCalledTimes(1);
  });

  it('模拟provider观察HTTP失败仍在调用方拒绝前释放APIResponse', async () => {
    /** 远端状态由原调用方判错，但观察响应缓存必须在状态断言前完成释放。 */
    const fake = fakePage(503, vi.fn(async () => ({ observation_healthy: false,
      observation_error_count: 1, submit_http_count: 0 })));
    await expect(httpCount(fake.page)).rejects.toThrow();
    expect(fake.dispose).toHaveBeenCalledTimes(1);
  });
});

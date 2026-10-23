/**
 * @Time       : 2026/10/03
 * @Author     : zhanglp8181
 * @File       : sd7-feedback.f3.live.fullstack.e2e.ts
 * @CallChain  : Composer旧目标缺失 → 本人反馈分享 → 真实SkillEditor → 人工发布 → Composer业务回读
 * @Description: 固定本人/关联人独立额度，实际SDK与Operation，真实重启旧等待；所有业务写走UI。
 */
import { expect, test, type Page } from '@playwright/test';
import { createHash } from 'node:crypto';
import { readFile, writeFile, access, rm } from 'node:fs/promises';
import { join } from 'node:path';
import type { ConversationSessionPageRead, SkillCard, SkillRead } from '../src/types';
import type { SkillCandidateRead, SkillCandidateReceipt } from '../src/types/skill-candidate';
import type { FeedbackMaterialRead } from '../src/types/feedback';

const AGENT = 'agent_sd7_feedback';
const SKILL = 'skill_sd7_feedback_quota';
const FEEDBACK = `/enterprise/feedback?agent_id=${AGENT}`;
const CONTROL = process.env.FULLSTACK_SD6B_CONTROL_DIR || '';
test.skip(process.env.FULLSTACK_SD7_F3 !== '1' || process.env.LIVE_ATTACHMENT_E2E !== '1',
  'F3 requires isolated real provider and temporary database');
test.setTimeout(900_000);
test.describe.configure({ mode: 'serial' });

type Instance = { id: string; session_id: string; kind: string; status: string;
  skill_version_id: string; checksum: string; slots: Record<string, string | null> };
type Operation = { id: string; instance_id: string; status: string; employee_id: string;
  request_hash: string; result: { employee_id?: string; quota?: number } };
type State = { business: Record<string, number>; publication_count: number;
  business_calls: Array<{ employee_id: string; quota: number; request_hash: string }>;
  instances: Instance[]; operations: Operation[]; sessions: Array<{ id: string; title: string }>;
  messages: Array<{ id: string; session_id: string; role: string; content_hash: string }>;
  feedback: Array<{ id: string; session_id: string; message_id: string }> };
type SDK = { call_id: string; actual_provider_call: boolean; rewrite: boolean;
  status: string; request_hash: string; response_hash?: string; duration_ms: number };

async function read<T>(page: Page, path: string): Promise<T> {
  /** 中央API只读旁证，不用写API替代必要页面操作。 */
  return page.evaluate(async (path) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch(path, { headers: { Authorization: `Bearer ${auth.token || ''}` } });
    if (!response.ok) throw new Error(`SD7 read refused ${response.status}`);
    return response.json();
  }, path);
}
const state = (page: Page) => read<State>(page, '/__e2e/sd7/state');
const head = (page: Page) => read<SkillRead>(page,
  `/api/enterprise/skills/${SKILL}?tenant_id=tenant_demo&agent_id=${AGENT}`);
const responseFor = (page: Page, suffix: string, method: string) => page.waitForResponse((response) =>
  response.request().method() === method && new URL(response.url()).pathname.endsWith(suffix),
{ timeout: 120_000 });

async function login(page: Page, username: string, key: string) {
  /** 双actor使用真正登录页面；认证状态和合成密码不归档。 */
  const password = process.env[key];
  if (!password) throw new Error('SD7 synthetic login required');
  await page.goto(FEEDBACK);
  await page.evaluate((agent) => {
    localStorage.setItem('gongge_onboarding_guide_seen', '1');
    localStorage.setItem('gongge_quick_start_guide_seen', '1');
    localStorage.setItem('gongge_enterprise_agent_scope', agent);
  }, AGENT);
  await page.getByRole('button', { name: '进入平台' }).click();
  await page.getByRole('textbox', { name: '账号' }).fill(username);
  await page.getByRole('textbox', { name: '密码', exact: true }).fill(password);
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect.poll(() => page.evaluate(() => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { user?: { username?: string } };
    return auth.user?.username || '';
  })).toBe(username);
  await page.goto(FEEDBACK);
}

async function draft(page: Page, dynamic = false) {
  /** 真实Composer新建任务，明确选择原SOP或DTA分支。 */
  await page.goto(`/workspace/chat/draft/${AGENT}`);
  const engine = page.getByRole('button', { name: '选择 DynamicTaskAgent 复杂任务引擎' });
  await expect(engine).toBeVisible({ timeout: 30_000 });
  if ((await engine.getAttribute('aria-pressed') === 'true') !== dynamic) await engine.click();
  await expect(engine).toHaveAttribute('aria-pressed', String(dynamic));
}

async function send(page: Page, text: string): Promise<string> {
  /** 源轮从真正用户消息摘要发现，不预置session/instance/成功消息。 */
  const composer = page.getByPlaceholder('输入消息，按 Enter 发送...');
  await expect(composer).toBeVisible({ timeout: 30_000 });
  await composer.fill(text);
  await expect(page.getByRole('button', { name: '发送', exact: true })).toBeEnabled({ timeout: 120_000 });
  const accepted = responseFor(page, '/api/chat/stream', 'POST');
  await page.getByRole('button', { name: '发送', exact: true }).click();
  expect((await accepted).status()).toBe(200);
  const hash = createHash('sha256').update(JSON.stringify(text)).digest('hex');
  let sessionId = '';
  await expect.poll(async () => {
    sessionId = (await state(page)).messages.find((row) => row.role === 'user' && row.content_hash === hash)?.session_id || '';
    return sessionId;
  }, { timeout: 120_000 }).not.toBe('');
  return sessionId;
}

async function instance(page: Page, sessionId: string, status: string): Promise<Instance> {
  /** 真实Runtime账本终态/等待而非模型宣称完成；每任务必须只有一个实例。 */
  await expect.poll(async () => (await state(page)).instances
    .filter((row) => row.session_id === sessionId && row.status === status).length,
  { timeout: 300_000, intervals: [500, 1000, 2000] }).toBe(1);
  const rows = (await state(page)).instances.filter((row) => row.session_id === sessionId);
  expect(rows).toHaveLength(1);
  await expect.poll(async () => (await state(page)).messages.some((row) =>
    row.session_id === sessionId && row.role === 'assistant'), { timeout: 120_000 }).toBe(true);
  return rows[0];
}

async function openCandidate(page: Page): Promise<SkillCandidateRead> {
  /** 管理者从实际共享材料按钮进入当前编辑器，不调用候选写API。 */
  await page.goto(FEEDBACK);
  await page.getByRole('button', { name: '查看共享材料' }).click();
  const opened = responseFor(page, '/feedback-candidates', 'POST');
  await page.getByRole('button', { name: '在当前编辑器改进 SOP' }).click();
  const candidate = await (await opened).json() as SkillCandidateRead;
  await expect(page.getByRole('region', { name: '反馈候选审核' })).toBeVisible();
  return candidate;
}

async function sdk(): Promise<SDK[]> {
  /** 读取真实SDK安全观察，无模型请求/回复/原始错误。 */
  const text = await readFile(join(CONTROL, 'sdk-observations.safe.jsonl'), 'utf8').catch(() => '');
  return text.split('\n').filter(Boolean).map((line) => JSON.parse(line) as SDK);
}

function behavior(card: SkillCard): SkillCard {
  /** 只排除预先允许的可读说明；工具、条件、输入、metadata及全部行为保持完整比较。 */
  return { ...structuredClone(card), description: '',
    nodes: card.nodes.map((row) => ({ ...structuredClone(row), instruction: '' })) };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  /** 校验共享SkillCard中扩展metadata真实结构，避免无依据类型断言。 */
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function metadata(node: Record<string, unknown> | undefined): Record<string, unknown> {
  /** 读取真实节点metadata；缺结构意味着冻结定义不符合本用例，立即失败。 */
  if (!node || !isRecord(node.metadata)) throw new Error('Frozen query metadata missing');
  return node.metadata;
}

test('F3 FB-R05/06: real rewrite fixes actual employee quota; restarted old waiting retains original publication', async ({ page, browser }, info) => {
  const context = await browser.newContext({ baseURL: info.project.use.baseURL });
  const manager = await context.newPage();
  page.setDefaultTimeout(30_000); manager.setDefaultTimeout(30_000);
  const checkpoint = async (phase: string) => info.attach('sd7-f3-safe-journey', {
    contentType: 'application/json', body: Buffer.from(JSON.stringify({ phase })),
  });
  try {
    await login(page, 'sd7-owner', 'FULLSTACK_SD7_OWNER_PASSWORD');
    await login(manager, 'sd7-manager', 'FULLSTACK_SD7_MANAGER_PASSWORD');
    const original = await head(manager);
    expect(original.publication_scope_agent_id).toBe(AGENT);
    expect(metadata(original.content.nodes.find((row) => row.node_id === 'query')).operation_input)
      .toEqual({ employee_id: 'slots.sponsor_id' });
    expect((await state(page)).business).toEqual({ E001: 1500, E002: 600 });
    expect((await state(page)).instances).toHaveLength(0);

    await draft(page);
    const waitingSession = await send(page, '查询本人额度 SD7-WAIT：请先询问我的员工号和关联人编号。');
    const waiting = await instance(page, waitingSession, 'waiting');
    expect(waiting.skill_version_id).toBe(original.publication_id);
    expect((await state(page)).business_calls).toHaveLength(0);
    await checkpoint('old-waiting-created');

    await draft(page);
    const oldSession = await send(page, '查询本人额度 SD7-OLD：本人员工号是E001，关联人编号是E002。请完成本人的额度查询。');
    const old = await instance(page, oldSession, 'succeeded');
    const oldState = await state(page);
    const oldOperation = oldState.operations.filter((row) => row.instance_id === old.id);
    expect(oldOperation).toHaveLength(1);
    expect(oldOperation[0].employee_id).toBe('E002');
    expect(oldState.business_calls).toHaveLength(1);
    expect(oldState.business_calls[0]).toMatchObject({ employee_id: 'E002', quota: 600 });
    expect(oldState.business_calls[0].request_hash).toBe(oldOperation[0].request_hash);
    expect(oldState.business_calls[0].quota).not.toBe(oldState.business.E001);
    await checkpoint('old-business-goal-missing');

    await page.goto(`/workspace/chat/${oldSession}`);
    await expect(page.getByRole('button', { name: '点踩', exact: true }).last()).toBeVisible();
    const feedbackResponse = responseFor(page, '/feedback', 'POST');
    await page.getByRole('button', { name: '点踩', exact: true }).last().click();
    expect((await feedbackResponse).status()).toBe(200);
    await page.goto(FEEDBACK);
    const title = oldState.sessions.find((row) => row.id === oldSession)?.title;
    if (!title) throw new Error('Actual source session title missing');
    await page.getByRole('row').filter({ hasText: title }).getByRole('button', { name: '查看', exact: true }).click();
    await page.getByRole('dialog', { name: '对话日志详情' }).getByRole('button', { name: '预览改进材料', exact: true }).click();
    const preview = page.getByRole('dialog', { name: '预览改进材料' });
    await preview.getByRole('textbox', { name: '用户说明' }).fill('查询本人E001应得到1500，却查询关联人E002得到600。只修正query节点的metadata.operation_input.employee_id为slots.employee_id；保留输入、工具和其余流程。');
    const prepared = page.waitForResponse((row) => row.request().method() === 'POST' && new URL(row.url()).pathname.endsWith('/materials/preview'));
    await preview.getByRole('button', { name: '生成脱敏预览' }).click();
    const material = await (await prepared).json() as FeedbackMaterialRead;
    expect(material.material.source.status).toBe('verified');
    expect(material.material.source.skill_version_id).toBe(original.publication_id);
    expect(material.material.source.execution_id).toBe(old.id);
    expect(material.material.source.definition_checksum).toBe(original.publication_checksum);
    expect(material.material.source.agent_id).toBe(AGENT);
    expect(oldState.messages.some((row) => row.id === material.material.source.turn_id
      && row.session_id === oldSession && row.role === 'user')).toBe(true);
    expect(oldState.messages.some((row) => row.id === material.material.source.message_id
      && row.session_id === oldSession && row.role === 'assistant')).toBe(true);
    await preview.getByRole('button', { name: '确认分享给该员工管理者' }).click();
    await expect(preview).toHaveCount(0);
    await checkpoint('owner-shared-actual-failure');

    let candidate = await openCandidate(manager);
    const candidateUrl = manager.url();
    const sourceRevision = candidate.revision;
    // 真正模型结果必须直接满足调用前冻结标准，不能人工注入正确绑定。
    await manager.getByRole('button', { name: '全选', exact: true }).click();
    const beforeCalls = (await sdk()).length;
    await manager.getByRole('textbox', { name: '改写选中内容' }).fill(
      '仅修正query节点metadata.operation_input.employee_id从slots.sponsor_id为slots.employee_id。保持skill_id、三个node_id、tool sd7.quota_query、输入employee_id/sponsor_id、edges和其余metadata不变。不得硬编码额度或员工号。反馈是目标缺失证据，必须输出可编译完整原SkillCard或局部patch。');
    await manager.getByRole('button', { name: '发送', exact: true }).click();
    await expect.poll(async () => {
      const row = await read<SkillCandidateRead>(manager, `/api/enterprise/skills/feedback-candidates/${candidate.id}?tenant_id=tenant_demo`);
      candidate = row;
      return row.revision;
    }, { timeout: 300_000, intervals: [1000, 2000] }).toBeGreaterThan(sourceRevision);
    expect(metadata(candidate.content?.nodes.find((row) => row.node_id === 'query')).operation_input)
      .toEqual({ employee_id: 'slots.employee_id' });
    expect(candidate.content?.required_info).toEqual(original.content.required_info);
    expect(candidate.content?.nodes.map((row) => row.node_id)).toEqual(original.content.nodes.map((row) => row.node_id));
    expect(candidate.content?.edges).toEqual(original.content.edges);
    if (!candidate.content) throw new Error('Actual model candidate missing');
    const expectedBehavior = behavior(original.content);
    const expectedQuery = expectedBehavior.nodes.find((row) => row.node_id === 'query');
    if (!expectedQuery) throw new Error('Frozen original query missing');
    metadata(expectedQuery).operation_input = { employee_id: 'slots.employee_id' };
    expect(behavior(candidate.content)).toEqual(expectedBehavior);
    // 原先冻结的说明许可不允许硬编码业务答案；额度和员工号仍由输入/实际工具获得。
    const readableInstructions = [candidate.content.description,
      ...candidate.content.nodes.map((row) => String(row.instruction || ''))].join('\n');
    expect(readableInstructions).not.toMatch(/\bE00[12]\b|\b1500\b|\b600\b/i);
    const realCalls = (await sdk()).slice(beforeCalls).filter((row) => row.rewrite && row.actual_provider_call);
    expect(realCalls.some((row) => row.status === 'completed')).toBe(true);
    expect((await head(manager)).publication_id).toBe(original.publication_id);
    await manager.reload();
    await expect(manager.getByRole('textbox', { name: 'SOP名称', exact: true })).toBeVisible();
    await checkpoint('real-model-candidate-validated');

    const review = manager.getByRole('region', { name: '反馈候选审核' });
    await review.getByText('查看当前原版与已保存候选差异').click();
    await expect(review.getByText('slots.employee_id', { exact: false }).last()).toBeVisible();
    await review.getByRole('button', { name: '人工确认发布候选' }).click();
    const confirmation = manager.getByRole('dialog', { name: '确认沿现有发布链发布候选' });
    const published = responseFor(manager, `/feedback-candidates/${candidate.id}/publish`, 'POST');
    await confirmation.getByRole('button', { name: '确认发布', exact: true }).dblclick();
    const receipt = await (await published).json() as SkillCandidateReceipt;
    expect(receipt.skill_version_id).not.toBe(original.publication_id);
    expect((await head(manager)).publication_id).toBe(receipt.skill_version_id);
    await manager.reload();
    await expect(review.getByRole('status')).toContainText(receipt.skill_version_id);
    expect((await state(page)).business_calls).toHaveLength(1);
    expect((await state(page)).publication_count).toBe(2);
    await checkpoint('manual-publication-confirmed');

    await draft(page);
    const newSession = await send(page, '查询本人额度 SD7-NEW：本人员工号是E001，关联人编号是E002。请完成本人的额度查询。');
    const improved = await instance(page, newSession, 'succeeded');
    expect(improved.skill_version_id).toBe(receipt.skill_version_id);
    expect(improved.checksum).toBe(receipt.definition_checksum);
    const improvedState = await state(page);
    const improvedOperation = improvedState.operations.filter((row) => row.instance_id === improved.id);
    expect(improvedOperation).toHaveLength(1);
    expect(improvedOperation[0]).toMatchObject({ employee_id: 'E001', status: 'succeeded' });
    expect(improvedState.business_calls).toHaveLength(2);
    expect(improvedState.business_calls[1]).toMatchObject({ employee_id: 'E001', quota: improvedState.business.E001 });
    expect(improvedState.business_calls[1].request_hash).toBe(improvedOperation[0].request_hash);
    expect(improvedOperation[0].result).toMatchObject({ employee_id: 'E001', quota: improvedState.business.E001 });
    await checkpoint('new-composer-business-success');

    await rm(join(CONTROL, 'restart.done'), { force: true });
    await writeFile(join(CONTROL, 'restart.request'), 'restart-owned-runtime');
    await expect.poll(async () => { try { await access(join(CONTROL, 'restart.done')); return true; } catch { return false; } },
      { timeout: 120_000 }).toBe(true);
    await expect.poll(async () => { try { return (await state(page)).instances.some((row) => row.id === waiting.id); } catch { return false; } },
      { timeout: 120_000 }).toBe(true);
    await page.goto(`/workspace/chat/${waitingSession}`);
    await send(page, 'SD7-RESUME：本人员工号E001，关联人编号E002，请继续原来的本人额度查询。');
    const resumed = await instance(page, waitingSession, 'succeeded');
    expect(resumed.id).toBe(waiting.id);
    expect(resumed.skill_version_id).toBe(original.publication_id);
    expect(resumed.checksum).toBe(waiting.checksum);
    const restored = await state(page);
    const restoredOperation = restored.operations.filter((row) => row.instance_id === waiting.id);
    expect(restoredOperation).toHaveLength(1);
    expect(restoredOperation[0].employee_id).toBe('E002');
    expect(restored.business_calls).toHaveLength(3);
    expect(restored.business_calls[2].request_hash).toBe(restoredOperation[0].request_hash);
    await page.reload();
    await expect(page.getByPlaceholder('输入消息，按 Enter 发送...')).toBeVisible();
    expect((await state(page)).business_calls).toHaveLength(3);
    await checkpoint('restarted-old-instance-retains-original');
    const improvedSourceMessage = restored.messages.find((row) =>
      row.session_id === newSession && row.role === 'user');
    if (!improvedSourceMessage) throw new Error('Actual SOP feedback source message missing');
    const improvedIdentity = Object.fromEntries([
      ['tenant_id', 'tenant_demo'], ['user_id', 'sd7_owner'], ['agent_id', AGENT],
      ['session_id', newSession], ['source_input_id', improvedSourceMessage.id],
    ].map(([key, value]) => [key, createHash('sha256').update(JSON.stringify(value)).digest('hex')]));
    await info.attach('f3-safe-criterion', { contentType: 'application/json', body: Buffer.from(JSON.stringify({
      criterion: 'case-sdk-identity-binding', case_id: 'sop-feedback_improvement',
      stage: 'candidate-published-business-readback', passed: true,
      identity_sha256: improvedIdentity,
    })) });
    await info.attach('f3-safe-criterion', { contentType: 'application/json', body: Buffer.from(JSON.stringify({
      criterion: 'SOP-feedback-improvement', case_id: 'sop-feedback_improvement',
      stage: 'candidate-published-business-readback', passed: true,
      version_binding_valid: true, citation_check_passed: true,
      feedback_source_preserved: true, no_sop_candidate: false,
      real_vendor_qualified: false, business_provider_kind: 'simulated_async_service',
    })) });
    await info.attach('sd7-f3-safe-journey', { contentType: 'application/json', body: Buffer.from(JSON.stringify({
      original_id: original.publication_id, published_id: receipt.skill_version_id,
      definition_checksum: receipt.definition_checksum, material_id: material.id, candidate_id: candidate.id,
      old_instance_id: old.id, old_operation_id: oldOperation[0].id,
      new_instance_id: improved.id, new_operation_id: improvedOperation[0].id,
      waiting_instance_id: waiting.id, restored_operation_id: restoredOperation[0].id,
      old_goal_missing: true, actual_rewrite: true, independent_candidate_matches: true,
      exact_new_version: true, actual_business_success: true, restarted_old_frozen: true,
      reload_no_duplicate_dispatch: true, real_provider_quality: true,
      sdk_call_ids: realCalls.map((row) => row.call_id), business_dispatches: 3,
    })) });
    expect(candidateUrl).toContain(candidate.id);
  } finally { await context.close(); }
});

test('F3 FB-R05 negatives: rejection, compile failure, model failure, stale base, unauthorized scope, withdrawal and DTA feedback', async ({ page, browser }, info) => {
  const context = await browser.newContext({ baseURL: info.project.use.baseURL });
  const manager = await context.newPage();
  const adminContext = await browser.newContext({ baseURL: info.project.use.baseURL });
  const admin = await adminContext.newPage();
  page.setDefaultTimeout(30_000); manager.setDefaultTimeout(30_000); admin.setDefaultTimeout(30_000);
  const checkpoint = async (phase: string) => info.attach('sd7-f3-safe-journey', {
    contentType: 'application/json', body: Buffer.from(JSON.stringify({ phase })),
  });
  try {
    await login(page, 'sd7-owner', 'FULLSTACK_SD7_OWNER_PASSWORD');
    await login(manager, 'sd7-manager', 'FULLSTACK_SD7_MANAGER_PASSWORD');
    const baseline = await head(manager);
    const dispatches = (await state(page)).business_calls.length;
    expect(dispatches).toBe(3);
    const review = manager.getByRole('region', { name: '反馈候选审核' });
    const unchanged = async (expectedId = baseline.publication_id) => {
      expect((await head(manager)).publication_id).toBe(expectedId);
      expect((await state(page)).business_calls).toHaveLength(dispatches);
    };

    // 拒绝与刷新都是实际页面行为，普通写身份只能明确重新加载原版获得。
    let candidate = await openCandidate(manager);
    await review.getByRole('button', { name: '拒绝此候选' }).click();
    await expect(review.getByRole('alert')).toBeVisible();
    await manager.reload();
    await expect(review.getByRole('alert')).toBeVisible();
    await unchanged();
    await review.getByRole('button', { name: '丢弃并加载当前范围原版' }).click();
    await expect(manager.getByRole('textbox', { name: 'SOP名称', exact: true })).toBeVisible();
    await checkpoint('negative-reject-reload-keeps-head');

    // 可读节点类型编辑生成合法SkillCard但不可编译的子流程；真正发布返回422。
    candidate = await openCandidate(manager);
    await manager.getByRole('combobox').filter({ hasText: '调用工具' }).click();
    await manager.getByRole('option', { name: '子流程', exact: true }).click();
    const saveBad = responseFor(manager, `/feedback-candidates/${candidate.id}`, 'PUT');
    await review.getByRole('button', { name: '仅保存隔离候选' }).click();
    expect((await saveBad).status()).toBe(200);
    await review.getByRole('button', { name: '人工确认发布候选' }).click();
    const confirm = manager.getByRole('dialog', { name: '确认沿现有发布链发布候选' });
    await confirm.getByRole('checkbox', { name: '我已核对原反馈版本与当前基础的差异' }).click();
    const invalidPublication = responseFor(manager, `/feedback-candidates/${candidate.id}/publish`, 'POST');
    await confirm.getByRole('button', { name: '确认发布', exact: true }).click();
    expect((await invalidPublication).status()).toBe(422);
    await expect(review.getByRole('alert')).toBeVisible();
    await unchanged();
    await review.getByRole('button', { name: '丢弃并加载当前范围原版' }).click();
    await checkpoint('negative-compile-failure-keeps-head');

    // 末端SDK固定故障仅验证失败处理，不计作真实供应商质量或成功改写。
    candidate = await openCandidate(manager);
    const revision = candidate.revision;
    const beforeSDK = (await sdk()).length;
    await writeFile(join(CONTROL, 'model-failure'), 'fixed-protocol-negative');
    try {
      await manager.getByRole('textbox', { name: '改写选中内容' }).fill('只修正本人参数绑定，保留其余行为。');
      await manager.getByRole('button', { name: '发送', exact: true }).click();
      await expect(review.getByRole('alert')).toContainText('候选已失效或权限已变化', { timeout: 120_000 });
      await expect(manager.getByRole('textbox', { name: 'SOP名称', exact: true })).toHaveCount(0);
      const calls = (await sdk()).slice(beforeSDK).filter((row) => row.rewrite);
      expect(calls.length).toBeGreaterThan(0);
      expect(calls.every((row) => !row.actual_provider_call && row.status === 'controlled_failure')).toBe(true);
    } finally { await rm(join(CONTROL, 'model-failure'), { force: true }); }
    await manager.reload();
    const untouched = await read<SkillCandidateRead>(manager,
      `/api/enterprise/skills/feedback-candidates/${candidate.id}?tenant_id=tenant_demo`);
    expect(untouched.revision).toBe(revision);
    await unchanged();
    await checkpoint('negative-model-failure-keeps-head');

    // 使用者只具执行与本人反馈权，不能读取另一个管理者的受管候选。
    const candidateUrl = manager.url();
    await page.goto(candidateUrl);
    const ownerReview = page.getByRole('region', { name: '反馈候选审核' });
    await expect(ownerReview.getByRole('alert')).toBeVisible();
    await expect(page.getByRole('textbox', { name: 'SOP名称', exact: true })).toHaveCount(0);
    await expect(ownerReview.getByRole('button', { name: '人工确认发布候选' })).toHaveCount(0);
    await page.goto(candidateUrl.replace(`agent_id=${AGENT}`, 'agent_id=agent_e2e_employee'));
    await expect(ownerReview.getByRole('alert')).toBeVisible();
    await unchanged();
    await checkpoint('negative-unauthorized-scope-refused');

    // 另一个真正管理员沿普通原编辑链更新基础；原候选不能陈旧覆盖。
    await login(admin, 'admin', 'FULLSTACK_SD7_ADMIN_PASSWORD');
    await admin.goto(`/enterprise/skills/distill?skill_id=${SKILL}&agent_id=${AGENT}`);
    await expect(admin.getByRole('textbox', { name: 'SOP名称', exact: true })).toBeVisible();
    await admin.getByRole('textbox', { name: 'SOP名称', exact: true }).fill('本人额度查询-合法新基础');
    await admin.getByRole('button', { name: '保存草稿', exact: true }).click();
    const ordinarySave = admin.getByRole('dialog', { name: '保存SOP版本' });
    await ordinarySave.getByRole('button', { name: '保存', exact: true }).click();
    await expect(ordinarySave).toHaveCount(0);
    const current = await head(admin);
    expect(current.publication_id).not.toBe(baseline.publication_id);
    await review.getByRole('button', { name: '重新校验候选' }).click();
    await expect(review.getByRole('alert')).toBeVisible();
    await unchanged(current.publication_id);
    await review.getByRole('button', { name: '丢弃并加载当前范围原版' }).click();
    await checkpoint('negative-stale-base-refused');

    // 本人撤回后旧页面、已保存私密编辑和刷新续接均不能复活。
    candidate = await openCandidate(manager);
    await manager.getByRole('textbox', { name: 'SOP名称', exact: true }).fill('SD7_PRIVATE_WITHDRAWN_CANDIDATE');
    const savedPrivate = responseFor(manager, `/feedback-candidates/${candidate.id}`, 'PUT');
    await review.getByRole('button', { name: '仅保存隔离候选' }).click();
    expect((await savedPrivate).status()).toBe(200);
    await manager.getByRole('textbox', { name: '改写选中内容' }).fill('只修正本人参数绑定，保留其余行为。');
    await manager.getByRole('button', { name: '发送', exact: true }).click();
    const pointerKey = `skill-feedback-job:tenant_demo:${candidate.id}`;
    let withdrawnJob = '';
    await expect.poll(async () => {
      withdrawnJob = await manager.evaluate((key) => {
        const value: unknown = JSON.parse(sessionStorage.getItem(key) || 'null');
        return value && typeof value === 'object' && 'job_id' in value
          && typeof value.job_id === 'string' ? value.job_id : '';
      }, pointerKey);
      return withdrawnJob;
    }, { timeout: 120_000 }).not.toBe('');
    await page.goto(FEEDBACK);
    const withdrawn = responseFor(page, '/withdraw', 'POST');
    await page.getByRole('button', { name: '撤回分享' }).click();
    expect((await withdrawn).status()).toBe(200);
    await manager.reload();
    await expect(review.getByRole('alert')).toBeVisible();
    await expect(manager.getByRole('textbox', { name: 'SOP名称', exact: true })).toHaveCount(0);
    const cacheStatus = await manager.evaluate(async (job) => {
      const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
      const response = await fetch(`/api/enterprise/skills/jobs/${job}?tenant_id=tenant_demo`,
        { headers: { Authorization: `Bearer ${auth.token || ''}` } });
      return response.status;
    }, withdrawnJob);
    expect([403, 404, 409]).toContain(cacheStatus);
    await unchanged(current.publication_id);
    await checkpoint('negative-withdrawal-refresh-refused');

    // 普通DTA真实运行及本人反馈保留，分享不能伪造SOP版本或进入SOP候选。
    await draft(page, true);
    const dtaSession = await send(page, 'SD7-DTA：请用两句话解释本人编号和关联人编号的区别，不要调用工具。');
    const dta = await instance(page, dtaSession, 'succeeded');
    expect(dta.kind).toBe('dynamic_task');
    expect(dta.skill_version_id).toBeNull();
    await page.goto(`/workspace/chat/${dtaSession}`);
    const dtaFeedback = responseFor(page, '/feedback', 'POST');
    await page.getByRole('button', { name: '点踩', exact: true }).last().click();
    expect((await dtaFeedback).status()).toBe(200);
    // 标题可合法为空；从当前页面真实列表响应按session ID读取原显示合同，不猜来源。
    const sourcePage = page.waitForResponse((row) => row.request().method() === 'GET'
      && new URL(row.url()).pathname === '/api/enterprise/sessions/page'
      && new URL(row.url()).searchParams.get('agent_id') === AGENT);
    await page.goto(FEEDBACK);
    const listed = await sourcePage;
    expect(listed.status()).toBe(200);
    const sourceRows = await listed.json() as ConversationSessionPageRead;
    const sourceRow = sourceRows.items.find((row) => row.id === dtaSession);
    if (!sourceRow) throw new Error('Actual DTA source row missing');
    const sourceLabel = sourceRow.title || sourceRow.summary || sourceRow.last_agent_question || sourceRow.id;
    await page.getByRole('row').filter({ hasText: sourceLabel }).getByRole('button', { name: '查看', exact: true }).click();
    await page.getByRole('dialog', { name: '对话日志详情' }).getByRole('button', { name: '预览改进材料', exact: true }).click();
    const preview = page.getByRole('dialog', { name: '预览改进材料' });
    const prepared = page.waitForResponse((row) => row.request().method() === 'POST' && new URL(row.url()).pathname.endsWith('/materials/preview'));
    await preview.getByRole('button', { name: '生成脱敏预览' }).click();
    const material = await (await prepared).json() as FeedbackMaterialRead;
    expect(material.material.source.branch).toBe('dynamic_task');
    expect(material.material.source.execution_id).toBe(dta.id);
    expect(material.material.source.skill_version_id).toBeNull();
    await preview.getByRole('button', { name: '确认分享给该员工管理者' }).click();
    await manager.goto(FEEDBACK);
    await manager.getByRole('button', { name: '查看共享材料' }).click();
    await expect(manager.getByText('dynamic_task', { exact: true })).toBeVisible();
    await expect(manager.getByText(dta.id, { exact: true })).toBeVisible();
    await expect(manager.getByRole('button', { name: '在当前编辑器改进 SOP' })).toHaveCount(0);
    await unchanged(current.publication_id);
    await checkpoint('negative-dta-no-fictitious-sop-source');
    const dtaSourceMessage = (await state(page)).messages.find((row) =>
      row.session_id === dtaSession && row.role === 'user');
    if (!dtaSourceMessage) throw new Error('Actual DTA source message missing');
    const dtaIdentity = Object.fromEntries([
      ['tenant_id', 'tenant_demo'], ['user_id', 'sd7_owner'], ['agent_id', AGENT],
      ['session_id', dtaSession], ['source_input_id', dtaSourceMessage.id], ['instance_id', dta.id],
    ].map(([key, value]) => [key, createHash('sha256').update(JSON.stringify(value)).digest('hex')]));
    await info.attach('f3-safe-criterion', { contentType: 'application/json', body: Buffer.from(JSON.stringify({
      criterion: 'case-sdk-identity-binding', case_id: 'dynamic_task-feedback_improvement',
      stage: 'dta-feedback-source', passed: true,
      identity_sha256: Object.fromEntries(Object.entries(dtaIdentity)
        .filter(([key]) => key !== 'instance_id')),
    })) });
    await info.attach('f3-safe-criterion', { contentType: 'application/json', body: Buffer.from(JSON.stringify({
      criterion: 'DTA-feedback-source-preserved', case_id: 'dynamic_task-feedback_improvement',
      stage: 'dta-feedback-source', passed: true, version_binding_valid: true,
      citation_check_passed: true, identity_sha256: dtaIdentity,
      feedback_source_preserved: true, no_sop_candidate: true,
      real_vendor_qualified: false, business_provider_kind: 'simulated_async_service',
    })) });
    await info.attach('sd7-f3-safe-negatives', { contentType: 'application/json', body: Buffer.from(JSON.stringify({
      rejected_keeps_head: true, compiled_failure_keeps_head: true, model_failure_keeps_head: true,
      unauthorized_scope_refused: true, stale_base_refused: true, withdrawal_refresh_refused: true,
      ordinary_dta_kept: true, dta_instance_id: dta.id, no_negative_business_dispatch: true,
      initial_publication_id: baseline.publication_id, current_publication_id: current.publication_id,
      business_dispatches: dispatches,
    })) });
  } finally {
    await rm(join(CONTROL, 'model-failure'), { force: true });
    await context.close(); await adminContext.close();
  }
});

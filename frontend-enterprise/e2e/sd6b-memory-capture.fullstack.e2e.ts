/**
 * @Time       : 2026/10/01
 * @Author     : zhanglp8181
 * @File       : sd6b-memory-capture.fullstack.e2e.ts
 * @CallChain  : Chromium Composer → 原完成事务/job → 双库 worker → 新 DTA 消费
 * @Description: 精确核对本轮 job，并通过页面验证更正、迟到、清空竞争和隔离进程重启。
 */
import { expect, test, type Page, type TestInfo } from '@playwright/test';
import { access, mkdir, writeFile, rm } from 'node:fs/promises';
import { join, resolve } from 'node:path';

const SOP_AGENT_ID = 'agent_e2e_member_employee';
const DTA_AGENT_ID = 'agent_e2e_employee';
const SOP_SESSION_ID = 'session_attachment_sales_sop';
const CONTROL_DIR = process.env.FULLSTACK_SD6B_CONTROL_DIR || '';
const FIXTURE = resolve('../backend/tests/fixtures/attachments/positive');

type SessionFact = {
  messages: Array<{ id: string; role: string; content: string; metadata?: Record<string, unknown> }>;
  events: Array<{ event_type?: string; data?: Record<string, unknown> }>;
};
type MemoryJob = {
  job_id: string; status: string; model_dispatch_count: number; attempt_count: number;
  applied_count: number; discarded_count: number; reason_code?: string | null;
};
type Turn = { sessionId: string; sourceId: string; assistantId: string; jobId: string };

async function login(page: Page, username: string, password: string, agentId: string): Promise<void> {
  /** 通过隔离登录建立当前 Composer 使用的租户、用户和 Agent 作用域。 */

  await page.goto('/enterprise/dashboard');
  const status = await page.evaluate(async ({ username, password, agentId }) => {
    const response = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tenant_id: 'tenant_demo', username, password }),
    });
    const body = await response.json() as Record<string, unknown>;
    if (response.ok) {
      localStorage.setItem('gongge_auth', JSON.stringify(body));
      localStorage.setItem('gongge_onboarding_guide_seen', '1');
      localStorage.setItem('gongge_quick_start_guide_seen', '1');
      localStorage.setItem('gongge_enterprise_agent_scope', agentId);
    }
    return response.status;
  }, { username, password, agentId });
  expect(status).toBe(200);
}

async function getJson<T>(page: Page, path: string): Promise<T> {
  /** 旁证只读正式 API；认证数据始终留在浏览器内，不进入报告。 */
  return page.evaluate(async (path) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch(path, { headers: { Authorization: `Bearer ${auth.token || ''}` } });
    if (!response.ok) throw new Error(`Read failed: ${response.status}`);
    return await response.json();
  }, path);
}

async function facts(page: Page, id: string): Promise<SessionFact> {
  const [messages, events] = await Promise.all([
    getJson<SessionFact['messages']>(page, `/api/chat/sessions/${id}/messages?tenant_id=tenant_demo`),
    getJson<SessionFact['events']>(page, `/api/chat/sessions/${id}/events?tenant_id=tenant_demo`),
  ]);
  return { messages, events };
}

async function send(page: Page, message: string, sessionId = ''): Promise<Turn> {
  /** 关键输入走 Composer；消息和入队事件精确绑定本轮，禁止 first-new-job 猜测。 */
  const composer = page.getByPlaceholder('输入消息，按 Enter 发送...');
  await expect(composer).toBeVisible({ timeout: 30_000 });
  await composer.fill(message);
  const response = page.waitForResponse((response) => response.request().method() === 'POST'
    && new URL(response.url()).pathname === '/api/chat/stream');
  await page.getByRole('button', { name: '发送', exact: true }).click();
  expect((await response).status()).toBe(200);
  if (!sessionId) {
    await expect(page).toHaveURL(/\/workspace\/chat\/session_[^/]+$/, { timeout: 30_000 });
    sessionId = new URL(page.url()).pathname.split('/').pop() || '';
  }
  let turn: Turn | undefined;
  await expect.poll(async () => {
    const value = await facts(page, sessionId);
    const source = value.messages.find((row) => row.role === 'user' && row.content === message);
    const assistant = value.messages.find((row) => row.role === 'assistant'
      && (row.metadata?.user_message_id === source?.id || row.metadata?.turn_id === source?.id));
    const enqueued = value.events.find((row) => row.event_type === 'async_job_enqueued'
      && row.data?.source_message_id === source?.id && row.data?.assistant_message_id === assistant?.id);
    const complete = value.events.find((row) => row.event_type === 'complete'
      && row.data?.user_message_id === source?.id);
    const created = value.events.find((row) => row.event_type === 'assistant_message_created'
      && row.data?.user_message_id === source?.id && row.data?.assistant_message_id === assistant?.id);
    if (source && assistant && enqueued && complete && created && typeof enqueued.data?.job_id === 'string') {
      turn = { sessionId, sourceId: source.id, assistantId: assistant.id, jobId: enqueued.data.job_id };
    }
    return Boolean(turn);
  }, { timeout: 90_000, intervals: [250, 500, 1000] }).toBe(true);
  if (!turn) throw new Error('Current source/assistant/complete/job association is missing');
  return turn;
}

async function newDtaTurn(page: Page, marker: string): Promise<Turn> {
  /** 正式草稿页面与 Composer 创建新会话，不用 API 写业务输入或预置任务。 */
  await page.goto(`/workspace/chat/draft/${DTA_AGENT_ID}`);
  const engine = page.getByRole('button', { name: '选择 DynamicTaskAgent 复杂任务引擎' });
  await expect(engine).toBeVisible({ timeout: 30_000 });
  if (await engine.getAttribute('aria-pressed') !== 'true') await engine.click();
  return send(page, `No content length specified for stream data；${marker}：请完成任务并处理本轮长期偏好。`);
}

async function jobs(page: Page, agentId = DTA_AGENT_ID): Promise<MemoryJob[]> {
  const result = await getJson<{ items: MemoryJob[] }>(page,
    `/api/enterprise/memories/jobs?tenant_id=tenant_demo&agent_id=${agentId}&page=1&page_size=100`);
  return result.items;
}

async function waitJob(page: Page, turn: Turn, status = 'succeeded', agentId = DTA_AGENT_ID): Promise<MemoryJob> {
  let result: MemoryJob | undefined;
  await expect.poll(async () => {
    result = (await jobs(page, agentId)).find((row) => row.job_id === turn.jobId);
    return result?.status;
  }, { timeout: 90_000, intervals: [250, 500, 1000] }).toBe(status);
  if (!result) throw new Error('Associated job is missing');
  return result;
}

async function saveEvidence(info: TestInfo, data: object): Promise<void> {
  await info.attach('sd6b-safe-journey', { body: Buffer.from(JSON.stringify(data, null, 2)), contentType: 'application/json' });
}

async function control(name: string, remove = false): Promise<void> {
  if (!CONTROL_DIR) throw new Error('Isolated SD6B control directory is required');
  await mkdir(CONTROL_DIR, { recursive: true });
  if (remove) await rm(join(CONTROL_DIR, name), { force: true });
  else await writeFile(join(CONTROL_DIR, name), 'synthetic-browser-control');
}

async function waitControl(name: string): Promise<void> {
  await expect.poll(async () => {
    try { await access(join(CONTROL_DIR, name)); return true; } catch { return false; }
  }, { timeout: 30_000 }).toBe(true);
}

async function memoryPage(page: Page): Promise<void> {
  await page.goto('/enterprise/memories');
  await expect(page.getByRole('tab', { name: '记忆' })).toHaveAttribute('data-state', 'active');
  await page.getByLabel('切换当前员工').first().click();
  const employee = page.getByRole('menuitem', { name: /E2E 数字员工/ });
  await expect(employee).toBeVisible({ timeout: 30_000 });
  await employee.click();
  await expect.poll(() => page.evaluate(() => localStorage.getItem('gongge_enterprise_agent_scope'))).toBe(DTA_AGENT_ID);
  await page.goto('/enterprise/memories');
  await expect(page.getByRole('tab', { name: '记忆' })).toHaveAttribute('data-state', 'active');
}

async function clearOwn(page: Page): Promise<void> {
  await memoryPage(page);
  page.once('dialog', (dialog) => dialog.accept());
  const cleared = page.waitForResponse((response) => response.request().method() === 'DELETE'
    && new URL(response.url()).pathname === '/api/enterprise/memories/me');
  await page.getByRole('button', { name: '清空我的记忆' }).click();
  expect((await cleared).status()).toBe(200);
}

async function assertFact(page: Page, expected: string): Promise<void> {
  await memoryPage(page);
  await page.getByRole('textbox', { name: '用户名', exact: true }).fill('admin');
  await page.getByRole('button', { name: '查询', exact: true }).click();
  await expect(page.getByRole('table', { name: '员工记忆' }).getByText(expected, { exact: false }).first()).toBeVisible({ timeout: 30_000 });
}

async function consume(page: Page, expected: 'DTA' | 'UPDATE' | 'FRESH'): Promise<Turn> {
  const turn = await newDtaTurn(page, `SD6B-MEMORY-CONSUME EXPECT=${expected}`);
  const value = await facts(page, turn.sessionId);
  const token = expected === 'UPDATE' ? 'SD6B 浏览器更新事实'
    : expected === 'FRESH' ? 'SD6B 浏览器新事实' : 'SD6B 浏览器 DTA 事实';
  expect(value.messages.find((row) => row.id === turn.assistantId)?.content).toContain('memory_context=present');
  expect(value.messages.find((row) => row.id === turn.assistantId)?.content).toContain(`fact=${token}`);
  return turn;
}

test('SOP/DTA Composer binds the exact source, assistant, completion and job; refresh and clear stay stable', async ({ page }, info) => {
  test.setTimeout(240_000);
  await login(page, 'member', 'member', SOP_AGENT_ID);
  await page.goto(`/workspace/chat/${SOP_SESSION_ID}`);
  await expect(page.getByText('Hello E2E 成员数字员工！')).toBeVisible({ timeout: 30_000 });
  await page.locator('input[type="file"]').setInputFiles([
    join(FIXTURE, 'sales_actuals.xlsx'), join(FIXTURE, 'sales_targets.csv'),
  ]);
  await expect(page.getByText('sales_actuals.xlsx', { exact: true })).toBeVisible();
  await expect(page.getByText('解析中', { exact: true })).toHaveCount(0, { timeout: 30_000 });
  const sop = await send(page, 'ATTACHMENT-SOP-SALES SD6B-MEMORY-SOP：请按已发布SOP核验附件并保留我的长期偏好。', SOP_SESSION_ID);
  const sopJob = await waitJob(page, sop, 'succeeded', SOP_AGENT_ID);
  expect(sopJob).toMatchObject({ model_dispatch_count: 1, attempt_count: 1, applied_count: 1 });
  await login(page, 'admin', 'admin', DTA_AGENT_ID);
  const dta = await newDtaTurn(page, 'SD6B-MEMORY-DTA');
  const dtaJob = await waitJob(page, dta);
  expect(dtaJob).toMatchObject({ model_dispatch_count: 1, attempt_count: 1, applied_count: 1 });
  const consumed = await consume(page, 'DTA');
  const consumedJob = await waitJob(page, consumed);
  expect(consumedJob).toMatchObject({ model_dispatch_count: 1, applied_count: 0 });
  await memoryPage(page);
  await expect(page.getByLabel('记忆提取状态').getByText('已尝试 1/3 次').first()).toBeVisible();
  await page.reload();
  expect((await jobs(page)).find((row) => row.job_id === dta.jobId)?.model_dispatch_count).toBe(1);
  await clearOwn(page);
  const result = await getJson<{ total: number }>(page,
    `/api/enterprise/memories/page?tenant_id=tenant_demo&agent_id=${DTA_AGENT_ID}&user_id=admin&page=1&page_size=10`);
  expect(result.total).toBe(0);
  await saveEvidence(info, { sop, sopJob, dta, dtaJob, consumed, consumedJob, clearedUserGroups: result.total });
});

test('new correction wins over a late old turn; clear rejects late output and fresh input is consumed', async ({ page }, info) => {
  test.setTimeout(240_000);
  await login(page, 'admin', 'admin', DTA_AGENT_ID);
  const late = await newDtaTurn(page, 'SD6B-MEMORY-LATE-UPDATE');
  await waitControl('late-update.started');
  await waitJob(page, late, 'running');
  const corrected = await newDtaTurn(page, 'SD6B-MEMORY-UPDATE');
  await waitJob(page, corrected);
  await assertFact(page, 'SD6B 浏览器更新事实');
  await control('late-update.release');
  const lateJob = await waitJob(page, late);
  expect(lateJob).toMatchObject({ applied_count: 0, discarded_count: 1, reason_code: 'superseded' });
  await assertFact(page, 'SD6B 浏览器更新事实');
  const correctedConsumer = await consume(page, 'UPDATE');
  await waitJob(page, correctedConsumer);
  const clearingLate = await newDtaTurn(page, 'SD6B-MEMORY-LATE-CLEAR');
  await waitControl('late-clear.started');
  await waitJob(page, clearingLate, 'running');
  await clearOwn(page);
  await control('late-clear.release');
  const discarded = await waitJob(page, clearingLate, 'discarded');
  expect(discarded.applied_count).toBe(0);
  await page.reload();
  await expect(page.getByText('来源、权限或模型已变更；请在当前权限下重新发送新输入').first()).toBeVisible();
  expect((await getJson<{ total: number }>(page,
    `/api/enterprise/memories/page?tenant_id=tenant_demo&agent_id=${DTA_AGENT_ID}&user_id=admin&page=1&page_size=10`)).total).toBe(0);
  const fresh = await newDtaTurn(page, 'SD6B-MEMORY-FRESH');
  const freshJob = await waitJob(page, fresh);
  await assertFact(page, 'SD6B 浏览器新事实');
  const freshConsumer = await consume(page, 'FRESH');
  await waitJob(page, freshConsumer);
  await saveEvidence(info, { late, corrected, lateJob, correctedConsumer, clearingLate, discarded, fresh, freshJob, freshConsumer });
});

test('lost notification survives a real isolated server restart and the same pending job is consumed', async ({ page }, info) => {
  test.setTimeout(240_000);
  await login(page, 'admin', 'admin', DTA_AGENT_ID);
  await control('pause');
  await control('drop-notification');
  const turn = await newDtaTurn(page, 'SD6B-MEMORY-FRESH-RESTART');
  const pending = await waitJob(page, turn, 'pending');
  expect(pending).toMatchObject({ model_dispatch_count: 0, attempt_count: 0 });
  await memoryPage(page);
  await page.reload();
  expect((await jobs(page)).find((row) => row.job_id === turn.jobId)?.status).toBe('pending');
  await control('restart.request');
  await waitControl('restart.done');
  await expect.poll(async () => {
    try { return (await page.request.get('/api/health')).status(); } catch { return 0; }
  }, { timeout: 90_000 }).toBe(200);
  await control('pause', true);
  await control('drop-notification', true);
  await page.reload();
  const recovered = await waitJob(page, turn);
  expect(recovered).toMatchObject({ job_id: turn.jobId, attempt_count: 1, model_dispatch_count: 1 });
  await assertFact(page, 'SD6B 浏览器新事实');
  const consumer = await consume(page, 'FRESH');
  await waitJob(page, consumer);
  await saveEvidence(info, { turn, pending, recovered, consumer, restart: 'new server process, preserved isolated database, no wake notification' });
});

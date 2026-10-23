/**
 * @Time       : 2026/10/04
 * @Author     : zhanglp8181
 * @File       : final-f2-h2-helpers.ts
 * @CallChain  : H2真实页面/Composer → 原版本/持久worker → 独立白名单核验
 * @Description: 精确关联源/assistant/job，故障仅控制自有worker和服务时机。
 */
import { access, rm, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { expect, type Page, type TestInfo } from '@playwright/test';
import { facts, object, request, send, type Row } from './final-f1-browser-helpers';
import { sendDynamicInput } from './final-f1-dta-journey';
import { audit, authenticatedRead, switchStaff } from './final-f2-browser-helpers';

export const OLD_MARKER = 'F2_POLICY_OLD_750';
export const NEW_MARKER = 'F2_POLICY_NEW_1200';
export const NEW_BODY = '# CYGNUS77配送政策\n\nCYGNUS77每批上限1200件，必须保留办理日期。\n唯一政策标记：' + NEW_MARKER;
export const H2_FACTS = {
  base: 'F1 完整持久事实：H2_BASE_完整收件要求，先检查外包装，再逐件核验。',
  late: 'F1 完整持久事实：H2_LATE_过期收件要求，不能覆盖更正。',
  correct: 'F1 完整持久事实：H2_CORRECT_完整更正要求，由指定收件人逐件核验并保留原始回执。',
  clear: 'F1 完整持久事实：H2_CLEAR_LATE_清空之前的要求，不得复活。',
  fresh: 'F1 完整持久事实：H2_FRESH_完整新要求，清空后由本人确认核验并保留独立回执。',
};

export async function h2Facts(page: Page): Promise<Row> {
  /** 原只读观察器返回结构须逐个核对，未知字段不能假报闭包或恢复成功。 */
  const value = object(await request(page, '/__e2e/f2/h2/facts'));
  for (const key of ['versions', 'branches', 'assets', 'revision_jobs', 'memories', 'capture_jobs', 'projections', 'sdk']) {
    if (!Array.isArray(value[key])) throw new Error('F2_H2_ROWS_REQUIRED_' + key);
  }
  return value;
}

export function rows(value: Row, key: string): Row[] {
  /** 收窄白名单数组，不对任意JSON作未验证的字段断言。 */
  if (!Array.isArray(value[key])) throw new Error('F2_H2_ARRAY_REQUIRED_' + key);
  return value[key].map(object);
}

export async function control(name: string, remove = false): Promise<void> {
  /** 故障切点只属于此轮服务，无数据库写入、租约或时钟覆盖。 */
  const directory = process.env.FULLSTACK_SD6B_CONTROL_DIR;
  if (!directory) throw new Error('F2_H2_OWNED_CONTROL_REQUIRED');
  if (remove) await rm(join(directory, name), { force: true });
  else await writeFile(join(directory, name), 'f2-h2-owned-control');
}

export async function waitControl(name: string): Promise<void> {
  /** 原worker/真实SDK到达指定切点后才能执行竞争动作。 */
  await expect.poll(async () => {
    try { await access(join(process.env.FULLSTACK_SD6B_CONTROL_DIR || '', name)); return true; }
    catch { return false; }
  }, { timeout: 30_000 }).toBe(true);
}

export async function restart(page: Page, info: TestInfo): Promise<void> {
  /** 同库真实新进程，旧PID必须消失；不种子覆盖任何业务任务或回执。 */
  const oldPid = (await facts(page)).pid;
  await control('restart.done', true);
  await control('restart.request');
  await waitControl('restart.done');
  await expect.poll(async () => {
    try { return (await page.request.get('/api/health')).status(); } catch { return 0; }
  }, { timeout: 90_000 }).toBe(200);
  const newPid = (await facts(page)).pid;
  expect(newPid).not.toBe(oldPid);
  let gone = false;
  try { process.kill(oldPid, 0); } catch { gone = true; }
  expect(gone).toBe(true);
  await audit(info, 'h2-real-server-restart', { old_pid: oldPid, new_pid: newPid, old_process_absent: gone });
}

export async function draft(page: Page, kind: 'SOP' | 'DTA', dynamic = false): Promise<void> {
  /** 正式草稿和原引擎选择，保留本人认证及员工原scope。 */
  await page.goto(`/workspace/chat/draft/agent_f1_${kind.toLowerCase()}`);
  const engine = page.getByRole('button', { name: '选择 DynamicTaskAgent 复杂任务引擎', exact: true });
  await expect(engine).toBeVisible();
  if ((await engine.getAttribute('aria-pressed') === 'true') !== dynamic) await engine.click();
  await expect(engine).toHaveAttribute('aria-pressed', String(dynamic));
}

export async function completedTurn(page: Page, previous: Row[], kind: 'SOP' | 'DTA'): Promise<{
  source: Row; assistant: Row; job: Row;
}> {
  /** 本轮user/assistant/complete/enqueue四者唯一绑定，禁止首个新任务猜测。 */
  let result: { source: Row; assistant: Row; job: Row } | undefined;
  await expect.poll(async () => {
    const state = await facts(page);
    const source = state.messages.find(r => r.role === 'user' && !previous.some(old => old.id === r.id));
    if (!source) return false;
    const assistant = state.messages.find(r => r.role === 'assistant' && r.session_id === source.session_id
      && (object(r.metadata).user_message_id === source.id || object(r.metadata).turn_id === source.id));
    if (!assistant) return false;
    const job = state.jobs.find(r => r.source_input_id === source.id && r.assistant_message_id === assistant.id
      && r.agent_id === `agent_f1_${kind.toLowerCase()}` && r.user_id === 'admin');
    const complete = state.events.filter(r => r.event_type === 'complete' && r.user_message_id === source.id);
    const created = state.events.filter(r => r.event_type === 'assistant_message_created'
      && r.user_message_id === source.id && r.assistant_message_id === assistant.id);
    const enqueued = state.events.filter(r => r.event_type === 'async_job_enqueued'
      && r.source_message_id === source.id && r.assistant_message_id === assistant.id && r.job_id === job?.job_id);
    if (job && complete.length === 1 && created.length === 1 && enqueued.length === 1) result = { source, assistant, job };
    return Boolean(result);
  }, { timeout: 60_000 }).toBe(true);
  if (!result) throw new Error('F2_H2_EXACT_SOURCE_JOB_MISSING');
  return result;
}

export async function capture(page: Page, kind: 'SOP' | 'DTA', fact: string): Promise<{
  source: Row; assistant: Row; job: Row;
}> {
  /** 记忆输入走Composer和原canonical complete事务，不直接新建任务。 */
  await draft(page, kind);
  const before = (await facts(page)).messages;
  await send(page, '请记住我的稳定服务偏好：' + fact);
  return completedTurn(page, before, kind);
}

export async function captureState(page: Page, jobId: unknown, status: string): Promise<Row> {
  /** 核原持久job而非页面成功通知，迟到/清空分别匹配精确终态。 */
  await expect.poll(async () => rows(await h2Facts(page), 'capture_jobs').find(r => r.id === jobId)?.status,
    { timeout: 75_000 }).toBe(status);
  return object(rows(await h2Facts(page), 'capture_jobs').find(r => r.id === jobId));
}

export async function manageMemory(page: Page, kind: 'SOP' | 'DTA'): Promise<void> {
  /** 沿原员工菜单切换后重新进入记忆管理页，无scope存储注入。 */
  await page.goto('/enterprise/memories');
  if (!(await page.getByLabel('切换当前员工', { exact: true }).innerText()).includes(`F1 ${kind}员工`)) {
    await switchStaff(page, `F1 ${kind}员工`);
    await page.goto('/enterprise/memories');
  }
  await expect(page.getByRole('tab', { name: '记忆', exact: true })).toHaveAttribute('data-state', 'active');
}

export async function clearMemory(page: Page, kind: 'SOP' | 'DTA'): Promise<void> {
  /** 真实本人清空交互，generation推进和另一员工保留另由后台旁证判断。 */
  await manageMemory(page, kind);
  page.once('dialog', dialog => dialog.accept());
  const response = page.waitForResponse(r => new URL(r.url()).pathname === '/api/enterprise/memories/me'
    && r.request().method() === 'DELETE');
  await page.getByRole('button', { name: '清空我的记忆', exact: true }).click();
  expect((await response).status()).toBe(200);
}

export async function consume(page: Page, info: TestInfo, kind: 'SOP' | 'DTA', label: 'correct' | 'fresh'): Promise<void> {
  /** 原Router或真实DTA Planner/Action/最终答案必须使用完整当前事实；SOP Step/Reply不注入。 */
  await draft(page, kind, kind === 'DTA');
  const before = (await facts(page)).messages;
  const message = 'F1事实消费：请解释我记住的稳定服务要求；只需回答，不提交新的配送。';
  if (kind === 'DTA') await sendDynamicInput(page, message); else await send(page, message);
  const turn = await completedTurn(page, before, kind);
  const observed = rows(await h2Facts(page), 'sdk').filter(r => r.source_input_id === turn.source.id);
  for (const phase of kind === 'SOP' ? ['Router'] : ['Dynamic Planner', 'Dynamic Action']) {
    const selected = observed.filter(r => r.phase === phase);
    expect(selected).toHaveLength(1);
    expect(object(selected[0]?.memory_fact_flags)[label]).toBe(true);
    expect(object(selected[0]?.memory_fact_flags).late).toBe(false);
    expect(object(selected[0]?.memory_fact_flags).clear).toBe(false);
  }
  if (kind === 'SOP') expect(object(observed.find(r => r.phase === 'Router')?.result_fact_flags)[label]).toBe(true);
  else await expect(page.getByText(H2_FACTS[label], { exact: false }).last()).toBeVisible();
  await audit(info, `${kind}-h2-${label}-actual-consumption`, { source: turn.source, assistant: turn.assistant,
    job: turn.job, current_fact_full: true, sdk: observed });
}

export async function openEditor(page: Page): Promise<void> {
  /** 当前知识页面选择原多文档库和指定文档，保留其实际head/revision快照。 */
  await page.goto('/enterprise/knowledge');
  await expect(page.getByText('当前员工', { exact: true })).toBeVisible();
  const governance = page.getByRole('button', { name: '平台知识治理', exact: true });
  if (await governance.isVisible()) await governance.click();
  await page.getByPlaceholder('搜索知识库名称、描述、状态或版本').fill('F1配送政策');
  await page.getByRole('row').filter({ hasText: 'F1配送政策' }).click();
  await expect(page.getByRole('heading', { name: 'CYGNUS77配送政策', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '详情', exact: true }).first().click();
  await page.getByRole('button', { name: '修改', exact: true }).click();
  await expect(page.getByRole('textbox', { name: '文档正文' })).toBeVisible();
}

export async function saveBody(page: Page, body: string, expected = 202): Promise<Row> {
  /** 保存必须由原按钮发送原CAS和客户端身份；不伪造expected版本。 */
  await page.getByRole('textbox', { name: '文档正文' }).fill(body);
  const response = page.waitForResponse(r => r.request().method() === 'PUT'
    && new URL(r.url()).pathname.startsWith('/api/enterprise/knowledge/documents/'));
  await page.getByRole('button', { name: '保存正文', exact: true }).click();
  const result = await response;
  expect(result.status()).toBe(expected);
  return object(await result.json());
}

export async function revisionState(page: Page, jobId: unknown, status: string): Promise<Row> {
  /** 当前原GET读取精确revision任务终态，候选或其它job不能替代。 */
  let result: Row | undefined;
  await expect.poll(async () => {
    const read = await authenticatedRead(page, `/api/enterprise/knowledge/jobs/${String(jobId)}?tenant_id=tenant_demo`);
    expect(read.status).toBe(200);
    result = object(read.body);
    return result.status;
  }, { timeout: 75_000 }).toBe(status);
  return object(result);
}

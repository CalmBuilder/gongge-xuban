/**
 * @Time       : 2026/10/04
 * @Author     : zhanglp8181
 * @File       : final-f1-dta-journey.ts
 * @CallChain  : 本人Composer/澄清 → 独立批准人UI → 原DTA/Operation
 * @Description: 复用真实普通动态任务前半程，保持发起人与批准人分离及单活页面。
 */
import { expect, type Browser, type Page, type TestInfo } from '@playwright/test';
import { criterion, facts, login, object, type Row } from './final-f1-browser-helpers';

export async function sendDynamicInput(page: Page, text: string): Promise<void> {
  /** 真Composer投递并核新增用户消息；DTA等待保留SSE，不要求业务提前终态。 */
  const before = (await facts(page)).messages.filter(row => row.role === 'user').map(row => row.id);
  const engine = page.getByRole('button', { name: '选择 DynamicTaskAgent 复杂任务引擎', exact: true });
  if (await engine.getAttribute('aria-pressed') !== 'true') await engine.click();
  await expect(engine).toHaveAttribute('aria-pressed', 'true');
  const input = page.getByPlaceholder('输入消息，按 Enter 发送...');
  await input.fill(text);
  const sent = page.waitForRequest(row => new URL(row.url()).pathname === '/api/chat/stream'
    && row.method() === 'POST');
  await page.getByRole('button', { name: '发送', exact: true }).click();
  const body = object((await sent).postDataJSON());
  expect(body.execution_engine).toBe('dynamic_task');
  await expect(input).toHaveValue('', { timeout: 20_000 });
  await expect.poll(async () => (await facts(page)).messages.filter(row => row.role === 'user'
    && !before.includes(row.id)).length, { timeout: 30_000 }).toBe(1);
}

export async function pendingAttention(page: Page, executionId: string, kind: string): Promise<Row> {
  /** 只读本执行原待办，必须是仍未处理的精确kind。 */
  await expect.poll(async () => (await facts(page)).work_items.filter(row => row.instance_id === executionId
    && row.attention_kind === kind && ['offered', 'claimed'].includes(String(row.status))).length,
  { timeout: 60_000 }).toBe(1);
  return object((await facts(page)).work_items.find(row => row.instance_id === executionId
    && row.attention_kind === kind && ['offered', 'claimed'].includes(String(row.status))));
}

export async function approveDynamic(page: Page, browser: Browser, info: TestInfo, attention: Row,
  expectedText: string[]): Promise<Page> {
  /** 实际第二账号批准原待办，单活页面降低资源占用；只提交原CAS决定。 */
  const originalContext = page.context();
  const execution = object((await facts(page)).instances.find(row => row.id === attention.instance_id));
  const originalUrl = new URL(`/workspace/chat/${String(execution.session_id)}`, page.url()).href;
  const baseURL = new URL(originalUrl).origin;
  await page.close();
  const reviewerContext = await browser.newContext({ baseURL });
  try {
    const reviewer = await reviewerContext.newPage();
    await login(reviewer, 'DTA', true);
    await reviewer.goto('/enterprise/work-items');
    const attentionButton = reviewer.locator(
      `[data-attention-item-id="${String(attention.id)}"]`,
    );
    await expect(attentionButton).toHaveCount(1);
    await attentionButton.click();
    const dialog = reviewer.getByRole('dialog');
    await expect(dialog).toContainText(String(attention.instance_id));
    for (const text of expectedText) await expect(dialog).toContainText(text);
    const response = reviewer.waitForResponse(row => new URL(row.url()).pathname
      === `/api/attention-items/${String(attention.id)}/resolve` && row.request().method() === 'POST');
    await dialog.getByRole('button', { name: '仅批准本次操作', exact: true }).click();
    expect((await response).status()).toBe(200);
    await expect(dialog).toHaveCount(0);
    await criterion(info, `approval-${String(attention.id)}`, { passed: true, attention_id: attention.id,
      execution_id: attention.instance_id, actual_approver_user_id: 'f1_reviewer', actor_separation: true });
  } finally {
    await reviewerContext.close();
  }
  const restored = await originalContext.newPage();
  await restored.goto(originalUrl);
  await expect(restored.getByPlaceholder('输入消息，按 Enter 发送...')).toBeVisible();
  return restored;
}

export async function startWaitingDynamic(page: Page, browser: Browser, info: TestInfo): Promise<{
  page: Page; instance: Row; operation: Row; remote: Row;
}> {
  /** 原引擎生成完整计划，经本人澄清、独立批准后等待真实HTTP accepted。 */
  await login(page, 'DTA');
  const before = await facts(page);
  const oldIds = before.instances.map(row => row.id);
  const oldMessages = before.messages.map(row => row.id);
  await sendDynamicInput(page, 'F1-DTA：商品ALPHA91、数量3、明天09:00配送，使用我记住的客户；先查询政策，澄清并等人工批准，取得原始回执后再登记并答复。');
  await expect.poll(async () => (await facts(page)).instances.find(row => row.kind === 'dynamic_task'
    && !oldIds.includes(row.id))?.status, { timeout: 60_000 }).toBe('waiting');
  const state = await facts(page);
  const instance = object(state.instances.find(row => row.kind === 'dynamic_task' && !oldIds.includes(row.id)));
  expect(instance).toMatchObject({ tenant_id: 'tenant_demo', agent_id: 'agent_f1_dta', initiator_user_id: 'admin' });
  const source = object(state.messages.find(row => row.role === 'user' && !oldMessages.includes(row.id)));
  const time = object(object(source.metadata).time_context);
  expect(time).toMatchObject({ source_input_id: source.id, anchor_at_utc: '2026-10-03T15:59:00Z', timezone: 'Asia/Shanghai' });
  for (const phase of ['Dynamic Planner']) {
    const requests = state.sdk.filter(row => row.phase === phase && row.source_input_id === source.id);
    expect(requests).toHaveLength(1);
    expect(requests[0]?.memory_complete).toEqual([true, true, true]);
    expect(requests[0]?.time_context).toEqual(time);
  }
  for (const phase of ['Router / Dynamic Task', 'Router / General Skill Selector']) {
    const requests = state.sdk.filter(row => row.phase === phase && row.source_input_id === source.id);
    expect(requests).toHaveLength(1);
    expect(requests[0]?.memory_complete).toEqual([false, false, false]);
    expect(requests[0]?.time_context).toEqual(time);
  }
  expect(instance.plan_revision_id).toBeTruthy();
  expect(instance.plan_checksum).toBeTruthy();
  expect(Array.isArray(instance.plan_steps) && instance.plan_steps.length === 5).toBe(true);
  expect(state.provider.submits).toHaveLength(before.provider.submits.length);
  const clarification = await pendingAttention(page, String(instance.id), 'clarification');
  const chatUrl = `/workspace/chat/${String(instance.session_id)}`;
  await page.goto('/enterprise/work-items');
  await page.getByRole('button').filter({ has: page.getByText(String(clarification.title), { exact: true }) }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByPlaceholder('填写任务继续执行所需的准确信息').fill('确认商品ALPHA91、数量3；客户与原明天09:00日期保持，请继续原计划。');
  const response = page.waitForResponse(row => new URL(row.url()).pathname
    === `/api/attention-items/${String(clarification.id)}/resolve` && row.request().method() === 'POST');
  await dialog.getByRole('button', { name: '补充并继续', exact: true }).click();
  expect((await response).status()).toBe(200);
  await expect(dialog).toHaveCount(0);
  await page.goto(chatUrl);
  const approval = await pendingAttention(page, String(instance.id), 'tool_approval');
  const unapproved = await facts(page);
  expect(unapproved.provider.submits).toHaveLength(before.provider.submits.length);
  const prepared = object(unapproved.operations.find(row => row.instance_id === instance.id
    && String(row.operation_name).split('@')[0] === 'final_f1.submit'));
  expect(prepared.status).toBe('prepared');
  expect(prepared.request).toEqual({ product_name: 'ALPHA91', quantity: 3, customer: 'CYGNUS77', due_date: '2026-10-04 09:00' });
  await criterion(info, 'dynamic-before-approval', { passed: true, instance, source_input_id: source.id,
    clarification_id: clarification.id, approval_id: approval.id, operation: prepared, provider_post_count: before.provider.submits.length });
  page = await approveDynamic(page, browser, info, approval, ['ALPHA91', 'CYGNUS77', '2026-10-04 09:00']);
  await expect.poll(async () => (await facts(page)).operations.find(row => row.id === prepared.id)?.status,
    { timeout: 60_000 }).toBe('waiting_external');
  const accepted = await facts(page);
  const operation = object(accepted.operations.find(row => row.id === prepared.id));
  const remote = object(accepted.provider.submits.find(row => row.task_id === operation.external_task_id));
  expect(operation).toMatchObject({ approved_by_user_id: 'f1_reviewer', request_hash: prepared.request_hash,
    request_fingerprint: prepared.request_fingerprint, capability_checksum: prepared.capability_checksum });
  expect(remote).toMatchObject({ post_count: 1, callback_header_received: true, effect_completed: false });
  expect(accepted.provider.submits).toHaveLength(before.provider.submits.length + 1);
  expect(accepted.instances.find(row => row.id === instance.id)?.plan_revision_id).toBe(instance.plan_revision_id);
  await criterion(info, 'dynamic-waiting-external', { passed: true, instance, operation, remote, ledger: accepted });
  return { page, instance, operation, remote };
}

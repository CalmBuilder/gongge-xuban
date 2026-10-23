/**
 * @Time       : 2026/09/28
 * @Author     : zhanglp8181
 * @File       : sd4-external-task.fullstack.e2e.ts
 * @CallChain  : Playwright Chromium → ToolsPage → provider_task API → inbox/worker
 * @Description: 验证受控异步工具双提交、刷新重放、回执恢复和真实 HTTP 派发计数。
 */

import { expect, test, type Page } from '@playwright/test';

const OVERALL_AGENT_ID = 'agent_tenant_demo_overall';
const TOOL_ID = 'tool_sd4_browser_external_task';

async function login(page: Page): Promise<void> {
  /** 建立隔离全栈浏览器登录态，不使用真实业务账号。 */

  await page.goto('/enterprise/dashboard');
  const status = await page.evaluate(async (overallAgentId) => {
    const response = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tenant_id: 'tenant_demo', username: 'admin', password: 'admin' }),
    });
    const body = await response.json();
    if (response.ok) {
      localStorage.setItem('gongge_auth', JSON.stringify(body));
      localStorage.setItem('gongge_onboarding_guide_seen', '1');
      localStorage.setItem('gongge_quick_start_guide_seen', '1');
      localStorage.setItem('gongge_enterprise_agent_scope', overallAgentId);
    }
    return response.status;
  }, OVERALL_AGENT_ID);
  expect(status).toBe(200);
}

async function readFixture<T>(page: Page, path: string): Promise<T> {
  /** 读取受控 provider 夹具事实，避免从页面文案推断派发次数。 */

  return page.evaluate(async (fixturePath) => {
    const response = await fetch(fixturePath);
    if (!response.ok) throw new Error(`fixture ${fixturePath} returned ${response.status}`);
    return response.json();
  }, path) as Promise<T>;
}

test('SD-4 工具测试双提交与刷新重放只产生一次真实派发并可恢复回执', async ({ page }) => {
  await login(page);
  await readFixture(page, '/__e2e/sd4/reset');

  const requestBodies: Array<Record<string, unknown>> = [];
  const apiResponses: import('@playwright/test').Response[] = [];
  page.on('request', (request) => {
    if (
      request.method() === 'POST'
      && new URL(request.url()).pathname === `/api/enterprise/tools/${TOOL_ID}/test`
    ) {
      requestBodies.push(request.postDataJSON() as Record<string, unknown>);
    }
  });
  page.on('response', (response) => {
    if (
      response.request().method() === 'POST'
      && new URL(response.url()).pathname === `/api/enterprise/tools/${TOOL_ID}/test`
    ) {
      apiResponses.push(response);
    }
  });

  await page.goto(`/enterprise/tools/${TOOL_ID}/test?agent_id=${OVERALL_AGENT_ID}`);
  const input = page.locator('textarea:visible').first();
  await expect(input).toBeVisible();
  await input.fill(JSON.stringify({ product_name: 'A1' }, null, 2));
  const invoke = page.getByRole('button', { name: '调用', exact: true });

  // 连续 DOM click 在同一浏览器事件循环内发出两次完全相同的提交，模拟真实双击。
  await invoke.evaluate((element) => {
    (element as HTMLButtonElement).click();
    (element as HTMLButtonElement).click();
  });
  await expect.poll(() => requestBodies.length, { timeout: 10_000 }).toBe(2);
  await expect.poll(() => apiResponses.length, { timeout: 10_000 }).toBe(2);
  expect(apiResponses.map((response) => response.status())).toEqual([200, 200]);
  const apiPayloads = await Promise.all(apiResponses.map((response) => response.json()));
  expect(apiPayloads[0]).toMatchObject({ disposition: 'waiting', success: false });
  expect(requestBodies[0]?.submission_id).toBeTruthy();
  expect(requestBodies[1]?.submission_id).toBe(requestBodies[0]?.submission_id);

  type Snapshot = {
    dispatch_count: number;
    operations: Array<{ status: string; instance_status: string; result: Record<string, unknown> }>;
  };
  await expect.poll(
    async () => (await readFixture<Snapshot>(page, '/__e2e/sd4/snapshot')).dispatch_count,
    { timeout: 10_000 },
  ).toBe(1);
  const waiting = await readFixture<Snapshot>(page, '/__e2e/sd4/snapshot');
  expect(waiting.dispatch_count).toBe(1);
  expect(waiting.operations).toHaveLength(1);
  expect(waiting.operations[0]).toMatchObject({ status: 'waiting_external', instance_status: 'waiting' });

  // 刷新后复用 sessionStorage 中的业务提交身份，不创建第二个 provider task。
  await page.reload();
  await expect(page.getByRole('button', { name: '调用', exact: true })).toBeVisible();
  const refreshRequest = page.waitForRequest((request) => (
    request.method() === 'POST'
    && new URL(request.url()).pathname === `/api/enterprise/tools/${TOOL_ID}/test`
  ));
  const refreshResponse = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && new URL(response.url()).pathname === `/api/enterprise/tools/${TOOL_ID}/test`
  ));
  await page.getByRole('button', { name: '调用', exact: true }).click();
  const refreshBody = (await refreshRequest).postDataJSON() as Record<string, unknown>;
  expect(refreshBody.submission_id).toBe(requestBodies[0]?.submission_id);
  const refreshResponseValue = await refreshResponse;
  expect(refreshResponseValue.status()).toBe(200);
  const afterRefresh = await readFixture<Snapshot>(page, '/__e2e/sd4/snapshot');
  expect(afterRefresh.dispatch_count).toBe(1);

  const completeResponse = await page.evaluate(async () => {
    const response = await fetch('/__e2e/sd4/complete', { method: 'POST' });
    return { status: response.status, body: await response.json() };
  });
  expect(completeResponse.status).toBe(200);
  expect(completeResponse.body.status).toBe(202);

  await expect.poll(
    async () => {
      const operation = (await readFixture<Snapshot>(page, '/__e2e/sd4/snapshot')).operations[0];
      return `${operation?.status}/${operation?.instance_status}`;
    },
    { timeout: 15_000, intervals: [250, 500, 1000] },
  ).toBe('succeeded/succeeded');
  const completed = await readFixture<Snapshot>(page, '/__e2e/sd4/snapshot');
  expect(completed.operations[0]).toMatchObject({ status: 'succeeded', instance_status: 'succeeded' });
  expect(completed.operations[0]?.result).toMatchObject({ data: { product_name: 'A1', price: 404 } });

  const finalResponse = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && new URL(response.url()).pathname === `/api/enterprise/tools/${TOOL_ID}/test`
  ));
  await page.getByRole('button', { name: '调用', exact: true }).click();
  const finalBody = await (await finalResponse).json();
  expect(finalBody).toMatchObject({ success: true, data: { product_name: 'A1', price: 404 } });
  expect((await readFixture<Snapshot>(page, '/__e2e/sd4/snapshot')).dispatch_count).toBe(1);
});

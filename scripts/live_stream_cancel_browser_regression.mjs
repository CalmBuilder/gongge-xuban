/**
 * @Time       : 2026/08/12 23:20
 * @Author     : zhanglp8181
 * @File       : live_stream_cancel_browser_regression.mjs
 * @CallChain  : Chromium 普通用户 → 聊天 SSE → 停止生成 → 刷新恢复
 * @Description: 正向验证取消终态刷新一致，反向验证普通刷新断连不会被误判为用户取消。
 */

import { chromium } from '../frontend-enterprise/node_modules/playwright/index.mjs';

const baseUrl = process.env.BROWSER_TEST_BASE_URL || 'http://127.0.0.1:5137';
const username = process.env.BROWSER_TEST_USERNAME || 'user_demo';
const password = process.env.BROWSER_TEST_PASSWORD || 'demo';
const browserErrors = [];
const failedResponses = [];

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
const page = await context.newPage();
page.on('pageerror', (error) => browserErrors.push(`pageerror: ${error.message}`));
page.on('console', (message) => {
  if (message.type() === 'error') browserErrors.push(`console: ${message.text()}`);
});
page.on('response', (response) => {
  if (response.status() >= 400) failedResponses.push(`${response.status()} ${response.url()}`);
});

/** 登录普通用户并进入任意可用数字员工的新会话。 */
async function openDraftChat() {
  await page.goto(`${baseUrl}/enterprise/dashboard`);
  await page.evaluate(() => {
    localStorage.setItem('gongge_onboarding_guide_seen', '1');
    localStorage.setItem('gongge_quick_start_guide_seen', '1');
  });
  await page.getByRole('button', { name: '进入平台' }).click();
  await page.getByRole('textbox', { name: '账号' }).fill(username);
  await page.getByRole('textbox', { name: '密码', exact: true }).fill(password);
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await page.waitForFunction(() => Boolean(localStorage.getItem('gongge_auth')));
  await page.goto(`${baseUrl}/workspace/gallery`);
  await page.getByRole('textbox', { name: '搜索数字员工' }).fill('法务');
  await page.locator('.gongge-employee-card').filter({ hasText: /^法务/ }).first()
    .getByRole('button', { name: '发起对话' }).click();
  await page.waitForURL(/\/workspace\/chat\/draft\//);
  await page.waitForTimeout(2500);
}

/** 从正式事件 API 读取当前会话事实，浏览器页面文本只作为可见性辅助证据。 */
async function fetchEvents(sessionId) {
  return page.evaluate(async (targetSessionId) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth'));
    const response = await fetch(`/api/chat/sessions/${targetSessionId}/events?tenant_id=tenant_demo`, {
      headers: { Authorization: `Bearer ${auth.token}` },
    });
    if (!response.ok) throw new Error(`${response.status} events`);
    return response.json();
  }, sessionId);
}

/** 从事件事实中取得本轮新建的权威 user_message_id。 */
async function waitForNewTurn(sessionId, knownTurns) {
  for (let attempt = 0; attempt < 120; attempt += 1) {
    const events = await fetchEvents(sessionId);
    const turnId = [...events].reverse().find(
      (event) => event.event === 'user_message_received'
        && !knownTurns.has(event.data?.user_message_id),
    )?.data?.user_message_id || '';
    if (turnId) return turnId;
    await page.waitForTimeout(100);
  }
  throw new Error('未取得新 Turn 的权威 user_message_id');
}

try {
  await openDraftChat();
  const emptySessionMatch = page.url().match(/\/workspace\/chat\/(session_[^/?#]+)/);
  const initialEvents = emptySessionMatch ? await fetchEvents(emptySessionMatch[1]) : [];
  const initialTurns = new Set(
    initialEvents
      .filter((event) => event.event === 'user_message_received')
      .map((event) => event.data?.user_message_id),
  );
  const composer = page.getByPlaceholder('输入消息，按 Enter 发送...');
  await composer.fill('请用中文逐段详细解释企业数字员工的二十个生产级建设要点，每点至少五句话。');
  const send = page.getByRole('button', { name: '发送' });
  if (await send.isDisabled()) throw new Error('发送按钮不应处于禁用态');
  await send.click();
  await page.waitForURL(/\/workspace\/chat\/(session_[^/?#]+)/, { timeout: 120_000 });
  const sessionId = page.url().match(/\/workspace\/chat\/(session_[^/?#]+)/)?.[1];
  if (!sessionId) throw new Error('未取得会话 ID');
  const cancelledTurn = await waitForNewTurn(sessionId, initialTurns);

  const stop = page.locator('button[aria-label="停止生成"]');
  await stop.waitFor({ state: 'visible', timeout: 60_000 });
  await stop.click();
  await page.waitForTimeout(1000);
  const beforeRefresh = await page.locator('body').innerText();
  await page.reload();
  await page.waitForLoadState('networkidle');
  const afterRefresh = await page.locator('body').innerText();
  const cancelledEvents = await fetchEvents(sessionId);
  const cancelledTurnEvents = cancelledEvents.filter(
    (event) => (event.data?.user_message_id || event.data?.turn_id) === cancelledTurn,
  );
  const cancelledTerminalIndexes = cancelledTurnEvents
    .map((event, index) => ({ event, index }))
    .filter(({ event }) => ['stream_end', 'stream_cancelled', 'stream_interrupted', 'error_occurred']
      .includes(event.event));
  const cancelIndex = cancelledTurnEvents.findIndex((event) => event.event === 'stream_cancelled');
  const lateDeltaCount = cancelIndex < 0 ? -1 : cancelledTurnEvents
    .slice(cancelIndex + 1)
    .filter((event) => event.event === 'stream_delta').length;
  const memoryAfterCancel = cancelIndex < 0 ? [] : cancelledTurnEvents
    .slice(cancelIndex + 1)
    .filter(
      (event) => event.event === 'async_job_enqueued'
        && event.data?.feature === 'memory',
    );
  const cancelledAssistantCount = cancelledTurnEvents.filter(
    (event) => event.event === 'assistant_message_created'
      && event.data?.status === 'cancelled',
  ).length;
  await page.screenshot({
    path: 'docs/manuals/assets/openworker-runtime-enhancements/05-stream-cancel-refresh.png',
    fullPage: true,
  });

  // 反向路径：新 Turn 在生成中只刷新页面，不点击停止；断连不应持久化 cancelled 终态。
  const beforeDisconnectEvents = await fetchEvents(sessionId);
  const priorUserTurns = new Set(
    beforeDisconnectEvents
      .filter((event) => event.event === 'user_message_received')
      .map((event) => event.data?.user_message_id),
  );
  const priorCancelledTurns = new Set(
    beforeDisconnectEvents
      .filter((event) => event.event === 'stream_cancelled')
      .map((event) => event.data?.user_message_id || event.data?.turn_id),
  );
  const composerAfterRefresh = page.getByPlaceholder('输入消息，按 Enter 发送...');
  await composerAfterRefresh.fill('请用中文列出十条合同归档检查事项，每条一句。');
  await page.getByRole('button', { name: '发送', exact: true }).last().click();
  const disconnectTurn = await waitForNewTurn(sessionId, priorUserTurns);
  const stopAfterReloadScenario = page.locator('button[aria-label="停止生成"]');
  await stopAfterReloadScenario.waitFor({ state: 'visible', timeout: 60_000 });
  await page.reload();
  await page.waitForLoadState('networkidle');
  let afterDisconnectEvents = [];
  for (let attempt = 0; attempt < 120; attempt += 1) {
    afterDisconnectEvents = await fetchEvents(sessionId);
    const terminal = afterDisconnectEvents.some(
      (event) => ['stream_end', 'stream_cancelled', 'error_occurred'].includes(event.event)
        && (event.data?.user_message_id || event.data?.turn_id) === disconnectTurn,
    );
    if (terminal) break;
    await page.waitForTimeout(500);
  }
  await page.screenshot({
    path: 'docs/manuals/assets/openworker-runtime-enhancements/06-disconnect-is-not-cancel.png',
    fullPage: true,
  });
  const disconnectWasCancelled = afterDisconnectEvents.some(
    (event) => event.event === 'stream_cancelled'
      && (event.data?.user_message_id || event.data?.turn_id) === disconnectTurn
      && !priorCancelledTurns.has(disconnectTurn),
  );
  const disconnectCompleted = afterDisconnectEvents.some(
    (event) => event.event === 'stream_end'
      && (event.data?.user_message_id || event.data?.turn_id) === disconnectTurn,
  );

  console.log(JSON.stringify({
    sessionId,
    stoppedBeforeRefresh: beforeRefresh.includes('已停止生成'),
    stoppedAfterRefresh: afterRefresh.includes('已停止生成'),
    cancelledTurn,
    cancelledTerminalIsUnique: cancelledTerminalIndexes.length === 1
      && cancelledTerminalIndexes[0].event.event === 'stream_cancelled',
    cancelledAssistantIsUnique: cancelledAssistantCount === 1,
    noLateDeltaAfterCancel: lateDeltaCount === 0,
    noMemoryAfterCancel: memoryAfterCancel.length === 0,
    disconnectDidNotAddCancelledTerminal: !disconnectWasCancelled,
    disconnectTurnCompleted: disconnectCompleted,
    browserErrors,
    failedResponses,
  }, null, 2));
  if (!beforeRefresh.includes('已停止生成')) process.exitCode = 2;
  if (!afterRefresh.includes('已停止生成')) process.exitCode = 3;
  if (
    cancelledTerminalIndexes.length !== 1
      || cancelledTerminalIndexes[0].event.event !== 'stream_cancelled'
      || cancelledAssistantCount !== 1
      || lateDeltaCount !== 0
      || memoryAfterCancel.length !== 0
  ) process.exitCode = 4;
  if (disconnectWasCancelled || !disconnectCompleted) process.exitCode = 5;
  if (browserErrors.length || failedResponses.length) process.exitCode = 6;
} catch (error) {
  console.error(JSON.stringify({
    url: page.url(),
    body: (await page.locator('body').innerText().catch(() => '')).slice(-2000),
    failedResponses,
    browserErrors,
  }, null, 2));
  throw error;
} finally {
  await context.close();
  await browser.close();
}

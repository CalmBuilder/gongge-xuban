/**
 * @Time       : 2026/07/22 20:55
 * @Author     : zhanglp8181
 * @File       : live_office_supply_browser_regression.mjs
 * @CallChain  : Chromium → 行政数字员工 → 用品确认门禁 → SOP Trace API
 * @Description: 验证用品对象清单、确认前零调用、确认后单次登记和 SUP 回执闭环。
 */

import { chromium } from '../frontend-enterprise/node_modules/playwright/index.mjs';

const baseUrl = process.env.BROWSER_TEST_BASE_URL || 'http://127.0.0.1:5137';
const username = process.env.BROWSER_TEST_USERNAME;
const password = process.env.BROWSER_TEST_PASSWORD;
const administrativeAgentId = 'agent_30b8f623c6fe445b';
const skillId = 'skill_office_supply_request';
const confirmationPrompt = '办公用品清单已收集，但尚未提交。请核对名称和数量后回复“确认申领”；如不再需要，请回复“取消申领”。';
const browserErrors = [];

if (!username || !password) {
  throw new Error('必须通过 BROWSER_TEST_USERNAME/BROWSER_TEST_PASSWORD 提供演示账号');
}

const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });

page.on('pageerror', (error) => browserErrors.push(`pageerror: ${error.message}`));
page.on('console', (message) => {
  if (message.type() === 'error') browserErrors.push(`console: ${message.text()}`);
});

/** 使用当前浏览器登录令牌读取受保护 API。 */
async function api(path) {
  return page.evaluate(async (targetPath) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth'));
    const response = await fetch(targetPath, {
      headers: { Authorization: `Bearer ${auth.token}` },
    });
    if (!response.ok) throw new Error(`${response.status} ${targetPath}`);
    return response.json();
  }, path);
}

/** 等待用品 Runtime 满足指定状态断言。 */
async function waitForRuntime(sessionId, predicate, description) {
  for (let attempt = 0; attempt < 120; attempt += 1) {
    const trace = await api(`/api/enterprise/traces/${sessionId}?tenant_id=tenant_demo`);
    const runtime = trace?.sop_runtime?.find((candidate) => candidate.skill_id === skillId);
    if (runtime && predicate(runtime)) return runtime;
    await page.waitForTimeout(500);
  }
  throw new Error(`未等到用品申领 Runtime ${description}`);
}

/** 等待助手消息达到指定数量并返回最后一条。 */
async function waitForAssistantMessageCount(sessionId, minimumCount) {
  for (let attempt = 0; attempt < 120; attempt += 1) {
    const messages = await api(
      `/api/chat/sessions/${sessionId}/messages?tenant_id=tenant_demo`,
    );
    const assistantMessages = messages.filter(
      (message) => message.role === 'assistant' && message.content?.trim(),
    );
    if (assistantMessages.length >= minimumCount) return assistantMessages.at(-1);
    await page.waitForTimeout(500);
  }
  throw new Error(`未等到第 ${minimumCount} 条助手消息`);
}

try {
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
  await page.getByRole('textbox', { name: '搜索数字员工' }).fill('行政');
  const agentCard = page.locator('.gongge-employee-card').filter({ hasText: /^行政/ }).first();
  await agentCard.waitFor({ state: 'visible' });
  await agentCard.getByRole('button', { name: '发起对话' }).click();
  await page.waitForURL(new RegExp(`/workspace/chat/draft/${administrativeAgentId}$`));
  await page.waitForTimeout(2500);

  const composer = page.getByPlaceholder('输入消息，按 Enter 发送...');
  await composer.fill('申请两包 A4 纸和三支签字笔');
  await page.getByRole('button', { name: '发送' }).click();
  await page.waitForURL(/\/workspace\/chat\/(session_[^/?#]+)/, { timeout: 60_000 });
  const sessionId = page.url().match(/\/workspace\/chat\/(session_[^/?#]+)/)?.[1];
  if (!sessionId) throw new Error('无法从地址栏取得会话 ID');

  const initialWaiting = await waitForRuntime(
    sessionId,
    (runtime) => runtime.status === 'waiting' && (runtime.operations?.length || 0) === 0,
    '首次确认等待且零调用',
  );
  const firstPrompt = await waitForAssistantMessageCount(sessionId, 1);

  await composer.fill('继续吧');
  await page.getByRole('button', { name: '发送' }).click();
  const secondPrompt = await waitForAssistantMessageCount(sessionId, 2);
  const fuzzyWaiting = await waitForRuntime(
    sessionId,
    (runtime) => runtime.status === 'waiting' && (runtime.operations?.length || 0) === 0,
    '模糊回复后继续等待且零调用',
  );

  await composer.fill('确认申领');
  await page.getByRole('button', { name: '发送' }).click();
  const completed = await waitForRuntime(
    sessionId,
    (runtime) => runtime.status === 'succeeded' && (runtime.operations?.length || 0) === 1,
    '明确确认后单次登记并完成',
  );
  const finalMessage = await waitForAssistantMessageCount(sessionId, 3);
  const operation = completed.operations?.[0] || null;

  await page.screenshot({
    path: `.dev/${sessionId}-office-supply.png`,
    fullPage: true,
  });

  console.log(JSON.stringify({
    sessionId,
    confirmationGate: {
      firstReply: firstPrompt.content,
      secondReply: secondPrompt.content,
      initialStatus: initialWaiting.status,
      fuzzyStatus: fuzzyWaiting.status,
      initialOperationCount: initialWaiting.operations?.length || 0,
      fuzzyOperationCount: fuzzyWaiting.operations?.length || 0,
    },
    finalReply: finalMessage.content,
    runtime: {
      version: completed.skill_version,
      status: completed.status,
      slots: completed.slots,
      identity: completed.identity,
      nodes: completed.node_executions?.map(({ node_id, status }) => ({ node_id, status })),
      operation: operation ? {
        name: operation.operation_name,
        status: operation.status,
        request: operation.request,
        result: operation.result,
      } : null,
    },
    browserErrors,
  }, null, 2));

  const requestItems = operation?.request?.items || [];
  const resultItems = operation?.result?.approved_items || [];
  if (firstPrompt.content !== confirmationPrompt) process.exitCode = 2;
  if (secondPrompt.content !== confirmationPrompt) process.exitCode = 3;
  if (initialWaiting.operations?.length) process.exitCode = 4;
  if (fuzzyWaiting.operations?.length) process.exitCode = 5;
  if (completed.skill_version !== '2.0.0') process.exitCode = 6;
  if (completed.slots?.employee_id !== 'E002') process.exitCode = 7;
  if (completed.slots?.confirmation !== 'confirmed') process.exitCode = 8;
  if (operation?.operation_name !== 'admin.supply_request') process.exitCode = 9;
  if (operation?.request?.employee_id !== 'E002') process.exitCode = 10;
  if (requestItems.length !== 2) process.exitCode = 11;
  if (!requestItems.some((item) => item.name.includes('A4') && item.quantity === 2)) {
    process.exitCode = 12;
  }
  if (!requestItems.some((item) => item.name.includes('签字笔') && item.quantity === 3)) {
    process.exitCode = 13;
  }
  if (operation?.result?.status !== 'approved') process.exitCode = 14;
  if (!String(operation?.result?.request_id || '').startsWith('SUP-')) process.exitCode = 15;
  if (resultItems.length !== 2) process.exitCode = 16;
  if (!finalMessage.content.includes('SUP-')) process.exitCode = 17;
  if (!finalMessage.content.includes('行政服务台')) process.exitCode = 18;
  if (browserErrors.length) process.exitCode = 19;
} finally {
  await browser.close();
}

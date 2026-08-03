/**
 * @Time       : 2026/07/22 20:35
 * @Author     : zhanglp8181
 * @File       : live_leave_balance_browser_regression.mjs
 * @CallChain  : Chromium → 人事数字员工会话 → SOP Trace API → 截图/断言
 * @Description: 在真实单端口服务上验证假期余额确定性 SOP 的身份、类型映射和工具闭环。
 */

import { chromium } from '../frontend-enterprise/node_modules/playwright/index.mjs';

const baseUrl = process.env.BROWSER_TEST_BASE_URL || 'http://127.0.0.1:5137';
const username = process.env.BROWSER_TEST_USERNAME;
const password = process.env.BROWSER_TEST_PASSWORD;
const humanResourcesAgentId = 'agent_9d3d1fdf171049ed';
const browserErrors = [];

if (!username || !password) {
  throw new Error('必须通过 BROWSER_TEST_USERNAME/BROWSER_TEST_PASSWORD 提供演示账号');
}

const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 1050 } });

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

/** 等待指定会话的 SOP Runtime 进入终态。 */
async function waitForTerminalRuntime(sessionId) {
  for (let attempt = 0; attempt < 90; attempt += 1) {
    const trace = await api(`/api/enterprise/traces/${sessionId}?tenant_id=tenant_demo`);
    const runtime = trace?.sop_runtime?.find(
      (candidate) => candidate.skill_id === 'skill_leave_balance_query',
    );
    if (runtime && ['succeeded', 'failed', 'cancelled', 'timed_out'].includes(runtime.status)) {
      return runtime;
    }
    await page.waitForTimeout(1000);
  }
  throw new Error('未等到假期余额 SOP Runtime 终态');
}

/** 等待最终助手消息落库，避免仅检查执行中占位文案。 */
async function waitForAssistantMessage(sessionId) {
  for (let attempt = 0; attempt < 90; attempt += 1) {
    const messages = await api(
      `/api/chat/sessions/${sessionId}/messages?tenant_id=tenant_demo`,
    );
    const assistantMessage = [...messages]
      .reverse()
      .find((message) => message.role === 'assistant' && message.content?.trim());
    if (assistantMessage) return assistantMessage;
    await page.waitForTimeout(500);
  }
  throw new Error('未等到假期余额最终助手消息');
}

/** 等待同一会话中的越权查询被 Runtime 在工具调用前拒绝。 */
async function waitForDeniedRuntime(sessionId) {
  for (let attempt = 0; attempt < 90; attempt += 1) {
    const trace = await api(`/api/enterprise/traces/${sessionId}?tenant_id=tenant_demo`);
    const runtimes = trace?.sop_runtime || [];
    const deniedRuntime = runtimes.find(
      (candidate) => candidate.identity?.error_code === 'SUBJECT_OVERRIDE_FORBIDDEN',
    );
    if (runtimes.length >= 2 && deniedRuntime?.status === 'failed') return deniedRuntime;
    await page.waitForTimeout(500);
  }
  throw new Error('未等到越权假期查询被 Runtime 拒绝');
}

/** 等待助手消息达到指定数量并返回最后一条。 */
async function waitForAssistantMessageCount(sessionId, minimumCount) {
  for (let attempt = 0; attempt < 90; attempt += 1) {
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
  await page.getByRole('textbox', { name: '搜索数字员工' }).fill('人事');
  const agentCard = page.locator('.gongge-employee-card').filter({ hasText: /^人事/ }).first();
  await agentCard.waitFor({ state: 'visible' });
  await agentCard.getByRole('button', { name: '发起对话' }).click();
  await page.waitForURL(new RegExp(`/workspace/chat/draft/${humanResourcesAgentId}$`));
  await page.waitForTimeout(2500);

  const composer = page.getByPlaceholder('输入消息，按 Enter 发送...');
  await composer.waitFor({ state: 'visible' });
  await composer.fill('查询我还剩多少年假');
  await page.waitForFunction(() => {
    const button = document.querySelector('button[aria-label="发送"]');
    return button && !button.hasAttribute('disabled');
  });
  await page.getByRole('button', { name: '发送' }).click();
  await page.waitForURL(/\/workspace\/chat\/(session_[^/?#]+)/, { timeout: 60_000 });

  const sessionId = page.url().match(/\/workspace\/chat\/(session_[^/?#]+)/)?.[1];
  if (!sessionId) throw new Error('无法从地址栏取得会话 ID');
  const runtime = await waitForTerminalRuntime(sessionId);
  const assistantMessage = await waitForAssistantMessage(sessionId);
  const operation = runtime.operations?.[0] || null;

  await composer.fill('查询工号 E001 的年假余额');
  await page.getByRole('button', { name: '发送' }).click();
  const deniedRuntime = await waitForDeniedRuntime(sessionId);
  const deniedMessage = await waitForAssistantMessageCount(sessionId, 2);

  await page.screenshot({
    path: `.dev/${sessionId}-leave-balance.png`,
    fullPage: true,
  });

  console.log(JSON.stringify({
    sessionId,
    reply: assistantMessage.content,
    runtime: {
      skillId: runtime.skill_id,
      version: runtime.skill_version,
      status: runtime.status,
      slots: runtime.slots,
      identity: runtime.identity,
      nodes: runtime.node_executions?.map(({ node_id, status }) => ({ node_id, status })),
      operation: operation ? {
        name: operation.operation_name,
        status: operation.status,
        request: operation.request,
        result: operation.result,
      } : null,
    },
    authorizationBoundary: {
      reply: deniedMessage.content,
      status: deniedRuntime.status,
      slots: deniedRuntime.slots,
      identity: deniedRuntime.identity,
      operationCount: deniedRuntime.operations?.length || 0,
    },
    browserErrors,
  }, null, 2));

  if (runtime.status !== 'succeeded') process.exitCode = 2;
  if (runtime.skill_version !== '2.1.0') process.exitCode = 3;
  if (runtime.slots?.employee_id !== 'E002') process.exitCode = 4;
  if (runtime.slots?.leave_type !== 'annual') process.exitCode = 5;
  if (runtime.identity?.actor_employee_id !== 'E002') process.exitCode = 6;
  if (runtime.identity?.subject_employee_id !== 'E002') process.exitCode = 7;
  if (runtime.identity?.delegated !== false) process.exitCode = 8;
  if (operation?.operation_name !== 'hr.balance_query') process.exitCode = 9;
  if (operation?.request?.employee_id !== 'E002') process.exitCode = 10;
  if (operation?.result?.leave_balance?.annual !== 5) process.exitCode = 11;
  if (!assistantMessage.content.includes('年假') || !assistantMessage.content.includes('5')) {
    process.exitCode = 12;
  }
  if (assistantMessage.content.includes('有效期')) process.exitCode = 13;
  if (browserErrors.length) process.exitCode = 14;
  if (deniedMessage.content !== '当前员工未被授予该业务角色，只能办理本人业务。') {
    process.exitCode = 15;
  }
  if (deniedRuntime.status !== 'failed') process.exitCode = 16;
  if (deniedRuntime.slots?.employee_id !== 'E002') process.exitCode = 17;
  if (deniedRuntime.operations?.length) process.exitCode = 18;
} finally {
  await browser.close();
}

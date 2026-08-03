/**
 * @Time       : 2026/07/22 17:12
 * @Author     : zhanglp8181
 * @File       : live_clause_modification_browser_regression.mjs
 * @CallChain  : Chromium 普通员工 → 法务数字员工 → 合同资料检索 → 条款建议终态
 * @Description: 验证受限合同类型、相关演示资料、结构化检索回执和法务复核边界。
 */

import { chromium } from '../frontend-enterprise/node_modules/playwright/index.mjs';

const baseUrl = process.env.BROWSER_TEST_BASE_URL || 'http://127.0.0.1:5137';
const username = process.env.BROWSER_TEST_USERNAME || 'user_demo';
const password = process.env.BROWSER_TEST_PASSWORD || 'demo';
const legalAgentId = 'agent_7d062081c03b4e16';
const skillId = 'skill_clause_modification';
const browserErrors = [];
const expectedAuthorizationDenials = [];

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 1100 } });
const page = await context.newPage();

page.on('pageerror', (error) => browserErrors.push(`pageerror: ${error.message}`));
page.on('console', (message) => {
  if (message.type() !== 'error') return;
  const text = message.text();
  if (text.includes('status of 403 (Forbidden)')) {
    expectedAuthorizationDenials.push(text);
    return;
  }
  browserErrors.push(`console: ${text}`);
});

/** 登录普通员工账号并等待浏览器保存认证令牌。 */
async function login() {
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
}

/** 使用当前普通员工令牌读取会话 Trace。 */
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

/** 等待条款修改 Runtime 完成并返回本次冻结执行投影。 */
async function waitForCompletedRuntime(sessionId) {
  for (let attempt = 0; attempt < 180; attempt += 1) {
    const trace = await api(
      `/api/enterprise/traces/${sessionId}?tenant_id=tenant_demo`,
    );
    const runtime = trace?.sop_runtime?.find((candidate) => candidate.skill_id === skillId);
    if (
      runtime?.status === 'succeeded'
      && runtime.current_node_id === 'node_clause_suggestion_with_reference'
      && (runtime.operations?.length || 0) === 1
    ) return runtime;
    await page.waitForTimeout(500);
  }
  throw new Error('未等到条款修改建议有依据终态');
}

/** 等待最终助手文本完成渲染，避免把 Runtime 终态误当作对话输出已完成。 */
async function waitForFinalSuggestion() {
  let latestPageText = '';
  for (let attempt = 0; attempt < 180; attempt += 1) {
    latestPageText = await page.locator('body').innerText();
    if (
      latestPageText.includes('建议条款')
      && latestPageText.includes('正式签署前')
    ) return latestPageText;
    await page.waitForTimeout(500);
  }
  return latestPageText;
}

try {
  await login();
  await page.goto(`${baseUrl}/workspace/gallery`);
  await page.getByRole('textbox', { name: '搜索数字员工' }).fill('法务');
  const agentCard = page.locator('.gongge-employee-card').filter({ hasText: /^法务/ }).first();
  await agentCard.getByRole('button', { name: '发起对话' }).click();
  await page.waitForURL(new RegExp(`/workspace/chat/draft/${legalAgentId}$`));
  await page.waitForTimeout(2500);

  const composer = page.getByPlaceholder('输入消息，按 Enter 发送...');
  await composer.fill(
    '这是软件采购合同。原条款：供应商因任何违约造成的全部损失承担无限责任。'
      + '请修改为累计赔偿责任有明确上限，并保留故意或重大过失、保密、知识产权和数据安全等例外。',
  );
  await page.getByRole('button', { name: '发送' }).click();
  await page.waitForURL(/\/workspace\/chat\/(session_[^/?#]+)/, { timeout: 60_000 });
  const sessionId = page.url().match(/\/workspace\/chat\/(session_[^/?#]+)/)?.[1];
  if (!sessionId) throw new Error('无法取得条款修改建议会话 ID');

  const runtime = await waitForCompletedRuntime(sessionId);
  const pageText = await waitForFinalSuggestion();
  const operation = runtime.operations.find(
    (candidate) => candidate.operation_name === 'contract.archive_query',
  );
  await page.screenshot({
    path: `.dev/${sessionId}-clause-modification.png`,
    fullPage: true,
  });

  console.log(JSON.stringify({
    credentials: `${username} / ${password}`,
    sessionId,
    skillVersion: runtime.skill_version,
    finalNode: runtime.current_node_id,
    slots: runtime.slots,
    operation,
    containsSuggestedClause: pageText.includes('建议条款'),
    containsFirstCitation: pageText.includes('DEMO-CLAUSE-LIABILITY-001'),
    containsSecondCitation: pageText.includes('DEMO-REVIEW-LIABILITY-002'),
    containsLegalReviewBoundary: pageText.includes('正式签署前'),
    expectedAuthorizationDenialCount: expectedAuthorizationDenials.length,
    browserErrors,
  }, null, 2));

  if (runtime.skill_version !== '2.0.0') process.exitCode = 2;
  if (runtime.slots?.contract_type !== 'software_procurement') process.exitCode = 3;
  if (!runtime.slots?.clause_content?.includes('无限责任')) process.exitCode = 4;
  if (!runtime.slots?.modification_request?.includes('上限')) process.exitCode = 5;
  if (operation?.request?.query !== runtime.slots?.clause_content) process.exitCode = 6;
  if (operation?.result?.total !== 2) process.exitCode = 7;
  if (!pageText.includes('建议条款')) process.exitCode = 10;
  if (!pageText.includes('DEMO-CLAUSE-LIABILITY-001')) process.exitCode = 11;
  if (!pageText.includes('DEMO-REVIEW-LIABILITY-002')) process.exitCode = 12;
  if (!pageText.includes('正式签署前')) process.exitCode = 13;
  if (browserErrors.length) process.exitCode = 14;
} finally {
  await context.close();
  await browser.close();
}

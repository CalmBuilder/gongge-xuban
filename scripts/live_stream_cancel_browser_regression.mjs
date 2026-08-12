/**
 * @Time       : 2026/08/12 23:20
 * @Author     : zhanglp8181
 * @File       : live_stream_cancel_browser_regression.mjs
 * @CallChain  : Chromium 普通用户 → 聊天 SSE → 停止生成 → 刷新恢复
 * @Description: 验证真实浏览器在首 token 前取消长模型请求，且取消终态刷新后仍一致。
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

try {
  await openDraftChat();
  const composer = page.getByPlaceholder('输入消息，按 Enter 发送...');
  await composer.fill('请用中文逐段详细解释企业数字员工的二十个生产级建设要点，每点至少五句话。');
  const send = page.getByRole('button', { name: '发送' });
  if (await send.isDisabled()) throw new Error('发送按钮不应处于禁用态');
  await send.click();
  await page.waitForURL(/\/workspace\/chat\/(session_[^/?#]+)/, { timeout: 120_000 });
  const sessionId = page.url().match(/\/workspace\/chat\/(session_[^/?#]+)/)?.[1];
  if (!sessionId) throw new Error('未取得会话 ID');

  const stop = page.locator('button[aria-label="停止生成"]');
  await stop.waitFor({ state: 'visible', timeout: 60_000 });
  await stop.click();
  await page.waitForTimeout(1000);
  const beforeRefresh = await page.locator('body').innerText();
  await page.reload();
  await page.waitForLoadState('networkidle');
  const afterRefresh = await page.locator('body').innerText();
  await page.screenshot({ path: `.dev/${sessionId}-stream-cancel.png`, fullPage: true });

  console.log(JSON.stringify({
    sessionId,
    stoppedBeforeRefresh: beforeRefresh.includes('已停止生成'),
    stoppedAfterRefresh: afterRefresh.includes('已停止生成'),
    browserErrors,
    failedResponses,
  }, null, 2));
  if (!beforeRefresh.includes('已停止生成')) process.exitCode = 2;
  if (!afterRefresh.includes('已停止生成')) process.exitCode = 3;
  if (browserErrors.length) process.exitCode = 4;
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

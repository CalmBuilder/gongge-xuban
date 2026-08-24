/**
 * @Time       : 2026/08/13 19:40
 * @Author     : zhanglp8181
 * @File       : live_attachment_f0_browser_baseline.mjs
 * @CallChain  : Chromium → 聊天草稿 → 附件选择/上传 → Composer 基线
 * @Description: 记录附件 A++ 实施前真实页面可选择和显示文件的基线，不把当前 ready 文案当作可分析证明。
 */

import { chromium } from '../frontend-enterprise/node_modules/playwright/index.mjs';
import fs from 'node:fs/promises';

const baseUrl = process.env.BROWSER_TEST_BASE_URL || 'http://127.0.0.1:5137';
const username = process.env.BROWSER_TEST_USERNAME || 'user_demo';
const password = process.env.BROWSER_TEST_PASSWORD || 'demo';
const agentId = process.env.ATTACHMENT_BASELINE_AGENT_ID || 'agent_7d062081c03b4e16';
const evidenceDir = 'docs/manuals/assets/attachment-analysis/f0-baseline';
const fixtureRoot = 'backend/tests/fixtures/attachments/positive';
const fixtures = [
  `${fixtureRoot}/sales_targets.csv`,
  `${fixtureRoot}/contract_text.pdf`,
  `${fixtureRoot}/product_screen.png`,
];
const browserErrors = [];
const failedResponses = [];

await fs.mkdir(evidenceDir, { recursive: true });
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

/** 登录现有普通演示用户，等待浏览器保存认证令牌。 */
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

try {
  await login();
  await page.goto(`${baseUrl}/workspace/chat/draft/${agentId}`);
  await page.getByPlaceholder('输入消息，按 Enter 发送...').waitFor({ state: 'visible' });
  await page.locator('input[type="file"]').setInputFiles(fixtures);

  for (const path of fixtures) {
    const filename = path.split('/').at(-1);
    await page.getByText(filename, { exact: true }).waitFor({ state: 'visible', timeout: 30_000 });
  }
  await page.getByText('解析中', { exact: true }).first().waitFor({ state: 'hidden', timeout: 30_000 });

  const visibleText = await page.locator('body').innerText();
  const observedLabels = ['sales_targets.csv', 'contract_text.pdf', 'product_screen.png']
    .filter((name) => visibleText.includes(name));
  if (observedLabels.length !== fixtures.length) {
    throw new Error(`附件基线文件名缺失：${observedLabels.join(',')}`);
  }

  await page.screenshot({ path: `${evidenceDir}/01-current-upload-baseline.png`, fullPage: true });
  const removeButtons = page.getByRole('button', { name: '移除附件' });
  while (await removeButtons.count()) await removeButtons.first().click();
  if (await page.getByText('sales_targets.csv', { exact: true }).count()) {
    throw new Error('移除附件后 Composer 仍显示旧文件名');
  }
  await page.screenshot({ path: `${evidenceDir}/02-current-remove-baseline.png`, fullPage: true });
} finally {
  await browser.close();
}

if (browserErrors.length || failedResponses.length) {
  throw new Error([
    ...browserErrors,
    ...failedResponses.map((item) => `failed response: ${item}`),
  ].join('\n'));
}

console.log(JSON.stringify({
  status: 'passed',
  browser: 'chromium',
  fixtures: fixtures.map((item) => item.split('/').at(-1)),
  claim: 'current-selection-and-display-baseline-only',
}, null, 2));

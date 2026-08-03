/**
 * @Time       : 2026/08/01 22:47
 * @Author     : zhanglp8181
 * @File       : live_management_pagination_browser_regression.mjs
 * @CallChain  : ./app.sh:5137 + MySQL → Chromium 管理员 → 员工/角色分页
 * @Description: 真实点击数字员工管理和组织角色的下一页，核对第二页 API 与页面状态。
 */

import assert from 'node:assert/strict';

import { chromium } from '../frontend-enterprise/node_modules/playwright/index.mjs';

const baseUrl = process.env.BROWSER_TEST_BASE_URL || 'http://127.0.0.1:5137';
const username = process.env.BROWSER_TEST_ADMIN_USERNAME || 'admin';
const password = process.env.BROWSER_TEST_ADMIN_PASSWORD || 'admin';
const browserErrors = [];
const badResponses = [];
const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1920, height: 1080 } });
const page = await context.newPage();

page.on('pageerror', (error) => browserErrors.push(`pageerror: ${error.message}`));
page.on('console', (message) => {
  if (message.type() === 'error') browserErrors.push(`console: ${message.text()}`);
});
page.on('response', (response) => {
  const url = new URL(response.url());
  if (url.pathname.startsWith('/api/') && response.status() >= 500) {
    badResponses.push(`${response.status()} ${url.pathname}${url.search}`);
  }
});

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

async function clickNextAndRead(pathname, paginationLabel) {
  const paginator = page.getByRole('navigation', { name: paginationLabel });
  await paginator.waitFor();
  const next = paginator.getByRole('button', { name: '下一页' });
  assert.equal(await next.isDisabled(), false, `${paginationLabel} 下一页不应禁用`);
  const [response] = await Promise.all([
    page.waitForResponse((candidate) => {
      const url = new URL(candidate.url());
      return url.pathname === pathname && url.searchParams.get('page') === '2';
    }),
    next.click(),
  ]);
  assert.equal(response.status(), 200, `${pathname} 第二页应返回 200`);
  const body = await response.json();
  assert.equal(body.page, 2);
  assert.ok(body.total > body.page_size, `${pathname} 真实数据必须足以翻页`);
  assert.ok(body.items.length > 0, `${pathname} 第二页必须有数据`);
  await assertCurrentPage(paginator, '02');
  return { total: body.total, pageSize: body.page_size, secondPageItems: body.items.length };
}

async function assertCurrentPage(paginator, label) {
  const current = paginator.locator('[aria-current="page"]');
  await current.waitFor();
  assert.equal((await current.textContent())?.trim(), label);
}

try {
  await login();

  await page.goto(`${baseUrl}/enterprise/agents?view=all`);
  const agents = await clickNextAndRead(
    '/api/enterprise/agents/management-page',
    '数字员工管理分页',
  );

  await page.goto(`${baseUrl}/enterprise/organization-roles?section=roles`);
  const roles = await clickNextAndRead(
    '/api/organization/business-roles/page',
    '角色目录分页',
  );

  assert.deepEqual(badResponses, []);
  assert.deepEqual(browserErrors, []);
  console.log(JSON.stringify({ agents, roles, badResponses, browserErrors }, null, 2));
} finally {
  await browser.close();
}

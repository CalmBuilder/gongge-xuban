/**
 * @Time       : 2026/07/28 20:20
 * @Author     : zhanglp8181
 * @File       : live_m3c_browser_regression.mjs
 * @CallChain  : 5137 实际 MySQL → Chromium 管理员 → SOP 版本/任务箱只读验收
 * @Description: 验证 M3-C v2.1 发布链、节点级组织范围和持久入口无 404/5xx。
 */

import assert from 'node:assert/strict';

import { chromium } from '../frontend-enterprise/node_modules/playwright/index.mjs';

const baseUrl = process.env.BROWSER_TEST_BASE_URL || 'http://127.0.0.1:5137';
const tenantId = process.env.BROWSER_TEST_TENANT_ID || 'tenant_demo';
const adminUsername = process.env.BROWSER_TEST_ADMIN_USERNAME || 'admin';
const adminPassword = process.env.BROWSER_TEST_ADMIN_PASSWORD || 'admin';
const failures = [];
const browserErrors = [];
const requests = [];

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1600, height: 1050 } });
const page = await context.newPage();

page.on('pageerror', (error) => browserErrors.push(`pageerror: ${error.message}`));
page.on('console', (message) => {
  if (message.type() === 'error') browserErrors.push(`console: ${message.text()}`);
});
page.on('response', (response) => {
  const url = new URL(response.url());
  if (
    url.pathname.startsWith('/api/enterprise/skills')
    || url.pathname.startsWith('/api/work-items')
    || url.pathname.startsWith('/api/auth')
  ) {
    requests.push(`${response.status()} ${response.request().method()} ${url.pathname}`);
    if (response.status() === 404 || response.status() >= 500) {
      failures.push(`${response.status()} ${url.pathname}`);
    }
  }
});

async function login() {
  await page.goto(`${baseUrl}/enterprise/dashboard`);
  await page.evaluate(() => {
    localStorage.setItem('gongge_onboarding_guide_seen', '1');
    localStorage.setItem('gongge_quick_start_guide_seen', '1');
  });
  await page.getByRole('button', { name: '进入平台' }).click();
  await page.getByRole('textbox', { name: '账号' }).fill(adminUsername);
  await page.getByRole('textbox', { name: '密码', exact: true }).fill(adminPassword);
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await page.waitForFunction(() => Boolean(localStorage.getItem('gongge_auth')));
}

try {
  await login();
  await page.goto(`${baseUrl}/enterprise/skills`);
  const skillTable = page.getByRole('table', { name: 'SOP 列表' });
  await skillTable.waitFor({ state: 'visible' });
  await page.getByPlaceholder('搜索 SOP 名称、ID、业务域').fill('超标报销特批');
  await skillTable.getByRole('row').filter({ hasText: '超标报销特批' }).waitFor({
    state: 'visible',
  });
  assert.equal(await page.getByText('Not Found', { exact: true }).count(), 0);

  const versionAudit = await page.evaluate(async (currentTenantId) => {
    const raw = localStorage.getItem('gongge_auth');
    if (!raw) throw new Error('管理员会话不存在');
    const token = JSON.parse(raw).token;
    const response = await fetch(
      `/api/enterprise/skills/expense_over_limit_approval/versions`
      + `?tenant_id=${currentTenantId}`,
      { headers: { Authorization: `Bearer ${token}` } },
    );
    const versions = await response.json();
    return { status: response.status, versions };
  }, tenantId);
  assert.equal(versionAudit.status, 200);
  assert.deepEqual(
    versionAudit.versions.map((version) => version.version).sort(),
    ['1.0.0', '2.0.0', '2.1.0', '2.1.1'],
  );
  const v2 = versionAudit.versions.find((version) => version.version === '2.0.0');
  const v21 = versionAudit.versions.find((version) => version.version === '2.1.0');
  assert.equal(v2.status, 'published');
  assert.equal(v21.status, 'published');
  assert.equal(v21.derived_from_version_id, v2.id);
  assert.ok(v2.content_checksum);
  assert.ok(v21.content_checksum);
  const v2Nodes = Object.fromEntries(v2.content.nodes.map((node) => [node.node_id, node]));
  const v21Nodes = Object.fromEntries(v21.content.nodes.map((node) => [node.node_id, node]));
  assert.equal(
    v2Nodes.department_special_approval.metadata.participant_policy
      .participant_scope_resolver,
    undefined,
  );
  assert.equal(
    v21Nodes.department_special_approval.metadata.participant_policy
      .participant_scope_resolver,
    'initiator_primary_org_subtree',
  );
  assert.equal(
    v21Nodes.finance_special_approval.metadata.participant_policy
      .participant_scope_resolver,
    undefined,
  );

  await page.goto(`${baseUrl}/enterprise/work-items`);
  await page.getByRole('table', { name: '流程任务列表' }).waitFor({ state: 'visible' });
  assert.equal(await page.getByText('Not Found', { exact: true }).count(), 0);
  assert.deepEqual(failures, []);
  assert.deepEqual(browserErrors, []);
  assert.ok(requests.length >= 4);
  await page.screenshot({
    path: '.dev/m3c-live-version-regression.png',
    fullPage: true,
  });
  console.log(JSON.stringify({
    status: 'passed',
    database: 'mysql',
    versions: versionAudit.versions.map((version) => version.version).sort(),
    departmentScope: 'initiator_primary_org_subtree',
    financeScope: 'tenant',
    requestCount: requests.length,
    failures: failures.length,
    browserErrors: browserErrors.length,
  }, null, 2));
} catch (error) {
  await page.screenshot({
    path: '.dev/m3c-live-version-regression-failure.png',
    fullPage: true,
  });
  throw error;
} finally {
  await browser.close();
}

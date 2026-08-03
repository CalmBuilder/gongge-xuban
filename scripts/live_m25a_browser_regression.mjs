/**
 * @Time       : 2026/07/28 17:55
 * @Author     : zhanglp8181
 * @File       : live_m25a_browser_regression.mjs
 * @CallChain  : 5137 实际部署 → Chromium 双账号 → 统一码表/组织负责人/权限边界
 * @Description: 只读验证 M2.5-A 实际 MySQL、前后端版本一致性和真实页面契约。
 */

import assert from 'node:assert/strict';

import { chromium } from '../frontend-enterprise/node_modules/playwright/index.mjs';

const baseUrl = process.env.BROWSER_TEST_BASE_URL || 'http://127.0.0.1:5137';
const tenantId = process.env.BROWSER_TEST_TENANT_ID || 'tenant_demo';
const adminUsername = process.env.BROWSER_TEST_ADMIN_USERNAME || 'admin';
const adminPassword = process.env.BROWSER_TEST_ADMIN_PASSWORD || 'admin';
const memberUsername = process.env.BROWSER_TEST_USERNAME || 'user_demo';
const memberPassword = process.env.BROWSER_TEST_PASSWORD || 'demo';
const browserErrors = [];
const adminFailures = [];
const expectedForbidden = [];

const browser = await chromium.launch({ headless: true });
const adminContext = await browser.newContext({ viewport: { width: 1600, height: 1050 } });
const memberContext = await browser.newContext({ viewport: { width: 1440, height: 960 } });
const adminPage = await adminContext.newPage();
const memberPage = await memberContext.newPage();

/** 记录未预期浏览器错误，权限边界的 403 单独计数。 */
function observe(page, actor) {
  page.on('pageerror', (error) => browserErrors.push(`${actor} pageerror: ${error.message}`));
  page.on('console', (message) => {
    if (message.type() !== 'error') return;
    if (message.text().includes('403 (Forbidden)')) {
      expectedForbidden.push(`${actor}: ${message.text()}`);
      return;
    }
    browserErrors.push(`${actor} console: ${message.text()}`);
  });
}

observe(adminPage, 'admin');
observe(memberPage, 'member');
adminPage.on('response', (response) => {
  const path = new URL(response.url()).pathname;
  if (
    response.status() >= 400
    && (path.startsWith('/api/organization/') || path.startsWith('/api/reference-data/'))
  ) {
    adminFailures.push(`${response.status()} ${path}`);
  }
});

/** 从真实登录页建立会话。 */
async function login(page, username, password) {
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
  await login(adminPage, adminUsername, adminPassword);
  await adminPage.goto(`${baseUrl}/enterprise/organization`);
  await adminPage.getByRole('tree', { name: '企业组织树' }).waitFor({ state: 'visible' });
  const root = adminPage.getByRole('treeitem').first();
  await root.click();
  await adminPage.getByText('组织负责人（当前与历史）', { exact: true }).waitFor({
    state: 'visible',
  });
  await adminPage.getByText('责任关系，不自动授予角色或权限', { exact: true }).waitFor({
    state: 'visible',
  });
  assert.equal(await adminPage.getByText('Not Found', { exact: true }).count(), 0);

  await adminPage.goto(`${baseUrl}/enterprise/reference-data`);
  await adminPage.getByRole('navigation', { name: '业务码表' }).waitFor({ state: 'visible' });
  await adminPage.getByRole('button', { name: /成员类别/ }).waitFor({ state: 'visible' });
  for (const name of ['成员类别', '组织类型', '岗位类型', '负责人类型']) {
    assert.ok(
      await adminPage.getByRole('button', { name: new RegExp(name) }).count() >= 1,
      `统一码表缺少 ${name}`,
    );
  }
  await adminPage.getByRole('button', { name: /负责人类型/ }).click();
  for (const name of ['主要负责人', '副负责人', '代理负责人', '项目负责人']) {
    await adminPage.getByText(name, { exact: true }).waitFor({ state: 'visible' });
  }
  assert.equal(await adminPage.getByText('Not Found', { exact: true }).count(), 0);
  assert.deepEqual(adminFailures, [], '管理员负责人或码表 API 存在失败');

  await login(memberPage, memberUsername, memberPassword);
  const memberBoundary = await memberPage.evaluate(async (currentTenantId) => {
    const raw = localStorage.getItem('gongge_auth');
    if (!raw) throw new Error('普通成员会话不存在');
    const session = JSON.parse(raw);
    const headers = { Authorization: `Bearer ${session.token}` };
    const units = await fetch(`/api/organization/units?tenant_id=${currentTenantId}`, {
      headers,
    });
    const orgUnitId = session.user.department_id;
    const [currentLeaders, leaderHistory, codeSets] = await Promise.all([
      fetch(
        `/api/organization/leader-assignments?tenant_id=${currentTenantId}`
        + `&org_unit_id=${orgUnitId}`,
        { headers },
      ),
      fetch(
        `/api/organization/leader-assignments?tenant_id=${currentTenantId}`
        + `&org_unit_id=${orgUnitId}&include_history=true`,
        { headers },
      ),
      fetch(`/api/reference-data/code-sets?tenant_id=${currentTenantId}`, { headers }),
    ]);
    return {
      units: units.status,
      currentLeaders: currentLeaders.status,
      leaderHistory: leaderHistory.status,
      codeSets: codeSets.status,
      hasCurrentOrganization: Boolean(orgUnitId),
    };
  }, tenantId);
  assert.equal(memberBoundary.units, 403);
  assert.equal(memberBoundary.leaderHistory, 403);
  assert.equal(memberBoundary.codeSets, 403);
  assert.equal(
    memberBoundary.currentLeaders,
    memberBoundary.hasCurrentOrganization ? 200 : 403,
  );

  await adminPage.goto(`${baseUrl}/enterprise/organization`);
  await adminPage.getByText('组织负责人（当前与历史）', { exact: true }).waitFor({
    state: 'visible',
  });
  await adminPage.screenshot({
    path: '.dev/m25a-live-organization-regression.png',
    fullPage: true,
  });
  assert.deepEqual(browserErrors, [], '浏览器控制台或页面存在未预期错误');
  console.log(JSON.stringify({
    status: 'passed',
    baseUrl,
    database: 'mysql',
    adminFailures: adminFailures.length,
    memberBoundary,
    browserErrors: browserErrors.length,
  }, null, 2));
} catch (error) {
  await adminPage.screenshot({
    path: '.dev/m25a-live-organization-regression-failure.png',
    fullPage: true,
  });
  throw error;
} finally {
  await browser.close();
}

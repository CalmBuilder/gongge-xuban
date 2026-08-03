/**
 * @Time       : 2026/07/28 18:05
 * @Author     : zhanglp8181
 * @File       : live_m3a_browser_regression.mjs
 * @CallChain  : 5137 实际 MySQL 部署 → Chromium 双账号 → 治理授权页面与服务端边界
 * @Description: 只读验证 M3-A 有效权限解释、权限导航、直接 URL 和新增路由版本一致性。
 */

import assert from 'node:assert/strict';

import { chromium } from '../frontend-enterprise/node_modules/playwright/index.mjs';

const baseUrl = process.env.BROWSER_TEST_BASE_URL || 'http://127.0.0.1:5137';
const tenantId = process.env.BROWSER_TEST_TENANT_ID || 'tenant_demo';
const adminUsername = process.env.BROWSER_TEST_ADMIN_USERNAME || 'admin';
const adminPassword = process.env.BROWSER_TEST_ADMIN_PASSWORD || 'admin';
const memberUsername = process.env.BROWSER_TEST_USERNAME || 'user_demo';
const memberPassword = process.env.BROWSER_TEST_PASSWORD || 'demo';
const failures = [];
const browserErrors = [];
const governanceRequests = [];

const browser = await chromium.launch({ headless: true });
const adminContext = await browser.newContext({ viewport: { width: 1600, height: 1050 } });
const memberContext = await browser.newContext({ viewport: { width: 1440, height: 960 } });
const adminPage = await adminContext.newPage();
const memberPage = await memberContext.newPage();

function observe(page, actor) {
  page.on('pageerror', (error) => browserErrors.push(`${actor} pageerror: ${error.message}`));
  page.on('console', (message) => {
    if (message.type() === 'error' && !message.text().includes('403 (Forbidden)')) {
      browserErrors.push(`${actor} console: ${message.text()}`);
    }
  });
  page.on('response', (response) => {
    const url = new URL(response.url());
    if (url.pathname.startsWith('/api/organization/') || url.pathname.startsWith('/api/auth/')) {
      governanceRequests.push(`${response.status()} ${response.request().method()} ${url.pathname}`);
      if (response.status() === 404 || response.status() >= 500) {
        failures.push(`${response.status()} ${url.pathname}`);
      }
    }
  });
}

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

observe(adminPage, 'admin');
observe(memberPage, 'member');

try {
  await login(adminPage, adminUsername, adminPassword);
  await adminPage.goto(`${baseUrl}/enterprise/organization-roles?section=effective`);
  const explanations = adminPage.getByRole('table', { name: '有效权限解释列表' });
  await explanations.waitFor({ state: 'visible' });
  await explanations.getByText('平台管理员兼容', { exact: true }).first().waitFor({
    state: 'visible',
  });
  await explanations.getByText('全企业', { exact: true }).first().waitFor({ state: 'visible' });
  assert.ok(await explanations.getByText('organization.manage', { exact: true }).count() >= 1);
  assert.equal(await adminPage.getByText('Not Found', { exact: true }).count(), 0);

  await adminPage.goto(`${baseUrl}/enterprise/organization-roles?section=assignments`);
  await adminPage.getByRole('table', { name: '成员角色授权列表' }).waitFor({
    state: 'visible',
  });
  assert.equal(await adminPage.getByText('Not Found', { exact: true }).count(), 0);

  const routeStatuses = await adminPage.evaluate(async (currentTenantId) => {
    const raw = localStorage.getItem('gongge_auth');
    if (!raw) throw new Error('管理员会话不存在');
    const token = JSON.parse(raw).token;
    const headers = { Authorization: `Bearer ${token}` };
    const paths = [
      `/api/organization/effective-permissions?tenant_id=${currentTenantId}`,
      `/api/organization/business-roles?tenant_id=${currentTenantId}`,
      `/api/organization/employee-role-assignments?tenant_id=${currentTenantId}`,
      `/api/organization/permission-definitions?tenant_id=${currentTenantId}`,
      `/api/organization/role-categories?tenant_id=${currentTenantId}`,
    ];
    return Promise.all(paths.map(async (path) => ({
      path,
      status: (await fetch(path, { headers })).status,
    })));
  }, tenantId);
  assert.ok(routeStatuses.every((item) => item.status === 200));

  await login(memberPage, memberUsername, memberPassword);
  assert.equal(await memberPage.getByRole('button', { name: '组织与岗位' }).count(), 0);
  assert.equal(await memberPage.getByRole('button', { name: '组织角色' }).count(), 0);
  await memberPage.goto(`${baseUrl}/enterprise/organization-roles`);
  await memberPage.waitForURL(/\/workspace\/gallery$/);
  const memberBoundary = await memberPage.evaluate(async (currentTenantId) => {
    const raw = localStorage.getItem('gongge_auth');
    if (!raw) throw new Error('普通成员会话不存在');
    const token = JSON.parse(raw).token;
    const headers = { Authorization: `Bearer ${token}` };
    const [units, assignments, selfGrants] = await Promise.all([
      fetch(`/api/organization/units?tenant_id=${currentTenantId}`, { headers }),
      fetch(`/api/organization/employee-role-assignments?tenant_id=${currentTenantId}`, {
        headers,
      }),
      fetch(`/api/organization/effective-permissions?tenant_id=${currentTenantId}`, { headers }),
    ]);
    return {
      assignments: assignments.status,
      selfGrantCount: (await selfGrants.json()).length,
      selfGrants: selfGrants.status,
      units: units.status,
    };
  }, tenantId);
  assert.deepEqual(memberBoundary, {
    assignments: 403,
    selfGrantCount: 0,
    selfGrants: 200,
    units: 403,
  });

  assert.deepEqual(failures, []);
  assert.deepEqual(browserErrors, []);
  assert.ok(governanceRequests.length >= 10);
  await adminPage.screenshot({
    path: '.dev/m3a-live-governance-regression.png',
    fullPage: true,
  });
  console.log(JSON.stringify({
    status: 'passed',
    database: 'mysql',
    routeStatuses,
    memberBoundary,
    requestCount: governanceRequests.length,
    failures: failures.length,
    browserErrors: browserErrors.length,
  }, null, 2));
} catch (error) {
  await adminPage.screenshot({
    path: '.dev/m3a-live-governance-regression-failure.png',
    fullPage: true,
  });
  throw error;
} finally {
  await browser.close();
}

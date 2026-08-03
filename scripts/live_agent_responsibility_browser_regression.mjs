/**
 * @Time       : 2026/07/29 17:15
 * @Author     : zhanglp8181
 * @File       : live_agent_responsibility_browser_regression.mjs
 * @CallChain  : 生产前端/FastAPI → Chromium 双账号 → 组织/成员/数字员工责任事实
 * @Description: 只读验证责任组织展示、试点成员登录与普通成员治理权限边界。
 */

import assert from 'node:assert/strict';

import { chromium } from '../frontend-enterprise/node_modules/playwright/index.mjs';

const baseUrl = process.env.BROWSER_TEST_BASE_URL || 'http://127.0.0.1:5137';
const tenantId = process.env.BROWSER_TEST_TENANT_ID || 'tenant_demo';
const expectedMappedAgents = Number(process.env.BROWSER_TEST_EXPECTED_MAPPED_AGENTS || 1);
const expectedOrganizationName = process.env.BROWSER_TEST_EXPECTED_ORG_NAME;
const expectedAgentName = process.env.BROWSER_TEST_EXPECTED_AGENT_NAME;
const adminCredentials = {
  username: process.env.BROWSER_TEST_ADMIN_USERNAME || 'admin',
  password: process.env.BROWSER_TEST_ADMIN_PASSWORD || 'admin',
};
const pilotCredentials = {
  username: process.env.BROWSER_TEST_PILOT_USERNAME,
  password: process.env.BROWSER_TEST_PILOT_PASSWORD,
};

if (!expectedOrganizationName || !expectedAgentName) {
  throw new Error('必须提供责任组织和数字员工的预期显示名称');
}
if (!pilotCredentials.username || !pilotCredentials.password) {
  throw new Error('必须通过环境变量提供试点成员凭据');
}

const browserErrors = [];
const unexpectedResponses = [];
const observedApiPaths = new Set();
const browser = await chromium.launch({ headless: true });
const adminContext = await browser.newContext({ viewport: { width: 1600, height: 1050 } });
const pilotContext = await browser.newContext({ viewport: { width: 1440, height: 960 } });
const adminPage = await adminContext.newPage();
const pilotPage = await pilotContext.newPage();

function observe(page, actor) {
  page.on('pageerror', (error) => browserErrors.push(`${actor} pageerror: ${error.message}`));
  page.on('console', (message) => {
    if (message.type() === 'error' && !message.text().includes('403 (Forbidden)')) {
      browserErrors.push(`${actor} console: ${message.text()}`);
    }
  });
  page.on('response', (response) => {
    const url = new URL(response.url());
    if (!url.pathname.startsWith('/api/')) return;
    observedApiPaths.add(url.pathname);
    if (response.status() === 404 || response.status() >= 500) {
      unexpectedResponses.push(`${actor} ${response.status()} ${url.pathname}`);
    }
  });
}

async function login(page, credentials) {
  await page.goto(`${baseUrl}/enterprise/dashboard`);
  await page.evaluate(() => {
    localStorage.setItem('gongge_onboarding_guide_seen', '1');
    localStorage.setItem('gongge_quick_start_guide_seen', '1');
  });
  await page.getByRole('button', { name: '进入平台' }).click();
  await page.getByRole('textbox', { name: '账号' }).fill(credentials.username);
  await page.getByRole('textbox', { name: '密码', exact: true }).fill(credentials.password);
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await page.waitForFunction(() => Boolean(localStorage.getItem('gongge_auth')));
}

async function authenticatedFetch(page, path) {
  return page.evaluate(async (requestPath) => {
    const raw = localStorage.getItem('gongge_auth');
    if (!raw) throw new Error('浏览器认证会话不存在');
    const response = await fetch(requestPath, {
      headers: { Authorization: `Bearer ${JSON.parse(raw).token}` },
    });
    const body = await response.json();
    return { status: response.status, body };
  }, path);
}

observe(adminPage, 'administrator');
observe(pilotPage, 'pilot-member');

try {
  await login(adminPage, adminCredentials);

  const agents = await authenticatedFetch(
    adminPage,
    `/api/enterprise/agents?tenant_id=${tenantId}&scope=manageable`,
  );
  assert.equal(agents.status, 200);
  const mappedAgents = agents.body.filter((agent) => agent.responsible_org_unit_id);
  assert.ok(mappedAgents.length >= expectedMappedAgents);
  const targetAgent = agents.body.find((agent) => agent.name === expectedAgentName);
  assert.ok(targetAgent, '管理范围内缺少目标数字员工');
  assert.equal(targetAgent.responsible_org_unit_name, expectedOrganizationName);

  await adminPage.goto(`${baseUrl}/enterprise/organization`);
  await adminPage.getByRole('tree', { name: '企业组织树' }).waitFor({ state: 'visible' });
  await adminPage.getByRole('textbox', { name: '搜索组织' }).fill(expectedOrganizationName);
  await adminPage.getByRole('button', {
    name: new RegExp(expectedOrganizationName),
  }).first().waitFor({ state: 'visible' });

  await adminPage.goto(`${baseUrl}/enterprise/accounts`);
  const memberTable = adminPage.getByRole('table', { name: '成员列表' });
  await memberTable.waitFor({ state: 'visible' });
  await adminPage.getByPlaceholder('搜索成员、账号、工号、状态或类别')
    .fill(pilotCredentials.username);
  await memberTable.getByText(pilotCredentials.username, { exact: true })
    .first().waitFor({ state: 'visible' });

  await adminPage.goto(`${baseUrl}/enterprise/agents?view=all`);
  await adminPage.getByRole('textbox', { name: '搜索员工' }).fill(expectedAgentName);
  const targetName = adminPage.getByText(expectedAgentName).first();
  await targetName.waitFor({ state: 'visible' });
  const card = targetName.locator('xpath=ancestor::div[@role=\"button\"][1]');
  await card.getByRole('button', { name: '员工操作' }).click();
  await adminPage.getByRole('menuitem', { name: '编辑资料' }).click();
  const profileDialog = adminPage.getByRole('dialog');
  await profileDialog.waitFor({ state: 'visible' });
  await profileDialog.getByText(expectedOrganizationName, { exact: true })
    .first().waitFor({ state: 'visible' });
  await profileDialog.getByText(
    '只表示谁负责治理该数字员工，不自动改变服务范围、执行授权或知识权限。',
    { exact: true },
  ).waitFor({ state: 'visible' });

  await login(pilotPage, pilotCredentials);
  await pilotPage.goto(`${baseUrl}/enterprise/dashboard`);
  await pilotPage.getByRole('button', { name: '开放广场平台' }).waitFor({ state: 'visible' });
  const memberEnumeration = await authenticatedFetch(
    pilotPage,
    `/api/auth/users/page?tenant_id=${tenantId}&page=1&page_size=10`,
  );
  assert.equal(memberEnumeration.status, 403);

  assert.ok(observedApiPaths.has('/api/organization/unit-search'));
  assert.ok(observedApiPaths.has('/api/auth/users/page'));
  assert.deepEqual(unexpectedResponses, []);
  assert.deepEqual(browserErrors, []);
  assert.equal(await adminPage.getByText('Not Found', { exact: true }).count(), 0);

  await adminPage.screenshot({
    path: '.dev/agent-responsibility-live-regression.png',
    fullPage: true,
  });
  console.log(JSON.stringify({
    status: 'passed',
    mappedAgentCount: mappedAgents.length,
    pilotMemberEnumerationStatus: memberEnumeration.status,
    unexpectedResponses: unexpectedResponses.length,
    browserErrors: browserErrors.length,
  }, null, 2));
} catch (error) {
  await adminPage.screenshot({
    path: '.dev/agent-responsibility-live-regression-failure.png',
    fullPage: true,
  });
  throw error;
} finally {
  await browser.close();
}

/**
 * @Time       : 2026/07/28 21:10
 * @Author     : zhanglp8181
 * @File       : live_m4a_browser_regression.mjs
 * @CallChain  : 5137 实际 MySQL 部署 → Chromium 双账号 → M4-A 正式字段与广场兼容验收
 * @Description: 只读验证 Agent 正式字段、码表、旧 owner 回填和企业广场页面的运行一致性。
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
const contractFailures = [];
const observedRequests = [];

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
    if (
      url.pathname.startsWith('/api/enterprise/agents')
      || url.pathname.startsWith('/api/reference-data')
    ) {
      observedRequests.push(`${actor} ${response.status()} ${url.pathname}`);
      if (response.status() === 404 || response.status() >= 500) {
        contractFailures.push(`${actor} ${response.status()} ${url.pathname}`);
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

async function readContracts(page) {
  return page.evaluate(async (currentTenantId) => {
    const raw = localStorage.getItem('gongge_auth');
    if (!raw) throw new Error('浏览器认证会话不存在');
    const headers = { Authorization: `Bearer ${JSON.parse(raw).token}` };
    const [agentsResponse, catalogsResponse] = await Promise.all([
      fetch(`/api/enterprise/agents?tenant_id=${currentTenantId}`, { headers }),
      fetch(`/api/reference-data/code-sets?tenant_id=${currentTenantId}`, { headers }),
    ]);
    return {
      agents: await agentsResponse.json(),
      agentsStatus: agentsResponse.status,
      catalogs: await catalogsResponse.json(),
      catalogsStatus: catalogsResponse.status,
    };
  }, tenantId);
}

observe(adminPage, 'admin');
observe(memberPage, 'member');

try {
  await login(adminPage, adminUsername, adminPassword);
  const adminContracts = await readContracts(adminPage);
  assert.equal(adminContracts.agentsStatus, 200);
  assert.equal(adminContracts.catalogsStatus, 200);
  assert.ok(adminContracts.agents.length > 0);
  assert.ok(
    adminContracts.catalogs.some((item) => item.code === 'agent_category'),
    '统一码表必须返回 agent_category',
  );
  assert.ok(adminContracts.agents.every((item) => (
    Number.isInteger(item.profile_revision)
    && item.profile_revision >= 1
    && typeof item.published_to_gallery === 'boolean'
    && ['assistant', 'professional', 'service', 'operations'].includes(
      item.agent_category_code,
    )
    && ['private', 'tenant'].includes(item.visibility_scope)
  )));
  assert.ok(adminContracts.agents.every((item) => (
    item.is_overall || typeof item.owner_user_id === 'string'
  )));
  assert.ok(adminContracts.agents.every((item) => (
    !item.published_to_gallery || item.visibility_scope === 'tenant'
  )));

  await adminPage.goto(`${baseUrl}/enterprise/agents`);
  await adminPage.getByRole('heading', { name: '可管理数字员工' }).waitFor({
    state: 'visible',
  });
  assert.equal(await adminPage.getByText('Not Found', { exact: true }).count(), 0);

  await login(memberPage, memberUsername, memberPassword);
  const memberContracts = await readContracts(memberPage);
  assert.equal(memberContracts.agentsStatus, 200);
  const published = memberContracts.agents.find((item) => (
    !item.is_overall && item.published_to_gallery
  ));
  assert.ok(published, '普通成员必须能发现至少一个企业广场数字员工');

  await memberPage.goto(`${baseUrl}/workspace/gallery`);
  await memberPage.getByRole('tab', { name: '数字员工广场' }).click();
  await memberPage.getByText(published.name, { exact: false }).first().waitFor({
    state: 'visible',
  });
  assert.equal(await memberPage.getByText('Not Found', { exact: true }).count(), 0);

  assert.deepEqual(contractFailures, []);
  assert.deepEqual(browserErrors, []);
  assert.ok(observedRequests.length >= 4);

  await memberPage.screenshot({
    path: '.dev/m4a-live-agent-gallery-regression.png',
    fullPage: true,
  });
  console.log(JSON.stringify({
    status: 'passed',
    database: 'mysql',
    contract: 'agent-identity-and-gallery',
    agentCount: adminContracts.agents.length,
    publishedAgent: published.id,
    requestCount: observedRequests.length,
    contractFailures: contractFailures.length,
    browserErrors: browserErrors.length,
  }, null, 2));
} catch (error) {
  await memberPage.screenshot({
    path: '.dev/m4a-live-agent-gallery-regression-failure.png',
    fullPage: true,
  });
  throw error;
} finally {
  await browser.close();
}

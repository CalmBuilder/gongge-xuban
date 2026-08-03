/**
 * @Time       : 2026/07/28 23:40
 * @Author     : zhanglp8181
 * @File       : live_m5a_browser_regression.mjs
 * @CallChain  : 5137 实际 MySQL → Chromium 管理员 → M5-A 知识治理与局部降级验收
 * @Description: 创建临时知识库，验证默认最小权限、组织治理、revision 冲突和失败接口局部降级后清理。
 */

import assert from 'node:assert/strict';

import { chromium } from '../frontend-enterprise/node_modules/playwright/index.mjs';

const baseUrl = process.env.BROWSER_TEST_BASE_URL || 'http://127.0.0.1:5137';
const tenantId = process.env.BROWSER_TEST_TENANT_ID || 'tenant_demo';
const adminUsername = process.env.BROWSER_TEST_ADMIN_USERNAME || 'admin';
const adminPassword = process.env.BROWSER_TEST_ADMIN_PASSWORD || 'admin';
const temporaryName = `M5-A 浏览器临时知识库 ${Date.now()}`;
const browserErrors = [];
const contractFailures = [];
const observedRequests = [];

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1600, height: 1050 } });
const page = await context.newPage();
let temporaryKnowledgeBaseId = '';

page.on('pageerror', (error) => browserErrors.push(`pageerror: ${error.message}`));
page.on('console', (message) => {
  if (
    message.type() === 'error'
    && !message.text().includes('404 (Not Found)')
    && !message.text().includes('409 (Conflict)')
  ) {
    browserErrors.push(`console: ${message.text()}`);
  }
});
page.on('response', (response) => {
  const url = new URL(response.url());
  if (
    url.pathname.startsWith('/api/enterprise/knowledge-bases')
    || url.pathname.startsWith('/api/organization/')
  ) {
    observedRequests.push(`${response.status()} ${response.request().method()} ${url.pathname}`);
    if (response.status() >= 500 || (response.status() === 404 && !url.pathname.includes('/documents'))) {
      contractFailures.push(`${response.status()} ${url.pathname}`);
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
  await page.evaluate(() => {
    localStorage.removeItem('gongge_enterprise_agent_scope');
  });
}

async function authenticatedFetch(path, options = {}) {
  return page.evaluate(async ({ requestPath, requestOptions }) => {
    const raw = localStorage.getItem('gongge_auth');
    if (!raw) throw new Error('浏览器认证会话不存在');
    const response = await fetch(requestPath, {
      ...requestOptions,
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${JSON.parse(raw).token}`,
        ...(requestOptions.headers || {}),
      },
    });
    let body = null;
    try {
      body = await response.json();
    } catch {
      body = null;
    }
    return { status: response.status, body };
  }, { requestPath: path, requestOptions: options });
}

try {
  await login();
  const created = await authenticatedFetch('/api/enterprise/knowledge-bases', {
    method: 'POST',
    body: JSON.stringify({
      tenant_id: tenantId,
      name: temporaryName,
      description: 'M5-A 真实浏览器可清理验收数据',
    }),
  });
  assert.equal(created.status, 200);
  temporaryKnowledgeBaseId = created.body.id;
  assert.equal(created.body.owner_user_id.length > 0, true);
  assert.equal(created.body.access_scope, 'owner');
  assert.equal(created.body.download_policy, 'restricted');
  assert.equal(created.body.revision, 1);

  await page.route('**/api/enterprise/knowledge/documents?**', async (route) => {
    await route.fulfill({
      status: 404,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'forced M5-A partial failure' }),
    });
  });
  await page.goto(`${baseUrl}/enterprise/knowledge`);
  const platformGovernanceButton = page.getByRole('button', { name: '平台知识治理' });
  const employeeScopeLoaded = await platformGovernanceButton
    .waitFor({ state: 'visible', timeout: 10_000 })
    .then(() => true)
    .catch(() => false);
  if (employeeScopeLoaded) {
    await platformGovernanceButton.click();
  }
  const row = page.getByRole('row').filter({ hasText: temporaryName });
  await row.waitFor({ state: 'visible' });
  assert.equal(await page.getByText('Not Found', { exact: true }).count(), 0);
  await page.unroute('**/api/enterprise/knowledge/documents?**');

  await row.waitFor({ state: 'visible' });
  await row.getByRole('button', { name: '知识库操作' }).click();
  await page.getByText('访问治理', { exact: true }).click();
  const tree = page.getByRole('tree', { name: '企业组织树' });
  await tree.waitFor({ state: 'visible' });
  const root = tree.getByRole('treeitem').first();
  const rootText = (await root.innerText()).trim();
  await root.click();
  await page.getByRole('combobox', { name: '知识访问范围' }).click();
  await page.getByRole('option', { name: '指定组织' }).click();
  await page.getByRole('button', { name: '设为责任组织' }).click();
  await page.getByRole('button', { name: '加入所选组织' }).click();
  const governanceResponse = page.waitForResponse((response) => (
    response.url().includes(`/api/enterprise/knowledge-bases/${temporaryKnowledgeBaseId}/governance`)
    && response.request().method() === 'PUT'
  ));
  await page.getByRole('button', { name: '保存治理范围' }).click();
  assert.equal((await governanceResponse).status(), 200);

  const saved = await authenticatedFetch(
    `/api/enterprise/knowledge-bases/${temporaryKnowledgeBaseId}?tenant_id=${tenantId}`,
  );
  assert.equal(saved.status, 200);
  assert.equal(saved.body.revision, 2);
  assert.equal(saved.body.access_scope, 'organization');
  assert.equal(saved.body.download_policy, 'restricted');
  assert.equal(saved.body.organization_access.length, 1);
  assert.equal(saved.body.responsible_org_unit_id, saved.body.organization_access[0].org_unit_id);

  const stale = await authenticatedFetch(
    `/api/enterprise/knowledge-bases/${temporaryKnowledgeBaseId}/governance`,
    {
      method: 'PUT',
      body: JSON.stringify({
        tenant_id: tenantId,
        expected_revision: 1,
        responsible_org_unit_id: null,
        access_scope: 'owner',
        download_policy: 'restricted',
        organization_access: [],
      }),
    },
  );
  assert.equal(stale.status, 409);
  assert.equal(stale.body.detail.code, 'KNOWLEDGE_BASE_REVISION_CONFLICT');

  assert.deepEqual(contractFailures, []);
  assert.deepEqual(browserErrors, []);
  assert.ok(observedRequests.length >= 6);
  await page.screenshot({
    path: '.dev/m5a-live-knowledge-governance-regression.png',
    fullPage: true,
  });
  console.log(JSON.stringify({
    status: 'passed',
    database: 'mysql',
    revision: '20260728_0025',
    temporaryKnowledgeBaseId,
    organizationRoot: rootText,
    requestCount: observedRequests.length,
    contractFailures: contractFailures.length,
    browserErrors: browserErrors.length,
  }, null, 2));
} catch (error) {
  await page.screenshot({
    path: '.dev/m5a-live-knowledge-governance-regression-failure.png',
    fullPage: true,
  });
  throw error;
} finally {
  if (temporaryKnowledgeBaseId) {
    const cleaned = await authenticatedFetch(
      `/api/enterprise/knowledge-bases/${temporaryKnowledgeBaseId}?tenant_id=${tenantId}`,
      { method: 'DELETE' },
    );
    assert.equal(cleaned.status, 200);
  }
  await browser.close();
}

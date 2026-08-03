/**
 * @Time       : 2026/07/29 08:30
 * @Author     : zhanglp8181
 * @File       : live_m5c_browser_regression.mjs
 * @CallChain  : 生产前端/FastAPI → Chromium 三账号 → 管理审计页面/API/数据库事实
 * @Description: 验证租户审计员、组织范围审计员和普通成员的成功、拒绝、失败审计及直接 URL 边界。
 */

import assert from 'node:assert/strict';

import { chromium } from '../frontend-enterprise/node_modules/playwright/index.mjs';

const baseUrl = process.env.BROWSER_TEST_BASE_URL || 'http://127.0.0.1:5137';
const databaseLabel = process.env.BROWSER_TEST_DATABASE || 'mysql';
const tenantId = process.env.BROWSER_TEST_TENANT_ID || 'tenant_demo';
const adminCredentials = {
  username: process.env.BROWSER_TEST_ADMIN_USERNAME || 'admin',
  password: process.env.BROWSER_TEST_ADMIN_PASSWORD || 'admin',
};
const scopedCredentials = {
  username: process.env.BROWSER_TEST_SCOPED_USERNAME || 'user_demo',
  password: process.env.BROWSER_TEST_SCOPED_PASSWORD || 'demo',
};
const ordinaryCredentials = {
  username: process.env.BROWSER_TEST_ORDINARY_USERNAME || 'approver_demo',
  password: process.env.BROWSER_TEST_ORDINARY_PASSWORD || 'demo',
};
const suffix = Date.now();
const browserErrors = [];
const unexpectedResponses = [];
const observedResponses = [];
const browser = await chromium.launch({ headless: true });
const adminContext = await browser.newContext({ viewport: { width: 1600, height: 1050 } });
const scopedContext = await browser.newContext({ viewport: { width: 1500, height: 1000 } });
const ordinaryContext = await browser.newContext({ viewport: { width: 1440, height: 960 } });
const adminPage = await adminContext.newPage();
const scopedPage = await scopedContext.newPage();
const ordinaryPage = await ordinaryContext.newPage();

let scopedOrganizationId = '';
let siblingOrganizationId = '';
let roleAssignmentId = '';
let knowledgeBaseId = '';

function observe(page, actor) {
  page.on('pageerror', (error) => browserErrors.push(`${actor} pageerror: ${error.message}`));
  page.on('console', (message) => {
    if (
      message.type() === 'error'
      && !message.text().includes('403 (Forbidden)')
      && !message.text().includes('404 (Not Found)')
      && !message.text().includes('409 (Conflict)')
    ) {
      browserErrors.push(`${actor} console: ${message.text()}`);
    }
  });
  page.on('response', (response) => {
    const url = new URL(response.url());
    if (
      url.pathname.startsWith('/api/management-audit')
      || url.pathname.startsWith('/api/organization')
      || url.pathname.startsWith('/api/enterprise/knowledge-bases')
    ) {
      const record = `${actor} ${response.status()} ${response.request().method()} ${url.pathname}`;
      observedResponses.push(record);
      if (response.status() >= 500) unexpectedResponses.push(record);
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
  await page.goto(`${baseUrl}/enterprise/dashboard`);
  await page.getByRole('button', { name: '开放广场平台' }).waitFor({
    state: 'visible',
  });
}

async function authenticatedFetch(page, path, options = {}) {
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
    const contentType = response.headers.get('content-type') || '';
    const body = contentType.includes('application/json')
      ? await response.json()
      : await response.text();
    return { status: response.status, body };
  }, { requestPath: path, requestOptions: options });
}

function auditQuery(filters = {}) {
  const params = new URLSearchParams({
    tenant_id: tenantId,
    page: '1',
    page_size: '100',
    ...filters,
  });
  return `/api/management-audit/logs?${params.toString()}`;
}

observe(adminPage, 'tenant-auditor');
observe(scopedPage, 'scoped-auditor');
observe(ordinaryPage, 'ordinary-member');

try {
  await login(adminPage, adminCredentials);
  const roots = await authenticatedFetch(
    adminPage,
    `/api/organization/unit-children?tenant_id=${tenantId}`,
  );
  assert.equal(roots.status, 200);
  assert.equal(roots.body.length, 1);
  const rootId = roots.body[0].id;

  const users = await authenticatedFetch(adminPage, `/api/auth/users?tenant_id=${tenantId}`);
  assert.equal(users.status, 200);
  const scopedUser = users.body.find((item) => item.username === scopedCredentials.username);
  assert.ok(scopedUser?.employee_profile_id);

  const scopedOrganization = await authenticatedFetch(adminPage, '/api/organization/units', {
    method: 'POST',
    body: JSON.stringify({
      tenant_id: tenantId,
      parent_id: rootId,
      code: `M5CA${suffix}`,
      name: `M5-C 审计范围 ${suffix}`,
      unit_type_code: 'department',
      sort_order: 999,
    }),
  });
  assert.equal(scopedOrganization.status, 200);
  scopedOrganizationId = scopedOrganization.body.id;

  const siblingOrganization = await authenticatedFetch(adminPage, '/api/organization/units', {
    method: 'POST',
    body: JSON.stringify({
      tenant_id: tenantId,
      parent_id: rootId,
      code: `M5CB${suffix}`,
      name: `M5-C 范围外组织 ${suffix}`,
      unit_type_code: 'department',
      sort_order: 999,
    }),
  });
  assert.equal(siblingOrganization.status, 200);
  siblingOrganizationId = siblingOrganization.body.id;

  const roleAssignment = await authenticatedFetch(
    adminPage,
    '/api/organization/employee-role-assignments',
    {
      method: 'POST',
      body: JSON.stringify({
        tenant_id: tenantId,
        employee_profile_id: scopedUser.employee_profile_id,
        role_code: 'governance_auditor',
        scope_type: 'org_unit',
        scope_id: scopedOrganizationId,
        include_descendants: true,
        grant_reason: 'M5-C 浏览器范围授权回归，测试结束后停用',
        effective_until: new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString(),
      }),
    },
  );
  assert.equal(roleAssignment.status, 200);
  roleAssignmentId = roleAssignment.body.id;

  const knowledgeBase = await authenticatedFetch(
    adminPage,
    '/api/enterprise/knowledge-bases',
    {
      method: 'POST',
      body: JSON.stringify({
        tenant_id: tenantId,
        name: `M5-C 私有审计资料 ${suffix}`,
        description: 'M5-C 可清理浏览器验收数据',
      }),
    },
  );
  assert.equal(knowledgeBase.status, 200);
  knowledgeBaseId = knowledgeBase.body.id;

  const governed = await authenticatedFetch(
    adminPage,
    `/api/enterprise/knowledge-bases/${knowledgeBaseId}/governance`,
    {
      method: 'PUT',
      body: JSON.stringify({
        tenant_id: tenantId,
        expected_revision: 1,
        responsible_org_unit_id: scopedOrganizationId,
        access_scope: 'owner',
        download_policy: 'restricted',
        organization_access: [],
      }),
    },
  );
  assert.equal(governed.status, 200);
  assert.equal(governed.body.revision, 2);

  const failedUpdate = await authenticatedFetch(
    adminPage,
    `/api/enterprise/knowledge-bases/${knowledgeBaseId}/governance`,
    {
      method: 'PUT',
      body: JSON.stringify({
        tenant_id: tenantId,
        expected_revision: 1,
        responsible_org_unit_id: scopedOrganizationId,
        access_scope: 'owner',
        download_policy: 'restricted',
        organization_access: [],
      }),
    },
  );
  assert.equal(failedUpdate.status, 409);

  await login(ordinaryPage, ordinaryCredentials);
  assert.equal(await ordinaryPage.getByRole('button', { name: '管理审计' }).count(), 0);
  const deniedKnowledge = await authenticatedFetch(
    ordinaryPage,
    `/api/enterprise/knowledge-bases/${knowledgeBaseId}/versions?tenant_id=${tenantId}`,
  );
  assert.equal(deniedKnowledge.status, 404);
  const deniedAuditApi = await authenticatedFetch(ordinaryPage, auditQuery());
  assert.equal(deniedAuditApi.status, 403);
  await ordinaryPage.goto(`${baseUrl}/enterprise/management-audit`);
  await ordinaryPage.waitForURL(/\/workspace\/gallery$/);

  const successLogs = await authenticatedFetch(
    adminPage,
    auditQuery({ action: 'knowledge.governance.update', outcome: 'success' }),
  );
  const failureLogs = await authenticatedFetch(
    adminPage,
    auditQuery({ action: 'knowledge.governance.update', outcome: 'failure' }),
  );
  const deniedLogs = await authenticatedFetch(
    adminPage,
    auditQuery({ action: 'knowledge.read', outcome: 'denied' }),
  );
  assert.equal(successLogs.status, 200);
  assert.equal(failureLogs.status, 200);
  assert.equal(deniedLogs.status, 200);
  const successRow = successLogs.body.items.find((row) => row.resource_id === knowledgeBaseId);
  const failureRow = failureLogs.body.items.find((row) => row.resource_id === knowledgeBaseId);
  const deniedRow = deniedLogs.body.items.find((row) => row.resource_id === knowledgeBaseId);
  assert.ok(successRow?.request_id);
  assert.ok(failureRow?.request_id);
  assert.ok(deniedRow?.request_id);
  assert.equal(successRow.target_org_unit_id, scopedOrganizationId);
  assert.equal(failureRow.target_org_unit_id, scopedOrganizationId);
  assert.equal(deniedRow.target_org_unit_id, scopedOrganizationId);

  const siblingLogs = await authenticatedFetch(
    adminPage,
    auditQuery({ action: 'organization.create', resource_id: siblingOrganizationId }),
  );
  assert.equal(siblingLogs.status, 200);
  assert.equal(siblingLogs.body.items.length, 1);

  await login(scopedPage, scopedCredentials);
  assert.equal(await scopedPage.getByRole('button', { name: '管理审计' }).count(), 1);
  const scopedFailureLogs = await authenticatedFetch(
    scopedPage,
    auditQuery({ action: 'knowledge.governance.update', outcome: 'failure' }),
  );
  assert.equal(scopedFailureLogs.status, 200);
  assert.ok(scopedFailureLogs.body.items.some((row) => row.id === failureRow.id));
  const outsideDirect = await authenticatedFetch(
    scopedPage,
    `/api/management-audit/logs/${siblingLogs.body.items[0].id}?tenant_id=${tenantId}`,
  );
  assert.equal(outsideDirect.status, 404);

  await scopedPage.goto(`${baseUrl}/enterprise/management-audit`);
  await scopedPage.getByText('企业管理操作台账', { exact: true }).waitFor({ state: 'visible' });
  await scopedPage.getByLabel('操作编码').fill('knowledge.governance.update');
  await scopedPage.getByRole('combobox', { name: '结果' }).click();
  await scopedPage.getByRole('option', { name: '失败' }).click();
  const filteredResponse = scopedPage.waitForResponse((response) => (
    response.url().includes('/api/management-audit/logs?')
    && response.url().includes('action=knowledge.governance.update')
    && response.url().includes('outcome=failure')
  ));
  await scopedPage.getByRole('button', { name: '查询' }).click();
  assert.equal((await filteredResponse).status(), 200);
  const filteredRow = scopedPage
    .getByRole('row')
    .filter({ hasText: 'knowledge.governance.update' })
    .filter({ hasText: '失败' });
  await filteredRow.waitFor({ state: 'visible' });
  await filteredRow.getByRole('button', { name: '查看详情' }).click();
  await scopedPage.getByText('审计详情', { exact: true }).waitFor({ state: 'visible' });
  assert.ok(await scopedPage.getByText(failureRow.request_id, { exact: true }).count() >= 1);
  assert.equal(await scopedPage.getByRole('button', { name: /导出/ }).count(), 0);

  await adminPage.goto(`${baseUrl}/enterprise/management-audit`);
  await adminPage.getByText('企业管理操作台账', { exact: true }).waitFor({ state: 'visible' });
  assert.equal(await adminPage.getByText('Not Found', { exact: true }).count(), 0);

  assert.deepEqual(unexpectedResponses, []);
  assert.deepEqual(browserErrors, []);
  assert.ok(observedResponses.length >= 20);
  await scopedPage.screenshot({
    path: `.dev/m5c-${databaseLabel}-scoped-audit.png`,
    fullPage: true,
  });
  console.log(JSON.stringify({
    status: 'passed',
    database: databaseLabel,
    scopedOrganizationId,
    siblingOrganizationId,
    knowledgeBaseId,
    successAuditId: successRow.id,
    deniedAuditId: deniedRow.id,
    failureAuditId: failureRow.id,
    observedResponseCount: observedResponses.length,
    unexpectedResponses: unexpectedResponses.length,
    browserErrors: browserErrors.length,
  }, null, 2));
} catch (error) {
  await scopedPage.screenshot({
    path: `.dev/m5c-${databaseLabel}-regression-failure.png`,
    fullPage: true,
  });
  throw error;
} finally {
  if (knowledgeBaseId) {
    const removed = await authenticatedFetch(
      adminPage,
      `/api/enterprise/knowledge-bases/${knowledgeBaseId}?tenant_id=${tenantId}`,
      { method: 'DELETE' },
    );
    assert.equal(removed.status, 200);
  }
  if (roleAssignmentId) {
    const deactivated = await authenticatedFetch(
      adminPage,
      `/api/organization/employee-role-assignments/${roleAssignmentId}?tenant_id=${tenantId}`,
      { method: 'DELETE' },
    );
    assert.equal(deactivated.status, 200);
  }
  for (const organizationId of [scopedOrganizationId, siblingOrganizationId]) {
    if (organizationId) {
      const deactivated = await authenticatedFetch(
        adminPage,
        `/api/organization/units/${organizationId}?tenant_id=${tenantId}`,
        { method: 'DELETE' },
      );
      assert.equal(deactivated.status, 200);
    }
  }
  await browser.close();
}

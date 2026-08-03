/**
 * @Time       : 2026/07/29 16:30
 * @Author     : zhanglp8181
 * @File       : live_m5b_browser_regression.mjs
 * @CallChain  : 5137 实际 MySQL → Chromium 三账号 → 知识正文交集与直接 URL 验收
 * @Description: 用可清理组织归属验证范围内、范围外和仅治理三类账号的列表、检索、正文与导出边界。
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
const insideCredentials = {
  username: process.env.BROWSER_TEST_INSIDE_USERNAME || 'user_demo',
  password: process.env.BROWSER_TEST_INSIDE_PASSWORD || 'demo',
};
const outsideCredentials = {
  username: process.env.BROWSER_TEST_OUTSIDE_USERNAME || 'approver_demo',
  password: process.env.BROWSER_TEST_OUTSIDE_PASSWORD || 'demo',
};
const agentId = process.env.BROWSER_TEST_AGENT_ID || 'agent_9d3d1fdf171049ed';
const suffix = Date.now();
const temporaryKnowledgeName = `M5-B 政企研发资料库 ${suffix}`;
const temporaryOrganizationName = `M5-B 政企项目集 ${suffix}`;
const knowledgeSnippet = `M5-B-${suffix}：政企项目交付前必须完成安全检查。`;
const browserErrors = [];
const unexpectedResponses = [];
const observedResponses = [];

const browser = await chromium.launch({ headless: true });
const adminContext = await browser.newContext({ viewport: { width: 1600, height: 1050 } });
const insideContext = await browser.newContext({ viewport: { width: 1440, height: 960 } });
const outsideContext = await browser.newContext({ viewport: { width: 1440, height: 960 } });
const adminPage = await adminContext.newPage();
const insidePage = await insideContext.newPage();
const outsidePage = await outsideContext.newPage();

let knowledgeBaseId = '';
let documentId = '';
let organizationId = '';
let assignmentId = '';

function observe(page, actor) {
  page.on('pageerror', (error) => browserErrors.push(`${actor} pageerror: ${error.message}`));
  page.on('console', (message) => {
    if (
      message.type() === 'error'
      && !message.text().includes('404 (Not Found)')
      && !message.text().includes('403 (Forbidden)')
    ) {
      browserErrors.push(`${actor} console: ${message.text()}`);
    }
  });
  page.on('response', (response) => {
    const url = new URL(response.url());
    if (
      url.pathname.startsWith('/api/enterprise/knowledge')
      || url.pathname.startsWith('/api/organization/')
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
    let body = null;
    if (contentType.includes('application/json')) {
      body = await response.json();
    } else {
      body = await response.text();
    }
    return { status: response.status, body };
  }, { requestPath: path, requestOptions: options });
}

async function useAgentScope(page) {
  await page.evaluate((currentAgentId) => {
    localStorage.setItem('gongge_enterprise_agent_scope', currentAgentId);
  }, agentId);
}

async function waitForIngest(jobId) {
  for (let index = 0; index < 80; index += 1) {
    const result = await authenticatedFetch(
      adminPage,
      `/api/enterprise/knowledge/jobs/${jobId}?tenant_id=${tenantId}&agent_id=${agentId}`,
    );
    assert.equal(result.status, 200);
    if (result.body.status === 'succeeded') return result.body;
    if (result.body.status === 'failed' || result.body.status === 'cancelled') {
      throw new Error(`知识摄取未成功：${JSON.stringify(result.body)}`);
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error('知识摄取超时');
}

observe(adminPage, 'governor');
observe(insidePage, 'inside');
observe(outsidePage, 'outside');

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
  const insideUser = users.body.find((item) => item.username === insideCredentials.username);
  assert.ok(insideUser?.employee_profile_id);

  const organization = await authenticatedFetch(adminPage, '/api/organization/units', {
    method: 'POST',
    body: JSON.stringify({
      tenant_id: tenantId,
      parent_id: rootId,
      code: `M5B${suffix}`,
      name: temporaryOrganizationName,
      unit_type_code: 'department',
      sort_order: 999,
    }),
  });
  assert.equal(organization.status, 200);
  organizationId = organization.body.id;

  const assignment = await authenticatedFetch(
    adminPage,
    '/api/organization/member-org-assignments',
    {
      method: 'POST',
      body: JSON.stringify({
        tenant_id: tenantId,
        employee_profile_id: insideUser.employee_profile_id,
        org_unit_id: organizationId,
        assignment_type: 'project',
      }),
    },
  );
  assert.equal(assignment.status, 200);
  assignmentId = assignment.body.id;

  const created = await authenticatedFetch(
    adminPage,
    `/api/enterprise/knowledge-bases?agent_id=${agentId}`,
    {
      method: 'POST',
      body: JSON.stringify({
        tenant_id: tenantId,
        name: temporaryKnowledgeName,
        description: 'M5-B 三账号真实浏览器可清理验收数据',
      }),
    },
  );
  assert.equal(created.status, 200);
  knowledgeBaseId = created.body.id;

  const markdown = [
    '# 政企研发规范',
    '',
    '## 交付安全检查',
    '',
    knowledgeSnippet,
    '',
    '检查完成后才能进入交付审批。',
  ].join('\n');
  const uploaded = await authenticatedFetch(
    adminPage,
    `/api/enterprise/knowledge/documents?agent_id=${agentId}`,
    {
      method: 'POST',
      body: JSON.stringify({
        tenant_id: tenantId,
        knowledge_base_id: knowledgeBaseId,
        filename: `m5b-policy-${suffix}.md`,
        title: '政企研发规范',
        content_base64: Buffer.from(markdown, 'utf8').toString('base64'),
      }),
    },
  );
  assert.equal(uploaded.status, 200);
  const completedJob = await waitForIngest(uploaded.body.id);
  documentId = completedJob.document_id;
  assert.ok(documentId);

  const governance = await authenticatedFetch(
    adminPage,
    `/api/enterprise/knowledge-bases/${knowledgeBaseId}/governance`,
    {
      method: 'PUT',
      body: JSON.stringify({
        tenant_id: tenantId,
        expected_revision: 1,
        responsible_org_unit_id: organizationId,
        access_scope: 'organization',
        download_policy: 'restricted',
        organization_access: [{
          org_unit_id: organizationId,
          include_descendants: true,
        }],
      }),
    },
  );
  assert.equal(governance.status, 200);
  assert.equal(governance.body.content_access_allowed, false);

  const governorDirect = await authenticatedFetch(
    adminPage,
    `/api/enterprise/knowledge/documents/${documentId}?tenant_id=${tenantId}&agent_id=${agentId}`,
  );
  assert.equal(governorDirect.status, 404);

  await adminPage.evaluate(() => localStorage.removeItem('gongge_enterprise_agent_scope'));
  await adminPage.goto(`${baseUrl}/enterprise/knowledge`);
  const platformGovernanceButton = adminPage.getByRole('button', {
    name: '平台知识治理',
  });
  const employeeScopeLoaded = await platformGovernanceButton
    .waitFor({ state: 'visible', timeout: 10_000 })
    .then(() => true)
    .catch(() => false);
  if (employeeScopeLoaded) {
    await platformGovernanceButton.click();
  }
  const governanceRow = adminPage.getByRole('row').filter({ hasText: temporaryKnowledgeName });
  await governanceRow.waitFor({ state: 'visible' });
  assert.ok(await governanceRow.getByText('仅可治理', { exact: true }).count() >= 1);
  await governanceRow.getByRole('button', { name: '知识库操作' }).click();
  assert.equal(
    await adminPage.getByRole('menuitem', { name: '访问治理' }).getAttribute('data-disabled'),
    null,
  );
  assert.notEqual(
    await adminPage.getByRole('menuitem', { name: '版本管理' }).getAttribute('data-disabled'),
    null,
  );

  await login(insidePage, insideCredentials);
  await useAgentScope(insidePage);
  const insideList = await authenticatedFetch(
    insidePage,
    `/api/enterprise/knowledge-bases?tenant_id=${tenantId}&agent_id=${agentId}`,
  );
  assert.equal(insideList.status, 200);
  assert.ok(insideList.body.some((item) => item.id === knowledgeBaseId));
  const insideDocuments = await authenticatedFetch(
    insidePage,
    `/api/enterprise/knowledge/documents?tenant_id=${tenantId}&agent_id=${agentId}`,
  );
  assert.equal(insideDocuments.status, 200);
  assert.ok(insideDocuments.body.some((item) => item.id === documentId));
  const insideDirect = await authenticatedFetch(
    insidePage,
    `/api/enterprise/knowledge/documents/${documentId}?tenant_id=${tenantId}&agent_id=${agentId}`,
  );
  assert.equal(insideDirect.status, 200);
  const insideSearch = await authenticatedFetch(insidePage, '/api/enterprise/knowledge/search', {
    method: 'POST',
    body: JSON.stringify({
      tenant_id: tenantId,
      agent_id: agentId,
      query: '政企研发规范 安全检查',
    }),
  });
  assert.equal(insideSearch.status, 200);
  assert.ok(JSON.stringify(insideSearch.body).includes(knowledgeSnippet));
  const inaccessibleVersionSearch = await authenticatedFetch(
    insidePage,
    '/api/enterprise/knowledge/search',
    {
      method: 'POST',
      body: JSON.stringify({
        tenant_id: tenantId,
        agent_id: agentId,
        query: '政企研发规范 安全检查',
        knowledge_base_version_ids: ['kbver_forbidden'],
      }),
    },
  );
  assert.equal(inaccessibleVersionSearch.status, 200);
  assert.equal(JSON.stringify(inaccessibleVersionSearch.body).includes(knowledgeSnippet), false);
  assert.ok(
    inaccessibleVersionSearch.body.trace.some(
      (item) => item.phase === 'no_accessible_knowledge',
    ),
  );
  const restrictedExport = await authenticatedFetch(
    insidePage,
    `/api/enterprise/knowledge-bases/${knowledgeBaseId}/okf/export?tenant_id=${tenantId}&agent_id=${agentId}`,
  );
  assert.equal(restrictedExport.status, 404);
  await insidePage.goto(`${baseUrl}/enterprise/knowledge`);
  const insideRow = insidePage.getByRole('row').filter({ hasText: temporaryKnowledgeName });
  await insideRow.waitFor({ state: 'visible' });
  assert.ok(await insidePage.getByText('可读取正文', { exact: true }).count() >= 1);

  await login(outsidePage, outsideCredentials);
  await useAgentScope(outsidePage);
  const outsideList = await authenticatedFetch(
    outsidePage,
    `/api/enterprise/knowledge-bases?tenant_id=${tenantId}&agent_id=${agentId}`,
  );
  assert.equal(outsideList.status, 200);
  assert.equal(outsideList.body.some((item) => item.id === knowledgeBaseId), false);
  const outsideDirect = await authenticatedFetch(
    outsidePage,
    `/api/enterprise/knowledge/documents/${documentId}?tenant_id=${tenantId}&agent_id=${agentId}`,
  );
  assert.equal(outsideDirect.status, 404);
  const outsideSearch = await authenticatedFetch(outsidePage, '/api/enterprise/knowledge/search', {
    method: 'POST',
    body: JSON.stringify({
      tenant_id: tenantId,
      agent_id: agentId,
      query: '政企研发规范 安全检查',
    }),
  });
  assert.equal(outsideSearch.status, 200);
  assert.equal(JSON.stringify(outsideSearch.body).includes(knowledgeSnippet), false);
  assert.ok(
    outsideSearch.body.trace.some((item) => item.phase === 'no_accessible_knowledge'),
  );
  await outsidePage.goto(`${baseUrl}/enterprise/knowledge`);
  await outsidePage.waitForLoadState('networkidle');
  assert.equal(await outsidePage.getByText(temporaryKnowledgeName, { exact: true }).count(), 0);

  assert.deepEqual(unexpectedResponses, []);
  assert.deepEqual(browserErrors, []);
  assert.ok(observedResponses.length >= 20);
  await adminPage.screenshot({
    path: '.dev/m5b-live-knowledge-governance-only.png',
    fullPage: true,
  });
  await insidePage.screenshot({
    path: '.dev/m5b-live-knowledge-inside.png',
    fullPage: true,
  });
  console.log(JSON.stringify({
    status: 'passed',
    database: databaseLabel,
    knowledgeBaseId,
    organizationId,
    documentId,
    insideAccount: insideCredentials.username,
    outsideAccount: outsideCredentials.username,
    governorAccount: adminCredentials.username,
    observedResponseCount: observedResponses.length,
    unexpectedResponses: unexpectedResponses.length,
    browserErrors: browserErrors.length,
  }, null, 2));
} catch (error) {
  await adminPage.screenshot({
    path: '.dev/m5b-live-knowledge-regression-failure.png',
    fullPage: true,
  });
  throw error;
} finally {
  if (knowledgeBaseId) {
    const cleaned = await authenticatedFetch(
      adminPage,
      `/api/enterprise/knowledge-bases/${knowledgeBaseId}?tenant_id=${tenantId}`,
      { method: 'DELETE' },
    );
    assert.equal(cleaned.status, 200);
  }
  if (assignmentId) {
    const ended = await authenticatedFetch(
      adminPage,
      `/api/organization/member-org-assignments/${assignmentId}/end`,
      {
        method: 'POST',
        body: JSON.stringify({ tenant_id: tenantId }),
      },
    );
    assert.equal(ended.status, 200);
  }
  if (organizationId) {
    const deactivated = await authenticatedFetch(
      adminPage,
      `/api/organization/units/${organizationId}?tenant_id=${tenantId}`,
      { method: 'DELETE' },
    );
    assert.equal(deactivated.status, 200);
  }
  await browser.close();
}

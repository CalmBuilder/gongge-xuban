/**
 * @Time       : 2026/08/02 15:40
 * @Author     : zhanglp8181
 * @File       : live_sop_dependency_coverage_browser_regression.mjs
 * @CallChain  : ./app.sh:5137 + MySQL → Chromium 双账号 → SOP 依赖覆盖/迁移预检 API
 * @Description: 真实浏览器验证全量覆盖与迁移预检共用判定、动态组织缺口和治理权限。
 */

import assert from 'node:assert/strict';

import { chromium } from '../frontend-enterprise/node_modules/playwright/index.mjs';

const baseUrl = process.env.BROWSER_TEST_BASE_URL || 'http://127.0.0.1:5137';
const tenantId = process.env.BROWSER_TEST_TENANT_ID || 'tenant_demo';
const adminCredentials = {
  username: process.env.BROWSER_TEST_ADMIN_USERNAME || 'admin',
  password: process.env.BROWSER_TEST_ADMIN_PASSWORD || 'admin',
};
const ordinaryCredentials = {
  username: process.env.BROWSER_TEST_ORDINARY_USERNAME || 'approver_demo',
  password: process.env.BROWSER_TEST_ORDINARY_PASSWORD || 'demo',
};
const browserErrors = [];
const badResponses = [];
const observedResponses = [];
const browser = await chromium.launch({ headless: true });
const adminContext = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
const ordinaryContext = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const adminPage = await adminContext.newPage();
const ordinaryPage = await ordinaryContext.newPage();

function observe(page, actor) {
  page.on('pageerror', (error) => browserErrors.push(`${actor} pageerror: ${error.message}`));
  page.on('console', (message) => {
    if (message.type() === 'error' && !message.text().includes('403 (Forbidden)')) {
      browserErrors.push(`${actor} console: ${message.text()}`);
    }
  });
  page.on('response', (response) => {
    const url = new URL(response.url());
    if (!url.pathname.startsWith('/api/sop-migrations')) return;
    const record = `${actor} ${response.status()} ${response.request().method()} ${url.pathname}`;
    observedResponses.push(record);
    if (response.status() >= 500) badResponses.push(record);
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
  await page.getByRole('button', { name: '开放广场平台' }).waitFor();
}

async function authenticatedFetch(page, path) {
  return page.evaluate(async (requestPath) => {
    const raw = localStorage.getItem('gongge_auth');
    if (!raw) throw new Error('浏览器认证会话不存在');
    const response = await fetch(requestPath, {
      headers: { Authorization: `Bearer ${JSON.parse(raw).token}` },
    });
    return { status: response.status, body: await response.json() };
  }, path);
}

observe(adminPage, 'administrator');
observe(ordinaryPage, 'ordinary-member');

try {
  await login(adminPage, adminCredentials);
  const coveragePath = `/api/sop-migrations/coverage?tenant_id=${tenantId}`;
  const previewPath = `/api/sop-migrations/preview?tenant_id=${tenantId}`;
  const coverage = await authenticatedFetch(adminPage, coveragePath);
  const preview = await authenticatedFetch(adminPage, previewPath);

  assert.equal(coverage.status, 200);
  assert.equal(preview.status, 200);
  assert.equal(coverage.body.total, 22);
  assert.equal(coverage.body.entries.length, coverage.body.total);
  assert.deepEqual(coverage.body.readiness_counts, preview.body.dependency_counts);
  assert.deepEqual(coverage.body.readiness_counts, {
    ready: 22,
    attention_required: 0,
    blocked: 0,
  });
  const previewBySkill = new Map(
    preview.body.entries.map((entry) => [entry.skill_id, entry.dependency_assessment]),
  );
  for (const entry of coverage.body.entries) {
    assert.equal(entry.requester_policy, 'active_tenant_member');
    assert.equal(entry.requester_policy_explicit, false);
    assert.deepEqual(entry.dependency_assessment, previewBySkill.get(entry.skill_id));
  }

  const expense = coverage.body.entries.find(
    (entry) => entry.skill_id === 'expense_over_limit_approval',
  );
  assert.ok(expense, '覆盖报告必须包含超标费用审批');
  assert.equal(expense.dependency_assessment.readiness, 'ready');
  assert.deepEqual(expense.dependency_assessment.issue_codes, []);
  const departmentApproval = expense.dependency_assessment.human_participants.find(
    (participant) => participant.node_id === 'department_special_approval',
  );
  assert.ok(departmentApproval, '必须返回部门特批人工节点覆盖明细');
  assert.equal(departmentApproval.context_count, 5);
  assert.equal(departmentApproval.covered_context_count, 5);
  assert.equal(departmentApproval.uncovered_org_unit_ids.length, 0);
  assert.equal(departmentApproval.exclude_initiator, true);

  await login(ordinaryPage, ordinaryCredentials);
  const forbidden = await authenticatedFetch(ordinaryPage, coveragePath);
  assert.equal(forbidden.status, 403);

  assert.deepEqual(badResponses, []);
  assert.deepEqual(browserErrors, []);
  console.log(JSON.stringify({
    total: coverage.body.total,
    readiness: coverage.body.readiness_counts,
    expenseContexts: {
      total: departmentApproval.context_count,
      covered: departmentApproval.covered_context_count,
      uncovered: departmentApproval.uncovered_org_unit_ids.length,
    },
    ordinaryStatus: forbidden.status,
    observedResponses,
    badResponses,
    browserErrors,
  }, null, 2));
} finally {
  await browser.close();
}

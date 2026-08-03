/**
 * @Time       : 2026/07/28 22:00
 * @Author     : zhanglp8181
 * @File       : live_m4b_browser_regression.mjs
 * @CallChain  : 5137 实际 MySQL → Chromium 双账号 → M4 关系、会话锚点与管理页验收
 * @Description: 验证五种关系视图、使用移除、历史会话、能力快照和发布治理 UI，并恢复测试前使用关系。
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
let createdSessionId = '';
let selectedAgentId = '';
let usageExisted = false;

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
    const path = new URL(response.url()).pathname;
    if (path.startsWith('/api/enterprise/agents') || path.startsWith('/api/chat/')) {
      if (response.status() === 404 || response.status() >= 500) {
        contractFailures.push(`${actor} ${response.status()} ${path}`);
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

async function memberContract(page) {
  return page.evaluate(async (currentTenantId) => {
    const raw = localStorage.getItem('gongge_auth');
    if (!raw) throw new Error('浏览器认证会话不存在');
    const token = JSON.parse(raw).token;
    const headers = {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
    };
    const readScope = async (scope) => {
      const response = await fetch(
        `/api/enterprise/agents?tenant_id=${currentTenantId}&scope=${scope}`,
        { headers },
      );
      return { status: response.status, rows: await response.json() };
    };
    const scopePairs = await Promise.all(
      ['manageable', 'owned', 'used', 'gallery', 'expert'].map(
        async (scope) => [scope, await readScope(scope)],
      ),
    );
    const scopes = Object.fromEntries(scopePairs);
    const selected = scopes.gallery.rows.find(
      (item) => !item.is_overall && item.owned_by_current_user !== true,
    );
    if (!selected) throw new Error('实际库没有可用于 M4 验收的广场员工');
    const usageExistedBefore = scopes.used.rows.some((item) => item.id === selected.id);
    const useResponse = await fetch(
      `/api/chat/agents/${selected.id}/use?tenant_id=${currentTenantId}`,
      { method: 'POST', headers, body: '{}' },
    );
    const sessionResponse = await fetch('/api/chat/sessions', {
      method: 'POST',
      headers,
      body: JSON.stringify({
        tenant_id: currentTenantId,
        agent_id: selected.id,
        origin: 'gallery',
      }),
    });
    const session = await sessionResponse.json();
    const removeResponse = await fetch(
      `/api/chat/agents/${selected.id}/use?tenant_id=${currentTenantId}`,
      { method: 'DELETE', headers },
    );
    const history = await fetch(`/api/chat/sessions?tenant_id=${currentTenantId}`, {
      headers,
    }).then((response) => response.json());
    const rejectedResponse = await fetch('/api/chat/sessions', {
      method: 'POST',
      headers,
      body: JSON.stringify({
        tenant_id: currentTenantId,
        agent_id: selected.id,
        origin: 'gallery',
      }),
    });
    return {
      scopes,
      selected,
      usageExistedBefore,
      useStatus: useResponse.status,
      sessionStatus: sessionResponse.status,
      session,
      removeStatus: removeResponse.status,
      historyPreserved: history.some((item) => item.id === session.id),
      rejectedStatus: rejectedResponse.status,
    };
  }, tenantId);
}

async function cleanupMemberState(page) {
  if (!selectedAgentId) return;
  await page.evaluate(async ({ currentTenantId, agentId, sessionId, restoreUsage }) => {
    const raw = localStorage.getItem('gongge_auth');
    if (!raw) return;
    const token = JSON.parse(raw).token;
    const headers = {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
    };
    if (sessionId) {
      await fetch(`/api/chat/sessions/${sessionId}?tenant_id=${currentTenantId}`, {
        method: 'DELETE',
        headers,
      });
    }
    if (restoreUsage) {
      await fetch(`/api/chat/agents/${agentId}/use?tenant_id=${currentTenantId}`, {
        method: 'POST',
        headers,
        body: '{}',
      });
    } else {
      await fetch(`/api/chat/agents/${agentId}/use?tenant_id=${currentTenantId}`, {
        method: 'DELETE',
        headers,
      });
    }
  }, {
    currentTenantId: tenantId,
    agentId: selectedAgentId,
    sessionId: createdSessionId,
    restoreUsage: usageExisted,
  });
}

observe(adminPage, 'admin');
observe(memberPage, 'member');

try {
  await login(memberPage, memberUsername, memberPassword);
  const result = await memberContract(memberPage);
  selectedAgentId = result.selected.id;
  createdSessionId = result.session.id;
  usageExisted = result.usageExistedBefore;

  assert.deepEqual(
    Object.values(result.scopes).map((scope) => scope.status),
    [200, 200, 200, 200, 200],
  );
  assert.equal(result.useStatus, 200);
  assert.equal(result.sessionStatus, 200);
  assert.equal(result.removeStatus, 200);
  assert.equal(result.historyPreserved, true);
  assert.equal(result.rejectedStatus, 403);
  assert.equal(result.session.origin, 'gallery');
  assert.ok(Number.isInteger(result.session.agent_profile_revision));
  assert.equal(result.session.capability_snapshot.agent_id, result.selected.id);
  assert.doesNotMatch(
    JSON.stringify(result.session.capability_snapshot),
    /authorization|token|secret|header/i,
  );

  await memberPage.goto(`${baseUrl}/workspace/gallery`);
  await memberPage.getByRole('tab', { name: '数字员工广场' }).click();
  const selectedCard = memberPage
    .locator('.gongge-employee-card')
    .filter({ hasText: result.selected.name })
    .first();
  await selectedCard.waitFor({
    state: 'visible',
  });
  await selectedCard.getByRole('button', { name: '添加到常用' }).waitFor({
    state: 'visible',
  });

  await login(adminPage, adminUsername, adminPassword);
  await adminPage.goto(`${baseUrl}/enterprise/agents?view=governance`);
  await adminPage.getByRole('heading', { name: '发布治理' }).waitFor({
    state: 'visible',
  });
  assert.equal(await adminPage.getByText('Not Found', { exact: true }).count(), 0);

  assert.deepEqual(contractFailures, []);
  assert.deepEqual(browserErrors, []);
  await memberPage.screenshot({
    path: '.dev/m4b-live-agent-relationships.png',
    fullPage: true,
  });
  console.log(JSON.stringify({
    status: 'passed',
    database: 'mysql',
    revision: '20260728_0024',
    selectedAgent: selectedAgentId,
    scopeCounts: Object.fromEntries(
      Object.entries(result.scopes).map(([key, value]) => [key, value.rows.length]),
    ),
    sessionRevision: result.session.agent_profile_revision,
    historyPreserved: result.historyPreserved,
    newSessionRejectedAfterRemoval: result.rejectedStatus === 403,
    browserErrors: browserErrors.length,
    contractFailures: contractFailures.length,
  }, null, 2));
} finally {
  await cleanupMemberState(memberPage);
  await browser.close();
}

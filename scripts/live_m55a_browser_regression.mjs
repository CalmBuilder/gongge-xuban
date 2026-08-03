/**
 * @Time       : 2026/07/29 10:18
 * @Author     : zhanglp8181
 * @File       : live_m55a_browser_regression.mjs
 * @CallChain  : 生产前端/FastAPI → Chromium 双账号 → SOP 迁移预检页面/API
 * @Description: 验证全量发布头、业务依赖、历史版本、分支同步、当前岗位责任 UI 和治理权限。
 */

import assert from 'node:assert/strict';

import { chromium } from '../frontend-enterprise/node_modules/playwright/index.mjs';

const baseUrl = process.env.BROWSER_TEST_BASE_URL || 'http://127.0.0.1:5137';
const databaseLabel = process.env.BROWSER_TEST_DATABASE || 'mysql';
const phaseLabel = process.env.BROWSER_TEST_PHASE || 'm55a';
const tenantId = process.env.BROWSER_TEST_TENANT_ID || 'tenant_demo';
const expectedTotal = Number(process.env.BROWSER_TEST_EXPECTED_SOP_TOTAL || 0);
const expectedUnsupported = optionalExpected('BROWSER_TEST_EXPECTED_UNSUPPORTED');
const expectedBusinessConfirmation = optionalExpected(
  'BROWSER_TEST_EXPECTED_BUSINESS_CONFIRMATION',
);
const expectedDependencyBlocked = optionalExpected(
  'BROWSER_TEST_EXPECTED_DEPENDENCY_BLOCKED',
);
const expectedDependencyReady = optionalExpected(
  'BROWSER_TEST_EXPECTED_DEPENDENCY_READY',
);
const expectedDependencyAttention = optionalExpected(
  'BROWSER_TEST_EXPECTED_DEPENDENCY_ATTENTION',
);
const requireCompleteSnapshots =
  process.env.BROWSER_TEST_REQUIRE_COMPLETE_SNAPSHOTS === 'true';
const requireDerivedHeads =
  process.env.BROWSER_TEST_REQUIRE_DERIVED_HEADS === 'true';
const requireSyncedBranches =
  process.env.BROWSER_TEST_REQUIRE_SYNCED_BRANCHES === 'true';
const expectedSyncedBranchCount = optionalExpected(
  'BROWSER_TEST_EXPECTED_SYNCED_BRANCH_COUNT',
);
const expectedWebSearchVersion = process.env.BROWSER_TEST_EXPECTED_WEB_SEARCH_VERSION || '';
const requireOpenGalleryPoolSemantics =
  process.env.BROWSER_TEST_REQUIRE_OPEN_GALLERY_POOL_SEMANTICS === 'true';
const adminCredentials = {
  username: process.env.BROWSER_TEST_ADMIN_USERNAME || 'admin',
  password: process.env.BROWSER_TEST_ADMIN_PASSWORD || 'admin',
};
const ordinaryCredentials = {
  username: process.env.BROWSER_TEST_ORDINARY_USERNAME || 'approver_demo',
  password: process.env.BROWSER_TEST_ORDINARY_PASSWORD || 'demo',
};

function optionalExpected(name) {
  if (process.env[name] === undefined) return null;
  const value = Number(process.env[name]);
  if (!Number.isInteger(value) || value < 0) {
    throw new Error(`${name} 必须是非负整数`);
  }
  return value;
}

const browserErrors = [];
const unexpectedResponses = [];
const observedResponses = [];
const browser = await chromium.launch({ headless: true });
const adminContext = await browser.newContext({ viewport: { width: 1600, height: 1050 } });
const ordinaryContext = await browser.newContext({ viewport: { width: 1440, height: 960 } });
const adminPage = await adminContext.newPage();
const ordinaryPage = await ordinaryContext.newPage();

function observe(page, actor) {
  page.on('pageerror', (error) => browserErrors.push(`${actor} pageerror: ${error.message}`));
  page.on('console', (message) => {
    if (
      message.type() === 'error'
      && !message.text().includes('403 (Forbidden)')
    ) {
      browserErrors.push(`${actor} console: ${message.text()}`);
    }
  });
  page.on('response', (response) => {
    const url = new URL(response.url());
    if (url.pathname.startsWith('/api/sop-migrations')) {
      const record = `${actor} ${response.status()} ${response.request().method()} ${url.pathname}`;
      observedResponses.push(record);
      if (response.status() >= 500) {
        unexpectedResponses.push(record);
      }
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
  await page.getByRole('button', { name: '开放广场平台' }).waitFor({ state: 'visible' });
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
observe(ordinaryPage, 'ordinary-member');

try {
  await login(adminPage, adminCredentials);

  const previewPath = `/api/sop-migrations/preview?tenant_id=${tenantId}`;
  const preview = await authenticatedFetch(adminPage, previewPath);
  assert.equal(preview.status, 200);
  assert.equal(preview.body.total, preview.body.entries.length);
  assert.equal(
    Object.values(preview.body.disposition_counts).reduce((sum, count) => sum + count, 0),
    preview.body.total,
  );
  assert.ok(preview.body.disposition_counts.no_migration > 0);
  assert.equal(
    Object.values(preview.body.dependency_counts).reduce((sum, count) => sum + count, 0),
    preview.body.total,
  );
  assert.ok(
    preview.body.entries.every(
      (entry) => entry.dependency_assessment
        && entry.dependency_assessment.executable_agent_count
          <= entry.dependency_assessment.bound_agent_count,
    ),
  );
  if (expectedTotal) assert.equal(preview.body.total, expectedTotal);
  if (expectedUnsupported !== null) {
    assert.equal(
      preview.body.disposition_counts.temporarily_unsupported,
      expectedUnsupported,
    );
  }
  if (expectedBusinessConfirmation !== null) {
    assert.equal(
      preview.body.disposition_counts.business_confirmation,
      expectedBusinessConfirmation,
    );
  }
  if (expectedDependencyBlocked !== null) {
    assert.equal(
      preview.body.dependency_counts.blocked,
      expectedDependencyBlocked,
    );
  }
  if (expectedDependencyReady !== null) {
    assert.equal(preview.body.dependency_counts.ready, expectedDependencyReady);
  }
  if (expectedDependencyAttention !== null) {
    assert.equal(
      preview.body.dependency_counts.attention_required,
      expectedDependencyAttention,
    );
  }
  if (expectedWebSearchVersion) {
    const webSearch = preview.body.entries.find((entry) => entry.skill_id === 'web_search');
    assert.ok(webSearch, '全量迁移预检应包含联网信息查询');
    assert.equal(webSearch.current_version, expectedWebSearchVersion);
    assert.equal(webSearch.disposition, 'no_migration');
    assert.ok(webSearch.derived_from_version_id, '联网信息查询当前头应保留派生来源');
  }
  if (requireCompleteSnapshots) {
    assert.ok(preview.body.entries.every((entry) => entry.current_version_id));
    assert.ok(
      preview.body.entries.every(
        (entry) => entry.reason_code !== 'CURRENT_PUBLISHED_SNAPSHOT_MISSING',
      ),
    );
  }
  if (requireDerivedHeads) {
    for (const entry of preview.body.entries) {
      assert.ok(
        entry.derived_from_version_id,
        `${entry.skill_id}@${entry.current_version} 缺少派生来源`,
      );
    }
  }
  if (requireSyncedBranches) {
    const agents = await authenticatedFetch(
      adminPage,
      `/api/enterprise/agents?tenant_id=${tenantId}`,
    );
    assert.equal(agents.status, 200);
    const globalVersions = new Map(
      preview.body.entries.map((entry) => [entry.skill_id, entry.current_version]),
    );
    let syncedBranchCount = 0;
    for (const agent of agents.body) {
      if (agent.status !== 'active' || agent.is_overall) continue;
      const skills = await authenticatedFetch(
        adminPage,
        `/api/enterprise/agents/${encodeURIComponent(agent.id)}/skills`
          + `?tenant_id=${tenantId}`,
      );
      assert.equal(skills.status, 200);
      for (const skill of skills.body) {
        if (
          skill.status !== 'published'
          || skill.branch_status !== 'active'
          || skill.branch_sync_state !== 'synced'
          || !globalVersions.has(skill.skill_id)
        ) {
          continue;
        }
        syncedBranchCount += 1;
        assert.equal(
          skill.branch_head_version,
          globalVersions.get(skill.skill_id),
          `${agent.name}/${skill.skill_id} 未跟随当前发布头`,
        );
        assert.equal(skill.version, skill.branch_head_version);
      }
    }
    if (expectedSyncedBranchCount !== null) {
      assert.equal(syncedBranchCount, expectedSyncedBranchCount);
    }
  }
  if (requireOpenGalleryPoolSemantics) {
    const agents = await authenticatedFetch(
      adminPage,
      `/api/enterprise/agents?tenant_id=${tenantId}`,
    );
    assert.equal(agents.status, 200);
    const pool = agents.body.find((agent) => agent.is_overall);
    assert.ok(pool, '缺少开放广场资源池');
    assert.equal(pool.name, '开放广场资源池');
    assert.equal(pool.owner_user_id, null);
    for (const name of ['购物售后助手', '平台能力演示助手']) {
      const agent = agents.body.find((item) => item.name === name);
      assert.ok(agent, `缺少真实演示数字员工：${name}`);
      assert.equal(agent.is_overall, false);
      assert.equal(agent.status, 'active');
      assert.equal(agent.published_to_gallery, true);
    }

    await adminPage.goto(`${baseUrl}/enterprise/organization-roles?section=agents`);
    await adminPage.getByRole('heading', { name: '数字员工映射' }).waitFor({
      state: 'visible',
    });
    await adminPage.getByRole('button', { name: '绑定业务角色' }).click();
    await adminPage.getByRole('combobox').first().click();
    assert.equal(
      await adminPage.getByRole('option', { name: '开放广场资源池' }).count(),
      0,
    );
    assert.equal(
      await adminPage.getByRole('option', { name: '购物售后助手' }).count(),
      1,
    );
    await adminPage.keyboard.press('Escape');
  }

  await adminPage.goto(`${baseUrl}/enterprise/organization`);
  await adminPage.getByRole('tree', { name: '企业组织树' }).waitFor();
  await adminPage.getByLabel('搜索组织').fill('财务部');
  await adminPage.getByRole('button', { name: /财务部/ }).first().click();
  await adminPage.getByRole('button', { name: /财务部门经理/ }).click();
  const impactRail = adminPage.getByRole('region', { name: '岗位流程责任影响' });
  await impactRail.waitFor();
  await impactRail.getByText('责任闭环轨道', { exact: true }).waitFor();

  await login(ordinaryPage, ordinaryCredentials);
  const denied = await authenticatedFetch(ordinaryPage, previewPath);
  assert.equal(denied.status, 403);

  assert.deepEqual(unexpectedResponses, []);
  assert.deepEqual(browserErrors, []);
  assert.ok(observedResponses.length >= 2);
  await adminPage.screenshot({
    path: `.dev/${phaseLabel}-${databaseLabel}-sop-migration.png`,
    fullPage: true,
  });
  console.log(JSON.stringify({
    status: 'passed',
    database: databaseLabel,
    total: preview.body.total,
    dispositionCounts: preview.body.disposition_counts,
    dependencyCounts: preview.body.dependency_counts,
    activeInstances: preview.body.active_instance_count,
    activeHistoricalInstances: preview.body.active_historical_instance_count,
    activeWorkItems: preview.body.active_work_item_count,
    observedResponseCount: observedResponses.length,
    unexpectedResponses: unexpectedResponses.length,
    browserErrors: browserErrors.length,
  }, null, 2));
} catch (error) {
  await adminPage.screenshot({
    path: `.dev/${phaseLabel}-${databaseLabel}-regression-failure.png`,
    fullPage: true,
  });
  throw error;
} finally {
  await browser.close();
}

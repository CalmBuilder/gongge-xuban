/**
 * @Time       : 2026/07/28 18:45
 * @Author     : zhanglp8181
 * @File       : live_m25b_browser_regression.mjs
 * @CallChain  : 5137 实际部署 → Chromium 双账号 → 大组织查询契约与权限边界
 * @Description: 只读验证 M2.5-B 实际 MySQL、懒加载分页、局部页面和运行版本一致性。
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
const organizationRequests = [];

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

observe(adminPage, 'admin');
observe(memberPage, 'member');
adminPage.on('response', (response) => {
  const url = new URL(response.url());
  if (url.pathname.startsWith('/api/organization/') || url.pathname.startsWith('/api/auth/users')) {
    organizationRequests.push(`${response.request().method()} ${url.pathname}${url.search}`);
    if (response.status() === 404 || response.status() >= 500) {
      contractFailures.push(`${response.status()} ${url.pathname}`);
    }
  }
});

try {
  await login(adminPage, adminUsername, adminPassword);
  await adminPage.goto(`${baseUrl}/enterprise/organization`);
  const tree = adminPage.getByRole('tree', { name: '企业组织树' });
  await tree.waitFor({ state: 'visible' });
  const root = tree.getByRole('treeitem').first();
  const rootName = (await root.innerText()).trim();
  await root.click();
  await adminPage.getByText(/直属 .* 人 · 子树 .* 人/).waitFor({ state: 'visible' });

  const search = adminPage.getByRole('textbox', { name: '搜索组织' });
  await search.fill(rootName);
  await adminPage.getByRole('button', { name: new RegExp(rootName) }).first().click();
  await tree.waitFor({ state: 'visible' });

  await adminPage.goto(`${baseUrl}/enterprise/accounts`);
  await adminPage.getByRole('table', { name: '成员列表' }).waitFor({ state: 'visible' });
  assert.ok(
    await adminPage.getByRole('table', { name: '成员列表' }).locator('tbody tr').count() <= 10,
    '成员页首屏必须遵守服务端页大小',
  );
  await adminPage.getByPlaceholder('搜索成员、账号、工号、状态或类别').fill(adminUsername);
  await adminPage.getByRole('table', { name: '成员列表' }).getByText(adminUsername, {
    exact: true,
  }).waitFor({ state: 'visible' });
  await adminPage.getByPlaceholder('搜索成员、账号、工号、状态或类别').fill('');
  assert.equal(
    await adminPage.getByRole('checkbox', { name: '包含下级组织成员' }).isChecked(),
    false,
    '成员页必须默认仅显示所选组织直属成员',
  );

  const roots = await authenticatedFetch(
    adminPage,
    `/api/organization/unit-children?tenant_id=${tenantId}`,
  );
  assert.equal(roots.status, 200);
  assert.equal(roots.body.length, 1);
  const directChildren = await authenticatedFetch(
    adminPage,
    `/api/organization/unit-children?tenant_id=${tenantId}`
      + `&parent_id=${encodeURIComponent(roots.body[0].id)}`,
  );
  assert.equal(directChildren.status, 200);
  assert.ok(directChildren.body.length > 0);
  assert.equal(
    directChildren.body.some((unit) => /^M5-[BC] /.test(unit.name) || unit.status !== 'active'),
    false,
    '正常组织树不得出现 M5 回归节点或停用节点',
  );
  let scopedUnit = null;
  let scopedSummary = null;
  for (const unit of directChildren.body) {
    const summary = await authenticatedFetch(
      adminPage,
      `/api/organization/unit-summary?tenant_id=${tenantId}`
        + `&org_unit_id=${encodeURIComponent(unit.id)}`,
    );
    assert.equal(summary.status, 200);
    if (summary.body.subtree_member_count > summary.body.direct_member_count) {
      scopedUnit = unit;
      scopedSummary = summary.body;
      break;
    }
  }
  assert.ok(scopedUnit, '实际组织数据应包含直属人数少于子树人数的层级节点');
  await adminPage.getByRole('textbox', { name: '搜索组织' }).fill(scopedUnit.name);
  const directMemberResponse = adminPage.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === '/api/auth/users/page'
      && url.searchParams.get('org_unit_id') === scopedUnit.id
      && url.searchParams.get('include_descendants') === 'false';
  });
  await adminPage.getByRole('button', { name: new RegExp(scopedUnit.name) }).first().click();
  const directMemberBody = await (await directMemberResponse).json();
  assert.equal(
    directMemberBody.total,
    scopedSummary.direct_member_count,
    '右侧成员列表必须采用所选组织直属人数口径',
  );
  assert.ok(
    scopedSummary.subtree_member_count > directMemberBody.total,
    '测试组织必须能证明后代成员没有混入直属列表',
  );
  const descendantMemberResponse = adminPage.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === '/api/auth/users/page'
      && url.searchParams.get('org_unit_id') === scopedUnit.id
      && url.searchParams.get('include_descendants') === 'true';
  });
  await adminPage.getByRole('checkbox', { name: '包含下级组织成员' }).click();
  const descendantMemberBody = await (await descendantMemberResponse).json();
  assert.equal(
    descendantMemberBody.total,
    scopedSummary.subtree_member_count,
    '用户主动勾选后才应切换为组织子树成员口径',
  );
  const restoredDirectResponse = adminPage.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === '/api/auth/users/page'
      && url.searchParams.get('org_unit_id') === scopedUnit.id
      && url.searchParams.get('include_descendants') === 'false';
  });
  await adminPage.getByRole('checkbox', { name: '包含下级组织成员' }).click();
  await restoredDirectResponse;

  await login(memberPage, memberUsername, memberPassword);
  const memberEnumerationStatus = await memberPage.evaluate(async (currentTenantId) => {
    const raw = localStorage.getItem('gongge_auth');
    if (!raw) throw new Error('普通成员会话不存在');
    const response = await fetch(
      `/api/auth/users/page?tenant_id=${currentTenantId}&page=1&page_size=10`,
      { headers: { Authorization: `Bearer ${JSON.parse(raw).token}` } },
    );
    return response.status;
  }, tenantId);
  assert.equal(memberEnumerationStatus, 403);

  assert.equal(
    organizationRequests.some((request) => (
      request.startsWith('GET /api/organization/units?')
      || request.startsWith('GET /api/auth/users?')
    )),
    false,
    '新管理页不得回退到旧全量组织或成员接口',
  );
  assert.ok(organizationRequests.some((request) => request.includes('/unit-children?')));
  assert.ok(organizationRequests.some((request) => request.includes('/unit-search?')));
  assert.ok(organizationRequests.some((request) => request.includes('/api/auth/users/page?')));
  assert.deepEqual(contractFailures, []);
  assert.deepEqual(browserErrors, []);
  assert.equal(await adminPage.getByText('Not Found', { exact: true }).count(), 0);

  await adminPage.screenshot({
    path: '.dev/m25b-live-large-organization-regression.png',
    fullPage: true,
  });
  console.log(JSON.stringify({
    status: 'passed',
    database: 'mysql',
    requestCount: organizationRequests.length,
    contractFailures: contractFailures.length,
    browserErrors: browserErrors.length,
    memberEnumerationStatus,
    scopedOrganization: scopedUnit.name,
    directMemberCount: directMemberBody.total,
    subtreeMemberCount: scopedSummary.subtree_member_count,
    descendantOptInCount: descendantMemberBody.total,
  }, null, 2));
} catch (error) {
  await adminPage.screenshot({
    path: '.dev/m25b-live-large-organization-regression-failure.png',
    fullPage: true,
  });
  throw error;
} finally {
  await browser.close();
}

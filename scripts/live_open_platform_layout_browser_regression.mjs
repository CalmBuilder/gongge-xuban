/**
 * @Time       : 2026/08/01 23:58
 * @Author     : zhanglp8181
 * @File       : live_open_platform_layout_browser_regression.mjs
 * @CallChain  : ./app.sh:5137 + MySQL → Chromium 管理员 → 开放广场/会话端/管理端员工卡片
 * @Description: 真实浏览器验证开放广场单分类交互，并核对三处员工卡片使用一致的布局规格。
 */

import assert from 'node:assert/strict';

import { chromium } from '../frontend-enterprise/node_modules/playwright/index.mjs';

const baseUrl = process.env.BROWSER_TEST_BASE_URL || 'http://127.0.0.1:5137';
const username = process.env.BROWSER_TEST_ADMIN_USERNAME || 'admin';
const password = process.env.BROWSER_TEST_ADMIN_PASSWORD || 'admin';
const screenshotPath = process.env.BROWSER_TEST_SCREENSHOT
  || '/tmp/gongge-open-platform-layout-regression.png';
const resourceScreenshotPath = screenshotPath.replace(/\.png$/, '-resource.png');
const browserErrors = [];
const badResponses = [];
const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1920, height: 1080 } });
const page = await context.newPage();

page.on('pageerror', (error) => browserErrors.push(`pageerror: ${error.message}`));
page.on('console', (message) => {
  if (message.type() === 'error') browserErrors.push(`console: ${message.text()}`);
});
page.on('response', (response) => {
  const url = new URL(response.url());
  if (url.pathname.startsWith('/api/') && response.status() >= 500) {
    badResponses.push(`${response.status()} ${url.pathname}${url.search}`);
  }
});

/** 登录真实统一应用，并关闭首次使用引导，避免遮挡被测交互。 */
async function login() {
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

/** 读取首张员工卡片及其网格的浏览器实际布局指标。 */
async function readEmployeeLayout(pageName) {
  const card = page.locator('.gongge-employee-card').first();
  await card.waitFor();
  const metrics = await card.evaluate((element) => {
    const cardStyle = getComputedStyle(element);
    const grid = element.parentElement;
    const gridStyle = grid ? getComputedStyle(grid) : null;
    const identity = element.querySelector('.gongge-employee-identity');
    const identityRect = identity?.getBoundingClientRect();
    const rect = element.getBoundingClientRect();
    return {
      cardWidth: Math.round(rect.width),
      cardHeight: Math.round(rect.height),
      borderRadius: cardStyle.borderRadius,
      columns: gridStyle?.gridTemplateColumns.split(' ').filter(Boolean).length || 0,
      columnGap: gridStyle?.columnGap || '',
      identityHeight: identityRect ? Math.round(identityRect.height) : 0,
    };
  });
  assert.equal(metrics.columns, 4, `${pageName} 在 1920px 下应使用四列员工卡片网格`);
  assert.equal(metrics.columnGap, '32px', `${pageName} 员工卡片间距应为 32px`);
  assert.equal(metrics.borderRadius, '14px', `${pageName} 员工卡片圆角应为 14px`);
  assert.equal(metrics.identityHeight, 72, `${pageName} 员工身份区高度应为 72px`);
  assert.ok(metrics.cardHeight >= 262, `${pageName} 员工卡片高度不得小于 262px`);
  return metrics;
}

/** 读取当前资源分类首张卡片的实际尺寸和网格规格。 */
async function readResourceLayout(pageName) {
  const card = page.locator('.gongge-platform-resource-card').first();
  await card.waitFor();
  const metrics = await card.evaluate((element) => {
    const rect = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    const gridStyle = element.parentElement ? getComputedStyle(element.parentElement) : null;
    return {
      cardWidth: Math.round(rect.width),
      cardHeight: Math.round(rect.height),
      borderRadius: style.borderRadius,
      columns: gridStyle?.gridTemplateColumns.split(' ').filter(Boolean).length || 0,
      columnGap: gridStyle?.columnGap || '',
      artworkSize: Math.round(
        element.querySelector('[data-plaza-resource-artwork]')?.getBoundingClientRect().width || 0,
      ),
    };
  });
  assert.equal(metrics.cardHeight, 292, `${pageName} 卡片高度应统一为 292px`);
  assert.equal(metrics.columns, 4, `${pageName} 在 1920px 下应使用四列网格`);
  assert.equal(metrics.columnGap, '32px', `${pageName} 卡片间距应为 32px`);
  assert.equal(metrics.borderRadius, '14px', `${pageName} 卡片圆角应为 14px`);
  assert.equal(metrics.artworkSize, 80, `${pageName} 卡片应使用统一的 80px 大图标`);
  return metrics;
}

try {
  await login();

  await page.goto(`${baseUrl}/enterprise/platform`);
  const platformTabs = page.getByRole('tablist', { name: '广场资源类型' });
  await platformTabs.waitFor();
  assert.equal(await platformTabs.getByRole('tab', { name: '全部', exact: true }).count(), 0);
  await page.getByText('数字员工广场', { exact: true }).last().waitFor();
  assert.equal(
    await platformTabs.getByRole('tab', { name: /数字员工/ }).getAttribute('aria-selected'),
    'true',
    '开放广场根路由应默认显示数字员工分类',
  );
  assert.equal(await page.getByText('查看全部', { exact: true }).count(), 0);
  const platform = await readEmployeeLayout('开放广场');
  assert.equal(platform.cardHeight, 292, '数字员工分类卡片高度应统一为 292px');
  const employeePaginator = page.getByRole('navigation', { name: '数字员工广场分页' });
  await employeePaginator.waitFor();

  const employeeSearch = page.getByPlaceholder('搜索员工');
  await employeeSearch.fill('__browser_no_match__');
  await page.getByText('没有匹配的广场内容', { exact: true }).waitFor();
  await employeeSearch.fill('');
  await page.locator('.gongge-employee-card').first().waitFor();

  const resourceCategories = [
    { tab: /知识库/, path: 'knowledge', name: '知识库', title: '知识库广场', hasNext: true },
    { tab: /技能/, path: 'general-skills', name: '技能', title: '技能广场', hasNext: false },
    { tab: /^SOP/, path: 'skills', name: 'SOP', title: 'SOP 广场', hasNext: true },
    { tab: /工具/, path: 'tools', name: '工具', title: '工具广场', hasNext: true },
  ];
  const resourceLayouts = {};
  for (const category of resourceCategories) {
    await platformTabs.getByRole('tab', { name: category.tab }).click();
    await page.waitForURL(`**/enterprise/platform/${category.path}`);
    resourceLayouts[category.path] = await readResourceLayout(category.name);
    if (category.path === 'knowledge') {
      await page.screenshot({ path: resourceScreenshotPath, fullPage: true });
    }
    assert.equal(await page.locator('.gongge-employee-card').count(), 0);
    assert.ok(await page.getByPlaceholder('搜索内容').count() === 1);
    assert.equal(
      resourceLayouts[category.path].cardWidth,
      platform.cardWidth,
      `${category.name} 与数字员工卡片宽度应一致`,
    );
    const paginator = page.getByRole('navigation', { name: `${category.title}分页` });
    await paginator.waitFor();
    const next = paginator.getByRole('button', { name: '下一页' });
    assert.equal(
      await next.isDisabled(),
      !category.hasNext,
      `${category.name} 下一页状态应与真实数据总量一致`,
    );
    if (category.hasNext) {
      await next.click();
      await paginator.locator('[aria-current="page"]').filter({ hasText: '02' }).waitFor();
    }
    assert.ok(
      await page.locator('.gongge-platform-resource-card').count() <= 12,
      `${category.name} 每页最多展示 12 张卡片`,
    );
  }
  await platformTabs.getByRole('tab', { name: /数字员工/ }).click();
  await page.waitForURL('**/enterprise/platform/agents');
  await page.locator('.gongge-employee-card').first().waitFor();
  await page.screenshot({ path: screenshotPath, fullPage: true });

  await page.goto(`${baseUrl}/enterprise/agents?view=all`);
  await page.getByText('可管理数字员工', { exact: true }).last().waitFor();
  const management = await readEmployeeLayout('管理端');

  await page.goto(`${baseUrl}/workspace/gallery?view=discover&sub=gallery`);
  await page.getByRole('tablist', { name: '发现分类' }).waitFor();
  await page.locator('.gongge-employee-card').first().waitFor();
  const conversation = await readEmployeeLayout('会话端');

  assert.deepEqual(badResponses, []);
  assert.deepEqual(browserErrors, []);
  console.log(JSON.stringify({
    platform,
    resourceLayouts,
    management,
    conversation,
    screenshotPath,
    resourceScreenshotPath,
    badResponses,
    browserErrors,
  }, null, 2));
} finally {
  await browser.close();
}

/**
 * @Time       : 2026/07/29 19:48
 * @Author     : zhanglp8181
 * @File       : live_concept_help_browser_regression.mjs
 * @CallChain  : 5137 统一服务 → Chromium 管理员 → 六类概念说明 → 桌面/移动端布局断言
 * @Description: 只读验证真人、数字员工、专家、治理和广场关系的完整说明及响应式展示。
 */

import assert from 'node:assert/strict';

import { chromium } from '../frontend-enterprise/node_modules/playwright/index.mjs';

const baseUrl = process.env.BROWSER_TEST_BASE_URL || 'http://127.0.0.1:5137';
const username = process.env.BROWSER_TEST_ADMIN_USERNAME || 'admin';
const password = process.env.BROWSER_TEST_ADMIN_PASSWORD || 'admin';
const browserErrors = [];
const unexpectedResponses = [];

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1600, height: 1050 } });
const page = await context.newPage();

function observePage(observedPage, label) {
  observedPage.on('pageerror', (error) => browserErrors.push(`${label} pageerror: ${error.message}`));
  observedPage.on('console', (message) => {
    if (message.type() === 'error') browserErrors.push(`${label} console: ${message.text()}`);
  });
  observedPage.on('response', (response) => {
    const url = new URL(response.url());
    if (url.pathname.startsWith('/api/') && (response.status() === 404 || response.status() >= 500)) {
      unexpectedResponses.push(`${label} ${response.status()} ${url.pathname}`);
    }
  });
}

async function login(targetPage) {
  await targetPage.goto(`${baseUrl}/enterprise/dashboard`);
  await targetPage.evaluate(() => {
    localStorage.setItem('gongge_onboarding_guide_seen', '1');
    localStorage.setItem('gongge_quick_start_guide_seen', '1');
  });
  await targetPage.getByRole('button', { name: '进入平台' }).click();
  await targetPage.getByRole('textbox', { name: '账号' }).fill(username);
  await targetPage.getByRole('textbox', { name: '密码', exact: true }).fill(password);
  await targetPage.getByRole('button', { name: '登录', exact: true }).click();
  await targetPage.waitForFunction(() => Boolean(localStorage.getItem('gongge_auth')));
}

async function assertPopover(targetPage, expectedTexts, expectedWidth) {
  const popover = targetPage.locator('[data-slot="popover-content"][data-state="open"]');
  await popover.waitFor({ state: 'visible' });
  const content = await popover.textContent();
  for (const expectedText of expectedTexts) {
    assert.ok(content?.includes(expectedText), `概念说明缺少完整文案：${expectedText}`);
  }
  const metrics = await popover.evaluate((element) => {
    const rect = element.getBoundingClientRect();
    return {
      width: rect.width,
      height: rect.height,
      clientWidth: element.clientWidth,
      scrollWidth: element.scrollWidth,
      viewportWidth: window.innerWidth,
      viewportHeight: window.innerHeight,
    };
  });
  assert.ok(
    metrics.width >= expectedWidth.min && metrics.width <= expectedWidth.max,
    `弹层宽度 ${metrics.width}px 不在 ${expectedWidth.min}-${expectedWidth.max}px`,
  );
  assert.ok(metrics.scrollWidth <= metrics.clientWidth + 1, '概念说明存在横向文字截断');
  assert.ok(metrics.height <= metrics.viewportHeight * 0.78 + 2, '概念说明超出 78vh 高度上限');
  assert.ok(metrics.width <= metrics.viewportWidth - 24 + 1, '概念说明未保留页面安全边距');
}

try {
  observePage(page, 'desktop');
  await login(page);

  await page.goto(`${baseUrl}/enterprise/accounts`);
  await page.getByText('成员管理（真人）', { exact: true }).waitFor({ state: 'visible' });
  await page.getByText(/这里不管理 AI 数字员工/).waitFor({ state: 'visible' });
  await page.getByRole('button', { name: '了解企业成员' }).click();
  await assertPopover(
    page,
    ['企业成员是真人，不是数字员工', '真实用户解决“谁登录”', '真人不会因为使用数字员工平台'],
    { min: 560, max: 622 },
  );
  await page.keyboard.press('Escape');

  await page.goto(`${baseUrl}/enterprise/agents?view=expert`);
  await page.getByText(/本页管理 AI 数字员工/).waitFor({ state: 'visible' });
  await page.getByRole('button', { name: '了解数字员工' }).click();
  await assertPopover(
    page,
    ['数字员工是 AI 工作主体', '由真人对话、流程节点、定时任务或 API 触发', '数字员工不是企业成员'],
    { min: 560, max: 622 },
  );
  await page.keyboard.press('Escape');

  await page.getByRole('button', { name: '了解个人专家与组织数字员工' }).click();
  await assertPopover(
    page,
    ['个人专家与组织数字员工是同一运行主体的不同治理形态', '广场数字员工模板', '发布到组织内数字员工广场'],
    { min: 560, max: 622 },
  );
  await page.keyboard.press('Escape');

  await page.getByRole('heading', { name: /专家（能力分身）/ }).waitFor({ state: 'visible' });
  await page.getByRole('button', { name: '了解专家' }).click();
  await assertPopover(
    page,
    ['专家是数字员工的能力分身形态', '王超从广场添加“人事政策专家”', '不建立另一套运行引擎'],
    { min: 560, max: 622 },
  );
  assert.ok(
    await page.getByText('专家（能力分身）', { exact: true }).count() > 0,
    '专家页面必须使用消歧后的可见术语',
  );
  await page.keyboard.press('Escape');

  await page.goto(`${baseUrl}/enterprise/dashboard`);
  await page.getByRole('button', { name: '编辑资料', exact: true }).click();
  await page.getByRole('button', { name: '了解治理关系' }).waitFor({ state: 'visible' });
  await page.getByRole('button', { name: '了解治理关系' }).click();
  await assertPopover(
    page,
    ['所有者、责任组织和监督者各管一件事', '组织负责人不会自动成为数字员工监督者', '发布者、使用者和所有者'],
    { min: 560, max: 622 },
  );
  await page.keyboard.press('Escape');
  await page.keyboard.press('Escape');

  await page.goto(`${baseUrl}/enterprise/platform`);
  await page.getByText(/直接使用只建立使用关系/).waitFor({ state: 'visible' });
  await page.waitForFunction(() => document.querySelectorAll('.animate-pulse').length === 0);
  await page.getByRole('button', { name: '了解数字员工广场' }).first().click();
  await assertPopover(
    page,
    [
      '数字员工广场是经过发布、可被用户发现和使用的数字员工目录',
      'AgentUsage',
      'owner_user_id',
      'source_agent_id',
      '不复制发布者的私人记忆、凭据或业务授权',
    ],
    { min: 560, max: 622 },
  );
  await page.waitForTimeout(350);

  await page.screenshot({
    path: '.dev/concept-help-live-regression.png',
    fullPage: true,
  });

  const mobileContext = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const mobilePage = await mobileContext.newPage();
  observePage(mobilePage, 'mobile');
  await login(mobilePage);
  await mobilePage.goto(`${baseUrl}/enterprise/platform`);
  await mobilePage.getByText(/直接使用只建立使用关系/).waitFor({ state: 'visible' });
  await mobilePage.waitForFunction(() => document.querySelectorAll('.animate-pulse').length === 0);
  await mobilePage.getByRole('button', { name: '了解数字员工广场' }).first().click();
  await assertPopover(
    mobilePage,
    ['AgentUsage', 'owner_user_id', 'source_agent_id', '安全复制'],
    { min: 340, max: 367 },
  );
  await mobilePage.waitForTimeout(350);
  await mobilePage.screenshot({
    path: '.dev/concept-help-mobile-live-regression.png',
    fullPage: false,
  });
  const mobilePopover = mobilePage.locator('[data-slot="popover-content"][data-state="open"]');
  const mobileBoundary = mobilePopover.getByText(/添加或使用过，只表示使用关系/);
  await mobileBoundary.scrollIntoViewIfNeeded();
  await mobileBoundary.waitFor({ state: 'visible' });
  const scrollState = await mobilePopover.evaluate((element) => ({
    scrollTop: element.scrollTop,
    scrollHeight: element.scrollHeight,
    clientHeight: element.clientHeight,
  }));
  assert.ok(scrollState.scrollTop > 0, '移动端长文必须能在弹层内部滚动到完整边界说明');
  await mobileContext.close();

  assert.deepEqual(unexpectedResponses, []);
  assert.deepEqual(browserErrors, []);
  assert.equal(await page.getByText('Not Found', { exact: true }).count(), 0);

  console.log(JSON.stringify({
    status: 'passed',
    pages: ['enterprise-members', 'digital-employees', 'expert-forms', 'agent-governance', 'open-gallery'],
    conceptPopovers: 6,
    responsiveViewports: ['1600x1050', '390x844'],
    unexpectedResponses: unexpectedResponses.length,
    browserErrors: browserErrors.length,
  }, null, 2));
} catch (error) {
  await page.screenshot({
    path: '.dev/concept-help-live-regression-failure.png',
    fullPage: true,
  });
  throw error;
} finally {
  await browser.close();
}

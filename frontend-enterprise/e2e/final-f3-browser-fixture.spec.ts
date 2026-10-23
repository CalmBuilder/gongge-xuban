/**
 * @Time       : 2026/10/10
 * @Author     : zhanglp8181
 * @File       : final-f3-browser-fixture.spec.ts
 * @CallChain  : 资源夹具回归 → 多个Playwright案例 → Chromium逐例关闭核验
 * @Description: 保证F3 worker不会把前一浏览器留到下一案例并累积占用。
 */
import { expect } from '@playwright/test';
import { browserFixtureLifecycle, test } from './final-f3-browser-fixture';
import { withOwnerPageParked } from './final-f3-jrn08-helpers';

test.describe.configure({ mode: 'default' });

test('first case owns one live browser lease and preserves project context options', async ({ caseBrowser, page }) => {
  const lifecycle = browserFixtureLifecycle();
  expect(caseBrowser.isConnected()).toBe(true);
  expect(lifecycle).toEqual({ opened: 1, closed: 0, active: 1 });
  expect(page.viewportSize()).toEqual({ width: 1024, height: 768 });
  expect(await page.evaluate(() => navigator.language)).toBe('zh-CN');
});

test('prior case browser is closed before the next lease starts', async ({ caseBrowser }) => {
  const lifecycle = browserFixtureLifecycle();
  expect(caseBrowser.isConnected()).toBe(true);
  expect(lifecycle).toEqual({ opened: 2, closed: 1, active: 1 });
});

test('secondary reviewer can use an isolated page while the owner document is parked', async ({ caseBrowser, page }) => {
  /** 对账期间卸载员工React文档，管理员关闭后恢复员工原URL与页面内容。 */
  await page.goto('data:text/html,<title>owner</title><main>owner-session-open</main>');
  const ownerUrl = page.url();
  await withOwnerPageParked(page, async () => {
    expect(page.url()).toBe('about:blank');
    const reviewerContext = await caseBrowser.newContext();
    try {
      const reviewer = await reviewerContext.newPage();
      await reviewer.goto('data:text/html,<title>reviewer</title><main>reviewer-session-open</main>');
      await expect(reviewer.getByText('reviewer-session-open', { exact: true })).toBeVisible();
      expect(page.url()).toBe('about:blank');
    } finally {
      await reviewerContext.close();
    }
  });
  expect(page.url()).toBe(ownerUrl);
  await expect(page.getByText('owner-session-open', { exact: true })).toBeVisible();
});

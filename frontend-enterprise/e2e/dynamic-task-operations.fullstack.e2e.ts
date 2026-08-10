/**
 * @Time       : 2026/08/10 23:45
 * @Author     : zhanglp8181
 * @File       : dynamic-task-operations.fullstack.e2e.ts
 * @CallChain  : Playwright Chromium → 管理审计页 → operations snapshot API → Runtime DB
 * @Description: 验证租户管理员可见脱敏动态任务运行门禁及隔离环境显式停止阈值。
 */

import { expect, test, type Page } from '@playwright/test';

async function login(page: Page) {
  /** 通过真实认证 API 建立租户管理员浏览器会话。 */

  await page.goto('/enterprise/dashboard');
  const status = await page.evaluate(async () => {
    const response = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tenant_id: 'tenant_demo', username: 'admin', password: 'admin' }),
    });
    const body = await response.json();
    if (response.ok) {
      localStorage.setItem('gongge_auth', JSON.stringify(body));
      localStorage.setItem('gongge_onboarding_guide_seen', '1');
      localStorage.setItem('gongge_quick_start_guide_seen', '1');
    }
    return response.status;
  });
  expect(status).toBe(200);
}

test('租户管理员从真实运行表读取动态任务门禁和显式停止阈值', async ({ page }) => {
  await login(page);
  const snapshotResponse = page.waitForResponse((response) => (
    response.request().method() === 'GET'
    && new URL(response.url()).pathname === '/api/dynamic-task-operations/snapshot'
  ));

  await page.goto('/enterprise/management-audit');

  const response = await snapshotResponse;
  expect(response.status()).toBe(200);
  const payload = await response.json();
  expect(payload.tenant_id).toBe('tenant_demo');
  expect(payload.thresholds_configured).toBe(true);
  expect(payload.quota_limits_configured).toBe(true);
  expect(payload.quota_limits).toEqual({ tenant: 16, agent: 8, user: 4, tool: 4 });
  expect(JSON.stringify(payload)).not.toMatch(/prompt|password|secret|request_json|result_json/i);
  await expect(page.getByRole('region', { name: '动态任务运行门禁' })).toBeVisible();
  await expect(page.getByText('运行阈值内')).toBeVisible();
  await expect(page.getByText('停止阈值 待配置')).toHaveCount(0);
});

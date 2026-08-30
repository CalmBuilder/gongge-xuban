/**
 * @Time       : 2026/08/10 23:45
 * @Author     : zhanglp8181
 * @File       : dynamic-task-operations.fullstack.e2e.ts
 * @CallChain  : Playwright Chromium → 管理审计页 → operations snapshot API → Runtime DB
 * @Description: 验证租户管理员可见普通动态开放状态及两类高风险灰度的独立状态。
 */

import { expect, test, type Page } from '@playwright/test';

const PROFILE = process.env.FULLSTACK_E2E_PROFILE ?? 'base-open';

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
  const baseOpen = !['kill-switch', 'runtime-capacity-saturated'].includes(PROFILE);
  const runtimeCapacityAvailable = PROFILE !== 'runtime-capacity-saturated';
  expect(payload.tenant_id).toBe('tenant_demo');
  const thresholdsConfigured = ['high-risk-gray', 'destructive-gray'].includes(PROFILE);
  expect(payload.thresholds_configured).toBe(thresholdsConfigured);
  expect(payload.base_execution_available).toBe(baseOpen);
  expect(payload.runtime_capacity_limits_configured).toBe(PROFILE !== 'runtime-capacity-saturated');
  expect(payload.runtime_capacity_available).toBe(runtimeCapacityAvailable);
  expect(payload.high_risk_external_write_available).toBe(PROFILE === 'high-risk-gray');
  expect(payload.high_risk_destructive_available).toBe(PROFILE === 'destructive-gray');
  expect(payload.quota_limits).toEqual(
    PROFILE === 'runtime-capacity-saturated'
      ? { tenant: 0, agent: 0, user: 0, tool: 0 }
      : { tenant: 16, agent: 8, user: 4, tool: 4 },
  );
  expect(JSON.stringify(payload)).not.toMatch(/prompt|password|secret|request_json|result_json/i);
  await expect(page.getByRole('region', { name: '动态任务运行门禁' })).toBeVisible();
  if (baseOpen) {
    await expect(page.getByText('普通动态已开放', { exact: true })).toBeVisible();
  } else if (PROFILE === 'runtime-capacity-saturated') {
    await expect(page.getByText('运行容量不可用', { exact: true })).toBeVisible();
  } else {
    await expect(page.getByText('普通动态未开放', { exact: true })).toBeVisible();
  }
  await expect(
    page.getByText('普通动态', { exact: true }).locator('..').getByText(
      baseOpen ? '已开放' : '不可用',
      { exact: true },
    ),
  ).toBeVisible();
  await expect(
    page.getByText('external write', { exact: true }).locator('..').getByText(
      PROFILE === 'high-risk-gray' ? '独立灰度中' : '默认关闭',
      { exact: true },
    ),
  ).toBeVisible();
  await expect(
    page.getByText('destructive-gray', { exact: true }).locator('..').getByText(
      PROFILE === 'destructive-gray' ? '隔离验证中' : '默认关闭',
      { exact: true },
    ),
  ).toBeVisible();
  await expect(page.getByText('停止阈值 待配置')).toHaveCount(thresholdsConfigured ? 0 : 5);
});

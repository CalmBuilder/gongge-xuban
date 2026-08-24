/**
 * @Time       : 2026/08/14
 * @Author     : zhanglp8181
 * @File       : model-config-connection.fullstack.e2e.ts
 * @CallChain  : Playwright Chromium → 模型配置页 → FastAPI connection test → LLM adapter probe
 * @Description: 验证管理员可从真实页面执行分阶段模型连接诊断，且结果不泄露 API Key。
 */

import { expect, test, type Page } from '@playwright/test';

async function login(page: Page) {
  /** 通过真实认证 API 建立管理员会话，保留模型配置权限检查。 */

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

test('真实 Chromium 从模型列表执行连接测试并查看四阶段诊断', async ({ page }) => {
  /** 确认按钮、正式 API、阶段结果与脱敏边界形成可人工复验的产品闭环。 */

  const serverErrors: string[] = [];
  page.on('response', (response) => {
    const path = new URL(response.url()).pathname;
    if (path.startsWith('/api/') && response.status() >= 500) {
      serverErrors.push(`${response.status()} ${path}`);
    }
  });
  await login(page);
  await page.goto('/enterprise/models');

  const firstRow = page.getByRole('row').filter({ hasText: 'Demo Qwen Compatible' });
  await expect(firstRow).toBeVisible();
  const responsePromise = page.waitForResponse((response) => (
    /\/api\/enterprise\/model-configs\/[^/]+\/test$/.test(new URL(response.url()).pathname)
    && response.request().method() === 'POST'
  ));
  await firstRow.getByRole('button', { name: '连接测试 Demo Qwen Compatible' }).click();
  const response = await responsePromise;
  expect(response.status()).toBe(200);
  const body = await response.json();
  expect(body.success).toBe(true);
  expect(body.output).toBe('E2E-CONNECTION-OK');
  expect(body.checks.map((item: { name: string; status: string }) => [item.name, item.status])).toEqual([
    ['配置', 'passed'],
    ['模型目录', 'passed'],
    ['账户状态', 'skipped'],
    ['最小生成', 'passed'],
  ]);

  const dialog = page.getByRole('dialog', { name: '模型连接诊断' });
  await expect(dialog.getByText('连接可用')).toBeVisible();
  await expect(dialog.getByText('模型目录', { exact: true })).toBeVisible();
  await expect(dialog.getByText('最小生成', { exact: true })).toBeVisible();
  await expect(dialog).not.toContainText('e2e-schedule-model-key');
  expect(serverErrors).toEqual([]);
});

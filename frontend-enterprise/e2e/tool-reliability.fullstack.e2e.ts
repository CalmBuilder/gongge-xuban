/**
 * @Time       : 2026/08/10
 * @Author     : zhanglp8181
 * @File       : tool-reliability.fullstack.e2e.ts
 * @CallChain  : Playwright Chromium → ToolsPage → Tools API → reliability publication
 * @Description: 验证管理端发布、回读并撤销 explore-safe 纯读工具契约。
 */

import { expect, test, type Page } from '@playwright/test';

const OVERALL_AGENT_ID = 'agent_tenant_demo_overall';

async function login(page: Page) {
  /** 通过真实登录 API 建立管理端认证状态。 */

  await page.goto('/enterprise/dashboard');
  const status = await page.evaluate(async (overallAgentId) => {
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
      localStorage.setItem('gongge_enterprise_agent_scope', overallAgentId);
    }
    return response.status;
  }, OVERALL_AGENT_ID);
  expect(status).toBe(200);
}

test('工具管理端发布并撤销 explore-safe 可靠性契约', async ({ page }) => {
  await login(page);
  const tool = await page.evaluate(async (agentId) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}');
    const response = await fetch(
      `/api/enterprise/tools?tenant_id=tenant_demo&agent_id=${encodeURIComponent(agentId)}`,
      { headers: { Authorization: `Bearer ${auth.token}` } },
    );
    const rows = await response.json();
    return rows.find((row: { name: string }) => row.name === 'product.price_query');
  }, OVERALL_AGENT_ID);
  expect(tool?.id).toBeTruthy();

  await page.goto(`/enterprise/tools/${tool.id}/edit?agent_id=${encodeURIComponent(OVERALL_AGENT_ID)}`);
  const editor = page.getByLabel('动态任务可靠性契约');
  await expect(editor).toBeVisible();
  await expect(editor).toHaveValue('');
  const contract = {
    risk_class: 'read',
    side_effect: 'none',
    confirmation_policy: 'none',
    timeout_policy: 'failed',
    dynamic_task_enabled: true,
    explore_safe: true,
    model_visibility: {
      allowed_paths: ['input.product_name', 'output.price', 'output.currency'],
      user_display_paths: ['output.price', 'output.currency'],
      audit_only_paths: [],
    },
  };
  await editor.fill(JSON.stringify(contract, null, 2));
  const publishResponse = page.waitForResponse((response) => (
    response.request().method() === 'PUT'
    && new URL(response.url()).pathname === `/api/enterprise/tools/${tool.id}`
  ));
  await page.getByRole('button', { name: '保存', exact: true }).click();
  expect((await publishResponse).status()).toBe(200);
  await expect(editor).toHaveValue(/"explore_safe": true/);

  const reloadResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return response.request().method() === 'GET'
      && response.status() === 200
      && url.pathname === `/api/enterprise/tools/${tool.id}`
      && url.searchParams.get('tenant_id') === 'tenant_demo'
      && url.searchParams.get('agent_id') === OVERALL_AGENT_ID;
  });
  await page.reload();
  const reloadedToolResponse = await reloadResponse;
  expect(reloadedToolResponse.status()).toBe(200);
  expect((await reloadedToolResponse.json()).reliability_contract).toMatchObject({
    dynamic_task_enabled: true,
    explore_safe: true,
  });
  await expect(page.getByLabel('动态任务可靠性契约')).toHaveValue(/"explore_safe": true/);
  await page.getByLabel('动态任务可靠性契约').fill('');
  const revokeResponse = page.waitForResponse((response) => (
    response.request().method() === 'PUT'
    && new URL(response.url()).pathname === `/api/enterprise/tools/${tool.id}`
  ));
  await page.getByRole('button', { name: '保存', exact: true }).click();
  expect((await revokeResponse).status()).toBe(200);
  await expect(page.getByLabel('动态任务可靠性契约')).toHaveValue('');
});

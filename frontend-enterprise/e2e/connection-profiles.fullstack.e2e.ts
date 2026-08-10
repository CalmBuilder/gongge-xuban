/**
 * @Time       : 2026/08/10
 * @Author     : zhanglp8181
 * @File       : connection-profiles.fullstack.e2e.ts
 * @CallChain  : Playwright Chromium → 单端口 FastAPI/SQLite → ConnectionProfile/Attention
 * @Description: 验证连接控制面、显式 Agent 绑定和重授权待办的真实浏览器闭环。
 */

import { expect, test, type Page } from '@playwright/test';

async function login(page: Page) {
  /** 通过真实认证 API 保存管理员会话，不跳过前后端权限解析。 */

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

test('真实 Chromium 创建连接、显式绑定并停用，响应不泄露凭据', async ({ page }) => {
  const browserErrors: string[] = [];
  page.on('pageerror', (error) => browserErrors.push(error.message));
  page.on('response', (response) => {
    const path = new URL(response.url()).pathname;
    if (path.startsWith('/api/') && response.status() >= 500) {
      browserErrors.push(`${response.status()} ${path}`);
    }
  });
  await login(page);
  await page.goto('/enterprise/connections');

  await expect(page.getByText('E2E 管理工作区', { exact: true })).toBeVisible();
  await expect(page.getByText('T-E2E-MANAGE', { exact: false })).toBeVisible();
  await expect(page.getByText(/xoxb-e2e-manage-seed/)).toHaveCount(0);

  await page.getByRole('button', { name: '连接企业微信' }).first().click();
  const createDialog = page.getByRole('dialog');
  await createDialog.getByRole('combobox', { name: '连接类型' }).click();
  await page.getByRole('option', { name: 'Slack 工作区' }).click();
  await createDialog.getByLabel('连接显示名称').fill('E2E 浏览器新工作区');
  await createDialog.getByLabel('Slack Bot Token').fill('xoxb-create-browser-secret');
  const createResponse = page.waitForResponse((response) => (
    new URL(response.url()).pathname === '/api/enterprise/connection-profiles'
    && response.request().method() === 'POST'
  ));
  await createDialog.getByRole('button', { name: '验证并连接' }).click();
  const created = await createResponse;
  expect(created.status()).toBe(201);
  expect(JSON.stringify(await created.json())).not.toContain('xoxb-create-browser-secret');

  const card = page.getByRole('article').filter({ hasText: 'E2E 浏览器新工作区' });
  await expect(card).toContainText('T-E2E-CREATED');
  await card.getByRole('button', { name: 'Agent 绑定' }).click();
  const bindingDialog = page.getByRole('dialog', { name: '数字员工绑定' });
  await bindingDialog.getByRole('combobox', { name: '选择数字员工' }).click();
  await page.getByRole('option', { name: 'E2E 成员数字员工' }).click();
  const bindingResponse = page.waitForResponse((response) => (
    new URL(response.url()).pathname.endsWith('/bindings')
    && response.request().method() === 'POST'
  ));
  await bindingDialog.getByRole('button', { name: '新增绑定' }).click();
  expect((await bindingResponse).status()).toBe(201);
  await expect(bindingDialog.getByText('E2E 成员数字员工', { exact: true })).toBeVisible();
  await bindingDialog.getByRole('button', { name: '关闭' }).click();

  await card.getByRole('button', { name: '停用' }).click();
  const disableDialog = page.getByRole('dialog', { name: '停用 E2E 浏览器新工作区' });
  const disableResponse = page.waitForResponse((response) => (
    new URL(response.url()).pathname.endsWith('/disable')
    && response.request().method() === 'POST'
  ));
  await disableDialog.getByRole('button', { name: '确认停用' }).click();
  expect((await disableResponse).status()).toBe(200);
  await expect(card).toContainText('此连接已停用');
  expect(browserErrors).toEqual([]);
});

test('真实 Chromium 配置企业微信入站路由并从安全事件授权用户', async ({ page }) => {
  /** 验证管理端不接触外部 UserID/正文，路由和主体授权均落真实后端事务。 */

  await login(page);
  await page.goto('/enterprise/connections');
  const card = page.getByRole('article').filter({ hasText: 'E2E 企业微信消息' });
  await expect(card).toBeVisible();
  await card.getByRole('button', { name: 'Agent 绑定' }).click();
  const bindingDialog = page.getByRole('dialog', { name: '数字员工绑定' });
  const actionResponse = page.waitForResponse((response) => (
    new URL(response.url()).pathname.endsWith('/actions')
    && response.request().method() === 'POST'
  ));
  await bindingDialog.getByRole('switch', { name: 'E2E 数字员工审批后发送' }).click();
  const action = await actionResponse;
  expect(action.status()).toBe(200);
  expect((await action.json()).allowed_actions).toEqual(['wecom.message_send']);
  await bindingDialog.getByRole('button', { name: '关闭' }).click();

  await card.getByRole('button', { name: '消息接入' }).click();
  const dialog = page.getByRole('dialog', { name: '消息接入' });

  await dialog.getByRole('combobox', { name: '接收消息的数字员工' }).click();
  await page.getByRole('option', { name: 'E2E 数字员工' }).click();
  const routeResponse = page.waitForResponse((response) => (
    new URL(response.url()).pathname.endsWith('/inbound-route')
    && response.request().method() === 'POST'
  ));
  await dialog.getByRole('button', { name: '保存消息路由' }).click();
  expect((await routeResponse).status()).toBe(200);

  await dialog.getByRole('combobox', { name: /选择事件connin_e2e_pending对应用户/ }).click();
  await page.getByRole('option', { name: 'E2E Member' }).click();
  const principalResponse = page.waitForResponse((response) => (
    new URL(response.url()).pathname.endsWith('/inbound/principal-bindings')
    && response.request().method() === 'POST'
  ));
  await dialog.getByRole('button', { name: '授权并恢复' }).click();
  const principal = await principalResponse;
  expect(principal.status()).toBe(201);
  const serialized = JSON.stringify(await principal.json());
  expect(serialized).not.toContain('sender_ref');
  expect(serialized).not.toContain('e2e-external-user');
  await expect(dialog.getByText('没有待授权发送者')).toBeVisible();
});

test('真实 Chromium 原子办理 reauth Attention 并进入最近已处理', async ({ page }) => {
  await login(page);
  await page.goto('/enterprise/work-items');

  const item = page.getByRole('button', { name: /重新授权 E2E 待重授权工作区/ });
  await expect(item).toBeVisible();
  await item.click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toContainText('T-E2E-REAUTH');
  await dialog.getByLabel('新的 Slack Bot Token').fill('xoxb-reauth-browser-secret');
  const reauthResponse = page.waitForResponse((response) => (
    new URL(response.url()).pathname.includes('/reauthorize-attention/')
    && response.request().method() === 'POST'
  ));
  await dialog.getByRole('button', { name: '验证并恢复任务' }).click();
  const response = await reauthResponse;
  expect(response.status()).toBe(200);
  expect(JSON.stringify(await response.json())).not.toContain('xoxb-reauth-browser-secret');

  await page.getByRole('tab', { name: '最近已处理' }).click();
  await expect(page.getByRole('button', { name: /重新授权 E2E 待重授权工作区/ })).toBeVisible();
});

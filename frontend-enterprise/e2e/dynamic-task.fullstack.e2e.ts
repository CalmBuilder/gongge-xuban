/**
 * @Time       : 2026/08/10
 * @Author     : zhanglp8181
 * @File       : dynamic-task.fullstack.e2e.ts
 * @CallChain  : Playwright Chromium → 单端口前端/FastAPI → Execution/Attention/Artifact
 * @Description: 验证动态任务卡、鉴权下载、刷新恢复、越权拒绝和待我处理闭环。
 */

import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';

import { expect, test, type Page } from '@playwright/test';

const ARTIFACT_CONTENT = '# 浏览器续约风险简报\n\n合同证据、风险项和处理建议均已核验。';

async function login(page: Page, username = 'admin', password = 'admin') {
  /** 通过真实登录 API 建立浏览器认证会话，避免绕过后端认证。 */

  await page.goto('/enterprise/dashboard');
  const status = await page.evaluate(async (credentials) => {
    const response = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tenant_id: 'tenant_demo', ...credentials }),
    });
    const body = await response.json();
    if (response.ok) {
      localStorage.setItem('gongge_auth', JSON.stringify(body));
      localStorage.setItem('gongge_onboarding_guide_seen', '1');
      localStorage.setItem('gongge_quick_start_guide_seen', '1');
      localStorage.setItem('gongge_enterprise_agent_scope', 'agent_e2e_employee');
    }
    return response.status;
  }, { username, password });
  expect(status).toBe(200);
}

test('真实 Chromium 展示并鉴权下载动态任务 Artifact，刷新后仍可恢复', async ({ page }) => {
  const browserErrors: string[] = [];
  page.on('pageerror', (error) => browserErrors.push(`pageerror: ${error.message}`));
  page.on('response', (response) => {
    const path = new URL(response.url()).pathname;
    if (path.startsWith('/api/') && response.status() >= 500) {
      browserErrors.push(`${response.status()} ${path}`);
    }
  });
  await login(page);

  const executionResponse = page.waitForResponse((response) => (
    new URL(response.url()).pathname === '/api/executions/execution_e2e_dynamic_artifact'
  ));
  await page.goto('/workspace/chat/session_e2e_dynamic_artifact');
  await expect(page.getByText('普通问答仍可正常展示，不会创建新的动态执行。')).toBeVisible();
  await expect(page.getByText('合同证据、风险项和处理建议均已核验。')).toBeVisible();
  await expect(page.getByLabel('任务交付物')).toBeVisible();
  const executionPayload = await (await executionResponse).json() as Record<string, unknown>;
  expect(JSON.stringify(executionPayload)).not.toContain('storage_locator');
  expect(JSON.stringify(executionPayload)).not.toContain('token');

  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('button', { name: /浏览器续约风险简报\.md/ }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe('浏览器续约风险简报.md');
  const downloadedPath = await download.path();
  expect(downloadedPath).not.toBeNull();
  const downloaded = await readFile(downloadedPath!);
  expect(downloaded.toString('utf-8')).toBe(ARTIFACT_CONTENT);
  expect(createHash('sha256').update(downloaded).digest('hex')).toBe(
    createHash('sha256').update(ARTIFACT_CONTENT).digest('hex'),
  );

  await page.reload();
  await expect(page.getByLabel('任务交付物')).toBeVisible();
  await expect(page.getByRole('button', { name: /浏览器续约风险简报\.md/ })).toBeVisible();
  expect(browserErrors).toEqual([]);
});

test('非 ACL 用户在真实浏览器会话中无法枚举或下载 Artifact', async ({ page }) => {
  await login(page, 'other-member', 'other-member');
  const result = await page.evaluate(async () => {
    const rawSession = localStorage.getItem('gongge_auth');
    if (!rawSession) throw new Error('登录后未保存认证会话');
    const token = (JSON.parse(rawSession) as { token: string }).token;
    const executionResponse = await fetch(
      '/api/executions/execution_e2e_dynamic_artifact?tenant_id=tenant_demo',
      { headers: { Authorization: `Bearer ${token}` } },
    );
    return { status: executionResponse.status };
  });
  expect(result.status).toBe(403);
});

test('真实 Chromium 在待我处理办理动态澄清并进入最近已处理', async ({ page }) => {
  const browserErrors: string[] = [];
  page.on('pageerror', (error) => browserErrors.push(error.message));
  await login(page);
  await page.goto('/enterprise/work-items');

  await page.getByRole('button', { name: /确认需要核验的合作方/ }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByText('请选择需要核验的合作方')).toBeVisible();
  await expect(dialog.getByLabel('执行计划')).toContainText('补充合作方后继续合同核验');
  await dialog.getByRole('button', { name: '星海科技' }).click();
  const resolveResponse = page.waitForResponse((response) => (
    new URL(response.url()).pathname
      === '/api/attention-items/attention_e2e_dynamic_clarification/resolve'
    && response.request().method() === 'POST'
  ));
  await dialog.getByRole('button', { name: '补充并继续' }).click();
  expect((await resolveResponse).status()).toBe(200);

  await page.getByRole('tab', { name: '最近已处理' }).click();
  await expect(page.getByRole('button', { name: /确认需要核验的合作方/ })).toBeVisible();
  await page.getByRole('button', { name: /确认需要核验的合作方/ }).click();
  await expect(page.getByRole('dialog')).toContainText('星海科技');
  expect(browserErrors).toEqual([]);
});

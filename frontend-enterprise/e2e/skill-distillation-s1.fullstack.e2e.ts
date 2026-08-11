/**
 * @Time       : 2026/08/12
 * @Author     : zhanglp8181
 * @File       : skill-distillation-s1.fullstack.e2e.ts
 * @CallChain  : Playwright Chromium → Skill 安全导入 UI → 真实 FastAPI/SQLite/object store
 * @Description: 验证本人数字员工上传、预览恢复、checksum 确认、绑定和恶意包失败闭环。
 */

import { expect, test, type Page } from '@playwright/test';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const VALID_SKILL_ZIP_BASE64 = 'UEsDBBQAAAAIAMWqC11jNQ9nvAAAAOwAAAAWAAAAcmVmdW5kLWhlbHBlci9TS0lMTC5tZHWOOwrCQBQA+z3FgvV6gNwmmC2EJCsbwTaIGvNTixgQQUQLP5jYiPgJehh9m03lFVTS2NjPMEMIQaZqUAXLQ5K7G4hSGA3kqgP9iZifim2ANGrVeL3RrDNTwSJwYJ/9suBthOuXrEwXEI6fFx/SGZyPcrUsHK+wbbG7ieieJ/HDbiNV11mLaqTJmG4pCGOCa9yoMq5RXuVU1RD5TFX+DyHo9uX+CsP4t/fKAuiFkI3zaF0mSwmmszyJvuE3UEsDBBQAAAAIAMWqC12C6hQSMwAAADIAAAApAAAAcmVmdW5kLWhlbHBlci9yZWZlcmVuY2VzL3JlZnVuZC1wb2xpY3kubWRTVnjZ0PBszb4Xy1uedszk4no2Z9XT/okvtix/sW7R096pT/vXP+2b/2L7eoiqxw1NXABQSwECFAMUAAAACADFqgtdYzUPZ7wAAADsAAAAFgAAAAAAAAAAAAAAgAEAAAAAcmVmdW5kLWhlbHBlci9TS0lMTC5tZFBLAQIUAxQAAAAIAMWqC12C6hQSMwAAADIAAAApAAAAAAAAAAAAAACAAfAAAAByZWZ1bmQtaGVscGVyL3JlZmVyZW5jZXMvcmVmdW5kLXBvbGljeS5tZFBLBQYAAAAAAgACAJsAAABqAQAAAAA=';
const MALICIOUS_SKILL_ZIP_BASE64 = 'UEsDBBQAAAAAANKqC11uA8/yBgAAAAYAAAALAAAALi4vU0tJTEwubWR1bnNhZmVQSwECFAMUAAAAAADSqgtdbgPP8gYAAAAGAAAACwAAAAAAAAAAAAAAgAEAAAAALi4vU0tJTEwubWRQSwUGAAAAAAEAAQA5AAAALwAAAAAA';

type BrowserFailure = {
  kind: 'console' | 'pageerror' | 'request' | 'response';
  detail: string;
};

const E2E_DIRECTORY = path.dirname(fileURLToPath(import.meta.url));

async function loginAsMember(page: Page) {
  const status = await page.evaluate(async () => {
    localStorage.setItem('gongge_onboarding_guide_seen', '1');
    localStorage.setItem('gongge_quick_start_guide_seen', '1');
    localStorage.setItem('gongge_enterprise_agent_scope', 'agent_e2e_member_employee');
    const response = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        username: 'member',
        password: 'member',
      }),
    });
    const body = await response.json();
    if (response.ok) localStorage.setItem('gongge_auth', JSON.stringify(body));
    return response.status;
  });
  expect(status).toBe(200);
}

function collectBrowserFailures(page: Page): BrowserFailure[] {
  const failures: BrowserFailure[] = [];
  page.on('pageerror', (error) => failures.push({ kind: 'pageerror', detail: error.message }));
  page.on('console', (message) => {
    if (message.type() === 'error') failures.push({ kind: 'console', detail: message.text() });
  });
  page.on('requestfailed', (request) => {
    failures.push({ kind: 'request', detail: `${request.method()} ${request.url()}` });
  });
  page.on('response', (response) => {
    const path = new URL(response.url()).pathname;
    if (path.startsWith('/api/') && response.status() >= 500) {
      failures.push({ kind: 'response', detail: `${response.status()} ${path}` });
    }
  });
  return failures;
}

async function openSecureImport(page: Page) {
  await page.getByRole('button', { name: /新增/ }).click();
  await page.getByRole('menuitem', { name: '安全导入 Skill' }).click();
  await expect(page.getByRole('dialog', { name: '安全导入 Skill 包' })).toBeVisible();
}

async function uploadPackage(page: Page, base64: string, name: string) {
  const dialog = page.getByRole('dialog', { name: '安全导入 Skill 包' });
  await dialog.locator('input[type="file"]').setInputFiles({
    name,
    mimeType: 'application/zip',
    buffer: Buffer.from(base64, 'base64'),
  });
  const responsePromise = page.waitForResponse((response) => (
    new URL(response.url()).pathname === '/api/enterprise/general-skill-import-jobs'
    && response.request().method() === 'POST'
  ));
  await dialog.getByRole('button', { name: '生成安全预览' }).click();
  expect((await responsePromise).status()).toBe(202);
  return dialog;
}

test('S1 本人数字员工审核候选后固定修订并自动建立 pinned 绑定', async ({ page }) => {
  const failures = collectBrowserFailures(page);
  await page.goto('/enterprise/dashboard');
  await loginAsMember(page);
  await page.goto('/enterprise/general-skills');
  await openSecureImport(page);
  const dialog = await uploadPackage(page, VALID_SKILL_ZIP_BASE64, 'refund-helper.zip');

  await expect(dialog.getByText('购物售后规则核验', { exact: true })).toBeVisible();
  await expect(dialog.getByText('crm.order.read', { exact: true })).toBeVisible();
  await expect(dialog.getByText('申请工具（不代表已授权）')).toBeVisible();
  await expect(dialog.getByText('规范包 checksum')).toBeVisible();

  const confirmResponse = page.waitForResponse((response) => (
    new URL(response.url()).pathname.endsWith('/confirm')
    && response.request().method() === 'POST'
  ));
  await dialog.getByRole('button', { name: '固定版本并绑定' }).click();
  expect((await confirmResponse).status()).toBe(200);

  await expect(page.getByRole('row').filter({ hasText: '购物售后规则核验' })).toBeVisible();
  await expect(page.getByRole('row').filter({ hasText: '购物售后规则核验' })).toContainText('已启用');
  expect(failures).toEqual([]);
});

test('S1 安全预览刷新后恢复且取消会清除待确认作业', async ({ page }) => {
  const failures = collectBrowserFailures(page);
  await page.goto('/enterprise/dashboard');
  await loginAsMember(page);
  await page.goto('/enterprise/general-skills');
  await openSecureImport(page);
  await uploadPackage(page, VALID_SKILL_ZIP_BASE64, 'resume-helper.zip');
  await expect(page.getByText('规范包 checksum')).toBeVisible();

  await page.reload();
  const restored = page.getByRole('dialog', { name: '安全导入 Skill 包' });
  await expect(restored).toBeVisible();
  await expect(restored.getByText('购物售后规则核验', { exact: true })).toBeVisible();
  const cancelResponse = page.waitForResponse((response) => (
    new URL(response.url()).pathname.endsWith('/cancel')
    && response.request().method() === 'POST'
  ));
  await restored.getByRole('button', { name: '取消' }).click();
  expect((await cancelResponse).status()).toBe(200);
  await expect(restored).toBeHidden();
  expect(failures).toEqual([]);
});

test('S1 危险路径包在浏览器中进入明确失败终态且不能确认', async ({ page }) => {
  const failures = collectBrowserFailures(page);
  await page.goto('/enterprise/dashboard');
  await loginAsMember(page);
  await page.goto('/enterprise/general-skills');
  await openSecureImport(page);
  const dialog = await uploadPackage(page, MALICIOUS_SKILL_ZIP_BASE64, 'unsafe.zip');

  await expect(dialog.getByRole('alert')).toContainText('Skill 包未通过安全检查');
  await expect(dialog.getByRole('button', { name: '固定版本并绑定' })).toHaveCount(0);
  expect(failures).toEqual([]);
});

test('S1 GitHub 固定 commit 经供应商边界发现多候选并全部绑定', async ({ page }) => {
  const failures = collectBrowserFailures(page);
  await page.goto('/enterprise/dashboard');
  await loginAsMember(page);
  await page.goto('/enterprise/general-skills');
  await openSecureImport(page);
  const dialog = page.getByRole('dialog', { name: '安全导入 Skill 包' });
  await dialog.getByRole('tab', { name: 'GitHub 固定版本' }).click();
  await dialog.getByLabel('GitHub 仓库地址').fill('https://github.com/mattpocock/skills');
  await dialog.getByLabel('完整 commit SHA').fill('84fdeffd12f2ee307994d1eb6feb48173b6e0502');
  const createResponse = page.waitForResponse((response) => (
    new URL(response.url()).pathname === '/api/enterprise/general-skill-import-jobs'
    && response.request().method() === 'POST'
  ));
  await dialog.getByRole('button', { name: '生成安全预览' }).click();
  expect((await createResponse).status()).toBe(202);
  await expect(dialog.getByText('tdd', { exact: true })).toBeVisible();
  await expect(dialog.getByText('systematic-debugging', { exact: true })).toBeVisible();
  await expect(dialog.getByText('仅显式调用', { exact: true })).toBeVisible();
  await expect(dialog.getByText('调用提示：Describe the reproducible failure', { exact: true })).toBeVisible();
  await expect(dialog.getByText(/待确认的同包 Skill 引用/)).toBeVisible();
  await expect(dialog.getByText('/systematic-debugging', { exact: true })).toBeVisible();
  await dialog.getByLabel('依赖 /systematic-debugging 的处理方式').selectOption('required');

  const confirmResponse = page.waitForResponse((response) => (
    new URL(response.url()).pathname.endsWith('/confirm')
    && response.request().method() === 'POST'
  ));
  await dialog.getByRole('button', { name: '固定版本并绑定' }).click();
  expect((await confirmResponse).status()).toBe(200);
  await expect(page.getByRole('row').filter({ hasText: 'tdd' })).toBeVisible();
  await expect(page.getByRole('row').filter({ hasText: 'systematic-debugging' })).toBeVisible();
  expect(failures).toEqual([]);
});

test('S1 浏览器选择完整文件夹后复用安全预览并固定绑定', async ({ page }) => {
  const failures = collectBrowserFailures(page);
  await page.goto('/enterprise/dashboard');
  await loginAsMember(page);
  await page.goto('/enterprise/general-skills');
  await openSecureImport(page);
  const dialog = page.getByRole('dialog', { name: '安全导入 Skill 包' });
  await dialog.getByRole('tab', { name: '选择文件夹' }).click();
  await dialog.locator('input[webkitdirectory]').setInputFiles(
    path.join(E2E_DIRECTORY, 'fixtures/folder-skill'),
  );
  await expect(dialog.getByText('已选择 2 个文件')).toBeVisible();
  const previewResponse = page.waitForResponse((response) => (
    new URL(response.url()).pathname === '/api/enterprise/general-skill-import-jobs'
    && response.request().method() === 'POST'
  ));
  await dialog.getByRole('button', { name: '生成安全预览' }).click();
  expect((await previewResponse).status()).toBe(202);
  await expect(dialog.getByText('browser-folder-skill', { exact: true })).toBeVisible();
  await expect(dialog.getByText('2 个文件', { exact: true })).toBeVisible();
  const confirmResponse = page.waitForResponse((response) => (
    new URL(response.url()).pathname.endsWith('/confirm')
    && response.request().method() === 'POST'
  ));
  await dialog.getByRole('button', { name: '固定版本并绑定' }).click();
  expect((await confirmResponse).status()).toBe(200);
  await expect(page.getByRole('row').filter({ hasText: 'browser-folder-skill' })).toBeVisible();
  expect(failures).toEqual([]);
});

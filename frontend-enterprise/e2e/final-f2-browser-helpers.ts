/**
 * @Time       : 2026/10/04
 * @Author     : zhanglp8181
 * @File       : final-f2-browser-helpers.ts
 * @CallChain  : 三身份真实登录/管理页面 → 原授权API → 白名单交叉旁证
 * @Description: 不注入认证或scope，不复制自由正文；闲置页面释放React资源。
 */
import { expect, type Page, type TestInfo } from '@playwright/test';
import { object, request, type Row } from './final-f1-browser-helpers';

export async function identityFacts(page: Page): Promise<Record<string, Row[]>> {
  /** 验证只读身份旁证的实际结构，未知值不作为通过依据。 */
  const value = object(await request(page, '/__e2e/f2/facts'));
  const rows: Record<string, Row[]> = {};
  for (const [key, values] of Object.entries(value)) {
    if (!Array.isArray(values)) throw new Error('F2_IDENTITY_ARRAY_REQUIRED');
    rows[key] = values.map(object);
  }
  return rows;
}

export async function audit(info: TestInfo, name: string, value: Row): Promise<void> {
  /** 即时保存原阶段白名单结果，未完成阶段不可被后续重跑覆盖。 */
  console.info('F2_CHECKPOINT', name);
  await info.attach('f2-safe-criterion', { contentType: 'application/json',
    body: Buffer.from(JSON.stringify({ criterion: name, ...value })) });
}

export async function authenticatedRead(page: Page, path: string): Promise<{status: number; body: unknown}> {
  /** 从真实登录态发起原只读API；响应正文仅供当前断言，不进入公开证据。 */
  return page.evaluate(async path => {
    const stored = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as {token?: string};
    const response = await fetch(path, { headers: { Authorization: `Bearer ${stored.token || ''}` } });
    const text = await response.text();
    return { status: response.status, body: text ? JSON.parse(text) as unknown : null };
  }, path);
}

export async function idle(page: Page): Promise<Page> {
  /** 关闭结束的重页面释放renderer，独立context及原登录持久态保持。 */
  const context = page.context();
  await page.close();
  const restored = await context.newPage();
  await restored.goto('/__e2e/f2/idle');
  return restored;
}

export async function foreignLogin(page: Page): Promise<Page> {
  /** 外租户使用原VITE_TENANT_ID构建、原登录页和API，不替换请求或token。 */
  await page.goto('/__e2e/f2/foreign/');
  await page.evaluate(() => {
    localStorage.setItem('gongge_onboarding_guide_seen', '1');
    localStorage.setItem('gongge_quick_start_guide_seen', '1');
  });
  await page.getByRole('button', { name: /进入平台/ }).click();
  await page.getByRole('textbox', { name: '账号', exact: true }).fill('f2_foreign');
  await page.getByLabel('密码', { exact: true }).fill('f2-foreign-synthetic-password');
  const response = page.waitForResponse(row => new URL(row.url()).pathname === '/api/auth/login');
  await page.getByRole('button', { name: '登录', exact: true }).click();
  expect((await response).status()).toBe(200);
  await expect.poll(async () => {
    const read = await authenticatedRead(page, '/api/auth/context');
    return object(object(read.body).tenant).id;
  }).toBe('tenant_f2_foreign');
  return idle(page);
}

export async function logout(page: Page): Promise<void> {
  /** 退出沿当前真实账户菜单，不删除认证存储冒充用户操作。 */
  await page.goto('/enterprise/feedback');
  await page.getByRole('button', { name: '账户菜单', exact: true }).click();
  await page.getByRole('menuitem', { name: '退出登录', exact: true }).click();
  await expect(page.getByRole('button', { name: /进入平台/ })).toBeVisible();
}

export async function switchStaff(page: Page, name: string): Promise<void> {
  /** 原员工菜单驱动scope切换，浏览器不得直接改scope键。 */
  await page.getByLabel('切换当前员工', { exact: true }).click();
  await page.getByRole('menuitem', { name: new RegExp(name) }).click();
  await expect(page.getByLabel('切换当前员工', { exact: true })).toContainText(name);
}

export async function downgradeManager(page: Page): Promise<void> {
  /** 管理员真实成员编辑撤销经理权限，后台auth_revision必须单调前进。 */
  await page.goto('/enterprise/accounts');
  const row = page.getByRole('row').filter({ hasText: 'f1_reviewer' });
  await row.getByLabel('账号操作').click();
  await page.getByRole('menuitem', { name: '编辑', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '编辑成员：f1_reviewer' });
  await dialog.getByText('平台角色', { exact: true }).locator('..').getByRole('combobox').click();
  await page.getByRole('option', { name: '普通成员', exact: true }).click();
  const changed = page.waitForResponse(row => new URL(row.url()).pathname === '/api/auth/users/f1_reviewer'
    && row.request().method() === 'PUT');
  await dialog.getByRole('button', { name: '保存', exact: true }).click();
  expect((await changed).status()).toBe(200);
  await expect(dialog).toHaveCount(0);
}

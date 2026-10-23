/**
 * @Time       : 2026/10/03
 * @Author     : zhanglp8181
 * @File       : sd8-governance.ui.fullstack.e2e.ts
 * @CallChain  : Chromium → 原知识治理/员工切换 → 实际权限资源API
 * @Description: 独立双库预检分页定位、撤权及未绑定员工，不替代完整模型质量旅程。
 */
import { expect, test, type Page } from '@playwright/test';
import { observeAssertion } from './sd8-safe-observation';

const KB = 'SD8 CYPRESS42 政策库';
const OWNER = 'agent_sd8_owner';
const VIEWER = 'agent_sd8_viewer';
const EMPTY = 'agent_sd8_empty';

function object(value: unknown): Record<string, unknown> {
  /** 仅在原JSON对象边界收窄，未知错误不能被当成成功资源。 */
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('OBJECT_REQUIRED');
  return value as Record<string, unknown>;
}

async function request(page: Page, path: string) {
  /** 正式资源API只读旁证，JWT只留隔离浏览器不归档。 */
  return page.evaluate(async path => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch(path, { headers: { Authorization: `Bearer ${auth.token || ''}` } });
    return { status: response.status, body: await response.json() as unknown };
  }, path);
}

async function login(page: Page, viewer = false) {
  /** 原真实登录合成账号，不设置权限、发布头或成功执行账本。 */
  await page.goto('/enterprise/dashboard');
  const status = await page.evaluate(async ({ viewer, agent }) => {
    const response = await fetch('/api/auth/login', { method: 'POST', headers: {
      'Content-Type': 'application/json' }, body: JSON.stringify({ tenant_id: 'tenant_demo',
      username: viewer ? 'sd8-viewer' : 'admin', password: viewer ? 'sd8-viewer' : 'admin' }) });
    if (response.ok) {
      localStorage.setItem('gongge_auth', JSON.stringify(await response.json()));
      localStorage.setItem('gongge_enterprise_agent_scope', agent);
      localStorage.setItem('gongge_onboarding_guide_seen', '1');
      localStorage.setItem('gongge_quick_start_guide_seen', '1');
    }
    return response.status;
  }, { viewer, agent: viewer ? VIEWER : OWNER });
  expect(status).toBe(200);
}

test('SD8 独立治理分页/撤权/未绑定视角预检', async ({ page, browser }, info) => {
  test.setTimeout(180_000);
  page.setDefaultTimeout(30_000);
  await login(page);
  const initial = object(object((await request(page, '/__e2e/sd8/state')).body).initial);
  const document = String(initial.old_document_id);
  const viewerContext = await browser.newContext();
  const viewerPage = await viewerContext.newPage();
  viewerPage.setDefaultTimeout(30_000);
  await login(viewerPage, true);
  await viewerPage.goto('/enterprise/knowledge');
  await viewerPage.getByText(KB, { exact: true }).last().click();
  await viewerPage.getByRole('button', { name: '详情', exact: true }).last().click();
  await expect(viewerPage.getByRole('dialog', { name: '文档详情' })).toContainText('600');
  const content = (agent: string) => `/api/enterprise/knowledge/documents/${document}/content?tenant_id=tenant_demo&agent_id=${agent}`;
  expect((await request(viewerPage, content(VIEWER))).status).toBe(200);
  await page.goto('/enterprise/knowledge');
  await expect(page.getByLabel('切换当前员工', { exact: true })).toContainText('SD8 知识员工');
  await page.getByPlaceholder('搜索知识库名称、描述、状态或版本').fill(KB);
  await expect(page.getByRole('row').filter({ has: page.getByText(KB, { exact: true }) }))
    .toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole('button', { name: '刷新', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: '平台知识治理', exact: true }).click();
  await expect(page.getByText('知识库广场列表', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '刷新', exact: true })).toBeEnabled();
  await page.getByPlaceholder('搜索知识库名称、描述、状态或版本').fill(KB);
  const listed = await request(page, '/api/enterprise/knowledge-bases?tenant_id=tenant_demo');
  const rows = listed.body;
  await info.attach('sd8-safe-criterion', { body: Buffer.from(JSON.stringify({
    criterion: 'governance-target-listed', status: listed.status,
    expected_kb_listed: Array.isArray(rows) && rows.some(row => object(row).id === initial.knowledge_base_id),
    platform_scope_empty: await page.evaluate(() => !localStorage.getItem('gongge_enterprise_agent_scope')),
  })), contentType: 'application/json' });
  const row = page.getByRole('row').filter({ has: page.getByText(KB, { exact: true }) });
  await observeAssertion(() => expect(row).toBeVisible({ timeout: 30_000 }), async () => {
    await info.attach('sd8-safe-criterion', { body: Buffer.from(JSON.stringify({
      criterion: 'governance-target-ui-state',
      platform_caption_visible: await page.getByText('知识库广场列表', { exact: true }).isVisible(),
      target_rows: await row.count(),
      target_text_count: await page.getByText(KB, { exact: true }).count(),
      search_is_fixed_kb: await page.getByPlaceholder('搜索知识库名称、描述、状态或版本').inputValue() === KB,
      refresh_enabled: await page.getByRole('button', { name: '刷新', exact: true }).isEnabled(),
      platform_scope_empty: await page.evaluate(() => !localStorage.getItem('gongge_enterprise_agent_scope')),
    })), contentType: 'application/json' });
  });
  await row.getByRole('button', { name: '知识库操作', exact: true }).click();
  await page.getByRole('menuitem', { name: '访问治理', exact: true }).click();
  await page.getByRole('combobox', { name: '知识访问范围' }).click();
  await page.getByRole('option', { name: '仅所有者', exact: true }).click();
  await page.getByRole('button', { name: '保存治理范围', exact: true }).click();
  await expect(page.getByRole('combobox', { name: '知识访问范围' })).not.toBeVisible();
  await viewerPage.reload();
  await expect(viewerPage.getByRole('dialog', { name: '文档详情' })).not.toBeVisible();
  const denied = await request(viewerPage, content(VIEWER));
  expect([403, 404]).toContain(denied.status);
  await page.getByLabel('切换当前员工', { exact: true }).click();
  await page.getByRole('menuitem', { name: /SD8 知识员工/ }).click();
  await expect.poll(() => page.evaluate(() => localStorage.getItem('gongge_enterprise_agent_scope'))).toBe(OWNER);
  await page.goto('/enterprise/knowledge');
  expect((await request(page, content(OWNER))).status).toBe(200);
  await page.getByLabel('切换当前员工', { exact: true }).click();
  await page.getByRole('menuitem', { name: /SD8 无知识员工/ }).click();
  await expect.poll(() => page.evaluate(() => localStorage.getItem('gongge_enterprise_agent_scope'))).toBe(EMPTY);
  await page.goto('/enterprise/knowledge');
  await page.getByRole('button', { name: '刷新', exact: true }).click();
  await expect(page.getByText(KB, { exact: true })).toHaveCount(0);
  expect([403, 404]).toContain((await request(page, content(EMPTY))).status);
  const final = object((await request(page, '/__e2e/sd8/state')).body);
  if (!Array.isArray(final.sdk_calls)) throw new Error('SDK_CALLS_REQUIRED');
  const calls = final.sdk_calls.map(object);
  expect(calls.every(x => x.operation === 'llm.request')).toBe(true);
  expect(final.instances).toEqual([]);
  expect(final.operations).toEqual([]);
  await info.attach('sd8-safe-criterion', { body: Buffer.from(JSON.stringify({
    criterion: 'governance-ui-precheck', passed: true, real_provider_quality: false,
    viewer_source_before: 200, viewer_source_after: denied.status,
    legitimate_owner_source: 200, unbound_source_denied: true,
    actual_sdk_calls: calls.length, sdk_operations: calls.map(x => x.operation),
    actual_instances: 0, actual_operations: 0,
  })), contentType: 'application/json' });
  await viewerContext.close();
});

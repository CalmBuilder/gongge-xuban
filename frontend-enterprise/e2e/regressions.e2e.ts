import { expect, test, type Page, type Route } from '@playwright/test';

const ADMIN_USER = {
  id: 'user_e2e_admin',
  tenant_id: 'tenant_demo',
  username: 'e2e-admin',
  display_name: 'E2E Admin',
  role: 'admin',
  membership_status: 'active',
  member_category_code: 'employee',
  governance_permission_codes: ['knowledge.read', 'knowledge.manage'],
};

async function installAuthenticatedApiMocks(page: Page) {
  page.on('pageerror', (error) => console.error(`browser page error: ${error.message}`));
  page.on('console', (message) => {
    if (message.type() === 'error') console.error(`browser console error: ${message.text()}`);
  });
  await page.addInitScript((user) => {
    window.localStorage.clear();
    window.localStorage.setItem(
      'gongge_auth',
      JSON.stringify({ token: 'e2e-token', user }),
    );
  }, ADMIN_USER);

  await page.route('http://127.0.0.1:4174/api/**', async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/auth/me') {
      await fulfillJson(route, ADMIN_USER);
      return;
    }
    if (url.pathname === '/api/auth/context') {
      await fulfillJson(route, {
        tenant: { id: ADMIN_USER.tenant_id, name: 'E2E 企业' },
        member: ADMIN_USER,
        is_administrator: true,
      });
      return;
    }
    if (url.pathname === '/api/enterprise/expert-taxonomy') {
      await fulfillJson(route, { version: 1, categories: [] });
      return;
    }
    if (url.pathname === '/api/enterprise/skills/files/extract') {
      const payload = route.request().postDataJSON() as { filename: string };
      await fulfillJson(route, {
        filename: payload.filename,
        text: `浏览器回归内容：${payload.filename}`,
      });
      return;
    }
    await fulfillJson(route, []);
  });
}

async function fulfillJson(route: Route, body: unknown) {
  await route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(body),
  });
}

test('欢迎和快速入门结束前隐藏模型提示，完成十步引导后再显示', async ({ page }) => {
  await installAuthenticatedApiMocks(page);
  await page.goto('/enterprise/dashboard');

  const modelNotice = page.getByText(
    '还没有可用模型配置，数字员工暂不能调用模型。请先完成模型配置。',
  );
  const onboardingDialog = page.getByRole('dialog');
  await expect(onboardingDialog).toBeVisible();
  await expect(modelNotice).toBeHidden();

  await onboardingDialog.getByText('下一步', { exact: true }).click();
  await onboardingDialog.getByRole('button', { name: '开始使用' }).click();

  await expect(onboardingDialog).toBeHidden();
  const quickStart = page.locator('[data-slot="popover-content"][aria-label="快速入门"]');
  await expect(quickStart).toBeVisible();
  await expect(quickStart.getByText('先接入数字员工的大脑', { exact: true })).toBeVisible();
  await expect(modelNotice).toBeHidden();

  const remainingTitles = [
    '创建你的数字员工',
    '从开放广场复用能力',
    '查看员工档案',
    '安排定时任务',
    '管理员工记忆',
    '沉淀业务知识',
    '扩展通用技能',
    '编排标准作业流程',
    '开始与数字员工协作',
  ];
  for (const title of remainingTitles) {
    await quickStart.getByRole('button', { name: '下一步' }).click();
    await expect(quickStart.getByText(title, { exact: true })).toBeVisible();
  }
  await expect(quickStart.getByText('开始与数字员工协作', { exact: true })).toBeVisible();
  await expect(modelNotice).toBeHidden();

  await quickStart.getByRole('button', { name: '开始协作' }).click();
  await expect(page).toHaveURL(/\/workspace\/gallery$/);
  expect(await page.evaluate(() => localStorage.getItem('gongge_quick_start_guide_seen'))).toBe('1');

  await page.goBack();
  await expect(page).toHaveURL(/\/enterprise\/skills$/);
  await expect(modelNotice).toBeVisible();
});

test('快速入门在窄屏中保持完整可操作', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await installAuthenticatedApiMocks(page);
  await page.addInitScript(() => {
    window.localStorage.setItem('gongge_onboarding_guide_seen', '1');
  });
  await page.goto('/enterprise/dashboard');

  const quickStart = page.locator('[data-slot="popover-content"][aria-label="快速入门"]');
  await expect(quickStart).toBeVisible();
  const bounds = await quickStart.boundingBox();
  expect(bounds).not.toBeNull();
  expect(bounds!.x).toBeGreaterThanOrEqual(0);
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(390);
  await expect(quickStart.getByRole('button', { name: '配置模型' })).toBeVisible();
  await expect(quickStart.getByRole('button', { name: '下一步' })).toBeVisible();

  const spotlight = page.locator('[data-slot="popover-anchor"]');
  await expect(spotlight).toHaveCSS('box-shadow', /rgba\(24, 33, 61/);
  await quickStart.getByRole('button', { name: '关闭快速入门' }).click();
  await expect(quickStart).toBeHidden();
  await expect(spotlight).toHaveCSS('box-shadow', 'none');
  await expect(spotlight).toHaveCSS('opacity', '0');
  expect(await page.evaluate(() => localStorage.getItem('gongge_quick_start_guide_seen'))).toBe('1');
});

test('蒸馏输入框和附件列表在长内容下保持固定区域滚动', async ({ page }) => {
  await installAuthenticatedApiMocks(page);
  await page.addInitScript(() => {
    window.localStorage.setItem('gongge_onboarding_guide_seen', '1');
    window.localStorage.setItem('gongge_quick_start_guide_seen', '1');
  });
  await page.goto('/enterprise/skills/distill?mode=create');

  const composer = page.getByPlaceholder('输入或粘贴需要整理的 SOP 流程说明');
  await expect(composer).toBeVisible();
  await composer.fill(Array.from({ length: 120 }, (_, index) => `流程步骤 ${index + 1}`).join('\n'));

  const composerMetrics = await composer.evaluate((element) => {
    const textarea = element as HTMLTextAreaElement;
    const style = window.getComputedStyle(textarea);
    return {
      height: textarea.getBoundingClientRect().height,
      clientHeight: textarea.clientHeight,
      scrollHeight: textarea.scrollHeight,
      overflowY: style.overflowY,
    };
  });
  expect(composerMetrics.height).toBeGreaterThanOrEqual(110);
  expect(composerMetrics.height).toBeLessThanOrEqual(114);
  expect(composerMetrics.scrollHeight).toBeGreaterThan(composerMetrics.clientHeight);
  expect(composerMetrics.overflowY).toBe('auto');

  const files = Array.from({ length: 10 }, (_, index) => ({
    name: `fixture-${String(index + 1).padStart(2, '0')}-very-long-document-name.txt`,
    mimeType: 'text/plain',
    buffer: Buffer.from(`fixture ${index + 1}`),
  }));
  await page.locator('input[type="file"]').setInputFiles(files);
  await expect(page.getByText(files.at(-1)!.name)).toBeVisible();

  const uploadList = page.getByText(files[0].name).locator('..').locator('..');
  const listMetrics = await uploadList.evaluate((element) => {
    const style = window.getComputedStyle(element);
    return {
      clientHeight: element.clientHeight,
      scrollHeight: element.scrollHeight,
      overflowY: style.overflowY,
    };
  });
  expect(listMetrics.clientHeight).toBeLessThanOrEqual(168);
  expect(listMetrics.scrollHeight).toBeGreaterThan(listMetrics.clientHeight);
  expect(listMetrics.overflowY).toBe('auto');
});

test('知识治理可退出员工范围且文档接口失败不清空知识库', async ({ page }) => {
  await installAuthenticatedApiMocks(page);
  await page.addInitScript(() => {
    window.localStorage.setItem('gongge_onboarding_guide_seen', '1');
    window.localStorage.setItem('gongge_quick_start_guide_seen', '1');
  });
  await page.route('**/api/enterprise/agents?**', async (route) => {
    await fulfillJson(route, [{
      id: 'agent_legal',
      tenant_id: 'tenant_demo',
      name: '法务数字员工',
      description: '',
      status: 'active',
      is_overall: false,
      metadata: {
        owner_user_id: ADMIN_USER.id,
        role_name: '法务',
        is_default_employee: true,
      },
    }]);
  });
  await page.route('**/api/enterprise/knowledge/documents?**', async (route) => {
    await route.fulfill({
      status: 404,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'forced isolated regression failure' }),
    });
  });
  await page.route('**/api/enterprise/knowledge-bases?**', async (route) => {
    await fulfillJson(route, [{
      id: 'kb_policy',
      tenant_id: 'tenant_demo',
      name: '研究院制度库',
      description: '制度资料',
      status: 'active',
      owner_user_id: ADMIN_USER.id,
      responsible_org_unit_id: null,
      access_scope: 'owner',
      download_policy: 'restricted',
      revision: 1,
      organization_access: [],
      version: '1.0.0',
      metadata: { creator_name: '管理员' },
      document_count: 0,
      bucket_count: 0,
      chunk_count: 0,
      created_at: '2026-07-28T00:00:00Z',
      updated_at: '2026-07-28T00:00:00Z',
    }]);
  });

  await page.goto('/enterprise/knowledge');
  await page.getByRole('button', { name: '平台知识治理' }).click();

  await expect(page.getByRole('row').filter({ hasText: '研究院制度库' })).toBeVisible();
  await expect(
    page.getByText('部分知识数据加载失败：文档列表。已保留其他可用数据。').first(),
  ).toBeVisible();
  await expect(page.getByText('Not Found', { exact: true })).toHaveCount(0);
  expect(await page.evaluate(() => localStorage.getItem('gongge_enterprise_agent_scope'))).toBeNull();
});

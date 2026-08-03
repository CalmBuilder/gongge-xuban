import { expect, test, type Page, type Response } from '@playwright/test';

const AGENT_ID = 'agent_e2e_employee';

async function loginAsAdmin(page: Page) {
  await page.goto('/enterprise/dashboard');
  const status = await page.evaluate(async (agentId) => {
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
      localStorage.setItem('gongge_enterprise_agent_scope', agentId);
    }
    return response.status;
  }, AGENT_ID);
  expect(status).toBe(200);
}

function pageTwoResponse(page: Page, path: string): Promise<Response> {
  return page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === path && url.searchParams.get('page') === '2';
  });
}

async function expectSuccessfulPageTwo(
  page: Page,
  navigationLabel: string,
  endpointPath: string,
) {
  const navigation = page.getByRole('navigation', { name: navigationLabel });
  await expect(navigation).toBeVisible();
  const responsePromise = pageTwoResponse(page, endpointPath);
  await navigation.getByRole('button', { name: '下一页' }).click();
  const response = await responsePromise;
  expect(response.status()).toBe(200);
  const body = await response.json() as { page: number; items: unknown[] };
  expect(body.page).toBe(2);
  expect(body.items.length).toBeGreaterThan(0);
}

test('真实 Chromium 逐页回归全部服务端分页入口', async ({ page }) => {
  const failures: string[] = [];
  page.on('pageerror', (error) => failures.push(`pageerror: ${error.message}`));
  page.on('response', (response) => {
    const path = new URL(response.url()).pathname;
    if (path.startsWith('/api/') && response.status() >= 500) {
      failures.push(`${response.status()} ${path}`);
    }
  });

  await loginAsAdmin(page);

  await page.goto('/workspace/gallery?view=mine&sub=owned');
  await expect(page.getByText(/浏览器分页员工/).first()).toBeVisible();
  await expectSuccessfulPageTwo(
    page,
    '数字员工分页',
    '/api/enterprise/agents/gallery-page',
  );

  await page.goto('/enterprise/work-items');
  await expect(page.getByRole('paragraph').filter({ hasText: '流程任务箱' })).toBeVisible();
  await expectSuccessfulPageTwo(page, '流程任务分页', '/api/work-items/page');

  await page.goto('/enterprise/scheduled-tasks');
  await expect(page.getByRole('tab', { name: '定时任务' })).toHaveAttribute('data-state', 'active');
  await expect(page.getByText('档案部分数据加载失败：定时任务。')).toHaveCount(0);
  await expectSuccessfulPageTwo(
    page,
    '任务列表分页',
    '/api/enterprise/scheduled-tasks/page',
  );
  await expectSuccessfulPageTwo(
    page,
    '执行记录分页',
    '/api/enterprise/scheduled-tasks/runs/page',
  );

  await page.goto('/enterprise/memories');
  await expect(page.getByRole('tab', { name: '记忆' })).toHaveAttribute('data-state', 'active');
  await expectSuccessfulPageTwo(
    page,
    '员工记忆分页',
    '/api/enterprise/memories/page',
  );

  await page.goto('/enterprise/feedback');
  await expect(page.getByRole('tab', { name: '对话日志' })).toHaveAttribute('data-state', 'active');
  await expectSuccessfulPageTwo(
    page,
    '对话日志分页',
    '/api/enterprise/sessions/page',
  );

  expect(failures).toEqual([]);
});

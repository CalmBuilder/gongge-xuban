import { expect, test, type Page, type Response } from '@playwright/test';

const AGENT_ID = 'agent_e2e_employee';
const OTHER_AGENT_ID = 'agent_e2e_memory_other';

async function loginAs(page: Page, username: string, password: string, agentId: string) {
  await page.goto('/enterprise/dashboard');
  const status = await page.evaluate(async ({ agentId: nextAgentId, password: nextPassword, username: nextUsername }) => {
    const response = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tenant_id: 'tenant_demo', username: nextUsername, password: nextPassword }),
    });
    const body = await response.json();
    if (response.ok) {
      localStorage.setItem('gongge_auth', JSON.stringify(body));
      localStorage.setItem('gongge_onboarding_guide_seen', '1');
      localStorage.setItem('gongge_quick_start_guide_seen', '1');
      localStorage.setItem('gongge_enterprise_agent_scope', nextAgentId);
    }
    return response.status;
  }, { agentId, password, username });
  expect(status).toBe(200);
}

async function loginAsAdmin(page: Page) {
  await loginAs(page, 'admin', 'admin', AGENT_ID);
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
  await expect(page.getByText('SOP 流程任务', { exact: true })).toBeVisible();
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

test('真实 Chromium 验证员工记忆历史、搜索、切换和本人清空权限', async ({ page }) => {
  await loginAsAdmin(page);
  await page.goto('/enterprise/memories');
  await expect(page.getByRole('tab', { name: '记忆' })).toHaveAttribute('data-state', 'active');
  const memoryTable = page.getByRole('table', { name: '员工记忆' });
  await expect(memoryTable.getByText('浏览器清空个人记忆')).toBeVisible();

  const wildcardSearches = await page.evaluate(async () => {
    const queries = ['%%%%%%%%b', '%a%a%a%a%a%a%a%a%b'];
    return Promise.all(queries.map(async (query) => {
      const controller = new AbortController();
      const timeoutId = window.setTimeout(() => controller.abort(), 5000);
      const params = new URLSearchParams({
        tenant_id: 'tenant_demo',
        agent_id: 'agent_e2e_employee',
        page: '1',
        page_size: '10',
        q: query,
      });
      try {
        const auth = JSON.parse(localStorage.getItem('gongge_auth') ?? '{}') as { token?: string };
        const response = await fetch(`/api/enterprise/memories/page?${params}`, {
          headers: auth.token ? { Authorization: `Bearer ${auth.token}` } : {},
          signal: controller.signal,
        });
        const body = await response.json() as { total: number };
        return { status: response.status, total: body.total };
      } catch {
        return { status: 0, total: -1 };
      } finally {
        window.clearTimeout(timeoutId);
      }
    }));
  });
  expect(wildcardSearches).toHaveLength(2);
  for (const result of wildcardSearches) {
    expect(result.status).toBe(200);
    expect(Number.isInteger(result.total)).toBe(true);
  }

  const obsoleteValueResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === '/api/enterprise/memories/page'
      && url.searchParams.get('agent_id') === AGENT_ID
      && url.searchParams.get('q') === '浏览器覆盖前的旧值';
  });
  await page.getByPlaceholder('用户名、用户 ID、记忆内容').fill('浏览器覆盖前的旧值');
  await page.getByRole('button', { name: '查询' }).click();
  const obsoleteBody = await obsoleteValueResponse;
  expect(obsoleteBody.status()).toBe(200);
  expect((await obsoleteBody.json() as { total: number }).total).toBe(0);
  await expect(memoryTable.getByText('浏览器覆盖前的旧值')).toHaveCount(0);

  const overwrittenValueResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === '/api/enterprise/memories/page'
      && url.searchParams.get('agent_id') === AGENT_ID
      && url.searchParams.get('q') === '浏览器覆盖后的新值';
  });
  await page.getByPlaceholder('用户名、用户 ID、记忆内容').fill('浏览器覆盖后的新值');
  await page.getByRole('button', { name: '查询' }).click();
  const overwrittenBody = await overwrittenValueResponse;
  expect(overwrittenBody.status()).toBe(200);
  expect((await overwrittenBody.json() as { total: number }).total).toBe(1);
  await expect(memoryTable.getByText('浏览器覆盖后的新值')).toBeVisible();

  const historySearchResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === '/api/enterprise/memories/page'
      && url.searchParams.get('agent_id') === AGENT_ID
      && url.searchParams.get('q') === '浏览器历史归属记忆';
  });
  await page.getByPlaceholder('用户名、用户 ID、记忆内容').fill('浏览器历史归属记忆');
  await page.getByRole('button', { name: '查询' }).click();
  const historyResponse = await historySearchResponse;
  expect(historyResponse.status()).toBe(200);
  const historyBody = await historyResponse.json() as { total: number; items: Array<{ user_id: string }> };
  expect(historyBody.total).toBe(1);
  expect(historyBody.items.every((item) => item.user_id === 'admin')).toBe(true);
  await expect(memoryTable.getByText('浏览器历史归属记忆')).toBeVisible();

  const resetResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === '/api/enterprise/memories/page'
      && url.searchParams.get('agent_id') === AGENT_ID
      && !url.searchParams.has('q')
      && url.searchParams.get('page') === '1';
  });
  await page.getByRole('button', { name: '重置' }).click();
  expect((await resetResponse).status()).toBe(200);
  await expect(memoryTable.getByText('浏览器清空个人记忆')).toBeVisible();

  const refreshResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === '/api/enterprise/memories/page'
      && url.searchParams.get('agent_id') === AGENT_ID
      && url.searchParams.get('page') === '1';
  });
  await page.reload();
  expect((await refreshResponse).status()).toBe(200);
  await expect(memoryTable.getByText('浏览器清空个人记忆')).toBeVisible();

  page.once('dialog', (dialog) => dialog.accept());
  const deleteResponse = page.waitForResponse((response) => (
    new URL(response.url()).pathname === '/api/enterprise/memories/me'
      && response.request().method() === 'DELETE'
  ));
  const afterClearResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === '/api/enterprise/memories/page'
      && url.searchParams.get('agent_id') === AGENT_ID
      && url.searchParams.get('page') === '1';
  });
  await page.getByRole('button', { name: '清空我的记忆' }).click();
  const cleared = await deleteResponse;
  expect(cleared.status()).toBe(200);
  expect((await cleared.json() as { deleted: number }).deleted).toBeGreaterThanOrEqual(2);
  expect((await afterClearResponse).status()).toBe(200);

  const clearedSearchResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === '/api/enterprise/memories/page'
      && url.searchParams.get('agent_id') === AGENT_ID
      && url.searchParams.get('q') === '浏览器清空个人记忆';
  });
  await page.getByPlaceholder('用户名、用户 ID、记忆内容').fill('浏览器清空个人记忆');
  await page.getByRole('button', { name: '查询' }).click();
  const clearedBody = await clearedSearchResponse;
  expect((await clearedBody.json() as { total: number }).total).toBe(0);
  await expect(memoryTable.getByText('浏览器清空个人记忆')).toHaveCount(0);

  const preSwitchResetResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === '/api/enterprise/memories/page'
      && url.searchParams.get('agent_id') === AGENT_ID
      && !url.searchParams.has('q')
      && url.searchParams.get('page') === '1';
  });
  await page.getByRole('button', { name: '重置' }).click();
  expect((await preSwitchResetResponse).status()).toBe(200);

  const switchResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === '/api/enterprise/memories/page'
      && url.searchParams.get('agent_id') === OTHER_AGENT_ID
      && url.searchParams.get('page') === '1';
  });
  await page.evaluate((agentId) => {
    localStorage.setItem('gongge_enterprise_agent_scope', agentId);
    window.dispatchEvent(new CustomEvent('gongge-enterprise-agent-scope-change', { detail: { agentId } }));
  }, OTHER_AGENT_ID);
  const switched = await switchResponse;
  expect(switched.status()).toBe(200);
  await expect(memoryTable.getByText('浏览器其他员工不应混入')).toBeVisible();

  const switchedRefreshResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === '/api/enterprise/memories/page'
      && url.searchParams.get('agent_id') === OTHER_AGENT_ID
      && url.searchParams.get('page') === '1';
  });
  await page.reload();
  expect((await switchedRefreshResponse).status()).toBe(200);
  await expect(memoryTable.getByText('浏览器其他员工不应混入')).toBeVisible();

  await loginAs(page, 'member', 'member', AGENT_ID);
  await page.goto('/enterprise/memories');
  await expect(page.getByRole('button', { name: '清空我的记忆' })).toBeVisible();
  const memberResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === '/api/enterprise/memories/page'
      && url.searchParams.get('agent_id') === AGENT_ID
      && url.searchParams.get('page') === '1';
  });
  await page.evaluate((agentId) => {
    localStorage.setItem('gongge_enterprise_agent_scope', agentId);
    window.dispatchEvent(new CustomEvent('gongge-enterprise-agent-scope-change', { detail: { agentId } }));
  }, AGENT_ID);
  const memberPageResponse = await memberResponse;
  expect(memberPageResponse.status()).toBe(200);
  const memberBody = await memberPageResponse.json() as {
    items: Array<{ user_id: string; memories: Array<{ content: string }> }>;
  };
  expect(memberBody.items.every((item) => item.user_id === 'member_e2e')).toBe(true);
  await expect(page.getByRole('table', { name: '员工记忆' }).getByText('浏览器成员个人记忆')).toBeVisible();
  await expect(page.getByRole('table', { name: '员工记忆' }).getByText('浏览器清空个人记忆')).toHaveCount(0);
});

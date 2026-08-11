/**
 * @Time       : 2026/08/11
 * @Author     : zhanglp8181
 * @File       : skill-distillation-s0.fullstack.e2e.ts
 * @CallChain  : Playwright Chromium → 构建后 React → 真实 FastAPI/隔离 SQLite
 * @Description: 固定 Skill 蒸馏 S0 的管理员、成员 Agent scope 与普通问答浏览器基线。
 */

import { expect, test, type Page } from '@playwright/test';

type BrowserFailure = {
  kind: 'console' | 'pageerror' | 'request' | 'response';
  detail: string;
};

async function login(
  page: Page,
  username: string,
  password: string,
  agentId?: string,
) {
  const status = await page.evaluate(async (credentials) => {
    localStorage.setItem('gongge_onboarding_guide_seen', '1');
    localStorage.setItem('gongge_quick_start_guide_seen', '1');
    if (credentials.agentId) {
      localStorage.setItem('gongge_enterprise_agent_scope', credentials.agentId);
    } else {
      localStorage.removeItem('gongge_enterprise_agent_scope');
    }
    const response = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        username: credentials.username,
        password: credentials.password,
      }),
    });
    const body = await response.json();
    if (response.ok) localStorage.setItem('gongge_auth', JSON.stringify(body));
    return response.status;
  }, { username, password, agentId });
  expect(status).toBe(200);
}

function collectBrowserFailures(page: Page): BrowserFailure[] {
  const failures: BrowserFailure[] = [];
  page.on('pageerror', (error) => failures.push({ kind: 'pageerror', detail: error.message }));
  page.on('console', (message) => {
    if (message.type() === 'error') {
      failures.push({ kind: 'console', detail: message.text() });
    }
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

test('S0 管理员在真实浏览器看到现有通用技能广场和新建入口', async ({ page }) => {
  const failures = collectBrowserFailures(page);
  await page.goto('/enterprise/dashboard');
  await login(page, 'admin', 'admin');
  const slug = 's0-browser-baseline';

  try {
    const created = await page.evaluate(async (skillSlug) => {
      const rawSession = localStorage.getItem('gongge_auth');
      if (!rawSession) throw new Error('登录后未保存认证会话');
      const token = (JSON.parse(rawSession) as { token: string }).token;
      const response = await fetch('/api/enterprise/general-skills/import', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({
          tenant_id: 'tenant_demo',
          slug: skillSlug,
          name: 'S0 浏览器基线技能',
          markdown: '# S0 浏览器基线技能\n',
          status: 'published',
          usage_mode: 'atomic_execution',
        }),
      });
      return { status: response.status, body: await response.json() };
    }, slug);
    expect(created.status).toBe(200);

    const listResponse = page.waitForResponse((response) => (
      new URL(response.url()).pathname === '/api/enterprise/general-skills'
      && response.request().method() === 'GET'
    ));
    await page.goto('/enterprise/general-skills');
    expect((await listResponse).status()).toBe(200);

    await expect(page.getByLabel('技能统计')).toBeVisible();
    await expect(
      page.getByRole('row').filter({ hasText: 'S0 浏览器基线技能' }),
    ).toBeVisible();
    await page.getByRole('button', { name: /新增/ }).click();
    await expect(page.getByText('新建技能', { exact: true })).toBeVisible();
    expect(failures).toEqual([]);
  } finally {
    const deleted = await page.evaluate(async (skillSlug) => {
      const rawSession = localStorage.getItem('gongge_auth');
      if (!rawSession) return 0;
      const token = (JSON.parse(rawSession) as { token: string }).token;
      const agentsResponse = await fetch('/api/enterprise/agents?tenant_id=tenant_demo', {
        headers: { Authorization: `Bearer ${token}` },
      });
      const agents = await agentsResponse.json() as Array<{ id: string; is_overall: boolean }>;
      const overallAgent = agents.find((item) => item.is_overall);
      if (!overallAgent) throw new Error('隔离租户缺少整体员工');
      const response = await fetch(
        `/api/enterprise/general-skills/${skillSlug}?tenant_id=tenant_demo&agent_id=${overallAgent.id}`,
        { method: 'DELETE', headers: { Authorization: `Bearer ${token}` } },
      );
      return response.status;
    }, slug);
    expect(deleted).toBe(200);
  }
});

test('S0 普通用户在真实浏览器只进入本人数字员工的技能范围', async ({ page }) => {
  const failures = collectBrowserFailures(page);
  await page.goto('/enterprise/dashboard');
  await login(page, 'member', 'member', 'agent_e2e_member_employee');

  const listResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === '/api/enterprise/general-skills'
      && url.searchParams.get('agent_id') === 'agent_e2e_member_employee';
  });
  await page.goto('/enterprise/general-skills');
  const response = await listResponse;
  expect(response.status()).toBe(200);
  expect(await response.json()).toEqual([]);

  await expect(page.getByText('技能', { exact: true }).first()).toBeVisible();
  await expect(page.getByRole('cell', { name: '当前员工暂无技能' })).toBeVisible();
  await expect(page.getByRole('button', { name: /新增/ })).toBeVisible();
  await expect(page.getByText('中国城市天气', { exact: true })).toHaveCount(0);
  expect(failures).toEqual([]);
});

test('S0 真实浏览器保留普通问答展示基线且不创建动态任务', async ({ page }) => {
  const failures = collectBrowserFailures(page);
  await page.goto('/enterprise/dashboard');
  await login(page, 'admin', 'admin');

  await page.goto('/workspace/chat/session_e2e_dynamic_artifact');

  await expect(page.getByText('普通问答仍可正常展示，不会创建新的动态执行。')).toBeVisible();
  await expect(page.getByText('Not Found', { exact: true })).toHaveCount(0);
  expect(failures).toEqual([]);
});

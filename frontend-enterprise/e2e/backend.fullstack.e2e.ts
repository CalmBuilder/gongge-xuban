import { expect, test, type Page } from '@playwright/test';

type ApiResult = {
  status: number;
  body: { detail?: string };
};

async function login(page: Page, username: string, password: string, agentId?: string) {
  await page.evaluate(() => localStorage.setItem('gongge_onboarding_guide_seen', '1'));
  await page.evaluate(() => localStorage.setItem('gongge_quick_start_guide_seen', '1'));
  if (agentId) {
    await page.evaluate((value) => {
      localStorage.setItem('gongge_enterprise_agent_scope', value);
    }, agentId);
  }
  await page.getByRole('button', { name: '进入平台' }).click();
  await page.getByRole('textbox', { name: '账号' }).fill(username);
  await page.getByRole('textbox', { name: '密码', exact: true }).fill(password);
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect.poll(
    async () => page.evaluate(() => Boolean(localStorage.getItem('gongge_auth'))),
  ).toBe(true);
}

async function loginAsAdmin(page: Page, agentId?: string) {
  await login(page, 'admin', 'admin', agentId);
}

async function loginForTenant(
  page: Page,
  tenantId: string,
  username: string,
  password: string,
) {
  await page.goto('/enterprise/dashboard');
  const result = await page.evaluate(async (credentials) => {
    const response = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(credentials),
    });
    const body = await response.json();
    if (response.ok) {
      localStorage.setItem('gongge_auth', JSON.stringify(body));
      localStorage.setItem('gongge_onboarding_guide_seen', '1');
      localStorage.setItem('gongge_quick_start_guide_seen', '1');
    }
    return { status: response.status, detail: body.detail as string | undefined };
  }, { tenant_id: tenantId, username, password });
  expect(result, result.detail).toMatchObject({ status: 200 });
}

test('M5-A 知识治理在真实 SQLite 服务中默认私有并拒绝伪造组织', async ({ page }) => {
  await page.goto('/enterprise/knowledge');
  await loginAsAdmin(page);
  let knowledgeBaseId = '';

  try {
    const created = await page.evaluate(async () => {
      const rawSession = localStorage.getItem('gongge_auth');
      if (!rawSession) throw new Error('登录后未保存认证会话');
      const token = (JSON.parse(rawSession) as { token: string }).token;
      const response = await fetch('/api/enterprise/knowledge-bases', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({
          tenant_id: 'tenant_demo',
          name: 'M5-A SQLite 浏览器知识库',
          description: '隔离全栈回归',
        }),
      });
      return { status: response.status, body: await response.json() };
    });
    expect(created.status).toBe(200);
    expect(created.body).toMatchObject({
      owner_user_id: 'admin',
      access_scope: 'owner',
      download_policy: 'restricted',
      revision: 1,
    });
    knowledgeBaseId = created.body.id as string;

    const invalidOrganization = await page.evaluate(async (id) => {
      const rawSession = localStorage.getItem('gongge_auth');
      if (!rawSession) throw new Error('登录后未保存认证会话');
      const token = (JSON.parse(rawSession) as { token: string }).token;
      const response = await fetch(`/api/enterprise/knowledge-bases/${id}/governance`, {
        method: 'PUT',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({
          tenant_id: 'tenant_demo',
          expected_revision: 1,
          responsible_org_unit_id: null,
          access_scope: 'organization',
          download_policy: 'restricted',
          organization_access: [{
            org_unit_id: 'org_from_another_tenant',
            include_descendants: true,
          }],
        }),
      });
      return response.status;
    }, knowledgeBaseId);
    expect(invalidOrganization).toBe(400);

    await page.goto('/enterprise/knowledge');
    await page.getByRole('button', { name: '平台知识治理' }).click();
    const row = page.getByRole('row').filter({ hasText: 'M5-A SQLite 浏览器知识库' });
    await expect(row).toBeVisible();
    await row.getByRole('button', { name: '知识库操作' }).click();
    await page.getByText('访问治理', { exact: true }).click();
    const root = page.getByRole('tree', { name: '企业组织树' }).getByRole('treeitem').first();
    await expect(root).toBeVisible();
    await root.click();
    await page.getByRole('combobox', { name: '知识访问范围' }).click();
    await page.getByRole('option', { name: '指定组织' }).click();
    await page.getByRole('button', { name: '加入所选组织' }).click();
    const savedResponse = page.waitForResponse((response) => (
      response.url().includes(`/api/enterprise/knowledge-bases/${knowledgeBaseId}/governance`)
      && response.request().method() === 'PUT'
    ));
    await page.getByRole('button', { name: '保存治理范围' }).click();
    expect((await savedResponse).status()).toBe(200);

    const saved = await page.evaluate(async (id) => {
      const rawSession = localStorage.getItem('gongge_auth');
      if (!rawSession) throw new Error('登录后未保存认证会话');
      const token = (JSON.parse(rawSession) as { token: string }).token;
      const response = await fetch(
        `/api/enterprise/knowledge-bases/${id}?tenant_id=tenant_demo`,
        { headers: { Authorization: `Bearer ${token}` } },
      );
      return { status: response.status, body: await response.json() };
    }, knowledgeBaseId);
    expect(saved.status).toBe(200);
    expect(saved.body).toMatchObject({
      access_scope: 'organization',
      download_policy: 'restricted',
      revision: 2,
    });
    expect(saved.body.organization_access).toHaveLength(1);
  } finally {
    if (knowledgeBaseId) {
      const cleanupStatus = await page.evaluate(async (id) => {
        const rawSession = localStorage.getItem('gongge_auth');
        if (!rawSession) return 0;
        const token = (JSON.parse(rawSession) as { token: string }).token;
        const response = await fetch(
          `/api/enterprise/knowledge-bases/${id}?tenant_id=tenant_demo`,
          {
            method: 'DELETE',
            headers: { Authorization: `Bearer ${token}` },
          },
        );
        return response.status;
      }, knowledgeBaseId);
      expect(cleanupStatus).toBe(200);
    }
  }
});

test('M4-A 正式字段兼容旧 owner，并在真实 Chromium 展示企业广场员工', async ({ page }) => {
  const failures: string[] = [];
  page.on('pageerror', (error) => failures.push(`pageerror: ${error.message}`));
  page.on('response', (response) => {
    const path = new URL(response.url()).pathname;
    if (
      path.startsWith('/api/enterprise/agents')
      && (response.status() === 404 || response.status() >= 500)
    ) {
      failures.push(`${response.status()} ${path}`);
    }
  });

  await loginForTenant(page, 'tenant_demo', 'member', 'member');
  const result = await page.evaluate(async () => {
    const rawSession = localStorage.getItem('gongge_auth');
    if (!rawSession) throw new Error('登录后未保存认证会话');
    const token = (JSON.parse(rawSession) as { token: string }).token;
    const response = await fetch('/api/enterprise/agents?tenant_id=tenant_demo', {
      headers: { Authorization: `Bearer ${token}` },
    });
    return {
      status: response.status,
      rows: await response.json() as Array<{
        id: string;
        owner_user_id?: string;
        profile_revision: number;
        published_to_gallery: boolean;
        agent_category_code: string;
        visibility_scope: string;
      }>,
    };
  });

  expect(result.status).toBe(200);
  expect(result.rows).toContainEqual(expect.objectContaining({
    id: 'agent_e2e_member_employee',
    owner_user_id: 'member_e2e',
    profile_revision: 1,
  }));
  expect(result.rows).toContainEqual(expect.objectContaining({
    id: 'agent_e2e_gallery',
    owner_user_id: 'admin',
    published_to_gallery: true,
    agent_category_code: 'assistant',
    visibility_scope: 'tenant',
  }));

  await page.goto('/workspace/gallery');
  await page.getByRole('tab', { name: '发现' }).click();
  await expect(page.getByText(/^E2E 企业广场员工/)).toBeVisible();
  await expect(page.getByText('Not Found', { exact: true })).toHaveCount(0);
  expect(failures).toEqual([]);
});

test('管理员通过旧版密码哈希登录，并获得知识发现的 422/409 响应', async ({ page }) => {
  page.on('pageerror', (error) => console.error(`browser page error: ${error.message}`));

  await page.goto('/enterprise/dashboard');
  await loginAsAdmin(page);

  const results = await page.evaluate(async () => {
    const rawSession = localStorage.getItem('gongge_auth');
    if (!rawSession) throw new Error('登录后未保存认证会话');
    const token = (JSON.parse(rawSession) as { token: string }).token;
    const headers = { Authorization: `Bearer ${token}` };

    async function request(path: string): Promise<ApiResult> {
      const response = await fetch(path, { method: 'POST', headers });
      return { status: response.status, body: await response.json() as { detail?: string } };
    }

    const meResponse = await fetch('/api/auth/me', { headers });
    return {
      me: { status: meResponse.status, body: await meResponse.json() },
      invalid: await request(
        '/api/enterprise/knowledge/discoveries/kdisc_e2e_invalid/confirm?tenant_id=tenant_demo',
      ),
      handled: await request(
        '/api/enterprise/knowledge/discoveries/kdisc_e2e_handled/confirm?tenant_id=tenant_demo',
      ),
    };
  });

  expect(results.me.status).toBe(200);
  expect(results.me.body).toMatchObject({ username: 'admin', role: 'admin' });
  expect(results.invalid.status).toBe(422);
  expect(results.invalid.body.detail).toContain('共格·序伴技能格式');
  expect(results.handled.status).toBe(409);
  expect(results.handled.body.detail).toContain('只有待处理建议可以确认');
});

test('管理员从知识发现界面确认建议', async ({ page }) => {
  await page.goto('/enterprise/knowledge/new');
  await loginAsAdmin(page, 'agent_e2e_employee');

  const dialog = page.getByRole('dialog', { name: '发现可新增资源' });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText('浏览器确认技能', { exact: true })).toBeVisible();

  const confirmResponse = page.waitForResponse((response) =>
    response.url().includes('/discoveries/kdisc_e2e_ui/confirm') &&
    response.request().method() === 'POST',
  );
  await dialog.getByRole('button', { name: '确认建议：浏览器确认技能' }).click();
  expect((await confirmResponse).status()).toBe(200);
  await expect(page.getByText('已确认建议', { exact: true })).toBeVisible();
  await expect(dialog.getByText('浏览器确认技能', { exact: true })).toBeHidden();

  const confirmedDiscovery = await page.evaluate(async () => {
    const rawSession = localStorage.getItem('gongge_auth');
    if (!rawSession) throw new Error('登录后未保存认证会话');
    const token = (JSON.parse(rawSession) as { token: string }).token;
    const response = await fetch(
      '/api/enterprise/knowledge/discoveries?tenant_id=tenant_demo&agent_id=agent_e2e_employee',
      {
      headers: { Authorization: `Bearer ${token}` },
      },
    );
    return {
      status: response.status,
      body: await response.json() as Array<{ id: string; status: string }>,
    };
  });
  expect(confirmedDiscovery.status).toBe(200);
  expect(confirmedDiscovery.body).toContainEqual(
    expect.objectContaining({ id: 'kdisc_e2e_ui', status: 'confirmed' }),
  );

  await page.goto('/enterprise/skills');
  const sopTable = page.getByRole('table', { name: 'SOP 列表' });
  await expect(sopTable.getByText('浏览器确认技能', { exact: true })).toBeVisible();
  await expect(sopTable.getByText('browser_confirmed_skill', { exact: true })).toBeVisible();
});

test('管理员确认工具建议后可在员工工具列表看到结果', async ({ page }) => {
  await page.goto('/enterprise/knowledge/new');
  await loginAsAdmin(page, 'agent_e2e_employee');

  const dialog = page.getByRole('dialog', { name: '发现可新增资源' });
  await expect(dialog.getByText('浏览器确认工具', { exact: true })).toBeVisible();

  const confirmResponse = page.waitForResponse((response) =>
    response.url().includes('/discoveries/kdisc_e2e_tool_ui/confirm') &&
    response.request().method() === 'POST',
  );
  await dialog.getByRole('button', { name: '确认建议：浏览器确认工具' }).click();
  expect((await confirmResponse).status()).toBe(200);
  await expect(page.getByText('已确认建议', { exact: true })).toBeVisible();

  await page.goto('/enterprise/tools');
  const toolTable = page.getByRole('table', { name: '工具列表' });
  await expect(toolTable.getByText('浏览器确认工具', { exact: true })).toBeVisible();
  await expect(toolTable.getByText('browser.confirmed.tool', { exact: true })).toBeVisible();
});

test('管理员拒绝 SOP 和工具建议后不会创建或展示资源', async ({ page }) => {
  await page.goto('/enterprise/knowledge/new');
  await loginAsAdmin(page, 'agent_e2e_employee');

  const dialog = page.getByRole('dialog', { name: '发现可新增资源' });
  for (const item of [
    { id: 'kdisc_e2e_reject_skill', title: '浏览器拒绝技能' },
    { id: 'kdisc_e2e_reject_tool', title: '浏览器拒绝工具' },
  ]) {
    await expect(dialog.getByText(item.title, { exact: true })).toBeVisible();
    const rejectResponse = page.waitForResponse((response) =>
      response.url().includes(`/discoveries/${item.id}/reject`) &&
      response.request().method() === 'POST',
    );
    await dialog.getByRole('button', { name: `拒绝建议：${item.title}` }).click();
    expect((await rejectResponse).status()).toBe(200);
    await expect(dialog.getByText(item.title, { exact: true })).toBeHidden();
  }
  await expect(page.getByText('已拒绝建议', { exact: true }).last()).toBeVisible();

  const rejectedState = await page.evaluate(async () => {
    const rawSession = localStorage.getItem('gongge_auth');
    if (!rawSession) throw new Error('登录后未保存认证会话');
    const token = (JSON.parse(rawSession) as { token: string }).token;
    const headers = { Authorization: `Bearer ${token}` };
    const [discoveries, skills, tools] = await Promise.all([
      fetch(
        '/api/enterprise/knowledge/discoveries?tenant_id=tenant_demo&agent_id=agent_e2e_employee',
        { headers },
      ).then((response) => response.json() as Promise<Array<{ id: string; status: string }>>),
      fetch('/api/enterprise/skills?tenant_id=tenant_demo&agent_id=agent_e2e_employee', {
        headers,
      }).then((response) => response.json() as Promise<Array<{ skill_id: string }>>),
      fetch('/api/enterprise/tools?tenant_id=tenant_demo&agent_id=agent_e2e_employee', {
        headers,
      }).then((response) => response.json() as Promise<Array<{ name: string }>>),
    ]);
    return { discoveries, skills, tools };
  });
  expect(rejectedState.discoveries).toEqual(
    expect.arrayContaining([
      expect.objectContaining({ id: 'kdisc_e2e_reject_skill', status: 'rejected' }),
      expect.objectContaining({ id: 'kdisc_e2e_reject_tool', status: 'rejected' }),
    ]),
  );
  expect(rejectedState.skills).not.toContainEqual(
    expect.objectContaining({ skill_id: 'browser_rejected_skill' }),
  );
  expect(rejectedState.tools).not.toContainEqual(
    expect.objectContaining({ name: 'browser.rejected.tool' }),
  );

  await page.goto('/enterprise/skills');
  await expect(
    page.getByRole('table', { name: 'SOP 列表' }).getByText('浏览器拒绝技能', { exact: true }),
  ).toBeHidden();
  await page.goto('/enterprise/tools');
  await expect(
    page.getByRole('table', { name: '工具列表' }).getByText('浏览器拒绝工具', { exact: true }),
  ).toBeHidden();
});

test('超标特批 v2.1 按申请部门子树竞争认领并流转到集中财务', async ({ browser }) => {
  const memberContext = await browser.newContext();
  const memberTwoContext = await browser.newContext();
  const outsideContext = await browser.newContext();
  const financeContext = await browser.newContext();
  const memberPage = await memberContext.newPage();
  const memberTwoPage = await memberTwoContext.newPage();
  const outsidePage = await outsideContext.newPage();
  const financePage = await financeContext.newPage();
  const browserErrors: string[] = [];
  for (const page of [memberPage, memberTwoPage, outsidePage, financePage]) {
    page.on('pageerror', (error) => browserErrors.push(error.message));
    await page.goto('/enterprise/dashboard');
  }
  await login(memberPage, 'member', 'member');
  await login(memberTwoPage, 'member-two', 'member-two');
  await login(outsidePage, 'other-member', 'other-member');
  await login(financePage, 'finance', 'finance');

  for (const page of [memberPage, memberTwoPage]) {
    await page.goto('/enterprise/work-items');
    await expect(page.getByText('expense_over_limit_approval', { exact: true })).toBeVisible();
    await expect(page.getByText('department_special_approval · 2.1.0')).toBeVisible();
  }
  await outsidePage.goto('/enterprise/work-items');
  await expect(
    outsidePage.getByText('expense_over_limit_approval', { exact: true }),
  ).toBeHidden();

  const workItem = await memberPage.evaluate(async () => {
    const rawSession = localStorage.getItem('gongge_auth');
    if (!rawSession) throw new Error('登录后未保存认证会话');
    const token = (JSON.parse(rawSession) as { token: string }).token;
    const response = await fetch('/api/work-items?tenant_id=tenant_demo&view=pending', {
      headers: { Authorization: `Bearer ${token}` },
    });
    const items = await response.json() as Array<{
      id: string;
      skill_id: string;
      revision: number;
    }>;
    const item = items.find(
      (candidate) => candidate.skill_id === 'expense_over_limit_approval',
    );
    if (!item) throw new Error('未找到超标特批 v2.1 部门任务');
    return item;
  });
  const outsideStatus = await outsidePage.evaluate(async (item) => {
    const rawSession = localStorage.getItem('gongge_auth');
    if (!rawSession) throw new Error('登录后未保存认证会话');
    const token = (JSON.parse(rawSession) as { token: string }).token;
    const response = await fetch(`/api/work-items/${item.id}/claim`, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        command_id: 'm3c-browser-outside-claim',
        expected_revision: item.revision,
      }),
    });
    return response.status;
  }, workItem);
  expect(outsideStatus).toBe(403);

  async function claimFrom(page: Page, commandId: string) {
    return page.evaluate(async ({ item, command }) => {
      const rawSession = localStorage.getItem('gongge_auth');
      if (!rawSession) throw new Error('登录后未保存认证会话');
      const token = (JSON.parse(rawSession) as { token: string }).token;
      const response = await fetch(`/api/work-items/${item.id}/claim`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${token}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          tenant_id: 'tenant_demo',
          command_id: command,
          expected_revision: item.revision,
        }),
      });
      return response.status;
    }, { item: workItem, command: commandId });
  }
  const raceStatuses = await Promise.all([
    claimFrom(memberPage, 'm3c-browser-member-claim'),
    claimFrom(memberTwoPage, 'm3c-browser-member-two-claim'),
  ]);
  expect([...raceStatuses].sort()).toEqual([200, 409]);
  const winnerPage = raceStatuses[0] === 200 ? memberPage : memberTwoPage;
  const loserPage = raceStatuses[0] === 200 ? memberTwoPage : memberPage;

  await winnerPage.goto('/enterprise/work-items');
  await winnerPage.getByRole('tab', { name: '待我处理' }).click();
  await expect(
    winnerPage.getByText('expense_over_limit_approval', { exact: true }),
  ).toBeVisible();
  await loserPage.goto('/enterprise/work-items');
  await expect(
    loserPage.getByText('expense_over_limit_approval', { exact: true }),
  ).toBeHidden();

  await winnerPage.getByRole('row').filter({ hasText: 'expense_over_limit_approval' })
    .getByRole('button', { name: '查看' }).click();
  await winnerPage.getByPlaceholder('请填写本次处理结果和依据')
    .fill('部门预算与超标事由属实');
  const departmentComplete = winnerPage.waitForResponse((response) =>
    response.url().includes(`/api/work-items/${workItem.id}/complete`)
    && response.request().method() === 'POST',
  );
  await winnerPage.getByRole('button', { name: '批准本级' }).click();
  expect((await departmentComplete).status()).toBe(200);

  await financePage.goto('/enterprise/work-items');
  await expect(
    financePage.getByText('expense_over_limit_approval', { exact: true }),
  ).toBeVisible();
  await expect(financePage.getByText('finance_special_approval · 2.1.0')).toBeVisible();
  await financePage.getByRole('row').filter({ hasText: 'expense_over_limit_approval' })
    .getByRole('button', { name: '查看' }).click();
  await financePage.getByRole('button', { name: '认领任务' }).click();
  await financePage.getByRole('button', { name: '关闭' }).click();
  await financePage.getByRole('tab', { name: '待我处理' }).click();
  await financePage.getByRole('row').filter({ hasText: 'expense_over_limit_approval' })
    .getByRole('button', { name: '查看' }).click();
  await financePage.getByPlaceholder('请填写本次处理结果和依据')
    .fill('财务政策与额度复核通过');
  const financeComplete = financePage.waitForResponse((response) =>
    response.url().includes('/api/work-items/')
    && response.url().endsWith('/complete')
    && response.request().method() === 'POST',
  );
  await financePage.getByRole('button', { name: '批准本级' }).click();
  const financeCompleteResponse = await financeComplete;
  expect(
    financeCompleteResponse.status(),
    JSON.stringify(await financeCompleteResponse.json()),
  ).toBe(200);
  await expect(financePage.getByRole('dialog')).toBeHidden();
  await financePage.getByRole('tab', { name: '已办' }).click();
  await expect(
    financePage.getByText('expense_over_limit_approval', { exact: true }),
  ).toBeVisible();

  expect(browserErrors).toEqual([]);
  await memberContext.close();
  await memberTwoContext.close();
  await outsideContext.close();
  await financeContext.close();
});

test('管理员治理发布后普通成员可添加对话和复制，但不能绕过发布命令', async ({ page }) => {
  const sourceName = 'M0 广场行政助手';
  const copiedName = 'M0 我的行政分身';

  await page.goto('/enterprise/platform/agents');
  await loginAsAdmin(page);
  const sourceAgent = await page.evaluate(async (name) => {
    const rawSession = localStorage.getItem('gongge_auth');
    if (!rawSession) throw new Error('登录后未保存认证会话');
    const token = (JSON.parse(rawSession) as { token: string }).token;
    const headers = {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
    };
    const createdResponse = await fetch('/api/enterprise/agents', {
      method: 'POST',
      headers,
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        name,
        description: '用于验证广场使用、对话和复制边界。',
        source_mode: 'blank',
        metadata: { role_name: '行政助理' },
      }),
    });
    const created = await createdResponse.json() as { id: string };
    return {
      id: created.id,
      createStatus: createdResponse.status,
    };
  }, sourceName);
  expect(sourceAgent.createStatus).toBe(200);

  await page.evaluate(() => localStorage.removeItem('gongge_auth'));
  await page.goto('/enterprise/platform/agents');
  await login(page, 'member', 'member');
  const privateAgentGuards = await page.evaluate(async (sourceId) => {
    const rawSession = localStorage.getItem('gongge_auth');
    if (!rawSession) throw new Error('登录后未保存认证会话');
    const token = (JSON.parse(rawSession) as { token: string }).token;
    const headers = {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
    };
    const detail = await fetch(
      `/api/enterprise/agents/${sourceId}?tenant_id=tenant_demo`,
      { headers },
    );
    const use = await fetch(
      `/api/chat/agents/${sourceId}/use?tenant_id=tenant_demo`,
      { method: 'POST', headers, body: '{}' },
    );
    const session = await fetch('/api/chat/sessions', {
      method: 'POST',
      headers,
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        agent_id: sourceId,
        origin: 'gallery',
      }),
    });
    return [detail.status, use.status, session.status];
  }, sourceAgent.id);
  expect(privateAgentGuards).toEqual([403, 403, 403]);

  await page.evaluate(() => localStorage.removeItem('gongge_auth'));
  await page.goto('/enterprise/platform/agents');
  await loginAsAdmin(page);
  const publishStatus = await page.evaluate(async (sourceId) => {
    const rawSession = localStorage.getItem('gongge_auth');
    if (!rawSession) throw new Error('登录后未保存认证会话');
    const token = (JSON.parse(rawSession) as { token: string }).token;
    const response = await fetch(
      `/api/enterprise/agents/${sourceId}/gallery-publication`,
      {
        method: 'PUT',
        headers: {
          Authorization: `Bearer ${token}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ tenant_id: 'tenant_demo', published: true }),
      },
    );
    return response.status;
  }, sourceAgent.id);
  expect(publishStatus).toBe(200);
  await page.reload();
  await expect(page.getByText('数字员工广场', { exact: true }).first()).toBeVisible();
  await page.goto('/enterprise/work-items');
  await expect(page.getByRole('tab', { name: '可认领' })).toBeVisible();
  await expect(page.getByText('m0_admin_process', { exact: true })).toBeHidden();

  await page.evaluate(() => localStorage.removeItem('gongge_auth'));
  await page.goto('/enterprise/platform/agents');
  await login(page, 'member', 'member');
  await page.goto('/enterprise/work-items');
  await expect(page.getByText('m0_admin_process', { exact: true })).toBeVisible();
  const scopedWorkItem = await page.evaluate(async () => {
    const rawSession = localStorage.getItem('gongge_auth');
    if (!rawSession) throw new Error('登录后未保存认证会话');
    const token = (JSON.parse(rawSession) as { token: string }).token;
    const response = await fetch('/api/work-items?tenant_id=tenant_demo&view=pending', {
      headers: { Authorization: `Bearer ${token}` },
    });
    const items = await response.json() as Array<{
      id: string;
      skill_id: string;
      revision: number;
    }>;
    const item = items.find((candidate) => candidate.skill_id === 'm0_admin_process');
    if (!item) throw new Error('未找到组织范围工作项');
    return item;
  });

  await page.evaluate(() => localStorage.removeItem('gongge_auth'));
  await page.goto('/enterprise/platform/agents');
  await login(page, 'other-member', 'other-member');
  await page.goto('/enterprise/work-items');
  await expect(page.getByText('m0_admin_process', { exact: true })).toBeHidden();
  const outsideClaimStatus = await page.evaluate(async (item) => {
    const rawSession = localStorage.getItem('gongge_auth');
    if (!rawSession) throw new Error('登录后未保存认证会话');
    const token = (JSON.parse(rawSession) as { token: string }).token;
    const response = await fetch(`/api/work-items/${item.id}/claim`, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        command_id: 'e2e-outside-scope-claim',
        expected_revision: item.revision,
      }),
    });
    return response.status;
  }, scopedWorkItem);
  expect(outsideClaimStatus).toBe(403);

  await page.evaluate(() => localStorage.removeItem('gongge_auth'));
  await page.goto('/enterprise/platform/agents');
  await login(page, 'member', 'member');
  await page.goto('/enterprise/work-items');
  await expect(page.getByText('m0_admin_process', { exact: true })).toBeVisible();
  await page.getByRole('row').filter({ hasText: 'm0_admin_process' })
    .getByRole('button', { name: '查看' }).click();
  const claimResponse = page.waitForResponse((response) =>
    response.url().includes('/api/work-items/')
    && response.url().endsWith('/claim')
    && response.request().method() === 'POST',
  );
  await page.getByRole('button', { name: '认领任务' }).click();
  expect((await claimResponse).status()).toBe(200);
  await page.getByRole('button', { name: '关闭' }).click();
  await page.getByRole('tab', { name: '待我处理' }).click();
  await expect(page.getByText('m0_admin_process', { exact: true })).toBeVisible();

  await page.evaluate(() => localStorage.removeItem('gongge_auth'));
  await page.goto('/enterprise/dashboard');
  await loginAsAdmin(page);
  const endScopedAssignmentStatus = await page.evaluate(async () => {
    const rawSession = localStorage.getItem('gongge_auth');
    if (!rawSession) throw new Error('登录后未保存认证会话');
    const token = (JSON.parse(rawSession) as { token: string }).token;
    const headers = {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
    };
    const response = await fetch(
      '/api/organization/member-org-assignments?tenant_id=tenant_demo'
      + '&employee_profile_id=profile_e2e_member',
      { headers },
    );
    const assignments = await response.json() as Array<{
      id: string;
      assignment_type: string;
      status: string;
    }>;
    const scoped = assignments.find(
      (assignment) => assignment.assignment_type === 'concurrent'
        && assignment.status === 'active',
    );
    if (!scoped) throw new Error('未找到待结束的组织范围归属');
    const endResponse = await fetch(
      `/api/organization/member-org-assignments/${scoped.id}/end`,
      {
        method: 'POST',
        headers,
        body: JSON.stringify({ tenant_id: 'tenant_demo' }),
      },
    );
    return endResponse.status;
  });
  expect(endScopedAssignmentStatus).toBe(200);

  await page.evaluate(() => localStorage.removeItem('gongge_auth'));
  await page.goto('/enterprise/platform/agents');
  await login(page, 'member', 'member');
  await page.goto('/enterprise/work-items');
  await page.getByRole('tab', { name: '待我处理' }).click();
  await expect(page.getByText('m0_admin_process', { exact: true })).toBeVisible();
  await page.getByRole('row').filter({ hasText: 'm0_admin_process' })
    .getByRole('button', { name: '查看' }).click();
  await expect(page.getByRole('button', { name: '同意' })).toBeHidden();
  await expect(page.getByRole('button', { name: '拒绝', exact: true })).toBeHidden();
  await page.getByRole('button', { name: '关闭' }).click();
  const revokedCompleteStatus = await page.evaluate(async (workItemId) => {
    const rawSession = localStorage.getItem('gongge_auth');
    if (!rawSession) throw new Error('登录后未保存认证会话');
    const token = (JSON.parse(rawSession) as { token: string }).token;
    const claimedResponse = await fetch(
      '/api/work-items?tenant_id=tenant_demo&view=claimed',
      { headers: { Authorization: `Bearer ${token}` } },
    );
    const items = await claimedResponse.json() as Array<{ id: string; revision: number }>;
    const item = items.find((candidate) => candidate.id === workItemId);
    if (!item) throw new Error('未找到已认领工作项');
    const response = await fetch(`/api/work-items/${item.id}/complete`, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        command_id: 'e2e-revoked-scope-complete',
        expected_revision: item.revision,
        outcome: 'approved',
      }),
    });
    return response.status;
  }, scopedWorkItem.id);
  expect(revokedCompleteStatus).toBe(403);

  await page.getByRole('tab', { name: '可认领' }).click();
  await expect(page.getByText('m0_legacy_tenant_process', { exact: true })).toBeVisible();
  await page.getByRole('row').filter({ hasText: 'm0_legacy_tenant_process' })
    .getByRole('button', { name: '查看' }).click();
  const legacyClaimResponse = page.waitForResponse((response) =>
    response.url().includes('/api/work-items/')
    && response.url().endsWith('/claim')
    && response.request().method() === 'POST',
  );
  await page.getByRole('button', { name: '认领任务' }).click();
  expect((await legacyClaimResponse).status()).toBe(200);
  await page.getByRole('button', { name: '关闭' }).click();
  await page.getByRole('tab', { name: '待我处理' }).click();
  await page.getByRole('row').filter({ hasText: 'm0_legacy_tenant_process' })
    .getByRole('button', { name: '查看' }).click();
  const legacyCompleteResponse = page.waitForResponse((response) =>
    response.url().includes('/api/work-items/')
    && response.url().endsWith('/complete')
    && response.request().method() === 'POST',
  );
  await page.getByRole('button', { name: '同意' }).click();
  expect((await legacyCompleteResponse).status()).toBe(200);
  await expect(page.getByRole('dialog')).toBeHidden();
  await page.goto('/enterprise/platform/agents');

  const sourceCard = page.getByRole('button', { name: new RegExp(sourceName) });
  await expect(sourceCard).toBeVisible();
  await sourceCard.click();
  const drawer = page.locator('.platform-employee-drawer');
  await expect(drawer.getByRole('button', { name: '添加使用并开始对话' })).toBeVisible();
  await expect(drawer.getByRole('button', { name: '复制并定制' })).toBeVisible();

  await drawer.getByRole('button', { name: '复制并定制' }).click();
  const createDialog = page.getByRole('dialog', { name: '新建数字员工' });
  await expect(createDialog.getByText(sourceName, { exact: false })).toBeVisible();
  await createDialog.getByRole('textbox', { name: '数字员工姓名' }).fill(copiedName);
  const createCopyResponse = page.waitForResponse((response) =>
    response.url().endsWith('/api/enterprise/agents')
    && response.request().method() === 'POST',
  );
  await createDialog.getByRole('button', { name: '创建', exact: true }).click();
  expect((await createCopyResponse).status()).toBe(200);
  await expect(createDialog).toBeHidden();

  const publicationGuards = await page.evaluate(async ({ sourceId, copyName }) => {
    const rawSession = localStorage.getItem('gongge_auth');
    if (!rawSession) throw new Error('登录后未保存认证会话');
    const token = (JSON.parse(rawSession) as { token: string }).token;
    const headers = {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
    };
    const agents = await fetch('/api/enterprise/agents?tenant_id=tenant_demo', { headers })
      .then((response) => response.json() as Promise<Array<{
        id: string;
        name: string;
        metadata: Record<string, unknown>;
      }>>);
    const copied = agents.find((item) => item.name === copyName);
    if (!copied) throw new Error('复制的数字员工不存在');
    const genericResponse = await fetch(`/api/enterprise/agents/${copied.id}`, {
      method: 'PUT',
      headers,
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        metadata: { ...copied.metadata, published_to_gallery: true },
      }),
    });
    const genericBody = await genericResponse.json() as {
      metadata: Record<string, unknown>;
    };
    const governanceResponse = await fetch(
      `/api/enterprise/agents/${sourceId}/gallery-publication`,
      {
        method: 'PUT',
        headers,
        body: JSON.stringify({ tenant_id: 'tenant_demo', published: false }),
      },
    );
    return {
      copiedId: copied.id,
      genericStatus: genericResponse.status,
      genericPublished: genericBody.metadata.published_to_gallery,
      governanceStatus: governanceResponse.status,
    };
  }, { sourceId: sourceAgent.id, copyName: copiedName });
  expect(publicationGuards.genericStatus).toBe(200);
  expect(publicationGuards.genericPublished).not.toBe(true);
  expect(publicationGuards.governanceStatus).toBe(403);

  await page.goto('/enterprise/platform/agents');
  await page.getByRole('button', { name: new RegExp(sourceName) }).click();
  const useResponse = page.waitForResponse((response) =>
    response.url().includes(`/api/chat/agents/${sourceAgent.id}/use`)
    && response.request().method() === 'POST',
  );
  await page.locator('.platform-employee-drawer')
    .getByRole('button', { name: '添加使用并开始对话' })
    .click();
  expect((await useResponse).status()).toBe(200);
  await expect(page).toHaveURL(new RegExp(`/workspace/chat/draft/${sourceAgent.id}$`));

  await page.goto('/workspace/gallery');
  await expect(page.getByRole('tab', { name: '常用', exact: true })).toHaveAttribute(
    'aria-selected',
    'true',
  );
  await expect(page.getByRole('button', { name: new RegExp(sourceName) })).toBeVisible();
  await page.getByRole('tab', { name: /^我创建的/ }).click();
  await expect(page.getByRole('button', { name: new RegExp(copiedName) })).toBeVisible();

  const m4Contracts = await page.evaluate(async ({ sourceId, copyName }) => {
    const rawSession = localStorage.getItem('gongge_auth');
    if (!rawSession) throw new Error('登录后未保存认证会话');
    const token = (JSON.parse(rawSession) as { token: string }).token;
    const headers = {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
    };
    const readScope = async (scope: string) => {
      const response = await fetch(
        `/api/enterprise/agents?tenant_id=tenant_demo&scope=${scope}`,
        { headers },
      );
      return {
        status: response.status,
        rows: await response.json() as Array<{
          id: string;
          name: string;
          owner_user_id?: string;
          source_agent_id?: string;
          source_agent_version?: string;
        }>,
      };
    };
    const [owned, used, gallery, expert, manageable] = await Promise.all(
      ['owned', 'used', 'gallery', 'expert', 'manageable'].map(readScope),
    );
    const copied = owned.rows.find((item) => item.name === copyName);
    if (!copied) throw new Error('owned scope 未返回复制员工');

    const sessionResponse = await fetch('/api/chat/sessions', {
      method: 'POST',
      headers,
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        agent_id: sourceId,
        origin: 'gallery',
      }),
    });
    const session = await sessionResponse.json() as {
      id: string;
      agent_profile_revision?: number;
      capability_snapshot?: Record<string, unknown>;
      origin?: string;
    };
    const removeResponse = await fetch(
      `/api/chat/agents/${sourceId}/use?tenant_id=tenant_demo`,
      { method: 'DELETE', headers },
    );
    const sessionsAfterRemove = await fetch(
      '/api/chat/sessions?tenant_id=tenant_demo',
      { headers },
    ).then((response) => response.json() as Promise<Array<{ id: string }>>);
    const rejectedSession = await fetch('/api/chat/sessions', {
      method: 'POST',
      headers,
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        agent_id: sourceId,
        origin: 'gallery',
      }),
    });
    const readdResponse = await fetch(
      `/api/chat/agents/${sourceId}/use?tenant_id=tenant_demo`,
      { method: 'POST', headers, body: '{}' },
    );
    return {
      scopes: { owned, used, gallery, expert, manageable },
      copied,
      sessionStatus: sessionResponse.status,
      session,
      removeStatus: removeResponse.status,
      historyPreserved: sessionsAfterRemove.some((item) => item.id === session.id),
      rejectedSessionStatus: rejectedSession.status,
      readdStatus: readdResponse.status,
    };
  }, { sourceId: sourceAgent.id, copyName: copiedName });

  expect(Object.values(m4Contracts.scopes).map((result) => result.status)).toEqual(
    [200, 200, 200, 200, 200],
  );
  expect(m4Contracts.scopes.used.rows).toContainEqual(
    expect.objectContaining({ id: sourceAgent.id }),
  );
  expect(m4Contracts.scopes.gallery.rows).toContainEqual(
    expect.objectContaining({ id: sourceAgent.id }),
  );
  expect(m4Contracts.copied).toMatchObject({
    owner_user_id: 'member_e2e',
    source_agent_id: sourceAgent.id,
    source_agent_version: expect.any(String),
  });
  expect(m4Contracts.sessionStatus).toBe(200);
  expect(m4Contracts.session).toMatchObject({
    origin: 'gallery',
    agent_profile_revision: expect.any(Number),
    capability_snapshot: expect.objectContaining({
      agent_id: sourceAgent.id,
      profile_revision: expect.any(Number),
    }),
  });
  expect(JSON.stringify(m4Contracts.session.capability_snapshot)).not.toMatch(
    /authorization|token|secret|header/i,
  );
  expect(m4Contracts.removeStatus).toBe(200);
  expect(m4Contracts.historyPreserved).toBe(true);
  expect(m4Contracts.rejectedSessionStatus).toBe(403);
  expect(m4Contracts.readdStatus).toBe(200);

  await page.evaluate(() => localStorage.removeItem('gongge_auth'));
  await page.goto('/enterprise/platform/agents');
  await login(page, 'other-member', 'other-member');
  const secondMemberIsolation = await page.evaluate(async ({ sourceId, firstSessionId }) => {
    const rawSession = localStorage.getItem('gongge_auth');
    if (!rawSession) throw new Error('登录后未保存认证会话');
    const token = (JSON.parse(rawSession) as { token: string }).token;
    const headers = {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
    };
    const useResponse = await fetch(
      `/api/chat/agents/${sourceId}/use?tenant_id=tenant_demo`,
      { method: 'POST', headers, body: '{}' },
    );
    const sessionResponse = await fetch('/api/chat/sessions', {
      method: 'POST',
      headers,
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        agent_id: sourceId,
        origin: 'gallery',
      }),
    });
    const session = await sessionResponse.json() as { id: string; user_id?: string };
    const visibleSessionsResponse = await fetch(
      '/api/chat/sessions?tenant_id=tenant_demo',
      { headers },
    );
    const visibleSessions = await visibleSessionsResponse.json() as Array<{ id: string }>;
    await fetch(`/api/chat/sessions/${session.id}?tenant_id=tenant_demo`, {
      method: 'DELETE',
      headers,
    });
    await fetch(`/api/chat/agents/${sourceId}/use?tenant_id=tenant_demo`, {
      method: 'DELETE',
      headers,
    });
    return {
      useStatus: useResponse.status,
      sessionStatus: sessionResponse.status,
      sessionId: session.id,
      visibleSessionsStatus: visibleSessionsResponse.status,
      firstSessionVisible: visibleSessions.some((item) => item.id === firstSessionId),
    };
  }, {
    sourceId: sourceAgent.id,
    firstSessionId: m4Contracts.session.id,
  });
  expect(secondMemberIsolation.useStatus).toBe(200);
  expect(secondMemberIsolation.sessionStatus).toBe(200);
  expect(secondMemberIsolation.sessionId).not.toBe(m4Contracts.session.id);
  expect(secondMemberIsolation.visibleSessionsStatus).toBe(200);
  expect(secondMemberIsolation.firstSessionVisible).toBe(false);
});

test('确认按钮连续触发时只提交一次请求', async ({ page }) => {
  await page.goto('/enterprise/knowledge/new');
  await loginAsAdmin(page, 'agent_e2e_employee');

  const dialog = page.getByRole('dialog', { name: '发现可新增资源' });
  const confirmButton = dialog.getByRole('button', { name: '确认建议：浏览器防重复工具' });
  await expect(confirmButton).toBeVisible();
  let requestCount = 0;
  page.on('request', (request) => {
    if (
      request.url().includes('/discoveries/kdisc_e2e_double_click/confirm') &&
      request.method() === 'POST'
    ) {
      requestCount += 1;
    }
  });
  const confirmResponse = page.waitForResponse((response) =>
    response.url().includes('/discoveries/kdisc_e2e_double_click/confirm') &&
    response.request().method() === 'POST',
  );

  await confirmButton.evaluate((button) => {
    (button as HTMLButtonElement).click();
    (button as HTMLButtonElement).click();
  });

  expect((await confirmResponse).status()).toBe(200);
  expect(requestCount).toBe(1);
  await expect(confirmButton).toBeHidden();
});

test('确认请求断网后保留建议并允许重试', async ({ page }) => {
  const confirmPath = '/discoveries/kdisc_e2e_retry_tool/confirm';
  await page.route(
    `**${confirmPath}**`,
    async (route) => route.abort('failed'),
    { times: 1 },
  );
  await page.goto('/enterprise/knowledge/new');
  await loginAsAdmin(page, 'agent_e2e_employee');

  const dialog = page.getByRole('dialog', { name: '发现可新增资源' });
  const confirmButton = dialog.getByRole('button', { name: '确认建议：浏览器重试工具' });
  await expect(confirmButton).toBeEnabled();
  const failedRequest = page.waitForEvent('requestfailed', (request) =>
    request.url().includes(confirmPath) && request.method() === 'POST',
  );
  await confirmButton.click();
  await failedRequest;

  await expect(dialog.getByText('浏览器重试工具', { exact: true })).toBeVisible();
  await expect(confirmButton).toBeEnabled();
  await expect(page.getByText('Failed to fetch', { exact: true })).toBeVisible();

  const retryResponse = page.waitForResponse((response) =>
    response.url().includes(confirmPath) && response.request().method() === 'POST',
  );
  await confirmButton.click();
  expect((await retryResponse).status()).toBe(200);
  await expect(confirmButton).toBeHidden();

  await page.goto('/enterprise/tools');
  const toolTable = page.getByRole('table', { name: '工具列表' });
  await expect(toolTable.getByText('浏览器重试工具', { exact: true })).toBeVisible();
  await expect(toolTable.getByText('browser.retry.tool', { exact: true })).toBeVisible();
});

test('并发确认只创建一个资源并返回明确冲突', async ({ page }) => {
  await page.goto('/enterprise/dashboard');
  await loginAsAdmin(page, 'agent_e2e_employee');

  const result = await page.evaluate(async () => {
    const rawSession = localStorage.getItem('gongge_auth');
    if (!rawSession) throw new Error('登录后未保存认证会话');
    const token = (JSON.parse(rawSession) as { token: string }).token;
    const path =
      '/api/enterprise/knowledge/discoveries/kdisc_e2e_concurrent/confirm?tenant_id=tenant_demo';
    const confirm = () => fetch(path, {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    }).then(async (response) => ({
      status: response.status,
      body: await response.json() as { detail?: string },
    }));
    const responses = await Promise.all([confirm(), confirm()]);
    const skills = await fetch(
      '/api/enterprise/skills?tenant_id=tenant_demo&agent_id=agent_e2e_employee',
      { headers: { Authorization: `Bearer ${token}` } },
    ).then((response) => response.json() as Promise<Array<{ skill_id: string }>>);
    return { responses, skills };
  });

  expect(result.responses.map((response) => response.status).sort()).toEqual([200, 409]);
  expect(result.responses.find((response) => response.status === 409)?.body.detail).toMatch(
    /已存在|已被其他请求处理/,
  );
  expect(
    result.skills.filter((skill) => skill.skill_id === 'browser_concurrent_skill'),
  ).toHaveLength(1);
});

test('普通成员不能处理不属于自己的知识发现建议', async ({ page }) => {
  await page.goto('/enterprise/dashboard');
  await login(page, 'member', 'member');

  const result = await page.evaluate(async () => {
    const rawSession = localStorage.getItem('gongge_auth');
    if (!rawSession) throw new Error('成员登录后未保存认证会话');
    const memberToken = (JSON.parse(rawSession) as { token: string }).token;
    const memberHeaders = { Authorization: `Bearer ${memberToken}` };
    async function mutate(path: string) {
      const response = await fetch(path, { method: 'POST', headers: memberHeaders });
      return {
        status: response.status,
        body: await response.json() as { detail?: string },
      };
    }
    const [confirm, reject] = await Promise.all([
      mutate(
        '/api/enterprise/knowledge/discoveries/kdisc_e2e_forbidden_confirm/confirm?tenant_id=tenant_demo',
      ),
      mutate(
        '/api/enterprise/knowledge/discoveries/kdisc_e2e_forbidden_reject/reject?tenant_id=tenant_demo',
      ),
    ]);

    const adminLogin = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tenant_id: 'tenant_demo', username: 'admin', password: 'admin' }),
    }).then((response) => response.json() as Promise<{ token: string }>);
    const adminHeaders = { Authorization: `Bearer ${adminLogin.token}` };
    const [discoveries, skills, tools] = await Promise.all([
      fetch(
        '/api/enterprise/knowledge/discoveries?tenant_id=tenant_demo&agent_id=agent_e2e_employee',
        { headers: adminHeaders },
      ).then((response) => response.json() as Promise<Array<{ id: string; status: string }>>),
      fetch('/api/enterprise/skills?tenant_id=tenant_demo&agent_id=agent_e2e_employee', {
        headers: adminHeaders,
      }).then((response) => response.json() as Promise<Array<{ skill_id: string }>>),
      fetch('/api/enterprise/tools?tenant_id=tenant_demo&agent_id=agent_e2e_employee', {
        headers: adminHeaders,
      }).then((response) => response.json() as Promise<Array<{ name: string }>>),
    ]);
    return { confirm, reject, discoveries, skills, tools };
  });

  expect(result.confirm.status).toBe(403);
  expect(result.reject.status).toBe(403);
  expect(result.confirm.body.detail).toContain('Only the creator or administrator');
  expect(result.reject.body.detail).toContain('Only the creator or administrator');
  expect(result.discoveries).toEqual(
    expect.arrayContaining([
      expect.objectContaining({ id: 'kdisc_e2e_forbidden_confirm', status: 'pending' }),
      expect.objectContaining({ id: 'kdisc_e2e_forbidden_reject', status: 'pending' }),
    ]),
  );
  expect(result.skills).not.toContainEqual(
    expect.objectContaining({ skill_id: 'member_forbidden_skill' }),
  );
  expect(result.tools).not.toContainEqual(
    expect.objectContaining({ name: 'member.forbidden.tool' }),
  );
});

test('普通成员可以处理自己员工的建议，其他成员仍被拒绝', async ({ page }) => {
  await page.goto('/enterprise/dashboard');
  await login(page, 'other-member', 'other-member');

  const forbidden = await page.evaluate(async () => {
    const rawSession = localStorage.getItem('gongge_auth');
    if (!rawSession) throw new Error('其他成员登录后未保存认证会话');
    const token = (JSON.parse(rawSession) as { token: string }).token;
    const headers = { Authorization: `Bearer ${token}` };
    async function mutate(action: 'confirm' | 'reject', id: string) {
      const response = await fetch(
        `/api/enterprise/knowledge/discoveries/${id}/${action}?tenant_id=tenant_demo`,
        { method: 'POST', headers },
      );
      return {
        status: response.status,
        body: await response.json() as { detail?: string },
      };
    }
    return Promise.all([
      mutate('confirm', 'kdisc_e2e_member_confirm'),
      mutate('reject', 'kdisc_e2e_member_reject'),
    ]);
  });

  for (const result of forbidden) {
    expect(result.status).toBe(403);
    expect(result.body.detail).toContain('Only the creator or administrator');
  }

  await page.evaluate(() => localStorage.clear());
  await page.goto('/enterprise/knowledge/new');
  await login(page, 'member', 'member', 'agent_e2e_member_employee');

  const dialog = page.getByRole('dialog', { name: '发现可新增资源' });
  const confirmTitle = '成员确认自己的技能';
  const rejectTitle = '成员拒绝自己的工具';
  await expect(dialog.getByText(confirmTitle, { exact: true })).toBeVisible();
  await expect(dialog.getByText(rejectTitle, { exact: true })).toBeVisible();

  const confirmResponse = page.waitForResponse((response) =>
    response.url().includes('/discoveries/kdisc_e2e_member_confirm/confirm') &&
    response.request().method() === 'POST',
  );
  await dialog.getByRole('button', { name: `确认建议：${confirmTitle}` }).click();
  expect((await confirmResponse).status()).toBe(200);
  await expect(dialog.getByText(confirmTitle, { exact: true })).toBeHidden();

  const rejectResponse = page.waitForResponse((response) =>
    response.url().includes('/discoveries/kdisc_e2e_member_reject/reject') &&
    response.request().method() === 'POST',
  );
  await dialog.getByRole('button', { name: `拒绝建议：${rejectTitle}` }).click();
  expect((await rejectResponse).status()).toBe(200);
  await expect(dialog.getByText(rejectTitle, { exact: true })).toBeHidden();

  const verified = await page.evaluate(async () => {
    const rawSession = localStorage.getItem('gongge_auth');
    if (!rawSession) throw new Error('成员登录后未保存认证会话');
    const token = (JSON.parse(rawSession) as { token: string }).token;
    const headers = { Authorization: `Bearer ${token}` };
    const scope = 'tenant_id=tenant_demo&agent_id=agent_e2e_member_employee';
    const [discoveries, skills, tools] = await Promise.all([
      fetch(`/api/enterprise/knowledge/discoveries?${scope}`, { headers })
        .then((response) => response.json() as Promise<Array<{ id: string; status: string }>>),
      fetch(`/api/enterprise/skills?${scope}`, { headers })
        .then((response) => response.json() as Promise<Array<{ skill_id: string }>>),
      fetch(`/api/enterprise/tools?${scope}`, { headers })
        .then((response) => response.json() as Promise<Array<{ name: string }>>),
    ]);
    return { discoveries, skills, tools };
  });

  expect(verified.discoveries).toEqual(
    expect.arrayContaining([
      expect.objectContaining({ id: 'kdisc_e2e_member_confirm', status: 'confirmed' }),
      expect.objectContaining({ id: 'kdisc_e2e_member_reject', status: 'rejected' }),
    ]),
  );
  expect(verified.skills).toContainEqual(
    expect.objectContaining({ skill_id: 'member_owned_skill' }),
  );
  expect(verified.tools).not.toContainEqual(
    expect.objectContaining({ name: 'member.owned.rejected.tool' }),
  );
});

test('错误密码被拒绝且不会创建本地会话', async ({ page }) => {
  await page.goto('/enterprise/dashboard');
  await page.getByRole('button', { name: '进入平台' }).click();
  await page.getByRole('textbox', { name: '账号' }).fill('admin');
  await page.getByRole('textbox', { name: '密码', exact: true }).fill('wrong-password');

  const loginResponse = page.waitForResponse((response) =>
    response.url().endsWith('/api/auth/login') && response.request().method() === 'POST',
  );
  await page.getByRole('button', { name: '登录', exact: true }).click();

  expect((await loginResponse).status()).toBe(401);
  await expect(page.getByRole('button', { name: '登录', exact: true })).toBeVisible();
  expect(await page.evaluate(() => localStorage.getItem('gongge_auth'))).toBeNull();
});

test('退出登录会清除会话并返回登录页', async ({ page }) => {
  await page.goto('/enterprise/knowledge');
  await loginAsAdmin(page);
  await expect(page.getByRole('button', { name: '账户菜单' })).toBeVisible();

  await page.getByRole('button', { name: '账户菜单' }).click();
  await page.getByRole('menuitem', { name: '退出登录' }).click();

  await expect(page.getByRole('button', { name: '进入平台' })).toBeVisible();
  expect(await page.evaluate(() => localStorage.getItem('gongge_auth'))).toBeNull();
});

test('刷新页面后通过服务端校验恢复登录会话', async ({ page }) => {
  await page.goto('/enterprise/knowledge');
  await loginAsAdmin(page);
  await expect(page.getByRole('button', { name: '账户菜单' })).toBeVisible();

  const contextResponse = page.waitForResponse((response) =>
    response.url().endsWith('/api/auth/context') && response.request().method() === 'GET',
  );
  await page.reload();

  expect((await contextResponse).status()).toBe(200);
  await expect(page.getByRole('button', { name: '账户菜单' })).toBeVisible();
  expect(await page.evaluate(() => Boolean(localStorage.getItem('gongge_auth')))).toBe(true);

  const forgedTenantStatus = await page.evaluate(async () => {
    const rawSession = localStorage.getItem('gongge_auth');
    if (!rawSession) throw new Error('登录后未保存认证会话');
    const token = (JSON.parse(rawSession) as { token: string }).token;
    const response = await fetch('/api/auth/users?tenant_id=tenant_forged', {
      headers: { Authorization: `Bearer ${token}` },
    });
    return response.status;
  });
  expect(forgedTenantStatus).toBe(403);
});

test('管理员维护单根组织树，普通成员不能通过浏览器构造写请求', async ({ page }) => {
  await page.goto('/enterprise/dashboard');
  await loginAsAdmin(page);

  const adminResult = await page.evaluate(async () => {
    const rawSession = localStorage.getItem('gongge_auth');
    if (!rawSession) throw new Error('登录后未保存认证会话');
    const token = (JSON.parse(rawSession) as { token: string }).token;
    const headers = {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
    };
    const listResponse = await fetch('/api/organization/units?tenant_id=tenant_demo', { headers });
    const initial = await listResponse.json() as Array<{ id: string; is_root: boolean }>;
    const root = initial.find((item) => item.is_root);
    if (!root) throw new Error('组织树缺少根节点');

    async function postUnit(code: string, name: string) {
      const response = await fetch('/api/organization/units', {
        method: 'POST',
        headers,
        body: JSON.stringify({
          tenant_id: 'tenant_demo',
          parent_id: root.id,
          code,
          name,
          unit_type_code: 'department',
        }),
      });
      return {
        status: response.status,
        body: await response.json() as { id: string; tree_path: string },
      };
    }

    const division = await postUnit('e2e_engineering', '浏览器研发部');
    const center = await postUnit('e2e_delivery', '浏览器交付中心');
    const moveResponse = await fetch(`/api/organization/units/${division.body.id}`, {
      method: 'PUT',
      headers,
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        parent_id: center.body.id,
      }),
    });
    const moved = await moveResponse.json() as { tree_path?: string };
    const cycleResponse = await fetch(`/api/organization/units/${center.body.id}`, {
      method: 'PUT',
      headers,
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        parent_id: division.body.id,
      }),
    });
    const rootUpdateResponse = await fetch(`/api/organization/units/${root.id}`, {
      method: 'PUT',
      headers,
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        name: '伪造的新根名称',
      }),
    });
    return {
      rootId: root.id,
      divisionId: division.body.id,
      centerId: center.body.id,
      listStatus: listResponse.status,
      divisionStatus: division.status,
      centerStatus: center.status,
      moveStatus: moveResponse.status,
      movedPath: moved.tree_path,
      cycleStatus: cycleResponse.status,
      rootUpdateStatus: rootUpdateResponse.status,
    };
  });

  expect(adminResult).toMatchObject({
    listStatus: 200,
    divisionStatus: 200,
    centerStatus: 200,
    moveStatus: 200,
    cycleStatus: 400,
    rootUpdateStatus: 400,
  });
  expect(adminResult.movedPath).toBe(
    `${adminResult.rootId}/${adminResult.centerId}/${adminResult.divisionId}`,
  );

  await page.reload();
  await expect(page.getByRole('button', { name: '账户菜单' })).toBeVisible();
  const persistedPath = await page.evaluate(async (divisionId) => {
    const rawSession = localStorage.getItem('gongge_auth');
    if (!rawSession) throw new Error('刷新后未保存认证会话');
    const token = (JSON.parse(rawSession) as { token: string }).token;
    const response = await fetch('/api/organization/units?tenant_id=tenant_demo', {
      headers: { Authorization: `Bearer ${token}` },
    });
    const rows = await response.json() as Array<{ id: string; tree_path: string }>;
    return rows.find((item) => item.id === divisionId)?.tree_path;
  }, adminResult.divisionId);
  expect(persistedPath).toBe(adminResult.movedPath);

  await page.getByRole('button', { name: '账户菜单' }).click();
  await page.getByRole('menuitem', { name: '退出登录' }).click();
  await login(page, 'member', 'member');
  const memberWriteStatus = await page.evaluate(async (rootId) => {
    const rawSession = localStorage.getItem('gongge_auth');
    if (!rawSession) throw new Error('成员登录后未保存认证会话');
    const token = (JSON.parse(rawSession) as { token: string }).token;
    const response = await fetch('/api/organization/units', {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        parent_id: rootId,
        code: 'member_forged',
        name: '成员伪造部门',
        unit_type_code: 'department',
      }),
    });
    return response.status;
  }, adminResult.rootId);
  expect(memberWriteStatus).toBe(403);
});

test('管理员通过真实浏览器执行组织调动和岗位任职并保留历史', async ({ page }) => {
  await page.goto('/enterprise/dashboard');
  await loginAsAdmin(page);

  const result = await page.evaluate(async () => {
    const rawSession = localStorage.getItem('gongge_auth');
    if (!rawSession) throw new Error('登录后未保存认证会话');
    const token = (JSON.parse(rawSession) as { token: string }).token;
    const headers = {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
    };
    async function request(path: string, init?: RequestInit) {
      const response = await fetch(path, { ...init, headers });
      return { status: response.status, body: await response.json() };
    }
    const user = await request('/api/auth/users', {
      method: 'POST',
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        username: 'employee-m2b',
        password: 'employee-m2b-password',
        display_name: 'M2-B 浏览器成员',
        employee_id: 'E-M2B',
        employee_name: 'M2-B 浏览器成员',
      }),
    });
    const profileId = (user.body as { employee_profile_id: string }).employee_profile_id;
    const units = await request('/api/organization/units?tenant_id=tenant_demo');
    const root = (units.body as Array<{ id: string; is_root: boolean }>)
      .find((item) => item.is_root);
    if (!root) throw new Error('组织树缺少根节点');
    async function createUnit(code: string, name: string) {
      return request('/api/organization/units', {
        method: 'POST',
        body: JSON.stringify({
          tenant_id: 'tenant_demo',
          parent_id: root.id,
          code,
          name,
          unit_type_code: 'department',
        }),
      });
    }
    const finance = await createUnit('e2e_m2b_finance', 'M2-B 财务部');
    const research = await createUnit('e2e_m2b_research', 'M2-B 研发部');
    const financeId = (finance.body as { id: string }).id;
    const researchId = (research.body as { id: string }).id;
    const firstOrg = await request('/api/organization/member-org-assignments', {
      method: 'POST',
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        employee_profile_id: profileId,
        org_unit_id: financeId,
        assignment_type: 'primary',
      }),
    });
    const firstPosition = await request('/api/organization/positions', {
      method: 'POST',
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        org_unit_id: financeId,
        code: 'e2e_m2b_fin_specialist',
        name: 'M2-B 财务专员',
        position_type_code: 'professional',
      }),
    });
    const firstPositionAssignment = await request('/api/organization/position-assignments', {
      method: 'POST',
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        employee_profile_id: profileId,
        position_id: (firstPosition.body as { id: string }).id,
        assignment_type: 'primary',
      }),
    });
    const transferredOrg = await request('/api/organization/member-org-assignments', {
      method: 'POST',
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        employee_profile_id: profileId,
        org_unit_id: researchId,
        assignment_type: 'primary',
      }),
    });
    const secondPosition = await request('/api/organization/positions', {
      method: 'POST',
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        org_unit_id: researchId,
        code: 'e2e_m2b_rd_specialist',
        name: 'M2-B 研发专员',
        position_type_code: 'professional',
      }),
    });
    const secondPositionAssignment = await request('/api/organization/position-assignments', {
      method: 'POST',
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        employee_profile_id: profileId,
        position_id: (secondPosition.body as { id: string }).id,
        assignment_type: 'primary',
      }),
    });
    const orgHistory = await request(
      `/api/organization/member-org-assignments?tenant_id=tenant_demo&employee_profile_id=${profileId}`,
    );
    const positionHistory = await request(
      `/api/organization/position-assignments?tenant_id=tenant_demo&employee_profile_id=${profileId}`,
    );
    return {
      userId: (user.body as { id: string }).id,
      profileId,
      statuses: [
        user.status,
        firstOrg.status,
        firstPosition.status,
        firstPositionAssignment.status,
        transferredOrg.status,
        secondPosition.status,
        secondPositionAssignment.status,
        orgHistory.status,
        positionHistory.status,
      ],
      orgHistory: orgHistory.body as Array<{ status: string; org_unit_id: string }>,
      positionHistory: positionHistory.body as Array<{ status: string; position_id: string }>,
      firstPositionId: (firstPosition.body as { id: string }).id,
      secondPositionId: (secondPosition.body as { id: string }).id,
    };
  });

  expect(result.statuses).toEqual([200, 200, 200, 200, 200, 200, 200, 200, 200]);
  expect(result.orgHistory).toHaveLength(2);
  expect(result.orgHistory.map((item) => item.status)).toEqual(['inactive', 'active']);
  expect(result.positionHistory).toEqual([
    expect.objectContaining({ status: 'inactive', position_id: result.firstPositionId }),
    expect.objectContaining({ status: 'active', position_id: result.secondPositionId }),
  ]);

  await page.reload();
  await expect(page.getByRole('button', { name: '账户菜单' })).toBeVisible();
  const persisted = await page.evaluate(async (profileId) => {
    const rawSession = localStorage.getItem('gongge_auth');
    if (!rawSession) throw new Error('刷新后未保存认证会话');
    const token = (JSON.parse(rawSession) as { token: string }).token;
    const response = await fetch(
      `/api/organization/member-org-assignments?tenant_id=tenant_demo&employee_profile_id=${profileId}`,
      { headers: { Authorization: `Bearer ${token}` } },
    );
    return { status: response.status, count: ((await response.json()) as unknown[]).length };
  }, result.profileId);
  expect(persisted).toEqual({ status: 200, count: 2 });

  const lifecycleClosure = await page.evaluate(async ({ profileId, userId }) => {
    const rawSession = localStorage.getItem('gongge_auth');
    if (!rawSession) throw new Error('刷新后未保存认证会话');
    const token = (JSON.parse(rawSession) as { token: string }).token;
    const headers = {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
    };
    const suspend = await fetch(`/api/auth/users/${userId}`, {
      method: 'PUT',
      headers,
      body: JSON.stringify({ tenant_id: 'tenant_demo', membership_status: 'suspended' }),
    });
    const orgResponse = await fetch(
      `/api/organization/member-org-assignments?tenant_id=tenant_demo&employee_profile_id=${profileId}`,
      { headers },
    );
    const positionResponse = await fetch(
      `/api/organization/position-assignments?tenant_id=tenant_demo&employee_profile_id=${profileId}`,
      { headers },
    );
    const orgRows = await orgResponse.json() as Array<{ status: string }>;
    const positionRows = await positionResponse.json() as Array<{ status: string }>;
    return {
      suspendStatus: suspend.status,
      activeOrgCount: orgRows.filter((item) => item.status === 'active').length,
      activePositionCount: positionRows.filter((item) => item.status === 'active').length,
    };
  }, { profileId: result.profileId, userId: result.userId });
  expect(lifecycleClosure).toEqual({
    suspendStatus: 200,
    activeOrgCount: 0,
    activePositionCount: 0,
  });

  await page.getByRole('button', { name: '账户菜单' }).click();
  await page.getByRole('menuitem', { name: '退出登录' }).click();
  await login(page, 'member', 'member');
  const memberResult = await page.evaluate(async (profileId) => {
    const rawSession = localStorage.getItem('gongge_auth');
    if (!rawSession) throw new Error('成员登录后未保存认证会话');
    const token = (JSON.parse(rawSession) as { token: string }).token;
    const headers = {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
    };
    const broadRead = await fetch(
      '/api/organization/member-org-assignments?tenant_id=tenant_demo',
      { headers },
    );
    const forgedWrite = await fetch('/api/organization/member-org-assignments', {
      method: 'POST',
      headers,
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        employee_profile_id: profileId,
        org_unit_id: 'forged',
        assignment_type: 'primary',
      }),
    });
    return { broadRead: broadRead.status, forgedWrite: forgedWrite.status };
  }, result.profileId);
  expect(memberResult).toEqual({ broadRead: 403, forgedWrite: 403 });
});

test('管理员在组织页面配置任职和岗位默认角色，普通成员不能进入或伪造绑定', async ({ page }) => {
  await page.goto('/enterprise/organization');
  await loginAsAdmin(page);

  await expect(page.getByRole('heading', { name: '谁在什么组织、以什么岗位承担工作' })).toBeVisible();
  await page.getByRole('treeitem', { name: /M2-B 研发部/ }).click();
  await page.getByRole('button', { name: /M2-B 研发专员/ }).click();

  await page.getByRole('button', { name: '调入成员' }).click();
  const memberDialog = page.getByRole('dialog', { name: /调入“M2-B 研发部”/ });
  await memberDialog.getByRole('combobox').click();
  await page.getByRole('option').first().click();
  const orgAssignmentResponse = page.waitForResponse((response) =>
    response.url().endsWith('/api/organization/member-org-assignments')
    && response.request().method() === 'POST',
  );
  await memberDialog.getByRole('button', { name: '保存', exact: true }).click();
  expect((await orgAssignmentResponse).status()).toBe(200);

  const positionPicker = page.getByRole('combobox', { name: '选择岗位任职成员' });
  await positionPicker.click();
  await page.getByRole('option').first().click();
  await page.keyboard.press('Escape');
  const positionAssignmentResponse = page.waitForResponse((response) =>
    response.url().endsWith('/api/organization/position-assignments')
    && response.request().method() === 'POST',
  );
  await page.getByRole('button', { name: '任职', exact: true }).click();
  expect((await positionAssignmentResponse).status()).toBe(200);

  await page.getByRole('button', { name: '绑定', exact: true }).click();
  const roleDialog = page.getByRole('dialog', { name: /绑定默认角色/ });
  await roleDialog.getByRole('combobox').click();
  await page.getByRole('option', { name: /财务报销专员/ }).click();
  const roleBindingResponse = page.waitForResponse((response) =>
    response.url().endsWith('/api/organization/position-role-bindings')
    && response.request().method() === 'POST',
  );
  await roleDialog.getByRole('button', { name: '保存', exact: true }).click();
  expect((await roleBindingResponse).status()).toBe(200);
  await expect(page.getByText('岗位带入', { exact: true })).toBeVisible();

  await page.goto('/enterprise/accounts');
  const memberTable = page.getByRole('table', { name: '成员列表' });
  await expect(memberTable).toBeVisible();
  await expect(memberTable.getByText(/M2-B 研发部 · M2-B 研发专员/).first()).toBeVisible();
  await expect(memberTable.getByText(/财务报销专员（岗位）/).first()).toBeVisible();

  await page.getByRole('button', { name: '账户菜单' }).click();
  await page.getByRole('menuitem', { name: '退出登录' }).click();
  await login(page, 'member', 'member');
  await page.goto('/enterprise/organization');
  await expect(page).toHaveURL(/\/workspace\/gallery$/);
  const forgedStatus = await page.evaluate(async () => {
    const rawSession = localStorage.getItem('gongge_auth');
    if (!rawSession) throw new Error('成员登录后未保存认证会话');
    const token = (JSON.parse(rawSession) as { token: string }).token;
    const response = await fetch('/api/organization/position-role-bindings', {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        position_id: 'forged',
        business_role_id: 'forged',
      }),
    });
    return response.status;
  });
  expect(forgedStatus).toBe(403);
});

test('成员管理通过真实后端写入唯一员工工号绑定', async ({ page }) => {
  await page.goto('/enterprise/accounts');
  await loginAsAdmin(page);

  await expect(page.getByRole('cell', { name: 'E001', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '新增成员' }).click();
  await page.getByLabel('用户名').fill('employee-e100');
  await page.getByLabel('显示名').fill('员工一百');
  await page.getByLabel('员工工号').fill('E100');
  await page.getByLabel('员工姓名').fill('演示员工一百');
  await page.getByLabel('部门编号').fill('FINANCE');
  await page.getByLabel('初始密码').fill('employee-e100-password');
  await page.getByRole('checkbox', {
    name: '财务报销专员 finance_expense_specialist',
    exact: true,
  }).check();

  const createResponse = page.waitForResponse((response) =>
    response.url().endsWith('/api/auth/users') && response.request().method() === 'POST',
  );
  await page.getByRole('button', { name: '创建', exact: true }).click();
  const response = await createResponse;
  expect(response.status()).toBe(200);
  expect(await response.json()).toEqual(expect.objectContaining({
    username: 'employee-e100',
    employee_id: 'E100',
    employee_name: '演示员工一百',
    department_id: 'FINANCE',
    business_role_codes: ['finance_expense_specialist'],
  }));
  await page.getByPlaceholder('搜索成员、账号、工号、状态或类别').fill('E100');
  const createdRow = page.getByRole('row').filter({ has: page.getByRole('cell', { name: 'E100', exact: true }) });
  await expect(createdRow).toBeVisible();
  await expect(createdRow.getByRole('cell', { name: '财务报销专员', exact: true })).toBeVisible();
});

test('管理员停用和恢复成员后，登录、刷新与普通成员越权边界立即生效', async ({ page }) => {
  async function changeMemberStatus(statusLabel: '在职' | '停用') {
    const memberRow = page.getByRole('row').filter({
      has: page.getByRole('cell', { name: 'member', exact: true }),
    });
    await memberRow.getByRole('button', { name: '账号操作' }).click();
    await page.getByRole('menuitem', { name: '编辑' }).click();
    const dialog = page.getByRole('dialog', { name: '编辑成员：member' });
    const statusField = dialog.getByText('成员状态', { exact: true }).locator('..');
    await statusField.getByRole('combobox').click();
    await page.getByRole('option', { name: statusLabel, exact: true }).click();
    const updateResponse = page.waitForResponse((response) =>
      response.url().endsWith('/api/auth/users/member_e2e')
      && response.request().method() === 'PUT',
    );
    await dialog.getByRole('button', { name: '保存', exact: true }).click();
    expect((await updateResponse).status()).toBe(200);
    await expect(dialog).toBeHidden();
  }

  await page.goto('/enterprise/accounts');
  await loginAsAdmin(page);
  await changeMemberStatus('停用');

  await page.getByRole('button', { name: '账户菜单' }).click();
  await page.getByRole('menuitem', { name: '退出登录' }).click();
  await page.getByRole('button', { name: '进入平台' }).click();
  await page.getByRole('textbox', { name: '账号' }).fill('member');
  await page.getByRole('textbox', { name: '密码', exact: true }).fill('member');
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect(page.getByText('Member account is not active', { exact: true })).toBeVisible();
  expect(await page.evaluate(() => localStorage.getItem('gongge_auth'))).toBeNull();

  await page.getByRole('textbox', { name: '账号' }).fill('admin');
  await page.getByRole('textbox', { name: '密码', exact: true }).fill('admin');
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect.poll(
    async () => page.evaluate(() => Boolean(localStorage.getItem('gongge_auth'))),
  ).toBe(true);
  await page.goto('/enterprise/accounts');
  await changeMemberStatus('在职');

  await page.getByRole('button', { name: '账户菜单' }).click();
  await page.getByRole('menuitem', { name: '退出登录' }).click();
  await login(page, 'member', 'member');
  const contextResponse = page.waitForResponse((response) =>
    response.url().endsWith('/api/auth/context') && response.request().method() === 'GET',
  );
  await page.reload();
  expect((await contextResponse).status()).toBe(200);

  await page.goto('/enterprise/accounts');
  await expect(page).toHaveURL(/\/workspace\/gallery$/);
  const forbidden = await page.evaluate(async () => {
    const rawSession = localStorage.getItem('gongge_auth');
    if (!rawSession) throw new Error('登录后未保存认证会话');
    const token = (JSON.parse(rawSession) as { token: string }).token;
    const response = await fetch('/api/auth/users/member_e2e', {
      method: 'PUT',
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        membership_status: 'suspended',
      }),
    });
    return response.status;
  });
  expect(forbidden).toBe(403);
});

test('未登录访问管理地址时只展示登录页', async ({ page }) => {
  await page.goto('/enterprise/accounts');

  await expect(page.getByRole('button', { name: '进入平台' })).toBeVisible();
  await expect(page.getByRole('button', { name: '账户菜单' })).toBeHidden();
  expect(await page.evaluate(() => localStorage.getItem('gongge_auth'))).toBeNull();
});

test('统一码表与负责人任期在隔离全栈中形成可回归闭环', async ({ page }) => {
  await page.goto('/enterprise/organization');
  await loginAsAdmin(page);

  const created = await page.evaluate(async () => {
    const rawSession = localStorage.getItem('gongge_auth');
    if (!rawSession) throw new Error('登录后未保存认证会话');
    const token = (JSON.parse(rawSession) as { token: string }).token;
    const headers = {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
    };
    const [unitsResponse, membersResponse] = await Promise.all([
      fetch('/api/organization/units?tenant_id=tenant_demo', { headers }),
      fetch('/api/auth/users?tenant_id=tenant_demo', { headers }),
    ]);
    const units = await unitsResponse.json() as Array<{ id: string }>;
    const members = (
      await membersResponse.json() as Array<{
        id: string;
        employee_profile_id?: string;
        employee_name?: string;
        membership_status: string;
      }>
    ).filter((member) => (
      member.id !== 'admin'
      && member.membership_status === 'active'
      && member.employee_profile_id
    ));
    if (!units[0] || members.length < 2) throw new Error('负责人 E2E 夹具不足');
    const unitResponse = await fetch('/api/organization/units', {
      method: 'POST',
      headers,
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        parent_id: units[0].id,
        code: 'M25A_E2E',
        name: 'M2.5-A E2E 项目组',
        unit_type_code: 'project',
      }),
    });
    const unit = await unitResponse.json() as { id: string };
    for (const member of members.slice(0, 2)) {
      const assignmentResponse = await fetch('/api/organization/member-org-assignments', {
        method: 'POST',
        headers,
        body: JSON.stringify({
          tenant_id: 'tenant_demo',
          employee_profile_id: member.employee_profile_id,
          org_unit_id: unit.id,
          assignment_type: 'concurrent',
        }),
      });
      if (assignmentResponse.status !== 200) {
        throw new Error(`负责人 E2E 组织归属失败：${assignmentResponse.status}`);
      }
    }
    const customTypeResponse = await fetch(
      '/api/reference-data/code-sets/organization_leader_type/items',
      {
        method: 'POST',
        headers,
        body: JSON.stringify({
          tenant_id: 'tenant_demo',
          code: 'e2e_coordinator',
          name: 'E2E 协调负责人',
          sort_order: 90,
        }),
      },
    );
    const primaryResponse = await fetch('/api/organization/leader-assignments', {
      method: 'POST',
      headers,
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        org_unit_id: unit.id,
        employee_profile_id: members[0].employee_profile_id,
        leader_type_code: 'primary',
      }),
    });
    const primary = await primaryResponse.json() as { id: string };
    const duplicatePrimary = await fetch('/api/organization/leader-assignments', {
      method: 'POST',
      headers,
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        org_unit_id: unit.id,
        employee_profile_id: members[1].employee_profile_id,
        leader_type_code: 'primary',
      }),
    });
    const customResponse = await fetch('/api/organization/leader-assignments', {
      method: 'POST',
      headers,
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        org_unit_id: unit.id,
        employee_profile_id: members[1].employee_profile_id,
        leader_type_code: 'e2e_coordinator',
      }),
    });
    const custom = await customResponse.json() as { id: string };
    return {
      customTypeStatus: customTypeResponse.status,
      primaryStatus: primaryResponse.status,
      duplicatePrimaryStatus: duplicatePrimary.status,
      customStatus: customResponse.status,
      primaryId: primary.id,
      customId: custom.id,
      unitId: unit.id,
      firstName: members[0].employee_name,
      secondName: members[1].employee_name,
    };
  });
  expect(created).toMatchObject({
    customTypeStatus: 200,
    primaryStatus: 200,
    duplicatePrimaryStatus: 409,
    customStatus: 200,
  });

  await page.reload();
  await page.getByRole('treeitem', { name: /M2.5-A E2E 项目组/ }).click();
  await expect(page.getByText('组织负责人（当前与历史）', { exact: true })).toBeVisible();
  await expect(page.getByText('主要负责人', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('E2E 协调负责人', { exact: true }).first()).toBeVisible();

  const ended = await page.evaluate(async ({ primaryId, customId }) => {
    const rawSession = localStorage.getItem('gongge_auth');
    if (!rawSession) throw new Error('登录后未保存认证会话');
    const token = (JSON.parse(rawSession) as { token: string }).token;
    const headers = {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
    };
    const [primaryEnd, customEnd, typeUpdate] = await Promise.all([
      fetch(`/api/organization/leader-assignments/${primaryId}/end`, {
        method: 'POST',
        headers,
        body: JSON.stringify({ tenant_id: 'tenant_demo' }),
      }),
      fetch(`/api/organization/leader-assignments/${customId}/end`, {
        method: 'POST',
        headers,
        body: JSON.stringify({ tenant_id: 'tenant_demo' }),
      }),
      fetch(
        '/api/reference-data/code-sets/organization_leader_type/items/e2e_coordinator',
        {
          method: 'PUT',
          headers,
          body: JSON.stringify({
            tenant_id: 'tenant_demo',
            name: 'E2E 协调负责人',
            status: 'inactive',
            sort_order: 90,
            revision: 0,
          }),
        },
      ),
    ]);
    return [primaryEnd.status, customEnd.status, typeUpdate.status];
  }, created);
  expect(ended).toEqual([200, 200, 200]);

  await page.reload();
  await page.getByRole('treeitem', { name: /M2.5-A E2E 项目组/ }).click();
  await expect(page.getByText('E2E 协调负责人', { exact: true }).first()).toBeVisible();
  await page.goto('/enterprise/reference-data');
  await page.getByRole('button', { name: /负责人类型/ }).click();
  await expect(page.getByText('E2E 协调负责人', { exact: true })).toBeVisible();
  await expect(page.getByText('停用', { exact: true }).last()).toBeVisible();
});

test('大组织按层定位、服务端分页和失败隔离在真实浏览器中闭环', async ({ browser }) => {
  const adminContext = await browser.newContext();
  const memberContext = await browser.newContext();
  const page = await adminContext.newPage();
  const memberPage = await memberContext.newPage();
  const failures: string[] = [];
  const organizationRequests: string[] = [];
  page.on('response', (response) => {
    const url = new URL(response.url());
    if (url.pathname.startsWith('/api/organization/') || url.pathname.startsWith('/api/auth/users')) {
      organizationRequests.push(`${response.request().method()} ${url.pathname}${url.search}`);
      if (response.status() >= 500 || response.status() === 404) {
        failures.push(`${response.status()} ${url.pathname}`);
      }
    }
  });

  await loginForTenant(page, 'tenant_scale', 'member_00000', 'scale-admin');
  await page.goto('/enterprise/organization');
  await expect(page.getByRole('tree', { name: '企业组织树' })).toBeVisible();
  await expect(page.getByRole('treeitem')).toHaveCount(11);

  await page.getByRole('textbox', { name: '搜索组织' }).fill('匿名组织 0499');
  await page.getByRole('button', { name: /匿名组织 0499/ }).click();
  await expect(page.getByText(/直属 .* 人 · 子树 .* 人/)).toBeVisible();

  await page.goto('/enterprise/accounts');
  await expect(page.getByRole('table', { name: '成员列表' })).toBeVisible();
  await expect(page.getByRole('table', { name: '成员列表' }).locator('tbody tr')).toHaveCount(10);
  await page.getByRole('button', { name: '下一页' }).click();
  await expect(page.getByRole('button', { name: '02', exact: true })).toHaveAttribute(
    'aria-current',
    'page',
  );
  await page.getByPlaceholder('搜索成员、账号、工号、状态或类别').fill('member_04999');
  await expect(
    page.getByRole('table', { name: '成员列表' }).getByText('member_04999', { exact: true }),
  ).toBeVisible();

  await page.goto('/enterprise/organization');
  await page.route('**/api/organization/unit-summary?**', (route) => route.fulfill({
    status: 503,
    contentType: 'application/json',
    body: JSON.stringify({ detail: 'isolated summary failure' }),
  }));
  await page.reload();
  await expect(page.getByRole('tree', { name: '企业组织树' })).toBeVisible();
  await expect(page.getByText(/当前组织部分数据加载失败：组织摘要/)).toBeVisible();
  await page.unroute('**/api/organization/unit-summary?**');

  await loginForTenant(memberPage, 'tenant_scale', 'member_00001', 'scale-member');
  await memberPage.goto('/enterprise/accounts');
  await expect(memberPage).toHaveURL(/\/workspace\/gallery$/);
  const memberEnumerationStatus = await memberPage.evaluate(async () => {
    const rawSession = localStorage.getItem('gongge_auth');
    if (!rawSession) throw new Error('普通成员会话不存在');
    const token = (JSON.parse(rawSession) as { token: string }).token;
    const response = await fetch(
      '/api/auth/users/page?tenant_id=tenant_scale&page=1&page_size=10',
      { headers: { Authorization: `Bearer ${token}` } },
    );
    return response.status;
  });
  expect(memberEnumerationStatus).toBe(403);

  expect(failures).toEqual(['503 /api/organization/unit-summary']);
  expect(organizationRequests.some((request) => (
    request.startsWith('GET /api/organization/units?')
    || request.startsWith('GET /api/auth/users?')
  ))).toBe(false);
  expect(organizationRequests.some((request) => request.includes('/unit-children?'))).toBe(true);
  expect(organizationRequests.some((request) => request.includes('/unit-search?'))).toBe(true);
  expect(organizationRequests.some((request) => request.includes('/api/auth/users/page?'))).toBe(true);
  await adminContext.close();
  await memberContext.close();
});

test('治理角色按组织子树授权，范围管理员与普通成员在真实页面保持同一边界', async ({ browser }) => {
  const adminContext = await browser.newContext();
  const scopedContext = await browser.newContext();
  const ordinaryContext = await browser.newContext();
  const adminPage = await adminContext.newPage();
  const scopedPage = await scopedContext.newPage();
  const ordinaryPage = await ordinaryContext.newPage();
  const failures: string[] = [];

  for (const page of [adminPage, scopedPage, ordinaryPage]) {
    page.on('response', (response) => {
      const path = new URL(response.url()).pathname;
      if (
        (path.startsWith('/api/organization/') || path.startsWith('/api/auth/'))
        && (response.status() === 404 || response.status() >= 500)
      ) {
        failures.push(`${response.status()} ${path}`);
      }
    });
  }

  await adminPage.goto('/enterprise/dashboard');
  await loginAsAdmin(adminPage);
  const fixture = await adminPage.evaluate(async () => {
    const rawSession = localStorage.getItem('gongge_auth');
    if (!rawSession) throw new Error('管理员会话不存在');
    const token = (JSON.parse(rawSession) as { token: string }).token;
    const headers = {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
    };

    async function request<T>(path: string, init?: RequestInit): Promise<T> {
      const response = await fetch(path, { ...init, headers: { ...headers, ...init?.headers } });
      const body = await response.json();
      if (!response.ok) {
        throw new Error(
          `${init?.method || 'GET'} ${path}: ${response.status} ${JSON.stringify(body)}`,
        );
      }
      return body as T;
    }

    const units = await request<Array<{ id: string; code: string; name: string }>>(
      '/api/organization/units?tenant_id=tenant_demo',
    );
    const scoped = units.find((unit) => unit.code === 'E2E_SCOPED_BRANCH');
    const child = units.find((unit) => unit.code === 'E2E_SCOPED_CHILD');
    const sibling = units.find((unit) => unit.code === 'E2E_SIBLING_BRANCH');
    const root = units.find((unit) => unit.code === 'ROOT');
    if (!scoped || !child || !sibling || !root) throw new Error('治理范围组织夹具不完整');

    const role = await request<{ id: string; role_code: string }>(
      '/api/organization/business-roles',
      {
        method: 'POST',
        body: JSON.stringify({
          tenant_id: 'tenant_demo',
          role_code: 'e2e_scoped_org_admin',
          name: 'E2E 范围组织管理员',
          role_kind: 'governance',
          category: 'governance',
          permissions: [
            'member.read',
            'member.manage',
            'organization.read',
            'organization.manage',
            'position.read',
            'position.manage',
            'authorization.read',
          ],
        }),
      },
    );
    const assignment = await request<{ id: string }>(
      '/api/organization/employee-role-assignments',
      {
        method: 'POST',
        body: JSON.stringify({
          tenant_id: 'tenant_demo',
          employee_profile_id: 'profile_e2e_member',
          role_code: role.role_code,
          scope_type: 'org_unit',
          scope_id: scoped.id,
          include_descendants: true,
          grant_reason: '隔离浏览器治理范围回归',
          effective_until: '2099-12-31T00:00:00Z',
        }),
      },
    );
    return {
      assignmentId: assignment.id,
      childId: child.id,
      rootId: root.id,
      scopedId: scoped.id,
      scopedName: scoped.name,
      siblingId: sibling.id,
    };
  });

  await adminPage.goto('/enterprise/organization-roles?section=assignments');
  await expect(adminPage.getByRole('table', { name: '成员角色授权列表' })).toContainText(
    'E2E 范围组织管理员',
  );
  await expect(adminPage.getByRole('table', { name: '成员角色授权列表' })).toContainText(
    '含下级',
  );

  await scopedPage.goto('/enterprise/dashboard');
  await login(scopedPage, 'member', 'member');
  await expect(scopedPage.getByRole('button', { name: '组织与岗位' })).toBeVisible();
  await expect(scopedPage.getByRole('button', { name: '组织角色' })).toBeVisible();
  await expect(scopedPage.getByRole('button', { name: '数据码表' })).toBeHidden();

  await scopedPage.goto('/enterprise/organization');
  const tree = scopedPage.getByRole('tree', { name: '企业组织树' });
  await expect(tree).toBeVisible();
  await expect(tree.getByRole('treeitem').first()).toContainText(fixture.scopedName);
  await expect(tree).not.toContainText('E2E 兄弟分部');

  const boundary = await scopedPage.evaluate(async (ids) => {
    const rawSession = localStorage.getItem('gongge_auth');
    if (!rawSession) throw new Error('范围管理员会话不存在');
    const token = (JSON.parse(rawSession) as { token: string }).token;
    const headers = {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
    };
    const update = await fetch(`/api/organization/units/${ids.childId}`, {
      method: 'PUT',
      headers,
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        name: 'E2E 授权分部下级（已验证）',
      }),
    });
    const parent = await fetch(
      `/api/organization/unit-children?tenant_id=tenant_demo&parent_id=${ids.rootId}`,
      { headers },
    );
    const sibling = await fetch(
      `/api/organization/unit-children?tenant_id=tenant_demo&parent_id=${ids.siblingId}`,
      { headers },
    );
    const grants = await fetch(
      '/api/organization/effective-permissions?tenant_id=tenant_demo',
      { headers },
    );
    return {
      grantRows: await grants.json() as Array<{
        permission_code: string;
        role_code: string;
        scope_id: string;
        include_descendants: boolean;
      }>,
      grants: grants.status,
      parent: parent.status,
      sibling: sibling.status,
      update: update.status,
    };
  }, fixture);
  expect(boundary.update).toBe(200);
  expect(boundary.parent).toBe(403);
  expect(boundary.sibling).toBe(403);
  expect(boundary.grants).toBe(200);
  expect(boundary.grantRows).toContainEqual(expect.objectContaining({
    permission_code: 'organization.manage',
    role_code: 'e2e_scoped_org_admin',
    scope_id: fixture.scopedId,
    include_descendants: true,
  }));

  await scopedPage.goto('/enterprise/organization-roles?section=effective');
  const explanations = scopedPage.getByRole('table', { name: '有效权限解释列表' });
  await expect(explanations).toContainText('organization.manage');
  await expect(explanations).toContainText('E2E 范围组织管理员');
  await expect(explanations).toContainText('含下级');

  await ordinaryPage.goto('/enterprise/dashboard');
  await login(ordinaryPage, 'other-member', 'other-member');
  await ordinaryPage.goto('/enterprise/organization');
  await expect(ordinaryPage).toHaveURL(/\/workspace\/gallery$/);
  const ordinaryStatus = await ordinaryPage.evaluate(async () => {
    const rawSession = localStorage.getItem('gongge_auth');
    if (!rawSession) throw new Error('普通成员会话不存在');
    const token = (JSON.parse(rawSession) as { token: string }).token;
    const response = await fetch('/api/organization/units?tenant_id=tenant_demo', {
      headers: { Authorization: `Bearer ${token}` },
    });
    return response.status;
  });
  expect(ordinaryStatus).toBe(403);
  expect(failures).toEqual([]);

  await adminContext.close();
  await scopedContext.close();
  await ordinaryContext.close();
});

/**
 * @Time       : 2026/08/13
 * @Author     : zhanglp8181
 * @File       : skill-distillation-s4.fullstack.e2e.ts
 * @CallChain  : Chromium → Skill 导入 → AgentLoop → DynamicTask → Attention/Knowledge/Result
 * @Description: 验证动态任务实际消费固定 Skill，并经人工澄清和真实知识 Operation 恢复完成。
 */

import { expect, test, type Page } from '@playwright/test';

const SKILL_NAME = 's4-dynamic-guidance';
const SKILL_MARKDOWN = [
  '---',
  `name: ${SKILL_NAME}`,
  'description: S4 动态任务先确认范围、再检索证据并形成可审计结论。',
  'allowed-tools: knowledge.search',
  '---',
  '# S4 dynamic guidance',
  'S4-DYNAMIC-FULL-GUIDANCE：必须先确认范围，再检索知识证据，最后形成可审计结论。',
  '',
].join('\n');

const CODE_SKILL_NAME = 's4-code-guidance';
const CODE_SKILL_MARKDOWN = [
  '---',
  `name: ${CODE_SKILL_NAME}`,
  'description: S4 代码交付必须先读、审批写入、隔离回归、审批提交并形成证据。',
  'allowed-tools:',
  '  - workspace.refund.read',
  '  - workspace.refund.apply-set',
  '  - workspace.refund.check',
  '  - workspace.refund.commit',
  '---',
  '# S4 code delivery guidance',
  'S4-CODE-FULL-GUIDANCE：代码交付必须使用受管工作区工具，不得执行 Skill 包脚本。',
  '',
].join('\n');
const CODE_DENY_SKILL_NAME = 's4-code-deny-guidance';
const CODE_DENY_SKILL_MARKDOWN = CODE_SKILL_MARKDOWN.replace(
  `name: ${CODE_SKILL_NAME}`,
  `name: ${CODE_DENY_SKILL_NAME}`,
);
const CODE_COUNTERMAND_SKILL_NAME = 's4-code-countermand-guidance';
const CODE_COUNTERMAND_SKILL_MARKDOWN = CODE_SKILL_MARKDOWN.replace(
  `name: ${CODE_SKILL_NAME}`,
  `name: ${CODE_COUNTERMAND_SKILL_NAME}`,
);

async function loginAsMember(page: Page, agentId = 'agent_e2e_member_employee') {
  /** 通过真实认证 API 登录并固定成员自己的数字员工。 */

  await page.goto('/enterprise/dashboard');
  const status = await page.evaluate(async (selectedAgentId) => {
    const response = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tenant_id: 'tenant_demo', username: 'member', password: 'member' }),
    });
    const body = await response.json();
    if (response.ok) {
      localStorage.setItem('gongge_auth', JSON.stringify(body));
      localStorage.setItem('gongge_onboarding_guide_seen', '1');
      localStorage.setItem('gongge_quick_start_guide_seen', '1');
      localStorage.setItem('gongge_enterprise_agent_scope', selectedAgentId);
    }
    return response.status;
  }, agentId);
  expect(status).toBe(200);
}

async function importFixedGitHubSkill(page: Page, skillName: string) {
  /** 从方案固定 commit 的单一仓库子目录导入一个上游原始 Skill。 */

  await page.goto('/enterprise/general-skills');
  await page.getByRole('button', { name: /新增/ }).click();
  await page.getByRole('menuitem', { name: '安全导入 Skill' }).click();
  const dialog = page.getByRole('dialog', { name: '安全导入 Skill 包' });
  await dialog.getByRole('tab', { name: 'GitHub 固定版本' }).click();
  await dialog.getByLabel('GitHub 仓库地址').fill('https://github.com/mattpocock/skills');
  await dialog.getByLabel('完整 commit SHA').fill('84fdeffd12f2ee307994d1eb6feb48173b6e0502');
  await dialog.getByLabel('仓库内 Skill 目录').fill(`skills/engineering/${skillName}`);
  const previewResponse = page.waitForResponse((response) => (
    new URL(response.url()).pathname === '/api/enterprise/general-skill-import-jobs'
    && response.request().method() === 'POST'
  ));
  await dialog.getByRole('button', { name: '生成安全预览' }).click();
  expect((await previewResponse).status()).toBe(202);
  await expect(dialog.getByText(skillName, { exact: true })).toBeVisible({ timeout: 30_000 });
  const confirmResponse = page.waitForResponse((response) => (
    new URL(response.url()).pathname.endsWith('/confirm')
    && response.request().method() === 'POST'
  ));
  await dialog.getByRole('button', { name: '固定版本并绑定' }).click();
  expect((await confirmResponse).status()).toBe(200);
  await expect(dialog).not.toBeVisible();
}

async function loginAsAdmin(page: Page) {
  /** 切换为独立租户管理员，办理与发起人分离的代码操作审批。 */

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

async function importDynamicGuidance(page: Page) {
  /** 经正式 UI 导入 model-allowed Skill 并固定到当前数字员工。 */

  await page.goto('/enterprise/general-skills');
  await page.getByRole('button', { name: /新增/ }).click();
  await page.getByRole('menuitem', { name: '安全导入 Skill' }).click();
  const dialog = page.getByRole('dialog', { name: '安全导入 Skill 包' });
  await dialog.locator('input[type="file"]').setInputFiles({
    name: 'SKILL.md',
    mimeType: 'text/markdown',
    buffer: Buffer.from(SKILL_MARKDOWN),
  });
  await dialog.getByRole('button', { name: '生成安全预览' }).click();
  await expect(dialog.getByText(SKILL_NAME, { exact: true })).toBeVisible();
  await expect(dialog.getByText('允许模型选择', { exact: true })).toBeVisible();
  await expect(dialog.getByText(/knowledge\.search/)).toBeVisible();
  const confirmed = page.waitForResponse((response) => (
    new URL(response.url()).pathname.endsWith('/confirm')
    && response.request().method() === 'POST'
  ));
  await dialog.getByRole('button', { name: '固定版本并绑定' }).click();
  expect((await confirmed).status()).toBe(200);
}

async function importCodeGuidance(
  page: Page,
  skillName = CODE_SKILL_NAME,
  markdown = CODE_SKILL_MARKDOWN,
) {
  /** 经同一安全导入 UI 固定代码交付 Skill 与四项非扩权工具声明。 */

  await page.goto('/enterprise/general-skills');
  await page.getByRole('button', { name: /新增/ }).click();
  await page.getByRole('menuitem', { name: '安全导入 Skill' }).click();
  const dialog = page.getByRole('dialog', { name: '安全导入 Skill 包' });
  await dialog.locator('input[type="file"]').setInputFiles({
    name: 'SKILL.md',
    mimeType: 'text/markdown',
    buffer: Buffer.from(markdown),
  });
  await dialog.getByRole('button', { name: '生成安全预览' }).click();
  await expect(dialog.getByText(skillName, { exact: true })).toBeVisible();
  await expect(dialog.getByText(/workspace\.refund\.apply-set/)).toBeVisible();
  await dialog.getByRole('button', { name: '固定版本并绑定' }).click();
  await expect(dialog).not.toBeVisible();
}

test('S4 动态任务从 Skill 目录到人工澄清、知识 Operation 和结果形成真实闭环', async ({ page }) => {
  test.setTimeout(90_000);
  const failures: string[] = [];
  page.on('pageerror', (error) => failures.push(error.message));
  page.on('response', (response) => {
    const path = new URL(response.url()).pathname;
    if (path.startsWith('/api/') && response.status() >= 500) failures.push(`${response.status()} ${path}`);
  });
  await loginAsMember(page);
  await importDynamicGuidance(page);
  const sessionId = await page.evaluate(async () => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch('/api/chat/sessions', {
      method: 'POST',
      headers: { Authorization: `Bearer ${auth.token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        agent_id: 'agent_e2e_member_employee',
        title: 'S4 动态 Skill 闭环',
        origin: 'owned',
      }),
    });
    const body = await response.json() as { id?: string; detail?: string };
    if (!response.ok || !body.id) throw new Error(body.detail || 'session creation failed');
    return body.id;
  });

  await page.goto(`/workspace/chat/${sessionId}`);
  const streamed = await page.evaluate(async (id) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch('/api/chat/stream', {
      method: 'POST',
      headers: { Authorization: `Bearer ${auth.token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        session_id: id,
        agent_id: 'agent_e2e_member_employee',
        client_turn_id: 'turn_s4_browser_dynamic',
        message: 'S4动态：诊断企业知识检索结果不稳定的问题，先确认范围再给出可审计结论',
        channel: 'web',
      }),
    });
    return { status: response.status, body: await response.text() };
  }, sessionId);
  expect(streamed.status).toBe(200);
  expect(streamed.body).toContain('event: complete');
  await page.reload();
  await expect(page.getByRole('paragraph').filter({ hasText: /任务已暂停，正在等待你补充信息/ })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByLabel('动态任务控制')).toContainText('S4动态');

  const initial = await page.evaluate(async (id) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch(`/api/chat/sessions/${id}/events?tenant_id=tenant_demo`, {
      headers: { Authorization: `Bearer ${auth.token}` },
    });
    const events = await response.json() as Array<{ event_type: string; data?: Record<string, unknown> }>;
    const delegated = events.find((event) => event.event_type === 'dynamic_task_delegated');
    return { events, executionId: String(delegated?.data?.execution_id || '') };
  }, sessionId);
  expect(initial.executionId).not.toBe('');
  expect(initial.events.filter((event) => event.event_type === 'skill_loaded')).toHaveLength(1);
  expect(initial.events.find((event) => event.event_type === 'skill_loaded')?.data).toMatchObject({
    selection_mode: 'auto',
    consumer: 'dynamic_task',
  });

  await page.goto('/enterprise/work-items');
  await page.getByRole('button', { name: /确认诊断范围/ }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByText('请选择本次需要巡检的合同范围')).toBeVisible();
  await dialog.getByRole('button', { name: '未来30天到期' }).click();
  await dialog.getByRole('button', { name: '补充并继续' }).click();

  await expect.poll(async () => page.evaluate(async (executionId) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch(`/api/executions/${executionId}?tenant_id=tenant_demo`, {
      headers: { Authorization: `Bearer ${auth.token}` },
    });
    return response.json() as Promise<{ status: string; steps: Array<{ kind: string; status: string }>; usage: { tool_calls?: number } }>;
  }, initial.executionId), { timeout: 30_000 }).toMatchObject({
    status: 'succeeded',
    steps: [
      { kind: 'clarification', status: 'succeeded' },
      { kind: 'knowledge', status: 'succeeded' },
      { kind: 'answer', status: 'succeeded' },
    ],
    usage: { tool_calls: 1 },
  });

  await page.goto(`/workspace/chat/${sessionId}`);
  await expect(page.getByRole('main').getByText(/S4-DYNAMIC-GUIDED-SUCCESS/)).toBeVisible({ timeout: 30_000 });
  await expect(page.getByLabel('动态任务控制').filter({ hasText: '已完成' }).last()).toBeVisible();
  expect(failures).toEqual([]);
});

test('S4 研发交付数字员工经五次独立审批完成迁移、后端前端回归和 Git 提交', async ({ page }) => {
  test.setTimeout(120_000);
  const failures: string[] = [];
  page.on('pageerror', (error) => failures.push(error.message));
  page.on('response', (response) => {
    const path = new URL(response.url()).pathname;
    if (path.startsWith('/api/') && response.status() >= 500) failures.push(`${response.status()} ${path}`);
  });
  await loginAsMember(page);
  await importCodeGuidance(page);
  const sessionId = await page.evaluate(async () => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch('/api/chat/sessions', {
      method: 'POST',
      headers: { Authorization: `Bearer ${auth.token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        agent_id: 'agent_e2e_member_employee',
        title: 'S4 受管代码交付',
        origin: 'owned',
      }),
    });
    const body = await response.json() as { id?: string; detail?: string };
    if (!response.ok || !body.id) throw new Error(body.detail || 'session creation failed');
    return body.id;
  });
  const started = await page.evaluate(async (id) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch('/api/chat/stream', {
      method: 'POST',
      headers: { Authorization: `Bearer ${auth.token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        session_id: id,
        agent_id: 'agent_e2e_member_employee',
        client_turn_id: 'turn_s4_code_delivery',
        message: 'S4代码：在受管演示仓库把高金额退款改为必须审批，完成真实测试和提交',
        channel: 'web',
      }),
    });
    return { status: response.status, body: await response.text() };
  }, sessionId);
  expect(started.status).toBe(200);
  expect(started.body).toContain('event: complete');

  const executionId = await page.evaluate(async (id) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch(`/api/chat/sessions/${id}/events?tenant_id=tenant_demo`, {
      headers: { Authorization: `Bearer ${auth.token}` },
    });
    const events = await response.json() as Array<{ event_type: string; data?: Record<string, unknown> }>;
    return String(events.find((event) => event.event_type === 'dynamic_task_delegated')?.data?.execution_id || '');
  }, sessionId);
  expect(executionId).not.toBe('');

  await loginAsAdmin(page);
  for (const title of [
    '批准受管代码工作区执行检查',
    '批准受管代码工作区变更',
    '批准受管代码工作区执行检查',
    '批准受管代码工作区执行检查',
    '批准受管代码工作区变更',
  ]) {
    await page.goto('/enterprise/work-items');
    const card = page.getByRole('button', { name: new RegExp(title) }).first();
    await expect(card).toBeVisible({ timeout: 30_000 });
    await card.click();
    const dialog = page.getByRole('dialog');
    await expect(dialog.getByLabel('待批准受管代码操作')).toContainText('refund-demo');
    await dialog.getByRole('button', { name: '仅批准本次操作' }).click();
  }

  await loginAsMember(page);
  await expect.poll(async () => page.evaluate(async (id) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch(`/api/executions/${id}?tenant_id=tenant_demo`, {
      headers: { Authorization: `Bearer ${auth.token}` },
    });
    return response.json() as Promise<Record<string, unknown>>;
  }, executionId), { timeout: 45_000 }).toMatchObject({ status: 'succeeded' });
  const details = await page.evaluate(async ({ sessionId, executionId }) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch(`/api/enterprise/traces/${sessionId}?tenant_id=tenant_demo`, {
      headers: { Authorization: `Bearer ${auth.token}` },
    });
    const trace = await response.json() as {
      sop_runtime?: Array<{
        instance_id: string;
        operations?: Array<{
          operation_name: string;
          request?: Record<string, unknown>;
          result?: { data?: Record<string, unknown> };
        }>;
      }>;
    };
    return trace.sop_runtime?.find((item) => item.instance_id === executionId);
  }, { sessionId, executionId });
  const operations = details?.operations || [];
  const checks = operations.filter((item) => item.operation_name === 'workspace.refund.check');
  expect(checks.map((item) => item.request?.profile)).toEqual([
    'backend-red',
    'backend-unit',
    'frontend-unit',
  ]);
  expect(checks[0]?.result?.data).toMatchObject({
    exit_code: 1,
    passed: true,
    expected_exit_codes: [1],
  });
  expect(checks.slice(1).map((item) => item.result?.data?.exit_code)).toEqual([0, 0]);
  expect(
    operations.find((item) => item.operation_name === 'workspace.refund.apply-set')?.result?.data,
  ).toMatchObject({ changed_count: 3, replayed: false });
  expect(
    operations.find((item) => item.operation_name === 'workspace.refund.commit')?.result?.data,
  ).toMatchObject({ replayed: false });

  await page.goto(`/workspace/chat/${sessionId}`);
  await expect(page.getByRole('main').getByText(/S4-CODE-DELIVERY-SUCCESS/)).toBeVisible({ timeout: 30_000 });
  expect(failures).toEqual([]);
});

test('S4 受管代码写入被独立管理员拒绝后零写入并稳定终止', async ({ page }) => {
  test.setTimeout(90_000);
  await loginAsMember(page);
  await importCodeGuidance(page, CODE_DENY_SKILL_NAME, CODE_DENY_SKILL_MARKDOWN);
  const started = await page.evaluate(async () => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const sessionResponse = await fetch('/api/chat/sessions', {
      method: 'POST',
      headers: { Authorization: `Bearer ${auth.token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        agent_id: 'agent_e2e_member_employee',
        title: 'S4 受管代码拒绝',
        origin: 'owned',
      }),
    });
    const session = await sessionResponse.json() as { id: string };
    const response = await fetch('/api/chat/stream', {
      method: 'POST',
      headers: { Authorization: `Bearer ${auth.token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        session_id: session.id,
        agent_id: 'agent_e2e_member_employee',
        client_turn_id: 'turn_s4_code_denied',
        message: 'S4代码拒绝：尝试修改高金额退款规则，但本次管理员必须拒绝写入',
        channel: 'web',
      }),
    });
    const body = await response.text();
    const executionId = body.match(/"execution_id":\s*"([^"]+)"/)?.[1] || '';
    return { status: response.status, executionId };
  });
  expect(started.status).toBe(200);
  expect(started.executionId).not.toBe('');

  await loginAsAdmin(page);
  await page.goto('/enterprise/work-items');
  const redCheck = page.getByRole('button', { name: /批准受管代码工作区执行检查/ }).first();
  await expect(redCheck).toBeVisible({ timeout: 30_000 });
  await redCheck.click();
  await page.getByRole('dialog').getByRole('button', { name: '仅批准本次操作' }).click();
  await page.goto('/enterprise/work-items');
  const card = page.getByRole('button', { name: /批准受管代码工作区变更/ }).first();
  await expect(card).toBeVisible({ timeout: 30_000 });
  await card.click();
  await page.getByRole('dialog').getByRole('button', { name: '拒绝操作' }).click();

  await loginAsMember(page);
  await expect.poll(async () => page.evaluate(async (executionId) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch(`/api/executions/${executionId}?tenant_id=tenant_demo`, {
      headers: { Authorization: `Bearer ${auth.token}` },
    });
    return response.json();
  }, started.executionId), { timeout: 30_000 }).toMatchObject({
    status: 'failed',
    terminal_reason: { code: 'DYNAMIC_LOCAL_DENIED' },
    usage: { tool_calls: 2 },
  });
});

test('S4 Skill 在审批后解绑会 countermand 零写入且恢复绑定后可重新规划', async ({ page }) => {
  /** 验证旧 Use 不会借已完成审批越过实时绑定资格，新 Execution 可采用恢复后的绑定。 */

  test.setTimeout(120_000);
  await loginAsMember(page);
  await importCodeGuidance(
    page,
    CODE_COUNTERMAND_SKILL_NAME,
    CODE_COUNTERMAND_SKILL_MARKDOWN,
  );
  const started = await page.evaluate(async (skillName) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const sessionResponse = await fetch('/api/chat/sessions', {
      method: 'POST',
      headers: { Authorization: `Bearer ${auth.token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        agent_id: 'agent_e2e_member_employee',
        title: 'S4 Skill 撤权',
        origin: 'owned',
      }),
    });
    const session = await sessionResponse.json() as { id: string };
    const response = await fetch('/api/chat/stream', {
      method: 'POST',
      headers: { Authorization: `Bearer ${auth.token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        session_id: session.id,
        agent_id: 'agent_e2e_member_employee',
        client_turn_id: 'turn_s4_code_countermand',
        message: 'S4代码撤权：准备高金额退款审批变更，在写入审批后验证 Skill 实时撤权',
        channel: 'web',
      }),
    });
    const body = await response.text();
    return {
      status: response.status,
      sessionId: session.id,
      executionId: body.match(/"execution_id":\s*"([^"]+)"/)?.[1] || '',
      skillName,
    };
  }, CODE_COUNTERMAND_SKILL_NAME);
  expect(started.status).toBe(200);
  expect(started.executionId).not.toBe('');

  await loginAsAdmin(page);
  await page.goto('/enterprise/work-items');
  const redCheck = page.getByRole('button', { name: /批准受管代码工作区执行检查/ }).first();
  await expect(redCheck).toBeVisible({ timeout: 30_000 });
  await redCheck.click();
  await page.getByRole('dialog').getByRole('button', { name: '仅批准本次操作' }).click();
  await page.goto('/enterprise/work-items');
  await expect(page.getByRole('button', { name: /批准受管代码工作区变更/ }).first()).toBeVisible({ timeout: 30_000 });

  await loginAsMember(page);
  const archived = await page.evaluate(async (slug) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch(
      `/api/enterprise/general-skills/${slug}/archive?tenant_id=tenant_demo&agent_id=agent_e2e_member_employee`,
      { method: 'POST', headers: { Authorization: `Bearer ${auth.token}` } },
    );
    const catalogResponse = await fetch(
      '/api/enterprise/general-skill-governance/agents/agent_e2e_member_employee/catalog',
      { headers: { Authorization: `Bearer ${auth.token}` } },
    );
    const catalog = await catalogResponse.json() as { items?: Array<{ name: string }> };
    return {
      status: response.status,
      catalogStatus: catalogResponse.status,
      names: (catalog.items || []).map((item) => item.name),
    };
  }, CODE_COUNTERMAND_SKILL_NAME);
  expect(archived).toMatchObject({ status: 200, catalogStatus: 200 });
  expect(archived.names).not.toContain(CODE_COUNTERMAND_SKILL_NAME);

  await loginAsAdmin(page);
  await page.goto('/enterprise/work-items');
  const writeApproval = page.getByRole('button', { name: /批准受管代码工作区变更/ }).first();
  await writeApproval.click();
  await page.getByRole('dialog').getByRole('button', { name: '仅批准本次操作' }).click();

  await loginAsMember(page);
  await expect.poll(async () => page.evaluate(async (executionId) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch(`/api/executions/${executionId}?tenant_id=tenant_demo`, {
      headers: { Authorization: `Bearer ${auth.token}` },
    });
    return response.json();
  }, started.executionId), { timeout: 30_000 }).toMatchObject({
    status: 'failed',
    terminal_reason: { code: 'GENERAL_SKILL_COUNTERMANDED' },
    usage: { tool_calls: 2 },
  });
  const countermandEvents = await page.evaluate(async (sessionId) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch(`/api/chat/sessions/${sessionId}/events?tenant_id=tenant_demo`, {
      headers: { Authorization: `Bearer ${auth.token}` },
    });
    return response.json() as Promise<Array<{ event_type: string; data?: Record<string, unknown> }>>;
  }, started.sessionId);
  expect(countermandEvents.find((event) => event.event_type === 'skill_countermanded')?.data).toMatchObject({
    execution_id: started.executionId,
    reason: 'GENERAL_SKILL_COUNTERMANDED',
  });
  const countermandTrace = await page.evaluate(async ({ sessionId, executionId }) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch(`/api/enterprise/traces/${sessionId}?tenant_id=tenant_demo`, {
      headers: { Authorization: `Bearer ${auth.token}` },
    });
    const trace = await response.json() as {
      sop_runtime?: Array<{ instance_id: string; operations?: Array<Record<string, unknown>> }>;
    };
    return trace.sop_runtime?.find((item) => item.instance_id === executionId)?.operations || [];
  }, { sessionId: started.sessionId, executionId: started.executionId });
  expect(countermandTrace.find((item) => item.operation_name === 'workspace.refund.apply-set')).toMatchObject({
    status: 'cancelled',
    result: {},
  });

  const restored = await page.evaluate(async (slug) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch(
      `/api/enterprise/general-skills/${slug}/publish?tenant_id=tenant_demo&agent_id=agent_e2e_member_employee`,
      { method: 'POST', headers: { Authorization: `Bearer ${auth.token}` } },
    );
    const publishBody = await response.json() as Record<string, unknown>;
    const catalogResponse = await fetch(
      '/api/enterprise/general-skill-governance/agents/agent_e2e_member_employee/catalog',
      { headers: { Authorization: `Bearer ${auth.token}` } },
    );
    const catalog = await catalogResponse.json() as {
      items?: Array<{ name: string; skill_id: string }>;
    };
    return {
      status: response.status,
      publishBody,
      catalog,
      names: (catalog.items || []).map((item) => item.name),
      skillId: (catalog.items || []).find((item) => item.name === slug)?.skill_id || '',
    };
  }, CODE_COUNTERMAND_SKILL_NAME);
  expect(restored.publishBody).toMatchObject({
    slug: CODE_COUNTERMAND_SKILL_NAME,
    status: 'published',
    binding_status: 'active',
  });
  expect(restored).toMatchObject({
    status: 200,
    names: expect.arrayContaining([CODE_COUNTERMAND_SKILL_NAME]),
  });
  expect(restored.skillId).not.toBe('');

  const replanned = await page.evaluate(async () => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const sessionResponse = await fetch('/api/chat/sessions', {
      method: 'POST',
      headers: { Authorization: `Bearer ${auth.token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        agent_id: 'agent_e2e_member_employee',
        title: 'S4 Skill 撤权恢复重规划',
        origin: 'owned',
      }),
    });
    const session = await sessionResponse.json() as { id: string };
    const streamResponse = await fetch('/api/chat/stream', {
      method: 'POST',
      headers: { Authorization: `Bearer ${auth.token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        session_id: session.id,
        agent_id: 'agent_e2e_member_employee',
        client_turn_id: 'turn_s4_code_countermand_replan',
        message: 'S4代码撤权恢复：重新规划高金额退款审批变更',
        channel: 'web',
      }),
    });
    await streamResponse.text();
    const eventsResponse = await fetch(
      `/api/chat/sessions/${session.id}/events?tenant_id=tenant_demo`,
      { headers: { Authorization: `Bearer ${auth.token}` } },
    );
    const events = await eventsResponse.json() as Array<{
      event_type: string;
      data?: Record<string, unknown>;
    }>;
    return {
      streamStatus: streamResponse.status,
      selectedSkill: events.find((event) => event.event_type === 'skill_loaded')?.data,
    };
  });
  expect(replanned.streamStatus).toBe(200);
  expect(replanned.selectedSkill).toMatchObject({
    skill_id: restored.skillId,
    selection_mode: 'auto',
    consumer: 'dynamic_task',
  });

  await loginAsAdmin(page);
  await page.goto('/enterprise/work-items');
  const cleanupApproval = page.getByRole('button', {
    name: /批准受管代码工作区执行检查/,
  }).first();
  await expect(cleanupApproval).toBeVisible({ timeout: 30_000 });
  await cleanupApproval.click();
  await page.getByRole('dialog').getByRole('button', { name: '拒绝操作' }).click();
});

test('S4 疑难诊断分身组合固定上游 Skill、知识、记忆和代码工具完成 red-green 闭环', async ({ page }) => {
  /** 验证场景 B 的三 Skill、Agent 隔离上下文、真实知识 Operation 与受管代码回归。 */

  test.setTimeout(180_000);
  const failures: string[] = [];
  page.on('pageerror', (error) => failures.push(error.message));
  page.on('response', (response) => {
    const path = new URL(response.url()).pathname;
    if (path.startsWith('/api/') && response.status() >= 500) failures.push(`${response.status()} ${path}`);
  });
  await loginAsMember(page, 'agent_e2e_diagnosis');
  for (const skillName of ['diagnosing-bugs', 'tdd', 'codebase-design']) {
    await importFixedGitHubSkill(page, skillName);
  }
  const catalogs = await page.evaluate(async () => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const headers = { Authorization: `Bearer ${auth.token}` };
    const diagnosis = await fetch(
      '/api/enterprise/general-skill-governance/agents/agent_e2e_diagnosis/catalog',
      { headers },
    ).then((response) => response.json());
    const other = await fetch(
      '/api/enterprise/general-skill-governance/agents/agent_e2e_member_employee/catalog',
      { headers },
    ).then((response) => response.json());
    return { diagnosis, other } as {
      diagnosis: { items: Array<{ name: string; skill_id: string; revision_id: string }> };
      other: { items: Array<{ name: string }> };
    };
  });
  const expectedSkills = ['codebase-design', 'diagnosing-bugs', 'tdd'];
  expect(catalogs.diagnosis.items.map((item) => item.name).sort()).toEqual(expectedSkills);
  expect(catalogs.other.items.map((item) => item.name)).not.toEqual(expect.arrayContaining(expectedSkills));

  const started = await page.evaluate(async () => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const sessionResponse = await fetch('/api/chat/sessions', {
      method: 'POST',
      headers: { Authorization: `Bearer ${auth.token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        agent_id: 'agent_e2e_diagnosis',
        title: 'S4 疑难故障诊断分身',
        origin: 'owned',
      }),
    });
    const session = await sessionResponse.json() as { id: string };
    const response = await fetch('/api/chat/stream', {
      method: 'POST',
      headers: { Authorization: `Bearer ${auth.token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        session_id: session.id,
        agent_id: 'agent_e2e_diagnosis',
        client_turn_id: 'turn_s4_diagnosis_scene_b',
        message: 'S4诊断：无 SOP 普通问答忘记当前分身用户偏好，请先复现再修复，不能先猜原因',
        channel: 'web',
      }),
    });
    const body = await response.text();
    return {
      status: response.status,
      sessionId: session.id,
      executionId: body.match(/"execution_id":\s*"([^"]+)"/)?.[1] || '',
    };
  });
  expect(started.status).toBe(200);
  expect(started.executionId).not.toBe('');

  await loginAsAdmin(page);
  for (const title of [
    '批准受管代码工作区变更',
    '批准受管代码工作区执行检查',
  ]) {
    await page.goto('/enterprise/work-items');
    const card = page.getByRole('button', { name: new RegExp(title) }).first();
    await expect(card).toBeVisible({ timeout: 30_000 });
    await card.click();
    await expect(page.getByRole('dialog').getByLabel('待批准受管代码操作')).toContainText(
      'memory-diagnosis-demo',
    );
    await page.getByRole('dialog').getByRole('button', { name: '仅批准本次操作' }).click();
  }

  await loginAsMember(page, 'agent_e2e_diagnosis');
  await page.goto('/enterprise/work-items');
  const hypothesis = page.getByRole('button', { name: /确认可证伪根因假设/ }).first();
  await expect(hypothesis).toBeVisible({ timeout: 30_000 });
  await hypothesis.click();
  const hypothesisDialog = page.getByRole('dialog');
  await expect(hypothesisDialog.getByText(/red 已命中原始症状/)).toBeVisible();
  await hypothesisDialog.getByRole('button', {
    name: '无 SOP 分支把已召回 memory_context 替换为空数组',
  }).click();
  await hypothesisDialog.getByRole('button', { name: '补充并继续' }).click();

  await loginAsAdmin(page);
  for (const title of [
    '批准受管代码工作区变更',
    '批准受管代码工作区执行检查',
  ]) {
    await page.goto('/enterprise/work-items');
    const card = page.getByRole('button', { name: new RegExp(title) }).first();
    await expect(card).toBeVisible({ timeout: 30_000 });
    await card.click();
    await page.getByRole('dialog').getByRole('button', { name: '仅批准本次操作' }).click();
  }

  await loginAsMember(page, 'agent_e2e_diagnosis');
  await expect.poll(async () => page.evaluate(async (executionId) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch(`/api/executions/${executionId}?tenant_id=tenant_demo`, {
      headers: { Authorization: `Bearer ${auth.token}` },
    });
    return response.json();
  }, started.executionId), { timeout: 45_000 }).toMatchObject({
    status: 'succeeded',
    usage: { tool_calls: 6 },
  });
  const evidence = await page.evaluate(async ({ sessionId, executionId }) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const headers = { Authorization: `Bearer ${auth.token}` };
    const events = await fetch(
      `/api/chat/sessions/${sessionId}/events?tenant_id=tenant_demo`,
      { headers },
    ).then((response) => response.json()) as Array<{
      event_type: string;
      data?: Record<string, unknown>;
    }>;
    const trace = await fetch(
      `/api/enterprise/traces/${sessionId}?tenant_id=tenant_demo`,
      { headers },
    ).then((response) => response.json()) as {
      sop_runtime?: Array<{
        instance_id: string;
        plan_revisions?: Array<Record<string, unknown>>;
        operations?: Array<{
          operation_name: string;
          request?: Record<string, unknown>;
          result?: { data?: Record<string, unknown> };
        }>;
      }>;
    };
    return {
      events,
      runtime: trace.sop_runtime?.find((item) => item.instance_id === executionId),
    };
  }, { sessionId: started.sessionId, executionId: started.executionId });
  expect(evidence.events.filter((event) => event.event_type === 'skill_loaded')).toHaveLength(3);
  const recalled = evidence.events.find((event) => event.event_type === 'memory_recalled');
  expect(JSON.stringify(recalled?.data)).toContain('称呼我为张工');
  expect(JSON.stringify(recalled?.data)).not.toContain('购物售后助手只展示物流摘要');
  const operations = evidence.runtime?.operations || [];
  expect(operations.map((item) => item.operation_name)).toEqual([
    'workspace.memory.read',
    'knowledge.search',
    'workspace.memory.apply-set',
    'workspace.memory.check',
    'workspace.memory.apply-set',
    'workspace.memory.check',
  ]);
  const checks = operations.filter((item) => item.operation_name === 'workspace.memory.check');
  expect(checks[0]?.result?.data).toMatchObject({
    profile: 'diagnosis-red',
    exit_code: 1,
    passed: true,
  });
  expect(checks[1]?.result?.data).toMatchObject({
    profile: 'diagnosis-green-clean',
    exit_code: 0,
    passed: true,
  });
  await page.goto(`/workspace/chat/${started.sessionId}`);
  await expect(page.getByRole('main').getByText(/S4-DIAGNOSIS-SUCCESS/)).toBeVisible({
    timeout: 30_000,
  });
  expect(failures).toEqual([]);
});

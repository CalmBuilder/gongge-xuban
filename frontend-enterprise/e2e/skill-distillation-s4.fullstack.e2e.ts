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

async function loginAsMember(page: Page) {
  /** 通过真实认证 API 登录并固定成员自己的数字员工。 */

  await page.goto('/enterprise/dashboard');
  const status = await page.evaluate(async () => {
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
      localStorage.setItem('gongge_enterprise_agent_scope', 'agent_e2e_member_employee');
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
  await expect(page.getByLabel('动态任务控制')).toContainText('已完成');
  expect(failures).toEqual([]);
});

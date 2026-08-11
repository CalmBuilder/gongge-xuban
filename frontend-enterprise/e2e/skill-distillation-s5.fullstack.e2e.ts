/**
 * @Time       : 2026/08/13
 * @Author     : zhanglp8181
 * @File       : skill-distillation-s5.fullstack.e2e.ts
 * @CallChain  : Chromium → Chat/DynamicTask → Skill proposal → Attention → publish/bind → Chat consume
 * @Description: 验证分身提出 Skill 后须由所有者审核，批准才可绑定消费，拒绝不进入目录。
 */

import { expect, test, type Page } from '@playwright/test';

const AGENT_ID = 'agent_e2e_member_employee';
const SKILL_NAME = 's5-refund-evidence-review';

test.describe.configure({ timeout: 90_000 });

async function loginAsMember(page: Page) {
  /** 通过真实认证 API 登录 Skill 所有者，并固定其私有数字员工。 */

  await page.goto('/enterprise/dashboard');
  const status = await page.evaluate(async (agentId) => {
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
      localStorage.setItem('gongge_enterprise_agent_scope', agentId);
    }
    return response.status;
  }, AGENT_ID);
  expect(status).toBe(200);
}

async function startProposal(page: Page, marker: string): Promise<string> {
  /** 从真实聊天入口发起动态任务，并从持久事件读取其 Execution 身份。 */

  return page.evaluate(async ({ agentId, turnMarker }) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const headers = { Authorization: `Bearer ${auth.token}`, 'Content-Type': 'application/json' };
    const sessionResponse = await fetch('/api/chat/sessions', {
      method: 'POST',
      headers,
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        agent_id: agentId,
        title: `S5 Skill 提案 ${turnMarker}`,
        origin: 'owned',
      }),
    });
    const session = await sessionResponse.json() as { id?: string; detail?: string };
    if (!sessionResponse.ok || !session.id) throw new Error(session.detail || 'session failed');
    const streamResponse = await fetch('/api/chat/stream', {
      method: 'POST',
      headers,
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        session_id: session.id,
        agent_id: agentId,
        client_turn_id: `turn_s5_${turnMarker}`,
        message: `S5创建Skill：${turnMarker}，总结退款证据复核方法并提交我审核`,
        channel: 'web',
      }),
    });
    const streamBody = await streamResponse.text();
    if (!streamResponse.ok || !streamBody.includes('event: complete')) {
      throw new Error(`proposal stream failed: ${streamResponse.status} ${streamBody}`);
    }
    const events = await fetch(
      `/api/chat/sessions/${session.id}/events?tenant_id=tenant_demo`,
      { headers },
    ).then((response) => response.json()) as Array<{
      event_type: string;
      data?: Record<string, unknown>;
    }>;
    const executionId = String(
      events.find((event) => event.event_type === 'dynamic_task_delegated')?.data?.execution_id || '',
    );
    if (!executionId) throw new Error('dynamic execution id is absent');
    return executionId;
  }, { agentId: AGENT_ID, turnMarker: marker });
}

async function executionStatus(page: Page, executionId: string): Promise<Record<string, unknown>> {
  /** 使用认证 API 读取持久 Execution 状态，不根据聊天文案猜测完成情况。 */

  return page.evaluate(async (id) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    return fetch(`/api/executions/${id}?tenant_id=tenant_demo`, {
      headers: { Authorization: `Bearer ${auth.token}` },
    }).then((response) => response.json()) as Promise<Record<string, unknown>>;
  }, executionId);
}

async function boundSkillRows(page: Page): Promise<Array<{ id: string; name: string; status: string }>> {
  /** 查询原分身真正可见的 Skill 目录，draft/reviewing 根不会被算作已绑定能力。 */

  return page.evaluate(async (agentId) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    return fetch(
      `/api/enterprise/general-skills?tenant_id=tenant_demo&agent_id=${agentId}`,
      { headers: { Authorization: `Bearer ${auth.token}` } },
    ).then((response) => response.json()) as Promise<
      Array<{ id: string; name: string; status: string }>
    >;
  }, AGENT_ID);
}

test('S5 分身提案经刷新恢复、所有者审批、发布绑定后由原分身真实消费', async ({ page }) => {
  const failures: string[] = [];
  page.on('pageerror', (error) => failures.push(error.message));
  page.on('response', (response) => {
    const path = new URL(response.url()).pathname;
    if (path.startsWith('/api/') && response.status() >= 500) failures.push(`${response.status()} ${path}`);
  });
  await loginAsMember(page);
  const executionId = await startProposal(page, '批准闭环');
  expect((await boundSkillRows(page)).filter((row) => row.name === SKILL_NAME)).toHaveLength(0);

  await page.goto('/enterprise/work-items');
  const card = page.getByRole('button', { name: /审核并发布当前分身提出的 Skill/ }).first();
  await expect(card).toBeVisible({ timeout: 30_000 });
  await page.reload();
  await page.getByRole('button', { name: /审核并发布当前分身提出的 Skill/ }).first().click();
  const review = page.getByLabel('待审核 Skill 提案');
  await expect(review).toContainText(SKILL_NAME);
  await expect(review).toContainText('user_only');
  await expect(review).toContainText('完整 diff');
  await expect(review).toContainText('S5-PROPOSAL-GUIDANCE');
  await page.getByRole('dialog').getByRole('button', { name: '批准并发布' }).click();

  await expect.poll(() => executionStatus(page, executionId), { timeout: 45_000 }).toMatchObject({
    status: 'succeeded',
  });
  const rows = (await boundSkillRows(page)).filter((row) => row.name === SKILL_NAME);
  expect(rows).toHaveLength(1);
  expect(rows[0].status).toBe('published');

  const consumed = await page.evaluate(async ({ agentId, skillId }) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const headers = { Authorization: `Bearer ${auth.token}`, 'Content-Type': 'application/json' };
    const sessionResponse = await fetch('/api/chat/sessions', {
      method: 'POST',
      headers,
      body: JSON.stringify({
        tenant_id: 'tenant_demo', agent_id: agentId, title: 'S5 Skill 消费', origin: 'owned',
      }),
    });
    const session = await sessionResponse.json() as { id: string };
    const response = await fetch('/api/chat/stream', {
      method: 'POST',
      headers,
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        session_id: session.id,
        agent_id: agentId,
        client_turn_id: 'turn_s5_consume',
        message: '使用本轮选定的指南复核 CASE-S5-001',
        channel: 'web',
        forced_general_skill_id: skillId,
      }),
    });
    const body = await response.text();
    const events = await fetch(
      `/api/chat/sessions/${session.id}/events?tenant_id=tenant_demo`,
      { headers },
    ).then((eventResponse) => eventResponse.json()) as Array<{
      event_type: string;
      data?: Record<string, unknown>;
    }>;
    return { status: response.status, body, events };
  }, { agentId: AGENT_ID, skillId: rows[0].id });
  expect(consumed.status).toBe(200);
  expect(consumed.body).toContain('S5-CONSUMED-SUCCESS');
  expect(consumed.events).toEqual(expect.arrayContaining([
    expect.objectContaining({ event_type: 'skill_loaded', data: expect.objectContaining({ skill_id: rows[0].id }) }),
  ]));
  expect(failures).toEqual([]);
});

test('S5 所有者拒绝后任务失败且候选永不进入原分身目录', async ({ page }) => {
  /** 覆盖拒绝终态，证明待审核候选不会因同名或重试泄漏为可用 Skill。 */

  await loginAsMember(page);
  const before = (await boundSkillRows(page)).filter((row) => row.name === SKILL_NAME).length;
  const executionId = await startProposal(page, '拒绝闭环');
  await page.goto('/enterprise/work-items');
  await page.getByRole('button', { name: /审核并发布当前分身提出的 Skill/ }).first().click();
  await expect(page.getByLabel('待审核 Skill 提案')).toContainText(SKILL_NAME);
  await page.getByRole('dialog').getByRole('button', { name: '拒绝提案' }).click();

  await expect.poll(() => executionStatus(page, executionId), { timeout: 45_000 }).toMatchObject({
    status: 'failed',
  });
  const after = (await boundSkillRows(page)).filter((row) => row.name === SKILL_NAME).length;
  expect(after).toBe(before);
});

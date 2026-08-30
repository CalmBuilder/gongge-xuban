/**
 * @Time       : 2026/08/31
 * @Author     : zhanglp8181
 * @File       : dynamic-task-capability-matrix.fullstack.e2e.ts
 * @CallChain  : Chromium → Chat Composer → AgentLoop/DynamicTask → Attention/Operation/Result
 * @Description: 验证普通动态多轮闭环、destructive 默认拒绝及隔离 provider 正向确认。
 */

import { expect, test, type Page } from '@playwright/test';

const PROFILE = process.env.FULLSTACK_E2E_PROFILE ?? 'base-open';
const TENANT_ID = 'tenant_demo';
const AGENT_ID = 'agent_e2e_employee';

type SessionFacts = {
  events: Array<{ event_type: string; data?: Record<string, unknown> }>;
  messages: Array<{ id: string; role: string; content: string }>;
};

async function login(page: Page, username = 'admin', password = 'admin'): Promise<void> {
  /** 通过真实认证 API 建立浏览器会话，并让后续操作仍经页面 UI 完成。 */

  await page.goto('/enterprise/dashboard');
  const status = await page.evaluate(async ({ username, password, tenantId, agentId }) => {
    const response = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tenant_id: tenantId, username, password }),
    });
    const body = await response.json();
    if (response.ok) {
      localStorage.setItem('gongge_auth', JSON.stringify(body));
      localStorage.setItem('gongge_onboarding_guide_seen', '1');
      localStorage.setItem('gongge_quick_start_guide_seen', '1');
      localStorage.setItem('gongge_enterprise_agent_scope', agentId);
    }
    return response.status;
  }, { username, password, tenantId: TENANT_ID, agentId: AGENT_ID });
  expect(status).toBe(200);
}

async function createSession(page: Page, title: string): Promise<string> {
  /** 只用认证后的会话创建 API 做测试数据准备，消息发送仍使用真实 Composer。 */

  const sessionId = await page.evaluate(async ({ title, tenantId, agentId }) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch('/api/chat/sessions', {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${auth.token}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        tenant_id: tenantId,
        agent_id: agentId,
        title,
        origin: 'owned',
      }),
    });
    const body = await response.json() as { id?: string; detail?: string };
    if (!response.ok || !body.id) throw new Error(body.detail || 'session creation failed');
    return body.id;
  }, { title, tenantId: TENANT_ID, agentId: AGENT_ID });
  expect(sessionId).toMatch(/^session_/);
  return sessionId;
}

async function readFacts(page: Page, sessionId: string): Promise<SessionFacts> {
  /** 从受保护的会话事件和消息账本读取终态，不以流式 DOM 片段冒充完成。 */

  return page.evaluate(async ({ sessionId, tenantId }) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const headers = { Authorization: `Bearer ${auth.token}` };
    const [eventsResponse, messagesResponse] = await Promise.all([
      fetch(`/api/chat/sessions/${sessionId}/events?tenant_id=${tenantId}`, { headers }),
      fetch(`/api/chat/sessions/${sessionId}/messages?tenant_id=${tenantId}`, { headers }),
    ]);
    return {
      events: await eventsResponse.json() as SessionFacts['events'],
      messages: await messagesResponse.json() as SessionFacts['messages'],
    };
  }, { sessionId, tenantId: TENANT_ID });
}

async function sendThroughComposer(page: Page, message: string): Promise<void> {
  /** 通过真实聊天输入框发送一轮，并等待发送队列完成入站。 */

  const composer = page.getByPlaceholder('输入消息，按 Enter 发送...');
  await expect(composer).toBeVisible();
  await composer.fill(message);
  await page.getByRole('button', { name: '发送', exact: true }).click();
  await expect(composer).toHaveValue('', { timeout: 10_000 });
}

async function executionIdFor(page: Page, sessionId: string): Promise<string> {
  /** 读取本会话最新动态委派事件的 Execution 标识。 */

  const facts = await readFacts(page, sessionId);
  return String(
    [...facts.events].reverse().find((event) => event.event_type === 'dynamic_task_delegated')
      ?.data?.execution_id || '',
  );
}

async function readExecution(page: Page, executionId: string): Promise<Record<string, unknown>> {
  /** 读取服务端 Execution 状态作为多轮闭环的权威终态。 */

  return page.evaluate(async ({ executionId, tenantId }) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch(`/api/executions/${executionId}?tenant_id=${tenantId}`, {
      headers: { Authorization: `Bearer ${auth.token}` },
    });
    return response.json() as Promise<Record<string, unknown>>;
  }, { executionId, tenantId: TENANT_ID });
}

async function activeAttention(page: Page, executionId: string): Promise<Record<string, unknown> | null> {
  /** 从统一待处理接口寻找指定 Execution 的活动 Attention。 */

  return page.evaluate(async ({ executionId, tenantId }) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch(
      `/api/attention-items?tenant_id=${tenantId}&view=active&page=1&page_size=100`,
      { headers: { Authorization: `Bearer ${auth.token}` } },
    );
    const body = await response.json() as { items?: Array<Record<string, unknown>> };
    return body.items?.find((item) => item.execution_id === executionId) || null;
  }, { executionId, tenantId: TENANT_ID });
}

test.describe.configure({ mode: 'serial', timeout: 120_000 });

test('普通 DynamicTaskAgent 通过真实 Composer 多轮澄清并闭环完成', async ({ page }) => {
  /** 验证无 SOP 时普通动态链路默认开放，首轮等待、次轮补充和终态均可恢复。 */

  test.skip(PROFILE !== 'base-open', '普通动态多轮正向只在 base-open 隔离 profile 运行');
  await login(page);
  const sessionId = await createSession(page, '普通动态多轮闭环');
  await page.goto(`/workspace/chat/${sessionId}`);
  await expect(page.getByPlaceholder('输入消息，按 Enter 发送...')).toBeVisible();

  await sendThroughComposer(page, '请做一次合同巡检，并先确认本次需要检查的范围。');
  await expect.poll(() => executionIdFor(page, sessionId), {
    timeout: 45_000,
    intervals: [250, 500, 1_000, 2_000],
  }).not.toBe('');
  const executionId = await executionIdFor(page, sessionId);
  const execution = await readExecution(page, executionId);
  expect(execution.status).toBe('waiting');
  expect((await readFacts(page, sessionId)).events.map((event) => event.event_type)).toContain(
    'dynamic_task_delegated',
  );

  await page.goto('/enterprise/work-items');
  const clarification = page.getByRole('button', { name: /确认合同范围/ }).first();
  await expect(clarification).toBeVisible({ timeout: 30_000 });
  await clarification.click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toContainText('请选择本次需要巡检的合同范围');
  await dialog.getByRole('button', { name: '未来30天到期' }).click();
  await dialog.getByRole('button', { name: '补充并继续' }).click();

  await expect.poll(() => readExecution(page, executionId), {
    timeout: 45_000,
    intervals: [500, 1_000, 2_000],
  }).toMatchObject({ status: 'succeeded' });
  await expect.poll(async () => {
    const facts = await readFacts(page, sessionId);
    return facts.messages.filter((item) => item.role === 'assistant').length;
  }, { timeout: 30_000, intervals: [500, 1_000, 2_000] }).toBeGreaterThanOrEqual(2);
  const finalFacts = await readFacts(page, sessionId);
  expect(finalFacts.events.map((event) => event.event_type)).toEqual(expect.arrayContaining([
    'dynamic_task_delegated',
    'execution_succeeded',
  ]));
  expect(finalFacts.messages.at(-1)?.content || '').toMatch(/巡检|合同|结果/);
});

test('destructive 默认关闭时不创建 Operation，隔离灰度时必须逐次确认后完成', async ({ page }) => {
  /** 验证 destructive 的反向零外呼和 disposable provider 正向闭环。 */

  // 正向场景固定发起人与审批人分离：admin 发起，publication-admin 审批。
  // 这既验证了逐次确认，也避免用发起人的管理员身份误测候选人过滤。
  const initiator = 'admin';
  const initiatorPassword = 'admin';
  await login(page, initiator, initiatorPassword);
  const sessionId = await createSession(page, `destructive ${PROFILE}`);
  await page.goto(`/workspace/chat/${sessionId}`);
  await sendThroughComposer(page, 'DESTRUCTIVE-GRAY：请删除隔离 fixture object-1，并返回可核验回执。');

  if (PROFILE !== 'destructive-gray') {
    await expect.poll(async () => {
      const facts = await readFacts(page, sessionId);
      return facts.events.some((event) => event.event_type === 'dynamic_task_delegation_failed');
    }, { timeout: 45_000, intervals: [250, 500, 1_000, 2_000] }).toBe(true);
    const facts = await readFacts(page, sessionId);
    expect(facts.events.some((event) => event.event_type === 'dynamic_task_delegated')).toBe(false);
    expect(facts.events.some((event) => event.event_type === 'tool_call_started')).toBe(false);
    expect(await executionIdFor(page, sessionId)).toBe('');
    return;
  }

  await expect.poll(() => executionIdFor(page, sessionId), {
    timeout: 45_000,
    intervals: [250, 500, 1_000, 2_000],
  }).not.toBe('');
  const executionId = await executionIdFor(page, sessionId);

  await expect.poll(() => activeAttention(page, executionId), {
    timeout: 45_000,
    intervals: [250, 500, 1_000, 2_000],
  }).not.toBeNull();
  const attention = await activeAttention(page, executionId);
  expect(attention?.kind).toBe('tool_approval');
  expect(attention?.title).toBe('批准 destructive 隔离 provider 单次操作');

  await login(page, 'publication-admin', 'publication-admin');
  await page.goto('/enterprise/work-items');
  const approval = page.getByRole('button', { name: /批准 destructive 隔离 provider 单次操作/ });
  await expect(approval).toBeVisible({ timeout: 30_000 });
  await approval.click();
  const approvalDialog = page.getByRole('dialog');
  await expect(approvalDialog).toContainText('disposable://fixture/object-1');
  await expect(approvalDialog).toContainText('destructive');
  await approvalDialog.getByRole('button', { name: '仅批准本次 destructive 操作' }).click();

  await login(page, initiator, initiatorPassword);
  await expect.poll(() => readExecution(page, executionId), {
    timeout: 60_000,
    intervals: [500, 1_000, 2_000],
  }).toMatchObject({ status: 'succeeded' });
  await page.goto(`/workspace/chat/${sessionId}`);
  await expect(page.getByRole('paragraph').filter({ hasText: 'DESTRUCTIVE-GRAY-SUCCESS' }))
    .toBeVisible({ timeout: 30_000 });
  const finalFacts = await readFacts(page, sessionId);
  expect(finalFacts.events.map((event) => event.event_type)).toEqual(expect.arrayContaining([
    'dynamic_task_delegated',
    'execution_succeeded',
  ]));
});

test('external_write 独立灰度时可逐次审批发送，未灰度时保持零派发', async ({ page }) => {
  /** 验证 external_write 与 destructive 分开：默认不暴露连接写能力，灰度后经真实连接授权和审批派发。 */

  await login(page, 'admin', 'admin');
  const sessionId = 'session_e2e_dynamic_external_write';
  await page.goto(`/workspace/chat/${sessionId}`);
  await expect(page.getByPlaceholder('输入消息，按 Enter 发送...')).toBeVisible();
  await sendThroughComposer(
    page,
    'EXTERNAL-WRITE-GRAY：请向当前企业微信会话发送一条回归消息，并返回可核验回执。',
  );

  if (PROFILE !== 'high-risk-gray') {
    await expect.poll(async () => {
      const facts = await readFacts(page, sessionId);
      return facts.events.some((event) => event.event_type === 'dynamic_task_delegation_failed');
    }, { timeout: 45_000, intervals: [250, 500, 1_000, 2_000] }).toBe(true);
    const facts = await readFacts(page, sessionId);
    expect(facts.events.some((event) => event.event_type === 'dynamic_task_delegated')).toBe(false);
    expect(facts.events.some((event) => event.event_type === 'tool_call_started')).toBe(false);
    expect(await executionIdFor(page, sessionId)).toBe('');
    return;
  }

  await expect.poll(() => executionIdFor(page, sessionId), {
    timeout: 45_000,
    intervals: [250, 500, 1_000, 2_000],
  }).not.toBe('');
  const executionId = await executionIdFor(page, sessionId);
  await expect.poll(() => activeAttention(page, executionId), {
    timeout: 45_000,
    intervals: [250, 500, 1_000, 2_000],
  }).not.toBeNull();
  const attention = await activeAttention(page, executionId);
  expect(attention?.kind).toBe('tool_approval');
  expect(attention?.title).toBe('批准企业微信消息发送');

  await login(page, 'publication-admin', 'publication-admin');
  await page.goto('/enterprise/work-items');
  const approval = page.getByRole('button', { name: /批准企业微信消息发送/ });
  await expect(approval).toBeVisible({ timeout: 30_000 });
  await approval.click();
  const approvalDialog = page.getByRole('dialog');
  await expect(approvalDialog).toContainText('EXTERNAL-WRITE-GRAY');
  await expect(approvalDialog).toContainText('当前企业微信会话');
  await approvalDialog.getByRole('button', { name: '仅批准本次发送' }).click();

  await login(page, 'admin', 'admin');
  await expect.poll(() => readExecution(page, executionId), {
    timeout: 60_000,
    intervals: [500, 1_000, 2_000],
  }).toMatchObject({ status: 'succeeded' });
  const completed = await readExecution(page, executionId);
  const operations = Array.isArray(completed.operations) ? completed.operations : [];
  expect(operations).toEqual(expect.arrayContaining([
    expect.objectContaining({
      operation_name: expect.stringMatching(/^wecom\.message_send@/),
      effect_kind: 'external_write',
      status: 'succeeded',
    }),
  ]));
  await page.goto(`/workspace/chat/${sessionId}`);
  await expect(page.getByRole('paragraph').filter({ hasText: 'EXTERNAL-WRITE-GRAY-SUCCESS' }))
    .toBeVisible({ timeout: 30_000 });
  const finalFacts = await readFacts(page, sessionId);
  expect(finalFacts.events.map((event) => event.event_type)).toEqual(expect.arrayContaining([
    'dynamic_task_delegated',
    'execution_succeeded',
  ]));
});

test('模型或 Provider 错误只结束当前轮，不改变普通动态开放状态', async ({ page }) => {
  /** 验证模型配额/Provider 失败按模型错误协议返回，不伪装成 DynamicTask 产品未开放。 */

  test.skip(PROFILE !== 'model-error', '模型错误反向只在 model-error 隔离 profile 运行');
  await login(page);
  const snapshot = await page.evaluate(async () => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch('/api/dynamic-task-operations/snapshot?tenant_id=tenant_demo', {
      headers: { Authorization: `Bearer ${auth.token}` },
    });
    return response.json() as Promise<Record<string, unknown>>;
  });
  expect(snapshot.base_execution_available).toBe(true);

  const sessionId = await createSession(page, '模型错误不改变动态开放');
  await page.goto(`/workspace/chat/${sessionId}`);
  await sendThroughComposer(page, '请生成一份需要确认范围的合同巡检结果。');
  await expect.poll(() => readFacts(page, sessionId), {
    timeout: 45_000,
    intervals: [250, 500, 1_000, 2_000],
  }).toMatchObject({
    events: expect.arrayContaining([
      expect.objectContaining({
        event_type: 'non_sop_capability_primary_decided',
        data: expect.objectContaining({
          failure_code: 'dynamic_primary_failed',
          execution_created: false,
        }),
      }),
    ]),
    messages: expect.arrayContaining([
      expect.objectContaining({
        role: 'assistant',
        content: expect.stringContaining('MODEL_PROVIDER_QUOTA_EXCEEDED'),
      }),
    ]),
  });
  const facts = await readFacts(page, sessionId);
  expect(facts.events.some((event) => event.event_type === 'dynamic_task_rollout_denied')).toBe(false);
  expect(facts.events.some((event) => event.event_type === 'dynamic_task_delegated')).toBe(false);
  expect(facts.messages.at(-1)?.content || '').toContain('模型调用失败');
  expect(facts.messages.at(-1)?.content || '').toContain('MODEL_PROVIDER_QUOTA_EXCEEDED');
  expect(facts.messages.at(-1)?.content || '').not.toContain('动态能力未开放');
});

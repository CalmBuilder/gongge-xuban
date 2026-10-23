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
const OVERALL_AGENT_ID = 'agent_tenant_demo_overall';

test.use({ timezoneId: 'Asia/Shanghai' });

type SessionFacts = {
  events: Array<{
    event_type: string;
    data?: Record<string, unknown>;
    persistent_action_id?: string;
    receipt_source?: string;
    success?: boolean;
  }>;
  messages: Array<{
    id: string;
    role: string;
    content: string;
    metadata?: { time_context?: Record<string, string> };
  }>;
};

type Sd1RestoreSnapshot = {
  tool_id: string;
  skill_id: string;
  operation_id: string | null;
  operation_status: string | null;
  instance_status: string | null;
  execution_status: string | null;
  dispatch_count: number;
  phase?: string;
  action?: string | null;
  is_step_completed?: boolean;
  runtime_error_code?: string | null;
};

type Sd1ActionSnapshot = {
  dispatch_count: number;
  dispatched_products: string[];
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

async function readProviderDispatchCount(page: Page, provider: string): Promise<number> {
  /** 从隔离 provider 适配器边界读取真实 send_text 调用次数。 */

  return page.evaluate(async (providerName) => {
    const response = await fetch('/__e2e/provider-dispatches');
    if (!response.ok) throw new Error(`provider dispatch snapshot failed: ${response.status}`);
    const body = await response.json() as Record<string, number>;
    return Number(body[providerName] || 0);
  }, provider);
}

async function readSd1ActionSnapshot(page: Page): Promise<Sd1ActionSnapshot> {
  /** 从隔离 HTTP 工具适配器边界读取同轮多个动作的真实派发计数。 */

  const response = await page.request.get('/__e2e/sd1/action/snapshot');
  expect(response.status()).toBe(200);
  return await response.json() as Sd1ActionSnapshot;
}

async function readSd1PendingActionSnapshot(page: Page): Promise<Sd1ActionSnapshot> {
  /** 从隔离 HTTP 工具适配器边界读取两个待办任务的实际派发顺序和次数。 */

  const response = await page.request.get('/__e2e/sd1/pending/snapshot');
  expect(response.status()).toBe(200);
  return await response.json() as Sd1ActionSnapshot;
}

async function setToolAllowedSkills(
  page: Page,
  toolId: string,
  allowedSkills: string[],
): Promise<void> {
  /** 通过真实管理 API 撤销或恢复正式 SOP 工具的 Skill 白名单。 */

  const status = await page.evaluate(async ({ toolId, allowedSkills, tenantId, agentId }) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const headers = { Authorization: `Bearer ${auth.token}` };
    const detailResponse = await fetch(
      `/api/enterprise/tools/${toolId}?tenant_id=${tenantId}&agent_id=${agentId}`,
      { headers },
    );
    if (!detailResponse.ok) return detailResponse.status;
    const current = await detailResponse.json() as {
      tenant_id: string;
      name: string;
      display_name: string | null;
      description: string | null;
      bucket: string;
      tool_type: 'http' | 'mcp' | 'managed_workspace';
      method: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
      url: string;
      input_schema: Record<string, unknown>;
      output_schema: Record<string, unknown>;
      required_permission_code: string | null;
      permission_authorization_mode: string;
      reliability_contract: Record<string, unknown> | null;
      enabled: boolean;
    };
    const updateResponse = await fetch(
      `/api/enterprise/tools/${toolId}?agent_id=${agentId}`,
      {
        method: 'PUT',
        headers: { ...headers, 'Content-Type': 'application/json' },
        body: JSON.stringify({
          tenant_id: current.tenant_id,
          name: current.name,
          display_name: current.display_name,
          description: current.description,
          bucket: current.bucket,
          tool_type: current.tool_type,
          method: current.method,
          url: current.url,
          headers: {},
          auth: {},
          mcp_config: {},
          input_schema: current.input_schema,
          output_schema: current.output_schema,
          allowed_skills: allowedSkills,
          required_permission_code: current.required_permission_code,
          permission_authorization_mode: current.permission_authorization_mode,
          reliability_contract: current.reliability_contract,
          enabled: current.enabled,
          preserve_credential_fields: [],
          clear_credential_fields: [],
        }),
      },
    );
    return updateResponse.status;
  }, { toolId, allowedSkills, tenantId: TENANT_ID, agentId: OVERALL_AGENT_ID });
  expect(status).toBe(200);
}

test.describe.configure({ mode: 'serial', timeout: 120_000 });

test('正式 SOP 浏览器恢复在撤权时不推进，恢复绑定后复用原 Operation', async ({ page }) => {
  /** 通过真实 AgentLoop 派发、管理 API 撤权和正式 prepare_step 核对恢复边界。 */

  test.skip(PROFILE !== 'base-open', '正式 SOP 恢复授权场景只在 base-open 隔离 profile 运行');
  const prepareResponse = await page.request.get('/__e2e/sd1/prepare');
  expect(prepareResponse.status()).toBe(200);
  const prepared = await prepareResponse.json() as Sd1RestoreSnapshot;
  expect(prepared).toMatchObject({
    phase: 'prepared',
    operation_status: 'succeeded',
    instance_status: 'running',
    execution_status: 'running',
    dispatch_count: 1,
  });
  expect(prepared.operation_id).not.toBeNull();

  await login(page);
  // 空白 allowlist 在本项目表示不限制 Skill；改为无关 Skill 才是真正撤销当前 SOP。
  await setToolAllowedSkills(page, prepared.tool_id, ['sd1_browser_unrelated_skill']);
  const deniedResponse = await page.request.get('/__e2e/sd1/restore');
  expect(deniedResponse.status()).toBe(200);
  const denied = await deniedResponse.json() as Sd1RestoreSnapshot;
  expect(denied).toMatchObject({
    phase: 'restored',
    action: 'reply',
    runtime_error_code: 'NOT_ALLOWED',
    is_step_completed: false,
    operation_status: 'succeeded',
    instance_status: 'running',
    execution_status: 'running',
    dispatch_count: 1,
    operation_id: prepared.operation_id,
  });

  await setToolAllowedSkills(page, prepared.tool_id, [prepared.skill_id]);
  const restoredResponse = await page.request.get('/__e2e/sd1/restore');
  expect(restoredResponse.status()).toBe(200);
  const restored = await restoredResponse.json() as Sd1RestoreSnapshot;
  expect(restored).toMatchObject({
    phase: 'restored',
    is_step_completed: true,
    operation_status: 'succeeded',
    instance_status: 'succeeded',
    execution_status: 'succeeded',
    dispatch_count: 1,
    operation_id: prepared.operation_id,
  });
});

test('SD-1 真实 Composer 同轮多个业务动作保持独立身份并分别派发', async ({ page }) => {
  /** 沿真实页面、AgentLoop、Step Agent 和 HTTP ToolExecutor 核对双动作回执与派发次数。 */

  test.skip(PROFILE !== 'base-open', 'SD-1 同轮动作身份只在 base-open 隔离 profile 运行');
  const resetResponse = await page.request.get('/__e2e/sd1/action/reset');
  expect(resetResponse.status()).toBe(200);

  await login(page);
  const sessionId = await createSession(page, 'SD-1 同轮多动作身份');
  await page.goto(`/workspace/chat/${sessionId}`);
  let replayBody: Record<string, unknown> | null = null;
  const captureComposerRequest = (request: { method(): string; url(): string; postDataJSON(): unknown }) => {
    if (request.method() !== 'POST' || !request.url().includes('/api/chat/stream')) return;
    replayBody = request.postDataJSON() as Record<string, unknown>;
  };
  page.on('request', captureComposerRequest);
  await sendThroughComposer(page, 'SD1-ACTION-IDENTITY：请分别查询 A1 和 A3，并返回两次工具结果。');
  page.off('request', captureComposerRequest);

  await expect(page.getByRole('paragraph').filter({ hasText: 'SD1-ACTION-IDENTITY-SUCCESS' }))
    .toBeVisible({ timeout: 45_000 });
  const facts = await readFacts(page, sessionId);
  const started = facts.events.filter((event) => event.event_type === 'tool_call_started');
  const finished = facts.events.filter((event) => event.event_type === 'tool_call_finished');
  const authoritativeFinished = finished.filter((event) => event.receipt_source !== 'compatibility_event');
  expect(started).toHaveLength(2);
  expect(authoritativeFinished).toHaveLength(2);
  expect(started.map((event) => event.data?.persistent_action_id)).toEqual([
    expect.stringContaining(':step:query_a1:action:0:'),
    expect.stringContaining(':step:query_a3:action:0:'),
  ]);
  expect(new Set(started.map((event) => event.data?.persistent_action_id)).size).toBe(2);
  expect(started.map((event) => (event.data?.arguments as { product_name?: string } | undefined)
    ?.product_name)).toEqual(['A1', 'A3']);
  expect(authoritativeFinished.map((event) => event.success)).toEqual([true, true]);

  const snapshot = await readSd1ActionSnapshot(page);
  expect(snapshot).toEqual({
    dispatch_count: 2,
    dispatched_products: ['A1', 'A3'],
  });
  expect(facts.messages.at(-1)?.content || '').toContain('A1');
  expect(facts.messages.at(-1)?.content || '').toContain('A3');
  expect(facts.events.some((event) => (
    event.event_type === 'skill_completed' && event.data?.step_id === 'query_a3'
  ))).toBe(true);

  expect(replayBody).not.toBeNull();
  expect(replayBody?.client_turn_id).toEqual(expect.any(String));
  const authToken = await page.evaluate(() => (
    (JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string }).token || ''
  ));
  const replayResponse = await page.request.post('/api/chat/stream', {
    headers: {
      Authorization: `Bearer ${authToken}`,
      'Content-Type': 'application/json',
    },
    data: replayBody,
  });
  expect(replayResponse.status()).toBe(200);
  await replayResponse.text();
  const replayFacts = await readFacts(page, sessionId);
  const replayStarted = replayFacts.events.filter((event) => event.event_type === 'tool_call_started');
  expect(replayStarted).toHaveLength(2);
  const replaySnapshot = await readSd1ActionSnapshot(page);
  expect(replaySnapshot).toEqual(snapshot);
  console.log('SD1_BROWSER_ACTION_RESULT', JSON.stringify({
    session_id: sessionId,
    action_ids: started.map((event) => event.data?.persistent_action_id),
    dispatch_count: snapshot.dispatch_count,
    dispatched_products: snapshot.dispatched_products,
    receipt_statuses: authoritativeFinished.map((event) => event.success),
    replay_dispatch_count: replaySnapshot.dispatch_count,
  }));
});

test('SD-1 真实 Composer 同轮两个独立待办任务各派发一次且重试不重复', async ({ page }) => {
  /** 沿真实页面待办续接链核对同 Skill/Step 的双任务身份、完成回执和重放计数。 */

  test.skip(PROFILE !== 'base-open', 'SD-1 待办任务身份只在 base-open 隔离 profile 运行');
  const resetResponse = await page.request.get('/__e2e/sd1/pending/reset');
  expect(resetResponse.status()).toBe(200);

  await login(page);
  const sessionId = await createSession(page, 'SD-1 双待办任务身份');
  await page.goto(`/workspace/chat/${sessionId}`);
  let replayBody: Record<string, unknown> | null = null;
  const captureComposerRequest = (request: { method(): string; url(): string; postDataJSON(): unknown }) => {
    if (request.method() !== 'POST' || !request.url().includes('/api/chat/stream')) return;
    replayBody = request.postDataJSON() as Record<string, unknown>;
  };
  page.on('request', captureComposerRequest);
  await sendThroughComposer(page, 'SD1-PENDING-TASK-IDENTITY：请分别完成任务 A1 和 A3。');
  page.off('request', captureComposerRequest);

  await expect(page.getByRole('paragraph').filter({ hasText: 'SD1-PENDING-TASK-IDENTITY-SUCCESS' }))
    .toBeVisible({ timeout: 45_000 });
  const facts = await readFacts(page, sessionId);
  const started = facts.events.filter((event) => event.event_type === 'tool_call_started');
  const finished = facts.events.filter((event) => event.event_type === 'tool_call_finished');
  const authoritativeFinished = finished.filter((event) => event.receipt_source !== 'compatibility_event');
  expect(started).toHaveLength(2);
  expect(authoritativeFinished).toHaveLength(2);
  expect(started.map((event) => event.data?.persistent_action_id)).toEqual([
    expect.stringContaining(':task:task_A1:skill:sd1_browser_pending_sop:step:query:'),
    expect.stringContaining(':task:task_A3:skill:sd1_browser_pending_sop:step:query:'),
  ]);
  expect(new Set(started.map((event) => event.data?.persistent_action_id)).size).toBe(2);
  expect(started.map((event) => (event.data?.arguments as { product_name?: string } | undefined)
    ?.product_name)).toEqual(['A1', 'A3']);
  expect(authoritativeFinished.map((event) => event.success)).toEqual([true, true]);

  const snapshot = await readSd1PendingActionSnapshot(page);
  expect(snapshot).toEqual({
    dispatch_count: 2,
    dispatched_products: ['A1', 'A3'],
  });
  expect(replayBody).not.toBeNull();

  const authToken = await page.evaluate(() => (
    (JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string }).token || ''
  ));
  const replayResponse = await page.request.post('/api/chat/stream', {
    headers: {
      Authorization: `Bearer ${authToken}`,
      'Content-Type': 'application/json',
    },
    data: replayBody,
  });
  expect(replayResponse.status()).toBe(200);
  await replayResponse.text();
  const replayFacts = await readFacts(page, sessionId);
  expect(replayFacts.events.filter((event) => event.event_type === 'tool_call_started'))
    .toHaveLength(2);
  const replaySnapshot = await readSd1PendingActionSnapshot(page);
  expect(replaySnapshot).toEqual(snapshot);
  console.log('SD1_BROWSER_PENDING_TASK_RESULT', JSON.stringify({
    session_id: sessionId,
    action_ids: started.map((event) => event.data?.persistent_action_id),
    dispatch_count: snapshot.dispatch_count,
    dispatched_products: snapshot.dispatched_products,
    receipt_statuses: authoritativeFinished.map((event) => event.success),
    replay_dispatch_count: replaySnapshot.dispatch_count,
  }));
});

test('普通 DynamicTaskAgent 通过真实 Composer 多轮澄清并闭环完成', async ({ page }) => {
  /** 验证无 SOP 时普通动态链路默认开放，首轮等待、次轮补充和终态均可恢复。 */

  test.skip(PROFILE !== 'base-open', '普通动态多轮正向只在 base-open 隔离 profile 运行');
  await login(page);
  const sessionId = await createSession(page, '普通动态多轮闭环');
  await page.goto(`/workspace/chat/${sessionId}`);
  await expect(page.getByPlaceholder('输入消息，按 Enter 发送...')).toBeVisible();

  await sendThroughComposer(page, '请做一次合同巡检，并先确认本次需要检查的范围。');
  await expect.poll(async () => {
    const facts = await readFacts(page, sessionId);
    return facts.messages.find((item) => item.role === 'user')?.id || '';
  }, { timeout: 15_000 }).not.toBe('');
  const initialFacts = await readFacts(page, sessionId);
  const initialUserMessage = initialFacts.messages.find((item) => item.role === 'user');
  expect(initialUserMessage?.metadata?.time_context).toMatchObject({
    source_input_id: initialUserMessage?.id,
    timezone: 'Asia/Shanghai',
  });
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
  const providerDispatchesBefore = await readProviderDispatchCount(page, 'wecom_message_send');
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
  const writeOperations = operations.filter((operation) => (
    String((operation as Record<string, unknown>).operation_name || '').startsWith('wecom.message_send@')
  ));
  expect(writeOperations).toHaveLength(1);
  expect(writeOperations[0]).toMatchObject({
    operation_name: expect.stringMatching(/^wecom\.message_send@/),
    effect_kind: 'external_write',
    status: 'succeeded',
  });
  const traceOperations = await page.evaluate(async ({ sessionId, executionId }) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch(`/api/enterprise/traces/${sessionId}?tenant_id=tenant_demo`, {
      headers: { Authorization: `Bearer ${auth.token}` },
    });
    const trace = await response.json() as {
      sop_runtime?: Array<{ instance_id: string; operations?: Array<Record<string, unknown>> }>;
    };
    return trace.sop_runtime?.find((item) => item.instance_id === executionId)?.operations || [];
  }, { sessionId, executionId });
  expect(traceOperations).toEqual(expect.arrayContaining([
    expect.objectContaining({
      operation_name: expect.stringMatching(/^wecom\.message_send@/),
      status: 'succeeded',
      result: expect.objectContaining({
        data: expect.objectContaining({
          delivery_status: 'sent',
          message_id: expect.stringMatching(/^e2e-wecom-message-/),
        }),
      }),
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
  const providerDispatchesAfter = await readProviderDispatchCount(page, 'wecom_message_send');
  expect(providerDispatchesAfter - providerDispatchesBefore).toBe(1);
  console.log('EXTERNAL_WRITE_OPERATION_RESULT', JSON.stringify({
    execution_id: executionId,
    dispatch_count: providerDispatchesAfter - providerDispatchesBefore,
    operation_count: writeOperations.length,
    operation_status: writeOperations[0]?.status,
    receipt_source: 'operation.result.data.provider',
    message_id: (traceOperations.find((operation) => (
      String(operation.operation_name || '').startsWith('wecom.message_send@')
    ))?.result as { data?: { message_id?: string } } | undefined)?.data?.message_id,
  }));
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

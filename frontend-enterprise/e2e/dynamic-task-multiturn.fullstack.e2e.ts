/**
 * @Time       : 2026/08/31
 * @Author     : zhanglp8181
 * @File       : dynamic-task-multiturn.fullstack.e2e.ts
 * @CallChain  : Playwright Chromium → Chat session → DynamicTask turn/resume/continue → Execution
 * @Description: 用真实浏览器验证 DynamicTask 等待态恢复、终态续接、附件继承和 20+ 轮会话链。
 */

import { expect, test, type Page } from '@playwright/test';
import { basename, resolve } from 'node:path';

test.describe.configure({ timeout: 600_000 });

const TENANT_ID = 'tenant_demo';
const AGENT_ID = 'agent_e2e_employee';
const LOG_FILE = resolve('../docs/logs-from-acct-corp-in-acct-corp-5994bf6d7b-k8fng.txt');
const S3_MARKER = 'No content length specified for stream data';

type BrowserStreamRequest = {
  body: Record<string, unknown>;
};

type SessionEvent = {
  event_type?: string;
  data?: Record<string, unknown>;
  [key: string]: unknown;
};

type SessionFacts = {
  session: { status?: string; summary?: string | null };
  messages: Array<{
    id: string;
    role: string;
    content: string;
    metadata?: Record<string, unknown>;
  }>;
  events: SessionEvent[];
};

async function login(page: Page): Promise<void> {
  /** 通过真实浏览器上下文登录隔离租户，并固定到回归用数字员工。 */

  await page.goto('/');
  const status = await page.evaluate(async ({ tenantId, agentId }) => {
    const response = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tenant_id: tenantId, username: 'admin', password: 'admin' }),
    });
    const body = await response.json();
    if (response.ok) {
      localStorage.setItem('gongge_auth', JSON.stringify(body));
      localStorage.setItem('gongge_onboarding_guide_seen', '1');
      localStorage.setItem('gongge_quick_start_guide_seen', '1');
      localStorage.setItem('gongge_enterprise_agent_scope', agentId);
    }
    return response.status;
  }, { tenantId: TENANT_ID, agentId: AGENT_ID });
  expect(status).toBe(200);
}

async function createSession(page: Page, title: string): Promise<string> {
  /** 通过正式聊天 API 创建绑定回归数字员工的会话。 */

  return page.evaluate(async ({ tenantId, agentId, title: sessionTitle }) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch('/api/chat/sessions', {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${auth.token || ''}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        tenant_id: tenantId,
        agent_id: agentId,
        title: sessionTitle,
        origin: 'owned',
      }),
    });
    const body = await response.json() as { id?: string; detail?: string };
    if (!response.ok || !body.id) throw new Error(body.detail || '会话创建失败');
    return body.id;
  }, { tenantId: TENANT_ID, agentId: AGENT_ID, title });
}

async function readFacts(page: Page, sessionId: string): Promise<SessionFacts> {
  /** 读取当前会话的权威消息、事件和状态。 */

  return page.evaluate(async ({ id, tenantId }) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const headers = { Authorization: `Bearer ${auth.token || ''}` };
    const [sessionsResponse, messagesResponse, eventsResponse] = await Promise.all([
      fetch(`/api/chat/sessions?tenant_id=${tenantId}`, { headers }),
      fetch(`/api/chat/sessions/${encodeURIComponent(id)}/messages?tenant_id=${tenantId}`, { headers }),
      fetch(`/api/chat/sessions/${encodeURIComponent(id)}/events?tenant_id=${tenantId}`, { headers }),
    ]);
    if (!sessionsResponse.ok || !messagesResponse.ok || !eventsResponse.ok) {
      throw new Error(
        `会话事实读取失败: ${sessionsResponse.status}/${messagesResponse.status}/${eventsResponse.status}`,
      );
    }
    const sessions = await sessionsResponse.json() as Array<SessionFacts['session'] & { id?: string }>;
    const session = sessions.find((item) => item.id === id);
    if (!session) throw new Error('会话状态未找到');
    return {
      session,
      messages: await messagesResponse.json() as SessionFacts['messages'],
      events: await eventsResponse.json() as SessionEvent[],
    };
  }, { id: sessionId, tenantId: TENANT_ID });
}

async function readExecution(page: Page, executionId: string): Promise<Record<string, unknown>> {
  /** 读取 Execution 的终态、目标和附件快照，作为浏览器验收的权威事实。 */

  return page.evaluate(async ({ id, tenantId }) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch(`/api/executions/${encodeURIComponent(id)}?tenant_id=${tenantId}`, {
      headers: { Authorization: `Bearer ${auth.token || ''}` },
    });
    if (!response.ok) throw new Error(`Execution 读取失败: ${response.status}`);
    return response.json() as Promise<Record<string, unknown>>;
  }, { id: executionId, tenantId: TENANT_ID });
}

function eventData(event: SessionEvent): Record<string, unknown> {
  /** 兼容事件接口的嵌套 data 和历史扁平投影。 */

  return event.data || event;
}

function eventExecutionId(event: SessionEvent): string {
  /** 提取事件绑定的 Execution 身份。 */

  return String(eventData(event).execution_id || '').trim();
}

function eventTypes(facts: SessionFacts): string[] {
  /** 返回事件类型序列，供回归失败时快速定位状态机分支。 */

  return facts.events.map((event) => String(event.event_type || '').trim()).filter(Boolean);
}

async function prepareChat(page: Page, sessionId: string): Promise<ReturnType<typeof page.locator>> {
  /** 打开聊天页并确保 DynamicTaskAgent 处于选中状态。 */

  await page.goto(`/workspace/chat/${sessionId}`);
  const engine = page.getByLabel('选择 DynamicTaskAgent 复杂任务引擎');
  await expect(engine).toBeVisible({ timeout: 30_000 });
  if (await engine.getAttribute('aria-pressed') !== 'true') await engine.click();
  const composer = page.getByPlaceholder('输入消息，按 Enter 发送...');
  await expect(composer).toBeVisible({ timeout: 30_000 });
  return composer;
}

async function uploadLog(page: Page): Promise<void> {
  /** 通过真实文件选择器上传用户原始日志，并等待受管解析完成。 */

  const uploadResponse = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && new URL(response.url()).pathname === '/api/chat/attachments'
  ));
  await page.locator('input[type="file"]').setInputFiles(LOG_FILE);
  const response = await uploadResponse;
  expect(response.status()).toBe(200);
  await expect(page.getByText(basename(LOG_FILE), { exact: true })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText('解析中', { exact: true })).toHaveCount(0, { timeout: 60_000 });
}

async function sendTurn(
  page: Page,
  composer: ReturnType<typeof page.locator>,
  sessionId: string,
  prompt: string,
  expectedAssistantCount: number,
): Promise<void> {
  /** 发送一轮聊天输入，并等待真实流式请求和权威助手消息完成。 */

  await composer.fill(prompt);
  const responsePromise = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && new URL(response.url()).pathname === '/api/chat/stream'
  ));
  await page.getByRole('button', { name: '发送', exact: true }).click();
  const response = await responsePromise;
  expect(response.status()).toBe(200);
  await expect.poll(
    async () => (await readFacts(page, sessionId)).messages.filter((item) => item.role === 'assistant').length,
    { timeout: 90_000, intervals: [250, 500, 1_000, 2_000] },
  ).toBe(expectedAssistantCount);
}

test('DynamicTask 首轮成功后沿同一会话连续 21 轮并继承日志附件', async ({ page }) => {
  /** 验证终态追问不冲突、不新开无关上下文，并持续保持父子 Execution 链。 */

  const streamRequests: BrowserStreamRequest[] = [];
  page.on('request', (request) => {
    if (request.method() !== 'POST' || new URL(request.url()).pathname !== '/api/chat/stream') return;
    streamRequests.push({ body: JSON.parse(request.postData() || '{}') as Record<string, unknown> });
  });

  await login(page);
  const sessionId = await createSession(page, 'DynamicTask 终态多轮连续回归');
  const composer = await prepareChat(page, sessionId);

  await sendTurn(
    page,
    composer,
    sessionId,
    `请分析日志中的 ${S3_MARKER}，并给出根因和修复建议。`,
    1,
  );
  let facts = await readFacts(page, sessionId);
  expect(facts.messages.filter((item) => item.role === 'assistant')[0]?.content).toContain('S3 日志分析');
  expect(streamRequests[0]?.body.attachments).toEqual([]);
  const dynamicExecutionStatus = facts.events.find((event) => (
    event.event_type === 'stream_status'
    && String(eventData(event).phase || '') === 'dynamic_execution'
  ));
  const dynamicExecutionStatusText = dynamicExecutionStatus
    ? String(eventData(dynamicExecutionStatus).text || '')
    : '';
  expect(dynamicExecutionStatusText).toBe('已接管任务，正在执行分析；任务可能需要一些时间。');
  expect(dynamicExecutionStatusText).not.toContain('读取附件');

  await uploadLog(page);
  await sendTurn(
    page,
    composer,
    sessionId,
    `根据附件日志继续分析 ${S3_MARKER}，并核对第 2 轮结论。`,
    2,
  );

  for (let turn = 3; turn <= 21; turn += 1) {
    await sendTurn(
      page,
      composer,
      sessionId,
      `第 ${turn} 轮继续基于同一日志核对 ${S3_MARKER}，并补充第 ${turn} 个结论。`,
      turn,
    );
  }

  facts = await readFacts(page, sessionId);
  const delegated = facts.events.filter((event) => event.event_type === 'dynamic_task_delegated');
  const continuations = facts.events.filter((event) => event.event_type === 'dynamic_task_continued');
  expect(delegated).toHaveLength(1);
  expect(continuations).toHaveLength(20);
  expect(facts.messages.filter((item) => item.role === 'user')).toHaveLength(21);
  expect(facts.messages.filter((item) => item.role === 'assistant')).toHaveLength(21);

  const forbidden = [
    'error_occurred',
    'dynamic_task_continuation_failed',
    'dynamic_task_execution_failed',
    'dynamic_task_chat_turn_deferred',
    'dynamic_task_rollout_denied',
  ];
  expect(eventTypes(facts).filter((type) => forbidden.includes(type))).toEqual([]);

  const rootExecutionId = eventExecutionId(delegated[0]);
  expect(rootExecutionId).not.toBe('');
  const executionIds = [rootExecutionId];
  let parentExecutionId = rootExecutionId;
  for (const event of continuations) {
    const childExecutionId = eventExecutionId(event);
    const data = eventData(event);
    expect(childExecutionId).not.toBe('');
    expect(data.parent_execution_id).toBe(parentExecutionId);
    executionIds.push(childExecutionId);
    parentExecutionId = childExecutionId;
  }
  expect(new Set(executionIds).size).toBe(21);

  const executions = await Promise.all(executionIds.map(async (id) => ({
    id,
    detail: await readExecution(page, id),
  })));
  for (const [index, item] of executions.entries()) {
    expect(item.detail.status, `第 ${index + 1} 轮 Execution 未成功`).toBe('succeeded');
    expect(item.detail.session_id).toBe(sessionId);
    if (index > 0) {
      expect(String(item.detail.goal || '')).toContain('原始任务：');
      expect(String(item.detail.goal || '')).toContain('本轮用户追加输入：');
      const resources = item.detail.input_resources as Array<Record<string, unknown>>;
      expect(resources.some((resource) => resource.filename === basename(LOG_FILE))).toBe(true);
    }
  }
  console.log('DYNAMIC_MULTITURN_21_ROUND_RESULT', JSON.stringify({
    session_id: sessionId,
    stream_requests: streamRequests.length,
    execution_count: executionIds.length,
    event_types: eventTypes(facts),
    second_turn_attachments: streamRequests[1]?.body.attachments,
  }));
});

test('DynamicTask 首轮等待后用附件恢复同一 Execution', async ({ page }) => {
  /** 验证 clarification waiting 不是冲突终态，第二轮会在原 Execution 上恢复并完成。 */

  const streamRequests: BrowserStreamRequest[] = [];
  page.on('request', (request) => {
    if (request.method() !== 'POST' || new URL(request.url()).pathname !== '/api/chat/stream') return;
    streamRequests.push({ body: JSON.parse(request.postData() || '{}') as Record<string, unknown> });
  });

  await login(page);
  const sessionId = await createSession(page, 'DynamicTask 等待态恢复回归');
  const composer = await prepareChat(page, sessionId);
  await sendTurn(page, composer, sessionId, '请先建立日志分析上下文并确认范围。', 1);
  await uploadLog(page);
  await sendTurn(page, composer, sessionId, '根据附件继续分析，并在本轮确认范围。', 2);

  const facts = await readFacts(page, sessionId);
  const delegated = facts.events.filter((event) => event.event_type === 'dynamic_task_delegated');
  const resumed = facts.events.filter((event) => event.event_type === 'dynamic_task_resumed');
  const continuations = facts.events.filter((event) => event.event_type === 'dynamic_task_continued');
  expect(delegated).toHaveLength(1);
  expect(resumed).toHaveLength(1);
  expect(continuations).toHaveLength(0);
  expect(eventExecutionId(resumed[0])).toBe(eventExecutionId(delegated[0]));
  expect(eventTypes(facts).filter((type) => (
    type === 'error_occurred'
    || type === 'dynamic_task_execution_failed'
    || type === 'dynamic_task_chat_turn_deferred'
  ))).toEqual([]);

  const execution = await readExecution(page, eventExecutionId(delegated[0]));
  expect(execution.status).toBe('succeeded');
  const resources = execution.input_resources as Array<Record<string, unknown>>;
  expect(resources.some((resource) => resource.filename === basename(LOG_FILE))).toBe(true);
  expect(streamRequests).toHaveLength(2);
  expect((streamRequests[1].body.attachments as unknown[]).length).toBeGreaterThan(0);
  console.log('DYNAMIC_WAITING_RESUME_RESULT', JSON.stringify({
    session_id: sessionId,
    execution_id: eventExecutionId(delegated[0]),
    event_types: eventTypes(facts),
  }));
});

test('DynamicTask 真实浏览器断流刷新后重连且不重复渲染', async ({ page }) => {
  /** 让首个 SSE 请求真实发出后刷新页面，验证事件回放能够补齐同一轮终态。 */

  const streamRequests: BrowserStreamRequest[] = [];
  let reloadPromise: Promise<void> | null = null;
  page.on('request', (request) => {
    if (request.method() !== 'POST' || new URL(request.url()).pathname !== '/api/chat/stream') return;
    streamRequests.push({ body: JSON.parse(request.postData() || '{}') as Record<string, unknown> });
    if (reloadPromise) return;
    reloadPromise = new Promise((resolve) => {
      setTimeout(() => {
        void page.reload({ waitUntil: 'domcontentloaded' }).then(() => resolve()).catch(() => resolve());
      }, 75);
    });
  });

  await login(page);
  const sessionId = await createSession(page, 'DynamicTask 断流刷新重连回归');
  const composer = await prepareChat(page, sessionId);
  await composer.fill(`断流刷新后继续分析 ${S3_MARKER}，并给出可执行修复建议。`);
  const sendPromise = page.getByRole('button', { name: '发送', exact: true }).click().catch(() => undefined);
  await expect.poll(
    async () => (await readFacts(page, sessionId)).messages.filter((item) => item.role === 'user').length,
    { timeout: 30_000, intervals: [100, 250, 500] },
  ).toBe(1);
  await expect(reloadPromise).not.toBeNull();
  await reloadPromise;
  await sendPromise;

  await expect.poll(
    async () => (await readFacts(page, sessionId)).messages.filter((item) => item.role === 'assistant').length,
    { timeout: 90_000, intervals: [250, 500, 1_000, 2_000] },
  ).toBe(1);
  const facts = await readFacts(page, sessionId);
  expect(facts.messages.filter((item) => item.role === 'user')).toHaveLength(1);
  expect(facts.messages.filter((item) => item.role === 'assistant')).toHaveLength(1);
  expect(facts.messages.filter((item) => item.role === 'assistant')[0]?.content).toContain('S3 日志分析');
  expect(facts.events.filter((event) => event.event_type === 'assistant_message_created')).toHaveLength(1);
  expect(facts.events.filter((event) => event.event_type === 'execution_succeeded')).toHaveLength(1);
  expect(eventTypes(facts).filter((type) => (
    type === 'error_occurred'
    || type === 'dynamic_task_execution_failed'
    || type === 'dynamic_task_chat_turn_deferred'
  ))).toEqual([]);
  expect(streamRequests).toHaveLength(1);
  console.log('DYNAMIC_RECONNECT_RESULT', JSON.stringify({
    session_id: sessionId,
    stream_requests: streamRequests.length,
    user_messages: facts.messages.filter((item) => item.role === 'user').length,
    assistant_messages: facts.messages.filter((item) => item.role === 'assistant').length,
    event_types: eventTypes(facts),
  }));
});

test('DynamicTask 重复提交同一 client_turn_id 只保留一份回答', async ({ page }) => {
  /** 在真实浏览器上下文重放同一幂等请求，验证服务端回放不会追加第二份答案。 */

  const streamRequests: BrowserStreamRequest[] = [];
  page.on('request', (request) => {
    if (request.method() !== 'POST' || new URL(request.url()).pathname !== '/api/chat/stream') return;
    streamRequests.push({ body: JSON.parse(request.postData() || '{}') as Record<string, unknown> });
  });

  await login(page);
  const sessionId = await createSession(page, 'DynamicTask 重复请求幂等回归');
  const composer = await prepareChat(page, sessionId);
  await sendTurn(page, composer, sessionId, `请分析 ${S3_MARKER} 并输出一份简明结论。`, 1);
  expect(streamRequests).toHaveLength(1);
  const originalBody = streamRequests[0].body;
  expect(originalBody.session_id).toBe(sessionId);
  expect(String(originalBody.client_turn_id || '')).not.toBe('');

  const replay = await page.evaluate(async (body) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch('/api/chat/stream', {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${auth.token || ''}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(body),
    });
    return { status: response.status, stream: await response.text() };
  }, originalBody);
  expect(replay.status).toBe(200);
  expect(replay.stream).toContain('complete');

  const facts = await readFacts(page, sessionId);
  expect(facts.messages.filter((item) => item.role === 'user')).toHaveLength(1);
  expect(facts.messages.filter((item) => item.role === 'assistant')).toHaveLength(1);
  expect(facts.events.filter((event) => event.event_type === 'user_message_received')).toHaveLength(1);
  expect(facts.events.filter((event) => event.event_type === 'assistant_message_created')).toHaveLength(1);
  expect(facts.events.filter((event) => event.event_type === 'execution_succeeded')).toHaveLength(1);
  console.log('DYNAMIC_IDEMPOTENCY_RESULT', JSON.stringify({
    session_id: sessionId,
    client_turn_id: originalBody.client_turn_id,
    replay_status: replay.status,
    user_messages: facts.messages.filter((item) => item.role === 'user').length,
    assistant_messages: facts.messages.filter((item) => item.role === 'assistant').length,
  }));
});

test('DynamicTask 编辑最新问题后可真实重发且不复制旧回答', async ({ page }) => {
  /** 通过页面编辑按钮回填最新用户消息，修改后再次发送并核验两轮各自唯一。 */

  const streamRequests: BrowserStreamRequest[] = [];
  page.on('request', (request) => {
    if (request.method() !== 'POST' || new URL(request.url()).pathname !== '/api/chat/stream') return;
    streamRequests.push({ body: JSON.parse(request.postData() || '{}') as Record<string, unknown> });
  });

  await login(page);
  const sessionId = await createSession(page, 'DynamicTask 编辑重发回归');
  const composer = await prepareChat(page, sessionId);
  const firstPrompt = `请分析 ${S3_MARKER}，先输出第一版结论。`;
  const editedPrompt = `请重新分析 ${S3_MARKER}，只保留根因、风险和修复建议。`;
  await sendTurn(page, composer, sessionId, firstPrompt, 1);

  const editButton = page.getByRole('button', { name: '编辑', exact: true }).last();
  await expect(editButton).toBeVisible({ timeout: 30_000 });
  await editButton.click();
  await expect(composer).toHaveValue(firstPrompt);
  await composer.fill(editedPrompt);
  await page.getByRole('button', { name: '发送', exact: true }).click();
  await expect.poll(
    async () => (await readFacts(page, sessionId)).messages.filter((item) => item.role === 'assistant').length,
    { timeout: 90_000, intervals: [250, 500, 1_000, 2_000] },
  ).toBe(2);

  const facts = await readFacts(page, sessionId);
  const userMessages = facts.messages.filter((item) => item.role === 'user');
  const assistantMessages = facts.messages.filter((item) => item.role === 'assistant');
  expect(userMessages).toHaveLength(2);
  expect(userMessages.map((item) => item.content)).toEqual([firstPrompt, editedPrompt]);
  expect(assistantMessages).toHaveLength(2);
  expect(assistantMessages.every((item) => item.content.includes('S3 日志分析'))).toBe(true);
  expect(facts.events.filter((event) => event.event_type === 'assistant_message_created')).toHaveLength(2);
  expect(facts.events.filter((event) => event.event_type === 'execution_succeeded')).toHaveLength(2);
  expect(streamRequests).toHaveLength(2);
  console.log('DYNAMIC_EDIT_RESEND_RESULT', JSON.stringify({
    session_id: sessionId,
    stream_requests: streamRequests.length,
    user_messages: userMessages.length,
    assistant_messages: assistantMessages.length,
    client_turn_ids: streamRequests.map((item) => item.body.client_turn_id),
  }));
});

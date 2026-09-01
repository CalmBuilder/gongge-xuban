/**
 * @Time       : 2026/08/31
 * @Author     : zhanglp8181
 * @File       : s3-log-diagnosis.fullstack.e2e.ts
 * @CallChain  : Playwright Chromium → Chat Composer → AgentLoop → DynamicTask/普通回复
 * @Description: 用用户提供的 S3 日志验证动态任务门禁失败是否能被真实浏览器定位。
 */

import { expect, test, type Page } from '@playwright/test';

const TENANT_ID = 'tenant_demo';
const AGENT_ID = 'agent_e2e_employee';
const EXPECT_ROLLOUT_DENIAL = process.env.FULLSTACK_E2E_PROFILE === 'kill-switch';
const S3_LOG_PROMPT = `WARN AmazonS3Client - No content length specified for stream data.
Stream contents will be buffered in memory and could result in out of memory errors.
请分析这段 S3 上传告警并给出修复建议。`;

type ChatFacts = {
  messages: Array<{ role: string; content: string }>;
  events: Array<{ event_type?: string; data?: Record<string, unknown> }>;
};

async function login(page: Page): Promise<void> {
  /** 通过真实浏览器上下文建立认证会话。 */

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

async function createSession(page: Page): Promise<string> {
  /** 通过浏览器认证 API 创建绑定到测试数字员工的正式会话。 */

  return page.evaluate(async ({ tenantId, agentId }) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch('/api/chat/sessions', {
      method: 'POST',
      headers: {
        Authorization: 'Bearer ' + auth.token,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        tenant_id: tenantId,
        agent_id: agentId,
        title: 'S3 日志门禁复现',
        origin: 'owned',
      }),
    });
    const body = await response.json() as { id?: string; detail?: string };
    if (!response.ok || !body.id) throw new Error(body.detail || '会话创建失败');
    return body.id;
  }, { tenantId: TENANT_ID, agentId: AGENT_ID });
}

async function readFacts(page: Page, sessionId: string): Promise<ChatFacts> {
  /** 从浏览器上下文读取本轮消息和事件账本。 */

  return page.evaluate(async ({ id, tenantId }) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const headers = auth.token ? { Authorization: 'Bearer ' + auth.token } : {};
    const [messagesResponse, eventsResponse] = await Promise.all([
      fetch('/api/chat/sessions/' + encodeURIComponent(id) + '/messages?tenant_id=' + tenantId, { headers }),
      fetch('/api/chat/sessions/' + encodeURIComponent(id) + '/events?tenant_id=' + tenantId, { headers }),
    ]);
    if (!messagesResponse.ok || !eventsResponse.ok) throw new Error('会话事实读取失败');
    return {
      messages: await messagesResponse.json() as ChatFacts['messages'],
      events: await eventsResponse.json() as ChatFacts['events'],
    };
  }, { id: sessionId, tenantId: TENANT_ID });
}

test('真实 Chromium 发送用户提供的 S3 日志并记录动态门禁结果', async ({ page }) => {
  /** 记录真实请求、响应和事件，不把 S3 日志误判为应用异常来源。 */

  await login(page);
  const sessionId = await createSession(page);
  await page.goto('/workspace/chat/' + sessionId);
  const engine = page.getByLabel('选择 DynamicTaskAgent 复杂任务引擎');
  await expect(engine).toBeVisible({ timeout: 30_000 });
  if (await engine.getAttribute('aria-pressed') !== 'true') {
    await engine.click();
  }
  const composer = page.getByPlaceholder('输入消息，按 Enter 发送...');
  await expect(composer).toBeVisible({ timeout: 30_000 });
  await composer.fill(S3_LOG_PROMPT);
  const requestPromise = page.waitForRequest((request) => (
    request.method() === 'POST' && new URL(request.url()).pathname === '/api/chat/stream'
  ));
  const responsePromise = page.waitForResponse((response) => (
    response.request().method() === 'POST' && new URL(response.url()).pathname === '/api/chat/stream'
  ));
  await page.getByRole('button', { name: '发送', exact: true }).click();
  const request = await requestPromise;
  const response = await responsePromise;
  expect(response.status()).toBe(200);
  const payload = JSON.parse(request.postData() || '{}') as Record<string, unknown>;
  console.log('S3_LOG_BROWSER_REPRO', JSON.stringify({
    execution_engine: payload.execution_engine,
    agent_id: payload.agent_id,
    session_id: payload.session_id,
  }));

  await expect.poll(
    async () => (await readFacts(page, sessionId)).messages.filter((item) => item.role === 'assistant').length,
    { timeout: 60_000, intervals: [250, 500, 1_000, 2_000] },
  ).toBe(1);
  const facts = await readFacts(page, sessionId);
  expect(payload.execution_engine).toBe('dynamic_task');
  if (EXPECT_ROLLOUT_DENIAL) {
    expect(facts.events.map((item) => item.event_type)).toContain('dynamic_task_rollout_denied');
  } else {
    expect(facts.events.map((item) => item.event_type)).not.toContain('dynamic_task_rollout_denied');
    expect(facts.events.map((item) => item.event_type)).toContain('dynamic_task_delegated');
  }
  const assistantContent = facts.messages.filter((item) => item.role === 'assistant').map((item) => item.content);
  if (EXPECT_ROLLOUT_DENIAL) {
    expect(assistantContent[0]).toContain('服务端普通动态总开关拒绝');
    expect(assistantContent[0]).toContain('无需重复选择 DynamicTaskAgent');
  } else {
    expect(assistantContent[0]).toContain('S3 日志分析');
    expect(assistantContent[0]).not.toMatch(/AGENT_LOOP_ERROR|DYNAMIC_TASK_ROLLOUT_DENIED|LLM_ERROR/);
  }
  console.log('S3_LOG_BROWSER_RESULT', JSON.stringify({
    event_types: facts.events.map((item) => item.event_type),
    assistant_excerpt: assistantContent[0]?.slice(0, 120),
    errors: facts.events
      .filter((item) => item.event_type === 'error_occurred' || item.event_type === 'dynamic_task_rollout_denied')
      .map((item) => ({
        event_type: item.event_type,
        code: typeof item.data?.code === 'string' ? item.data.code : undefined,
      })),
  }));
});

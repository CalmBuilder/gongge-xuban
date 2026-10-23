/**
 * @Time       : 2026/09/27
 * @Author     : zhanglp8181
 * @File       : sd3-time-context-live.fullstack.e2e.ts
 * @CallChain  : Chromium → Composer → SOP/DynamicTask AgentLoop → 持久时间事实/执行结果
 * @Description: 在隔离真实模型下验证相对日期跨轮恢复、新输入改期和两条执行分支的时间锚点。
 */

import { expect, test, type Page } from '@playwright/test';
import { resolve } from 'node:path';

import { extractExplicitCalendarDate } from '../src/lib/sd3-date-evidence';

const LIVE_ENABLED = (
  process.env.LIVE_SD3_TIME_E2E === '1' && process.env.LIVE_ATTACHMENT_E2E === '1'
);
const TENANT_ID = 'tenant_demo';
const SOP_SESSION_ID = 'session_attachment_sales_sop';
const DTA_AGENT_ID = 'agent_e2e_employee';
const EXECUTION_WAIT_TIMEOUT_MS = 240_000;
const SALES_ACTUALS_FIXTURE = resolve(
  '../backend/tests/fixtures/attachments/positive/sales_actuals.xlsx',
);
const SALES_TARGETS_FIXTURE = resolve(
  '../backend/tests/fixtures/attachments/positive/sales_targets.csv',
);

test.describe.configure({ mode: 'serial', timeout: 600_000 });
test.skip(
  !LIVE_ENABLED,
  '仅在 LIVE_SD3_TIME_E2E=1 且 LIVE_ATTACHMENT_E2E=1 时调用隔离真实模型',
);
test.use({ timezoneId: 'Pacific/Honolulu' });

type TimeContext = {
  source_input_id: string;
  anchor_at_utc: string;
  timezone: string;
  timezone_source: string;
  local_at: string;
};

type MessageFact = {
  id: string;
  role: string;
  content: string;
  metadata?: { time_context?: TimeContext };
};

type EventFact = {
  event_type: string;
  data?: Record<string, unknown>;
};

/** 通过隔离登录建立真实 Composer 所使用的浏览器认证。 */
async function login(page: Page, username: string, password: string): Promise<void> {
  await page.goto('/enterprise/dashboard');
  const status = await page.evaluate(async ({ username, password }) => {
    const response = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tenant_id: 'tenant_demo', username, password }),
    });
    const body = await response.json();
    if (response.ok) {
      localStorage.setItem('gongge_auth', JSON.stringify(body));
      localStorage.setItem('gongge_onboarding_guide_seen', '1');
      localStorage.setItem('gongge_quick_start_guide_seen', '1');
    }
    return response.status;
  }, { username, password });
  expect(status).toBe(200);
}

/** 只经页面 Composer 投递一轮消息，并等待前端清空发送队列。 */
async function sendThroughComposer(page: Page, message: string): Promise<void> {
  const composer = page.getByPlaceholder('输入消息，按 Enter 发送...');
  await expect(composer).toBeVisible();
  await composer.fill(message);
  await page.getByRole('button', { name: '发送', exact: true }).click();
  await expect(composer).toHaveValue('', { timeout: 15_000 });
}

/** 从服务端账本读取指定会话消息和事件，而不是用 DOM 片段判定执行完成。 */
async function readSessionFacts(page: Page, sessionId: string): Promise<{
  messages: MessageFact[];
  events: EventFact[];
}> {
  return page.evaluate(async ({ sessionId, tenantId }) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const headers = { Authorization: `Bearer ${auth.token}` };
    const [messagesResponse, eventsResponse] = await Promise.all([
      fetch(`/api/chat/sessions/${sessionId}/messages?tenant_id=${tenantId}`, { headers }),
      fetch(`/api/chat/sessions/${sessionId}/events?tenant_id=${tenantId}`, { headers }),
    ]);
    return {
      messages: await messagesResponse.json() as MessageFact[],
      events: await eventsResponse.json() as EventFact[],
    };
  }, { sessionId, tenantId: TENANT_ID });
}

/** 通过管理端会话 API 准备隔离会话，之后的业务输入仍全部来自 Composer。 */
async function createDtaSession(page: Page): Promise<string> {
  const sessionId = await page.evaluate(async ({ tenantId, agentId }) => {
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
        title: 'SD-3 相对日期真实模型验证',
        origin: 'owned',
      }),
    });
    const body = await response.json() as { id?: string; detail?: string };
    if (!response.ok || !body.id) throw new Error(body.detail || 'DTA session creation failed');
    return body.id;
  }, { tenantId: TENANT_ID, agentId: DTA_AGENT_ID });
  expect(sessionId).toMatch(/^session_/);
  return sessionId;
}

/** 查询执行权威状态；执行 ID 始终从本轮事件账本发现，不由测试手传旧 ID。 */
async function readExecution(
  page: Page,
  executionId: string,
): Promise<Record<string, unknown> | null> {
  return page.evaluate(async ({ executionId, tenantId }) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch(`/api/executions/${executionId}?tenant_id=${tenantId}`, {
      headers: { Authorization: `Bearer ${auth.token}` },
    });
    return response.ok ? await response.json() as Record<string, unknown> : null;
  }, { executionId, tenantId: TENANT_ID });
}

/** 读取正式 SOP Runtime 轨迹，核对实例/节点账本而不是只依赖 Execution 展示聚合。 */
async function readSessionTrace(page: Page, sessionId: string): Promise<Record<string, unknown>> {
  return page.evaluate(async ({ sessionId, tenantId }) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch(
      `/api/enterprise/traces/${sessionId}?tenant_id=${tenantId}`,
      { headers: { Authorization: `Bearer ${auth.token}` } },
    );
    return response.ok ? await response.json() as Record<string, unknown> : {};
  }, { sessionId, tenantId: TENANT_ID });
}

/** 从持久事件读取本会话最新动态执行，避免把模型流中间态当成恢复身份。 */
async function latestExecutionId(page: Page, sessionId: string): Promise<string> {
  const facts = await readSessionFacts(page, sessionId);
  return String(
    [...facts.events].reverse().find((event) => event.event_type === 'dynamic_task_delegated')
      ?.data?.execution_id || '',
  );
}

/** 从正式 SOP 启动事件发现当前执行，后续状态读取仍依据账本而非固定夹具 ID。 */
async function latestSopExecutionId(page: Page, sessionId: string): Promise<string> {
  const facts = await readSessionFacts(page, sessionId);
  return String(
    [...facts.events].reverse().find((event) => event.event_type === 'sop_execution_started')
      ?.data?.execution_id || '',
  );
}

/** 按指定 IANA 时区计算输入锚点后的自然日，保留 DST 的日历语义。 */
function plusLocalDays(localAt: string, timezone: string, days: number): string {
  const anchor = new Date(localAt);
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: timezone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(anchor);
  const values = new Map(parts.map((part) => [part.type, part.value]));
  const calendar = new Date(Date.UTC(
    Number(values.get('year')),
    Number(values.get('month')) - 1,
    Number(values.get('day')) + days,
  ));
  return calendar.toISOString().slice(0, 10);
}

/** 从消息正文读取模型明确回写的 V1 日期令牌。 */
function requiredDateToken(content: string, marker: string): string {
  const match = content.match(new RegExp(`${marker}=(\\d{4}-\\d{2}-\\d{2})`));
  expect(match, `真实模型回复缺少 ${marker}=YYYY-MM-DD`).not.toBeNull();
  return match?.[1] || '';
}

/** 从 DTA 的用户可见日期章节读取模型真正写出的新目标日。 */
function requiredDtaDate(content: string): string {
  const actual = extractExplicitCalendarDate(content);
  expect(actual, '真实 DTA 回复缺少本轮新输入的明确日历日期').not.toBeNull();
  return actual || '';
}

test('V1 真实 SOP 跨等待刷新后用新输入改期且回写相对日期', async ({ page }) => {
  /** 先让正式 SOP 因缺附件等待，再刷新、补输入并核对新锚点与日期结果。 */

  await login(page, 'member', 'member');
  await page.goto(`/workspace/chat/${SOP_SESSION_ID}`);
  await expect(page.getByText('Hello E2E 成员数字员工！')).toBeVisible();
  const firstMarker = 'SD3-V1-SOP-FIRST';
  const secondMarker = 'SD3-V1-SOP-RESUME';

  await sendThroughComposer(
    page,
    `${firstMarker}：请先记录一个跨日销售核验任务并等待我补充文件；` +
      '不要在缺少文件时完成任务。请记住本轮时间事实中的明天 09:00。',
  );
  await expect.poll(async () => {
    const facts = await readSessionFacts(page, SOP_SESSION_ID);
    return facts.messages.some((item) => item.content.includes(firstMarker));
  }, { timeout: 30_000 }).toBe(true);
  const firstFacts = await readSessionFacts(page, SOP_SESSION_ID);
  const firstMessage = firstFacts.messages.find((item) => item.content.includes(firstMarker));
  expect(firstMessage?.metadata?.time_context).toMatchObject({
    source_input_id: firstMessage?.id,
    timezone: 'Pacific/Honolulu',
  });
  const firstContext = firstMessage?.metadata?.time_context;
  expect(firstContext).toBeTruthy();
  await expect.poll(() => latestSopExecutionId(page, SOP_SESSION_ID), {
    timeout: 60_000,
    intervals: [500, 1_000, 2_000],
  }).not.toBe('');
  const firstExecutionId = await latestSopExecutionId(page, SOP_SESSION_ID);
  const firstExecution = await readExecution(page, firstExecutionId);
  expect(firstExecution).toMatchObject({ kind: 'sop', status: 'waiting' });
  const firstWaitingFacts = await readSessionFacts(page, SOP_SESSION_ID);
  const firstStartEvent = [...firstWaitingFacts.events].reverse().find((event) => (
    event.event_type === 'sop_execution_started'
    && event.data?.execution_id === firstExecutionId
  ));
  expect(firstStartEvent?.data?.source_message_id).toBe(firstMessage?.id);
  const firstTrace = await readSessionTrace(page, SOP_SESSION_ID);
  const firstRuntime = (
    (firstTrace.sop_runtime as Array<Record<string, unknown>> | undefined) || []
  ).find((item) => item.instance_id === firstExecutionId);
  expect(firstRuntime).toMatchObject({ instance_id: firstExecutionId, status: 'waiting' });
  const firstNodeExecutions = (
    (firstRuntime?.node_executions as Array<Record<string, unknown>> | undefined) || []
  );
  const firstWaitingNode = firstNodeExecutions.find((item) => item.status === 'waiting');
  expect(firstWaitingNode?.node_id).toBeTruthy();
  const firstExecutionIdentity = {
    skill_id: firstRuntime?.skill_id,
    skill_version: firstRuntime?.skill_version,
    definition_checksum: firstRuntime?.definition_checksum,
    current_node_id: firstRuntime?.current_node_id,
    waiting_node_id: firstWaitingNode?.node_id,
  };

  await page.reload();
  await expect(page.getByPlaceholder('输入消息，按 Enter 发送...')).toBeVisible();
  await expect.poll(async () => page.evaluate(async ({ sessionId, tenantId }) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch(`/api/chat/sessions?tenant_id=${tenantId}`, {
      headers: { Authorization: `Bearer ${auth.token}` },
    });
    const rows = await response.json() as Array<{ id: string; agent_id?: string }>;
    return rows.find((item) => item.id === sessionId)?.agent_id || '';
  }, { sessionId: SOP_SESSION_ID, tenantId: TENANT_ID }), {
    timeout: 30_000,
    intervals: [500, 1_000, 2_000],
  }).not.toBe('');
  await page.locator('input[type="file"]').setInputFiles([
    SALES_ACTUALS_FIXTURE,
    SALES_TARGETS_FIXTURE,
  ]);
  await expect(page.getByText('sales_actuals.xlsx', { exact: true })).toBeVisible();
  await expect(page.getByText('sales_targets.csv', { exact: true })).toBeVisible();
  await expect(page.getByText('解析中', { exact: true })).toHaveCount(0, { timeout: 30_000 });
  await sendThroughComposer(
    page,
    `ATTACHMENT-SOP-SALES ${secondMarker}：我已补充文件，请按已发布SOP核验实际与目标，` +
      '生成销售核验报告，并将本轮相对日期改为后天。' +
      '最终回复第一行必须写成 SD3-V1-SOP-DATE=YYYY-MM-DD，且只填本轮时间事实的后天日期。',
  );

  await expect.poll(async () => {
    const facts = await readSessionFacts(page, SOP_SESSION_ID);
    const second = facts.messages.find((item) => item.content.includes(secondMarker));
    return second?.id || '';
  }, {
    timeout: 30_000,
    intervals: [500, 1_000, 2_000],
  }).not.toBe('');
  const secondInput = (await readSessionFacts(page, SOP_SESSION_ID)).messages.find(
    (item) => item.content.includes(secondMarker),
  );
  expect(secondInput).toBeTruthy();
  await expect.poll(async () => {
    const facts = await readSessionFacts(page, SOP_SESSION_ID);
    return facts.events.some((event) => (
      event.event_type === 'complete' && event.data?.user_message_id === secondInput?.id
    ));
  }, {
    timeout: EXECUTION_WAIT_TIMEOUT_MS,
    intervals: [1_000, 2_000, 5_000],
  }).toBe(true);
  await expect.poll(() => latestSopExecutionId(page, SOP_SESSION_ID), {
    timeout: 60_000,
    intervals: [500, 1_000, 2_000],
  }).not.toBe('');
  const executionId = await latestSopExecutionId(page, SOP_SESSION_ID);
  expect(executionId).toBe(firstExecutionId);
  const resumedExecution = await readExecution(page, executionId);
  expect(resumedExecution?.kind).toBe('sop');
  const resumedTrace = await readSessionTrace(page, SOP_SESSION_ID);
  const resumedRuntime = (
    (resumedTrace.sop_runtime as Array<Record<string, unknown>> | undefined) || []
  ).find((item) => item.instance_id === executionId);
  expect(resumedRuntime).toMatchObject({
    instance_id: firstExecutionId,
    skill_id: firstExecutionIdentity.skill_id,
    skill_version: firstExecutionIdentity.skill_version,
    definition_checksum: firstExecutionIdentity.definition_checksum,
  });
  const resumedNodeExecutions = (
    (resumedRuntime?.node_executions as Array<Record<string, unknown>> | undefined) || []
  );
  expect(resumedRuntime?.current_node_id).toBeTruthy();
  expect(resumedNodeExecutions.some((item) => (
    item.node_id === firstExecutionIdentity.current_node_id
  ))).toBe(true);
  expect(resumedNodeExecutions.some((item) => (
    item.node_id === firstExecutionIdentity.waiting_node_id
  ))).toBe(true);
  expect(resumedNodeExecutions.some((item) => (
    item.node_id === resumedRuntime?.current_node_id
  ))).toBe(true);
  await expect.poll(async () => (await readExecution(page, executionId))?.status || null, {
    timeout: EXECUTION_WAIT_TIMEOUT_MS,
    intervals: [1_000, 2_000, 5_000],
  }).toBe('succeeded');
  await page.reload();
  const finalFacts = await readSessionFacts(page, SOP_SESSION_ID);
  const secondMessage = finalFacts.messages.find((item) => item.content.includes(secondMarker));
  const secondContext = secondMessage?.metadata?.time_context;
  expect(secondContext).toMatchObject({
    source_input_id: secondMessage?.id,
    timezone: 'Pacific/Honolulu',
  });
  expect(secondContext?.source_input_id).not.toBe(firstContext?.source_input_id);
  expect(secondContext?.anchor_at_utc).not.toBe(firstContext?.anchor_at_utc);
  const secondMessageIndex = finalFacts.messages.findIndex((item) => item.id === secondMessage?.id);
  const assistant = finalFacts.messages
    .slice(secondMessageIndex + 1)
    .reverse()
    .find((item) => item.role === 'assistant');
  expect(assistant).toBeTruthy();
  expect(requiredDateToken(assistant?.content || '', 'SD3-V1-SOP-DATE')).toBe(
    plusLocalDays(secondContext?.local_at || '', secondContext?.timezone || 'UTC', 2),
  );
  console.log('SD3_V1_SOP_RESULT', JSON.stringify({
    session_id: SOP_SESSION_ID,
    first_source_input_id: firstContext?.source_input_id,
    second_source_input_id: secondContext?.source_input_id,
    first_anchor_at_utc: firstContext?.anchor_at_utc,
    second_anchor_at_utc: secondContext?.anchor_at_utc,
    assistant: assistant?.content,
  }));
});

test('V1 真实 DTA 刷新恢复等待并用新输入改期', async ({ page }) => {
  /** 真实 DynamicTaskAgent 先进入澄清等待，刷新后由 Composer 新输入建立新日期锚点。 */

  await login(page, 'admin', 'admin');
  const sessionId = await createDtaSession(page);
  await page.goto(`/workspace/chat/${sessionId}`);
  const dynamicEngineButton = page.getByRole('button', {
    name: '选择 DynamicTaskAgent 复杂任务引擎',
  });
  await expect(dynamicEngineButton).toHaveAttribute('aria-pressed', 'false');
  await dynamicEngineButton.click();
  await expect(dynamicEngineButton).toHaveAttribute('aria-pressed', 'true');
  const firstMarker = 'SD3-V1-DTA-FIRST';
  const secondMarker = 'SD3-V1-DTA-RESCHEDULE';
  await sendThroughComposer(
    page,
    `${firstMarker}：请先确认本次合同巡检范围是全部合同还是指定区域，再完成合同巡检计划；` +
      '先检索已发布合同巡检规则作为回答前置；缺少范围前保持等待，' +
      '不要生成报告或标记完成；当前目标日期是明天 09:00。',
  );
  await expect.poll(() => latestExecutionId(page, sessionId), {
    timeout: 60_000,
    intervals: [500, 1_000, 2_000],
  }).not.toBe('');
  const executionId = await latestExecutionId(page, sessionId);
  await expect.poll(async () => (await readExecution(page, executionId))?.status || null, {
    timeout: EXECUTION_WAIT_TIMEOUT_MS,
    intervals: [1_000, 2_000, 5_000],
  }).toBe('waiting');
  const beforeReload = await readSessionFacts(page, sessionId);
  const firstMessage = beforeReload.messages.find((item) => item.content.includes(firstMarker));
  const firstContext = firstMessage?.metadata?.time_context;
  expect(firstContext).toMatchObject({
    source_input_id: firstMessage?.id,
    timezone: 'Pacific/Honolulu',
  });

  await page.reload();
  await expect(page.getByPlaceholder('输入消息，按 Enter 发送...')).toBeVisible();
  await sendThroughComposer(
    page,
    `${secondMarker}：我确认合同范围为全部合同，并把目标日期改为后天 10:00；` +
      '恢复原执行并完成，最终回复必须单独列出“目标日期（本轮新输入）”，' +
      '必须写出本轮新输入时间事实对应的实际 YYYY-MM-DD 日历日期，不能只写“后天”，并保留 10:00。',
  );
  await expect.poll(async () => (await readExecution(page, executionId))?.status || null, {
    timeout: EXECUTION_WAIT_TIMEOUT_MS,
    intervals: [1_000, 2_000, 5_000],
  }).toBe('succeeded');

  await page.reload();
  const finalFacts = await readSessionFacts(page, sessionId);
  const secondMessage = finalFacts.messages.find((item) => item.content.includes(secondMarker));
  const secondContext = secondMessage?.metadata?.time_context;
  expect(secondContext).toMatchObject({
    source_input_id: secondMessage?.id,
    timezone: 'Pacific/Honolulu',
  });
  expect(secondContext?.source_input_id).not.toBe(firstContext?.source_input_id);
  expect(secondContext?.anchor_at_utc).not.toBe(firstContext?.anchor_at_utc);
  const assistant = [...finalFacts.messages].reverse().find((item) => item.role === 'assistant');
  const expectedDate = plusLocalDays(
    secondContext?.local_at || '',
    secondContext?.timezone || 'UTC',
    2,
  );
  expect(requiredDtaDate(assistant?.content || '')).toBe(expectedDate);
  console.log('SD3_V1_DTA_RESULT', JSON.stringify({
    session_id: sessionId,
    execution_id: executionId,
    first_source_input_id: firstContext?.source_input_id,
    second_source_input_id: secondContext?.source_input_id,
    first_anchor_at_utc: firstContext?.anchor_at_utc,
    second_anchor_at_utc: secondContext?.anchor_at_utc,
    assistant: assistant?.content,
  }));
});

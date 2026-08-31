/**
 * @Time       : 2026/08/31
 * @Author     : zhanglp8181
 * @File       : builtin-gain-continuity.fullstack.e2e.ts
 * @CallChain  : 普通用户登录 → 开放广场 → 空白/专家能力分身 → Skill 安装 → 聊天 SSE → 消息与事件
 * @Description: 用两个普通用户的真实 Chromium 会话验证普通对话、内置专家、内置 Skill 和专家+Skill 的连续性及增益关系。
 */

import { expect, test, type Browser, type Page } from '@playwright/test';

const TENANT_ID = 'tenant_demo';
const BUILTIN_EXPERT_COUNT = 273;
const BUILTIN_SKILL_COUNT = 37;
const RUN_ID = (process.env.FULLSTACK_E2E_RUN_ID || `gain-${Date.now()}`).replace(/[^a-zA-Z0-9-]/g, '-');

const ACCOUNTS = [
  { username: 'member', password: 'member' },
  { username: 'other-member', password: 'other-member' },
] as const;

const ARMS = ['ordinary', 'expert', 'skill', 'expert+skill'] as const;
type GainArm = (typeof ARMS)[number];

type AgentRow = {
  id: string;
  name: string;
  status: string;
  owner_user_id?: string;
  source_agent_id?: string | null;
  persona_prompt?: string | null;
  published_to_gallery?: boolean;
  agent_category_code?: string;
  metadata?: Record<string, unknown>;
};

type SkillRow = {
  id: string;
  name: string;
  name_zh?: string | null;
  slug: string;
  status: string;
  tenant_id?: string | null;
  current_published_revision_id?: string | null;
  binding_id?: string | null;
  binding_status?: string | null;
  revision_policy?: string | null;
  pinned_revision_id?: string | null;
  metadata?: Record<string, unknown>;
};

type SessionSkillItem = {
  skill_id: string;
  name: string;
  enabled: boolean;
  revision_policy: string;
};

type ChatFacts = {
  messages: Array<{ role: string; content: string }>;
  events: Array<{ event_type?: string; data?: Record<string, unknown> }>;
};

type GainObservation = {
  arm: GainArm;
  score: number;
  sessionId: string;
  agentId: string;
};

type AccountObservation = {
  username: string;
  ownedAgentIds: string[];
  sessionIds: string[];
  observations: Record<GainArm, GainObservation>;
  samples: Array<{
    expertId: string;
    skillId: string;
    observations: Record<GainArm, GainObservation>;
  }>;
};

async function loginThroughUi(page: Page, username: string, password: string): Promise<string> {
  /** 通过产品登录页输入真实凭据，返回当前用户 ID。 */

  await page.goto('/');
  await page.evaluate(() => {
    localStorage.clear();
    localStorage.setItem('gongge_onboarding_guide_seen', '1');
    localStorage.setItem('gongge_quick_start_guide_seen', '1');
  });
  await page.getByRole('button', { name: '进入平台' }).click();
  await page.getByRole('textbox', { name: '账号' }).fill(username);
  await page.getByRole('textbox', { name: '密码', exact: true }).fill(password);
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect.poll(
    async () => page.evaluate((expectedUsername) => {
      const raw = localStorage.getItem('gongge_auth');
      if (!raw) return '';
      const session = JSON.parse(raw) as { user?: { id?: string; username?: string } };
      return session.user?.username === expectedUsername ? session.user.id || '' : '';
    }, username),
    { timeout: 30_000 },
  ).not.toBe('');
  return page.evaluate(() => {
    const raw = localStorage.getItem('gongge_auth');
    if (!raw) throw new Error('登录成功后未找到认证会话');
    const session = JSON.parse(raw) as { user?: { id?: string } };
    if (!session.user?.id) throw new Error('登录成功后未找到用户 ID');
    return session.user.id;
  });
}

async function readJson<T>(page: Page, path: string): Promise<T> {
  /** 使用当前浏览器认证读取受保护接口，避免用未登录请求替代用户操作。 */

  return page.evaluate(async (requestPath) => {
    const raw = localStorage.getItem('gongge_auth');
    const session = raw ? JSON.parse(raw) as { token?: string } : {};
    const response = await fetch(requestPath, {
      headers: session.token ? { Authorization: `Bearer ${session.token}` } : {},
    });
    const body = await response.json();
    if (!response.ok) throw new Error(`请求失败 ${response.status}: ${requestPath}`);
    return body as T;
  }, path);
}

async function readStatus(page: Page, path: string): Promise<number> {
  /** 读取接口状态码，专门验证另一位普通用户不能读取私人资源。 */

  return page.evaluate(async (requestPath) => {
    const raw = localStorage.getItem('gongge_auth');
    const session = raw ? JSON.parse(raw) as { token?: string } : {};
    const response = await fetch(requestPath, {
      headers: session.token ? { Authorization: `Bearer ${session.token}` } : {},
    });
    return response.status;
  }, path);
}

async function readChatFacts(page: Page, sessionId: string): Promise<ChatFacts> {
  /** 读取消息和事件持久账本，确认流式回答已经完成而不是只存在于 DOM。 */

  return page.evaluate(async ({ id, tenantId }) => {
    const raw = localStorage.getItem('gongge_auth');
    const session = raw ? JSON.parse(raw) as { token?: string } : {};
    const headers = session.token ? { Authorization: `Bearer ${session.token}` } : {};
    const [messagesResponse, eventsResponse] = await Promise.all([
      fetch(`/api/chat/sessions/${encodeURIComponent(id)}/messages?tenant_id=${tenantId}`, { headers }),
      fetch(`/api/chat/sessions/${encodeURIComponent(id)}/events?tenant_id=${tenantId}`, { headers }),
    ]);
    if (!messagesResponse.ok || !eventsResponse.ok) {
      throw new Error(`会话证据读取失败 messages=${messagesResponse.status} events=${eventsResponse.status}`);
    }
    return {
      messages: await messagesResponse.json() as ChatFacts['messages'],
      events: await eventsResponse.json() as ChatFacts['events'],
    };
  }, { id: sessionId, tenantId: TENANT_ID });
}

async function createBlankAgent(page: Page, userId: string, name: string): Promise<string> {
  /** 通过数字员工管理页真实创建不继承任何专家、Skill 或工具的空白分身。 */

  await page.goto('/enterprise/agents?view=capability');
  const createButton = page.getByRole('button', { name: /创建新员工/ }).last();
  await expect(createButton).toBeVisible({ timeout: 30_000 });
  await createButton.click();
  const dialog = page.getByRole('dialog', { name: '新建数字员工' });
  await expect(dialog).toBeVisible();
  await dialog.getByRole('button', { name: '从空白开始' }).click();
  await dialog.getByLabel('数字员工姓名').fill(name);
  const createResponse = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && new URL(response.url()).pathname === '/api/enterprise/agents'
  ));
  await dialog.getByRole('button', { name: '创建', exact: true }).click();
  expect((await createResponse).status()).toBe(200);
  await expect(dialog).toBeHidden({ timeout: 30_000 });

  const owned = await readJson<AgentRow[]>(
    page,
    `/api/enterprise/agents?tenant_id=${TENANT_ID}&scope=owned`,
  );
  const created = owned.find((item) => item.name === name);
  expect(created).toMatchObject({ id: expect.any(String), owner_user_id: userId });
  if (!created?.id) throw new Error(`空白分身「${name}」创建后无法读取`);
  expect(created.source_agent_id || null).toBeNull();
  expect(created.persona_prompt || '').toBe('');
  return created.id;
}

async function readBuiltinCatalog(page: Page): Promise<{ experts: AgentRow[]; skills: SkillRow[] }> {
  /** 从当前用户可见的开放广场读取并校验完整内置专家和 Skill 目录。 */

  const experts = await readJson<AgentRow[]>(
    page,
    `/api/enterprise/agents?tenant_id=${TENANT_ID}&scope=expert`,
  );
  const builtinExperts = experts.filter((item) => (
    item.metadata?.expert_source_code === 'agency-agents'
    && item.metadata?.expert_source_label === 'Agency Agents'
  ));
  expect(builtinExperts).toHaveLength(BUILTIN_EXPERT_COUNT);
  expect(builtinExperts.every((item) => (
    item.status === 'active'
    && item.published_to_gallery === true
    && item.agent_category_code === 'professional'
  ))).toBe(true);
  expect(builtinExperts.length).toBeGreaterThanOrEqual(2);

  const skills = await readJson<SkillRow[]>(
    page,
    `/api/enterprise/general-skills?tenant_id=${TENANT_ID}&agent_id=agent_tenant_demo_overall`,
  );
  const builtinSkills = skills.filter((item) => (
    item.status === 'published'
    && item.metadata?.managed_catalog === true
    && item.metadata?.source_kind === 'platform_builtin'
  ));
  expect(builtinSkills).toHaveLength(BUILTIN_SKILL_COUNT);
  expect(builtinSkills.every((item) => (
    item.tenant_id === null
    && Boolean(item.current_published_revision_id)
    && item.metadata?.review_status === 'approved'
    && item.metadata?.approval_status === 'approved'
    && item.metadata?.audit_status === 'approved'
    && item.metadata?.availability_status === 'available'
  ))).toBe(true);
  expect(builtinSkills.length).toBeGreaterThanOrEqual(2);
  return { experts: builtinExperts, skills: builtinSkills };
}

async function findVisibleBuiltinExpert(
  page: Page,
  experts: AgentRow[],
  excludedIds = new Set<string>(),
): Promise<AgentRow> {
  /** 从专家广场当前真实渲染的卡片中选择指定样本，避免只验证接口数据。 */

  const cards = page.locator('.gongge-employee-card');
  await expect(cards.first()).toBeVisible({ timeout: 30_000 });
  for (const expert of experts) {
    if (excludedIds.has(expert.id)) continue;
    const card = cards.filter({ hasText: expert.name }).first();
    if (await card.count()) return expert;
  }
  throw new Error('当前专家广场首屏没有找到可用于增益回归的第二个内置专家');
}

async function copyBuiltinExpert(page: Page, expert: AgentRow, userId: string, name: string): Promise<string> {
  /** 通过开放广场的“复制并定制”真实创建专家能力分身，保留专家身份上下文。 */

  await page.goto('/enterprise/platform/experts');
  const card = page.locator('.gongge-employee-card').filter({ hasText: expert.name }).first();
  await expect(card).toBeVisible({ timeout: 30_000 });
  await card.click();
  await page.getByRole('button', { name: '复制并定制' }).click();
  const dialog = page.getByRole('dialog', { name: '新建数字员工' });
  await expect(dialog).toBeVisible();
  await dialog.getByLabel('数字员工姓名').fill(name);
  await dialog.getByRole('button', { name: '创建', exact: true }).click();
  await expect(dialog).toBeHidden({ timeout: 30_000 });

  const owned = await readJson<AgentRow[]>(
    page,
    `/api/enterprise/agents?tenant_id=${TENANT_ID}&scope=owned`,
  );
  const copied = owned.find((item) => item.name === name);
  expect(copied).toMatchObject({
    id: expect.any(String),
    owner_user_id: userId,
    source_agent_id: expert.id,
  });
  expect(copied?.persona_prompt || '').toContain('专家');
  if (!copied?.id) throw new Error(`专家分身「${name}」创建后无法读取`);
  return copied.id;
}

async function findVisibleBuiltinSkill(page: Page, skills: SkillRow[]): Promise<SkillRow> {
  /** 从真实 Skill 广场卡片中选择一个已审核内置 Skill，确保不是只用目录接口。 */

  const cards = page.locator('.gongge-platform-resource-card');
  await expect(cards.first()).toBeVisible({ timeout: 30_000 });
  for (const skill of skills) {
    const label = (skill.name_zh || skill.name).trim();
    if (!label) continue;
    const card = cards.filter({ hasText: label }).first();
    if (await card.count()) return skill;
  }
  const renderedTitle = (await cards.first().locator('[data-resource-identity] p').first().innerText()).trim();
  const fallback = skills.find((item) => renderedTitle.includes((item.name_zh || item.name).trim()));
  if (!fallback) throw new Error(`首屏 Skill「${renderedTitle}」不在内置 Skill 目录中`);
  return fallback;
}

async function findBuiltinSkillBySlug(
  page: Page,
  skills: SkillRow[],
  slug: string,
): Promise<SkillRow> {
  /** 从公开广场真实渲染的固定 slug 卡片中选择动态组合回归所需的内置 Skill。 */

  const skill = skills.find((item) => item.slug === slug);
  expect(skill).toBeTruthy();
  const card = page.locator('.gongge-platform-resource-card').filter({ hasText: slug }).first();
  await expect(card).toBeVisible({ timeout: 30_000 });
  if (!skill) throw new Error(`内置 Skill「${slug}」不在公开目录中`);
  return skill;
}

async function installBuiltinSkill(page: Page, targetAgentId: string, skill: SkillRow): Promise<void> {
  /** 通过开放广场 Skill 卡片安装到指定分身，并核对固定发布修订绑定。 */

  await page.evaluate((agentId) => {
    localStorage.setItem('gongge_enterprise_agent_scope', agentId);
    window.dispatchEvent(new CustomEvent('gongge-enterprise-agent-scope-change', { detail: { agentId } }));
  }, targetAgentId);
  await page.goto('/enterprise/platform/general-skills');
  const skills = await readJson<SkillRow[]>(
    page,
    `/api/enterprise/general-skills?tenant_id=${TENANT_ID}&agent_id=agent_tenant_demo_overall`,
  );
  const selected = skills.find((item) => item.id === skill.id);
  expect(selected).toBeTruthy();
  const label = (skill.name_zh || skill.name).trim();
  const card = page.locator('.gongge-platform-resource-card').filter({ hasText: label }).first();
  if (await card.count() === 0) {
    const search = page.getByRole('textbox', { name: '搜索Skill' });
    await expect(search).toBeVisible({ timeout: 30_000 });
    await search.fill(skill.slug);
  }
  await expect(card).toBeVisible({ timeout: 30_000 });
  await card.click();
  await page.getByRole('button', { name: '安装到能力分身' }).click();
  await expect(page).toHaveURL(new RegExp(`/workspace/chat/draft/${targetAgentId}$`));

  const installed = await readJson<SkillRow[]>(
    page,
    `/api/enterprise/general-skills?tenant_id=${TENANT_ID}&agent_id=${encodeURIComponent(targetAgentId)}`,
  );
  expect(installed.find((item) => item.id === skill.id)).toMatchObject({
    status: 'published',
    binding_status: 'active',
    revision_policy: 'pinned',
    pinned_revision_id: skill.current_published_revision_id,
  });
}

async function createFormalSession(page: Page, agentId: string, title: string): Promise<string> {
  /** 用当前真实浏览器身份创建正式会话，随后仍由页面 Composer 完成全部消息交互。 */

  const sessionId = await page.evaluate(async ({ agentId, tenantId, title }) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch('/api/chat/sessions', {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${auth.token}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ tenant_id: tenantId, agent_id: agentId, title, origin: 'owned' }),
    });
    const body = await response.json() as { id?: string; detail?: string };
    if (!response.ok || !body.id) throw new Error(body.detail || '正式会话创建失败');
    return body.id;
  }, { agentId, tenantId: TENANT_ID, title });
  expect(sessionId).toMatch(/^session_/);
  return sessionId;
}

async function selectSkill(page: Page, skillName: string): Promise<void> {
  /** 每轮重新选择本轮 Skill，验证 Skill 选择不会错误泄漏或丢失。 */

  const trigger = page.getByRole('button', { name: '选择本轮 Skill' });
  await expect(trigger).toBeVisible({ timeout: 30_000 });
  await trigger.click();
  const item = page.getByRole('menuitem').filter({ hasText: skillName }).first();
  await expect(item).toBeVisible({ timeout: 30_000 });
  await item.click();
  await expect(trigger).toContainText('已选 1 个 Skill');
}

async function sendMessage(page: Page, content: string): Promise<void> {
  /** 通过真实 Composer 发送文本，并确认 SSE 请求成功返回。 */

  const composer = page.getByPlaceholder('输入消息，按 Enter 发送...');
  await expect(composer).toBeVisible({ timeout: 30_000 });
  await composer.fill(content);
  const sendButton = page.getByRole('button', { name: '发送', exact: true });
  await expect(sendButton).toBeEnabled({ timeout: 30_000 });
  const stream = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && new URL(response.url()).pathname === '/api/chat/stream'
  ));
  await sendButton.click();
  await expect(composer).toHaveValue('', { timeout: 10_000 });
  expect((await stream).status()).toBe(200);
}

async function waitForSessionUrl(page: Page, expectedAgentId: string): Promise<string> {
  /** 等待草稿提升为正式会话，并校验会话仍绑定到目标分身。 */

  await expect.poll(
    () => page.url(),
    { timeout: 60_000, intervals: [250, 500, 1_000, 2_000] },
  ).toMatch(/\/workspace\/chat\/session_[^/?#]+$/);
  const match = page.url().match(/\/workspace\/chat\/(session_[^/?#]+)$/);
  if (!match?.[1]) throw new Error('草稿会话没有提升为正式会话');
  const sessionId = match[1];
  const detail = await readJson<{ session?: { agent_id?: string } }>(
    page,
    `/api/enterprise/sessions/${encodeURIComponent(sessionId)}?tenant_id=${TENANT_ID}`,
  );
  expect(detail.session?.agent_id).toBe(expectedAgentId);
  return sessionId;
}

async function waitForAssistant(page: Page, sessionId: string, count: number): Promise<ChatFacts> {
  /** 等待指定数量的助手消息落账，避免读取流式中间态。 */

  await expect.poll(
    async () => (await readChatFacts(page, sessionId)).messages.filter((item) => item.role === 'assistant').length,
    { timeout: 60_000, intervals: [250, 500, 1_000, 2_000] },
  ).toBe(count);
  return readChatFacts(page, sessionId);
}

async function readDynamicExecutionEvidence(page: Page, sessionId: string): Promise<{
  events: ChatFacts['events'];
  executionId: string;
  execution: {
    id?: string;
    session_id?: string;
    agent_id?: string | null;
    kind?: string;
    status?: string;
    goal?: string | null;
    skill_uses?: Array<{
      id: string;
      skill_id: string;
      revision_id: string;
      selection_mode: string;
      status: string;
    }>;
    steps?: Array<{ kind: string; guidance_skill_use_ids: string[]; status?: string }>;
  };
  result: {
    result?: { markdown?: string; guidance_applications?: Array<Record<string, unknown>> };
    verification?: { passed?: boolean };
  };
}> {
  /** 从受保护 Execution 与结果接口读取进入 DynamicTaskAgent 的完整持久证据。 */

  return page.evaluate(async ({ id, tenantId }) => {
    const raw = localStorage.getItem('gongge_auth');
    const session = raw ? JSON.parse(raw) as { token?: string } : {};
    const headers = session.token ? { Authorization: `Bearer ${session.token}` } : {};
    const eventsResponse = await fetch(
      `/api/chat/sessions/${encodeURIComponent(id)}/events?tenant_id=${tenantId}`,
      { headers },
    );
    const events = await eventsResponse.json() as ChatFacts['events'];
    const executionId = String(
      events.find((event) => event.event_type === 'dynamic_task_delegated')?.data?.execution_id || '',
    );
    if (!executionId) throw new Error('真实会话没有 dynamic_task_delegated 事件');
    const executionResponse = await fetch(
      `/api/executions/${encodeURIComponent(executionId)}?tenant_id=${tenantId}`,
      { headers },
    );
    const execution = await executionResponse.json() as {
      id?: string;
      session_id?: string;
      agent_id?: string | null;
      kind?: string;
      status?: string;
      goal?: string | null;
      skill_uses?: Array<{
        id: string;
        skill_id: string;
        revision_id: string;
        selection_mode: string;
        status: string;
      }>;
      steps?: Array<{ kind: string; guidance_skill_use_ids: string[]; status?: string }>;
    };
    if (!executionResponse.ok) throw new Error(`Execution 读取失败：${executionResponse.status}`);
    const resultResponse = await fetch(
      `/api/executions/${encodeURIComponent(executionId)}/result?tenant_id=${tenantId}`,
      { headers },
    );
    const result = await resultResponse.json() as {
      result?: { markdown?: string; guidance_applications?: Array<Record<string, unknown>> };
      verification?: { passed?: boolean };
    };
    if (!resultResponse.ok) throw new Error(`Execution 结果读取失败：${resultResponse.status}`);
    return { events, executionId, execution, result };
  }, { id: sessionId, tenantId: TENANT_ID });
}

function latestAssistants(facts: ChatFacts): string[] {
  /** 从服务端消息账本提取助手回答，作为增益评分的唯一输入。 */

  return facts.messages.filter((item) => item.role === 'assistant').map((item) => item.content);
}

function parseGainAnswer(answer: string): { arm: GainArm; score: number; expert: string; skill: string; continuity: string } {
  /** 解析隔离模型返回的可重复评分字段，避免用回答长度冒充增益。 */

  const match = answer.match(/GAIN-E2E arm=(ordinary|expert|skill|expert\+skill) gain_score=(\d+) expert_signal=(active|inactive) skill_signal=(active|inactive) continuity=(first_turn|verified)/);
  if (!match) throw new Error(`回答缺少 GAIN-E2E 评分字段：${answer}`);
  return {
    arm: match[1] as GainArm,
    score: Number(match[2]),
    expert: match[3],
    skill: match[4],
    continuity: match[5],
  };
}

async function runConversation(
  page: Page,
  account: typeof ACCOUNTS[number],
  arm: GainArm,
  agentId: string,
  skillName?: string,
  formalSessionId?: string,
): Promise<GainObservation> {
  /** 在一个真实浏览器会话中完成四象限两轮连续对话，并核对 Skill 账本事件。 */

  if (formalSessionId) {
    await page.goto(`/workspace/chat/${formalSessionId}`);
  } else {
    await page.goto(`/workspace/chat/draft/${agentId}`);
  }
  await expect(page.getByPlaceholder('输入消息，按 Enter 发送...')).toBeVisible({ timeout: 30_000 });
  if (skillName) await selectSkill(page, skillName);
  await sendMessage(
    page,
    `GAIN-E2E-FIRST-${account.username}：请直接回复，不创建计划、文件或工具任务；分析 CASE-GAIN-REFUND-001，给出事实、风险、动作和验收。`,
  );
  const sessionId = formalSessionId || await waitForSessionUrl(page, agentId);
  const firstFacts = await waitForAssistant(page, sessionId, 1);
  const firstAnswer = latestAssistants(firstFacts)[0] || '';
  const firstParsed = parseGainAnswer(firstAnswer);
  expect(firstParsed.arm).toBe(arm);
  expect(firstParsed.continuity).toBe('first_turn');

  if (skillName) await selectSkill(page, skillName);
  await sendMessage(
    page,
    `GAIN-E2E-SECOND-${account.username}：请直接回复，不创建计划、文件或工具任务；沿用第一轮 CASE-GAIN-REFUND-001 的事实，补充复核结论。`,
  );
  const finalFacts = await waitForAssistant(page, sessionId, 2);
  const assistants = latestAssistants(finalFacts);
  expect(finalFacts.messages.filter((item) => item.role === 'user')).toHaveLength(2);
  expect(assistants).toHaveLength(2);
  const finalParsed = parseGainAnswer(assistants[1] || '');
  expect(finalParsed).toMatchObject({ arm, continuity: 'verified' });
  expect(finalParsed.score).toBe(firstParsed.score);
  expect(finalFacts.events.map((item) => item.event_type)).toContain('assistant_message_created');
  expect(finalFacts.events.map((item) => item.event_type)).not.toContain('error_occurred');
  expect(finalFacts.events.filter((item) => item.event_type === 'dynamic_task_delegated')).toHaveLength(0);
  if (skillName) {
    expect(finalFacts.events.filter((item) => item.event_type === 'skill_loaded')).toHaveLength(2);
    expect(finalFacts.events.filter((item) => item.event_type === 'skill_use_completed')).toHaveLength(2);
  } else {
    expect(finalFacts.events.filter((item) => item.event_type === 'skill_loaded')).toHaveLength(0);
  }
  return { arm, score: finalParsed.score, sessionId, agentId };
}

async function useBuiltinExpert(page: Page, expert: AgentRow): Promise<void> {
  /** 通过开放广场专家卡片的“添加使用并开始对话”进入真实专家草稿。 */

  await page.goto('/enterprise/platform/experts');
  const card = page.locator('.gongge-employee-card').filter({ hasText: expert.name }).first();
  await expect(card).toBeVisible({ timeout: 30_000 });
  await card.click();
  await page.getByRole('button', { name: '添加使用并开始对话' }).click();
  await expect(page).toHaveURL(new RegExp(`/workspace/chat/draft/${expert.id}$`));
}

async function exerciseAccount(
  browser: Browser,
  account: typeof ACCOUNTS[number],
  forbidden?: AccountObservation,
): Promise<AccountObservation> {
  /** 为一个普通用户执行四象限两轮会话，并在第二个用户上下文中验证私人资源隔离。 */

  const context = await browser.newContext();
  const page = await context.newPage();
  const browserFailures: string[] = [];
  page.on('pageerror', (error) => browserFailures.push(`pageerror: ${error.message}`));
  page.on('response', (response) => {
    if (response.status() >= 500) browserFailures.push(`${response.status()} ${response.url()}`);
  });

  try {
    const userId = await loginThroughUi(page, account.username, account.password);
    await page.goto('/enterprise/platform/experts');
    await expect(page.getByRole('heading', { name: '专家' }).first()).toBeVisible({ timeout: 30_000 });
    const catalog = await readBuiltinCatalog(page);
    const expert = await findVisibleBuiltinExpert(page, catalog.experts);
    const alternateExpert = await findVisibleBuiltinExpert(
      page,
      catalog.experts,
      new Set([expert.id]),
    );
    await page.goto('/enterprise/platform/general-skills');
    const skill = await findVisibleBuiltinSkill(page, catalog.skills);
    const alternateSkill = await findVisibleBuiltinSkill(
      page,
      catalog.skills.filter((item) => item.id !== skill.id),
    );

    const baselineAgentId = await createBlankAgent(page, userId, `GAIN ${RUN_ID} ${account.username} 普通基线`);
    const skillAgentId = await createBlankAgent(page, userId, `GAIN ${RUN_ID} ${account.username} Skill 对照`);
    const comboAgentId = await copyBuiltinExpert(
      page,
      expert,
      userId,
      `GAIN ${RUN_ID} ${account.username} 专家组合`,
    );
    const alternateSkillAgentId = await createBlankAgent(
      page,
      userId,
      `GAIN ${RUN_ID} ${account.username} 第二 Skill 对照`,
    );
    const alternateComboAgentId = await copyBuiltinExpert(
      page,
      alternateExpert,
      userId,
      `GAIN ${RUN_ID} ${account.username} 第二专家组合`,
    );

    if (forbidden) {
      const owned = await readJson<AgentRow[]>(
        page,
        `/api/enterprise/agents?tenant_id=${TENANT_ID}&scope=owned`,
      );
      expect(owned.some((item) => forbidden.ownedAgentIds.includes(item.id))).toBe(false);
      for (const sessionId of forbidden.sessionIds) {
        const status = await readStatus(
          page,
          `/api/chat/sessions/${encodeURIComponent(sessionId)}/messages?tenant_id=${TENANT_ID}`,
        );
        expect([403, 404]).toContain(status);
      }
    }

    const baselineSkills = await readJson<SkillRow[]>(
      page,
      `/api/enterprise/general-skills?tenant_id=${TENANT_ID}&agent_id=${encodeURIComponent(baselineAgentId)}`,
    );
    expect(baselineSkills.filter((item) => item.binding_status === 'active')).toHaveLength(0);
    const skillAgent = (await readJson<AgentRow[]>(
      page,
      `/api/enterprise/agents?tenant_id=${TENANT_ID}&scope=owned`,
    )).find((item) => item.id === skillAgentId);
    expect(skillAgent?.source_agent_id || null).toBeNull();
    expect(skillAgent?.persona_prompt || '').toBe('');

    const baseline = await runConversation(page, account, 'ordinary', baselineAgentId);

    await useBuiltinExpert(page, expert);
    const expertOnly = await runConversation(page, account, 'expert', expert.id);

    await installBuiltinSkill(page, skillAgentId, skill);
    const skillSessionId = await createFormalSession(page, skillAgentId, `GAIN ${RUN_ID} ${account.username} 仅 Skill`);
    const skillOnly = await runConversation(page, account, 'skill', skillAgentId, skill.name, skillSessionId);

    await installBuiltinSkill(page, comboAgentId, skill);
    const comboSessionId = await createFormalSession(page, comboAgentId, `GAIN ${RUN_ID} ${account.username} 专家加 Skill`);
    const expertAndSkill = await runConversation(
      page,
      account,
      'expert+skill',
      comboAgentId,
      skill.name,
      comboSessionId,
    );

    const observations = {
      ordinary: baseline,
      expert: expertOnly,
      skill: skillOnly,
      'expert+skill': expertAndSkill,
    } satisfies Record<GainArm, GainObservation>;

    await useBuiltinExpert(page, alternateExpert);
    const alternateExpertOnly = await runConversation(page, account, 'expert', alternateExpert.id);
    await installBuiltinSkill(page, alternateSkillAgentId, alternateSkill);
    const alternateSkillSessionId = await createFormalSession(
      page,
      alternateSkillAgentId,
      `GAIN ${RUN_ID} ${account.username} 第二仅 Skill`,
    );
    const alternateSkillOnly = await runConversation(
      page,
      account,
      'skill',
      alternateSkillAgentId,
      alternateSkill.name,
      alternateSkillSessionId,
    );
    await installBuiltinSkill(page, alternateComboAgentId, alternateSkill);
    const alternateComboSessionId = await createFormalSession(
      page,
      alternateComboAgentId,
      `GAIN ${RUN_ID} ${account.username} 第二专家加 Skill`,
    );
    const alternateExpertAndSkill = await runConversation(
      page,
      account,
      'expert+skill',
      alternateComboAgentId,
      alternateSkill.name,
      alternateComboSessionId,
    );
    const alternateObservations = {
      ordinary: baseline,
      expert: alternateExpertOnly,
      skill: alternateSkillOnly,
      'expert+skill': alternateExpertAndSkill,
    } satisfies Record<GainArm, GainObservation>;

    for (const sample of [observations, alternateObservations]) {
      expect(sample.ordinary.score).toBeLessThan(sample.expert.score);
      expect(sample.ordinary.score).toBeLessThan(sample.skill.score);
      expect(sample.expert.score).toBeLessThan(sample['expert+skill'].score);
      expect(sample.skill.score).toBeLessThan(sample['expert+skill'].score);
    }
    expect(browserFailures).toEqual([]);
    console.log(
      `GAIN-E2E ${account.username}: ordinary=${observations.ordinary.score}, `
      + `expert=${observations.expert.score}, skill=${observations.skill.score}, `
      + `expert+skill=${observations['expert+skill'].score}; `
      + `second-expert=${alternateObservations.expert.score}, `
      + `second-skill=${alternateObservations.skill.score}, `
      + `second-expert+skill=${alternateObservations['expert+skill'].score}`,
    );
    return {
      username: account.username,
      ownedAgentIds: [
        baselineAgentId,
        skillAgentId,
        comboAgentId,
        alternateSkillAgentId,
        alternateComboAgentId,
      ],
      sessionIds: [
        ...Object.values(observations).map((item) => item.sessionId),
        ...Object.values(alternateObservations)
          .filter((item, index) => index > 0)
          .map((item) => item.sessionId),
      ],
      observations,
      samples: [
        { expertId: expert.id, skillId: skill.id, observations },
        { expertId: alternateExpert.id, skillId: alternateSkill.id, observations: alternateObservations },
      ],
    };
  } finally {
    await context.close();
  }
}

test.describe.configure({ mode: 'serial', timeout: 300_000 });

test('两个普通用户通过真实浏览器验证内置专家和 Skill 的四象限增益闭环', async ({ browser }) => {
  /** 证明普通对话 < 单独专家/Skill < 专家+Skill，并覆盖跨轮上下文和用户隔离。 */

  const member = await exerciseAccount(browser, ACCOUNTS[0]);
  const otherMember = await exerciseAccount(browser, ACCOUNTS[1], member);
  expect(otherMember.observations.ordinary.score).toBe(member.observations.ordinary.score);
  expect(otherMember.observations.expert.score).toBe(member.observations.expert.score);
  expect(otherMember.observations.skill.score).toBe(member.observations.skill.score);
  expect(otherMember.observations['expert+skill'].score).toBe(member.observations['expert+skill'].score);
  expect(Object.keys(otherMember.observations)).toEqual(expect.arrayContaining(ARMS));
  expect(otherMember.samples).toHaveLength(member.samples.length);
  for (let index = 0; index < member.samples.length; index += 1) {
    for (const arm of ARMS) {
      expect(otherMember.samples[index]?.observations[arm].score)
        .toBe(member.samples[index]?.observations[arm].score);
    }
  }
});

test('普通用户通过真实浏览器让内置专家与内置 Skill 精确组合进入 DynamicTaskAgent', async ({ page }) => {
  /** 验证公开内置专家来源、公开内置 Skill 绑定和持久 DynamicTaskAgent 是同一条真实会话链路。 */

  const browserFailures: string[] = [];
  page.on('pageerror', (error) => browserFailures.push(`pageerror: ${error.message}`));
  page.on('response', (response) => {
    if (response.status() >= 500) browserFailures.push(`${response.status()} ${response.url()}`);
  });

  const userId = await loginThroughUi(page, ACCOUNTS[0].username, ACCOUNTS[0].password);
  await page.goto('/enterprise/platform/experts');
  await expect(page.getByRole('heading', { name: '专家' }).first()).toBeVisible({ timeout: 30_000 });
  const catalog = await readBuiltinCatalog(page);
  const expert = await findVisibleBuiltinExpert(page, catalog.experts);
  await page.goto('/enterprise/platform/general-skills');
  const skill = await findBuiltinSkillBySlug(page, catalog.skills, 'writing-for-agents');

  const comboAgentId = await copyBuiltinExpert(
    page,
    expert,
    userId,
    `DYNAMIC ${RUN_ID} 内置专家加内置 Skill`,
  );
  await installBuiltinSkill(page, comboAgentId, skill);
  const owned = await readJson<AgentRow[]>(
    page,
    `/api/enterprise/agents?tenant_id=${TENANT_ID}&scope=owned`,
  );
  expect(owned.find((item) => item.id === comboAgentId)).toMatchObject({
    owner_user_id: userId,
    source_agent_id: expert.id,
  });

  const sessionId = await createFormalSession(
    page,
    comboAgentId,
    `DYNAMIC ${RUN_ID} 内置专家 + 内置 Skill`,
  );
  await page.goto(`/workspace/chat/${sessionId}`);
  await expect(page.getByPlaceholder('输入消息，按 Enter 发送...')).toBeVisible({ timeout: 30_000 });
  await selectSkill(page, skill.name);
  const engineButton = page.getByRole('button', { name: '选择 DynamicTaskAgent 复杂任务引擎' });
  await expect(engineButton).toHaveAttribute('aria-pressed', 'false');
  await engineButton.click();
  await expect(engineButton).toHaveAttribute('aria-pressed', 'true');
  const engineRequest = page.waitForRequest((request) => (
    request.method() === 'POST'
    && new URL(request.url()).pathname === '/api/chat/stream'
  ));
  await sendMessage(
    page,
    'BUILTIN-COMBO-ENGINE-TOGGLE：使用本轮选定的内置 Skill 完成 '
      + 'CASE-BUILTIN-COMBO-001，并生成可审计结果。',
  );
  const requestPayload = JSON.parse((await engineRequest).postData() || '{}') as Record<string, unknown>;
  expect(requestPayload.execution_engine).toBe('dynamic_task');
  await waitForAssistant(page, sessionId, 1);
  const evidence = await readDynamicExecutionEvidence(page, sessionId);
  const eventTypes = evidence.events.map((event) => event.event_type);
  expect(eventTypes).toEqual(expect.arrayContaining([
    'skill_loaded',
    'dynamic_task_delegated',
    'execution_succeeded',
    'skill_use_completed',
    'assistant_message_created',
  ]));
  expect(eventTypes).not.toContain('error_occurred');
  expect(evidence.events.find((event) => event.event_type === 'dynamic_task_delegated')?.data)
    .toMatchObject({ requested_execution_engine: 'dynamic_task' });
  expect(evidence.execution).toMatchObject({
    id: evidence.executionId,
    session_id: sessionId,
    agent_id: comboAgentId,
    kind: 'dynamic_task',
    status: 'succeeded',
  });
  expect(evidence.execution.goal).toContain('BUILTIN-COMBO-ENGINE-TOGGLE');
  const skillUse = evidence.execution.skill_uses?.find((item) => item.skill_id === skill.id);
  expect(skillUse).toMatchObject({
    status: 'completed',
    revision_id: skill.current_published_revision_id,
  });
  const answerStep = evidence.execution.steps?.find((step) => step.kind === 'answer');
  expect(answerStep?.status).toBe('succeeded');
  expect(answerStep?.guidance_skill_use_ids).toContain(skillUse?.id);
  expect(evidence.result.verification?.passed).toBe(true);
  expect(evidence.result.result?.markdown).toContain('BUILTIN-COMBO-DYNAMIC-SUCCESS');
  expect(evidence.result.result?.guidance_applications?.length).toBeGreaterThan(0);
  expect(browserFailures).toEqual([]);
  console.log(
    `BUILTIN-COMBO-DYNAMIC ${ACCOUNTS[0].username}: expert=${expert.id}, `
      + `skill=${skill.slug}, agent=${comboAgentId}, execution=${evidence.executionId}`,
  );
});

/**
 * @Time       : 2026/08/31
 * @Author     : zhanglp8181
 * @File       : builtin-open-platform.fullstack.e2e.ts
 * @CallChain  : 登录页 → 开放广场 → 内置专家/Skill → 聊天 SSE → 持久消息与事件
 * @Description: 用两个独立普通用户的真实 Chromium 上下文验收内置专家和 Skill 的可见、安装与会话使用闭环。
 */

import { expect, test, type Browser, type Page } from '@playwright/test';

const TENANT_ID = 'tenant_demo';
const BUILTIN_EXPERT_COUNT = 273;
const BUILTIN_SKILL_COUNT = 37;

const ACCOUNTS = [
  { username: 'member', password: 'member' },
  { username: 'other-member', password: 'other-member' },
] as const;

type AgentRow = {
  id: string;
  name: string;
  status: string;
  owner_user_id?: string;
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
  revision_id: string;
  revision_number: number;
  name: string;
  enabled: boolean;
  revision_policy: string;
  invocation_policy: string;
};

type ChatFacts = {
  messages: Array<{ role: string; content: string }>;
  events: Array<{ event_type?: string; data?: Record<string, unknown> }>;
};

async function loginThroughUi(page: Page, username: string, password: string): Promise<string> {
  /** 通过产品登录页输入凭据，返回当前登录用户 ID。 */

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
  /** 使用当前真实浏览器会话读取受保护接口，作为目录和运行结果的权威断言。 */

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

async function readChatFacts(page: Page, sessionId: string): Promise<ChatFacts> {
  /** 读取会话持久消息和事件，确认 SSE 已完成落账且 Skill 实际被消费。 */

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

async function sendMessage(page: Page, content: string): Promise<void> {
  /** 通过真实聊天输入框发送一轮消息，并确认流式接口已接受请求。 */

  const composer = page.getByPlaceholder('输入消息，按 Enter 发送...');
  await expect(composer).toBeVisible({ timeout: 30_000 });
  await composer.fill(content);
  const sendButton = page.getByRole('button', { name: '发送', exact: true });
  await expect(sendButton).toBeEnabled({ timeout: 30_000 });
  const streamResponse = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && new URL(response.url()).pathname === '/api/chat/stream'
  ));
  await sendButton.click();
  await expect(composer).toHaveValue('', { timeout: 10_000 });
  expect((await streamResponse).status()).toBe(200);
}

async function waitForSessionUrl(page: Page, expectedAgentId: string): Promise<string> {
  /** 等待 draft 会话经 session_created 提升为正式会话，并校验仍绑定到目标 Agent。 */

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
  /** 等待助手消息持久化，避免把流式中间片段误当作完成。 */

  await expect.poll(
    async () => (await readChatFacts(page, sessionId)).messages.filter((item) => item.role === 'assistant').length,
    { timeout: 60_000, intervals: [250, 500, 1_000, 2_000] },
  ).toBe(count);
  return readChatFacts(page, sessionId);
}

async function findVisibleBuiltinSkill(
  page: Page,
  skills: SkillRow[],
): Promise<{ skill: SkillRow; card: ReturnType<Page['locator']> }> {
  /** 从真实页面首屏卡片中找到一个完整内置 Skill，避免只用接口绕过广场展示。 */

  const cards = page.locator('.gongge-platform-resource-card');
  await expect(cards.first()).toBeVisible({ timeout: 30_000 });
  for (const skill of skills) {
    const label = (skill.name_zh || skill.name).trim();
    if (!label) continue;
    const card = cards.filter({ hasText: label }).first();
    if (await card.count()) return { skill, card };
  }
  const renderedTitle = (await cards.first().locator('[data-resource-identity] p').first().innerText()).trim();
  const skill = skills.find((item) => renderedTitle.includes((item.name_zh || item.name).trim()));
  if (!skill) throw new Error(`首屏 Skill「${renderedTitle}」不在内置 Skill 目录中`);
  return { skill, card: cards.first() };
}

async function exerciseAccount(browser: Browser, account: typeof ACCOUNTS[number]): Promise<void> {
  /** 为一个普通用户完成专家发现/会话和 Skill 安装/选择/会话两条真实链路。 */

  const context = await browser.newContext();
  const page = await context.newPage();
  const browserFailures: string[] = [];
  page.on('pageerror', (error) => browserFailures.push(`pageerror: ${error.message}`));
  page.on('response', (response) => {
    if (response.status() >= 500) {
      browserFailures.push(`${response.status()} ${response.url()}`);
    }
  });

  try {
    const userId = await loginThroughUi(page, account.username, account.password);

    await page.goto('/enterprise/platform/experts');
    await expect(page.getByRole('heading', { name: '专家' }).first()).toBeVisible({ timeout: 30_000 });
    const experts = await readJson<AgentRow[]>(
      page,
      `/api/enterprise/agents?tenant_id=${TENANT_ID}&scope=expert`,
    );
    // 普通用户的 Agent 投影会主动裁剪审核内部字段；使用公开的稳定来源标签识别内置专家。
    const builtinExperts = experts.filter((item) => (
      item.metadata?.expert_source_code === 'agency-agents'
      && item.metadata?.expert_source_label === 'Agency Agents'
    ));
    expect(builtinExperts).toHaveLength(BUILTIN_EXPERT_COUNT);
    expect(builtinExperts.every((item) => (
      item.status === 'active'
      && item.published_to_gallery === true
      && item.agent_category_code === 'professional'
      && item.metadata?.expert_source_code === 'agency-agents'
      && item.metadata?.expert_source_label === 'Agency Agents'
    ))).toBe(true);
    const builtinExpert = builtinExperts[0];
    if (!builtinExpert) throw new Error('内置专家目录为空');
    const expertCard = page.locator('.gongge-employee-card').filter({ hasText: builtinExpert.name }).first();
    await expect(expertCard).toBeVisible({ timeout: 30_000 });
    await expertCard.click();
    await page.getByRole('button', { name: '添加使用并开始对话' }).click();
    await expect(page).toHaveURL(new RegExp(`/workspace/chat/draft/${builtinExpert.id}$`));
    await sendMessage(
      page,
      `请直接回复，不创建计划、文件或工具任务；请以「${builtinExpert.name}」身份给出一条结构化建议，标记 EXPERT-BUILTIN-${account.username}。`,
    );
    const expertSessionId = await waitForSessionUrl(page, builtinExpert.id);
    const expertFacts = await waitForAssistant(page, expertSessionId, 1);
    expect(expertFacts.messages.some((item) => item.role === 'assistant' && item.content.trim())).toBe(true);
    expect(expertFacts.events.map((item) => item.event_type)).toContain('assistant_message_created');

    await page.goto('/enterprise/platform/general-skills');
    await expect(page.getByRole('heading', { name: 'Skill' }).first()).toBeVisible({ timeout: 30_000 });
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
    const { skill: selectedSkill, card: skillCard } = await findVisibleBuiltinSkill(page, builtinSkills);
    await skillCard.click();
    await page.getByRole('button', { name: '安装到能力分身' }).click();
    await expect(page).toHaveURL(/\/workspace\/chat\/draft\/[^/?#]+$/);
    const draftMatch = page.url().match(/\/workspace\/chat\/draft\/([^/?#]+)$/);
    if (!draftMatch?.[1]) throw new Error('Skill 安装后没有目标能力分身草稿路由');
    const targetAgentId = draftMatch[1];
    const ownedAgents = await readJson<AgentRow[]>(
      page,
      `/api/enterprise/agents?tenant_id=${TENANT_ID}&scope=owned`,
    );
    const targetAgent = ownedAgents.find((item) => item.id === targetAgentId);
    expect(targetAgent?.owner_user_id).toBe(userId);
    if (account.username === 'other-member') {
      expect(targetAgent?.metadata?.provisioned_for).toBe('open_platform_skill_install');
    }
    const installedSkills = await readJson<SkillRow[]>(
      page,
      `/api/enterprise/general-skills?tenant_id=${TENANT_ID}&agent_id=${encodeURIComponent(targetAgentId)}`,
    );
    expect(installedSkills.find((item) => item.id === selectedSkill.id)).toMatchObject({
      status: 'published',
      binding_status: 'active',
      revision_policy: 'pinned',
      pinned_revision_id: selectedSkill.current_published_revision_id,
    });

    await sendMessage(
      page,
      `请直接回复，不创建计划、文件或工具任务；先核对内置 Skill「${selectedSkill.name}」已安装，再给出一条基线建议。`,
    );
    const skillSessionId = await waitForSessionUrl(page, targetAgentId);
    await waitForAssistant(page, skillSessionId, 1);
    const sessionCatalog = await readJson<{ items: SessionSkillItem[] }>(
      page,
      `/api/chat/sessions/${encodeURIComponent(skillSessionId)}/general-skills?agent_id=${encodeURIComponent(targetAgentId)}`,
    );
    const sessionSkill = sessionCatalog.items.find((item) => item.skill_id === selectedSkill.id);
    expect(sessionSkill).toMatchObject({
      skill_id: selectedSkill.id,
      enabled: true,
      revision_policy: 'pinned',
    });
    if (!sessionSkill) throw new Error('正式会话菜单没有出现已安装的内置 Skill');
    const skillTrigger = page.getByRole('button', { name: '选择本轮 Skill' });
    await expect(skillTrigger).toBeVisible({ timeout: 30_000 });
    await skillTrigger.click();
    await page.getByRole('menuitem').filter({ hasText: sessionSkill.name }).first().click();
    await expect(skillTrigger).toContainText('已选 1 个 Skill');
    const turnRequest = page.waitForRequest((request) => (
      request.method() === 'POST'
      && new URL(request.url()).pathname === '/api/chat/stream'
    ));
    await sendMessage(
      page,
      `请直接回复，不创建计划、文件或工具任务；严格使用本轮选择的「${sessionSkill.name}」输出结构化结果，标记 SKILL-BUILTIN-${account.username}。`,
    );
    const requestPayload = (await turnRequest).postDataJSON() as Record<string, unknown>;
    expect(requestPayload.forced_general_skill_id).toBe(selectedSkill.id);
    expect(requestPayload.forced_general_skill_ids).toEqual([selectedSkill.id]);
    const skillFacts = await waitForAssistant(page, skillSessionId, 2);
    const assistantAnswers = skillFacts.messages.filter((item) => item.role === 'assistant');
    expect(assistantAnswers.at(-1)?.content).toContain('SKILL-AB-TREATMENT');
    const eventTypes = skillFacts.events.map((item) => item.event_type);
    expect(eventTypes).toContain('skill_loaded');
    expect(eventTypes).toContain('skill_use_completed');
    expect(eventTypes).not.toContain('error_occurred');
    expect(browserFailures).toEqual([]);
  } finally {
    await context.close();
  }
}

test.describe.configure({ mode: 'serial', timeout: 300_000 });

test('两个普通用户通过真实浏览器看到并使用内置专家和 Skill', async ({ browser }) => {
  /** 使用独立浏览器上下文隔离两个普通用户的认证、能力分身和会话数据。 */

  for (const account of ACCOUNTS) {
    await exerciseAccount(browser, account);
  }
});

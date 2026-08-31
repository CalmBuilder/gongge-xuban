/**
 * @Time       : 2026/08/31
 * @Author     : zhanglp8181
 * @File       : builtin-skill-matrix.fullstack.e2e.ts
 * @CallChain  : 普通用户登录 → 内置专家 → 37 个 Skill 安装 → 聊天 Composer → DynamicTaskAgent → Execution 证据
 * @Description: 用真实 Chromium 逐个验证全部内置 Skill 的 DynamicTaskAgent 闭环和相对普通对话的增益门禁。
 */

import { expect, test, type Page } from '@playwright/test';

const TENANT_ID = 'tenant_demo';
const BUILTIN_EXPERT_COUNT = 273;
const BUILTIN_SKILL_COUNT = 37;
const RUN_ID = (process.env.FULLSTACK_E2E_RUN_ID || 'skill-matrix-' + Date.now())
  .replace(/[^a-zA-Z0-9-]/g, '-');

const ACCOUNT = {
  username: 'member',
  password: 'member',
} as const;

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

type ChatEvent = {
  event_type?: string;
  data?: Record<string, unknown>;
};

type ChatFacts = {
  messages: Array<{ role: string; content: string }>;
  events: ChatEvent[];
};

type SessionSkillItem = {
  skill_id: string;
  revision_id: string;
  revision_number: number;
  name: string;
  enabled: boolean;
  revision_policy: string;
};

type SkillMatrixScore = {
  arm: 'ordinary' | 'treatment';
  skill: string;
  score: number;
  baseline: number;
  gain: number;
  continuity: string;
};

type ExecutionEvidence = {
  events: ChatEvent[];
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
    steps?: Array<{
      kind: string;
      guidance_skill_use_ids: string[];
      status?: string;
    }>;
  };
  result: {
    result?: {
      markdown?: string;
      guidance_applications?: Array<Record<string, unknown>>;
    };
    verification?: { passed?: boolean };
  };
};

async function loginThroughUi(page: Page): Promise<string> {
  /** 通过产品登录页输入普通用户凭据，并返回登录用户 ID。 */

  await page.goto('/');
  await page.evaluate(() => {
    localStorage.clear();
    localStorage.setItem('gongge_onboarding_guide_seen', '1');
    localStorage.setItem('gongge_quick_start_guide_seen', '1');
  });
  await page.getByRole('button', { name: '进入平台' }).click();
  await page.getByRole('textbox', { name: '账号' }).fill(ACCOUNT.username);
  await page.getByRole('textbox', { name: '密码', exact: true }).fill(ACCOUNT.password);
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect.poll(
    async () => page.evaluate((expectedUsername) => {
      const raw = localStorage.getItem('gongge_auth');
      if (!raw) return '';
      const session = JSON.parse(raw) as { user?: { id?: string; username?: string } };
      return session.user?.username === expectedUsername ? session.user.id || '' : '';
    }, ACCOUNT.username),
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
  /** 使用当前浏览器中的普通用户认证读取受保护接口。 */

  return page.evaluate(async (requestPath) => {
    const raw = localStorage.getItem('gongge_auth');
    const session = raw ? JSON.parse(raw) as { token?: string } : {};
    const response = await fetch(requestPath, {
      headers: session.token ? { Authorization: 'Bearer ' + session.token } : {},
    });
    const body = await response.json();
    if (!response.ok) throw new Error('请求失败 ' + response.status + ': ' + requestPath);
    return body as T;
  }, path);
}

async function createBlankAgent(page: Page, userId: string, name: string): Promise<string> {
  /** 通过真实管理页面创建没有专家和 Skill 的普通对话基线分身。 */

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
    '/api/enterprise/agents?tenant_id=' + TENANT_ID + '&scope=owned',
  );
  const created = owned.find((item) => item.name === name);
  expect(created).toMatchObject({ id: expect.any(String), owner_user_id: userId });
  if (!created?.id) throw new Error('普通对话基线分身创建后无法读取');
  expect(created.source_agent_id || null).toBeNull();
  expect(created.persona_prompt || '').toBe('');
  return created.id;
}

async function readBuiltinCatalog(page: Page): Promise<{ experts: AgentRow[]; skills: SkillRow[] }> {
  /** 读取并校验普通用户在开放平台可见的完整内置专家和 Skill 目录。 */

  const experts = await readJson<AgentRow[]>(
    page,
    '/api/enterprise/agents?tenant_id=' + TENANT_ID + '&scope=expert',
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

  const skills = await readJson<SkillRow[]>(
    page,
    '/api/enterprise/general-skills?tenant_id=' + TENANT_ID
      + '&agent_id=agent_tenant_demo_overall',
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
  return { experts: builtinExperts, skills: builtinSkills };
}

async function findVisibleBuiltinExpert(page: Page, experts: AgentRow[]): Promise<AgentRow> {
  /** 在专家广场真实渲染的首屏卡片中找到一个内置专家。 */

  const cards = page.locator('.gongge-employee-card');
  await expect(cards.first()).toBeVisible({ timeout: 30_000 });
  for (const expert of experts) {
    const card = cards.filter({ hasText: expert.name }).first();
    if (await card.count()) return expert;
  }
  throw new Error('专家广场当前首屏没有找到内置专家');
}

async function copyBuiltinExpert(page: Page, expert: AgentRow, userId: string, name: string): Promise<string> {
  /** 通过开放平台真实复制一个内置专家作为 37 个 Skill 的统一测试对象。 */

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
    '/api/enterprise/agents?tenant_id=' + TENANT_ID + '&scope=owned',
  );
  const copied = owned.find((item) => item.name === name);
  expect(copied).toMatchObject({
    id: expect.any(String),
    owner_user_id: userId,
    source_agent_id: expert.id,
  });
  if (!copied?.id) throw new Error('内置专家能力分身创建后无法读取');
  expect(copied.persona_prompt || '').toContain('专家');
  return copied.id;
}

async function installBuiltinSkill(page: Page, targetAgentId: string, skill: SkillRow): Promise<void> {
  /** 通过开放平台真实卡片和“安装到能力分身”动作安装一个固定发布修订。 */

  await page.evaluate((agentId) => {
    localStorage.setItem('gongge_enterprise_agent_scope', agentId);
    window.dispatchEvent(new CustomEvent('gongge-enterprise-agent-scope-change', { detail: { agentId } }));
  }, targetAgentId);
  await page.goto('/enterprise/platform/general-skills');
  const cards = page.locator('.gongge-platform-resource-card');
  await expect(cards.first()).toBeVisible({ timeout: 30_000 });
  const label = (skill.name_zh || skill.name || skill.slug).trim();
  const card = cards.filter({ hasText: label }).first();
  if (await card.count() === 0) {
    const search = page.getByRole('textbox', { name: '搜索Skill' });
    await expect(search).toBeVisible({ timeout: 30_000 });
    await search.fill(skill.slug);
  }
  await expect(card).toBeVisible({ timeout: 30_000 });
  await card.click();
  await page.getByRole('button', { name: '安装到能力分身' }).click();
  await expect(page).toHaveURL(new RegExp('/workspace/chat/draft/' + targetAgentId + '$'));
}

async function createFormalSession(page: Page, agentId: string, title: string): Promise<string> {
  /** 使用当前普通用户创建绑定到指定能力分身的正式会话。 */

  const sessionId = await page.evaluate(async ({ agentId, tenantId, title }) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch('/api/chat/sessions', {
      method: 'POST',
      headers: {
        Authorization: 'Bearer ' + auth.token,
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

async function selectSkill(page: Page, skill: SkillRow): Promise<void> {
  /** 在聊天 Composer 的 Skill 菜单中选中本轮唯一 Skill。 */

  const trigger = page.getByRole('button', { name: '选择本轮 Skill' });
  await expect(trigger).toBeVisible({ timeout: 30_000 });
  await trigger.click();
  // Composer 菜单展示稳定 slug；必须匹配菜单项内的 exact 文本，避免 handoff
  // 被 claude-handoff 这类前缀项抢先命中。
  const exactLabel = page.getByText(skill.slug, { exact: true });
  const item = page.getByRole('menuitem').filter({ has: exactLabel }).first();
  await expect(item).toBeVisible({ timeout: 30_000 });
  await item.click();
  await expect(trigger).toContainText('已选 1 个 Skill');
}

async function ensureEngine(page: Page, enabled: boolean): Promise<void> {
  /** 通过 Composer 的无障碍 pressed 状态设置本轮 DynamicTaskAgent 开关。 */

  const button = page.getByRole('button', { name: '选择 DynamicTaskAgent 复杂任务引擎' });
  await expect(button).toBeVisible({ timeout: 30_000 });
  const expected = enabled ? 'true' : 'false';
  if (await button.getAttribute('aria-pressed') !== expected) {
    await button.click();
  }
  await expect(button).toHaveAttribute('aria-pressed', expected);
}

async function sendMessage(page: Page, content: string): Promise<void> {
  /** 通过真实 Composer 发送消息，并确认 SSE HTTP 请求返回成功。 */

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

async function readChatFacts(page: Page, sessionId: string): Promise<ChatFacts> {
  /** 从消息和事件账本读取已持久化的真实会话事实。 */

  return page.evaluate(async ({ id, tenantId }) => {
    const raw = localStorage.getItem('gongge_auth');
    const session = raw ? JSON.parse(raw) as { token?: string } : {};
    const headers = session.token ? { Authorization: 'Bearer ' + session.token } : {};
    const messagesPath = '/api/chat/sessions/' + encodeURIComponent(id)
      + '/messages?tenant_id=' + tenantId;
    const eventsPath = '/api/chat/sessions/' + encodeURIComponent(id)
      + '/events?tenant_id=' + tenantId;
    const [messagesResponse, eventsResponse] = await Promise.all([
      fetch(messagesPath, { headers }),
      fetch(eventsPath, { headers }),
    ]);
    if (!messagesResponse.ok || !eventsResponse.ok) {
      throw new Error(
        '会话证据读取失败 messages=' + messagesResponse.status
          + ' events=' + eventsResponse.status,
      );
    }
    return {
      messages: await messagesResponse.json() as ChatFacts['messages'],
      events: await eventsResponse.json() as ChatEvent[],
    };
  }, { id: sessionId, tenantId: TENANT_ID });
}

async function waitForAssistant(page: Page, sessionId: string): Promise<ChatFacts> {
  /** 等待本轮助手消息落账，避免读取流式中间态。 */

  await expect.poll(
    async () => (await readChatFacts(page, sessionId)).messages
      .filter((item) => item.role === 'assistant').length,
    { timeout: 60_000, intervals: [250, 500, 1_000, 2_000] },
  ).toBe(1);
  return readChatFacts(page, sessionId);
}

async function readExecutionEvidence(page: Page, sessionId: string): Promise<ExecutionEvidence> {
  /** 读取 DynamicTaskAgent 的 Execution、SkillUse、结果和事件闭环证据。 */

  return page.evaluate(async ({ id, tenantId }) => {
    const raw = localStorage.getItem('gongge_auth');
    const session = raw ? JSON.parse(raw) as { token?: string } : {};
    const headers = session.token ? { Authorization: 'Bearer ' + session.token } : {};
    const eventsPath = '/api/chat/sessions/' + encodeURIComponent(id)
      + '/events?tenant_id=' + tenantId;
    const eventsResponse = await fetch(eventsPath, { headers });
    if (!eventsResponse.ok) throw new Error('事件读取失败：' + eventsResponse.status);
    const events = await eventsResponse.json() as ChatEvent[];
    const executionId = String(
      events.find((event) => event.event_type === 'dynamic_task_delegated')?.data?.execution_id || '',
    );
    if (!executionId) throw new Error('没有 dynamic_task_delegated 事件');
    const executionPath = '/api/executions/' + encodeURIComponent(executionId)
      + '?tenant_id=' + tenantId;
    const executionResponse = await fetch(executionPath, { headers });
    if (!executionResponse.ok) throw new Error('Execution 读取失败：' + executionResponse.status);
    const execution = await executionResponse.json() as ExecutionEvidence['execution'];
    const resultPath = '/api/executions/' + encodeURIComponent(executionId)
      + '/result?tenant_id=' + tenantId;
    const resultResponse = await fetch(resultPath, { headers });
    if (!resultResponse.ok) throw new Error('Execution 结果读取失败：' + resultResponse.status);
    const result = await resultResponse.json() as ExecutionEvidence['result'];
    return { events, executionId, execution, result };
  }, { id: sessionId, tenantId: TENANT_ID });
}

function parseMatrixScore(answer: string): SkillMatrixScore {
  /** 解析固定 100 分制结果，拒绝没有 Skill 标识或普通基线的回答。 */

  const match = answer.match(
    /SKILL-MATRIX arm=(ordinary|treatment) skill=([a-z0-9-]+) score=(\d+) baseline=(\d+) gain=(\d+) continuity=(first_turn)/,
  );
  if (!match) throw new Error('回答缺少 SKILL-MATRIX 评分字段：' + answer);
  return {
    arm: match[1] as SkillMatrixScore['arm'],
    skill: match[2] || '',
    score: Number(match[3]),
    baseline: Number(match[4]),
    gain: Number(match[5]),
    continuity: match[6] || '',
  };
}

test.describe.configure({ mode: 'serial', timeout: 1_200_000 });

test('普通用户逐个验证 37 个内置 Skill 在 DynamicTaskAgent 中闭环并获得增益', async ({ page }) => {
  /** 每个 Skill 使用新正式会话，避免前一项的消息、选择状态或执行状态污染后一项。 */

  const browserFailures: string[] = [];
  page.on('pageerror', (error) => browserFailures.push('pageerror: ' + error.message));
  page.on('response', (response) => {
    if (response.status() >= 500) browserFailures.push(response.status() + ' ' + response.url());
  });

  const userId = await loginThroughUi(page);
  await page.goto('/enterprise/platform/experts');
  await expect(page.getByRole('heading', { name: '专家' }).first()).toBeVisible({ timeout: 30_000 });
  const catalog = await readBuiltinCatalog(page);
  const expert = await findVisibleBuiltinExpert(page, catalog.experts);
  const baselineAgentId = await createBlankAgent(
    page,
    userId,
    'MATRIX ' + RUN_ID + ' 普通对话基线',
  );
  const treatmentAgentId = await copyBuiltinExpert(
    page,
    expert,
    userId,
    'MATRIX ' + RUN_ID + ' 内置专家 Skill 全量验证',
  );

  const baselineSessionId = await createFormalSession(
    page,
    baselineAgentId,
    'MATRIX ' + RUN_ID + ' 普通对话基线',
  );
  await page.goto('/workspace/chat/' + baselineSessionId);
  await expect(page.getByPlaceholder('输入消息，按 Enter 发送...')).toBeVisible({ timeout: 30_000 });
  await ensureEngine(page, false);
  const baselineRequest = page.waitForRequest((request) => (
    request.method() === 'POST'
    && new URL(request.url()).pathname === '/api/chat/stream'
  ));
  await sendMessage(
    page,
    'SKILL-MATRIX-BASELINE：请完成 CASE-SKILL-MATRIX-BASELINE 的普通对话分析，'
      + '输出事实、风险、动作和验收结论。',
  );
  const baselinePayload = JSON.parse((await baselineRequest).postData() || '{}') as Record<string, unknown>;
  expect(baselinePayload.execution_engine).toBe('auto');
  expect(baselinePayload.forced_general_skill_id).toBeUndefined();
  const baselineFacts = await waitForAssistant(page, baselineSessionId);
  const baselineScore = parseMatrixScore(
    baselineFacts.messages.filter((item) => item.role === 'assistant')[0]?.content || '',
  );
  expect(baselineScore).toMatchObject({
    arm: 'ordinary',
    skill: 'none',
    score: 76,
    continuity: 'first_turn',
  });
  expect(baselineFacts.events.map((item) => item.event_type)).not.toContain('dynamic_task_delegated');
  expect(baselineFacts.events.map((item) => item.event_type)).not.toContain('error_occurred');

  const orderedSkills = [...catalog.skills].sort((left, right) => left.slug.localeCompare(right.slug));
  for (const skill of orderedSkills) {
    await installBuiltinSkill(page, treatmentAgentId, skill);
  }
  const installedSkills = await readJson<SkillRow[]>(
    page,
    '/api/enterprise/general-skills?tenant_id=' + TENANT_ID
      + '&agent_id=' + encodeURIComponent(treatmentAgentId),
  );
  const activeInstalledSkills = installedSkills.filter((item) => item.binding_status === 'active');
  expect(activeInstalledSkills).toHaveLength(BUILTIN_SKILL_COUNT);
  for (const skill of orderedSkills) {
    expect(activeInstalledSkills.find((item) => item.id === skill.id)).toMatchObject({
      status: 'published',
      binding_status: 'active',
      revision_policy: 'pinned',
      pinned_revision_id: skill.current_published_revision_id,
    });
  }

  const orderedMatrixSkills = process.env.BUILTIN_SKILL_MATRIX_ONLY_SLUG
    ? orderedSkills.filter((skill) => skill.slug === process.env.BUILTIN_SKILL_MATRIX_ONLY_SLUG)
    : orderedSkills;
  if (process.env.BUILTIN_SKILL_MATRIX_ONLY_SLUG) {
    expect(orderedMatrixSkills).toHaveLength(1);
  }
  const results: Array<{
    slug: string;
    score: number;
    baseline: number;
    gain: number;
    executionId: string;
  }> = [];
  const failures: string[] = [];
  for (const skill of orderedMatrixSkills) {
    let sessionId = '';
    try {
      sessionId = await createFormalSession(
        page,
        treatmentAgentId,
        'MATRIX ' + RUN_ID + ' ' + skill.slug,
      );
      await page.goto('/workspace/chat/' + sessionId);
      await expect(page.getByPlaceholder('输入消息，按 Enter 发送...')).toBeVisible({ timeout: 30_000 });
      const sessionCatalog = await readJson<{ items: SessionSkillItem[] }>(
        page,
        '/api/chat/sessions/' + encodeURIComponent(sessionId)
          + '/general-skills?agent_id=' + encodeURIComponent(treatmentAgentId),
      );
      const menuSkill = sessionCatalog.items.find((item) => item.name === skill.slug);
      expect(menuSkill).toMatchObject({
        skill_id: skill.id,
        revision_id: skill.current_published_revision_id,
        revision_policy: 'pinned',
        enabled: true,
      });
      await selectSkill(page, skill);
      await ensureEngine(page, true);
      const requestPromise = page.waitForRequest((request) => (
        request.method() === 'POST'
        && new URL(request.url()).pathname === '/api/chat/stream'
      ));
      await sendMessage(
        page,
        'SKILL-MATRIX-TREATMENT skill=' + skill.slug
          + ' 使用本轮唯一选定的内置 Skill 完成 CASE-SKILL-MATRIX-'
          + skill.slug
          + '，形成可审计、可验收的结果。',
      );
      const requestPayload = JSON.parse((await requestPromise).postData() || '{}') as Record<string, unknown>;
      expect(requestPayload.execution_engine).toBe('dynamic_task');
      expect(requestPayload.forced_general_skill_id).toBe(skill.id);
      expect(requestPayload.forced_general_skill_ids).toEqual([skill.id]);

      const facts = await waitForAssistant(page, sessionId);
      const score = parseMatrixScore(
        facts.messages.filter((item) => item.role === 'assistant')[0]?.content || '',
      );
      expect(score).toMatchObject({
        arm: 'treatment',
        skill: skill.slug,
        baseline: baselineScore.score,
        continuity: 'first_turn',
      });
      expect(score.gain).toBe(score.score - baselineScore.score);
      expect(score.gain >= 15 || score.score > 93).toBe(true);
      expect(facts.events.map((item) => item.event_type)).not.toContain('error_occurred');

      const evidence = await readExecutionEvidence(page, sessionId);
      const eventTypes = evidence.events.map((event) => event.event_type);
      expect(eventTypes).toEqual(expect.arrayContaining([
        'skill_loaded',
        'dynamic_task_delegated',
        'execution_succeeded',
        'skill_use_completed',
        'assistant_message_created',
      ]));
      expect(evidence.events.find((event) => event.event_type === 'dynamic_task_delegated')?.data)
        .toMatchObject({ requested_execution_engine: 'dynamic_task' });
      expect(evidence.execution).toMatchObject({
        id: evidence.executionId,
        session_id: sessionId,
        agent_id: treatmentAgentId,
        kind: 'dynamic_task',
        status: 'succeeded',
      });
      expect(evidence.execution.goal).toContain('CASE-SKILL-MATRIX-' + skill.slug);
      const skillUses = (evidence.execution.skill_uses || []).filter((item) => item.skill_id === skill.id);
      expect(skillUses).toHaveLength(1);
      const skillUse = skillUses[0];
      expect(skillUse).toMatchObject({
        revision_id: skill.current_published_revision_id,
        selection_mode: 'forced',
        status: 'completed',
      });
      const answerStep = evidence.execution.steps?.find((step) => step.kind === 'answer');
      expect(answerStep?.status).toBe('succeeded');
      expect(answerStep?.guidance_skill_use_ids).toContain(skillUse?.id);
      expect(evidence.result.verification?.passed).toBe(true);
      expect(evidence.result.result?.guidance_applications?.length).toBeGreaterThan(0);

      results.push({
        slug: skill.slug,
        score: score.score,
        baseline: score.baseline,
        gain: score.gain,
        executionId: evidence.executionId,
      });
      console.log(
        'SKILL-MATRIX PASS skill=' + skill.slug
          + ' score=' + score.score
          + ' baseline=' + score.baseline
          + ' gain=' + score.gain
          + ' execution=' + evidence.executionId,
      );
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      let serverDetail = '';
      if (sessionId) {
        try {
          const facts = await readChatFacts(page, sessionId);
          const failureEvent = facts.events.find((event) => event.event_type === 'error_occurred');
          const failureData = failureEvent?.data;
          if (failureData) {
            serverDetail = ' server=' + JSON.stringify({
              code: failureData.code,
              message: failureData.message,
            });
          }
        } catch {
          // 页面已结束时保留原始 Playwright 断言，避免诊断本身掩盖首个失败。
        }
      }
      failures.push(skill.slug + ': ' + detail + serverDetail);
      console.error('SKILL-MATRIX FAIL skill=' + skill.slug + ': ' + detail + serverDetail);
    }
  }

  expect(results).toHaveLength(orderedMatrixSkills.length);
  expect(failures).toEqual([]);
  expect(results.every((item) => item.gain >= 15 || item.score > 93)).toBe(true);
  expect(browserFailures).toEqual([]);
  console.log(
    'SKILL-MATRIX SUMMARY total=' + results.length
      + ' passed=' + results.length
      + ' baseline=' + baselineScore.score
      + ' min_gain=' + Math.min(...results.map((item) => item.gain))
      + ' min_score=' + Math.min(...results.map((item) => item.score)),
  );
});

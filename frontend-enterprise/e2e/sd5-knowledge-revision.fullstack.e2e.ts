/**
 * @Time       : 2026/09/29
 * @Author     : zhanglp8181
 * @File       : sd5-knowledge-revision.fullstack.e2e.ts
 * @CallChain  : Chromium → 知识页面正文编辑 → revision job → candidate publish/CAS
 * @Description: 验证正文修订双击去重、双上下文 CAS 冲突保留草稿，以及 SOP/DTA 对当前版本的真实消费。
 */

import { expect, test, type Page } from '@playwright/test';

type ApiCall = {
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE';
  body?: Record<string, unknown>;
};

type ApiResult = {
  status: number;
  body: unknown;
};

const TENANT_ID = 'tenant_demo';
const MEMBER_AGENT_ID = 'agent_e2e_member_employee';
const MEMBER_DOCUMENT_ID = 'kdoc_e2e_member';

function asRecord(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('expected JSON object');
  }
  return value as Record<string, unknown>;
}

async function login(page: Page): Promise<void> {
  /** 建立隔离全栈浏览器登录态，不使用真实业务账号。 */

  await page.goto('/enterprise/dashboard');
  const status = await page.evaluate(async (tenantId) => {
    const response = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tenant_id: tenantId, username: 'admin', password: 'admin' }),
    });
    const body = await response.json() as Record<string, unknown>;
    if (response.ok) {
      localStorage.setItem('gongge_auth', JSON.stringify(body));
      localStorage.setItem('gongge_onboarding_guide_seen', '1');
      localStorage.setItem('gongge_quick_start_guide_seen', '1');
    }
    if (!response.ok) return { status: response.status, overallAgentId: '' };
    const agentsResponse = await fetch(`/api/enterprise/agents?tenant_id=${encodeURIComponent(tenantId)}`, {
      headers: { Authorization: `Bearer ${String(body.token || '')}` },
    });
    const agents = await agentsResponse.json() as Array<{ id?: string; is_overall?: boolean }>;
    const overallAgentId = agents.find((agent) => agent.is_overall)?.id || '';
    if (overallAgentId) localStorage.setItem('gongge_enterprise_agent_scope', overallAgentId);
    return { status: response.status, overallAgentId };
  }, TENANT_ID);
  expect(status.status).toBe(200);
  expect(status.overallAgentId).toBeTruthy();
}

async function apiJson(page: Page, path: string, call: ApiCall = {}): Promise<ApiResult> {
  /** 通过真实服务 API 准备/核对隔离夹具，不替换页面请求。 */

  return page.evaluate(async ({ requestPath, method, body }) => {
    const rawSession = localStorage.getItem('gongge_auth');
    if (!rawSession) throw new Error('browser session is missing');
    const session = JSON.parse(rawSession) as { token?: string };
    const response = await fetch(requestPath, {
      method,
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${session.token || ''}`,
      },
      body,
    });
    const text = await response.text();
    let parsed: unknown = text;
    try {
      parsed = text ? JSON.parse(text) as unknown : {};
    } catch {
      // 保留非 JSON 错误，便于断言实际 HTTP 结果。
    }
    return { status: response.status, body: parsed };
  }, {
    requestPath: path,
    method: call.method || 'GET',
    body: call.body === undefined ? undefined : JSON.stringify(call.body),
  });
}

async function waitForJob(
  page: Page,
  jobId: string,
  agentId?: string,
): Promise<Record<string, unknown>> {
  /** 等待持久任务终态，并返回服务端保存的完成事实。 */

  let finalJob: Record<string, unknown> = {};
  await expect.poll(
    async () => {
      const agentQuery = agentId ? `&agent_id=${encodeURIComponent(agentId)}` : '';
      const result = await apiJson(
        page,
        `/api/enterprise/knowledge/jobs/${jobId}?tenant_id=${TENANT_ID}${agentQuery}`,
      );
      if (result.status !== 200) {
        return `http:${result.status}:${JSON.stringify(result.body)}`;
      }
      finalJob = asRecord(result.body);
      const status = String(finalJob.status || '');
      if (status === 'completed') return status;
      return `${status}:${String(finalJob.error_code || '')}:${String(finalJob.error || '')}`;
    },
    { timeout: 30_000, intervals: [250, 500, 1_000] },
  ).toBe('completed');
  return finalJob;
}

async function waitForTerminalJob(
  page: Page,
  jobId: string,
  expectedStatus: string,
  agentId?: string,
): Promise<Record<string, unknown>> {
  /** 等待真实服务将修订任务收口到指定失败或成功终态。 */

  let finalJob: Record<string, unknown> = {};
  await expect.poll(
    async () => {
      const agentQuery = agentId ? `&agent_id=${encodeURIComponent(agentId)}` : '';
      const result = await apiJson(
        page,
        `/api/enterprise/knowledge/jobs/${jobId}?tenant_id=${TENANT_ID}${agentQuery}`,
      );
      if (result.status !== 200) return `http:${result.status}`;
      finalJob = asRecord(result.body);
      return String(finalJob.status || '');
    },
    { timeout: 30_000, intervals: [250, 500, 1_000] },
  ).toBe(expectedStatus);
  return finalJob;
}

async function loginAsMember(page: Page): Promise<void> {
  /** 建立成员自己的真实浏览器会话，并固定到成员数字员工知识分支。 */

  await page.goto('/enterprise/dashboard');
  const status = await page.evaluate(({ agentId, tenantId }) => fetch('/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tenant_id: tenantId, username: 'member', password: 'member' }),
  }).then(async (response) => {
    const body = await response.json() as Record<string, unknown>;
    if (response.ok) {
      localStorage.setItem('gongge_auth', JSON.stringify(body));
      localStorage.setItem('gongge_onboarding_guide_seen', '1');
      localStorage.setItem('gongge_quick_start_guide_seen', '1');
      localStorage.setItem('gongge_enterprise_agent_scope', agentId);
    }
    return response.status;
  }), { agentId: MEMBER_AGENT_ID, tenantId: TENANT_ID });
  expect(status).toBe(200);
}

async function createChatSession(page: Page, title: string): Promise<string> {
  /** 通过真实聊天会话 API 创建 Composer 的持久会话。 */

  const created = await page.evaluate(async ({ sessionTitle, tenantId, agentId }) => {
    const raw = localStorage.getItem('gongge_auth');
    if (!raw) throw new Error('browser session is missing');
    const token = (JSON.parse(raw) as { token?: string }).token || '';
    const response = await fetch('/api/chat/sessions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({
        tenant_id: tenantId,
        agent_id: agentId,
        title: sessionTitle,
        origin: 'owned',
      }),
    });
    const body = await response.json() as { id?: string; detail?: string };
    if (!response.ok || !body.id) throw new Error(body.detail || 'chat session creation failed');
    return body.id;
  }, { sessionTitle: title, tenantId: TENANT_ID, agentId: MEMBER_AGENT_ID });
  return created;
}

async function sendComposerMessage(page: Page, message: string, sessionTitle?: string): Promise<void> {
  /** 通过实际 Composer 输入框发送一轮，并确认聊天 SSE 成功返回。 */

  if (sessionTitle) {
    await expect(page.getByRole('button', { name: new RegExp(sessionTitle) })).toBeVisible({
      timeout: 30_000,
    });
  }
  await page.getByPlaceholder('输入消息，按 Enter 发送...').fill(message);
  const responsePromise = page.waitForResponse((response) => (
    new URL(response.url()).pathname === '/api/chat/stream'
    && response.request().method() === 'POST'
  ));
  await page.getByRole('button', { name: '发送', exact: true }).click();
  expect((await responsePromise).status()).toBe(200);
}

async function openKnowledgeDocumentEditor(page: Page, title: string): Promise<void> {
  /** 在真实知识页面打开指定正文编辑器，保留页面自身的 head/CAS 快照。 */

  await page.goto('/enterprise/knowledge');
  // 等待 App 壳层首轮员工范围查询完成，再切到平台范围；否则慢 MySQL
  // 的异步默认员工选择可能在页面打开编辑器后覆盖平台范围。
  await expect(page.getByText('当前员工', { exact: true })).toBeVisible({ timeout: 30_000 });
  await page.getByRole('button', { name: '平台知识治理', exact: true }).click();
  await expect(page.getByText('知识库广场', { exact: true }).first()).toBeVisible();
  await expect(page.getByText(title, { exact: true }).first()).toBeVisible();
  await page.getByRole('button', { name: '详情', exact: true }).first().click();
  await page.getByRole('button', { name: '修改', exact: true }).click();
  await expect(page.getByRole('textbox', { name: '文档正文' })).toBeVisible();
}

async function readConsumerEvidence(
  page: Page,
  sessionId: string,
): Promise<{ events: Array<Record<string, unknown>>; trace: Record<string, unknown> }> {
  /** 从服务端事件与 Runtime trace 读取消费者和版本事实，不以 DOM 代替账本断言。 */

  return page.evaluate(async ({ id, tenantId }) => {
    const raw = localStorage.getItem('gongge_auth');
    if (!raw) throw new Error('browser session is missing');
    const token = (JSON.parse(raw) as { token?: string }).token || '';
    const headers = { Authorization: `Bearer ${token}` };
    const [eventsResponse, traceResponse] = await Promise.all([
      fetch(`/api/chat/sessions/${id}/events?tenant_id=${tenantId}`, { headers }),
      fetch(`/api/enterprise/traces/${id}?tenant_id=${tenantId}`, { headers }),
    ]);
    return {
      events: await eventsResponse.json() as Array<Record<string, unknown>>,
      trace: await traceResponse.json() as Record<string, unknown>,
    };
  }, { id: sessionId, tenantId: TENANT_ID });
}

async function importSd5DynamicGuidance(page: Page): Promise<void> {
  /** 通过正式 Skill 导入页面安装只允许 knowledge.search 的 DTA 指导。 */

  const markdown = [
    '---',
    'name: sd5-dynamic-knowledge-guidance',
    'description: SD5 动态任务必须读取当前发布知识版本。',
    'allowed-tools: knowledge.search',
    '---',
    '# SD5 Dynamic Knowledge Guidance',
    'SD5-DYNAMIC-KNOWLEDGE-GUIDANCE：必须检索当前发布正文并形成可审计结论。',
    '',
  ].join('\n');
  await page.goto('/enterprise/general-skills');
  await page.getByRole('button', { name: /新增/ }).click();
  await page.getByRole('menuitem', { name: '安全导入 Skill' }).click();
  const dialog = page.getByRole('dialog', { name: '安全导入 Skill 包' });
  await dialog.locator('input[type="file"]').setInputFiles({
    name: 'SKILL.md',
    mimeType: 'text/markdown',
    buffer: Buffer.from(markdown),
  });
  await dialog.getByRole('button', { name: '生成安全预览' }).click();
  await expect(dialog.getByText('sd5-dynamic-knowledge-guidance', { exact: true })).toBeVisible({
    timeout: 30_000,
  });
  const confirmed = page.waitForResponse((response) => (
    new URL(response.url()).pathname.endsWith('/confirm')
    && response.request().method() === 'POST'
  ));
  await dialog.getByRole('button', { name: '固定版本并绑定' }).click();
  expect((await confirmed).status()).toBe(200);
  await expect(dialog).not.toBeVisible();
}

test('SD-5 真实知识页面双击正文只受理一个修订并可刷新读取新 head', async ({ page }) => {
  test.setTimeout(120_000);
  await login(page);
  let knowledgeBaseId = '';
  const created = await apiJson(page, '/api/enterprise/knowledge-bases', {
      method: 'POST',
      body: {
        tenant_id: TENANT_ID,
        name: `SD5 浏览器修订 ${Date.now()}`,
        description: '隔离知识正文修订夹具',
      },
    });
    expect(created.status).toBe(200);
    knowledgeBaseId = String(asRecord(created.body).id || '');
    expect(knowledgeBaseId).toBeTruthy();

    const initialSource = '# 浏览器初始正文\n\n唯一旧事实：sd5-old-canary。';
    const initialUpload = await apiJson(page, '/api/enterprise/knowledge/documents', {
      method: 'POST',
      body: {
        tenant_id: TENANT_ID,
        knowledge_base_id: knowledgeBaseId,
        filename: 'sd5-browser-policy.md',
        title: 'SD5 浏览器正文',
        content_base64: Buffer.from(initialSource, 'utf-8').toString('base64'),
      },
    });
    expect(initialUpload.status).toBe(200);
    const initialJobId = String(asRecord(initialUpload.body).id || '');
    expect(initialJobId).toBeTruthy();
    const initialJob = await waitForJob(page, initialJobId);
    const initialDocumentId = String(initialJob.published_document_id || '');
    expect(initialDocumentId).toBeTruthy();

    const initialContent = await apiJson(
      page,
      `/api/enterprise/knowledge/documents/${initialDocumentId}/content?tenant_id=${TENANT_ID}`,
    );
    expect(initialContent.status).toBe(200);
    expect(asRecord(initialContent.body).content_md).toBe(initialSource);

    const revisionRequests: Array<Record<string, unknown>> = [];
    const revisionResponses: number[] = [];
    page.on('request', (request) => {
      if (
        request.method() === 'PUT'
        && new URL(request.url()).pathname === `/api/enterprise/knowledge/documents/${initialDocumentId}`
      ) {
        revisionRequests.push(request.postDataJSON() as Record<string, unknown>);
      }
    });
    page.on('response', (response) => {
      if (
        response.request().method() === 'PUT'
        && new URL(response.url()).pathname === `/api/enterprise/knowledge/documents/${initialDocumentId}`
      ) {
        revisionResponses.push(response.status());
      }
    });

    await page.goto('/enterprise/knowledge');
    await page.getByRole('button', { name: '平台知识治理', exact: true }).click();
    await expect(page.getByText('SD5 浏览器正文', { exact: true }).first()).toBeVisible();
    await page.getByRole('button', { name: '详情', exact: true }).first().click();
    await page.getByRole('button', { name: '修改', exact: true }).click();
    const editor = page.getByRole('textbox', { name: '文档正文' });
    await expect(editor).toHaveValue(initialSource);
    const updatedSource = '# 浏览器更新正文\n\n唯一新事实：sd5-new-canary。';
    await editor.fill(updatedSource);

    // 同一事件循环内两次真实 DOM click，模拟页面快速双击。
    await page.getByRole('button', { name: '保存正文', exact: true }).evaluate((element) => {
      (element as HTMLButtonElement).click();
      (element as HTMLButtonElement).click();
    });
    await expect.poll(() => revisionRequests.length, { timeout: 10_000 }).toBe(2);
    await expect.poll(() => revisionResponses.length, { timeout: 10_000 }).toBe(2);
    expect(revisionResponses).toEqual([202, 202]);
    expect(revisionRequests[0]?.client_request_id).toBeTruthy();
    expect(revisionRequests[1]?.client_request_id).toBe(revisionRequests[0]?.client_request_id);

    let revisionJobId = '';
    await expect.poll(
      async () => {
        const jobsResult = await apiJson(page, `/api/enterprise/knowledge/jobs?tenant_id=${TENANT_ID}&limit=20`);
        const jobs = Array.isArray(jobsResult.body) ? jobsResult.body.map(asRecord) : [];
        revisionJobId = String(
          jobs.find(
            (job) => job.job_kind === 'revision' && job.document_id === initialDocumentId,
          )?.id || '',
        );
        return revisionJobId;
      },
      { timeout: 10_000 },
    ).not.toBe('');
    const revisionJob = await waitForJob(page, revisionJobId);
    await expect(page.getByRole('textbox', { name: '文档正文' })).not.toBeVisible();
    // 以 job 列表中的 revision 事实为准，不能从按钮通知推断派发次数。
    const jobsResult = await apiJson(page, `/api/enterprise/knowledge/jobs?tenant_id=${TENANT_ID}&limit=20`);
    const jobs = Array.isArray(jobsResult.body) ? jobsResult.body.map(asRecord) : [];
    const revisionJobs = jobs.filter((job) => job.job_kind === 'revision');
    expect(revisionJobs).toHaveLength(2);
    expect(revisionJob.status).toBe('completed');
    const completedRevision = revisionJobs.find((job) => job.document_id === initialDocumentId);
    expect(completedRevision).toBeTruthy();
    if (!completedRevision) throw new Error('completed revision job is missing');
    const publishedDocumentId = String(completedRevision.published_document_id || '');
    expect(publishedDocumentId).toBeTruthy();
    const publishedContent = await apiJson(
      page,
      `/api/enterprise/knowledge/documents/${publishedDocumentId}/content?tenant_id=${TENANT_ID}`,
    );
    expect(publishedContent.status).toBe(200);
    expect(asRecord(publishedContent.body).content_md).toBe(updatedSource);

    await page.reload();
    await openKnowledgeDocumentEditor(page, 'SD5 浏览器正文');
  await expect(page.getByRole('textbox', { name: '文档正文' })).toHaveValue(updatedSource);
});

test('SD-5 页面修订后 Composer 的 SOP 与 DTA 都消费当前版本', async ({ page }) => {
  test.setTimeout(180_000);
  await loginAsMember(page);

  const currentContent = await apiJson(
    page,
    `/api/enterprise/knowledge/documents/${MEMBER_DOCUMENT_ID}/content?tenant_id=${TENANT_ID}&agent_id=${MEMBER_AGENT_ID}`,
  );
  expect(currentContent.status).toBe(200);
  const initialSource = String(asRecord(currentContent.body).content_md || '');
  expect(initialSource).toContain('sd5-browser-old');

  await page.goto('/enterprise/knowledge');
  await expect(page.getByText('E2E Member Knowledge Base', { exact: true }).last()).toBeVisible();
  await page.getByRole('button', { name: '知识库操作' }).last().click();
  await page.getByRole('menuitem', { name: '详情', exact: true }).click();
  await page.getByRole('dialog', { name: '知识库详情' }).getByRole('button', { name: '取消' }).click();
  await page.getByText('E2E Member Knowledge Base', { exact: true }).last().click();
  await expect(page.getByText('SD5 浏览器知识', { exact: true }).last()).toBeVisible();
  await page.getByRole('button', { name: '详情', exact: true }).last().click();
  await page.getByRole('dialog', { name: '文档详情' }).getByRole('button', { name: '修改', exact: true }).click();
  const editor = page.getByRole('textbox', { name: '文档正文' });
  await expect(editor).toHaveValue(initialSource);
  const updatedSource = '# SD5 浏览器当前正文\n\n唯一新事实：sd5-browser-new。';
  await editor.fill(updatedSource);
  await page.getByRole('button', { name: '保存正文', exact: true }).click();

  let revisionJobId = '';
  await expect.poll(async () => {
    const jobs = await apiJson(
      page,
      `/api/enterprise/knowledge/jobs?tenant_id=${TENANT_ID}&agent_id=${MEMBER_AGENT_ID}&limit=20`,
    );
    const rows = Array.isArray(jobs.body) ? jobs.body.map(asRecord) : [];
    revisionJobId = String(rows.find(
      (job) => job.job_kind === 'revision' && job.document_id === MEMBER_DOCUMENT_ID,
    )?.id || '');
    return revisionJobId;
  }, { timeout: 15_000 }).not.toBe('');
  const revision = await waitForJob(page, revisionJobId, MEMBER_AGENT_ID);
  expect(revision.status).toBe('completed');
  expect(String(revision.published_version_id || '')).toBeTruthy();
  const publishedContent = await apiJson(
    page,
    `/api/enterprise/knowledge/documents/${String(revision.published_document_id)}/content?tenant_id=${TENANT_ID}&agent_id=${MEMBER_AGENT_ID}`,
  );
  expect(asRecord(publishedContent.body).content_md).toBe(updatedSource);

  const sopSessionId = await createChatSession(page, 'SD5 SOP 当前知识消费者');
  await page.goto(`/workspace/chat/${sopSessionId}`);
  await sendComposerMessage(
    page,
    'SD5-SOP-KNOWLEDGE：请读取当前发布正文并回答。',
    'SD5 SOP 当前知识消费者',
  );
  await expect(page.getByRole('main').getByText(/SD5-SOP-KNOWLEDGE-SUCCESS/)).toBeVisible({
    timeout: 45_000,
  });
  const sopEvidence = await readConsumerEvidence(page, sopSessionId);
  const sopSerialized = JSON.stringify(sopEvidence);
  expect(sopSerialized).toContain('sd5-browser-new');
  expect(sopSerialized).not.toContain('sd5-browser-old');
  expect(sopSerialized).toContain(String(revision.published_version_id));

  await importSd5DynamicGuidance(page);
  const dtaSessionId = await createChatSession(page, 'SD5 DTA 当前知识消费者');
  await page.goto(`/workspace/chat/${dtaSessionId}`);
  await sendComposerMessage(
    page,
    'SD5-DYNAMIC-KNOWLEDGE：请依据当前发布知识形成结论。',
    'SD5 DTA 当前知识消费者',
  );
  await expect(page.getByRole('main').getByText(/SD5-DYNAMIC-KNOWLEDGE-SUCCESS/)).toBeVisible({
    timeout: 45_000,
  });
  const dtaEvidence = await readConsumerEvidence(page, dtaSessionId);
  const dtaDelegated = dtaEvidence.events.find((event) => event.event_type === 'dynamic_task_delegated');
  const dtaExecutionId = String(asRecord(dtaDelegated?.data).execution_id || '');
  expect(dtaExecutionId).toBeTruthy();
  const dtaExecution = await page.evaluate(async ({ executionId, tenantId }) => {
    const raw = localStorage.getItem('gongge_auth');
    if (!raw) throw new Error('browser session is missing');
    const token = (JSON.parse(raw) as { token?: string }).token || '';
    return fetch(`/api/executions/${executionId}?tenant_id=${tenantId}`, {
      headers: { Authorization: `Bearer ${token}` },
    }).then((response) => response.json()) as Promise<Record<string, unknown>>;
  }, { executionId: dtaExecutionId, tenantId: TENANT_ID });
  expect(dtaExecution.status).toBe('succeeded');
  const dtaSerialized = JSON.stringify(dtaEvidence);
  expect(dtaSerialized).toContain('sd5-browser-new');
  expect(dtaSerialized).not.toContain('sd5-browser-old');
  expect(dtaSerialized).toContain(String(revision.published_version_id));
});

test('SD-5 真实页面失败后重新编辑使用新身份并隔离员工范围', async ({ page }) => {
  test.setTimeout(150_000);
  await login(page);
  const title = `SD5 浏览器失败重试 ${Date.now()}`;
  const initialSource = '# 失败重试初始正文\n\n唯一事实：sd5-browser-failure-old。';
  const created = await apiJson(page, '/api/enterprise/knowledge-bases', {
    method: 'POST',
    body: {
      tenant_id: TENANT_ID,
      name: title,
      description: '隔离失败重试与范围切换夹具',
    },
  });
  expect(created.status).toBe(200);
  const knowledgeBaseId = String(asRecord(created.body).id || '');
  expect(knowledgeBaseId).toBeTruthy();

  const uploaded = await apiJson(page, '/api/enterprise/knowledge/documents', {
    method: 'POST',
    body: {
      tenant_id: TENANT_ID,
      knowledge_base_id: knowledgeBaseId,
      filename: 'sd5-browser-failure.md',
      title: 'SD5 浏览器失败重试正文',
      content_base64: Buffer.from(initialSource, 'utf-8').toString('base64'),
    },
  });
  expect(uploaded.status).toBe(200);
  const initialJob = await waitForJob(page, String(asRecord(uploaded.body).id || ''));
  const initialDocumentId = String(initialJob.published_document_id || '');
  expect(initialDocumentId).toBeTruthy();

  await openKnowledgeDocumentEditor(page, 'SD5 浏览器失败重试正文');
  const armed = await apiJson(page, '/__e2e/sd5/arm-failure', { method: 'POST' });
  expect(armed.status).toBe(200);

  const revisionRequests: Array<Record<string, unknown>> = [];
  page.on('request', (request) => {
    if (
      request.method() === 'PUT'
      && new URL(request.url()).pathname === `/api/enterprise/knowledge/documents/${initialDocumentId}`
    ) {
      revisionRequests.push(request.postDataJSON() as Record<string, unknown>);
    }
  });

  const firstDraft = '# 第一次失败修订\n\n唯一事实：sd5-browser-failure-red。';
  await page.getByRole('textbox', { name: '文档正文' }).fill(firstDraft);
  const firstResponse = page.waitForResponse((response) => (
    response.request().method() === 'PUT'
    && new URL(response.url()).pathname === `/api/enterprise/knowledge/documents/${initialDocumentId}`
  ));
  await page.getByRole('button', { name: '保存正文', exact: true }).click();
  const firstResult = await firstResponse;
  expect(firstResult.status()).toBe(202);
  const firstAccepted = asRecord(await firstResult.json());
  const firstJobId = String(firstAccepted.job_id || '');
  expect(firstJobId).toBeTruthy();
  const failedJob = await waitForTerminalJob(page, firstJobId, 'failed');
  expect(failedJob.error_code).toBe('KNOWLEDGE_REVISION_BUILD_FAILED');
  await expect(page.getByRole('textbox', { name: '文档正文' })).toBeVisible();

  const secondDraft = '# 失败后重新编辑\n\n唯一事实：sd5-browser-failure-retry。';
  await page.getByRole('textbox', { name: '文档正文' }).fill(secondDraft);
  const secondResponse = page.waitForResponse((response) => (
    response.request().method() === 'PUT'
    && new URL(response.url()).pathname === `/api/enterprise/knowledge/documents/${initialDocumentId}`
  ));
  await page.getByRole('button', { name: '保存正文', exact: true }).click();
  const secondResult = await secondResponse;
  expect(secondResult.status()).toBe(202);
  const secondAccepted = asRecord(await secondResult.json());
  const secondJobId = String(secondAccepted.job_id || '');
  expect(secondJobId).toBeTruthy();
  expect(secondJobId).not.toBe(firstJobId);
  await expect.poll(() => revisionRequests.length, { timeout: 10_000 }).toBe(2);
  expect(revisionRequests[1]?.client_request_id).toBeTruthy();
  expect(revisionRequests[1]?.client_request_id).not.toBe(revisionRequests[0]?.client_request_id);

  const completedJob = await waitForTerminalJob(page, secondJobId, 'completed');
  expect(completedJob.published_document_id).toBeTruthy();
  const publishedContent = await apiJson(
    page,
    `/api/enterprise/knowledge/documents/${String(completedJob.published_document_id)}/content?tenant_id=${TENANT_ID}`,
  );
  expect(asRecord(publishedContent.body).content_md).toBe(secondDraft);

  // 编辑器关闭后详情弹窗仍可能保留在页面上；先回到真实管理页，再验证员工范围切换。
  await page.goto('/enterprise/dashboard');
  await page.getByLabel('切换当前员工').first().click();
  await expect(page.getByRole('menuitem').first()).toBeVisible();
  await page.getByRole('menuitem').first().click();
  await expect(page).toHaveURL(/\/enterprise\/dashboard/);
  await page.goto('/enterprise/knowledge');
  await expect(page.getByText('当前员工', { exact: true })).toBeVisible();
  await expect(page.getByRole('textbox', { name: '文档正文' })).not.toBeVisible();
  expect(await page.evaluate(() => localStorage.getItem('gongge_enterprise_agent_scope'))).toBeTruthy();
});

test('SD-5 两个独立浏览器上下文的旧 head 保存冲突且保留草稿', async ({ page, browser }) => {
  test.setTimeout(150_000);
  await login(page);
  const title = `SD5 浏览器竞争 ${Date.now()}`;
  const documentTitle = 'SD5 竞争正文';
  const created = await apiJson(page, '/api/enterprise/knowledge-bases', {
    method: 'POST',
    body: {
      tenant_id: TENANT_ID,
      name: title,
      description: '隔离正文竞争夹具',
    },
  });
  expect(created.status).toBe(200);
  const knowledgeBaseId = String(asRecord(created.body).id || '');
  expect(knowledgeBaseId).toBeTruthy();

  const initialSource = '# 竞争初始正文\n\n唯一旧事实：sd5-conflict-old。';
  const uploaded = await apiJson(page, '/api/enterprise/knowledge/documents', {
    method: 'POST',
    body: {
      tenant_id: TENANT_ID,
      knowledge_base_id: knowledgeBaseId,
      filename: 'sd5-conflict.md',
      title: documentTitle,
      content_base64: Buffer.from(initialSource, 'utf-8').toString('base64'),
    },
  });
  expect(uploaded.status).toBe(200);
  const initialJob = await waitForJob(page, String(asRecord(uploaded.body).id || ''));
  expect(initialJob.published_document_id).toBeTruthy();

  const conflictContext = await browser.newContext();
  const conflictPage = await conflictContext.newPage();
  try {
    await login(page);
    await login(conflictPage);
    await openKnowledgeDocumentEditor(page, documentTitle);
    await openKnowledgeDocumentEditor(conflictPage, documentTitle);

    const firstDraft = '# 第一位编辑者正文\n\n事实：sd5-conflict-first。';
    await page.getByRole('textbox', { name: '文档正文' }).fill(firstDraft);
    const firstResponse = page.waitForResponse((response) => (
      response.request().method() === 'PUT'
      && new URL(response.url()).pathname.includes('/api/enterprise/knowledge/documents/')
    ));
    await page.getByRole('button', { name: '保存正文', exact: true }).click();
    const firstResult = await firstResponse;
    expect(firstResult.status()).toBe(202);
    const firstJob = asRecord(await firstResult.json());
    await waitForJob(page, String(firstJob.job_id || ''));

    const secondDraft = '# 第二位编辑者草稿\n\n事实：sd5-conflict-second。';
    await conflictPage.getByRole('textbox', { name: '文档正文' }).fill(secondDraft);
    const conflictResponse = conflictPage.waitForResponse((response) => (
      response.request().method() === 'PUT'
      && new URL(response.url()).pathname.includes('/api/enterprise/knowledge/documents/')
    ));
    await conflictPage.getByRole('button', { name: '保存正文', exact: true }).click();
    expect((await conflictResponse).status()).toBe(409);
    await expect(conflictPage.getByRole('textbox', { name: '文档正文' })).toHaveValue(secondDraft);
    await expect(conflictPage.getByText('正文修订冲突，请重新读取当前正文后合并保存', { exact: true })).toBeVisible();
  } finally {
    await conflictContext.close();
  }
});

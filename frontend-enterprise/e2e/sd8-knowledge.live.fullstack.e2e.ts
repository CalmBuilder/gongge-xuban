/**
 * @Time       : 2026/10/03
 * @Author     : zhanglp8181
 * @File       : sd8-knowledge.live.fullstack.e2e.ts
 * @CallChain  : Chromium → KnowledgePage/Composer/工作项 → 实际发布及双 Runtime/SDK
 * @Description: 固定人工业务事实的双库真实模型闭环，账本/来源/引用与页面同时验收。
 */
import { expect, test, type Page, type TestInfo } from '@playwright/test';
import { observeAssertion } from './sd8-safe-observation';

const TENANT = 'tenant_demo';
const AGENT = 'agent_sd8_owner';
const VIEWER = 'agent_sd8_viewer';
const EMPTY = 'agent_sd8_empty';
const KB = 'SD8 CYPRESS42 政策库';
const TITLE = 'CYPRESS42 完整政策';
const NEW = '# CYPRESS42 完整政策\n\n## 额度\nCYPRESS42额度：1500元。\n\n## 截止\nCYPRESS42截止：17:30。';
const callBoundaries = new Map<string, Set<string>>();
type Facts = { old_amount: boolean; old_deadline: boolean; new_amount: boolean;
  new_deadline: boolean; explicit_no_answer: boolean; policy_number_present: boolean };
type Identity = { id: string; version_id: string; tenant_id: string; valid: boolean };
type Retrieval = { version_ids: string[]; outcome: string; degraded: boolean;
  identity_valid: boolean; evidence_facts: Facts; evidence_chunk_ids: string[];
  identities: Record<string, Identity[]>; result_hash: string };
type Instance = { id: string; session_id: string; kind: string; status: string;
  agent_id: string; initiator_user_id: string;
  current_result_id: string | null;
  plan_checksum: string; skill_version_id: string; frozen_knowledge_versions: string[] };
type Operation = { id: string; instance_id: string; name: string; status: string;
  retrieval: Retrieval[]; matched_event_ids: string[]; result_hash: string;
  event_match_count: number; source_user_message_id: string; node_execution_id: string };
type Citation = { id: string; kind: string; source_valid: boolean; source_ids: string[];
  version_ids: string[]; chunk_id?: string; concept_id?: string };
type Message = { id: string; session_id: string; role: string; content_hash: string;
  facts: Facts; citations: Citation[]; source_user_message_id: string;
  execution_id: string; execution_status: string; message_kind: string; matched_operation_ids: string[];
  result_id: string | null; result_status: string | null; result_binding_valid: boolean };
type Job = { id: string; status: string; document_id: string; candidate_version_id: string;
  published_version_id: string; published_document_id: string };
type Call = { call_id: string; operation: string; actual_provider_call: boolean;
  actual_sdk_transport: boolean; real_provider_quality: boolean; status: string;
  completed_model_outputs: Array<{ execution_id?: string; origins: string[];
    execution_context_hash: string; missing_output: boolean; has_knowledge_result: boolean;
    outcome?: string; sufficient?: boolean; evidence_facts?: Facts }> };
type State = { initial: { knowledge_base_id: string; old_version_id: string;
  old_document_id: string; initial_execution_count: number; initial_operation_count: number;
  old_source_hash: string; expected_new_source_hash: string; foreign_version_id: string };
  pid: number; instances: Instance[]; operations: Operation[]; messages: Message[]; jobs: Job[];
  planning_failures: Array<{ id: string; session_id: string; code: string | null }>;
  guidance_resources: Array<{ skill_id: string; revision_id: string; slug: string;
    usage_mode: string; status: string; content_checksum: string;
    markdown_hash: string; native_wait_contract: boolean }>;
  guidance_uses: Array<{ id: string; session_id: string; execution_id: string | null;
    agent_id: string; user_id: string; skill_id: string; revision_id: string;
    content_checksum: string; status: string }>;
  candidate: { job_id: string; candidate_version_id: string; status: string } | null;
  versions: Array<{ id: string; version: string; state: string; scope: string }>;
  branches: Array<{ agent_id: string; head_version: string; revision: number }>;
  documents: Array<{ id: string; version_id: string; source_hash: string; source_facts: Facts }>;
  sdk_calls: Call[] };

function record(value: unknown): Record<string, unknown> {
  /** 只在真实JSON对象边界收窄类型，拒绝把错误响应猜成成功。 */
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('JSON_OBJECT_REQUIRED');
  return value as Record<string, unknown>;
}

async function request(page: Page, path: string, method = 'GET', body?: Record<string, unknown>) {
  /** 正式API只用于初始化空会话或只读旁证；待验编辑/发送/续办均走UI。 */
  return page.evaluate(async ({ path, method, body }) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch(path, { method, headers: { 'Content-Type': 'application/json',
      Authorization: `Bearer ${auth.token || ''}` }, body: body ? JSON.stringify(body) : undefined });
    return { status: response.status, body: await response.json() as unknown };
  }, { path, method, body });
}

async function state(page: Page): Promise<State> {
  /** 读取隔离服务真实持久账本摘要，不输出模型正文。 */
  const result = await request(page, '/__e2e/sd8/state');
  expect(result.status).toBe(200);
  return result.body as State;
}

async function attach(info: TestInfo, criterion: string, values: object): Promise<void> {
  /** 每个通过或待验步骤即时留下安全原始事实，后续失败不能覆盖前序证据。 */
  await info.attach('sd8-safe-criterion', { body: Buffer.from(JSON.stringify({ criterion, ...values })),
    contentType: 'application/json' });
}

async function login(page: Page, viewer = false): Promise<void> {
  /** 只登录隔离合成账号，真实认证头留在浏览器不归档。 */
  await page.goto('/enterprise/dashboard');
  const status = await page.evaluate(async ({ viewer, agent }) => {
    const response = await fetch('/api/auth/login', { method: 'POST', headers: {
      'Content-Type': 'application/json' }, body: JSON.stringify({ tenant_id: 'tenant_demo',
      username: viewer ? 'sd8-viewer' : 'admin', password: viewer ? 'sd8-viewer' : 'admin' }) });
    if (response.ok) {
      localStorage.setItem('gongge_auth', JSON.stringify(await response.json()));
      localStorage.setItem('gongge_enterprise_agent_scope', agent);
      localStorage.setItem('gongge_onboarding_guide_seen', '1');
      localStorage.setItem('gongge_quick_start_guide_seen', '1');
    }
    return response.status;
  }, { viewer, agent: viewer ? VIEWER : AGENT });
  expect(status).toBe(200);
}

async function guidance(page: Page, info: TestInfo, name = 'sd8-knowledge-guidance'): Promise<void> {
  /** 页面导入的指导只要求实际检索和确认顺序，不含任何答案或政策数值。 */
  const markdown = `---\nname: ${name}\ndescription: 企业政策任务必须先用knowledge.search读取精确冻结版本，不凭常识作答。\nallowed-tools: knowledge.search\n---\n# 知识查询\n所有政策问题先检索 knowledge.search，再依据完整证据回答并引用来源。未知政策明确无资料，不编造。用户要求等待确认时先规划clarification步骤，使用wait_input动作。只有该步骤实际进入completed_steps后才表示本人确认已收到，再检索并完成回答；等待期间不提前完成。\n`;
  await page.goto('/enterprise/general-skills');
  await page.getByRole('button', { name: /新增/ }).click();
  await page.getByRole('menuitem', { name: '安全导入 Skill' }).click();
  const dialog = page.getByRole('dialog', { name: '安全导入 Skill 包' });
  await dialog.locator('input[type="file"]').setInputFiles({ name: 'SKILL.md',
    mimeType: 'text/markdown', buffer: Buffer.from(markdown) });
  await dialog.getByRole('button', { name: '生成安全预览' }).click();
  await expect(dialog.getByText(name, { exact: true })).toBeVisible();
  await dialog.getByRole('button', { name: '固定版本并绑定' }).click();
  await expect(dialog).not.toBeVisible();
  const revision = (await state(page)).guidance_resources.find(x => x.slug === name);
  if (!revision) throw new Error('ACTUAL_GUIDANCE_REVISION_MISSING');
  expect(revision.status).toBe('published');
  expect(revision.usage_mode).toBe('planning_guidance');
  expect(revision.content_checksum).toMatch(/^[a-f0-9]{64}$/);
  expect(revision.native_wait_contract).toBe(true);
  await attach(info, name + '-v2-frozen', { passed: true, revision });
}

async function send(page: Page, title: string, text: string, dynamic: boolean, agent = AGENT): Promise<string> {
  /** 新建真实空会话后从Composer发送，显式核验用户选择的引擎。 */
  const created = await request(page, '/api/chat/sessions', 'POST', {
    tenant_id: TENANT, agent_id: agent, title, origin: 'owned' });
  expect(created.status).toBe(200);
  expect(record(created.body).agent_id).toBe(agent);
  const session = String(record(created.body).id);
  await page.goto(`/workspace/chat/${session}`);
  const engine = page.getByRole('button', { name: '选择 DynamicTaskAgent 复杂任务引擎' });
  await expect(engine).toBeVisible({ timeout: 30_000 });
  if ((await engine.getAttribute('aria-pressed') === 'true') !== dynamic) await engine.click();
  callBoundaries.set(session, new Set((await state(page)).sdk_calls.map(x => x.call_id)));
  await page.getByPlaceholder('输入消息，按 Enter 发送...').fill(text);
  const response = page.waitForResponse(value => value.request().method() === 'POST'
    && new URL(value.url()).pathname === '/api/chat/stream');
  await page.getByRole('button', { name: '发送', exact: true }).click();
  const started = await response;
  expect(started.status()).toBe(200);
  const posted = record(started.request().postDataJSON());
  expect(posted.agent_id).toBe(agent);
  expect(posted.execution_engine === 'dynamic_task').toBe(dynamic);
  return session;
}

async function execution(page: Page, session: string, waiting = false): Promise<Instance> {
  /** 确认本会话真实实例到达期望状态；失败保留原例，不无限等待。 */
  await expect.poll(async () => {
    const current = await state(page);
    const failure = current.planning_failures.find(x => x.session_id === session);
    if (failure) throw new Error(`ACTUAL_PLANNING_FAILED:${failure.id}:${failure.code || 'unknown'}`);
    return current.instances.find(x => x.session_id === session)?.status;
  },
    { timeout: 300_000, intervals: [500, 1000, 2000] }).toMatch(waiting
      ? /^(waiting|succeeded|failed|cancelled|timed_out)$/
      : /^(succeeded|failed|cancelled|timed_out)$/);
  const instance = (await state(page)).instances.find(x => x.session_id === session);
  if (!instance) throw new Error('ACTUAL_EXECUTION_MISSING');
  expect(instance.status).toBe(waiting ? 'waiting' : 'succeeded');
  return instance;
}

async function citations(page: Page, facts: 'old' | 'new'): Promise<void> {
  /** 打开实际展示的每个引用，合并摘录检查两方面；授权另经正式资源API断言。 */
  const buttons = page.getByLabel('知识引用').last().getByRole('button');
  await expect(buttons.first()).toBeVisible({ timeout: 30_000 });
  let excerpts = '';
  for (let i = 0; i < await buttons.count(); i++) {
    await buttons.nth(i).click();
    const dialog = page.getByRole('dialog', { name: '引用详情' });
    await expect(dialog).toBeVisible();
    excerpts += await dialog.innerText();
    await dialog.getByRole('button', { name: 'Close', exact: true }).click();
    await expect(dialog).not.toBeVisible();
  }
  expect(excerpts).toMatch(facts === 'old' ? /600/ : /1500/);
  expect(excerpts).toMatch(facts === 'old' ? /16:00/ : /17:30/);
}

async function answer(page: Page, info: TestInfo, criterion: string, session: string,
  kind: string, version: string, facts: 'old' | 'new' | 'none', degraded = false): Promise<Instance> {
  /** 联合实际Runtime、Operation、四层来源、证据、回答和引用，不只看答案文字。 */
  const instance = await execution(page, session);
  expect(instance.kind).toBe(kind);
  if (kind === 'sop') expect(instance.skill_version_id).toBeTruthy();
  else expect(instance.frozen_knowledge_versions).toContain(version);
  await expect.poll(async () => (await state(page)).messages.filter(x => x.session_id === session
    && x.role === 'assistant' && (kind === 'sop' ? x.matched_operation_ids.length > 0
      : x.execution_id === instance.id && x.result_id === instance.current_result_id
        && x.result_status === 'verified' && x.result_binding_valid)).length,
  { timeout: 60_000 }).toBeGreaterThan(0);
  const value = await state(page);
  if (kind === 'dynamic_task') {
    const slug = instance.agent_id === VIEWER ? 'sd8-viewer-knowledge-guidance' : 'sd8-knowledge-guidance';
    const resource = value.guidance_resources.find(x => x.slug === slug);
    if (!resource) throw new Error('ACTUAL_GUIDANCE_SOURCE_MISSING');
    expect(value.guidance_uses.some(x => x.session_id === session && x.execution_id === instance.id
      && x.agent_id === instance.agent_id && x.user_id === instance.initiator_user_id
      && x.skill_id === resource.skill_id && x.revision_id === resource.revision_id
      && x.content_checksum === resource.content_checksum && x.status === 'completed')).toBe(true);
  }
  const operations = value.operations.filter(x => x.instance_id === instance.id && x.name === 'knowledge.search');
  expect(operations.length).toBeGreaterThan(0);
  expect(operations.every(x => x.status === 'succeeded')).toBe(true);
  if (kind === 'sop') expect(operations.every(x => x.event_match_count === 1)).toBe(true);
  const retrieval = operations.flatMap(x => x.retrieval);
  expect(retrieval.length).toBeGreaterThan(0);
  expect(retrieval.every(x => x.version_ids.length === 1 && x.version_ids[0] === version
    && x.identity_valid)).toBe(true);
  const messages = value.messages.filter(x => x.session_id === session && x.role === 'assistant');
  const reply = messages[messages.length - 1];
  if (!reply) throw new Error('ACTUAL_ASSISTANT_MISSING');
  if (kind === 'sop') {
    expect(reply.source_user_message_id).toBe(operations[0]?.source_user_message_id);
    expect(operations.every(x => reply.matched_operation_ids.includes(x.id))).toBe(true);
  } else {
    expect(reply.execution_id).toBe(instance.id);
    expect(reply.result_id).toBe(instance.current_result_id);
    expect(reply.result_status).toBe('verified');
    expect(reply.result_binding_valid).toBe(true);
  }
  const before = callBoundaries.get(session);
  if (!before) throw new Error('SDK_WINDOW_START_MISSING');
  const calls = value.sdk_calls.filter(x => !before.has(x.call_id));
  await attach(info, criterion + '-raw', { instance, operations, messages,
    guidance_resources: value.guidance_resources,
    guidance_uses: value.guidance_uses.filter(x => x.execution_id === instance.id),
    sdk_window_start_excluded_ids: [...before], sdk_window_calls: calls });
  const knowledgeCalls = calls.filter(x => x.operation.startsWith('knowledge.'));
  expect(knowledgeCalls.some(x => x.operation === 'knowledge.document_route' && x.status === 'completed'
    && x.actual_sdk_transport && x.actual_provider_call === !degraded)).toBe(true);
  if (facts !== 'none') expect(knowledgeCalls.some(x => x.operation === 'knowledge.bucket_route'
    && x.status === 'completed' && x.actual_provider_call === !degraded)).toBe(true);
  expect(calls.some(x => x.actual_provider_call && x.status === 'completed'
    && (kind === 'sop' ? x.operation.startsWith('response.generate')
      : x.operation === 'dynamic_task.answer'))).toBe(true);
  if (kind === 'dynamic_task') {
    const modelOutputs = calls.filter(x => x.operation === 'dynamic_task.answer'
      && x.actual_provider_call && x.status === 'completed').flatMap(x => x.completed_model_outputs)
      .filter(x => x.execution_id === instance.id && x.origins.includes('execution_context')
        && !x.missing_output && x.has_knowledge_result);
    expect(modelOutputs.length).toBeGreaterThan(0);
    expect(modelOutputs.some(x => facts === 'none'
      ? ['no_match', 'insufficient'].includes(x.outcome || '') && x.sufficient === false
      : x.outcome === 'evidence_found' && x.sufficient === true
        && x.evidence_facts?.[`${facts}_amount`] && x.evidence_facts?.[`${facts}_deadline`])).toBe(true);
  }
  if (facts === 'none') {
    expect(retrieval.every(x => ['no_match', 'insufficient'].includes(x.outcome))).toBe(true);
    expect(reply.facts.explicit_no_answer).toBe(true);
    expect(reply.facts.old_amount || reply.facts.new_amount).toBe(false);
    expect(reply.facts.policy_number_present).toBe(false);
    expect(reply.citations).toHaveLength(0);
    await expect(page.getByLabel('知识引用')).toHaveCount(0);
  } else {
    expect(retrieval.some(x => x.outcome === 'evidence_found' && x.degraded === degraded)).toBe(true);
    expect(retrieval.some(x => x.evidence_facts[`${facts}_amount`]
      && x.evidence_facts[`${facts}_deadline`])).toBe(true);
    expect(reply.facts[`${facts}_amount`] && reply.facts[`${facts}_deadline`]).toBe(true);
    expect(reply.citations.length).toBeGreaterThan(0);
    expect(reply.citations.every(x => x.source_valid && x.version_ids.includes(version))).toBe(true);
    await page.goto(`/workspace/chat/${session}`);
    await citations(page, facts);
  }
  await attach(info, criterion, { passed: true, instance_id: instance.id, session_id: session,
    version_id: version, kind, operation_ids: operations.map(x => x.id),
    answer_message_id: reply.id, answer_hash: reply.content_hash, facts: reply.facts,
    citation_sources: reply.citations, degraded });
  return instance;
}

async function editor(page: Page): Promise<void> {
  /** 当前员工范围打开真实全文编辑器，不直接API写入待验正文。 */
  await page.goto('/enterprise/knowledge');
  await expect(page.getByText(KB, { exact: true }).last()).toBeVisible({ timeout: 30_000 });
  await page.getByText(KB, { exact: true }).last().click();
  await expect(page.getByText(TITLE, { exact: true }).last()).toBeVisible();
  await page.getByRole('button', { name: '详情', exact: true }).last().click();
  await page.getByRole('dialog', { name: '文档详情' }).getByRole('button', { name: '修改', exact: true }).click();
  await expect(page.getByRole('textbox', { name: '文档正文' })).toBeVisible();
}

async function search(page: Page, kb: string, version: string, agent = AGENT) {
  /** 正式HTTP显式版本求交，不把E2E内部Service调用当用户授权证明。 */
  return request(page, '/api/enterprise/knowledge/search', 'POST', { tenant_id: TENANT,
    agent_id: agent, query: 'CYPRESS42额度和截止', knowledge_base_ids: [kb],
    knowledge_base_version_ids: [version] });
}

function expectNoEvidence(body: unknown): void {
  /** 负向必须四层、证据与原概念引用全空，不能只看chunks漏掉概念路径。 */
  const value = record(body);
  for (const key of ['selected_documents', 'selected_concepts', 'selected_buckets', 'chunks',
    'evidence_pack', 'okf_citations']) expect(value[key]).toEqual([]);
}

test('SD8 G3 实际旧知识→正文发布→双消费者及旧冻结/权限/无答案/协议降级', async ({ page, browser }, info) => {
  test.setTimeout(2_400_000);
  page.setDefaultTimeout(30_000);
  await login(page);
  const initial = (await state(page)).initial;
  expect(initial.initial_execution_count).toBe(0);
  expect(initial.initial_operation_count).toBe(0);
  await guidance(page, info);
  const oldSession = await send(page, 'SD8 old SOP',
    '请执行CYPRESS42政策查询SOP，读取CYPRESS42政策的额度和截止时间，两个方面都回答并引用。', false);
  await answer(page, info, 'old-sop', oldSession, 'sop', initial.old_version_id, 'old');
  let viewerContext = await browser.newContext();
  let viewerPage = await viewerContext.newPage();
  await login(viewerPage, true);
  await guidance(viewerPage, info, 'sd8-viewer-knowledge-guidance');
  const frozenSession = await send(viewerPage, 'SD8 frozen waiting DTA',
    '使用sd8-viewer-knowledge-guidance查询CYPRESS42政策额度和截止时间。在检索前必须先问我是否确认并等待我的回复；现在不要检索和回答，不要直接完成。收到确认后再knowledge.search并回答两个方面和引用。', true, VIEWER);
  const frozen = await execution(viewerPage, frozenSession, true);
  expect(frozen.kind).toBe('dynamic_task');
  expect(frozen.agent_id).toBe(VIEWER);
  expect(frozen.initiator_user_id).toBe('sd8_viewer');
  expect(frozen.frozen_knowledge_versions).toEqual([initial.old_version_id]);
  const waitingOperations = (await state(page)).operations.filter(x => x.instance_id === frozen.id
    && x.name === 'knowledge.search');
  expect(waitingOperations).toEqual([]);
  await attach(info, 'waiting-created', { passed: true, instance: frozen, knowledge_operations: waitingOperations });

  await request(page, '/__e2e/sd8/control/hold-on', 'POST');
  await editor(page);
  await expect(page.getByRole('textbox', { name: '文档正文' })).toHaveValue(/600/);
  await page.getByRole('textbox', { name: '文档正文' }).fill(NEW);
  const revisionResponse = page.waitForResponse(x => x.request().method() === 'PUT'
    && new URL(x.url()).pathname.endsWith('/' + initial.old_document_id));
  await page.getByRole('button', { name: '保存正文', exact: true }).click();
  expect((await revisionResponse).status()).toBe(202);
  await expect.poll(async () => (await state(page)).candidate?.status, { timeout: 30_000 }).toBe('ready_to_publish');
  const candidate = (await state(page)).candidate;
  if (!candidate) throw new Error('BUILT_CANDIDATE_MISSING');
  const hidden = await search(page, initial.knowledge_base_id, candidate.candidate_version_id);
  expect(hidden.status).toBe(200);
  expectNoEvidence(hidden.body);
  await attach(info, 'candidate-hidden', { passed: true, candidate, status: hidden.status });
  await request(page, '/__e2e/sd8/control/release', 'POST');
  await expect.poll(async () => (await state(page)).jobs.find(x => x.id === candidate.job_id)?.status,
    { timeout: 60_000 }).toBe('completed');
  const published = (await state(page)).jobs.find(x => x.id === candidate.job_id);
  if (!published) throw new Error('ACTUAL_PUBLISHED_JOB_MISSING');
  const newVersion = published.published_version_id;
  expect(newVersion).not.toBe(initial.old_version_id);
  const content = await request(page, `/api/enterprise/knowledge/documents/${published.published_document_id}/content?tenant_id=${TENANT}&agent_id=${AGENT}`);
  expect(content.status).toBe(200);
  expect(record(content.body).content_md).toBe(NEW);
  const publishedState = await state(page);
  const viewerHead = publishedState.branches.find(x => x.agent_id === VIEWER)?.head_version;
  expect(publishedState.versions.find(x => x.id === initial.old_version_id)?.version).toBe(viewerHead);
  expect(publishedState.documents.find(x => x.id === published.published_document_id)?.source_hash)
    .toBe(initial.expected_new_source_hash);
  await attach(info, 'full-body-published', { passed: true, job: published,
    documents: publishedState.documents, branches: publishedState.branches, versions: publishedState.versions });

  await viewerContext.close();
  const disconnected = (await state(page)).instances.find(x => x.id === frozen.id);
  expect(disconnected?.status).toBe('waiting');
  expect(disconnected?.agent_id).toBe(frozen.agent_id);
  expect(disconnected?.initiator_user_id).toBe(frozen.initiator_user_id);
  expect(disconnected?.plan_checksum).toBe(frozen.plan_checksum);
  expect(disconnected?.frozen_knowledge_versions).toEqual(frozen.frozen_knowledge_versions);
  expect((await state(page)).operations.filter(x => x.instance_id === frozen.id
    && x.name === 'knowledge.search')).toEqual([]);
  await attach(info, 'client-disconnected-waiting', { passed: true, instance: disconnected,
    knowledge_operations: [] });
  const oldPid = publishedState.pid;
  await request(page, '/__e2e/sd8/control/restart', 'POST');
  await expect.poll(async () => {
    try { return (await state(page)).pid !== oldPid; } catch { return false; }
  }, { timeout: 120_000 }).toBe(true);
  const restored = (await state(page)).instances.find(x => x.id === frozen.id);
  expect(restored?.plan_checksum).toBe(frozen.plan_checksum);
  expect(restored?.frozen_knowledge_versions).toEqual(frozen.frozen_knowledge_versions);
  viewerContext = await browser.newContext();
  viewerPage = await viewerContext.newPage();
  await login(viewerPage, true);
  await viewerPage.goto('/enterprise/work-items');
  const inbox = await request(viewerPage, '/api/attention-items?tenant_id=tenant_demo&view=active&page=1&page_size=100');
  const items = record(inbox.body).items;
  if (!Array.isArray(items)) throw new Error('ATTENTION_ITEMS_REQUIRED');
  const item = items.map(record).find(x => x.execution_id === frozen.id);
  if (!item) throw new Error('ORIGINAL_CLARIFICATION_MISSING');
  await viewerPage.getByRole('button', { name: String(item.title), exact: false }).first().click();
  const dialog = viewerPage.getByRole('dialog');
  await expect(dialog).toContainText(frozen.id);
  await dialog.getByRole('textbox', { name: '补充信息' }).fill('确认允许检索。请按本次原任务冻结的知识版本查询CYPRESS42额度和截止时间并完整引用。');
  callBoundaries.set(frozenSession, new Set((await state(viewerPage)).sdk_calls.map(x => x.call_id)));
  const submitted = viewerPage.waitForResponse(value => value.request().method() === 'POST'
    && new URL(value.url()).pathname === `/api/attention-items/${String(item.id)}/resolve`);
  await dialog.getByRole('button', { name: '补充并继续' }).click();
  expect((await submitted).status()).toBe(200);
  await answer(viewerPage, info, 'restarted-old-dta', frozenSession, 'dynamic_task', initial.old_version_id, 'old');
  const newSop = await send(page, 'SD8 new SOP',
    '请执行CYPRESS42政策查询SOP，检索并完整回答CYPRESS42额度和截止时间，引用实际来源。', false);
  await answer(page, info, 'new-sop', newSop, 'sop', newVersion, 'new');
  const newDta = await send(page, 'SD8 new DTA',
    '使用sd8-knowledge-guidance，必须实际knowledge.search读取CYPRESS42政策，完整回答额度和截止时间并引用；不凭常识回答。', true);
  await answer(page, info, 'new-dta', newDta, 'dynamic_task', newVersion, 'new');
  const unknownSop = await send(page, 'SD8 unknown SOP',
    '执行ZIRCON999UNKNOWN未知政策SOP，请实际检索ZIRCON999UNKNOWN政策，无相应证据请明确未找到资料，不引用其他政策或编造数值。', false);
  await answer(page, info, 'unknown-sop', unknownSop, 'sop', newVersion, 'none');
  const unknownDta = await send(page, 'SD8 unknown DTA',
    '使用sd8-knowledge-guidance，必须knowledge.search查询ZIRCON999UNKNOWN。没有该政策资料就明确无答案；不使用其他政策，不编造额度、时间或引用。', true);
  await answer(page, info, 'unknown-dta', unknownDta, 'dynamic_task', newVersion, 'none');

  await request(page, '/__e2e/sd8/control/protocol-on', 'POST');
  const fallbackSop = await send(page, 'SD8 protocol fallback SOP',
    '执行CYPRESS42政策查询SOP，完整查询额度和截止时间并引用。', false);
  await answer(page, info, 'sdk-http-protocol-fallback', fallbackSop, 'sop', newVersion, 'new', true);
  const fallbackDta = await send(page, 'SD8 protocol fallback DTA',
    '使用sd8-knowledge-guidance，必须实际knowledge.search读取CYPRESS42政策，完整回答额度和截止时间并引用；不凭常识回答。', true);
  await answer(page, info, 'sdk-http-protocol-fallback-dta', fallbackDta, 'dynamic_task', newVersion, 'new', true);
  await request(page, '/__e2e/sd8/control/protocol-off', 'POST');
  expect((await state(page)).sdk_calls.some(x => !x.actual_provider_call && x.actual_sdk_transport
    && !x.real_provider_quality && x.operation.startsWith('knowledge.'))).toBe(true);

  await editor(page);
  await request(page, '/__e2e/sd8/control/build-failure', 'POST');
  await page.getByRole('textbox', { name: '文档正文' }).fill(NEW + '\n\n受控失败修订。');
  await page.getByRole('button', { name: '保存正文', exact: true }).click();
  await expect.poll(async () => (await state(page)).jobs.some(x => x.status === 'failed'),
    { timeout: 30_000 }).toBe(true);
  expect((await state(page)).branches).toEqual(publishedState.branches);
  await attach(info, 'build-failure-preserves-heads', { passed: true, jobs: (await state(page)).jobs });

  const oldViewer = await request(viewerPage, `/api/enterprise/knowledge/documents/${initial.old_document_id}/content?tenant_id=${TENANT}&agent_id=${VIEWER}`);
  expect(oldViewer.status).toBe(200);
  expect(String(record(oldViewer.body).content_md)).toContain('600');
  const viewerBeforeSearch = await search(viewerPage, initial.knowledge_base_id, initial.old_version_id, VIEWER);
  expect(viewerBeforeSearch.status).toBe(200);
  expect((record(viewerBeforeSearch.body).chunks as unknown[]).length).toBeGreaterThan(0);
  const viewerInvisibleNew = await search(viewerPage, initial.knowledge_base_id, newVersion, VIEWER);
  expect(viewerInvisibleNew.status).toBe(200);
  expectNoEvidence(viewerInvisibleNew.body);
  await viewerPage.goto('/enterprise/knowledge');
  await expect(viewerPage.getByText(KB, { exact: true }).last()).toBeVisible();
  await viewerPage.getByText(KB, { exact: true }).last().click();
  await viewerPage.getByRole('button', { name: '详情', exact: true }).last().click();
  await expect(viewerPage.getByRole('dialog', { name: '文档详情' })).toContainText('600');
  await page.goto('/enterprise/knowledge');
  await expect(page.getByLabel('切换当前员工', { exact: true })).toContainText('SD8 知识员工');
  await page.getByPlaceholder('搜索知识库名称、描述、状态或版本').fill(KB);
  await expect(page.getByRole('row').filter({ has: page.getByText(KB, { exact: true }) }))
    .toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole('button', { name: '刷新', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: '平台知识治理', exact: true }).click();
  await expect(page.getByText('知识库广场列表', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '刷新', exact: true })).toBeEnabled();
  await page.getByPlaceholder('搜索知识库名称、描述、状态或版本').fill(KB);
  const governanceRow = page.getByRole('row').filter({ has: page.getByText(KB, { exact: true }) });
  await observeAssertion(() => expect(governanceRow).toBeVisible({ timeout: 30_000 }), async () => {
    const listed = await request(page, '/api/enterprise/knowledge-bases?tenant_id=tenant_demo&governance_view=true');
    await attach(info, 'governance-target-ui-state', {
      status: listed.status,
      expected_kb_listed: Array.isArray(listed.body)
        && listed.body.some(x => record(x).id === initial.knowledge_base_id),
      platform_caption_visible: await page.getByText('知识库广场列表', { exact: true }).isVisible(),
      target_rows: await governanceRow.count(),
      search_is_fixed_kb: await page.getByPlaceholder('搜索知识库名称、描述、状态或版本').inputValue() === KB,
      platform_scope_empty: await page.evaluate(() => !localStorage.getItem('gongge_enterprise_agent_scope')),
    });
  });
  await governanceRow.getByRole('button', { name: '知识库操作', exact: true }).click();
  await page.getByRole('menuitem', { name: '访问治理', exact: true }).click();
  await page.getByRole('combobox', { name: '知识访问范围' }).click();
  await page.getByRole('option', { name: '仅所有者', exact: true }).click();
  await page.getByRole('button', { name: '保存治理范围', exact: true }).click();
  await expect(page.getByRole('combobox', { name: '知识访问范围' })).not.toBeVisible();
  await viewerPage.reload();
  await expect(viewerPage.getByRole('dialog', { name: '文档详情' })).not.toBeVisible();
  const denied = await request(viewerPage, `/api/enterprise/knowledge/documents/${initial.old_document_id}/content?tenant_id=${TENANT}&agent_id=${VIEWER}`);
  expect([403, 404]).toContain(denied.status);
  const viewerAfterSearch = await search(viewerPage, initial.knowledge_base_id, initial.old_version_id, VIEWER);
  expect(viewerAfterSearch.status).toBe(200);
  expectNoEvidence(viewerAfterSearch.body);
  await page.getByLabel('切换当前员工', { exact: true }).click();
  await page.getByRole('menuitem', { name: /SD8 知识员工/ }).click();
  await page.goto('/enterprise/knowledge');
  await expect(page.getByRole('button', { name: '平台知识治理', exact: true })).toBeVisible();
  const invisibleOld = await search(page, initial.knowledge_base_id, initial.old_version_id);
  expect(invisibleOld.status).toBe(200);
  expectNoEvidence(invisibleOld.body);
  const foreign = await search(page, initial.knowledge_base_id, initial.foreign_version_id);
  expect(foreign.status).toBe(200);
  expectNoEvidence(foreign.body);
  await page.getByLabel('切换当前员工', { exact: true }).click();
  await page.getByRole('menuitem', { name: /SD8 无知识员工/ }).click();
  await expect.poll(() => page.evaluate(() => localStorage.getItem('gongge_enterprise_agent_scope')))
    .toBe(EMPTY);
  await page.goto('/enterprise/knowledge');
  await page.getByRole('button', { name: '刷新', exact: true }).click();
  await expect(page.getByText(KB, { exact: true })).toHaveCount(0);
  await expect(page.getByRole('textbox', { name: '文档正文' })).not.toBeVisible();
  await expect(page.getByText(TITLE, { exact: true })).toHaveCount(0);
  const emptyContent = await request(page, `/api/enterprise/knowledge/documents/${initial.old_document_id}/content?tenant_id=${TENANT}&agent_id=${EMPTY}`);
  expect([403, 404]).toContain(emptyContent.status);
  const emptySearch = await search(page, initial.knowledge_base_id, newVersion, EMPTY);
  expect(emptySearch.status).toBe(200);
  expectNoEvidence(emptySearch.body);
  await attach(info, 'current-permissions-and-scope', { passed: true, viewer_old_access_before: oldViewer.status,
    viewer_old_access_after: denied.status, invisible_old_empty: true, foreign_empty: true,
    viewer_old_search_before_allowed: true, viewer_old_search_after_all_layers_empty: true,
    viewer_new_explicit_version_all_layers_empty: true,
    unbound_scope_body_empty: true, unbound_source_status: emptyContent.status,
    unbound_search_all_layers_empty: true, historical_citation_is_not_current_authorization: true });
  await viewerContext.close();
  const final = await state(page);
  await attach(info, 'final-ledger', { passed: true, initial: final.initial, versions: final.versions,
    documents: final.documents, branches: final.branches, instances: final.instances,
    operations: final.operations, messages: final.messages, jobs: final.jobs,
    real_provider_calls: final.sdk_calls.filter(x => x.actual_provider_call).map(x => x.call_id),
    controlled_protocol_calls: final.sdk_calls.filter(x => !x.actual_provider_call).map(x => x.call_id) });
});

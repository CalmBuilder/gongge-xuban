/**
 * @Time       : 2026/10/01
 * @Author     : zhanglp8181
 * @File       : sd6a-memory-projection.live.fullstack.e2e.ts
 * @CallChain  : 真实Chromium Composer → SOP/DTA → 实际供应商SDK → 安全选择/usage证据
 * @Description: 固定标注用例、管理全文、冻结恢复和清空/撤权；不使用模型替身。
 */
import { expect, test, type Page, type TestInfo } from '@playwright/test';
import { readFile, writeFile, access, rm } from 'node:fs/promises';
import { join } from 'node:path';

const CONTROL = process.env.FULLSTACK_SD6B_CONTROL_DIR || '';
const modes = new WeakMap<Page, { dynamic: boolean; agentId: string }>();
test.skip(process.env.LIVE_ATTACHMENT_E2E !== '1' || process.env.FULLSTACK_SD6A !== '1',
  'SD6A requires isolated real-provider settings');
test.setTimeout(600_000);

type Selected = { id: string; hash: string; currently_allowed: boolean };
type Execution = { id: string; kind: string; status: string; plan_checksum: string; selected: Selected[];
  slots: Record<string, string | null> };
type State = {
  executions: Execution[];
  sessions: Array<{ id: string; slots: Record<string, string | null>; router_audit: unknown[] }>;
  records: Array<{ id: string; characters: number; source_sequence: number; hash: string;
    source_input_id: string | null }>;
};
type SDKFact = {
  actual_provider_call: boolean; operation: string; source_input_id: string; status: string;
  selected: Array<{ id: string; hash: string; complete_in_sdk: boolean; occurrences: number }>;
  request_estimate: number; usage: { prompt_tokens: number | null }; sidecar_absent: boolean;
  revoked_fact_markers_absent: boolean;
};
type Message = { id: string; role: string; content: string };
type Turn = { sessionId: string; sourceId: string };

async function api<T>(page: Page, path: string): Promise<T> {
  /** 使用当前浏览器认证作只读旁证，认证状态不写报告。 */
  return page.evaluate(async (path) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const result = await fetch(path, { headers: { Authorization: `Bearer ${auth.token || ''}` } });
    if (!result.ok) throw new Error(`Read status ${result.status}`);
    return await result.json();
  }, path);
}

async function state(page: Page, agentId: string): Promise<State> {
  return api<State>(page, `/__e2e/sd6a/state/${agentId}`);
}

async function sdkFacts(): Promise<SDKFact[]> {
  /** 原始安全SDK观察由实际传输包装写入，无请求/回复正文。 */
  const text = await readFile(join(CONTROL, 'sdk-observations.safe.jsonl'), 'utf8').catch(() => '');
  return text.trim().split('\n').filter(Boolean).map((line: string) => JSON.parse(line) as SDKFact);
}

async function login(page: Page, agentId: string): Promise<void> {
  await page.goto('/enterprise/dashboard');
  const status = await page.evaluate(async (agentId) => {
    const response = await fetch('/api/auth/login', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tenant_id: 'tenant_demo', username: 'member', password: 'member' }) });
    const body = await response.json() as Record<string, unknown>;
    if (response.ok) {
      localStorage.setItem('gongge_auth', JSON.stringify(body));
      localStorage.setItem('gongge_onboarding_guide_seen', '1');
      localStorage.setItem('gongge_quick_start_guide_seen', '1');
      localStorage.setItem('gongge_enterprise_agent_scope', agentId);
    }
    return response.status;
  }, agentId);
  expect(status).toBe(200);
}

async function draft(page: Page, agentId: string, dynamic: boolean): Promise<void> {
  await page.goto(`/workspace/chat/draft/${agentId}`);
  const [branch, caseName] = agentId.replace('agent_sd6a_', '').split('_');
  await expect(page.getByRole('button', { name: '筛选会话' }))
    .toContainText(`SD6A ${branch.toUpperCase()} ${caseName}`, { timeout: 30_000 });
  modes.set(page, { dynamic, agentId });
  const engine = page.getByRole('button', { name: '选择 DynamicTaskAgent 复杂任务引擎' });
  await expect(engine).toBeVisible({ timeout: 30_000 });
  if ((await engine.getAttribute('aria-pressed') === 'true') !== dynamic) await engine.click();
  await expect(engine).toHaveAttribute('aria-pressed', String(dynamic));
}

async function send(page: Page, text: string, sessionId = '', waiting = false): Promise<Turn> {
  /** 业务输入只能走真实Composer，源身份从正式消息账本精确读取。 */
  const composer = page.getByPlaceholder('输入消息，按 Enter 发送...');
  await expect(composer).toBeVisible({ timeout: 30_000 });
  await composer.fill(text);
  const mode = modes.get(page);
  if (!mode) throw new Error('Explicit engine selection missing');
  const engine = page.getByRole('button', { name: '选择 DynamicTaskAgent 复杂任务引擎' });
  if ((await engine.getAttribute('aria-pressed') === 'true') !== mode.dynamic) await engine.click();
  await expect(engine).toHaveAttribute('aria-pressed', String(mode.dynamic));
  await expect(page.getByRole('button', { name: '发送', exact: true })).toBeEnabled({ timeout: 120_000 });
  const response = page.waitForResponse((value) => value.request().method() === 'POST'
    && new URL(value.url()).pathname === '/api/chat/stream');
  await page.getByRole('button', { name: '发送', exact: true }).click();
  const accepted = await response;
  expect(accepted.status()).toBe(200);
  const posted: unknown = accepted.request().postDataJSON();
  const forced = !!posted && typeof posted === 'object' && 'execution_engine' in posted
    && posted.execution_engine === 'dynamic_task';
  expect(forced).toBe(mode.dynamic);
  if (!sessionId && waiting) {
    // 等待态不以成功complete提升草稿；身份从正式账本取得，不伪造终态或消息。
    await expect.poll(async () => {
      for (const session of (await state(page, mode.agentId)).sessions) {
        const rows = await api<Message[]>(page, `/api/chat/sessions/${session.id}/messages?tenant_id=tenant_demo`);
        if (rows.some((row) => row.role === 'user' && row.content === text)) {
          sessionId = session.id;
          break;
        }
      }
      return sessionId;
    }, { timeout: 120_000 }).not.toBe('');
  } else if (!sessionId) {
    await expect(page).toHaveURL(/\/workspace\/chat\/session_[^/]+$/, { timeout: 60_000 });
    sessionId = new URL(page.url()).pathname.split('/').pop() || '';
  }
  let sourceId = '';
  await expect.poll(async () => {
    const messages = await api<Message[]>(page, `/api/chat/sessions/${sessionId}/messages?tenant_id=tenant_demo`);
    sourceId = messages.find((value) => value.role === 'user' && value.content === text)?.id || '';
    return sourceId;
  }, { timeout: 120_000 }).not.toBe('');
  return { sessionId, sourceId };
}

async function finished(page: Page, agentId: string, turn: Turn, kind: string): Promise<State> {
  await expect.poll(async () => {
    const value = await state(page, agentId);
    return value.executions.some((execution) => execution.kind === kind && execution.status === 'succeeded');
  }, { timeout: 300_000, intervals: [500, 1000, 2000] }).toBe(true);
  await expect.poll(async () => {
    const rows = await api<Message[]>(page, `/api/chat/sessions/${turn.sessionId}/messages?tenant_id=tenant_demo`);
    return rows.some((value) => value.role === 'assistant');
  }, { timeout: 60_000 }).toBe(true);
  return state(page, agentId);
}

async function confirmWaiting(page: Page, executionId: string, text: string): Promise<void> {
  /** 沿正式澄清页恢复原执行；等待轮尚未闭合时不把聊天队列当作已提交确认。 */
  await page.goto('/enterprise/work-items');
  const inbox = await api<{ items: Array<{ id: string; execution_id: string; title: string }> }>(
    page, '/api/attention-items?tenant_id=tenant_demo&view=active&page=1&page_size=100');
  const item = inbox.items.find((value) => value.execution_id === executionId);
  if (!item) throw new Error('Original execution clarification missing');
  await page.getByRole('button', { name: item.title, exact: false }).first().click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toContainText(executionId);
  await dialog.getByRole('textbox', { name: '补充信息' }).fill(text);
  const submitted = page.waitForResponse((value) => value.request().method() === 'POST'
    && new URL(value.url()).pathname === `/api/attention-items/${item.id}/resolve`, { timeout: 30_000 });
  await dialog.getByRole('button', { name: '补充并继续' }).click();
  expect((await submitted).status()).toBe(200);
}

async function waitingExecution(page: Page, agentId: string): Promise<Execution> {
  /** 原执行必须真实等待；模型已终止时立即判失败，不空等五分钟。 */
  await expect.poll(async () => (await state(page, agentId)).executions.at(-1)?.status || '',
    { timeout: 300_000 }).toMatch(/^(waiting|succeeded|failed|cancelled)$/);
  const execution = (await state(page, agentId)).executions.at(-1);
  if (!execution) throw new Error('Original execution missing');
  expect(execution.status).toBe('waiting');
  return execution;
}

async function managementVisible(page: Page, agentId: string): Promise<void> {
  /** 未选大事实仍在实际记忆页面，完整领域读取不会跟随模型预算裁剪。 */
  await page.evaluate((agentId) => localStorage.setItem('gongge_enterprise_agent_scope', agentId), agentId);
  await page.goto('/enterprise/memories');
  await expect(page.getByRole('tab', { name: '记忆' })).toHaveAttribute('data-state', 'active');
  const complete = page.getByRole('table', { name: '员工记忆' }).getByText('口令：SD6A-UNSELECTED-SECRET。',
    { exact: false }).first();
  await expect(complete).toBeVisible({ timeout: 30_000 });
  expect((await complete.textContent())?.length).toBeGreaterThan(4000);
  const value = await state(page, agentId);
  expect(value.records.find((record) => record.id.endsWith('_large'))?.characters).toBeGreaterThan(4000);
}

function question(branch: string, number: string, control = false): string {
  /** 运行前固定的标注问题；预期姓名/原偏好不会写入问题提示。 */
  const marker = `SD6A-${branch.toUpperCase()}-${number}`;
  const correction = number === '2' ? '本轮纠正：输出偏好改为详细表格，当前纠正优先。' : '';
  const common = `${marker} ${correction}仅沿本次实际长期记忆使用我的姓名与输出格式，格式只填关键词；`
    + '不得从账号资料或其他会话推断，不得猜未投影的大条目口令；不知道的口令填写未知。';
  if (branch === 'sop') return common + '请办理完整记忆核验流程，填写 user_name、reply_style、omitted_code。'
    + (control ? '不知道姓名请不要填写 user_name，让平台按原完整领域事实补全；不知道格式填未知。' : '');
  return common + '请直接形成结论，必须逐行写 NAME=姓名、STYLE=格式关键词、CODE=口令；资料未知时字段值写未知。';
}

function labeledFields(markdown: string): Record<string, string> {
  /** 核对完整字段值，支持同行明确字段分隔；冲突和字段内附加说明仍失败。 */
  const matches = [
    ...markdown.matchAll(/\b(NAME|STYLE|CODE)\b[\s*`]*=[\s*`]*([^\r\n]+?)(?=(?:\s+(?:与|and)\s+|[，,、；;]\s*|\s+)[*`]*(?:NAME|STYLE|CODE)[\s*`]*=|[\r\n]|$)/g),
    ...markdown.matchAll(/^\s*\|[\s*`]*(NAME|STYLE|CODE)[\s*`]*\|[\s*`]*([^|\r\n]+)\|/gm),
  ];
  const fields: Record<string, string> = {};
  for (const match of matches) {
    const value = match[2].trim().replace(/[\s*`]+$/, '');
    fields[match[1]] = fields[match[1]] && fields[match[1]] !== value
      ? '__CONFLICTING_LABEL_VALUES__' : value;
  }
  return fields;
}

async function attach(info: TestInfo, data: object): Promise<void> {
  await info.attach('sd6a-safe-labeled-result', { body: Buffer.from(JSON.stringify(data, null, 2)),
    contentType: 'application/json' });
}

for (const branch of ['sop', 'dta']) {
  for (const number of ['1', '2', '3']) {
    test(`MB-LIVE ${branch.toUpperCase()}-${number}: actual model, complete facts and independent labels`, async ({ page }, info) => {
      const agentId = `agent_sd6a_${branch}_${number}`;
      await login(page, agentId);
      await draft(page, agentId, branch === 'dta');
      const turn = await send(page, question(branch, number));
      const value = await finished(page, agentId, turn, branch === 'dta' ? 'dynamic_task' : 'sop');
      const expectedStyle = number === '2' ? '详细表格' : '简洁要点';
      if (branch === 'sop') {
        const slots = value.executions.find((execution) => execution.kind === 'sop'
          && execution.status === 'succeeded')?.slots;
        expect(slots).toMatchObject({ user_name: '青禾', reply_style: expectedStyle, omitted_code: '未知' });
      } else {
        const rows = await api<Message[]>(page, `/api/chat/sessions/${turn.sessionId}/messages?tenant_id=tenant_demo`);
        const answer = rows.filter((row) => row.role === 'assistant').map((row) => row.content).join('\n');
        expect(labeledFields(answer)).toMatchObject({ NAME: '青禾', STYLE: expectedStyle, CODE: '未知' });
        expect(answer).not.toContain('SD6A-UNSELECTED-SECRET');
      }
      const operation = branch === 'sop' ? 'router.scene' : 'dynamic_task.answer';
      const actual = (await sdkFacts()).filter((row) => row.source_input_id === turn.sourceId
        && row.operation === operation && row.status === 'completed');
      expect(actual.length).toBeGreaterThan(0);
      for (const row of actual) {
        expect(row.actual_provider_call && row.sidecar_absent).toBe(true);
        expect(row.selected.every((item) => item.complete_in_sdk)).toBe(true);
        expect(row.selected.some((item) => item.id.endsWith('_name'))).toBe(true);
        expect(row.selected.some((item) => item.id.endsWith('_style'))).toBe(true);
        expect(row.selected.some((item) => item.id.endsWith('_large'))).toBe(false);
        expect(row.request_estimate).toBeLessThanOrEqual(128000);
        expect(row.usage.prompt_tokens).toBeGreaterThan(0);
      }
      await page.reload();
      expect((await state(page, agentId)).executions.map((row) => row.id)).toEqual(value.executions.map((row) => row.id));
      await managementVisible(page, agentId);
      await attach(info, { branch, number, turn, independent_labels_passed: true,
        actual_model_calls: actual, management_large_fact_visible: true, refresh_created_no_execution: true });
    });
  }
  test(`MB-LIVE ${branch.toUpperCase()} zero-budget failure control keeps full management facts`, async ({ page }, info) => {
    const agentId = `agent_sd6a_${branch}_control`;
    await login(page, agentId);
    await draft(page, agentId, branch === 'dta');
    const turn = await send(page, question(branch, 'control', true));
    const value = await finished(page, agentId, turn, branch === 'dta' ? 'dynamic_task' : 'sop');
    if (branch === 'sop') {
      expect(value.executions.find((execution) => execution.kind === 'sop'
        && execution.status === 'succeeded')?.slots)
        .toMatchObject({ user_name: '青禾', reply_style: '未知', omitted_code: '未知' });
    } else {
      const rows = await api<Message[]>(page, `/api/chat/sessions/${turn.sessionId}/messages?tenant_id=tenant_demo`);
      const answer = rows.filter((row) => row.role === 'assistant').map((row) => row.content).join('\n');
      expect(labeledFields(answer)).toMatchObject({ NAME: '未知', STYLE: '未知' });
    }
    const actual = (await sdkFacts()).filter((row) => row.source_input_id === turn.sourceId
      && row.operation === (branch === 'sop' ? 'router.scene' : 'dynamic_task.answer'));
    expect(actual.length).toBeGreaterThan(0);
    expect(actual.every((row) => row.selected.length === 0)).toBe(true);
    await managementVisible(page, agentId);
    await attach(info, { branch, turn, zero_budget_real_model_control_passed: true,
      full_domain_hydration_preserved: branch === 'sop', actual_model_calls: actual });
  });
}

test('MB-08: real waiting DTA restarts with its frozen facts; clear/revoke and fresh sources', async ({ page }, info) => {
  const agentId = 'agent_sd6a_dta_freeze';
  await login(page, agentId);
  await draft(page, agentId, true);
  const first = await send(page, 'SD6A-FROZEN：请先提出一个确认问题并等待我回复确认，不要直接完成。'
    + '确认后只沿本次计划冻结的长期记忆输出三行 NAME=姓名、STYLE=格式关键词、CODE=口令，未知口令写未知。', '', true);
  const waiting = await waitingExecution(page, agentId);
  expect(waiting.selected.length).toBeGreaterThanOrEqual(2);
  await page.goto(`/workspace/chat/${first.sessionId}`);
  await page.reload();
  expect((await state(page, agentId)).executions.find((row) => row.id === waiting.id)?.selected).toEqual(waiting.selected);
  await rm(join(CONTROL, 'restart.done'), { force: true });
  await writeFile(join(CONTROL, 'restart.request'), 'isolated-child-only');
  await expect.poll(async () => { try { await access(join(CONTROL, 'restart.done')); return true; } catch { return false; } },
    { timeout: 90_000 }).toBe(true);
  await expect.poll(async () => { try { return (await page.request.get('/api/health')).status(); } catch { return 0; } },
    { timeout: 120_000 }).toBe(200);
  expect((await state(page, agentId)).executions.find((row) => row.id === waiting.id)?.plan_checksum).toBe(waiting.plan_checksum);
  await draft(page, agentId, true);
  const correction = await send(page, 'SD6A-LATER-SOURCE：请记住我的新姓名是映竹，输出偏好改为详细表格；简短确认即可。');
  await expect.poll(async () => (await state(page, agentId)).records.some((row) => row.source_sequence > 0
    && row.source_input_id === correction.sourceId),
    { timeout: 180_000 }).toBe(true);
  await confirmWaiting(page, waiting.id, '确认。请严格使用本次原计划的冻结事实，不用其他会话新事实。');
  await expect.poll(async () => (await state(page, agentId)).executions.find((row) => row.id === waiting.id)?.status,
    { timeout: 300_000 }).toBe('succeeded');
  const rows = await api<Message[]>(page, `/api/chat/sessions/${first.sessionId}/messages?tenant_id=tenant_demo`);
  const restoredAnswer = rows.filter((row) => row.role === 'assistant').map((row) => row.content).join('\n');
  const restoredLabels = labeledFields(restoredAnswer);
  expect(restoredLabels.NAME).toBe('青禾');
  // MB-08原合同核旧来源，不以附加旧事实否定冻结；三例质量字段仍严格比较完整值。
  expect(restoredLabels.STYLE).toContain('简洁要点');
  expect(restoredLabels.STYLE).not.toContain('详细表格');
  const restored = (await sdkFacts()).filter((row) => row.operation === 'dynamic_task.answer'
    && row.source_input_id === first.sourceId);
  expect(restored.length).toBeGreaterThan(0);
  expect(restored.every((row) => row.selected.every((item) => item.complete_in_sdk))).toBe(true);
  // 本人的真实常用关系撤销沿正式API；再授权不能使旧投影代数回退。
  const status = await page.evaluate(async (agentId) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch(`/api/chat/agents/${agentId}/use?tenant_id=tenant_demo`,
      { method: 'DELETE', headers: { Authorization: `Bearer ${auth.token || ''}` } });
    return response.status;
  }, agentId);
  expect(status).toBe(200);
  expect((await state(page, agentId)).executions.find((row) => row.id === waiting.id)?.selected
    .every((value) => !value.currently_allowed)).toBe(true);
  const regrant = await page.evaluate(async (agentId) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    return (await fetch(`/api/chat/agents/${agentId}/use?tenant_id=tenant_demo`,
      { method: 'POST', headers: { Authorization: `Bearer ${auth.token || ''}` } })).status;
  }, agentId);
  expect(regrant).toBe(200);
  expect((await state(page, agentId)).executions.find((row) => row.id === waiting.id)?.selected
    .every((value) => !value.currently_allowed)).toBe(true);
  await page.evaluate((agentId) => localStorage.setItem('gongge_enterprise_agent_scope', agentId), agentId);
  await page.goto('/enterprise/memories');
  await expect(page.getByRole('button', { name: '清空我的记忆' })).toBeVisible();
  page.once('dialog', (dialog) => dialog.accept());
  const cleared = page.waitForResponse((value) => value.request().method() === 'DELETE'
    && new URL(value.url()).pathname === '/api/enterprise/memories/me');
  await page.getByRole('button', { name: '清空我的记忆' }).click();
  expect((await cleared).status()).toBe(200);
  expect((await state(page, agentId)).records).toHaveLength(0);
  await draft(page, agentId, true);
  const fresh = await send(page, 'SD6A-FRESH：请记住我的姓名是柳南，输出偏好是简洁要点。请简短确认。');
  await expect.poll(async () => (await state(page, agentId)).records.some((value) => value.source_sequence > 0
    && value.source_input_id === fresh.sourceId),
    { timeout: 180_000 }).toBe(true);
  const freshFactIds = new Set((await state(page, agentId)).records
    .filter((value) => value.source_input_id === fresh.sourceId).map((value) => value.id));
  await draft(page, agentId, true);
  const consumed = await send(page, 'SD6A-FRESH-CONSUME：请只依据本次实际长期记忆输出 NAME=姓名 与 STYLE=格式关键词。');
  await expect.poll(async () => {
    const messages = await api<Message[]>(page, `/api/chat/sessions/${consumed.sessionId}/messages?tenant_id=tenant_demo`);
    return messages.some((value) => {
      if (value.role !== 'assistant') return false;
      const labels = labeledFields(value.content);
      return labels.NAME === '柳南' && labels.STYLE?.startsWith('简洁要点')
        && !labels.STYLE.includes('详细表格');
    });
  }, { timeout: 300_000 }).toBe(true);
  const freshCalls = (await sdkFacts()).filter((row) => row.source_input_id === consumed.sourceId
    && row.operation === 'dynamic_task.answer');
  expect(freshCalls.length).toBeGreaterThan(0);
  expect(freshCalls.every((row) => row.selected.every((value) => value.complete_in_sdk))).toBe(true);
  expect(freshCalls.every((row) => {
    const facts = row.selected.filter((value) => !value.id.startsWith('echo-'));
    return facts.length > 0 && facts.every((value) => freshFactIds.has(value.id));
  })).toBe(true);
  await attach(info, { first, correction, waiting, restored_actual_model_calls: restored,
    restart_kept_plan: true, revoked_old_projection: true, clear_through_memory_page: true,
    fresh, consumed, fresh_actual_model_calls: freshCalls });
});

for (const barrier of ['clear', 'revoke']) {
  test(`MB-08 ${barrier}: actual restored answer cannot disclose invalid frozen facts`, async ({ page }, info) => {
    const agentId = `agent_sd6a_dta_${barrier}`;
    await login(page, agentId);
    await draft(page, agentId, true);
    const first = await send(page, `SD6A-${barrier.toUpperCase()}-WAIT：请先问一个确认问题并等待我确认。`
      + '确认后按本次冻结的长期记忆输出 NAME=姓名、STYLE=格式关键词、CODE=口令。'
      + '如果实际可用记忆为空，三项都填未知，不能根据旧计划猜测。', '', true);
    const waiting = await waitingExecution(page, agentId);
    expect(waiting.selected.length).toBeGreaterThanOrEqual(2);
    const before = (await sdkFacts()).length;
    if (barrier === 'clear') {
      await page.evaluate((id) => localStorage.setItem('gongge_enterprise_agent_scope', id), agentId);
      await page.goto('/enterprise/memories');
      page.once('dialog', (dialog) => dialog.accept());
      const cleared = page.waitForResponse((value) => value.request().method() === 'DELETE'
        && new URL(value.url()).pathname === '/api/enterprise/memories/me');
      await page.getByRole('button', { name: '清空我的记忆' }).click();
      expect((await cleared).status()).toBe(200);
      expect((await state(page, agentId)).records).toHaveLength(0);
    } else {
      for (const method of ['DELETE', 'POST']) {
        const status = await page.evaluate(async ({ id, method }) => {
          const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
          return (await fetch(`/api/chat/agents/${id}/use?tenant_id=tenant_demo`,
            { method, headers: { Authorization: `Bearer ${auth.token || ''}` } })).status;
        }, { id: agentId, method });
        expect(status).toBe(200);
      }
    }
    expect((await state(page, agentId)).executions.find((row) => row.id === waiting.id)?.selected
      .every((item) => !item.currently_allowed)).toBe(true);
    await confirmWaiting(page, waiting.id, '确认。按当前实际可披露的记忆输出；不可披露的姓名、格式与口令都填未知。');
    await expect.poll(async () => (await state(page, agentId)).executions.find((row) => row.id === waiting.id)?.status,
      { timeout: 300_000 }).toBe('succeeded');
    const rows = await api<Message[]>(page, `/api/chat/sessions/${first.sessionId}/messages?tenant_id=tenant_demo`);
    const answer = rows.filter((row) => row.role === 'assistant').at(-1)?.content || '';
    expect(labeledFields(answer)).toMatchObject({ NAME: '未知', STYLE: '未知' });
    expect(answer).not.toContain('青禾');
    const calls = (await sdkFacts()).slice(before).filter((row) => row.operation === 'dynamic_task.answer'
      && row.source_input_id === first.sourceId && row.status === 'completed');
    expect(calls.length).toBeGreaterThan(0);
    expect(calls.every((row) => row.actual_provider_call && row.selected.length === 0
      && row.revoked_fact_markers_absent)).toBe(true);
    await attach(info, { barrier, first, waiting, actual_restored_answer_unknown: true,
      unchanged_plan_checksum: (await state(page, agentId)).executions.find((row) => row.id === waiting.id)?.plan_checksum
        === waiting.plan_checksum, actual_model_calls: calls });
  });
}

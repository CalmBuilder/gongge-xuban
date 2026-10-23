/**
 * @Time       : 2026/10/04
 * @Author     : zhanglp8181
 * @File       : final-f1-browser-helpers.ts
 * @CallChain  : 真实登录/员工/Composer → 原Runtime/SDK/HTTP → 原账本与远端读回
 * @Description: 按JRN01→04逐类执行最终F1，各方言独立完整旅程，不拼阶段。
 */

import { expect, test, type Page, type TestInfo } from '@playwright/test';

export type Row = Record<string, unknown>;
export type Facts = {
  pid: number;
  agents: Row[];
  instances: Row[];
  operations: Row[];
  nodes: Row[];
  attempts: Row[];
  messages: Row[];
  sessions: Row[];
  signals: Row[];
  inbox: Row[];
  jobs: Row[];
  provider: { submits: Row[]; finishes: Row[] };
  sdk: Row[];
  memories: Row[];
  work_items: Row[];
  proposals: Row[];
  results: Row[];
  commands: Row[];
  events: Row[];
};

export function object(value: unknown): Row {
  /** 严格收窄真实JSON对象，禁止把未知字段当既有合同。 */
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error('F1_EXPECTED_OBJECT');
  }
  return value as Row;
}

export async function request(page: Page, path: string, method = 'GET'): Promise<unknown> {
  /** 仅旁证GET或自有provider故障控制；业务写均由真实UI发起。 */
  return page.evaluate(async ({ path, method }) => {
    const response = await fetch(path, { method });
    if (!response.ok) throw new Error(`F1_READ_STATUS_${response.status}`);
    return response.json() as Promise<unknown>;
  }, { path, method });
}

export async function facts(page: Page): Promise<Facts> {
  /** 从真实账本和独立远端读回获取当前事实。 */
  const value = object(await request(page, '/__e2e/f1/facts'));
  for (const key of ['agents', 'instances', 'operations', 'nodes', 'attempts', 'messages', 'signals', 'inbox', 'jobs',
    'memories', 'work_items', 'proposals', 'results', 'commands', 'events', 'sdk', 'sessions']) {
    if (!Array.isArray(value[key])) throw new Error(`F1_MISSING_LEDGER_${key}`);
    for (const row of value[key]) object(row);
  }
  const provider = object(value.provider);
  if (!Array.isArray(provider.submits) || !Array.isArray(provider.finishes) || typeof value.pid !== 'number') {
    throw new Error('F1_MISSING_PROVIDER_OR_PID');
  }
  return value as Facts;
}

export async function criterion(info: TestInfo, name: string, value: Row): Promise<void> {
  /** 原通过阶段即时归档，后续失败不得覆盖其身份或冒充整程通过。 */
  await info.attach('f1-safe-criterion', { body: Buffer.from(JSON.stringify({ criterion: name, ...value })),
    contentType: 'application/json' });
}

export async function login(page: Page, kind: 'SOP' | 'DTA', reviewer = false): Promise<void> {
  /** 真登录与员工选择；批准人进入工作项，本人经原对话端，不注入认证/scope。 */
  await page.goto('/');
  await page.evaluate(() => {
    localStorage.setItem('gongge_onboarding_guide_seen', '1');
    localStorage.setItem('gongge_quick_start_guide_seen', '1');
  });
  const enter = page.getByRole('button', { name: /进入平台/ });
  if (await enter.isVisible()) await enter.click();
  await page.getByRole('textbox', { name: '账号', exact: true }).fill(reviewer ? 'f1_reviewer' : 'admin');
  await page.getByLabel('密码', { exact: true }).fill(reviewer ? 'f1-reviewer-synthetic-password' : 'admin');
  const response = page.waitForResponse(row => new URL(row.url()).pathname === '/api/auth/login');
  await page.getByRole('button', { name: '登录', exact: true }).click();
  expect((await response).status()).toBe(200);
  await page.getByRole('button', { name: '切换到管理端', exact: true }).click();
  await expect(page.getByLabel('切换当前员工', { exact: true })).toBeVisible();
  await page.getByLabel('切换当前员工', { exact: true }).click();
  await page.getByRole('menuitem', { name: new RegExp(`F1 ${kind}员工`) }).click();
  await expect(page.getByLabel('切换当前员工', { exact: true })).toContainText(`F1 ${kind}员工`);
  if (reviewer) return;
  await page.getByRole('button', { name: '切换到对话端', exact: true }).click();
  await page.getByRole('button', { name: new RegExp(`员工头像 F1 ${kind}员工`) })
    .getByRole('button', { name: '发起对话', exact: true }).click();
  // MySQL 首次登录还会等待迁移后的租户/员工数据读回，保留同一可见性断言但给真实浏览器足够的启动窗口。
  await expect(page.getByPlaceholder('输入消息，按 Enter 发送...')).toBeVisible({ timeout: 30_000 });
}

export async function send(page: Page, text: string): Promise<void> {
  /** Composer投递本人消息，保持原流式处理与UI发送守卫。 */
  const input = page.getByPlaceholder('输入消息，按 Enter 发送...');
  await input.fill(text);
  await page.getByRole('button', { name: '发送', exact: true }).click();
  await expect(input).toHaveValue('', { timeout: 20_000 });
  await expect(page.getByRole('button', { name: '停止生成', exact: true })).toHaveCount(0, { timeout: 45_000 });
}

export async function waiting(page: Page, priorIds: string[]): Promise<Row> {
  /** 精确发现本轮新SOP并等待原持久节点，不能借旧执行。 */
  await expect.poll(async () => {
    const state = await facts(page);
    return state.instances.find(row => row.kind === 'sop' && !priorIds.includes(String(row.id)))?.status;
  }, { timeout: 60_000 }).toBe('waiting');
  const row = (await facts(page)).instances.find(row => row.kind === 'sop' && !priorIds.includes(String(row.id)));
  expect(row).toBeTruthy();
  return object(row);
}

export async function openCitation(page: Page, info: TestInfo, messageId?: string): Promise<void> {
  /** 点击本轮原引用并读取实际完整来源，关闭原弹窗继续业务。 */
  const state = await facts(page);
  const message = object(messageId ? state.messages.find(row => row.id === messageId)
    : [...state.messages].reverse().find(row => Array.isArray(row.citations) && row.citations.length > 0));
  const messages = state.messages.filter(row => row.session_id === message.session_id
    && Array.isArray(row.citations) && row.citations.length > 0);
  const index = messages.findIndex(row => row.id === message.id);
  expect(index).toBeGreaterThanOrEqual(0);
  const citation = page.getByLabel('知识引用').nth(index).getByRole('button', { name: /cygnus77/i }).first();
  await expect(citation).toBeVisible({ timeout: 30_000 });
  await citation.click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toContainText('750');
  await expect(dialog).toContainText('办理日期');
  await criterion(info, 'actual-citation-open', { passed: true, policy_body_complete: true, message_id: message.id, citations: message.citations });
  await dialog.getByRole('button', { name: 'Close', exact: true }).click();
}

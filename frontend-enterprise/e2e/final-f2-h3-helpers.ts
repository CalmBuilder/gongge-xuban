/**
 * @Time       : 2026/10/04
 * @Author     : zhanglp8181
 * @File       : final-f2-h3-helpers.ts
 * @CallChain  : H3真实页面 → 原反馈/SkillEditor/发布 → 精确白名单账本
 * @Description: 写操作只走页面；故障文件只延迟自有SDK/worker，旁证不推进业务。
 */
import { expect, type Page } from '@playwright/test';
import { createHash } from 'node:crypto';
import { existsSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import type { ConversationSessionPageRead, SkillRead } from '../src/types';
import type { FeedbackMaterialRead } from '../src/types/feedback';
import type { SkillCandidateRead } from '../src/types/skill-candidate';
import { login as originalLogin, object, request, type Row } from './final-f1-browser-helpers';
import { authenticatedRead, idle as originalIdle, switchStaff } from './final-f2-browser-helpers';

export const AGENT = 'agent_f2_feedback';
export const SKILL = 'skill_f2_feedback_quota';
export const FEEDBACK = `/enterprise/feedback?agent_id=${AGENT}`;
export const INSTRUCTION = '仅将 nodes.query.metadata.operation_input.employee_id 改为 slots.employee_id；保留其他字段、输入、节点、工具和边，不硬编码员工号或额度。';
export const PRIVATE_NOTE = 'F2_H3_PRIVATE_NOTE';
const CONTROL = process.env.FULLSTACK_SD6B_CONTROL_DIR || '';

export function inputHash(text: string): string {
  /** 独立散列本轮唯一Composer文字，不依据数据库排序或实例source反推来源。 */
  return createHash('sha256').update(JSON.stringify(text)).digest('hex');
}

export async function idle(page: Page): Promise<Page> {
  /** 已验完的页面先导航原idle卸载React文档，再关闭原renderer；不动登录或待验页。 */
  await page.goto('/__e2e/f2/idle');
  await expect(page.locator('body')).toHaveText('F2 identity idle');
  const client = await page.context().newCDPSession(page);
  try {
    // 仅回收已卸载测试文档；本旅程不据此声称产品内存/长期性能已验收。
    await client.send('HeapProfiler.collectGarbage');
  } finally { await client.detach(); }
  return originalIdle(page);
}

export type H3Facts = {
  pid: number; labels: Row; publication_count: number; branch: Row; job_map: Row;
  business_calls: Row[]; sdk: Row[]; job_terminal: Row[]; versions: Row[]; instances: Row[];
  messages: Row[]; feedback: Row[]; materials: Row[]; candidates: Row[];
  operations: Row[]; nodes: Row[]; attempts: Row[]; signals: Row[];
  plans: Row[]; proposals: Row[]; results: Row[]; runtime_sdk: Row[];
};

export async function state(page: Page): Promise<H3Facts> {
  /** 校验本轮只读原账本结构，不把页面提示作为持久或业务成功。 */
  const value = object(await request(page, '/__e2e/f2/h3/facts'));
  for (const key of ['business_calls', 'sdk', 'job_terminal', 'versions', 'instances', 'messages',
    'feedback', 'materials', 'candidates', 'operations', 'nodes', 'attempts', 'signals',
    'plans', 'proposals', 'results', 'runtime_sdk']) {
    if (!Array.isArray(value[key])) throw new Error('H3_FACTS_ARRAY_REQUIRED_' + key);
    for (const row of value[key]) object(row);
  }
  object(value.labels); object(value.branch); object(value.job_map);
  if (typeof value.pid !== 'number' || typeof value.publication_count !== 'number') throw new Error('H3_FACTS_IDENTITY_REQUIRED');
  return value as H3Facts;
}

export async function read<T>(page: Page, path: string): Promise<T> {
  /** 读取原API的共享类型；只用于精确身份旁证，不通过写API推进旅程。 */
  const response = await authenticatedRead(page, path);
  expect(response.status).toBe(200);
  return response.body as T;
}

export const head = (page: Page) => read<SkillRead>(page,
  `/api/enterprise/skills/${SKILL}?tenant_id=tenant_demo&agent_id=${AGENT}`);
export const candidatePath = (id: string) => `/api/enterprise/skills/feedback-candidates/${id}?tenant_id=tenant_demo`;
export const pointer = (id: string) => `skill-feedback-job:tenant_demo:${id}`;
export const responseFor = (page: Page, suffix: string, method: string) => page.waitForResponse(row =>
  row.request().method() === method && new URL(row.url()).pathname.endsWith(suffix), { timeout: 120_000 });

export async function login(page: Page, manager = false): Promise<Page> {
  /** 原登录/员工菜单建立实际身份，scope不由存储注入；闲置重页面及时释放。 */
  await originalLogin(page, 'SOP', manager);
  if (!manager) await page.goto('/enterprise/feedback');
  const agents = await read<Row[]>(page, '/api/enterprise/agents?tenant_id=tenant_demo');
  const target = object(agents.find(row => row.id === AGENT));
  console.info('H3_SCOPE_OBSERVATION', JSON.stringify({ id: target.id, name: target.name,
    status: target.status, is_overall: target.is_overall, owned: target.owned_by_current_user,
    used: target.used_by_current_user }));
  expect(target.name).toBe('F2反馈员工');
  expect(target.status).toBe('active');
  await switchStaff(page, 'F2反馈员工');
  return idle(page);
}

export async function draft(page: Page, dynamic = false): Promise<void> {
  /** Composer新建两分支，按实际引擎按钮选择，不预种执行和消息。 */
  await page.goto(`/workspace/chat/draft/${AGENT}`);
  const engine = page.getByRole('button', { name: '选择 DynamicTaskAgent 复杂任务引擎' });
  await expect(engine).toBeVisible();
  if ((await engine.getAttribute('aria-pressed') === 'true') !== dynamic) await engine.click();
  await expect(engine).toHaveAttribute('aria-pressed', String(dynamic));
}

export async function send(page: Page, text: string): Promise<string> {
  /** 真实POST由Composer发起；用唯一用户消息散列发现本轮实际session。 */
  const composer = page.getByPlaceholder('输入消息，按 Enter 发送...');
  const button = page.getByRole('button', { name: '发送', exact: true });
  await expect(composer).toBeVisible();
  await composer.fill(text);
  await expect(composer).toHaveValue(text);
  await expect(button).toBeEnabled();
  const accepted = responseFor(page, '/api/chat/stream', 'POST');
  await button.click();
  expect((await accepted).status()).toBe(200);
  const hash = inputHash(text);
  let id = '';
  await expect.poll(async () => {
    id = String((await state(page)).messages.find(row => row.role === 'user' && row.content_hash === hash)?.session_id || '');
    return id;
  }, { timeout: 90_000 }).not.toBe('');
  await expect(page.getByRole('button', { name: '停止生成', exact: true })).toHaveCount(0, { timeout: 90_000 });
  return id;
}

export async function instance(page: Page, session: string, status: string): Promise<Row> {
  /** 每session原Runtime只有一实例，以持久终态/等待和同轮assistant共同判定。 */
  await expect.poll(async () => (await state(page)).instances.filter(row => row.session_id === session && row.status === status).length,
    { timeout: 120_000 }).toBe(1);
  const rows = (await state(page)).instances.filter(row => row.session_id === session);
  expect(rows).toHaveLength(1);
  await expect.poll(async () => (await state(page)).messages.some(row => row.session_id === session && row.role === 'assistant')).toBe(true);
  return rows[0];
}

export async function ownerDetail(page: Page, sessionId: string): Promise<void> {
  /** 当前后台原分页列定位真正来源session，再用页面按钮打开本人明细。 */
  await page.goto(FEEDBACK);
  const pageRead = await read<ConversationSessionPageRead>(page,
    `/api/enterprise/sessions/page?tenant_id=tenant_demo&agent_id=${AGENT}&feedback_filter=all&page=1&page_size=50`);
  const session = pageRead.items.find(row => row.id === sessionId);
  if (!session) throw new Error('H3_ACTUAL_SOURCE_SESSION_MISSING');
  const label = session.title || session.summary || session.last_agent_question || session.id;
  await page.getByRole('row').filter({ hasText: label }).getByRole('button', { name: '查看', exact: true }).click();
  await expect(page.getByRole('dialog', { name: '对话日志详情' })).toBeVisible();
}

export async function share(page: Page, sessionId: string): Promise<FeedbackMaterialRead> {
  /** 本人真预览及明确分享；邮箱/电话脱敏与准确来源必须在实际响应成立。 */
  await ownerDetail(page, sessionId);
  await page.getByRole('dialog', { name: '对话日志详情' }).getByRole('button', { name: '预览改进材料', exact: true }).click();
  const preview = page.getByRole('dialog', { name: '预览改进材料' });
  await preview.getByRole('textbox', { name: '用户说明' }).fill(PRIVATE_NOTE + ' 电话13912345678 邮箱f2.synthetic@example.test。' + INSTRUCTION);
  const prepared = responseFor(page, '/materials/preview', 'POST');
  await preview.getByRole('button', { name: '生成脱敏预览' }).click();
  const result = await prepared;
  expect(result.status()).toBe(200);
  const material = await result.json() as FeedbackMaterialRead;
  expect(material.status).toBe('preview');
  expect(JSON.stringify(material.material)).not.toMatch(/13912345678|f2\.synthetic@example\.test/);
  expect(material.material.source.status).toBe('verified');
  const posted = responseFor(page, '/materials/' + material.id + '/share', 'POST');
  await preview.getByRole('button', { name: '确认分享给该员工管理者' }).click();
  expect((await posted).status()).toBe(200);
  await expect(preview).toHaveCount(0);
  return material;
}

export async function openCandidate(page: Page, material: FeedbackMaterialRead): Promise<SkillCandidateRead> {
  /** 原共享材料按真实feedback身份定位；当前SkillEditor入口创建actor隔离候选。 */
  await page.goto(FEEDBACK);
  const region = page.getByRole('region', { name: '反馈改进材料' });
  await region.getByRole('heading', { name: '员工已分享材料' }).locator('..').locator('article')
    .filter({ hasText: material.feedback_id }).getByRole('button', { name: '查看共享材料' }).click();
  await expect(region).toContainText(material.material.source.message_id);
  await expect(region).toContainText(material.material.source.execution_id || '');
  const opened = responseFor(page, '/feedback-candidates', 'POST');
  await page.getByRole('button', { name: '在当前编辑器改进 SOP' }).click();
  const result = await opened;
  expect(result.status()).toBe(200);
  const candidate = await result.json() as SkillCandidateRead;
  expect(candidate.scope_agent_id).toBe(AGENT);
  expect(candidate.materials.map(row => row.id)).toEqual([material.id]);
  await expect(page.getByRole('region', { name: '反馈候选审核' })).toBeVisible();
  await expect(page.getByRole('textbox', { name: 'SOP名称', exact: true })).toBeVisible();
  return candidate;
}

export function arm(kind: 'candidate' | 'feedback', name: string, mode: string): void {
  /** 事前切点仅延迟原调用或提供坏协议，不写Runtime/反馈/候选结果。 */
  writeFileSync(join(CONTROL, `h3-${kind}-case.json`), JSON.stringify({ case: name, mode }));
}

export const entered = (name: string, phase = 'sdk') => expect.poll(() => existsSync(join(CONTROL, `h3-${name}-${phase}.entered`)),
  { timeout: 90_000 }).toBe(true);
export const release = (name: string, phase = 'sdk') => writeFileSync(join(CONTROL, `h3-${name}-${phase}.release`), 'release-owned-cut');

export async function rewrite(page: Page, name: string, mode = 'normal'): Promise<string> {
  /** 原SkillEditor收到人工局部指令；记录实际worker映射的job ID。 */
  arm('candidate', name, mode);
  await page.getByRole('button', { name: '全选', exact: true }).click();
  await page.getByRole('textbox', { name: '改写选中内容' }).fill(INSTRUCTION);
  await page.getByRole('button', { name: '发送', exact: true }).click();
  await expect.poll(async () => object((await state(page)).job_map[name] || {}).job_id).not.toBeUndefined();
  return String(object((await state(page)).job_map[name]).job_id);
}

export async function terminal(page: Page, name: string, status: string): Promise<Row> {
  /** 原worker finally记录实际终态后才计次数，首SDK未释放不能冒称后继被挡。 */
  await expect.poll(async () => (await state(page)).job_terminal.find(row => row.case === name)?.status,
    { timeout: 90_000 }).toBe(status);
  return object((await state(page)).job_terminal.find(row => row.case === name));
}

export async function withdraw(page: Page, material: FeedbackMaterialRead): Promise<void> {
  /** 本人材料卡原撤回按钮，真实响应后持久状态必须withdrawn。 */
  await page.goto(FEEDBACK);
  const region = page.getByRole('region', { name: '反馈改进材料' });
  const card = region.getByRole('heading', { name: '我的改进材料' }).locator('..').locator('article')
    .filter({ hasText: material.feedback_id }).filter({ hasText: '已分享' });
  const response = responseFor(page, '/materials/' + material.id + '/withdraw', 'POST');
  await card.getByRole('button', { name: '撤回分享' }).click();
  expect((await response).status()).toBe(200);
  await expect.poll(async () => (await state(page)).materials.find(row => row.id === material.id)?.status).toBe('withdrawn');
}

export async function cleared(page: Page, id: string): Promise<void> {
  /** 稳定失效必须清正文、私密说明、差异、弹窗和job指针，刷新不得复活。 */
  const review = page.getByRole('region', { name: '反馈候选审核' });
  await expect(review.getByRole('alert')).toContainText('候选已失效或权限已变化');
  await expect(page.getByRole('textbox', { name: 'SOP名称', exact: true })).toHaveCount(0);
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(review.getByRole('button', { name: '人工确认发布候选' })).toHaveCount(0);
  await expect.poll(() => page.evaluate(key => sessionStorage.getItem(key), pointer(id))).toBeNull();
  expect(await page.locator('body').textContent()).not.toContain(PRIVATE_NOTE);
  expect(await page.locator('body').textContent()).not.toContain('F2_H3_PRIVATE_UNSAVED');
  expect(await page.locator('body').textContent()).not.toContain('F2_H3_WITHDRAW_UNSAVED');
}

export async function restart(page: Page): Promise<{before: number; after: number; old_pid_absent: boolean}> {
  /** 仅自有服务同库真实PID重启；原进程缺失和原Runtime读回必须同时成立。 */
  const before = (await state(page)).pid;
  rmSync(join(CONTROL, 'restart.done'), { force: true });
  writeFileSync(join(CONTROL, 'restart.request'), 'restart-owned-h3');
  await expect.poll(() => existsSync(join(CONTROL, 'restart.done')), { timeout: 90_000 }).toBe(true);
  await expect.poll(async () => { try { return (await state(page)).pid; } catch { return before; } },
    { timeout: 90_000 }).not.toBe(before);
  const after = (await state(page)).pid;
  expect(existsSync('/proc/' + before)).toBe(false);
  return { before, after, old_pid_absent: true };
}

export function controlRows(name: string): Row[] {
  /** 原私有控制账本只用于对比真正SDK/worker，不公开原模型输入输出。 */
  const path = join(CONTROL, name);
  return existsSync(path) ? (JSON.parse(readFileSync(path, 'utf8')) as unknown[]).map(object) : [];
}

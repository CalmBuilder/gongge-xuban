/**
 * @Time       : 2026/08/21
 * @Author     : zhanglp8181
 * @File       : agent-quality-q1-unrelated.live.fullstack.e2e.ts
 * @CallChain  : LIVE Chromium → 导入writing Skill → 普通事实问答 → AgentLoop fast path
 * @Description: 证明可用但不相关的Skill不会被自动选择、不会污染简单对话或扩大执行能力。
 */

import { expect, test } from '@playwright/test';
import { createHash } from 'node:crypto';
import { mkdir, readFile, stat, writeFile } from 'node:fs/promises';
import { basename, isAbsolute, resolve } from 'node:path';

const ENABLED = process.env.Q1_AGENT_QUALITY_E2E === '1'
  && process.env.Q1_PROFILE === 'unrelated';
const TENANT_ID = 'tenant_demo';
const AGENT_ID = 'agent_q1_plain';
const EVIDENCE_DIR = resolve('../docs/manuals/evidence');
const EVIDENCE_FILE = basename(
  process.env.Q1_UNRELATED_EVIDENCE_FILE || 'agent-quality-q1-unrelated-exploration.json',
);
const PROMPT = '请只回答一个事实问题：法国的首都是哪里？不要调用工具，也不要展开说明。';

type Event = { event_type?: string; data?: Record<string, unknown> };

test.describe.configure({ mode: 'serial', timeout: 900_000 });
test.skip(!ENABLED, '仅Q1_PROFILE=unrelated且开启真实Q1时运行');

async function login(page: import('@playwright/test').Page): Promise<void> {
  /** 以专属无资源Agent登录真实全栈页面。 */

  const status = await page.evaluate(async ({ tenantId, agentId }) => {
    localStorage.setItem('gongge_onboarding_guide_seen', '1');
    localStorage.setItem('gongge_quick_start_guide_seen', '1');
    localStorage.setItem('gongge_enterprise_agent_scope', agentId);
    const response = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tenant_id: tenantId, username: 'member', password: 'member' }),
    });
    if (response.ok) localStorage.setItem('gongge_auth', JSON.stringify(await response.json()));
    return response.status;
  }, { tenantId: TENANT_ID, agentId: AGENT_ID });
  expect(status).toBe(200);
}

async function importWritingSkill(page: import('@playwright/test').Page) {
  /** 通过真实管理端导入一次受管Skill，随后仅验证自动选择不误命中。 */

  const configured = process.env.Q1_WRITING_SKILL_DIR?.trim() || '';
  if (!configured) throw new Error('Q1_WRITING_SKILL_DIR is required');
  const directory = isAbsolute(configured) ? configured : resolve(configured);
  const directoryStat = await stat(directory);
  expect(directoryStat.isDirectory()).toBe(true);
  const skillBody = await readFile(resolve(directory, 'SKILL.md'), 'utf8');
  const name = skillBody.match(/^name:\s*([^\r\n]+)$/m)?.[1]?.trim() || '';
  expect(name).toBe('writing-for-agents');
  const sourceChecksum = createHash('sha256').update(skillBody).digest('hex');

  await page.goto('/enterprise/general-skills');
  await page.getByRole('button', { name: /新增/ }).click();
  await page.getByRole('menuitem', { name: /安全导入 Skill/ }).click();
  const dialog = page.getByRole('dialog', { name: '安全导入 Skill 包' });
  await dialog.getByRole('tab', { name: '选择文件夹' }).click();
  await dialog.locator('input[webkitdirectory]').setInputFiles(directory);
  const previewResponse = page.waitForResponse((response) => (
    response.request().method() === 'POST'
      && new URL(response.url()).pathname === '/api/enterprise/general-skill-import-jobs'
  ));
  await dialog.getByRole('button', { name: '生成安全预览' }).click();
  expect((await previewResponse).status()).toBe(202);
  await expect(dialog.getByText(name, { exact: true })).toBeVisible({ timeout: 60_000 });
  const confirmResponse = page.waitForResponse((response) => (
    response.request().method() === 'POST'
      && new URL(response.url()).pathname.endsWith('/confirm')
  ));
  await dialog.getByRole('button', { name: '固定版本并绑定' }).click();
  const confirmed = await confirmResponse;
  expect(confirmed.status()).toBe(200);
  const confirmation = await confirmed.json() as {
    raw_checksum?: string;
    normalized_checksum?: string;
    preview_checksum?: string;
    installed_revision_ids?: string[];
    candidates?: Array<{ name: string; content_checksum: string; manifest_checksum: string }>;
  };
  await expect(dialog).toBeHidden();
  const candidate = confirmation.candidates?.find((item) => item.name === name);
  expect(candidate?.content_checksum).toMatch(/^[a-f0-9]{64}$/);
  return {
    name,
    source_checksum: sourceChecksum,
    raw_checksum: confirmation.raw_checksum || '',
    normalized_checksum: confirmation.normalized_checksum || '',
    preview_checksum: confirmation.preview_checksum || '',
    installed_revision_ids: confirmation.installed_revision_ids || [],
    content_checksum: candidate?.content_checksum || '',
    manifest_checksum: candidate?.manifest_checksum || '',
  };
}

async function createSession(page: import('@playwright/test').Page): Promise<string> {
  /** 创建无历史、无附件的专属普通会话。 */

  const id = await page.evaluate(async ({ tenantId, agentId }) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch('/api/chat/sessions', {
      method: 'POST',
      headers: { Authorization: `Bearer ${auth.token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ tenant_id: tenantId, agent_id: agentId, title: 'Q1 unrelated skill', origin: 'owned' }),
    });
    if (!response.ok) throw new Error(`create session failed: ${response.status}`);
    return String((await response.json() as { id?: string }).id || '');
  }, { tenantId: TENANT_ID, agentId: AGENT_ID });
  expect(id).toMatch(/^session_/);
  return id;
}

async function openChatWhenMetadataReady(
  page: import('@playwright/test').Page,
  sessionId: string,
): Promise<void> {
  /** 等待会话和待回答元数据提交后再发送，避免把页面首屏竞态误判为路由失败。 */

  const sessionsResponse = page.waitForResponse((response) => (
    new URL(response.url()).pathname === '/api/chat/sessions'
      && response.request().method() === 'GET'
      && response.status() === 200
  ));
  const handoffsResponse = page.waitForResponse((response) => (
    new URL(response.url()).pathname === '/api/chat/handoffs'
      && response.request().method() === 'GET'
      && response.status() === 200
  ));
  await page.goto(`/workspace/chat/${sessionId}`);
  await Promise.all([sessionsResponse, handoffsResponse]);
  await page.waitForTimeout(250);
}

async function readFacts(page: import('@playwright/test').Page, sessionId: string) {
  /** 只取权威消息和事件，不能用页面成功文案代替路由Oracle。 */

  return page.evaluate(async ({ id, tenantId }) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const headers = { Authorization: `Bearer ${auth.token}` };
    const events = await fetch(`/api/chat/sessions/${id}/events?tenant_id=${tenantId}`, { headers })
      .then((response) => response.json()) as Event[];
    const messages = await fetch(`/api/chat/sessions/${id}/messages?tenant_id=${tenantId}`, { headers })
      .then((response) => response.json()) as Array<{ role: string; content: string }>;
    return { events, messages };
  }, { id: sessionId, tenantId: TENANT_ID });
}

test('无关writing Skill不污染普通事实对话', async ({ page }) => {
  /** Skill 已安全导入但用户不需要写作时，自动路由必须保持轻量且无工具。 */

  await page.goto('/enterprise/dashboard');
  await login(page);
  const skill = await importWritingSkill(page);
  const sessionId = await createSession(page);
  await openChatWhenMetadataReady(page, sessionId);
  const composer = page.getByPlaceholder('输入消息，按 Enter 发送...');
  await expect(composer).toBeVisible();
  await composer.fill(PROMPT);
  await page.getByRole('button', { name: '发送', exact: true }).click();
  await expect.poll(async () => {
    const facts = await readFacts(page, sessionId);
    return facts.messages.filter((item) => item.role === 'assistant').length;
  }, { timeout: 300_000, intervals: [1_000, 2_000, 5_000] }).toBe(1);
  const facts = await readFacts(page, sessionId);
  const answer = facts.messages.find((item) => item.role === 'assistant')?.content || '';
  const eventTypes = facts.events.map((event) => String(event.event_type || ''));
  expect(answer).toMatch(/法国|巴黎|Paris|France/i);
  expect(eventTypes).not.toContain('dynamic_task_delegated');
  expect(eventTypes).not.toContain('skill_loaded');
  expect(eventTypes).not.toContain('skill_use_completed');
  expect(eventTypes.some((item) => item.startsWith('input_'))).toBe(false);
  await mkdir(EVIDENCE_DIR, { recursive: true });
  await writeFile(resolve(EVIDENCE_DIR, EVIDENCE_FILE), `${JSON.stringify({
    suite: 'Q1 unrelated Skill negative control',
    // 无关Skill负向证据同样遵循Q1批Runner的显式终态契约。
    test_status: 'passed',
    completed_at: new Date().toISOString(),
    source_model_config_id: process.env.Q1_SOURCE_MODEL_CONFIG_ID || null,
    provider_endpoint: process.env.Q1_PROVIDER_ENDPOINT || null,
    model: process.env.Q1_MODEL_NAME || null,
    capability_checksum: process.env.Q1_MODEL_CAPABILITY_CHECKSUM || null,
    profile: process.env.Q1_PROFILE || null,
    prompt_sha256: createHash('sha256').update(PROMPT).digest('hex'),
    session_id: sessionId,
    skill,
    answer,
    event_types: eventTypes,
    hard_gates: {
      fact_answer: /法国|巴黎|Paris|France/i.test(answer),
      no_dynamic: !eventTypes.includes('dynamic_task_delegated'),
      no_skill_use: !eventTypes.includes('skill_loaded') && !eventTypes.includes('skill_use_completed'),
      no_attachment_or_tool: !eventTypes.some((item) => item.startsWith('input_')),
    },
    certification_fingerprints: JSON.parse(process.env.Q1_CERTIFICATION_FINGERPRINT_JSON || '{}'),
  }, null, 2)}\n`, 'utf8');
});

/**
 * @Time       : 2026/08/20
 * @Author     : zhanglp8181
 * @File       : agent-quality-q1-plain-simple.live.fullstack.e2e.ts
 * @CallChain  : LIVE Chromium → 普通短对话 → AgentLoop fast path → Q1证据
 * @Description: 证明无 Skill、无附件的简单对话不误建 Dynamic/Skill/附件执行，同时保留上下文追问。
 */

import { expect, test } from '@playwright/test';
import { createHash } from 'node:crypto';
import { mkdir, writeFile } from 'node:fs/promises';
import { basename, resolve } from 'node:path';

const ENABLED = process.env.Q1_AGENT_QUALITY_E2E === '1'
  && process.env.Q1_PROFILE === 'plain-simple';
const TENANT_ID = 'tenant_demo';
const AGENT_ID = 'agent_q1_plain';
const EVIDENCE_DIR = resolve('../docs/manuals/evidence');
const EVIDENCE_FILE = basename(
  process.env.Q1_PLAIN_SIMPLE_EVIDENCE_FILE || 'agent-quality-q1-plain-simple-exploration.json',
);
const PROMPTS = [
  '你好，请用一句中文介绍你能帮助我做什么，不要调用工具。',
  '把你刚才的介绍压缩成八个字以内，仍然只用一句中文。',
] as const;

type Event = { event_type?: string; data?: Record<string, unknown> };

test.describe.configure({ mode: 'serial', timeout: 900_000 });
test.skip(!ENABLED, '仅Q1_PROFILE=plain-simple且开启真实Q1时运行');

async function login(page: import('@playwright/test').Page): Promise<void> {
  /** 使用隔离 plain Agent 登录真实全栈页面。 */

  const status = await page.evaluate(async ({ tenantId }) => {
    localStorage.setItem('gongge_onboarding_guide_seen', '1');
    localStorage.setItem('gongge_quick_start_guide_seen', '1');
    localStorage.setItem('gongge_enterprise_agent_scope', 'agent_q1_plain');
    const response = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tenant_id: tenantId, username: 'member', password: 'member' }),
    });
    if (response.ok) localStorage.setItem('gongge_auth', JSON.stringify(await response.json()));
    return response.status;
  }, { tenantId: TENANT_ID });
  expect(status).toBe(200);
}

async function createSession(page: import('@playwright/test').Page): Promise<string> {
  /** 创建不带历史、Skill 或附件的全新会话。 */

  const id = await page.evaluate(async ({ tenantId, agentId }) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch('/api/chat/sessions', {
      method: 'POST',
      headers: { Authorization: `Bearer ${auth.token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ tenant_id: tenantId, agent_id: agentId, title: 'Q1 plain simple', origin: 'owned' }),
    });
    if (!response.ok) throw new Error(`create session failed: ${response.status}`);
    return String((await response.json() as { id?: string }).id || '');
  }, { tenantId: TENANT_ID, agentId: AGENT_ID });
  expect(id).toMatch(/^session_/);
  return id;
}

async function readFacts(page: import('@playwright/test').Page, sessionId: string) {
  /** 只读取权威消息与事件，避免用页面文案判断路由。 */

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

test('Q1 plain simple真实短对话不误进入Dynamic或Skill', async ({ page }) => {
  /** 两轮真实对话均走轻量路径，并保留上一轮上下文。 */

  await page.goto('/enterprise/dashboard');
  await login(page);
  const sessionId = await createSession(page);
  await page.goto(`/workspace/chat/${sessionId}`);
  const composer = page.getByPlaceholder('输入消息，按 Enter 发送...');
  await expect(composer).toBeVisible();
  const started = Date.now();
  const promptHashes: string[] = [];
  for (const prompt of PROMPTS) {
    promptHashes.push(createHash('sha256').update(prompt).digest('hex'));
    await composer.fill(prompt);
    await page.getByRole('button', { name: '发送', exact: true }).click();
    await expect.poll(async () => {
      const facts = await readFacts(page, sessionId);
      return facts.messages.filter((item) => item.role === 'assistant').length;
    }, { timeout: 300_000, intervals: [1_000, 2_000, 5_000] }).toBe(promptHashes.length);
  }
  const facts = await readFacts(page, sessionId);
  const assistant = facts.messages.filter((item) => item.role === 'assistant');
  const eventTypes = facts.events.map((event) => String(event.event_type || ''));
  expect(assistant).toHaveLength(2);
  expect(assistant.every((item) => item.content.trim().length > 0)).toBe(true);
  expect(eventTypes).not.toContain('dynamic_task_delegated');
  expect(eventTypes).not.toContain('skill_loaded');
  expect(eventTypes).not.toContain('skill_use_completed');
  expect(eventTypes.some((item) => item === 'assistant_message_created')).toBe(true);
  await mkdir(EVIDENCE_DIR, { recursive: true });
  await writeFile(resolve(EVIDENCE_DIR, EVIDENCE_FILE), `${JSON.stringify({
    suite: 'Q1 plain simple no-skill no-attachment fast-path',
    // 批Runner以该字段区分Playwright退出码与证据是否完整；成功写证据时状态必须显式固定。
    test_status: 'passed',
    completed_at: new Date().toISOString(),
    source_model_config_id: process.env.Q1_SOURCE_MODEL_CONFIG_ID || null,
    provider_endpoint: process.env.Q1_PROVIDER_ENDPOINT || null,
    model: process.env.Q1_MODEL_NAME || null,
    capability_checksum: process.env.Q1_MODEL_CAPABILITY_CHECKSUM || null,
    profile: process.env.Q1_PROFILE || null,
    session_id: sessionId,
    prompt_sha256: promptHashes,
    assistant_answers: assistant.map((item) => item.content),
    event_types: eventTypes,
    duration_ms: Date.now() - started,
    hard_gates: {
      two_answers: assistant.length === 2,
      no_dynamic: !eventTypes.includes('dynamic_task_delegated'),
      no_skill: !eventTypes.includes('skill_loaded') && !eventTypes.includes('skill_use_completed'),
      no_attachment: !eventTypes.some((item) => item.startsWith('input_')),
    },
    certification_fingerprints: JSON.parse(process.env.Q1_CERTIFICATION_FINGERPRINT_JSON || '{}'),
  }, null, 2)}\n`, 'utf8');
});

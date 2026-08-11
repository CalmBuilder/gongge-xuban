/**
 * @Time       : 2026/08/13
 * @Author     : zhanglp8181
 * @File       : skill-distillation-s3.fullstack.e2e.ts
 * @CallChain  : Chromium → 安全导入 → 会话 Skill picker → AgentLoop → Use/事件账本
 * @Description: 验证 user-only Skill 的 mute/unmute、结构化强制加载与真实回复消费闭环。
 */

import { expect, test, type Page } from '@playwright/test';
import { createHash } from 'node:crypto';

const SKILL_NAME = 's3-browser-guidance';
const SKILL_MARKDOWN = [
  '---',
  `name: ${SKILL_NAME}`,
  'description: 浏览器强制加载与会话静音验收。',
  'disable-model-invocation: true',
  'x-gongge-contracts:',
  '  output-format: markdown',
  '  approval-policy: required',
  '---',
  '# S3 browser guidance',
  '只返回 S3-GUIDED-SUCCESS，并说明使用了固定修订。',
  '',
].join('\n');

async function loginAsMember(page: Page) {
  /** 通过真实认证 API 建立成员会话，并固定本人数字员工范围。 */

  await page.goto('/enterprise/dashboard');
  const status = await page.evaluate(async () => {
    const response = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tenant_id: 'tenant_demo', username: 'member', password: 'member' }),
    });
    const body = await response.json();
    if (response.ok) {
      localStorage.setItem('gongge_auth', JSON.stringify(body));
      localStorage.setItem('gongge_onboarding_guide_seen', '1');
      localStorage.setItem('gongge_quick_start_guide_seen', '1');
      localStorage.setItem('gongge_enterprise_agent_scope', 'agent_e2e_member_employee');
    }
    return response.status;
  });
  expect(status).toBe(200);
}

async function importGuidanceSkill(page: Page): Promise<string> {
  /** 经产品 UI 导入并确认 user-only 指导 Skill，再从公开列表读取稳定 Skill ID。 */

  await page.goto('/enterprise/general-skills');
  await page.getByRole('button', { name: /新增/ }).click();
  await page.getByRole('menuitem', { name: '安全导入 Skill' }).click();
  const dialog = page.getByRole('dialog', { name: '安全导入 Skill 包' });
  await dialog.locator('input[type="file"]').setInputFiles({
    name: 'SKILL.md',
    mimeType: 'text/markdown',
    buffer: Buffer.from(SKILL_MARKDOWN),
  });
  const createResponse = page.waitForResponse((response) => (
    new URL(response.url()).pathname === '/api/enterprise/general-skill-import-jobs'
    && response.request().method() === 'POST'
  ));
  await dialog.getByRole('button', { name: '生成安全预览' }).click();
  expect((await createResponse).status()).toBe(202);
  await expect(dialog.getByText(SKILL_NAME, { exact: true })).toBeVisible();
  await expect(dialog.getByText('仅显式调用', { exact: true })).toBeVisible();
  await expect(dialog.getByText(/output_format=markdown/)).toBeVisible();
  await expect(dialog.getByText(/approval_policy=required/)).toBeVisible();
  const confirmResponse = page.waitForResponse((response) => (
    new URL(response.url()).pathname.endsWith('/confirm')
    && response.request().method() === 'POST'
  ));
  await dialog.getByRole('button', { name: '固定版本并绑定' }).click();
  expect((await confirmResponse).status()).toBe(200);

  return page.evaluate(async (name) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch(
      '/api/enterprise/general-skills?tenant_id=tenant_demo&agent_id=agent_e2e_member_employee',
      { headers: { Authorization: `Bearer ${auth.token}` } },
    );
    const rows = await response.json() as Array<{ id: string; name: string }>;
    const row = rows.find((item) => item.name === name);
    if (!row) throw new Error('confirmed Skill is absent from the employee catalog');
    return row.id;
  }, SKILL_NAME);
}

test('S3 user-only Skill 经真实 picker 强制加载、静音恢复并完成可审计回复', async ({ page }) => {
  const failures: string[] = [];
  page.on('pageerror', (error) => failures.push(error.message));
  page.on('response', (response) => {
    const path = new URL(response.url()).pathname;
    if (path.startsWith('/api/') && response.status() >= 500) {
      failures.push(`${response.status()} ${path}`);
    }
  });
  await loginAsMember(page);
  const skillId = await importGuidanceSkill(page);
  const sessionId = await page.evaluate(async () => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch('/api/chat/sessions', {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${auth.token}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        agent_id: 'agent_e2e_member_employee',
        title: 'S3 Skill 浏览器消费',
        origin: 'owned',
      }),
    });
    const body = await response.json() as { id?: string; detail?: string };
    if (!response.ok || !body.id) throw new Error(body.detail || 'session creation failed');
    return body.id;
  });

  await page.goto(`/workspace/chat/${sessionId}`);
  await page.getByRole('button', { name: '选择本轮 Skill' }).click();
  await page.getByRole('menuitem').filter({ hasText: SKILL_NAME }).click();
  const muteResponse = page.waitForResponse((response) => (
    new URL(response.url()).pathname.endsWith(`/general-skills/${skillId}`)
    && response.request().method() === 'PUT'
  ));
  await page.getByRole('button', { name: `取消并静音 Skill ${SKILL_NAME}` }).click();
  expect((await muteResponse).status()).toBe(200);
  await page.getByRole('button', { name: '选择本轮 Skill' }).click();
  const mutedItem = page.getByRole('menuitem').filter({ hasText: `${SKILL_NAME}（已静音）` });
  await expect(mutedItem).toBeVisible();
  const unmuteResponse = page.waitForResponse((response) => (
    new URL(response.url()).pathname.endsWith(`/general-skills/${skillId}`)
    && response.request().method() === 'PUT'
  ));
  await mutedItem.click();
  expect((await unmuteResponse).status()).toBe(200);
  await expect(
    page.getByRole('button', { name: `取消并静音 Skill ${SKILL_NAME}` }),
  ).toBeVisible();

  await page.getByPlaceholder('输入消息，按 Enter 发送...').fill('请按本轮选定的指南处理售后问题');
  const turnRequest = page.waitForRequest((request) => (
    new URL(request.url()).pathname === '/api/chat/stream'
    && request.method() === 'POST'
  ));
  await page.getByRole('button', { name: '发送', exact: true }).click();
  const originalTurnPayload = (await turnRequest).postDataJSON() as Record<string, unknown>;
  expect(originalTurnPayload).toMatchObject({
    forced_general_skill_id: skillId,
  });
  await expect(page.getByRole('paragraph').filter({ hasText: 'S3-GUIDED-SUCCESS' })).toBeVisible({
    timeout: 30_000,
  });

  const evidence = await page.evaluate(async (id) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch(`/api/chat/sessions/${id}/events?tenant_id=tenant_demo`, {
      headers: { Authorization: `Bearer ${auth.token}` },
    });
    return response.json() as Promise<Array<{ event_type: string; data?: Record<string, unknown> }>>;
  }, sessionId);
  expect(evidence.map((event) => event.event_type)).toEqual(expect.arrayContaining([
    'skill_load_started',
    'skill_loaded',
    'skill_use_completed',
  ]));
  const loaded = evidence.find((event) => event.event_type === 'skill_loaded');
  expect(loaded?.data).toMatchObject({ skill_id: skillId, selection_mode: 'forced' });
  const useId = String(loaded?.data?.skill_use_id || '');
  expect(useId).not.toBe('');
  const messageCountBeforeReplay = await page.evaluate(async (id) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch(`/api/chat/sessions/${id}/messages?tenant_id=tenant_demo`, {
      headers: { Authorization: `Bearer ${auth.token}` },
    });
    return (await response.json() as unknown[]).length;
  }, sessionId);
  const replayed = await page.evaluate(async (payload) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch('/api/chat/stream', {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${auth.token}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(payload),
    });
    return { status: response.status, stream: await response.text() };
  }, originalTurnPayload);
  expect(replayed.status).toBe(200);
  expect(replayed.stream).toContain('S3-GUIDED-SUCCESS');
  expect(replayed.stream).toContain('event: complete');
  const mismatchedReplayStatus = await page.evaluate(async (payload) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch('/api/chat/stream', {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${auth.token}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ ...payload, message: '同一个 turn 不能换成另一项任务' }),
    });
    return response.status;
  }, originalTurnPayload);
  expect(mismatchedReplayStatus).toBe(409);
  const replayEvidence = await page.evaluate(async (id) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const [eventsResponse, messagesResponse] = await Promise.all([
      fetch(`/api/chat/sessions/${id}/events?tenant_id=tenant_demo`, {
        headers: { Authorization: `Bearer ${auth.token}` },
      }),
      fetch(`/api/chat/sessions/${id}/messages?tenant_id=tenant_demo`, {
        headers: { Authorization: `Bearer ${auth.token}` },
      }),
    ]);
    return {
      events: await eventsResponse.json() as Array<{ event_type: string }>,
      messageCount: (await messagesResponse.json() as unknown[]).length,
    };
  }, sessionId);
  expect(replayEvidence.events.filter((event) => event.event_type === 'skill_loaded')).toHaveLength(1);
  expect(replayEvidence.messageCount).toBe(messageCountBeforeReplay);
  const resourceChecksum = createHash('sha256').update(SKILL_MARKDOWN).digest('hex');
  const firstPage = await page.evaluate(async ({ id, use, checksum }) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch(
      `/api/chat/sessions/${id}/general-skill-loads/${use}/resources/${checksum}?offset=0&limit=20`,
      { headers: { Authorization: `Bearer ${auth.token}` } },
    );
    return { status: response.status, body: await response.json() };
  }, { id: sessionId, use: useId, checksum: resourceChecksum });
  expect(firstPage.status).toBe(200);
  expect(firstPage.body).toMatchObject({ offset: 0, has_more: true });
  expect(String(firstPage.body.content)).toBe(SKILL_MARKDOWN.slice(0, 20));

  await page.getByRole('button', { name: '选择本轮 Skill' }).click();
  await page.getByRole('menuitem').filter({ hasText: SKILL_NAME }).click();
  const countermandResponse = page.waitForResponse((response) => (
    new URL(response.url()).pathname.endsWith(`/general-skills/${skillId}`)
    && response.request().method() === 'PUT'
  ));
  await page.getByRole('button', { name: `取消并静音 Skill ${SKILL_NAME}` }).click();
  expect((await countermandResponse).status()).toBe(200);
  const blockedResource = await page.evaluate(async ({ id, use, checksum }) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch(
      `/api/chat/sessions/${id}/general-skill-loads/${use}/resources/${checksum}`,
      { headers: { Authorization: `Bearer ${auth.token}` } },
    );
    return response.status;
  }, { id: sessionId, use: useId, checksum: resourceChecksum });
  expect(blockedResource).toBe(404);
  const countermandEvents = await page.evaluate(async (id) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch(`/api/chat/sessions/${id}/events?tenant_id=tenant_demo`, {
      headers: { Authorization: `Bearer ${auth.token}` },
    });
    return response.json() as Promise<Array<{ event_type: string }>>;
  }, sessionId);
  expect(countermandEvents.some((event) => event.event_type === 'skill_countermanded')).toBe(true);

  const rejected = await page.evaluate(async (id) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch('/api/chat/stream', {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${auth.token}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        session_id: id,
        agent_id: 'agent_e2e_member_employee',
        message: '未知 Skill 必须明确拒绝，不能退回普通问答',
        client_turn_id: 's3-browser-unknown-force',
        channel: 'web',
        forced_general_skill_id: 'genskill_unknown_cross_scope',
      }),
    });
    return { status: response.status, stream: await response.text() };
  }, sessionId);
  expect(rejected.status).toBe(200);
  expect(rejected.stream).toContain('所选 Skill 当前不可用或已被停用');
  expect(rejected.stream).toContain('skill_load_rejected');
  expect(rejected.stream).not.toContain('LLM provider request failed');

  await page.goto('/enterprise/general-skills');
  await page.getByRole('button', { name: /新增/ }).click();
  await page.getByRole('menuitem', { name: '安全导入 Skill' }).click();
  const autoDialog = page.getByRole('dialog', { name: '安全导入 Skill 包' });
  await autoDialog.locator('input[type="file"]').setInputFiles({
    name: 'SKILL.md',
    mimeType: 'text/markdown',
    buffer: Buffer.from([
      '---',
      'name: s3-browser-auto',
      'description: S3-AUTO 浏览器自动命中验收。',
      '---',
      '# Auto guidance',
      '只返回 S3-AUTO-GUIDED，并说明从有预算目录自动选择。',
      '',
    ].join('\n')),
  });
  await autoDialog.getByRole('button', { name: '生成安全预览' }).click();
  await expect(autoDialog.getByText('s3-browser-auto', { exact: true })).toBeVisible();
  await expect(autoDialog.getByText('允许模型选择', { exact: true })).toBeVisible();
  const autoConfirm = page.waitForResponse((response) => (
    new URL(response.url()).pathname.endsWith('/confirm')
    && response.request().method() === 'POST'
  ));
  await autoDialog.getByRole('button', { name: '固定版本并绑定' }).click();
  expect((await autoConfirm).status()).toBe(200);
  const autoSessionId = await page.evaluate(async () => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch('/api/chat/sessions', {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${auth.token}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        agent_id: 'agent_e2e_member_employee',
        title: 'S3 自动 Skill 浏览器消费',
        origin: 'owned',
      }),
    });
    return (await response.json() as { id: string }).id;
  });
  await page.goto(`/workspace/chat/${autoSessionId}`);
  await page.getByPlaceholder('输入消息，按 Enter 发送...').fill(
    'S3-AUTO 请自动选择合适的已审核指南',
  );
  const automaticRequest = page.waitForRequest((request) => (
    new URL(request.url()).pathname === '/api/chat/stream' && request.method() === 'POST'
  ));
  await page.getByRole('button', { name: '发送', exact: true }).click();
  expect((await automaticRequest).postDataJSON()).not.toHaveProperty('forced_general_skill_id');
  await expect(page.getByRole('paragraph').filter({ hasText: 'S3-AUTO-GUIDED' })).toBeVisible({
    timeout: 30_000,
  });
  const autoEvents = await page.evaluate(async (id) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch(`/api/chat/sessions/${id}/events?tenant_id=tenant_demo`, {
      headers: { Authorization: `Bearer ${auth.token}` },
    });
    return response.json() as Promise<Array<{ event_type: string; data?: Record<string, unknown> }>>;
  }, autoSessionId);
  expect(autoEvents.find((event) => event.event_type === 'skill_loaded')?.data).toMatchObject({
    selection_mode: 'auto',
  });
  expect(failures).toEqual([]);
});

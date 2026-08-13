/**
 * @Time       : 2026/08/12
 * @Author     : zhanglp8181
 * @File       : skill-governance-g1.fullstack.e2e.ts
 * @CallChain  : Playwright Chromium → secure import → 我的 Skill 库 → batch binding API
 * @Description: 验证 G1-A 页面导入后可把同一不可变 Revision 原子装配到本人多个数字员工。
 */

import { expect, test, type Page } from '@playwright/test';

const FIXED_SKILLS_COMMIT = '84fdeffd12f2ee307994d1eb6feb48173b6e0502';

test.describe.configure({ timeout: 90_000 });

async function loginAsMember(page: Page) {
  const status = await page.evaluate(async () => {
    localStorage.setItem('gongge_onboarding_guide_seen', '1');
    localStorage.setItem('gongge_quick_start_guide_seen', '1');
    localStorage.setItem('gongge_enterprise_agent_scope', 'agent_skill_demo_a_docs');
    const response = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tenant_id: 'tenant_demo', username: 'member', password: 'member' }),
    });
    const body = await response.json();
    if (response.ok) localStorage.setItem('gongge_auth', JSON.stringify(body));
    return response.status;
  });
  expect(status).toBe(200);
}

test('G1-A 正式 GitHub Skill 导入后从我的 Skill 库原子装配多个数字员工', async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(Crypto.prototype, 'randomUUID', {
      configurable: true,
      value: undefined,
    });
  });
  const browserFailures: string[] = [];
  page.on('pageerror', (error) => browserFailures.push(error.message));
  page.on('console', (message) => {
    if (message.type() === 'error') browserFailures.push(message.text());
  });
  await page.goto('/enterprise/dashboard');
  await loginAsMember(page);
  await page.evaluate(() => {
    localStorage.setItem('gongge_enterprise_agent_scope', 'agent_skill_demo_a_docs');
  });
  await page.goto('/enterprise/general-skills');

  await page.getByRole('button', { name: /新增/ }).click();
  await page.getByRole('menuitem', { name: '安全导入 Skill' }).click();
  const importDialog = page.getByRole('dialog', { name: '安全导入 Skill 包' });
  await importDialog.getByRole('tab', { name: 'GitHub 固定版本' }).click();
  await importDialog.getByLabel('GitHub 仓库地址').fill('https://github.com/mattpocock/skills');
  await importDialog.getByLabel('完整 commit SHA').fill(FIXED_SKILLS_COMMIT);
  await importDialog.getByLabel('仓库内 Skill 目录').fill('skills/productivity/writing-for-agents');
  await importDialog.getByRole('button', { name: '生成安全预览' }).click();
  await expect(importDialog.getByText('writing-for-agents', { exact: true })).toBeVisible();
  await importDialog.getByRole('button', { name: '固定版本并绑定' }).click();
  await expect(importDialog).toBeHidden();

  await page.getByRole('button', { name: '我的 Skill 库' }).click();
  const library = page.getByRole('dialog', { name: '我的 Skill 库' });
  await expect(library.getByText('writing-for-agents', { exact: true })).toBeVisible();
  const docsAgent = library.getByLabel('Skill演示A｜文档规范分身');
  const diagnosisAgent = library.getByLabel('Skill演示B｜故障诊断分身');
  await expect(docsAgent).toBeChecked();
  await diagnosisAgent.check();
  await library.getByRole('button', { name: '预检装配' }).click();
  await expect(library.getByRole('region', { name: '装配预检结果' })).toContainText('Skill演示B｜故障诊断分身：新建绑定');
  await library.getByRole('button', { name: '确认整批生效' }).click();
  await expect(page.getByText('已原子更新所有数字员工的 Skill 装配')).toBeVisible();
  await expect(diagnosisAgent).toBeChecked();

  const facts = await page.evaluate(async () => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch('/api/enterprise/my-general-skills', {
      headers: { Authorization: `Bearer ${auth.token}` },
    });
    return response.json() as Promise<Array<{
      name: string;
      current_revision_id: string;
      bindings: Array<{ agent_id: string; pinned_revision_id?: string; status: string }>;
    }>>;
  });
  const skill = facts.find((item) => item.name === 'writing-for-agents');
  expect(skill).toBeTruthy();
  const active = skill?.bindings.filter((binding) => binding.status === 'active') || [];
  expect(active.map((binding) => binding.agent_id)).toEqual(expect.arrayContaining([
    'agent_skill_demo_a_docs',
    'agent_skill_demo_b_diagnosis',
  ]));
  expect(new Set(active.map((binding) => binding.pinned_revision_id))).toEqual(new Set([skill?.current_revision_id]));
  const consumed = await page.evaluate(async (skillId) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const headers = { Authorization: `Bearer ${auth.token}`, 'Content-Type': 'application/json' };
    const session = await fetch('/api/chat/sessions', {
      method: 'POST', headers,
      body: JSON.stringify({
        tenant_id: 'tenant_demo', agent_id: 'agent_skill_demo_a_docs', title: 'G1-A 消费', origin: 'owned',
      }),
    }).then((response) => response.json()) as { id: string };
    const response = await fetch('/api/chat/stream', {
      method: 'POST', headers,
      body: JSON.stringify({
        tenant_id: 'tenant_demo', session_id: session.id,
        agent_id: 'agent_skill_demo_a_docs', client_turn_id: 'turn_g1_a_consume',
        message: 'G1-A动态：使用固定 writing-for-agents Skill 编写含输入、步骤、异常和验收标准的售后升级操作规范', channel: 'web',
        forced_general_skill_id: skillId,
      }),
    });
    const body = await response.text();
    const events = await fetch(
      `/api/chat/sessions/${session.id}/events?tenant_id=tenant_demo`, { headers },
    ).then((eventResponse) => eventResponse.json()) as Array<{
      event_type: string; data?: Record<string, unknown>;
    }>;
    const executionId = String(
      events.find((event) => event.event_type === 'dynamic_task_delegated')?.data?.execution_id || '',
    );
    const execution = executionId
      ? await fetch(`/api/executions/${executionId}?tenant_id=tenant_demo`, { headers })
        .then((eventResponse) => eventResponse.json())
      : null;
    return { status: response.status, body, events, executionId, execution };
  }, skill?.id);
  expect(consumed.status).toBe(200);
  expect(consumed.body).toContain('G1-A-DYNAMIC-CONSUMED-SUCCESS');
  expect(consumed.events.map((event) => event.event_type)).toEqual(expect.arrayContaining([
    'skill_loaded', 'dynamic_task_delegated', 'execution_succeeded', 'skill_use_completed',
  ]));
  expect(consumed.executionId).not.toBe('');
  expect(consumed.execution).toMatchObject({ status: 'succeeded' });
  const executionUses = consumed.execution?.skill_uses as Array<{
    id: string; skill_id: string; content_checksum: string; status: string;
  }>;
  const executionSteps = consumed.execution?.steps as Array<{
    kind: string; guidance_skill_use_ids: string[];
  }>;
  const fixedUse = executionUses.find((use) => use.skill_id === skill?.id);
  expect(fixedUse).toMatchObject({ status: 'completed' });
  expect(fixedUse?.content_checksum).toMatch(/^[a-f0-9]{64}$/);
  expect(executionSteps.find((step) => step.kind === 'answer')?.guidance_skill_use_ids)
    .toContain(fixedUse?.id);
  expect(browserFailures).toEqual([]);
});

test('G1-B 对话显式安装固定 GitHub Skill 经持久卡确认后进入使用菜单', async ({ page }) => {
  await page.goto('/enterprise/dashboard');
  await loginAsMember(page);
  const sessionId = await page.evaluate(async () => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch('/api/chat/sessions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${auth.token}` },
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        agent_id: 'agent_skill_demo_b_diagnosis',
        title: 'G1-B 对话安装',
        origin: 'direct',
      }),
    });
    if (!response.ok) throw new Error(`failed to create G1-B session: ${response.status}`);
    const body = await response.json() as { id: string };
    return body.id;
  });
  await page.goto(`/workspace/chat/${sessionId}`);
  await expect(page).toHaveURL(new RegExp(`/workspace/chat/${sessionId}$`));
  await expect(page.getByRole('textbox', { name: /消息|发送/ })).toBeVisible();
  await page.getByRole('button', { name: '添加' }).click();
  await page.getByRole('menuitem', { name: '安装 Skill' }).click();
  const dialog = page.getByRole('dialog', { name: '安装 Skill 到当前分身' });
  await expect(dialog.getByLabel('安装 Skill GitHub 仓库地址')).toHaveValue('https://github.com/mattpocock/skills');
  await dialog.getByRole('button', { name: '生成安装预览' }).click();
  const card = dialog.getByRole('region', { name: 'Skill 安装确认卡' });
  await expect(card).toContainText('diagnosing-bugs');
  await expect(card).toContainText('contains_executable_content');

  await page.reload();
  await page.getByRole('button', { name: '选择本轮 Skill' }).click();
  await page.getByRole('menuitem', { name: '安装 Skill 到当前分身' }).click();
  const restored = page.getByRole('region', { name: 'Skill 安装确认卡' });
  await expect(restored).toContainText('awaiting_owner_confirmation');
  await restored.getByRole('button', { name: '确认安装到当前分身' }).click();
  await expect(restored).toContainText('installed');
  await dialog.getByRole('button', { name: '安装完成，开始使用' }).click();

  await page.getByRole('button', { name: '选择本轮 Skill' }).click();
  const installedSkill = page.getByRole('menuitem', { name: /diagnosing-bugs/ });
  await expect(installedSkill).toBeVisible();
  await installedSkill.click();
  await page.getByPlaceholder('输入消息，按 Enter 发送...').fill(
    '请使用本轮选定的指南，也就是刚安装的诊断 Skill 分析问题',
  );
  await page.getByRole('button', { name: '发送', exact: true }).click();
  await expect(page.getByRole('paragraph').filter({ hasText: 'G1-B-CONSUMED-SUCCESS' })).toBeVisible({
    timeout: 30_000,
  });
  const events = await page.evaluate(async (id) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch(`/api/chat/sessions/${id}/events?tenant_id=tenant_demo`, {
      headers: { Authorization: `Bearer ${auth.token}` },
    });
    return response.json() as Promise<Array<{ event_type: string; data?: Record<string, unknown> }>>;
  }, sessionId);
  expect(events.map((event) => event.event_type)).toEqual(expect.arrayContaining([
    'skill_load_started',
    'skill_loaded',
    'skill_use_completed',
  ]));
  expect(events.find((event) => event.event_type === 'skill_loaded')?.data).toMatchObject({
    selection_mode: 'forced',
  });
});

test('G1-C1 Agent 建议固定 GitHub Skill，本人批准后原分身真实消费', async ({ page }) => {
  await page.goto('/enterprise/dashboard');
  await loginAsMember(page);
  const executionId = await page.evaluate(async () => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const headers = { Authorization: `Bearer ${auth.token}`, 'Content-Type': 'application/json' };
    const sessionResponse = await fetch('/api/chat/sessions', {
      method: 'POST',
      headers,
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        agent_id: 'agent_skill_demo_c_test_first',
        title: 'G1-C1 Agent 远程提案',
        origin: 'owned',
      }),
    });
    const session = await sessionResponse.json() as { id: string };
    const response = await fetch('/api/chat/stream', {
      method: 'POST',
      headers,
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        session_id: session.id,
        agent_id: 'agent_skill_demo_c_test_first',
        client_turn_id: 'turn_g1_c1_remote',
        message: 'C1远程导入Skill：请建议从固定 GitHub commit 安装 TDD Skill，但必须让我确认',
        channel: 'web',
      }),
    });
    const body = await response.text();
    if (!response.ok || !body.includes('event: complete')) throw new Error(body);
    const events = await fetch(
      `/api/chat/sessions/${session.id}/events?tenant_id=tenant_demo`,
      { headers },
    ).then((eventResponse) => eventResponse.json()) as Array<{
      event_type: string;
      data?: Record<string, unknown>;
    }>;
    return String(
      events.find((event) => event.event_type === 'dynamic_task_delegated')?.data?.execution_id || '',
    );
  });
  expect(executionId).not.toBe('');
  const before = await page.evaluate(async () => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch(
      '/api/enterprise/general-skills?tenant_id=tenant_demo&agent_id=agent_skill_demo_c_test_first',
      { headers: { Authorization: `Bearer ${auth.token}` } },
    );
    return response.json() as Promise<Array<{ name: string }>>;
  });
  expect(before.filter((row) => row.name === 'tdd')).toHaveLength(0);

  await page.goto('/enterprise/work-items');
  await page.getByRole('button', { name: /审核并发布当前分身提出的 Skill/ }).first().click();
  const review = page.getByLabel('待审核 Skill 提案');
  await expect(review).toContainText('固定 GitHub Skill 导入建议');
  await expect(review).toContainText('mattpocock/skills@84fdeffd12f2ee307994d1eb6feb48173b6e0502');
  await expect(review).toContainText('批准前不会创建 Skill、Revision 或绑定');
  await expect(review).toContainText('tdd');
  await page.getByRole('dialog').getByRole('button', { name: '批准并发布' }).click();

  await expect.poll(async () => page.evaluate(async (id) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch(`/api/executions/${id}?tenant_id=tenant_demo`, {
      headers: { Authorization: `Bearer ${auth.token}` },
    });
    return (await response.json() as { status: string }).status;
  }, executionId), { timeout: 45_000 }).toBe('succeeded');
  const skill = await page.evaluate(async () => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch(
      '/api/enterprise/general-skills?tenant_id=tenant_demo&agent_id=agent_skill_demo_c_test_first',
      { headers: { Authorization: `Bearer ${auth.token}` } },
    );
    const rows = await response.json() as Array<{ id: string; name: string }>;
    return rows.find((row) => row.name === 'tdd');
  });
  expect(skill?.id).toBeTruthy();
  const consumed = await page.evaluate(async (skillId) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const headers = { Authorization: `Bearer ${auth.token}`, 'Content-Type': 'application/json' };
    const sessionResponse = await fetch('/api/chat/sessions', {
      method: 'POST',
      headers,
      body: JSON.stringify({
        tenant_id: 'tenant_demo', agent_id: 'agent_skill_demo_c_test_first', title: 'G1-C1 消费', origin: 'owned',
      }),
    });
    const session = await sessionResponse.json() as { id: string };
    const response = await fetch('/api/chat/stream', {
      method: 'POST',
      headers,
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        session_id: session.id,
        agent_id: 'agent_skill_demo_c_test_first',
        client_turn_id: 'turn_g1_c1_consume',
        message: '使用本轮选定的指南处理一个新增功能',
        channel: 'web',
        forced_general_skill_id: skillId,
      }),
    });
    const body = await response.text();
    const events = await fetch(
      `/api/chat/sessions/${session.id}/events?tenant_id=tenant_demo`,
      { headers },
    ).then((eventResponse) => eventResponse.json()) as Array<{ event_type: string }>;
    return { status: response.status, body, events };
  }, skill?.id);
  expect(consumed.status).toBe(200);
  expect(consumed.body).toContain('G1-C1-CONSUMED-SUCCESS');
  expect(consumed.events.map((event) => event.event_type)).toEqual(expect.arrayContaining([
    'skill_loaded', 'skill_use_completed',
  ]));
});

test('G1-C2 Agent 自主沉淀 Skill，经所有者确认后原分身真实消费', async ({ page }) => {
  await page.goto('/enterprise/dashboard');
  await loginAsMember(page);
  const executionId = await page.evaluate(async () => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const headers = { Authorization: `Bearer ${auth.token}`, 'Content-Type': 'application/json' };
    const sessionResponse = await fetch('/api/chat/sessions', {
      method: 'POST', headers,
      body: JSON.stringify({
        tenant_id: 'tenant_demo', agent_id: 'agent_skill_demo_c_test_first',
        title: 'G1-C2 Agent 自创 Skill', origin: 'owned',
      }),
    });
    const session = await sessionResponse.json() as { id?: string; detail?: string };
    if (!sessionResponse.ok || !session.id) throw new Error(session.detail || 'G1-C2 session failed');
    const stream = await fetch('/api/chat/stream', {
      method: 'POST', headers,
      body: JSON.stringify({
        tenant_id: 'tenant_demo', session_id: session.id,
        agent_id: 'agent_skill_demo_c_test_first', client_turn_id: 'turn_g1_c2_authored',
        message: 'S5创建Skill：沉淀退款证据复核方法并提交我确认，不能自行发布', channel: 'web',
      }),
    });
    const streamBody = await stream.text();
    if (!stream.ok || !streamBody.includes('event: complete')) throw new Error(streamBody);
    const events = await fetch(
      `/api/chat/sessions/${session.id}/events?tenant_id=tenant_demo`, { headers },
    ).then((response) => response.json()) as Array<{
      event_type: string; data?: Record<string, unknown>;
    }>;
    return String(
      events.find((event) => event.event_type === 'dynamic_task_delegated')?.data?.execution_id || '',
    );
  });
  expect(executionId).not.toBe('');
  await page.goto('/enterprise/work-items');
  await page.getByRole('button', { name: /审核并发布当前分身提出的 Skill/ }).first().click();
  const review = page.getByLabel('待审核 Skill 提案');
  await expect(review).toContainText('s5-refund-evidence-review');
  await expect(review).toContainText('S5-PROPOSAL-GUIDANCE');
  await expect(review).toContainText('无（不会获得新工具授权）');
  await page.getByRole('dialog').getByRole('button', { name: '批准并发布' }).click();
  await expect.poll(async () => page.evaluate(async (id) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    return fetch(`/api/executions/${id}?tenant_id=tenant_demo`, {
      headers: { Authorization: `Bearer ${auth.token}` },
    }).then((response) => response.json()).then((body: { status: string }) => body.status);
  }, executionId), { timeout: 45_000 }).toBe('succeeded');

  const consumed = await page.evaluate(async () => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const headers = { Authorization: `Bearer ${auth.token}`, 'Content-Type': 'application/json' };
    const skills = await fetch(
      '/api/enterprise/general-skills?tenant_id=tenant_demo&agent_id=agent_skill_demo_c_test_first',
      { headers },
    ).then((response) => response.json()) as Array<{ id: string; name: string }>;
    const skill = skills.find((item) => item.name === 's5-refund-evidence-review');
    if (!skill) throw new Error('G1-C2 published Skill missing');
    const session = await fetch('/api/chat/sessions', {
      method: 'POST', headers,
      body: JSON.stringify({
        tenant_id: 'tenant_demo', agent_id: 'agent_skill_demo_c_test_first',
        title: 'G1-C2 消费', origin: 'owned',
      }),
    }).then((response) => response.json()) as { id: string };
    const response = await fetch('/api/chat/stream', {
      method: 'POST', headers,
      body: JSON.stringify({
        tenant_id: 'tenant_demo', session_id: session.id,
        agent_id: 'agent_skill_demo_c_test_first', client_turn_id: 'turn_g1_c2_consume',
        message: '使用本轮选定的指南复核 CASE-G1-C2-001', channel: 'web',
        forced_general_skill_id: skill.id,
      }),
    });
    const body = await response.text();
    const events = await fetch(
      `/api/chat/sessions/${session.id}/events?tenant_id=tenant_demo`, { headers },
    ).then((eventResponse) => eventResponse.json()) as Array<{ event_type: string }>;
    return { status: response.status, body, events };
  });
  expect(consumed.status).toBe(200);
  expect(consumed.body).toContain('S5-CONSUMED-SUCCESS');
  expect(consumed.events.map((event) => event.event_type)).toEqual(expect.arrayContaining([
    'skill_loaded', 'skill_use_completed',
  ]));
});

test('G1-D 固定 GitHub Skill 经组织审核后由用户 B 主动采用并消费', async ({ page }) => {
  await page.goto('/enterprise/dashboard');
  await loginAsMember(page);
  const submitted = await page.evaluate(async () => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const headers = { Authorization: `Bearer ${auth.token}`, 'Content-Type': 'application/json' };
    let created = await fetch('/api/enterprise/general-skill-import-jobs', {
      method: 'POST',
      headers: { ...headers, 'Idempotency-Key': 'g1-d-import' },
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        target_agent_id: 'agent_skill_demo_d_publisher',
        source_kind: 'github',
        source_url: 'https://github.com/mattpocock/skills',
        revision: '84fdeffd12f2ee307994d1eb6feb48173b6e0502',
        source_subpath: 'skills/productivity/to-questionnaire',
      }),
    }).then((response) => response.json()) as {
      id: string; status: string; preview_checksum: string; row_version: number;
      candidates: Array<{ candidate_id: string }>;
    };
    for (let attempt = 0; attempt < 50 && created.status !== 'awaiting_approval'; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 100));
      created = await fetch(`/api/enterprise/general-skill-import-jobs/${created.id}`, { headers })
        .then((response) => response.json()) as typeof created;
    }
    if (created.status !== 'awaiting_approval') throw new Error(`G1-D preview ${created.status}`);
    const installed = await fetch(`/api/enterprise/general-skill-import-jobs/${created.id}/confirm`, {
      method: 'POST',
      headers,
      body: JSON.stringify({
        preview_checksum: created.preview_checksum,
        candidate_ids: created.candidates.map((item) => item.candidate_id),
        expected_row_version: created.row_version,
      }),
    }).then((response) => response.json()) as { installed_revision_ids: string[] };
    const skills = await fetch('/api/enterprise/my-general-skills', { headers })
      .then((response) => response.json()) as Array<{ id: string; name: string; row_version: number }>;
    const skill = skills.find((item) => item.name === 'to-questionnaire');
    if (!skill || !installed.installed_revision_ids.length) throw new Error('G1-D import missing');
    return fetch('/api/enterprise/publications', {
      method: 'POST',
      headers,
      body: JSON.stringify({
        resource_type: 'general_skill',
        resource_id: skill.id,
        expected_resource_revision: skill.row_version,
      }),
    }).then((response) => response.json()) as Promise<{
      id: string; attention_id: string; row_version: number; resource_id: string;
    }>;
  });

  const adminLoginStatus = await page.evaluate(async () => {
    const response = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        tenant_id: 'tenant_demo', username: 'publication-admin', password: 'publication-admin',
      }),
    });
    const body = await response.json();
    if (response.ok) localStorage.setItem('gongge_auth', JSON.stringify(body));
    return response.status;
  });
  expect(adminLoginStatus).toBe(200);
  await page.goto('/enterprise/work-items');
  await page.getByRole('button', { name: /审核组织发布：to-questionnaire/ }).click();
  const review = page.getByLabel('组织发布审核');
  await expect(review).toContainText('Skill 发布申请');
  await expect(review).toContainText('冻结快照');
  await page.getByRole('dialog').getByRole('button', { name: '批准发布到组织广场' }).click();

  const adopted = await page.evaluate(async (publishedResourceId) => {
    const login = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tenant_id: 'tenant_demo', username: 'member-two', password: 'member-two' }),
    });
    const auth = await login.json() as { token: string };
    localStorage.setItem('gongge_auth', JSON.stringify(auth));
    localStorage.setItem('gongge_enterprise_agent_scope', 'agent_skill_demo_d_adopter');
    const headers = { Authorization: `Bearer ${auth.token}`, 'Content-Type': 'application/json' };
    const releases = await fetch(
      '/api/enterprise/publications/releases?resource_type=general_skill', { headers },
    ).then((response) => response.json()) as Array<{
      id: string; resource_id: string; approved_revision_id: string;
    }>;
    const release = releases.find((item) => item.resource_id === publishedResourceId);
    if (!release) throw new Error('approved G1-D release absent');
    const adoption = await fetch(`/api/enterprise/publications/releases/${release.id}/adopt`, {
      method: 'POST',
      headers,
      body: JSON.stringify({
        target_agent_id: 'agent_skill_demo_d_adopter', idempotency_key: 'g1-d-adopt',
      }),
    }).then((response) => response.json()) as { binding_id: string };
    const session = await fetch('/api/chat/sessions', {
      method: 'POST',
      headers,
      body: JSON.stringify({
        tenant_id: 'tenant_demo', agent_id: 'agent_skill_demo_d_adopter', title: 'G1-D 消费', origin: 'owned',
      }),
    }).then((response) => response.json()) as { id: string };
    const catalog = await fetch(
      `/api/chat/sessions/${session.id}/general-skills?agent_id=agent_skill_demo_d_adopter`,
      { headers },
    ).then((response) => response.json()) as { items?: Array<{ skill_id: string }> };
    const response = await fetch('/api/chat/stream', {
      method: 'POST',
      headers,
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        session_id: session.id,
        agent_id: 'agent_skill_demo_d_adopter',
        client_turn_id: 'turn_g1_d_consume',
        message: '使用本轮选定的指南生成问卷',
        channel: 'web',
        forced_general_skill_id: publishedResourceId,
      }),
    });
    const body = await response.text();
    const events = await fetch(
      `/api/chat/sessions/${session.id}/events?tenant_id=tenant_demo`, { headers },
    ).then((eventResponse) => eventResponse.json()) as Array<{ event_type: string }>;
    return { adoption, release, catalog, status: response.status, body, events };
  }, submitted.resource_id);
  expect(adopted.adoption.binding_id).toBeTruthy();
  expect(adopted.catalog.items).toEqual(expect.arrayContaining([
    expect.objectContaining({ skill_id: submitted.resource_id }),
  ]));
  expect(adopted.status).toBe(200);
  expect(adopted.body).toContain('G1-D-CONSUMED-SUCCESS');
  expect(adopted.events.map((event) => event.event_type)).toEqual(expect.arrayContaining([
    'skill_loaded', 'skill_use_completed',
  ]));
});

test('G1.4 整 Agent 变更后旧申请失效，B 从已审冻结快照采用第五个员工', async ({ page }) => {
  await page.goto('/enterprise/dashboard');
  await loginAsMember(page);
  await page.evaluate(async () => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const headers = { Authorization: `Bearer ${auth.token}`, 'Content-Type': 'application/json' };
    const existing = await fetch(
      '/api/enterprise/general-skills?tenant_id=tenant_demo&agent_id=agent_skill_demo_c_test_first',
      { headers },
    ).then((response) => response.json()) as Array<{ name: string }>;
    if (existing.some((item) => item.name === 'tdd')) return;
    let job = await fetch('/api/enterprise/general-skill-import-jobs', {
      method: 'POST',
      headers: { ...headers, 'Idempotency-Key': 'g1-4-tdd-import' },
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        target_agent_id: 'agent_skill_demo_c_test_first',
        source_kind: 'github',
        source_url: 'https://github.com/mattpocock/skills',
        revision: '84fdeffd12f2ee307994d1eb6feb48173b6e0502',
        source_subpath: 'skills/engineering/tdd',
      }),
    }).then((response) => response.json()) as {
      id: string; status: string; preview_checksum: string; row_version: number;
      candidates: Array<{ candidate_id: string }>;
    };
    for (let attempt = 0; attempt < 50 && job.status !== 'awaiting_approval'; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 100));
      job = await fetch(`/api/enterprise/general-skill-import-jobs/${job.id}`, { headers })
        .then((response) => response.json()) as typeof job;
    }
    if (job.status !== 'awaiting_approval') throw new Error(`G1.4 TDD preview ${job.status}`);
    const confirmed = await fetch(`/api/enterprise/general-skill-import-jobs/${job.id}/confirm`, {
      method: 'POST', headers,
      body: JSON.stringify({
        preview_checksum: job.preview_checksum,
        candidate_ids: job.candidates.map((item) => item.candidate_id),
        expected_row_version: job.row_version,
      }),
    });
    if (!confirmed.ok) throw new Error(`G1.4 TDD confirm ${confirmed.status}: ${await confirmed.text()}`);
  });
  const authoredExecutionId = await page.evaluate(async () => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const headers = { Authorization: `Bearer ${auth.token}`, 'Content-Type': 'application/json' };
    const existing = await fetch(
      '/api/enterprise/general-skills?tenant_id=tenant_demo&agent_id=agent_skill_demo_c_test_first',
      { headers },
    ).then((response) => response.json()) as Array<{ name: string }>;
    if (existing.some((item) => item.name === 's5-refund-evidence-review')) return '';
    const sessionResponse = await fetch('/api/chat/sessions', {
      method: 'POST', headers,
      body: JSON.stringify({
        tenant_id: 'tenant_demo', agent_id: 'agent_skill_demo_c_test_first',
        title: 'G1.4 Agent 自创 Skill', origin: 'owned',
      }),
    });
    const session = await sessionResponse.json() as { id?: string; detail?: string };
    if (!sessionResponse.ok || !session.id) throw new Error(session.detail || 'G1.4 C2 session failed');
    const stream = await fetch('/api/chat/stream', {
      method: 'POST', headers,
      body: JSON.stringify({
        tenant_id: 'tenant_demo', session_id: session.id, agent_id: 'agent_skill_demo_c_test_first',
        client_turn_id: 'turn_g1_4_authored',
        message: 'S5创建Skill：G1.4整员工发布前，沉淀退款证据复核方法并提交我确认',
        channel: 'web',
      }),
    });
    const streamBody = await stream.text();
    if (!stream.ok || !streamBody.includes('event: complete')) throw new Error(streamBody);
    const events = await fetch(
      `/api/chat/sessions/${session.id}/events?tenant_id=tenant_demo`, { headers },
    ).then((response) => response.json()) as Array<{
      event_type: string; data?: Record<string, unknown>;
    }>;
    return String(
      events.find((event) => event.event_type === 'dynamic_task_delegated')?.data?.execution_id || '',
    );
  });
  if (authoredExecutionId) {
    await page.goto('/enterprise/work-items');
    await page.getByRole('button', { name: /审核并发布当前分身提出的 Skill/ }).first().click();
    const authoredReview = page.getByLabel('待审核 Skill 提案');
    await expect(authoredReview).toContainText('s5-refund-evidence-review');
    await expect(authoredReview).toContainText('S5-PROPOSAL-GUIDANCE');
    await page.getByRole('dialog').getByRole('button', { name: '批准并发布' }).click();
    await expect.poll(async () => page.evaluate(async (executionId) => {
      const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
      const response = await fetch(`/api/executions/${executionId}?tenant_id=tenant_demo`, {
        headers: { Authorization: `Bearer ${auth.token}` },
      });
      return (await response.json() as { status: string }).status;
    }, authoredExecutionId), { timeout: 45_000 }).toBe('succeeded');
  }
  await page.goto('/enterprise/agents');
  const sourceCard = page.locator('.gongge-employee-card').filter({ hasText: 'Skill演示C｜测试先行分身' });
  await sourceCard.getByRole('button', { name: '员工操作' }).click();
  await page.getByRole('menuitem', { name: '提交组织审核' }).click();

  const firstRequest = await page.evaluate(async () => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const headers = { Authorization: `Bearer ${auth.token}`, 'Content-Type': 'application/json' };
    const agent = await fetch('/api/enterprise/agents/agent_skill_demo_c_test_first?tenant_id=tenant_demo', { headers })
      .then((response) => response.json()) as {
        profile_revision: number; metadata: Record<string, unknown>;
      };
    const changed = await fetch('/api/enterprise/agents/agent_skill_demo_c_test_first', {
      method: 'PUT',
      headers,
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        description: 'G1.4 申请后改变 Persona，旧冻结快照必须失效',
        metadata: agent.metadata,
      }),
    }).then((response) => response.json()) as { profile_revision: number };
    const pageData = await fetch(
      '/api/attention-items?tenant_id=tenant_demo&view=active&page=1&page_size=100',
      { headers },
    ).then((response) => response.json()) as { items: Array<{
      id: string; revision: number; payload?: Record<string, unknown>;
    }> };
    const item = pageData.items.find((row) => row.payload?.resource_id === 'agent_skill_demo_c_test_first');
    if (!item) throw new Error('G1.4 publication work item missing');
    return { item, changed };
  });

  const staleStatus = await page.evaluate(async (item) => {
    const login = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        tenant_id: 'tenant_demo', username: 'publication-admin', password: 'publication-admin',
      }),
    });
    const auth = await login.json() as { token: string };
    localStorage.setItem('gongge_auth', JSON.stringify(auth));
    const requestId = String(item.payload?.publication_request_id || '');
    const response = await fetch(`/api/enterprise/publications/${requestId}/review`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${auth.token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        command_id: 'g1-4-stale-review',
        command: 'approve',
        expected_request_row_version: Number(item.payload?.request_row_version || 1),
        expected_attention_revision: item.revision,
      }),
    });
    return { status: response.status, body: await response.json() };
  }, firstRequest.item);
  expect(staleStatus.status).toBe(409);
  expect(staleStatus.body.detail.code).toBe('PUBLICATION_SNAPSHOT_STALE');

  await page.evaluate(async () => {
    const login = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tenant_id: 'tenant_demo', username: 'member', password: 'member' }),
    });
    localStorage.setItem('gongge_auth', JSON.stringify(await login.json()));
  });
  await page.goto('/enterprise/agents');
  const changedCard = page.locator('.gongge-employee-card').filter({ hasText: 'Skill演示C｜测试先行分身' });
  await changedCard.getByRole('button', { name: '员工操作' }).click();
  await page.getByRole('menuitem', { name: '提交组织审核' }).click();

  await page.evaluate(async () => {
    const login = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        tenant_id: 'tenant_demo', username: 'publication-admin', password: 'publication-admin',
      }),
    });
    localStorage.setItem('gongge_auth', JSON.stringify(await login.json()));
  });
  await page.goto('/enterprise/work-items');
  await page.getByRole('button', { name: /审核组织发布：Skill演示C｜测试先行分身/ }).click();
  const review = page.getByLabel('组织发布审核');
  await expect(review).toContainText('整 Agent 发布申请');
  await page.getByRole('dialog').getByRole('button', { name: '批准发布到组织广场' }).click();

  await page.evaluate(async () => {
    const login = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tenant_id: 'tenant_demo', username: 'member-two', password: 'member-two' }),
    });
    localStorage.setItem('gongge_auth', JSON.stringify(await login.json()));
  });
  await page.goto('/enterprise/agents');
  await page.getByRole('button', { name: '组织数字员工发布库' }).click();
  const gallery = page.getByRole('dialog', { name: /组织数字员工发布库/ });
  const releaseCard = gallery.getByRole('article').filter({ hasText: 'Skill演示C｜测试先行分身' });
  await expect(releaseCard).toContainText('已审 Release');
  await releaseCard.getByRole('button', { name: '采用为我的员工' }).click();
  await expect(page.locator('.gongge-employee-card').filter({
    hasText: 'Skill演示C｜测试先行分身（采用）',
  })).toBeVisible();

  const adoptionFacts = await page.evaluate(async () => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const headers = { Authorization: `Bearer ${auth.token}`, 'Content-Type': 'application/json' };
    const pageData = await fetch(
      '/api/enterprise/agents/management-page?tenant_id=tenant_demo&view=all&page=1&page_size=48',
      { headers },
    ).then(async (response) => {
      const body = await response.json();
      if (!response.ok) throw new Error(`G1.4 agent page ${response.status}: ${JSON.stringify(body)}`);
      return body;
    }) as { items: Array<{
      id: string; name: string; source_agent_id?: string; source_agent_version?: string;
    }> };
    const adopted = pageData.items.find((item) => item.name === 'Skill演示C｜测试先行分身（采用）');
    if (!adopted) throw new Error('G1.4 adopted agent missing');
    const session = await fetch('/api/chat/sessions', {
      method: 'POST', headers,
      body: JSON.stringify({
        tenant_id: 'tenant_demo', agent_id: adopted.id, title: 'G1.4 adopted runtime', origin: 'owned',
      }),
    }).then((response) => response.json()) as { id: string };
    const catalog = await fetch(
      `/api/chat/sessions/${session.id}/general-skills?agent_id=${adopted.id}`,
      { headers },
    ).then((response) => response.json()) as { items: Array<{ skill_id: string; name: string }> };
    const tdd = catalog.items.find((item) => item.name === 'tdd');
    const authored = catalog.items.find((item) => item.name === 's5-refund-evidence-review');
    if (!tdd) throw new Error('G1.4 adopted tdd missing');
    if (!authored) throw new Error('G1.4 adopted authored Skill missing');
    const tddResponse = await fetch('/api/chat/stream', {
      method: 'POST', headers,
      body: JSON.stringify({
        tenant_id: 'tenant_demo', session_id: session.id, agent_id: adopted.id,
        client_turn_id: 'turn_g1_4_adopted', message: '本轮选定的指南：使用固定 TDD 指南完成回归', channel: 'web',
        forced_general_skill_id: tdd.skill_id,
      }),
    });
    const tddBody = await tddResponse.text();
    const authoredResponse = await fetch('/api/chat/stream', {
      method: 'POST', headers,
      body: JSON.stringify({
        tenant_id: 'tenant_demo', session_id: session.id, agent_id: adopted.id,
        client_turn_id: 'turn_g1_4_authored_adopted',
        message: '使用本轮选定的指南复核 CASE-G1-4-C2', channel: 'web',
        forced_general_skill_id: authored.skill_id,
      }),
    });
    return {
      adopted,
      catalog,
      tddStatus: tddResponse.status,
      tddBody,
      authoredStatus: authoredResponse.status,
      authoredBody: await authoredResponse.text(),
    };
  });
  expect(adoptionFacts.adopted.source_agent_id).toBe('agent_skill_demo_c_test_first');
  expect(adoptionFacts.adopted.source_agent_version).toMatch(/^[a-f0-9]{64}$/);
  expect(adoptionFacts.catalog.items.map((item) => item.name)).toContain('tdd');
  expect(adoptionFacts.catalog.items.map((item) => item.name)).toContain('s5-refund-evidence-review');
  expect(adoptionFacts.tddStatus).toBe(200);
  expect(adoptionFacts.tddBody).toContain('G1-C1-CONSUMED-SUCCESS');
  expect(adoptionFacts.authoredStatus).toBe(200);
  expect(adoptionFacts.authoredBody).toContain('S5-CONSUMED-SUCCESS');
});

test('G1 反向权限：采用者不能查看或把所有者其他私有 Skill 绑定到本人分身', async ({ page }) => {
  await page.goto('/enterprise/dashboard');
  const privateSkill = await page.evaluate(async () => {
    const login = await fetch('/api/auth/login', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tenant_id: 'tenant_demo', username: 'member', password: 'member' }),
    });
    const auth = await login.json() as { token: string };
    const skills = await fetch('/api/enterprise/my-general-skills', {
      headers: { Authorization: `Bearer ${auth.token}` },
    }).then((response) => response.json()) as Array<{
      id: string; name: string; current_revision_id: string;
    }>;
    const target = skills.find((item) => item.name === 'writing-for-agents');
    if (!target) throw new Error('owner private Skill missing');
    return target;
  });

  const denied = await page.evaluate(async (target) => {
    const login = await fetch('/api/auth/login', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tenant_id: 'tenant_demo', username: 'member-two', password: 'member-two' }),
    });
    const auth = await login.json() as { token: string };
    localStorage.setItem('gongge_auth', JSON.stringify(auth));
    const headers = { Authorization: `Bearer ${auth.token}`, 'Content-Type': 'application/json' };
    const library = await fetch('/api/enterprise/my-general-skills', { headers })
      .then((response) => response.json()) as Array<{ id: string; name: string }>;
    const response = await fetch('/api/enterprise/general-skill-governance/bindings', {
      method: 'POST', headers,
      body: JSON.stringify({
        agent_id: 'agent_skill_demo_d_adopter',
        skill_id: target.id,
        revision_policy: 'pinned',
        pinned_revision_id: target.current_revision_id,
        invocation_policy: 'model_allowed',
      }),
    });
    return { library, status: response.status, body: await response.json() };
  }, privateSkill);
  expect(denied.library.map((item) => item.id)).not.toContain(privateSkill.id);
  expect(denied.status).toBe(403);
  expect(denied.body.detail.code).toBe('GENERAL_SKILL_FORBIDDEN');
});

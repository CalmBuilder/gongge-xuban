/**
 * @Time       : 2026/09/02
 * @Author     : zhanglp8181
 * @File       : vertical-ability.fullstack.e2e.ts
 * @CallChain  : 前台登录页 → Composer 能力包 → chat stream → DynamicTaskAgent Execution
 * @Description: 验证平台内置垂直能力对所有登录用户可见，并能经统一 Runtime 完成一次契约闭环。
 */

import { expect, test, type Page } from '@playwright/test';
import type { VerticalAbilityRelayPairing } from '../src/types/verticalAbilityConnections';

const TENANT_ID = 'tenant_demo';
const PUBLIC_AGENT_ID = 'agent_e2e_gallery';

type AuthSession = {
  token?: string;
  user?: { id?: string; username?: string };
};

type ChatEvent = {
  event_type?: string;
  data?: Record<string, unknown>;
};

async function loginThroughUi(page: Page, username: string, password: string): Promise<AuthSession> {
  /** 从真实产品入口完成登录，禁止用预置 token 替代登录页面。 */

  await page.goto('/');
  const accountMenu = page.getByRole('button', { name: '账户菜单' });
  const enterPlatform = page.getByRole('button', { name: '进入平台' });
  await expect(accountMenu.or(enterPlatform).first()).toBeVisible({ timeout: 30_000 });
  if (await accountMenu.isVisible()) {
    // 同一页面在管理员配置后切换普通用户时，优先走产品真实退出流程，避免旧 context 请求与存储清理竞态。
    await accountMenu.click();
    await page.getByRole('menuitem', { name: '退出登录' }).click();
    await expect(enterPlatform).toBeVisible({ timeout: 15_000 });
  }
  await page.evaluate(() => {
    localStorage.setItem('gongge_onboarding_guide_seen', '1');
    localStorage.setItem('gongge_quick_start_guide_seen', '1');
  });
  // 等待真实认证界面稳定并走退出流程，不用清空存储/重载与 React 认证恢复竞速。
  await enterPlatform.click({ timeout: 15_000 });
  await page.getByRole('textbox', { name: '账号' }).fill(username);
  await page.getByRole('textbox', { name: '密码', exact: true }).fill(password);
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect.poll(
    async () => page.evaluate((expectedUsername) => {
      const raw = localStorage.getItem('gongge_auth');
      if (!raw) return '';
      const session = JSON.parse(raw) as AuthSession;
      return session.user?.username === expectedUsername ? session.user.id || '' : '';
    }, username),
    { timeout: 30_000 },
  ).not.toBe('');
  return page.evaluate(() => {
    const raw = localStorage.getItem('gongge_auth');
    if (!raw) throw new Error('登录成功后未找到认证会话');
    return JSON.parse(raw) as AuthSession;
  });
}

async function readJson<T>(page: Page, path: string): Promise<T> {
  /** 使用当前浏览器认证上下文读取受保护的目录或 Execution 投影。 */

  return page.evaluate(async (requestPath) => {
    const raw = localStorage.getItem('gongge_auth');
    const session = raw ? JSON.parse(raw) as AuthSession : {};
    const response = await fetch(requestPath, {
      headers: session.token ? { Authorization: `Bearer ${session.token}` } : {},
    });
    const body = await response.json();
    if (!response.ok) throw new Error(`请求失败 ${response.status}: ${requestPath}`);
    return body as T;
  }, path);
}

async function readSessionEvents(page: Page, sessionId: string): Promise<ChatEvent[]> {
  /** 读取服务端事件账本，确认本轮不是只有前端乐观状态。 */

  return readJson<ChatEvent[]>(
    page,
    `/api/chat/sessions/${sessionId}/events?tenant_id=${TENANT_ID}`,
  );
}

async function waitForSession(page: Page): Promise<string> {
  /** 等待草稿会话经真实 SSE 提升为正式会话。 */

  await expect.poll(
    () => page.url(),
    { timeout: 60_000, intervals: [250, 500, 1_000, 2_000] },
  ).toMatch(/\/workspace\/chat\/session_[^/?#]+$/);
  const sessionId = page.url().match(/\/workspace\/chat\/(session_[^/?#]+)$/)?.[1];
  if (!sessionId) throw new Error('能力包草稿会话没有提升为正式会话');
  return sessionId;
}

async function waitForAssistant(page: Page, sessionId: string): Promise<void> {
  /** 等待最终助手消息落账，避免把流式片段误当成 Execution 结果。 */

  await expect.poll(
    async () => {
      const rows = await readJson<Array<{ role?: string }>>(
        page,
        `/api/chat/sessions/${sessionId}/messages?tenant_id=${TENANT_ID}`,
      );
      return rows.filter((item) => item.role === 'assistant').length;
    },
    { timeout: 90_000, intervals: [250, 500, 1_000, 2_000] },
  ).toBe(1);
}

async function selectWebIntelligence(
  page: Page,
  options: { dismissMissingConfiguration?: boolean } = {},
): Promise<void> {
  /** 在 Composer 的通用能力包弹窗中选择全网情报员，而不是点击专属按钮。 */

  const trigger = page.getByRole('button', { name: '选择垂直能力包' });
  await expect(trigger).toBeVisible({ timeout: 30_000 });
  await trigger.click();
  const item = page.getByRole('menuitemcheckbox', { name: /全网情报员/ }).first();
  await expect(item).toBeVisible();
  const readinessResponse = page.waitForResponse((response) => (
    response.request().method() === 'GET'
    && new URL(response.url()).pathname === '/api/chat/abilities/web-intelligence/readiness'
  ));
  await item.click();
  await readinessResponse;
  await page.waitForTimeout(0);
  const missingDialog = page.getByRole('dialog').filter({ hasText: 'Jina API Key 尚未配置' });
  if (await missingDialog.isVisible().catch(() => false)) {
    await page.getByRole('button', { name: '关闭提示' }).click();
    await expect(missingDialog).not.toBeVisible();
  }
  if (!options.dismissMissingConfiguration) await page.keyboard.press('Escape');
  await expect(page.locator('button[aria-label="选择垂直能力包"]')).toContainText('能力包：全网情报员');
  await expect(page.locator('button[aria-label="选择 DynamicTaskAgent 复杂任务引擎"]'))
    .toHaveAttribute('aria-pressed', 'true');
}

async function configureJinaForExecution(page: Page): Promise<void> {
  /** 通过管理员真实页面准备后续 Execution 所需的 Provider，避免依赖测试执行顺序。 */

  await loginThroughUi(page, 'admin', 'admin');
  await page.goto('/enterprise/vertical-abilities/web-intelligence');
  await expect(page.getByTestId('vertical-ability-settings-detail-page')).toBeVisible({ timeout: 30_000 });
  const input = page.getByLabel('Jina API Key');
  await input.fill('e2e-jina-provider-fixture');
  await page.getByTestId('vertical-provider-card-jina').getByRole('button', { name: '保存凭据' }).click();
  await expect(page.getByText('Jina API Key 已保存，后续对话可直接使用全网情报员')).toBeVisible();
}

async function openPublicAgentChat(page: Page): Promise<void> {
  /** 从数字员工广场建立当前用户的使用关系，再进入可用的聊天草稿。 */

  await page.goto('/enterprise/platform/agents');
  await expect(page.getByRole('heading', { name: '数字员工' }).first()).toBeVisible({ timeout: 30_000 });
  const card = page.locator('.gongge-employee-card').filter({ hasText: 'E2E 企业广场员工' }).first();
  await expect(card).toBeVisible({ timeout: 30_000 });
  await card.click();
  await page.getByRole('button', { name: '添加使用并开始对话' }).click();
  await expect(page).toHaveURL(new RegExp(`/workspace/chat/draft/${PUBLIC_AGENT_ID}$`));
}

test.describe.configure({ mode: 'serial', timeout: 300_000 });

test('普通用户选中未配置的全网情报员后可关闭提示，但再次发送会重新提醒', async ({ page }) => {
  /** 验证缺少 Provider 只触发 Composer 提示，不创建失败的 DynamicTaskAgent Execution。 */

  await loginThroughUi(page, 'member', 'member');
  await openPublicAgentChat(page);
  await selectWebIntelligence(page, { dismissMissingConfiguration: true });
  await expect(page.getByText('Jina Reader待配置')).toBeVisible();

  const streamRequests: string[] = [];
  page.on('request', (request) => {
    if (request.method() === 'POST' && new URL(request.url()).pathname === '/api/chat/stream') {
      streamRequests.push(request.url());
    }
  });
  const composer = page.getByPlaceholder('输入消息，按 Enter 发送...');
  await composer.fill('缺少配置时不应创建执行');
  await page.getByRole('button', { name: '发送', exact: true }).click();
  await expect(page.getByRole('dialog')).toContainText('Jina API Key 尚未配置');
  expect(streamRequests).toHaveLength(0);
  await expect(page).toHaveURL(new RegExp(`/workspace/chat/draft/${PUBLIC_AGENT_ID}$`));
});

test('管理员从真实登录页配置平台 Jina Key，保存后不回显完整密钥', async ({ page }) => {
  /** 验证前台登录、配置页、加密写入 API 和掩码状态组成真实浏览器闭环。 */

  const rawKey = 'browser-jina-key-not-rendered';
  await loginThroughUi(page, 'admin', 'admin');
  await page.goto('/enterprise/vertical-abilities');
  await expect(page.getByTestId('vertical-ability-settings-page')).toBeVisible();
  await expect(page.getByRole('heading', { name: '垂直能力包' })).toBeVisible();
  await page.getByTestId('vertical-ability-card-web-intelligence').click();
  await expect(page.getByTestId('vertical-ability-settings-detail-page')).toBeVisible();
  await expect(page.getByTestId('vertical-provider-card-jina')).toBeVisible();
  await expect(page.getByTestId('vertical-provider-card-groq')).toBeVisible();
  await expect(page.getByTestId('vertical-provider-card-openai')).toBeVisible();
  await expect(page.getByTestId('vertical-resource-card-youtube-runtime')).toBeVisible();
  await expect(page.getByTestId('vertical-resource-card-groq-whisper')).toBeVisible();
  await expect(page.getByTestId('vertical-resource-card-openai-whisper')).toBeVisible();
  await expect(page.getByTestId('vertical-provider-card-groq').getByRole('link', { name: /Groq API Keys/ }))
    .toHaveAttribute('href', 'https://console.groq.com/keys');
  await expect(page.getByTestId('vertical-provider-card-openai').getByRole('link', { name: /OpenAI API Keys/ }))
    .toHaveAttribute('href', 'https://platform.openai.com/api-keys');
  const input = page.getByLabel('Jina API Key');
  await expect(input).toBeVisible();
  await input.fill(rawKey);
  await page.getByTestId('vertical-provider-card-jina').getByRole('button', { name: '保存凭据' }).click();
  await expect(page.getByText('Jina API Key 已保存，后续对话可直接使用全网情报员')).toBeVisible();
  await expect(page.getByText('已配置').first()).toBeVisible();
  await expect(input).toHaveValue('');
  await expect(page.locator('body')).not.toContainText(rawKey);

  const stored = await readJson<{ configured: boolean; source: string; revision: number }>(
    page,
    '/api/enterprise/vertical-ability-settings/jina',
  );
  expect(stored).toMatchObject({ configured: true, source: 'database', revision: 1 });

  const groqCard = page.getByTestId('vertical-provider-card-groq');
  const groqInput = groqCard.getByLabel('Groq API Key');
  const groqKey = 'fixture-provider-value-not-rendered';
  await groqInput.fill(groqKey);
  await groqCard.getByRole('button', { name: '保存凭据' }).click();
  await expect(page.getByText('Groq API Key 已保存，后续对话可直接使用全网情报员')).toBeVisible();
  await expect(groqInput).toHaveValue('');
  await expect(page.locator('body')).not.toContainText(groqKey);
  const storedGroq = await readJson<{ configured: boolean; source: string; revision: number }>(
    page,
    '/api/enterprise/vertical-ability-settings/groq',
  );
  expect(storedGroq).toMatchObject({ configured: true, source: 'database', revision: 1 });
});

test('两个普通用户从真实登录页看到同一个公共垂直能力目录', async ({ browser }) => {
  /** 证明已提交的平台内置能力不因登录用户不同而消失或置灰。 */

  for (const account of [
    { username: 'member', password: 'member' },
    { username: 'other-member', password: 'other-member' },
  ]) {
    const context = await browser.newContext();
    const page = await context.newPage();
    try {
      await loginThroughUi(page, account.username, account.password);
      const catalog = await readJson<{ items: Array<{ id: string; name: string }> }>(
        page,
        '/api/chat/abilities',
      );
      expect(catalog.items.some((item) => (
        item.id === 'web-intelligence' && item.name === '全网情报员'
      ))).toBe(true);
      await openPublicAgentChat(page);
      await selectWebIntelligence(page);
    } finally {
      await context.close();
    }
  }
});

test('普通用户从真实对话框打开网站连接中心并选择已登录小红书浏览器会话', async ({ page }, testInfo) => {
  /** 验证小红书登录态采用平台专属浏览器会话，不出现通用用户名密码表单。 */

  await loginThroughUi(page, 'member', 'member');
  await openPublicAgentChat(page);
  await page.getByRole('button', { name: '选择垂直能力包' }).click();
  await page.getByRole('menuitem', { name: '配置网站连接' }).click();

  const dialog = page.getByRole('dialog').filter({ hasText: '网站连接中心' });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText('9 个平台');
  const xiaohongshu = dialog.getByRole('button', { name: /小红书/ }).first();
  await expect(xiaohongshu).toBeVisible();
  await xiaohongshu.click();
  await expect(dialog).toContainText('连接已登录浏览器');
  await expect(dialog.getByRole('radio', { name: /本机连接.*local_loopback/ })).toBeVisible();
  await expect(dialog.getByRole('radio', { name: /宿主机连接.*browser_relay/ })).toBeVisible();
  const localTransport = dialog.getByRole('radio', { name: /本机连接.*local_loopback/ });
  const relayTransport = dialog.getByRole('radio', { name: /宿主机连接.*browser_relay/ });
  await relayTransport.click();
  await expect(relayTransport).toHaveAttribute('aria-checked', 'true');
  await expect(localTransport).toHaveAttribute('aria-checked', 'false');
  await expect(dialog.getByRole('button', { name: '生成宿主机配对' })).toBeVisible();
  await expect(dialog.getByLabel('用户名')).toHaveCount(0);
  await expect(dialog.getByLabel('密码')).toHaveCount(0);
  await expect(dialog.getByRole('link', { name: /打开.*小红书/ })).toHaveAttribute(
    'href',
    'https://www.xiaohongshu.com/',
  );
  await page.screenshot({ path: testInfo.outputPath('website-connection-xiaohongshu.png'), fullPage: true });
});

for (const transport of ['local_loopback', 'browser_relay'] as const) {
  test(`浏览器设备配对端到端保持真实传输模式：${transport}`, async ({ page }, testInfo) => {
    /** 从登录页验证两种设备授权、页面撤销和旧令牌拒绝，不把 API 心跳模拟当作真实浏览器扩展。 */

    const auth = await loginThroughUi(page, 'member', 'member');
    await openPublicAgentChat(page);
    await page.getByRole('button', { name: '选择垂直能力包' }).click();
    await page.getByRole('menuitem', { name: '配置网站连接' }).click();
    const dialog = page.getByRole('dialog').filter({ hasText: '网站连接中心' });
    await dialog.getByRole('button', { name: /小红书/ }).first().click();
    await dialog.getByRole('radio', { name: new RegExp(transport) }).click();
    const responsePromise = page.waitForResponse((response) => (
      response.request().method() === 'POST'
      && new URL(response.url()).pathname === '/api/chat/vertical-ability-connections/browser-relay/pairings'
    ));
    await dialog.getByRole('button', {
      name: transport === 'local_loopback' ? '连接本机浏览器' : '生成宿主机配对',
    }).click();
    const response = await responsePromise;
    expect(response.status()).toBe(201);
    expect(response.request().postDataJSON().transport).toBe(transport);
    const pairing: VerticalAbilityRelayPairing = await response.json();
    expect(pairing.transport).toBe(transport);
    expect(pairing.status).toBe('pending');
    expect(Boolean(pairing.pairing_token)).toBe(true);
    await expect(dialog.getByText(
      transport === 'local_loopback' ? '本机浏览器安全配对' : '宿主机浏览器 Relay',
      { exact: true },
    )).toBeVisible();
    const path = `/api/chat/vertical-ability-connections/browser-relay/pairings/${pairing.session_id}`;
    const persisted = await readJson<VerticalAbilityRelayPairing>(page, path);
    expect(persisted.transport).toBe(transport);
    expect(persisted.pairing_token).toBeNull();
    await page.screenshot({
      path: testInfo.outputPath(`browser-pairing-${transport}.png`),
      fullPage: true, mask: [dialog.locator('code')],
    });
    if (!pairing.pairing_token) throw new Error('配对未返回设备令牌');
    const deviceHeaders = { Authorization: `Bearer ${pairing.pairing_token}` };
    // 使用真实后端心跳协议模拟连接器；不访问外部网站，也不绕过前台登录及设备配对。
    const heartbeat = await page.request.post('/api/browser-relay/heartbeat', { headers: deviceHeaders });
    expect(heartbeat.status()).toBe(200);
    await expect(dialog.getByRole('button', { name: '撤销连接', exact: true })).toBeEnabled({ timeout: 10_000 });
    const revocation = page.waitForResponse((result) => (
      result.request().method() === 'DELETE'
      && /^\/api\/chat\/vertical-ability-connections\/vaconnection_/.test(new URL(result.url()).pathname)
    ));
    await dialog.getByRole('button', { name: '撤销连接', exact: true }).click();
    expect((await revocation).status()).toBe(200);
    // 目录 API 不返回 revoked 连接，页面显示未连接；服务端撤销事实和旧令牌拒绝单独严格核验。
    await expect(dialog.getByRole('button', { name: /小红书/ }).first()).toContainText('未连接');
    const staleHeartbeat = await page.request.post('/api/browser-relay/heartbeat', { headers: deviceHeaders });
    expect(staleHeartbeat.status()).toBe(401);
    expect((await readJson<VerticalAbilityRelayPairing>(page, path)).status).toBe('revoked');
    await page.screenshot({
      path: testInfo.outputPath(`browser-revoked-${transport}.png`),
      fullPage: true, mask: [dialog.locator('code')],
    });
    const revoked = await page.request.delete(path, {
      headers: { Authorization: `Bearer ${auth.token}` },
    });
    expect(revoked.status()).toBe(200);
  });
}

test('普通用户选择全网情报员后由统一 DynamicTaskAgent 创建并完成 Execution', async ({ page }) => {
  /** 验证选择、请求、快照、结果和持久事件组成真实前台闭环。 */

  const browserFailures: string[] = [];
  page.on('pageerror', (error) => browserFailures.push(`pageerror: ${error.message}`));
  page.on('response', (response) => {
    if (response.status() >= 500) {
      browserFailures.push(`${response.status()} ${response.url()}`);
    }
  });

  await configureJinaForExecution(page);
  await loginThroughUi(page, 'member', 'member');
  await openPublicAgentChat(page);
  await selectWebIntelligence(page);

  const composer = page.getByPlaceholder('输入消息，按 Enter 发送...');
  await composer.fill('VERTICAL-ABILITY-E2E：请完成能力包契约验收报告。');
  const streamResponse = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && new URL(response.url()).pathname === '/api/chat/stream'
  ));
  await page.getByRole('button', { name: '发送', exact: true }).click();
  const request = await streamResponse;
  expect(request.status()).toBe(200);

  const sessionId = await waitForSession(page);
  await waitForAssistant(page, sessionId);
  const messages = await readJson<Array<{ role?: string; content?: string }>>(
    page,
    `/api/chat/sessions/${sessionId}/messages?tenant_id=${TENANT_ID}`,
  );
  expect(messages.find((item) => item.role === 'assistant')?.content).toContain(
    'VERTICAL-ABILITY-E2E',
  );

  const events = await readSessionEvents(page, sessionId);
  const delegated = events.find((item) => item.event_type === 'dynamic_task_delegated');
  const executionId = String(delegated?.data?.execution_id || '');
  expect(executionId).not.toBe('');
  const execution = await readJson<{
    kind: string;
    status: string;
    vertical_abilities: Array<{ id: string; name: string; version: string; checksum: string }>;
  }>(
    page,
    `/api/executions/${executionId}?tenant_id=${TENANT_ID}`,
  );
  expect(execution.kind).toBe('dynamic_task');
  expect(execution.status).toBe('succeeded');
  expect(execution.vertical_abilities).toEqual([
    { id: 'web-intelligence', name: '全网情报员', version: '1.7.4', checksum: expect.any(String) },
  ]);
  expect(browserFailures).toEqual([]);
});

test('普通用户通过全网情报员真实执行 GitHub 公共仓库 Operation', async ({ page }) => {
  /** 验证 GitHub 公共渠道从前台选择到适配器回执、Operation 和结果验证的完整闭环。 */

  const browserFailures: string[] = [];
  page.on('pageerror', (error) => browserFailures.push(`pageerror: ${error.message}`));
  page.on('response', (response) => {
    if (response.status() >= 500) {
      browserFailures.push(`${response.status()} ${response.url()}`);
    }
  });

  await configureJinaForExecution(page);
  await loginThroughUi(page, 'member', 'member');
  await openPublicAgentChat(page);
  await selectWebIntelligence(page);

  const composer = page.getByPlaceholder('输入消息，按 Enter 发送...');
  await composer.fill('GITHUB-PUBLIC-E2E：请读取公开仓库并形成带来源的情报报告。');
  const streamResponse = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && new URL(response.url()).pathname === '/api/chat/stream'
  ));
  await page.getByRole('button', { name: '发送', exact: true }).click();
  expect((await streamResponse).status()).toBe(200);

  const sessionId = await waitForSession(page);
  await waitForAssistant(page, sessionId);
  const events = await readSessionEvents(page, sessionId);
  const delegated = events.find((item) => item.event_type === 'dynamic_task_delegated');
  const executionId = String(delegated?.data?.execution_id || '');
  expect(executionId).not.toBe('');
  const execution = await readJson<{
    kind: string;
    status: string;
    operations: Array<{
      operation_name: string;
      effect_kind: string;
      status: string;
    }>;
    vertical_abilities: Array<{ id: string; name: string; version: string; checksum: string }>;
    current_result_id: string | null;
  }>(
    page,
    `/api/executions/${executionId}?tenant_id=${TENANT_ID}`,
  );
  expect(execution.kind).toBe('dynamic_task');
  expect(execution.status).toBe('succeeded');
  expect(execution.operations).toEqual(expect.arrayContaining([
    expect.objectContaining({
      operation_name: 'intelligence.github_repo',
      effect_kind: 'read',
      status: 'succeeded',
    }),
  ]));
  expect(execution.vertical_abilities).toEqual([
    { id: 'web-intelligence', name: '全网情报员', version: '1.7.4', checksum: expect.any(String) },
  ]);
  expect(execution.current_result_id).not.toBeNull();

  const result = await readJson<{ status: string; result: { markdown?: string } }>(
    page,
    `/api/executions/${executionId}/result?tenant_id=${TENANT_ID}`,
  );
  expect(result.status).toBe('verified');
  expect(result.result.markdown).toContain('GITHUB-PUBLIC-E2E');
  expect(result.result.markdown).toContain('https://github.com/octocat/Hello-World');
  expect(result.result.markdown).toContain('https://github.com/octocat/Hello-World/issues/1');
  expect(browserFailures).toEqual([]);
});

test('普通用户通过全网情报员真实执行 B站公开视频详情 Operation', async ({ page }) => {
  /** 验证 B站公开详情从前台选择到公共 API 适配器、Operation 和结果验证的完整闭环。 */

  const browserFailures: string[] = [];
  page.on('pageerror', (error) => browserFailures.push(`pageerror: ${error.message}`));
  page.on('response', (response) => {
    if (response.status() >= 500) {
      browserFailures.push(`${response.status()} ${response.url()}`);
    }
  });

  await configureJinaForExecution(page);
  await loginThroughUi(page, 'member', 'member');
  await openPublicAgentChat(page);
  await selectWebIntelligence(page);

  const composer = page.getByPlaceholder('输入消息，按 Enter 发送...');
  await composer.fill('BILIBILI-PUBLIC-E2E：请读取公开视频详情并形成带来源的情报报告。');
  const streamResponse = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && new URL(response.url()).pathname === '/api/chat/stream'
  ));
  await page.getByRole('button', { name: '发送', exact: true }).click();
  expect((await streamResponse).status()).toBe(200);

  const sessionId = await waitForSession(page);
  await waitForAssistant(page, sessionId);
  const events = await readSessionEvents(page, sessionId);
  const delegated = events.find((item) => item.event_type === 'dynamic_task_delegated');
  const executionId = String(delegated?.data?.execution_id || '');
  expect(executionId).not.toBe('');
  const execution = await readJson<{
    kind: string;
    status: string;
    operations: Array<{
      operation_name: string;
      effect_kind: string;
      status: string;
    }>;
    vertical_abilities: Array<{ id: string; name: string; version: string; checksum: string }>;
    current_result_id: string | null;
  }>(
    page,
    `/api/executions/${executionId}?tenant_id=${TENANT_ID}`,
  );
  expect(execution.kind).toBe('dynamic_task');
  expect(execution.status).toBe('succeeded');
  expect(execution.operations).toEqual(expect.arrayContaining([
    expect.objectContaining({
      operation_name: 'intelligence.bilibili_video',
      effect_kind: 'read',
      status: 'succeeded',
    }),
  ]));
  expect(execution.vertical_abilities).toEqual([
    { id: 'web-intelligence', name: '全网情报员', version: '1.7.4', checksum: expect.any(String) },
  ]);
  expect(execution.current_result_id).not.toBeNull();

  const result = await readJson<{ status: string; result: { markdown?: string } }>(
    page,
    `/api/executions/${executionId}/result?tenant_id=${TENANT_ID}`,
  );
  expect(result.status).toBe('verified');
  expect(result.result.markdown).toContain('BILIBILI-PUBLIC-E2E');
  expect(result.result.markdown).toContain('https://www.bilibili.com/video/BV1xx411c7mD');
  expect(result.result.markdown).toContain('Bilibili public API');
  expect(browserFailures).toEqual([]);
});

test('普通用户通过全网情报员真实执行 B站公开视频搜索 Operation', async ({ page }) => {
  /** 验证 B站公开搜索的数量/状态/来源契约从前台到统一 Execution 的完整闭环。 */

  const browserFailures: string[] = [];
  page.on('pageerror', (error) => browserFailures.push(`pageerror: ${error.message}`));
  page.on('response', (response) => {
    if (response.status() >= 500) {
      browserFailures.push(`${response.status()} ${response.url()}`);
    }
  });

  await configureJinaForExecution(page);
  await loginThroughUi(page, 'member', 'member');
  await openPublicAgentChat(page);
  await selectWebIntelligence(page);

  const composer = page.getByPlaceholder('输入消息，按 Enter 发送...');
  await composer.fill('BILIBILI-SEARCH-E2E：请搜索公开视频并形成带来源的情报报告。');
  const streamResponse = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && new URL(response.url()).pathname === '/api/chat/stream'
  ));
  await page.getByRole('button', { name: '发送', exact: true }).click();
  expect((await streamResponse).status()).toBe(200);

  const sessionId = await waitForSession(page);
  await waitForAssistant(page, sessionId);
  const events = await readSessionEvents(page, sessionId);
  const delegated = events.find((item) => item.event_type === 'dynamic_task_delegated');
  const executionId = String(delegated?.data?.execution_id || '');
  expect(executionId).not.toBe('');
  const execution = await readJson<{
    kind: string;
    status: string;
    operations: Array<{
      operation_name: string;
      effect_kind: string;
      status: string;
    }>;
    vertical_abilities: Array<{ id: string; name: string; version: string; checksum: string }>;
    current_result_id: string | null;
  }>(
    page,
    `/api/executions/${executionId}?tenant_id=${TENANT_ID}`,
  );
  expect(execution.kind).toBe('dynamic_task');
  expect(execution.status).toBe('succeeded');
  expect(execution.operations).toEqual(expect.arrayContaining([
    expect.objectContaining({
      operation_name: 'intelligence.bilibili_search',
      effect_kind: 'read',
      status: 'succeeded',
    }),
  ]));
  expect(execution.vertical_abilities).toEqual([
    { id: 'web-intelligence', name: '全网情报员', version: '1.7.4', checksum: expect.any(String) },
  ]);
  expect(execution.current_result_id).not.toBeNull();

  const result = await readJson<{ status: string; result: { markdown?: string } }>(
    page,
    `/api/executions/${executionId}/result?tenant_id=${TENANT_ID}`,
  );
  expect(result.status).toBe('verified');
  expect(result.result.markdown).toContain('BILIBILI-SEARCH-E2E');
  expect(result.result.markdown).toContain('https://www.bilibili.com/video/BV1xx411c7mD');
  expect(result.result.markdown).toContain('Bilibili public search API');
  expect(browserFailures).toEqual([]);
});

test('普通用户通过全网情报员真实执行 YouTube 公共视频字幕 Operation', async ({ page }) => {
  /** 验证 YouTube 公开视频元数据、非空字幕、来源和统一 Execution 结果契约。 */

  const browserFailures: string[] = [];
  page.on('pageerror', (error) => browserFailures.push(`pageerror: ${error.message}`));
  page.on('response', (response) => {
    if (response.status() >= 500) {
      browserFailures.push(`${response.status()} ${response.url()}`);
    }
  });

  await configureJinaForExecution(page);
  await loginThroughUi(page, 'member', 'member');
  await openPublicAgentChat(page);
  await selectWebIntelligence(page);

  const composer = page.getByPlaceholder('输入消息，按 Enter 发送...');
  await composer.fill('YOUTUBE-PUBLIC-E2E：请读取公开视频字幕并形成带来源的教程摘要。');
  const streamResponse = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && new URL(response.url()).pathname === '/api/chat/stream'
  ));
  await page.getByRole('button', { name: '发送', exact: true }).click();
  expect((await streamResponse).status()).toBe(200);

  const sessionId = await waitForSession(page);
  await waitForAssistant(page, sessionId);
  const events = await readSessionEvents(page, sessionId);
  const delegated = events.find((item) => item.event_type === 'dynamic_task_delegated');
  const executionId = String(delegated?.data?.execution_id || '');
  expect(executionId).not.toBe('');
  const execution = await readJson<{
    kind: string;
    status: string;
    operations: Array<{
      operation_name: string;
      effect_kind: string;
      status: string;
    }>;
    vertical_abilities: Array<{ id: string; name: string; version: string; checksum: string }>;
    current_result_id: string | null;
  }>(
    page,
    `/api/executions/${executionId}?tenant_id=${TENANT_ID}`,
  );
  expect(execution.kind).toBe('dynamic_task');
  expect(execution.status).toBe('succeeded');
  expect(execution.operations).toEqual(expect.arrayContaining([
    expect.objectContaining({
      operation_name: 'intelligence.youtube_video',
      effect_kind: 'read',
      status: 'succeeded',
    }),
  ]));
  expect(execution.vertical_abilities).toEqual([
    { id: 'web-intelligence', name: '全网情报员', version: '1.7.4', checksum: expect.any(String) },
  ]);
  expect(execution.current_result_id).not.toBeNull();

  const result = await readJson<{ status: string; result: { markdown?: string } }>(
    page,
    `/api/executions/${executionId}/result?tenant_id=${TENANT_ID}`,
  );
  expect(result.status).toBe('verified');
  expect(result.result.markdown).toContain('YOUTUBE-PUBLIC-E2E');
  expect(result.result.markdown).toContain('https://www.youtube.com/watch?v=e2e-youtube-public');
  expect(result.result.markdown).toContain('E2E subtitle line one');
  expect(result.result.markdown).toContain('success_with_data');
  expect(browserFailures).toEqual([]);
});

test('普通用户通过全网情报员真实执行公开视频 Whisper 转写 Operation', async ({ page }) => {
  /** 验证管理员配置的共享 Groq Provider 经统一 Runtime 完成公开视频转写和结果验证。 */

  const browserFailures: string[] = [];
  page.on('pageerror', (error) => browserFailures.push(`pageerror: ${error.message}`));
  page.on('response', (response) => {
    if (response.status() >= 500) {
      browserFailures.push(`${response.status()} ${response.url()}`);
    }
  });

  await configureJinaForExecution(page);
  await loginThroughUi(page, 'member', 'member');
  await openPublicAgentChat(page);
  await selectWebIntelligence(page);

  const composer = page.getByPlaceholder('输入消息，按 Enter 发送...');
  await composer.fill('TRANSCRIBE-PUBLIC-E2E：请转写公开视频并形成带来源的情报报告。');
  const streamResponse = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && new URL(response.url()).pathname === '/api/chat/stream'
  ));
  await page.getByRole('button', { name: '发送', exact: true }).click();
  expect((await streamResponse).status()).toBe(200);

  const sessionId = await waitForSession(page);
  await waitForAssistant(page, sessionId);
  const events = await readSessionEvents(page, sessionId);
  const delegated = events.find((item) => item.event_type === 'dynamic_task_delegated');
  const executionId = String(delegated?.data?.execution_id || '');
  expect(executionId).not.toBe('');
  const execution = await readJson<{
    kind: string;
    status: string;
    operations: Array<{
      operation_name: string;
      effect_kind: string;
      status: string;
    }>;
    vertical_abilities: Array<{ id: string; name: string; version: string; checksum: string }>;
    current_result_id: string | null;
  }>(
    page,
    `/api/executions/${executionId}?tenant_id=${TENANT_ID}`,
  );
  expect(execution.kind).toBe('dynamic_task');
  expect(execution.status).toBe('succeeded');
  expect(execution.operations).toEqual(expect.arrayContaining([
    expect.objectContaining({
      operation_name: 'intelligence.media_transcribe',
      effect_kind: 'read',
      status: 'succeeded',
    }),
  ]));
  expect(execution.vertical_abilities).toEqual([
    { id: 'web-intelligence', name: '全网情报员', version: '1.7.4', checksum: expect.any(String) },
  ]);
  expect(execution.current_result_id).not.toBeNull();

  const result = await readJson<{ status: string; result: { markdown?: string } }>(
    page,
    `/api/executions/${executionId}/result?tenant_id=${TENANT_ID}`,
  );
  expect(result.status).toBe('verified');
  expect(result.result.markdown).toContain('TRANSCRIBE-PUBLIC-E2E');
  expect(result.result.markdown).toContain('https://www.youtube.com/watch?v=e2e-transcribe-public');
  expect(result.result.markdown).toContain('Groq Whisper');
  expect(result.result.markdown).toContain('E2E transcription line one');
  expect(result.result.markdown).toContain('success_with_data');
  expect(browserFailures).toEqual([]);
});

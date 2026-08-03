/**
 * 从 stdin 读取临时认证会话，在真实单端口应用验证请假政策、确认和待审批受理闭环。
 * 调用方负责生成短期 token；脚本不保存账号、密码或 token。
 */

import fs from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';
import { chromium } from '@playwright/test';

const authChunks = [];
for await (const chunk of process.stdin) authChunks.push(chunk);
const auth = JSON.parse(Buffer.concat(authChunks).toString('utf8'));
if (!auth?.token || !auth?.user?.id) {
  throw new Error('stdin 必须提供包含 token 和 user 的认证会话 JSON');
}

const baseUrl = process.env.GONGGE_LIVE_BASE_URL || 'http://127.0.0.1:5137';
const agentId = process.env.GONGGE_LIVE_AGENT_ID || 'agent_9d3d1fdf171049ed';
const modelConfigId = process.env.GONGGE_LIVE_MODEL_CONFIG_ID || '';
const prompt = process.env.GONGGE_LIVE_PROMPT
  || '申请 2026-07-27 到 2026-07-28 两天年假，处理家庭事务';
const artifactDir = path.resolve(process.cwd(), '..', '.dev');
await fs.mkdir(artifactDir, { recursive: true });

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
const page = await context.newPage();
const browserErrors = [];
page.on('pageerror', (error) => browserErrors.push(`pageerror: ${error.message}`));
page.on('console', (message) => {
  if (message.type() === 'error') browserErrors.push(`console: ${message.text()}`);
});
page.on('requestfailed', (request) => {
  browserErrors.push(`requestfailed: ${request.method()} ${request.url()}`);
});

async function waitForTurn() {
  await page.getByRole('button', { name: '停止生成', exact: true }).waitFor({
    state: 'visible',
    timeout: 30_000,
  });
  await page.getByRole('button', { name: '停止生成', exact: true }).waitFor({
    state: 'hidden',
    timeout: 180_000,
  });
  await page.getByRole('button', { name: '发送', exact: true }).waitFor({
    state: 'visible',
    timeout: 30_000,
  });
}

try {
  await page.addInitScript(({ session, selectedAgentId, selectedModelConfigId }) => {
    localStorage.setItem('gongge_auth', JSON.stringify(session));
    localStorage.setItem('gongge_onboarding_guide_seen', '1');
    localStorage.setItem('gongge_quick_start_guide_seen', '1');
    localStorage.setItem('gongge_enterprise_agent_scope', selectedAgentId);
    if (selectedModelConfigId) {
      localStorage.setItem(
        'skill_agent_selected_model_config:tenant_demo',
        selectedModelConfigId,
      );
    }
  }, {
    session: auth,
    selectedAgentId: agentId,
    selectedModelConfigId: modelConfigId,
  });
  await page.goto(`${baseUrl}/workspace/chat/draft/${agentId}`, {
    waitUntil: 'networkidle',
  });

  const composer = page.locator('textarea[placeholder*="输入消息"]');
  await composer.waitFor({ state: 'visible', timeout: 30_000 });
  await composer.fill(prompt);
  await page.getByRole('button', { name: '发送', exact: true }).click();
  await page.waitForURL(/\/workspace\/chat\/session_/, { timeout: 60_000 });
  await waitForTurn();

  const messageArea = page.locator(
    'div[class*="overflow-y-auto"][class*="pb-[62px]"]',
  );
  const confirmationBody = await messageArea.innerText();
  if (!confirmationBody.includes('确认提交') || !confirmationBody.includes('取消提交')) {
    throw new Error('首轮未显示明确确认门禁');
  }
  if (confirmationBody.includes('LEAVE-')) {
    throw new Error('确认前不应生成请假申请单号');
  }

  await composer.fill('确认提交');
  await page.getByRole('button', { name: '发送', exact: true }).click();
  await waitForTurn();

  const sessionId = page.url().match(/session_[A-Za-z0-9]+/)?.[0];
  if (!sessionId) throw new Error(`未从地址栏解析到会话 ID：${page.url()}`);
  const finalBody = await messageArea.innerText();
  if (!finalBody.includes('LEAVE-')) {
    throw new Error('确认后页面未显示请假申请单号');
  }
  if (
    !finalBody.includes('待审批')
    && !finalBody.includes('待直属主管审批')
    && !finalBody.includes('pending')
  ) {
    throw new Error('确认后页面未明确显示待审批状态');
  }
  if (browserErrors.length > 0) {
    throw new Error(`浏览器出现错误：${browserErrors.join(' | ')}`);
  }

  const screenshot = path.join(artifactDir, `${sessionId}-leave-application-runtime.png`);
  await page.screenshot({ path: screenshot, fullPage: true });
  process.stdout.write(JSON.stringify({
    session_id: sessionId,
    screenshot,
    response_excerpt: finalBody.slice(-1400),
    browser_errors: browserErrors,
  }, null, 2));
} catch (error) {
  const failureScreenshot = path.join(artifactDir, 'leave-application-runtime-failure.png');
  await page.screenshot({ path: failureScreenshot, fullPage: true });
  const bodyText = await page.locator('body').innerText().catch(() => '');
  process.stderr.write(JSON.stringify({
    url: page.url(),
    screenshot: failureScreenshot,
    body_excerpt: bodyText.slice(-1800),
    browser_errors: browserErrors,
  }, null, 2));
  throw error;
} finally {
  await browser.close();
}

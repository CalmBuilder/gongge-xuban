/**
 * 读取 stdin 中的临时认证会话，在当前单端口应用执行一次真实 Chromium 知识 SOP 回归。
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
  || '请只运行图结构验证的知识路径，查询员工考勤迟到政策，不要启动其他SOP';
const expectedResponseText = process.env.GONGGE_LIVE_EXPECTED_TEXT || '考勤';
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
  await page.getByRole('button', { name: '停止生成', exact: true }).waitFor({
    state: 'hidden',
    timeout: 180_000,
  });
  await page.getByRole('button', { name: '发送', exact: true }).waitFor({
    state: 'visible',
    timeout: 30_000,
  });
  await page.locator('[aria-label="知识引用"]').last().waitFor({
    state: 'visible',
    timeout: 30_000,
  });

  const sessionId = page.url().match(/session_[A-Za-z0-9]+/)?.[0];
  if (!sessionId) throw new Error(`未从地址栏解析到会话 ID：${page.url()}`);
  const screenshot = path.join(artifactDir, `${sessionId}-graph-knowledge-runtime.png`);
  await page.screenshot({ path: screenshot, fullPage: true });
  const bodyText = await page.locator('body').innerText();
  const citationText = await page.locator('[aria-label="知识引用"]').last().innerText();
  if (!bodyText.includes(expectedResponseText)) {
    throw new Error(`最终页面未显示预期主题：${expectedResponseText}`);
  }
  if (browserErrors.length > 0) {
    throw new Error(`浏览器出现错误：${browserErrors.join(' | ')}`);
  }
  process.stdout.write(JSON.stringify({
    session_id: sessionId,
    screenshot,
    citations: citationText.split('\n').filter(Boolean),
    response_excerpt: bodyText.slice(-1200),
    browser_errors: browserErrors,
  }, null, 2));
} catch (error) {
  const failureScreenshot = path.join(artifactDir, 'graph-knowledge-runtime-failure.png');
  await page.screenshot({ path: failureScreenshot, fullPage: true });
  const bodyText = await page.locator('body').innerText().catch(() => '');
  process.stderr.write(JSON.stringify({
    url: page.url(),
    screenshot: failureScreenshot,
    body_excerpt: bodyText.slice(0, 1500),
    browser_errors: browserErrors,
  }, null, 2));
  throw error;
} finally {
  await browser.close();
}

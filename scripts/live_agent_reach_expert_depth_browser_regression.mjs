/**
 * @Time       : 2026/09/05
 * @Author     : zhanglp8181
 * @File       : live_agent_reach_expert_depth_browser_regression.mjs
 * @CallChain  : 真实登录页 → 专家自然问题 → DynamicTaskAgent → Trace 回执/页面最终报告
 * @Description: 默认自然问题验收，不指定工具或来源；保存可追溯证据，专业审核独立于机械通过。
 *
 * BROWSER_TEST_PROMPT_MODE=natural（默认）；diagnostic 仅补专业维度，不能替代自然问题验收。
 * BROWSER_TEST_EXPERT_CASE_IDS=all 选择全部矩阵；BROWSER_TEST_REPEAT=2 使用两次全新浏览器。
 * 本脚本会发送真实研究任务，运行者必须提供已有测试账号，不自动创建账号或写数据库。
 */
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { readFile } from 'node:fs/promises';

import { chromium } from '../frontend-enterprise/node_modules/playwright/index.mjs';
import {
  assertBrowserClean, assertResearchEvidence, buildResearchPrompt, captureReportScreenshots,
  installSseCapture,
  observeBrowser, readOperationEvidence, requirementsForCase, runRecordedCheck,
  trackCompletedStream, waitForTerminalReport,
} from './agent_reach_evidence.mjs';

const baseUrl = process.env.BROWSER_TEST_BASE_URL || 'http://127.0.0.1:5137';
const tenantId = process.env.BROWSER_TEST_TENANT_ID || 'tenant_demo';
const username = process.env.BROWSER_TEST_USERNAME || 'user_demo';
const password = process.env.BROWSER_TEST_PASSWORD;
const repeat = Number.parseInt(process.env.BROWSER_TEST_REPEAT || '1', 10);
const pollAttempts = Number.parseInt(process.env.BROWSER_TEST_EVIDENCE_POLL_ATTEMPTS || '900', 10);
const promptMode = process.env.BROWSER_TEST_PROMPT_MODE || 'natural';
const modelConfigId = process.env.BROWSER_TEST_MODEL_CONFIG_ID || '';
const matrixPath = new URL('./agent_reach_expert_depth_matrix.json', import.meta.url);

if (!password) throw new Error('必须通过 BROWSER_TEST_PASSWORD 提供真实演示账号密码。');
assert.ok(Number.isSafeInteger(repeat) && repeat > 0, 'BROWSER_TEST_REPEAT 必须是正整数。');
assert.ok(Number.isSafeInteger(pollAttempts) && pollAttempts > 0, '轮询次数必须是正整数。');
assert.ok(['natural', 'diagnostic'].includes(promptMode), '提问模式仅支持 natural/diagnostic。');

/** 按公开业务矩阵选择专家，不携带凭据、内部工具指令或固定来源。 */
async function loadCases() {
  const matrix = JSON.parse(await readFile(matrixPath, 'utf8'));
  assert.ok(Array.isArray(matrix) && matrix.length, '专家深度矩阵为空。');
  const requested = (process.env.BROWSER_TEST_EXPERT_CASE_IDS
    || process.env.BROWSER_TEST_EXPERT_CASE_ID
    || 'engineering-developer-tooling-engineer').split(',').map((value) => value.trim()).filter(Boolean);
  if (requested.includes('all')) return matrix;
  return requested.map((caseId) => {
    const expertCase = matrix.find((item) => item.case_id === caseId);
    assert.ok(expertCase, '指定的专家 case_id 不存在。');
    return expertCase;
  });
}

/** 从真实产品登录页建立认证上下文，不注入 token、cookie 或预置会话。 */
async function login(page) {
  await page.goto(`${baseUrl}/`);
  await page.evaluate(() => {
    localStorage.clear();
    localStorage.setItem('gongge_onboarding_guide_seen', '1');
    localStorage.setItem('gongge_quick_start_guide_seen', '1');
  });
  await page.getByRole('button', { name: '进入平台' }).click();
  await page.getByRole('textbox', { name: '账号' }).fill(username);
  await page.getByRole('textbox', { name: '密码', exact: true }).fill(password);
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await page.waitForFunction((expectedUsername) => {
    const raw = localStorage.getItem('gongge_auth');
    return raw && JSON.parse(raw).user?.username === expectedUsername;
  }, username, { timeout: 30_000 });
  return page.evaluate(() => {
    const session = JSON.parse(localStorage.getItem('gongge_auth'));
    return { id: session.user.id, username: session.user.username, role: session.user.role };
  });
}

/** 复用真实浏览器认证读取受保护 API，避免在 Node 侧复制认证凭据。 */
async function api(page, path) {
  return page.evaluate(async (requestPath) => {
    const raw = localStorage.getItem('gongge_auth');
    if (!raw) throw new Error('真实浏览器认证会话不存在');
    const auth = JSON.parse(raw);
    const response = await fetch(requestPath, {
      headers: auth.token ? { Authorization: `Bearer ${auth.token}` } : {},
      signal: AbortSignal.timeout(15_000),
    });
    const body = await response.json();
    if (!response.ok) throw new Error(`${response.status} ${requestPath}`);
    return body;
  }, path);
}

/** 在真实专家目录中按公开的中文名称与一级分类找到对应的内置专家卡片。 */
async function openExpertChat(page, expertCase) {
  const experts = await api(page, `/api/enterprise/agents?tenant_id=${tenantId}&scope=expert`);
  const matches = experts.filter((item) => (
    item.name === expertCase.expert_name
    && item.metadata?.expert_category === expertCase.category
  ));
  const expert = matches.length === 1 ? matches[0] : null;
  if (!expert) {
    throw new Error(
      matches.length > 1
        ? `真实专家目录存在重复公开身份：${expertCase.expert_name} / ${expertCase.category}`
        : `真实专家目录没有找到：${expertCase.expert_name} / ${expertCase.category}`,
    );
  }

  await page.goto(`${baseUrl}/enterprise/platform/experts`);
  await page.getByRole('heading', { name: '专家' }).first().waitFor({ state: 'visible', timeout: 30_000 });
  const search = page.getByRole('textbox', { name: '搜索专家' });
  await search.fill(expertCase.expert_name);
  const card = page.locator('.gongge-employee-card').filter({ hasText: expertCase.expert_name }).first();
  await card.waitFor({ state: 'visible', timeout: 30_000 });
  await card.click();
  await page.getByRole('button', { name: '添加使用并开始对话' }).click();
  await page.waitForURL(/\/workspace\/chat\/draft\/[^/?#]+$/, { timeout: 30_000 });
  return {
    ...expert,
    upstream_path: expertCase.upstream_path,
    agentId: page.url().match(/\/draft\/([^/?#]+)$/)?.[1] || expert.id,
  };
}

/** 在通用能力包菜单选择全网情报员，并确认 Jina 缺失提示没有误放行。 */
async function selectWebIntelligence(page) {
  const trigger = page.getByRole('button', { name: '选择垂直能力包' });
  await trigger.waitFor({ state: 'visible', timeout: 30_000 });
  await trigger.click();
  const item = page.getByRole('menuitemcheckbox', { name: /全网情报员/ }).first();
  await item.waitFor({ state: 'visible', timeout: 10_000 });
  await item.click();
  const dialog = page.getByRole('dialog');
  if (await dialog.isVisible().catch(() => false)) {
    throw new Error('全网情报员依赖 readiness 未通过，真实任务未发送；请检查能力包配置。');
  }
  assert.ok((await page.locator('button[aria-label="选择垂直能力包"]').textContent())?.includes('能力包：全网情报员'));
  assert.equal(
    await page.getByRole('button', { name: '选择 DynamicTaskAgent 复杂任务引擎' }).getAttribute('aria-pressed'),
    'true',
  );
}

/** 通过真实聊天框的模型菜单选择指定的已配置模型，仅供环境回归切换模型。 */
async function selectModelConfig(page) {
  if (!modelConfigId) return;
  const models = await api(page, `/api/enterprise/model-configs?tenant_id=${tenantId}`);
  const target = models.find((item) => item.id === modelConfigId && item.enabled);
  assert.ok(target, `真实模型目录没有找到可用配置：${modelConfigId}`);
  const currentModelButton = page.getByRole('button', { name: /默认模型|kimi-k3|DeepSeek|Qwen|模型/i }).last();
  await currentModelButton.click();
  await page.getByRole('menuitem').filter({ hasText: target.name || target.model }).click();
}


/** 记录一次干净上下文的全部判定；任何阶段失败都落新 JSON 和失败截图，不覆盖旧证据。 */
async function runCase(expertCase, runNumber) {
  const invocationId = randomUUID();
  const prefix = `.dev/agent-reach-expert-${expertCase.case_id}-${promptMode}-run${runNumber}-${invocationId}`;
  const path = `${prefix}.json`;
  const prompt = buildResearchPrompt(expertCase, promptMode);
  const report = {
    schemaVersion: 2, status: 'running', stage: 'browser_setup', invocationId,
    case_id: expertCase.case_id, category: expertCase.category, expert_name: expertCase.expert_name,
    upstream_path: expertCase.upstream_path, run: runNumber, user: username, promptMode,
    prompt, requirements: requirementsForCase(expertCase),
    acceptance: { automation: 'pending', professionalReview: { status: 'pending' } },
  };
  let browser;
  let context;
  let page;
  let streamTask;
  try {
    const saved = await runRecordedCheck({
      path, report, secrets: [password],
      captureFailure: () => page ? captureReportScreenshots(page, `${prefix}-failure`) : [],
      run: async () => {
        browser = await chromium.launch({ headless: true });
        context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
        page = await context.newPage();
        await installSseCapture(page);
        const observation = observeBrowser(page);
        report.browserObservation = observation;
        report.stage = 'login';
        report.identity = await login(page);
        assert.equal(report.identity.role, 'member', '深度验收必须使用普通平台用户');
        report.stage = 'expert_selection';
        const expert = await openExpertChat(page, expertCase);
        report.expertAgentId = expert.agentId;
        await selectWebIntelligence(page);
        await selectModelConfig(page);
        report.stage = 'submit';
        await page.getByPlaceholder('输入消息，按 Enter 发送...').fill(prompt);
        const streamResponse = page.waitForResponse((response) => (
          response.request().method() === 'POST'
          && new URL(response.url()).pathname === '/api/chat/stream'
        ));
        await page.getByRole('button', { name: '发送', exact: true }).click();
        const response = await streamResponse;
        assert.equal(response.status(), 200);
        const streamState = { complete: false };
        report.stream = streamState;
        streamTask = trackCompletedStream(response, streamState, page);
        // DynamicTaskAgent 允许长任务在草稿路由上持续执行；草稿提升发生在业务
        // complete 到达时，不能用固定的短导航窗口把正常长任务判成 submit 失败。
        // 先等待完整 SSE 终帧，随后前端应已处理 complete，再等待持久会话路由。
        await streamTask;
        assert.ok(!streamState.error, streamState.error || '聊天 SSE 未完成');
        await page.waitForURL(/\/workspace\/chat\/session_[^/?#]+$/, { timeout: 60_000 });
        const sessionId = page.url().match(/\/workspace\/chat\/(session_[^/?#]+)$/)?.[1];
        assert.ok(sessionId, '草稿没有提升为持久会话');
        report.sessionId = sessionId;
        report.url = page.url();
        report.stage = 'final_delivery';
        const evidence = await waitForTerminalReport({
          page, readJson: api, sessionId, tenantId, streamState, pollAttempts,
          onProgress: (progress) => Object.assign(report, progress),
        });
        report.delivery = evidence.delivery;
        report.executionResult = evidence.executionResult;
        report.execution = evidence.execution;
        report.executionId = evidence.executionId;
        report.stage = 'operation_evidence';
        const trace = await api(page, `/api/enterprise/traces/${sessionId}?tenant_id=${tenantId}`);
        const operations = readOperationEvidence(evidence.execution, trace);
        report.operations = operations;
        assert.equal(evidence.execution.kind, 'dynamic_task');
        assert.equal(evidence.execution.status, 'succeeded');
        assert.ok(observation.streamBodies.at(-1)?.ability_ids?.includes('web-intelligence'));
        assert.equal(observation.streamBodies.at(-1)?.execution_engine, 'dynamic_task');
        report.stage = 'research_depth';
        report.acceptance = assertResearchEvidence({
          operations, executionResult: evidence.executionResult, requirements: report.requirements,
        });
        report.stage = 'screenshots';
        report.screenshots = await captureReportScreenshots(page, prefix);
        report.stage = 'browser_errors';
        assertBrowserClean(observation);
      },
    });
    console.log(JSON.stringify({
      status: saved.status, case_id: expertCase.case_id, sessionId: saved.sessionId, evidence: path,
      automation: saved.acceptance.automation, professionalReview: saved.acceptance.professionalReview.status,
    }));
  } finally {
    await context?.close();
    await browser?.close();
    await streamTask;
  }
}

const cases = await loadCases();
for (const expertCase of cases) {
  for (let runNumber = 1; runNumber <= repeat; runNumber += 1) {
    await runCase(expertCase, runNumber);
  }
}

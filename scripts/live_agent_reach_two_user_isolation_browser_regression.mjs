/**
 * @Time       : 2026/09/05
 * @Author     : zhanglp8181
 * @File       : live_agent_reach_two_user_isolation_browser_regression.mjs
 * @CallChain  : 两个普通用户真实登录 → 不同专家自然问题 → Operation/报告 → 双向隔离
 * @Description: 从真实回执校验研究，逐请求验证拒绝响应体；失败也保留脱敏证据。
 *
 * BROWSER_TEST_REPEAT=2 可重复两次干净上下文；默认 natural 模式不指定工具/次数/来源。
 * 真实研究任务会调用已配置 Provider；此脚本不创建用户、不修改服务或数据库配置。
 */
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { readFile } from 'node:fs/promises';

import { chromium } from '../frontend-enterprise/node_modules/playwright/index.mjs';
import {
  assertBrowserClean, assertDeniedAccess, assertResearchEvidence, buildResearchPrompt,
  captureReportScreenshots, installSseCapture, observeBrowser, readOperationEvidence, requirementsForCase,
  runRecordedCheck, trackCompletedStream, waitForTerminalReport,
} from './agent_reach_evidence.mjs';

const baseUrl = process.env.BROWSER_TEST_BASE_URL || 'http://127.0.0.1:5137';
const tenantId = process.env.BROWSER_TEST_TENANT_ID || 'tenant_demo';
const sharedPassword = process.env.BROWSER_TEST_PASSWORD;
const secondPassword = process.env.BROWSER_TEST_SECOND_PASSWORD || sharedPassword;
const promptMode = process.env.BROWSER_TEST_PROMPT_MODE || 'natural';
const repeat = Number.parseInt(process.env.BROWSER_TEST_REPEAT || '1', 10);
const pollAttempts = Number.parseInt(process.env.BROWSER_TEST_EVIDENCE_POLL_ATTEMPTS || '1500', 10);
if (!sharedPassword || !secondPassword) throw new Error('请通过环境变量提供两个真实测试账号的密码。');
assert.ok(Number.isSafeInteger(repeat) && repeat > 0, 'BROWSER_TEST_REPEAT 必须是正整数。');
assert.ok(Number.isSafeInteger(pollAttempts) && pollAttempts > 0, '轮询次数必须是正整数。');
assert.ok(['natural', 'diagnostic'].includes(promptMode), '提问模式仅支持 natural/diagnostic。');
const matrix = JSON.parse(await readFile(new URL('./agent_reach_expert_depth_matrix.json', import.meta.url), 'utf8'));
const accounts = [
  { key: 'user-a', username: process.env.BROWSER_TEST_USERNAME || 'user_demo',
    password: sharedPassword, caseId: 'engineering-api-platform-engineer' },
  { key: 'user-b', username: process.env.BROWSER_TEST_SECOND_USERNAME || 'zhanglipeng',
    password: secondPassword, caseId: 'design-ui-designer' },
].map((account) => {
  const expertCase = matrix.find((item) => item.case_id === account.caseId);
  assert.ok(expertCase, '双用户场景对应的专家矩阵缺失');
  return { ...account, expertCase, expertName: expertCase.expert_name, category: expertCase.category };
});
assert.notEqual(accounts[0].username, accounts[1].username, '隔离验收必须使用两个不同账号');

/** 从当前真实浏览器上下文读取认证 API，不把令牌带出浏览器进程。 */
async function readJson(page, path) {
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

/** 读取跨用户访问的 HTTP 状态，仅返回状态和脱敏错误体，不暴露认证信息。 */
async function readAccessStatus(page, path) {
  return page.evaluate(async (requestPath) => {
    const raw = localStorage.getItem('gongge_auth');
    if (!raw) throw new Error('真实浏览器认证会话不存在');
    const auth = JSON.parse(raw);
    const response = await fetch(requestPath, {
      headers: auth.token ? { Authorization: `Bearer ${auth.token}` } : {},
      signal: AbortSignal.timeout(15_000),
    });
    let body = null;
    try {
      body = await response.json();
    } catch {
      body = null;
    }
    return { status: response.status, body };
  }, path);
}

/** 从产品入口走真实登录页，确保每个普通用户建立独立浏览器认证上下文。 */
async function login(page, account) {
  await page.goto(`${baseUrl}/`);
  await page.evaluate(() => {
    localStorage.clear();
    localStorage.setItem('gongge_onboarding_guide_seen', '1');
    localStorage.setItem('gongge_quick_start_guide_seen', '1');
  });
  await page.reload();
  await page.getByRole('button', { name: '进入平台' }).click();
  await page.getByRole('textbox', { name: '账号' }).fill(account.username);
  await page.getByRole('textbox', { name: '密码', exact: true }).fill(account.password);
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await page.waitForFunction((expectedUsername) => {
    const raw = localStorage.getItem('gongge_auth');
    if (!raw) return false;
    return JSON.parse(raw).user?.username === expectedUsername;
  }, account.username, { timeout: 30_000 });
  return page.evaluate(() => {
    const raw = localStorage.getItem('gongge_auth');
    if (!raw) throw new Error('登录成功后未找到认证会话');
    const session = JSON.parse(raw);
    return { id: session.user?.id, username: session.user?.username, role: session.user?.role };
  });
}

/** 从专家目录打开指定专家草稿，确保两个用户使用不同的专业需求视角。 */
async function openExpertChat(page, account) {
  const experts = await readJson(page, `/api/enterprise/agents?tenant_id=${tenantId}&scope=expert`);
  const matches = experts.filter((item) => (
    item.name === account.expertName
    && item.metadata?.expert_category === account.category
  ));
  assert.equal(matches.length, 1, `专家目录中未找到唯一专家：${account.category}/${account.expertName}`);

  await page.goto(`${baseUrl}/enterprise/platform/experts`);
  await page.getByRole('heading', { name: '专家' }).first().waitFor({ state: 'visible', timeout: 30_000 });
  await page.getByRole('textbox', { name: '搜索专家' }).fill(account.expertName);
  const card = page.locator('.gongge-employee-card').filter({ hasText: account.expertName }).first();
  await card.waitFor({ state: 'visible', timeout: 30_000 });
  await card.click();
  await page.getByRole('button', { name: '添加使用并开始对话' }).click();
  await page.waitForURL(/\/workspace\/chat\/draft\/[^/?#]+$/, { timeout: 30_000 });
}

/** 在通用能力包菜单选择全网情报员，并确认当前配置不会误弹依赖缺失提示。 */
async function selectWebIntelligence(page) {
  const trigger = page.getByRole('button', { name: '选择垂直能力包' });
  await trigger.waitFor({ state: 'visible', timeout: 30_000 });
  await trigger.click();
  const item = page.getByRole('menuitemcheckbox', { name: /全网情报员/ }).first();
  await item.waitFor({ state: 'visible', timeout: 10_000 });
  await item.click();
  const dialog = page.getByRole('dialog');
  if (await dialog.isVisible().catch(() => false)) {
    throw new Error('全网情报员依赖 readiness 未通过，双用户真实任务未发送。');
  }
  await assert.doesNotReject(async () => {
    await page.locator('button[aria-label="选择垂直能力包"]').waitFor({ state: 'visible' });
  });
  assert.ok((await page.locator('button[aria-label="选择垂直能力包"]').textContent())?.includes('能力包：全网情报员'));
  assert.equal(
    await page.getByRole('button', { name: '选择 DynamicTaskAgent 复杂任务引擎' }).getAttribute('aria-pressed'),
    'true',
  );
}


/** 执行一位用户的自然问题，立即把过程写入内存报告，以便任何失败阶段都能落失败证据。 */
async function runAccount(browser, account, report, activeUsers, prefix) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  await installSseCapture(page);
  const observation = observeBrowser(page);
  const marker = 'DUAL-' + report.invocationId + '-' + account.key;
  const record = {
    key: account.key, username: account.username, expertName: account.expertName, category: account.category,
    case_id: account.caseId, marker, requirements: requirementsForCase(account.expertCase),
    browserObservation: observation, acceptance: { automation: 'pending', professionalReview: { status: 'pending' } },
  };
  report.users.push(record);
  const active = { account, record, context, page, observation };
  activeUsers.push(active);
  report.stage = account.key + ':login';
  const identity = await login(page, account);
  assert.equal(identity.username, account.username);
  assert.equal(identity.role, 'member');
  record.identity = identity;
  const catalog = await readJson(page, '/api/chat/abilities');
  assert.ok(catalog.items.some((item) => item.id === 'web-intelligence' && item.name === '全网情报员'));
  report.stage = account.key + ':expert_selection';
  await openExpertChat(page, account);
  await selectWebIntelligence(page);
  const prompt = buildResearchPrompt(account.expertCase, promptMode) + '\n报告中保留本次任务标识：' + marker + '。';
  record.prompt = prompt;
  report.stage = account.key + ':submit';
  await page.getByPlaceholder('输入消息，按 Enter 发送...').fill(prompt);
  const streamResponse = page.waitForResponse((response) => (
    response.request().method() === 'POST' && new URL(response.url()).pathname === '/api/chat/stream'
  ));
  await page.getByRole('button', { name: '发送', exact: true }).click();
  const response = await streamResponse;
  assert.equal(response.status(), 200);
  record.stream = { complete: false };
  active.streamTask = trackCompletedStream(response, record.stream, page);
  await page.waitForURL(/\/workspace\/chat\/session_[^/?#]+$/, { timeout: 60_000 });
  const sessionId = page.url().match(/\/workspace\/chat\/(session_[^/?#]+)$/)?.[1];
  assert.ok(sessionId, '没有创建正式会话');
  record.sessionId = sessionId;
  record.url = page.url();
  report.stage = account.key + ':final_delivery';
  const evidence = await waitForTerminalReport({
    page, readJson, sessionId, tenantId, streamState: record.stream, pollAttempts,
    onProgress: (progress) => Object.assign(record, progress),
  });
  record.delivery = evidence.delivery;
  record.executionId = evidence.executionId;
  record.executionResult = evidence.executionResult;
  report.stage = account.key + ':operation_evidence';
  const trace = await readJson(page, '/api/enterprise/traces/' + sessionId + '?tenant_id=' + tenantId);
  const operations = readOperationEvidence(evidence.execution, trace);
  record.operations = operations;
  assert.equal(evidence.execution.kind, 'dynamic_task');
  assert.equal(evidence.execution.status, 'succeeded');
  assert.ok(observation.streamBodies.at(-1)?.ability_ids?.includes('web-intelligence'));
  assert.equal(observation.streamBodies.at(-1)?.execution_engine, 'dynamic_task');
  report.stage = account.key + ':research_depth';
  record.acceptance = assertResearchEvidence({
    operations, executionResult: evidence.executionResult, requirements: record.requirements,
  });
  assert.ok(evidence.executionResult.result.markdown.includes(marker), '报告未保留当前用户任务标识');
  report.stage = account.key + ':screenshots';
  record.screenshots = await captureReportScreenshots(page, prefix + '-' + account.key);
  return active;
}

/** 双向探测消息、事件、执行、结果和 Trace，拒绝体只允许安全错误字段且不能含对方资料。 */
async function assertIsolation(first, second, report) {
  report.isolation = [];
  for (const [actor, target] of [[first, second], [second, first]]) {
    const targetReport = target.record.executionResult.result.markdown;
    const forbidden = [
      target.record.sessionId, target.record.executionId, target.record.marker, targetReport,
      ...targetReport.split(/\r?\n/).map((line) => line.trim()).filter((line) => line.length >= 24),
      ...target.record.acceptance.readReceipts.map((item) => item.url),
    ];
    const paths = [
      '/api/chat/sessions/' + target.record.sessionId + '/messages?tenant_id=' + tenantId,
      '/api/chat/sessions/' + target.record.sessionId + '/events?tenant_id=' + tenantId,
      '/api/executions/' + target.record.executionId + '?tenant_id=' + tenantId,
      '/api/executions/' + target.record.executionId + '/result?tenant_id=' + tenantId,
      '/api/enterprise/traces/' + target.record.sessionId + '?tenant_id=' + tenantId,
    ];
    for (const path of paths) {
      actor.observation.phase = 'isolation';
      const denial = { url: new URL(path, baseUrl).toString(), validated: false };
      actor.observation.denials.push(denial);
      try {
        const access = await readAccessStatus(actor.page, path);
        denial.status = access.status;
        assertDeniedAccess(access, forbidden);
        denial.validated = true;
        report.isolation.push({ actor: actor.record.key, target: target.record.key, path,
          status: access.status, body: access.body, bodyCheck: 'no_foreign_data' });
        await actor.page.waitForTimeout(0);
      } finally {
        actor.observation.phase = 'research';
      }
    }
    const sessions = await readJson(actor.page, '/api/chat/sessions?tenant_id=' + tenantId);
    assert.ok(Array.isArray(sessions), '会话列表响应不是数组');
    assert.equal(sessions.some((item) => item.id === target.record.sessionId), false, '会话列表暴露对方会话');
    report.isolation.push({ actor: actor.record.key, target: target.record.key, listVisibility: 'hidden' });
    assert.ok(!actor.record.executionResult.result.markdown.includes(target.record.marker), '本用户报告混入对方任务标识');
  }
}

/** 两个普通用户的研究和双向隔离全部通过后才写 completed；失败保留可定位的新文件。 */
async function runPair(runNumber) {
  const invocationId = randomUUID();
  const prefix = '.dev/agent-reach-two-user-isolation-' + promptMode + '-run' + runNumber + '-' + invocationId;
  const path = prefix + '.json';
  const report = {
    schemaVersion: 2, status: 'running', stage: 'browser_setup', invocationId, tenantId,
    run: runNumber, promptMode, users: [],
    acceptance: { automation: 'pending', professionalReview: { status: 'pending' } },
  };
  let browser;
  const activeUsers = [];
  try {
    const saved = await runRecordedCheck({
      path, report, secrets: [sharedPassword, secondPassword],
      captureFailure: async () => {
        const screenshots = [];
        for (const active of activeUsers) {
          screenshots.push(...await captureReportScreenshots(active.page, prefix + '-' + active.record.key + '-failure'));
        }
        return screenshots;
      },
      run: async () => {
        browser = await chromium.launch({ headless: true });
        const first = await runAccount(browser, accounts[0], report, activeUsers, prefix);
        const second = await runAccount(browser, accounts[1], report, activeUsers, prefix);
        report.stage = 'isolation';
        assert.notEqual(first.record.identity.id, second.record.identity.id);
        assert.notEqual(first.record.sessionId, second.record.sessionId);
        assert.notEqual(first.record.executionId, second.record.executionId);
        assert.notEqual(first.record.executionResult.result.markdown, second.record.executionResult.result.markdown);
        await assertIsolation(first, second, report);
        report.stage = 'browser_errors';
        for (const active of activeUsers) assertBrowserClean(active.observation);
        report.acceptance.automation = 'passed';
      },
    });
    console.log(JSON.stringify({ status: saved.status, evidence: path, automation: saved.acceptance.automation,
      professionalReview: saved.acceptance.professionalReview.status,
      sessions: saved.users.map((item) => item.sessionId) }));
  } finally {
    for (const active of activeUsers) await active.context.close();
    await browser?.close();
    await Promise.all(activeUsers.map((active) => active.streamTask));
  }
}

for (let runNumber = 1; runNumber <= repeat; runNumber += 1) await runPair(runNumber);

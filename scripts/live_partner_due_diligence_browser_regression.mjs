/**
 * @Time       : 2026/07/27 00:00
 * @Author     : zhanglp8181
 * @File       : live_partner_due_diligence_browser_regression.mjs
 * @CallChain  : 双 Chromium 账号 → 合作方外部尽调/内部制度 → 低风险建议/高风险法务复核
 * @Description: 用真实 DeepSeek 验证 v2 工具、知识引用、真人候选和恢复终态闭环。
 */

import { chromium } from '../frontend-enterprise/node_modules/playwright/index.mjs';

const baseUrl = process.env.BROWSER_TEST_BASE_URL || 'http://127.0.0.1:5137';
const requesterUsername = process.env.BROWSER_TEST_USERNAME || 'user_demo';
const requesterPassword = process.env.BROWSER_TEST_PASSWORD || 'demo';
const reviewerUsername = process.env.BROWSER_TEST_REVIEWER_USERNAME || 'approver_demo';
const reviewerPassword = process.env.BROWSER_TEST_REVIEWER_PASSWORD || 'demo';
const legalAgentId = 'agent_7d062081c03b4e16';
const skillId = 'partner_onboarding_dd';
const browserErrors = [];
const expectedAuthorizationDenials = [];

const browser = await chromium.launch({ headless: true });
const requesterContext = await browser.newContext({ viewport: { width: 1440, height: 1100 } });
const reviewerContext = await browser.newContext({ viewport: { width: 1440, height: 1100 } });
const requesterPage = await requesterContext.newPage();
const reviewerPage = await reviewerContext.newPage();

for (const [actor, page] of [['requester', requesterPage], ['reviewer', reviewerPage]]) {
  page.on('pageerror', (error) => browserErrors.push(`${actor} pageerror: ${error.message}`));
  page.on('console', (message) => {
    if (message.type() !== 'error') return;
    const value = message.text();
    if (value.includes('status of 403 (Forbidden)')) {
      expectedAuthorizationDenials.push(`${actor}: ${value}`);
      return;
    }
    browserErrors.push(`${actor} console: ${value}`);
  });
  page.on('requestfailed', (request) => {
    const errorText = request.failure()?.errorText || '';
    if (errorText.includes('ERR_ABORTED')) return;
    browserErrors.push(
      `${actor} requestfailed: ${request.method()} ${request.url()} ${errorText}`,
    );
  });
}

/** 登录指定演示账号，并等待认证会话进入独立浏览器上下文。 */
async function login(page, username, password) {
  await page.goto(`${baseUrl}/enterprise/dashboard`);
  await page.evaluate(() => {
    localStorage.setItem('gongge_onboarding_guide_seen', '1');
    localStorage.setItem('gongge_quick_start_guide_seen', '1');
  });
  await page.getByRole('button', { name: '进入平台' }).click();
  await page.getByRole('textbox', { name: '账号' }).fill(username);
  await page.getByRole('textbox', { name: '密码', exact: true }).fill(password);
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await page.waitForFunction(() => Boolean(localStorage.getItem('gongge_auth')));
}

/** 使用页面所属账号的临时令牌读取受保护接口。 */
async function api(page, path) {
  return page.evaluate(async (targetPath) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth'));
    const response = await fetch(targetPath, {
      headers: { Authorization: `Bearer ${auth.token}` },
    });
    if (!response.ok) throw new Error(`${response.status} ${targetPath}`);
    return response.json();
  }, path);
}

/** 等待指定会话的合作方尽调 Runtime 满足持久状态断言。 */
async function waitForRuntime(sessionId, predicate, description) {
  for (let attempt = 0; attempt < 240; attempt += 1) {
    const trace = await api(
      requesterPage,
      `/api/enterprise/traces/${sessionId}?tenant_id=tenant_demo`,
    );
    const runtime = trace?.sop_runtime?.find((candidate) => candidate.skill_id === skillId);
    if (runtime && predicate(runtime)) return runtime;
    await requesterPage.waitForTimeout(500);
  }
  throw new Error(`未等到合作方尽调 Runtime：${description}`);
}

/** 等待页面最终回复包含全部标记，避免把 Runtime 终态误当成渲染完成。 */
async function waitForPageText(page, markers) {
  let latestText = '';
  for (let attempt = 0; attempt < 180; attempt += 1) {
    latestText = await page.locator('body').innerText();
    if (markers.every((marker) => latestText.includes(marker))) return latestText;
    await page.waitForTimeout(500);
  }
  throw new Error(`页面未出现全部标记：${markers.join(' / ')}`);
}

/** 等待复核人任务箱出现当前高风险合作方工作项。 */
async function waitForReviewerWorkItem(sessionId) {
  for (let attempt = 0; attempt < 120; attempt += 1) {
    const items = await api(reviewerPage, '/api/work-items?tenant_id=tenant_demo&view=pending');
    const item = items.find(
      (candidate) => candidate.session_id === sessionId && candidate.skill_id === skillId,
    );
    if (item) return item;
    await reviewerPage.waitForTimeout(500);
  }
  throw new Error('未等到合作方尽调真人复核任务');
}

/** 从数字员工广场发起一段全新的法务对话。 */
async function startLegalChat() {
  await requesterPage.goto(`${baseUrl}/workspace/gallery`);
  await requesterPage.getByRole('textbox', { name: '搜索数字员工' }).fill('法务');
  const card = requesterPage.locator('.gongge-employee-card').filter({ hasText: /^法务/ }).first();
  await card.getByRole('button', { name: '发起对话' }).click();
  await requesterPage.waitForURL(new RegExp(`/workspace/chat/draft/${legalAgentId}$`));
  await requesterPage.waitForTimeout(2500);
  return requesterPage.getByPlaceholder('输入消息，按 Enter 发送...');
}

/** 发送首条消息并从正式会话地址提取会话标识。 */
async function sendInitialMessage(composer, message) {
  await composer.fill(message);
  await requesterPage.getByRole('button', { name: '发送' }).click();
  await requesterPage.waitForURL(/\/workspace\/chat\/(session_[^/?#]+)/, { timeout: 60_000 });
  const sessionId = requesterPage.url().match(/\/workspace\/chat\/(session_[^/?#]+)/)?.[1];
  if (!sessionId) throw new Error('无法取得合作方尽调会话 ID');
  return sessionId;
}

try {
  await login(requesterPage, requesterUsername, requesterPassword);
  await login(reviewerPage, reviewerUsername, reviewerPassword);

  const lowRiskComposer = await startLegalChat();
  const lowRiskSessionId = await sendInitialMessage(
    lowRiskComposer,
    '请只运行合作方入库尽调。企业全称：共格演示科技有限公司；'
      + '统一社会信用代码：91370000MA3D3M001X。',
  );
  const lowRiskCompleted = await waitForRuntime(
    lowRiskSessionId,
    (runtime) => runtime.status === 'succeeded'
      && runtime.skill_version === '2.3.0'
      && runtime.current_node_id === 'issue_demo_onboarding_recommendation'
      && (runtime.operations?.length || 0) === 2,
    '低风险演示入库建议完成',
  );
  const lowRiskTool = lowRiskCompleted.operations.find(
    (operation) => operation.operation_name === 'partner.due_diligence_query',
  );
  const lowRiskKnowledge = lowRiskCompleted.operations.find(
    (operation) => operation.operation_name === 'knowledge.search',
  );
  await requesterPage.reload();
  const lowRiskText = await waitForPageText(requesterPage, [
    lowRiskTool?.result?.subject_name,
    '不代表',
  ]);
  const lowRiskCitation = await requesterPage
    .locator('[aria-label="知识引用"]')
    .last()
    .innerText();
  await requesterPage.screenshot({
    path: `.dev/${lowRiskSessionId}-partner-due-diligence-low.png`,
    fullPage: true,
  });

  const highRiskComposer = await startLegalChat();
  const highRiskSessionId = await sendInitialMessage(
    highRiskComposer,
    '请只运行合作方入库尽调。企业全称：共格演示风险供应商有限公司；'
      + '统一社会信用代码：91370000MA3R15K01X。',
  );
  const highRiskWaiting = await waitForRuntime(
    highRiskSessionId,
    (runtime) => runtime.status === 'waiting'
      && runtime.skill_version === '2.3.0'
      && runtime.current_node_id === 'partner_legal_review'
      && (runtime.operations?.length || 0) === 2,
    '高风险等待法务真人复核',
  );
  const highRiskTool = highRiskWaiting.operations.find(
    (operation) => operation.operation_name === 'partner.due_diligence_query',
  );
  const highRiskKnowledge = highRiskWaiting.operations.find(
    (operation) => operation.operation_name === 'knowledge.search',
  );
  const requesterItems = await api(
    requesterPage,
    '/api/work-items?tenant_id=tenant_demo&view=all',
  );
  const requesterView = requesterItems.find(
    (candidate) => candidate.session_id === highRiskSessionId && candidate.skill_id === skillId,
  );
  const offeredWorkItem = await waitForReviewerWorkItem(highRiskSessionId);

  await reviewerPage.goto(`${baseUrl}/enterprise/work-items`);
  const row = reviewerPage.getByText(skillId, { exact: true }).first();
  await row.waitFor({ state: 'visible' });
  await row.click();
  await reviewerPage.getByRole('button', { name: '认领任务' }).click();
  await reviewerPage.getByPlaceholder('请填写本次处理结果和依据').fill(
    '已核对演示执行记录和黑名单信号，建议暂停自动准入，'
      + '由采购补充实际控制人、廉洁承诺和风险处置材料后再决定。',
  );
  await reviewerPage.getByRole('button', { name: '提交尽调复核意见' }).click();

  const highRiskCompleted = await waitForRuntime(
    highRiskSessionId,
    (runtime) => runtime.status === 'succeeded'
      && runtime.current_node_id === 'partner_review_completed'
      && (runtime.operations?.length || 0) === 2,
    '高风险法务真人复核完成',
  );
  await requesterPage.reload();
  const highRiskText = await waitForPageText(requesterPage, [
    highRiskTool?.result?.check_id,
    '暂停自动准入',
    '不代表',
  ]);
  await requesterPage.screenshot({
    path: `.dev/${highRiskSessionId}-partner-due-diligence-high-requester.png`,
    fullPage: true,
  });
  await reviewerPage.screenshot({
    path: `.dev/${highRiskSessionId}-partner-due-diligence-high-reviewer.png`,
    fullPage: true,
  });

  const completedWorkItem = highRiskCompleted.work_items.find(
    (item) => item.id === offeredWorkItem.id,
  );
  const result = {
    lowRiskSessionId,
    lowRiskTool,
    lowRiskKnowledge: {
      operation_id: lowRiskKnowledge?.operation_id,
      status: lowRiskKnowledge?.status,
      evidence_count: lowRiskKnowledge?.result?.evidence_pack?.length || 0,
    },
    lowRiskFinalNode: lowRiskCompleted.current_node_id,
    lowRiskCitation,
    highRiskSessionId,
    highRiskTool,
    highRiskKnowledge: {
      operation_id: highRiskKnowledge?.operation_id,
      status: highRiskKnowledge?.status,
      evidence_count: highRiskKnowledge?.result?.evidence_pack?.length || 0,
    },
    highRiskWaitingStatus: highRiskWaiting.status,
    requesterAllowedActions: requesterView?.allowed_actions,
    roleCandidateSnapshot: offeredWorkItem.candidates,
    completedWorkItem,
    highRiskFinalNode: highRiskCompleted.current_node_id,
    highRiskReviewVisible: highRiskText.includes('暂停自动准入'),
    lowRiskBoundaryVisible: lowRiskText.includes('不代表'),
    expectedAuthorizationDenialCount: expectedAuthorizationDenials.length,
    browserErrors,
  };
  process.stdout.write(JSON.stringify(result, null, 2));

  if (lowRiskTool?.status !== 'succeeded') process.exitCode = 2;
  if (lowRiskTool?.result?.risk_level !== 'low') process.exitCode = 3;
  if (lowRiskTool?.result?.recommendation !== 'pass') process.exitCode = 4;
  if (lowRiskKnowledge?.status !== 'succeeded') process.exitCode = 5;
  if (!lowRiskCitation.includes('知识来源')) process.exitCode = 6;
  if (!lowRiskTool?.result?.check_id?.startsWith('DD-')) process.exitCode = 7;
  if (highRiskTool?.result?.risk_level !== 'high') process.exitCode = 8;
  if (highRiskTool?.result?.recommendation !== 'human_review') process.exitCode = 9;
  if (highRiskKnowledge?.status !== 'succeeded') process.exitCode = 10;
  if ((requesterView?.allowed_actions || []).length !== 0) process.exitCode = 11;
  if (offeredWorkItem.allowed_actions.join(',') !== 'claim') process.exitCode = 12;
  if (offeredWorkItem.candidates.length !== 1) process.exitCode = 13;
  if (offeredWorkItem.candidates[0]?.user_id !== reviewerUsername) process.exitCode = 14;
  if (!offeredWorkItem.candidates[0]?.source_role_codes.includes(
    'legal_partner_due_diligence_reviewer',
  )) process.exitCode = 15;
  if (completedWorkItem?.outcome !== 'reviewed') process.exitCode = 16;
  if (completedWorkItem?.assignee_user_id !== reviewerUsername) process.exitCode = 17;
  if (!completedWorkItem?.comment?.includes('暂停自动准入')) process.exitCode = 18;
  if (!highRiskText.includes(highRiskTool?.result?.check_id)) process.exitCode = 19;
  if (browserErrors.length) process.exitCode = 20;
} finally {
  await requesterContext.close();
  await reviewerContext.close();
  await browser.close();
}

/**
 * @Time       : 2026/07/22 17:31
 * @Author     : zhanglp8181
 * @File       : live_contract_risk_review_browser_regression.mjs
 * @CallChain  : 双 Chromium 账号 → 合同初筛 → 低风险报告/高风险法务复核 → Runtime 终态
 * @Description: 验证结构化风险回执、法务真人候选、非审批结果和申请人最终通知闭环。
 */

import { chromium } from '../frontend-enterprise/node_modules/playwright/index.mjs';

const baseUrl = process.env.BROWSER_TEST_BASE_URL || 'http://127.0.0.1:5137';
const requesterUsername = process.env.BROWSER_TEST_USERNAME || 'user_demo';
const requesterPassword = process.env.BROWSER_TEST_PASSWORD || 'demo';
const reviewerUsername = process.env.BROWSER_TEST_REVIEWER_USERNAME || 'approver_demo';
const reviewerPassword = process.env.BROWSER_TEST_REVIEWER_PASSWORD || 'demo';
const legalAgentId = 'agent_7d062081c03b4e16';
const skillId = 'contract_risk_review';
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
    const text = message.text();
    if (text.includes('status of 403 (Forbidden)')) {
      expectedAuthorizationDenials.push(`${actor}: ${text}`);
      return;
    }
    browserErrors.push(`${actor} console: ${text}`);
  });
}

/** 登录指定账号，并等待认证令牌写入独立浏览器上下文。 */
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

/** 使用页面所属账号令牌读取受保护 API。 */
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

/** 等待指定会话的合同风险 Runtime 满足断言。 */
async function waitForRuntime(sessionId, predicate, description) {
  for (let attempt = 0; attempt < 180; attempt += 1) {
    const trace = await api(
      requesterPage,
      `/api/enterprise/traces/${sessionId}?tenant_id=tenant_demo`,
    );
    const runtime = trace?.sop_runtime?.find((candidate) => candidate.skill_id === skillId);
    if (runtime && predicate(runtime)) return runtime;
    await requesterPage.waitForTimeout(500);
  }
  throw new Error(`未等到合同风险 Runtime ${description}`);
}

/** 等待最终助手回复包含指定标记，避免把 Runtime 终态当作页面输出完成。 */
async function waitForPageText(page, markers) {
  let latestText = '';
  for (let attempt = 0; attempt < 180; attempt += 1) {
    latestText = await page.locator('body').innerText();
    if (markers.every((marker) => latestText.includes(marker))) return latestText;
    await page.waitForTimeout(500);
  }
  return latestText;
}

/** 等待复核人任务箱出现当前高风险合同工作项。 */
async function waitForReviewerWorkItem(sessionId) {
  for (let attempt = 0; attempt < 120; attempt += 1) {
    const items = await api(reviewerPage, '/api/work-items?tenant_id=tenant_demo&view=pending');
    const item = items.find(
      (candidate) => candidate.session_id === sessionId && candidate.skill_id === skillId,
    );
    if (item) return item;
    await reviewerPage.waitForTimeout(500);
  }
  throw new Error('未等到法务高风险合同复核任务');
}

/** 从数字员工广场发起一段全新的法务对话。 */
async function startLegalChat() {
  await requesterPage.goto(`${baseUrl}/workspace/gallery`);
  await requesterPage.getByRole('textbox', { name: '搜索数字员工' }).fill('法务');
  const agentCard = requesterPage.locator('.gongge-employee-card').filter({ hasText: /^法务/ }).first();
  await agentCard.getByRole('button', { name: '发起对话' }).click();
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
  if (!sessionId) throw new Error('无法取得合同风险审查会话 ID');
  return sessionId;
}

try {
  await login(requesterPage, requesterUsername, requesterPassword);
  await login(reviewerPage, reviewerUsername, reviewerPassword);

  const lowRiskComposer = await startLegalChat();
  const lowRiskSessionId = await sendInitialMessage(
    lowRiskComposer,
    '请仅对以下软件采购合同关键条款做风险初筛，不需要审查合同全文。合同文本：'
      + '双方对履约过程中知悉的商业秘密承担保密义务，'
      + '未经对方书面同意不得向第三方披露，但法律法规或监管机关要求披露的除外，'
      + '保密义务自合同生效起至合同终止后三年。',
  );
  const lowRiskCompleted = await waitForRuntime(
    lowRiskSessionId,
    (runtime) => runtime.status === 'succeeded'
      && runtime.current_node_id === 'node_contract_risk_report'
      && (runtime.operations?.length || 0) === 1,
    '低风险初筛报告完成',
  );
  const lowRiskOperation = lowRiskCompleted.operations.find(
    (operation) => operation.operation_name === 'contract.risk_assess',
  );
  // Runtime 已落终态后重新读取持久消息，避免流式连接抖动让页面停留在旧渲染帧。
  await requesterPage.reload();
  const lowRiskPageText = await waitForPageText(requesterPage, [
    lowRiskOperation?.result?.assessment_id,
    '不等于正式',
  ]);
  await requesterPage.screenshot({
    path: `.dev/${lowRiskSessionId}-contract-risk-low.png`,
    fullPage: true,
  });

  const highRiskComposer = await startLegalChat();
  const highRiskSessionId = await sendInitialMessage(
    highRiskComposer,
    '请仅对以下软件采购合同关键条款做风险初筛，不需要审查合同全文。合同文本：'
      + '供应商对任何违约及全部损失承担无限责任；'
      + '采购方可以无需理由单方任意解除合同，供应商不得提出异议。',
  );
  const highRiskWaiting = await waitForRuntime(
    highRiskSessionId,
    (runtime) => runtime.status === 'waiting'
      && runtime.current_node_id === 'node_high_risk_legal_review'
      && (runtime.operations?.length || 0) === 1,
    '高风险等待法务真人复核',
  );
  const highRiskOperation = highRiskWaiting.operations.find(
    (operation) => operation.operation_name === 'contract.risk_assess',
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
  const workItemRow = reviewerPage.getByText(skillId, { exact: true }).first();
  await workItemRow.waitFor({ state: 'visible' });
  await workItemRow.click();
  await reviewerPage.getByRole('button', { name: '认领任务' }).click();
  await reviewerPage.getByPlaceholder('请填写本次处理结果和依据').fill(
    '确认命中无限责任和单方任意解除两项高风险；建议设置累计责任上限，'
      + '并限定解除事由、通知期与补救期。该意见基于当前条款，需补充完整合同后定稿。',
  );
  await reviewerPage.getByRole('button', { name: '提交复核意见' }).click();

  const highRiskCompleted = await waitForRuntime(
    highRiskSessionId,
    (runtime) => runtime.status === 'succeeded'
      && runtime.current_node_id === 'node_high_risk_review_completed'
      && (runtime.operations?.length || 0) === 1,
    '高风险法务人工复核完成',
  );
  await requesterPage.reload();
  const highRiskPageText = await waitForPageText(requesterPage, [
    highRiskOperation?.result?.assessment_id,
    '无限责任',
    '正式签署前',
  ]);
  await requesterPage.screenshot({
    path: `.dev/${highRiskSessionId}-contract-risk-high-requester.png`,
    fullPage: true,
  });
  await reviewerPage.screenshot({
    path: `.dev/${highRiskSessionId}-contract-risk-high-reviewer.png`,
    fullPage: true,
  });

  const completedWorkItem = highRiskCompleted.work_items.find(
    (item) => item.id === offeredWorkItem.id,
  );
  console.log(JSON.stringify({
    credentials: {
      requester: `${requesterUsername} / ${requesterPassword}`,
      reviewer: `${reviewerUsername} / ${reviewerPassword}`,
    },
    lowRiskSessionId,
    lowRiskOperation,
    lowRiskFinalNode: lowRiskCompleted.current_node_id,
    lowRiskBoundaryVisible: lowRiskPageText.includes('不等于正式'),
    highRiskSessionId,
    highRiskOperation,
    highRiskWaitingStatus: highRiskWaiting.status,
    requesterAllowedActions: requesterView?.allowed_actions,
    roleCandidateSnapshot: offeredWorkItem.candidates,
    completedWorkItem,
    highRiskFinalNode: highRiskCompleted.current_node_id,
    highRiskReviewVisible: highRiskPageText.includes('无限责任'),
    expectedAuthorizationDenialCount: expectedAuthorizationDenials.length,
    browserErrors,
  }, null, 2));

  if (lowRiskCompleted.skill_version !== '2.1.0') process.exitCode = 2;
  if (lowRiskOperation?.result?.risk_level !== 'low') process.exitCode = 3;
  if (lowRiskOperation?.result?.requires_human_review !== false) process.exitCode = 4;
  if (!lowRiskPageText.includes(lowRiskOperation?.result?.assessment_id)) process.exitCode = 5;
  if (!lowRiskPageText.includes('不等于正式')) process.exitCode = 6;
  if (highRiskOperation?.result?.risk_level !== 'high') process.exitCode = 7;
  if (highRiskOperation?.result?.requires_human_review !== true) process.exitCode = 8;
  if (highRiskOperation?.result?.risk_points?.length !== 2) process.exitCode = 9;
  if ((requesterView?.allowed_actions || []).length !== 0) process.exitCode = 10;
  if (offeredWorkItem.allowed_actions.join(',') !== 'claim') process.exitCode = 11;
  if (offeredWorkItem.candidates.length !== 1) process.exitCode = 12;
  if (offeredWorkItem.candidates[0]?.user_id !== reviewerUsername) process.exitCode = 13;
  if (!offeredWorkItem.candidates[0]?.source_role_codes.includes('legal_contract_reviewer')) {
    process.exitCode = 14;
  }
  if (completedWorkItem?.outcome !== 'reviewed') process.exitCode = 15;
  if (completedWorkItem?.assignee_user_id !== reviewerUsername) process.exitCode = 16;
  if (!completedWorkItem?.comment?.includes('单方任意解除')) process.exitCode = 17;
  if (!highRiskPageText.includes(highRiskOperation?.result?.assessment_id)) process.exitCode = 18;
  if (!highRiskPageText.includes('正式签署前')) process.exitCode = 19;
  if (browserErrors.length) process.exitCode = 20;
} finally {
  await requesterContext.close();
  await reviewerContext.close();
  await browser.close();
}

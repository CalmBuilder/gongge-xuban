/**
 * @Time       : 2026/07/22 23:58
 * @Author     : zhanglp8181
 * @File       : live_hr_certificate_browser_regression.mjs
 * @CallChain  : 双 Chromium 账号 → 本人证明申请 → 常规开具/特殊复核 → PDF 下载
 * @Description: 验证可信员工身份、明确确认、HR 角色复核、恢复执行和真实演示下载闭环。
 */

import { chromium } from '../frontend-enterprise/node_modules/playwright/index.mjs';

const baseUrl = process.env.BROWSER_TEST_BASE_URL || 'http://127.0.0.1:5137';
const requesterUsername = process.env.BROWSER_TEST_USERNAME || 'user_demo';
const requesterPassword = process.env.BROWSER_TEST_PASSWORD || 'demo';
const reviewerUsername = process.env.BROWSER_TEST_REVIEWER_USERNAME || 'approver_demo';
const reviewerPassword = process.env.BROWSER_TEST_REVIEWER_PASSWORD || 'demo';
const humanResourcesAgentId = 'agent_9d3d1fdf171049ed';
const skillId = 'skill_hr_cert_issue_001';
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
      expectedAuthorizationDenials.push(`${actor} console: ${text}`);
      return;
    }
    browserErrors.push(`${actor} console: ${text}`);
  });
}

/** 登录指定账号，并等待认证令牌写入当前浏览器上下文。 */
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

/** 使用页面所属账号的令牌读取受保护 API。 */
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

/** 等待在职证明 Runtime 达到指定状态。 */
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
  throw new Error(`未等到在职证明 Runtime ${description}`);
}

/** 等待复核人的任务箱出现本次特殊证明工作项。 */
async function waitForReviewerWorkItem(sessionId) {
  for (let attempt = 0; attempt < 120; attempt += 1) {
    const items = await api(
      reviewerPage,
      '/api/work-items?tenant_id=tenant_demo&view=pending',
    );
    const item = items.find(
      (candidate) => candidate.session_id === sessionId && candidate.skill_id === skillId,
    );
    if (item) return item;
    await reviewerPage.waitForTimeout(500);
  }
  throw new Error('未等到 HR 证明复核工作项');
}

/** 从数字员工广场发起一段新的人事对话。 */
async function startHumanResourcesChat() {
  await requesterPage.goto(`${baseUrl}/workspace/gallery`);
  await requesterPage.getByRole('textbox', { name: '搜索数字员工' }).fill('人事');
  const agentCard = requesterPage.locator('.gongge-employee-card').filter({ hasText: /^人事/ }).first();
  await agentCard.getByRole('button', { name: '发起对话' }).click();
  await requesterPage.waitForURL(new RegExp(`/workspace/chat/draft/${humanResourcesAgentId}$`));
  await requesterPage.waitForTimeout(2500);
  return requesterPage.getByPlaceholder('输入消息，按 Enter 发送...');
}

/** 发送首条消息，并从正式会话地址提取会话标识。 */
async function sendInitialMessage(composer, message) {
  await composer.fill(message);
  await requesterPage.getByRole('button', { name: '发送' }).click();
  await requesterPage.waitForURL(/\/workspace\/chat\/(session_[^/?#]+)/, { timeout: 60_000 });
  const sessionId = requesterPage.url().match(/\/workspace\/chat\/(session_[^/?#]+)/)?.[1];
  if (!sessionId) throw new Error('无法取得证明申请会话 ID');
  return sessionId;
}

try {
  await login(requesterPage, requesterUsername, requesterPassword);
  await login(reviewerPage, reviewerUsername, reviewerPassword);

  const regularComposer = await startHumanResourcesChat();
  const regularSessionId = await sendInitialMessage(
    regularComposer,
    '请为我开具一份中文在职证明，用于普通业务',
  );
  const regularConfirmation = await waitForRuntime(
    regularSessionId,
    (runtime) => runtime.status === 'waiting'
      && runtime.current_node_id === 'node_confirm_certificate_request'
      && (runtime.operations?.length || 0) === 0,
    '常规证明等待明确确认且零调用',
  );
  await regularComposer.fill('继续吧');
  await requesterPage.getByRole('button', { name: '发送' }).click();
  const ambiguousConfirmation = await waitForRuntime(
    regularSessionId,
    (runtime) => runtime.status === 'waiting'
      && runtime.current_node_id === 'node_confirm_certificate_request'
      && (runtime.operations?.length || 0) === 0,
    '模糊表达后继续阻断',
  );
  await regularComposer.fill('确认开具');
  await requesterPage.getByRole('button', { name: '发送' }).click();
  const regularCompleted = await waitForRuntime(
    regularSessionId,
    (runtime) => runtime.status === 'succeeded'
      && runtime.current_node_id === 'node_certificate_issued'
      && (runtime.operations?.length || 0) === 1,
    '常规证明开具成功',
  );
  const regularOperation = regularCompleted.operations.find(
    (operation) => operation.operation_name === 'hr.cert_issue',
  );
  const pdfResponse = await requesterPage.request.get(
    `${baseUrl}${regularOperation?.result?.download_url}`,
  );
  await requesterPage.screenshot({
    path: `.dev/${regularSessionId}-hr-certificate-regular.png`,
    fullPage: true,
  });

  const specialComposer = await startHumanResourcesChat();
  const specialSessionId = await sendInitialMessage(
    specialComposer,
    '请为我开具一份中文收入证明，用于贷款',
  );
  await waitForRuntime(
    specialSessionId,
    (runtime) => runtime.status === 'waiting'
      && runtime.current_node_id === 'node_confirm_certificate_request'
      && (runtime.operations?.length || 0) === 0,
    '收入证明等待明确确认',
  );
  await specialComposer.fill('确认开具');
  await requesterPage.getByRole('button', { name: '发送' }).click();
  const reviewWaiting = await waitForRuntime(
    specialSessionId,
    (runtime) => runtime.status === 'waiting'
      && runtime.current_node_id === 'node_special_certificate_review'
      && (runtime.operations?.length || 0) === 0,
    '收入证明等待角色复核且零调用',
  );

  const requesterItems = await api(
    requesterPage,
    '/api/work-items?tenant_id=tenant_demo&view=all',
  );
  const requesterView = requesterItems.find(
    (candidate) => candidate.session_id === specialSessionId && candidate.skill_id === skillId,
  );
  const offeredWorkItem = await waitForReviewerWorkItem(specialSessionId);
  await reviewerPage.goto(`${baseUrl}/enterprise/work-items`);
  const workItemRow = reviewerPage.getByText(skillId, { exact: true }).first();
  await workItemRow.waitFor({ state: 'visible' });
  await workItemRow.click();
  await reviewerPage.getByRole('button', { name: '认领任务' }).click();
  await reviewerPage.getByPlaceholder('请填写本次处理结果和依据').fill(
    '已核验贷款用途和本人申请信息，同意生成演示收入证明',
  );
  await reviewerPage.getByRole('button', { name: '批准并继续开具' }).click();

  const specialCompleted = await waitForRuntime(
    specialSessionId,
    (runtime) => runtime.status === 'succeeded'
      && runtime.current_node_id === 'node_certificate_issued'
      && (runtime.operations?.length || 0) === 1,
    '复核批准后恢复并完成证明开具',
  );
  await requesterPage.reload();
  await requesterPage.waitForTimeout(1200);
  await requesterPage.screenshot({
    path: `.dev/${specialSessionId}-hr-certificate-special-requester.png`,
    fullPage: true,
  });
  await reviewerPage.screenshot({
    path: `.dev/${specialSessionId}-hr-certificate-special-reviewer.png`,
    fullPage: true,
  });

  const completedWorkItem = specialCompleted.work_items.find(
    (item) => item.id === offeredWorkItem.id,
  );
  const specialOperation = specialCompleted.operations.find(
    (operation) => operation.operation_name === 'hr.cert_issue',
  );
  console.log(JSON.stringify({
    credentials: {
      requester: `${requesterUsername} / ${requesterPassword}`,
      reviewer: `${reviewerUsername} / ${reviewerPassword}`,
    },
    regularSessionId,
    regularConfirmationNode: regularConfirmation.current_node_id,
    ambiguousConfirmationNode: ambiguousConfirmation.current_node_id,
    regularOperation,
    pdfStatus: pdfResponse.status(),
    pdfContentType: pdfResponse.headers()['content-type'],
    specialSessionId,
    reviewWaitingStatus: reviewWaiting.status,
    requesterAllowedActions: requesterView?.allowed_actions,
    roleCandidateSnapshot: offeredWorkItem.candidates,
    completedWorkItem,
    specialOperation,
    finalNode: specialCompleted.current_node_id,
    expectedAuthorizationDenialCount: expectedAuthorizationDenials.length,
    browserErrors,
  }, null, 2));

  if (regularCompleted.skill_version !== '2.0.0') process.exitCode = 2;
  if (regularOperation?.request?.employee_id !== 'E002') process.exitCode = 3;
  if (regularOperation?.request?.employee_name !== '演示员工') process.exitCode = 4;
  if (regularOperation?.request?.cert_type !== 'employment') process.exitCode = 5;
  if (regularOperation?.result?.status !== 'issued') process.exitCode = 6;
  if (!regularOperation?.result?.cert_id?.startsWith('CERT-')) process.exitCode = 7;
  if (pdfResponse.status() !== 200) process.exitCode = 8;
  if (pdfResponse.headers()['content-type'] !== 'application/pdf') process.exitCode = 9;
  if ((requesterView?.allowed_actions || []).length !== 0) process.exitCode = 10;
  if (offeredWorkItem.allowed_actions.join(',') !== 'claim') process.exitCode = 11;
  if (offeredWorkItem.candidates.length !== 1) process.exitCode = 12;
  if (offeredWorkItem.candidates[0]?.user_id !== reviewerUsername) process.exitCode = 13;
  if (!offeredWorkItem.candidates[0]?.source_role_codes.includes('hr_certificate_reviewer')) {
    process.exitCode = 14;
  }
  if (completedWorkItem?.outcome !== 'approved') process.exitCode = 15;
  if (completedWorkItem?.assignee_user_id !== reviewerUsername) process.exitCode = 16;
  if (!completedWorkItem?.comment?.includes('贷款用途')) process.exitCode = 17;
  if (specialOperation?.request?.employee_id !== 'E002') process.exitCode = 18;
  if (specialOperation?.request?.cert_type !== 'income') process.exitCode = 19;
  if (specialOperation?.result?.status !== 'issued') process.exitCode = 20;
  if (specialCompleted.current_node_id !== 'node_certificate_issued') process.exitCode = 21;
  if (browserErrors.length) process.exitCode = 22;
} finally {
  await requesterContext.close();
  await reviewerContext.close();
  await browser.close();
}

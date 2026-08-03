/**
 * @Time       : 2026/07/22 23:48
 * @Author     : zhanglp8181
 * @File       : live_permission_grant_browser_regression.mjs
 * @CallChain  : 双 Chromium 账号 → 本人权限申请 → 自动开通/高权限审批 → Runtime 终态
 * @Description: 验证流程委托执行、明确确认、角色候选审批和不虚构高权限开通的浏览器闭环。
 */

import { chromium } from '../frontend-enterprise/node_modules/playwright/index.mjs';

const baseUrl = process.env.BROWSER_TEST_BASE_URL || 'http://127.0.0.1:5137';
const requesterUsername = process.env.BROWSER_TEST_USERNAME || 'user_demo';
const requesterPassword = process.env.BROWSER_TEST_PASSWORD || 'demo';
const approverUsername = process.env.BROWSER_TEST_APPROVER_USERNAME || 'approver_demo';
const approverPassword = process.env.BROWSER_TEST_APPROVER_PASSWORD || 'demo';
const informationTechnologyAgentId = 'agent_258e75c664b34151';
const skillId = 'skill_perm_grant_routing_001';
const browserErrors = [];
const expectedAuthorizationDenials = [];

const browser = await chromium.launch({ headless: true });
const requesterContext = await browser.newContext({ viewport: { width: 1440, height: 1100 } });
const approverContext = await browser.newContext({ viewport: { width: 1440, height: 1100 } });
const requesterPage = await requesterContext.newPage();
const approverPage = await approverContext.newPage();

for (const [actor, page] of [['requester', requesterPage], ['approver', approverPage]]) {
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

/** 登录指定浏览器上下文并等待本地认证令牌。 */
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

/** 使用页面所属账号的认证令牌读取受保护 API。 */
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

/** 等待权限开通 SOP Runtime 满足指定状态断言。 */
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
  throw new Error(`未等到权限开通 Runtime ${description}`);
}

/** 等待审批人的任务箱出现指定会话的高权限工作项。 */
async function waitForApproverWorkItem(sessionId) {
  for (let attempt = 0; attempt < 120; attempt += 1) {
    const items = await api(
      approverPage,
      '/api/work-items?tenant_id=tenant_demo&view=pending',
    );
    const item = items.find(
      (candidate) => candidate.session_id === sessionId && candidate.skill_id === skillId,
    );
    if (item) return item;
    await approverPage.waitForTimeout(500);
  }
  throw new Error('未等到 IT 高权限审批工作项');
}

/** 从数字员工广场发起一段新的 IT 对话并返回输入框。 */
async function startInformationTechnologyChat() {
  await requesterPage.goto(`${baseUrl}/workspace/gallery`);
  await requesterPage.getByRole('textbox', { name: '搜索数字员工' }).fill('IT');
  const agentCard = requesterPage.locator('.gongge-employee-card').filter({ hasText: /^IT/ }).first();
  await agentCard.getByRole('button', { name: '发起对话' }).click();
  await requesterPage.waitForURL(new RegExp(`/workspace/chat/draft/${informationTechnologyAgentId}$`));
  await requesterPage.waitForTimeout(2500);
  return requesterPage.getByPlaceholder('输入消息，按 Enter 发送...');
}

/** 发送首条消息并从正式会话 URL 提取会话标识。 */
async function sendInitialMessage(composer, message) {
  await composer.fill(message);
  await requesterPage.getByRole('button', { name: '发送' }).click();
  await requesterPage.waitForURL(/\/workspace\/chat\/(session_[^/?#]+)/, { timeout: 60_000 });
  const sessionId = requesterPage.url().match(/\/workspace\/chat\/(session_[^/?#]+)/)?.[1];
  if (!sessionId) throw new Error('无法取得权限申请会话 ID');
  return sessionId;
}

try {
  await login(requesterPage, requesterUsername, requesterPassword);
  await login(approverPage, approverUsername, approverPassword);

  const readComposer = await startInformationTechnologyChat();
  const readSessionId = await sendInitialMessage(readComposer, '申请 CRM 客户资料只读权限');
  const readConfirmationWaiting = await waitForRuntime(
    readSessionId,
    (runtime) => runtime.status === 'waiting'
      && runtime.current_node_id === 'node_confirm_access_request'
      && (runtime.operations?.length || 0) === 0,
    '普通权限等待明确确认且零调用',
  );
  await readComposer.fill('继续吧');
  await requesterPage.getByRole('button', { name: '发送' }).click();
  const ambiguousConfirmation = await waitForRuntime(
    readSessionId,
    (runtime) => runtime.status === 'waiting'
      && runtime.current_node_id === 'node_confirm_access_request'
      && (runtime.operations?.length || 0) === 0,
    '模糊表达后继续阻断',
  );
  await readComposer.fill('确认申请');
  await requesterPage.getByRole('button', { name: '发送' }).click();
  const readCompleted = await waitForRuntime(
    readSessionId,
    (runtime) => runtime.status === 'succeeded'
      && runtime.current_node_id === 'node_access_granted'
      && (runtime.operations?.length || 0) === 1,
    '本人只读权限自动开通',
  );
  await requesterPage.screenshot({
    path: `.dev/${readSessionId}-permission-read.png`,
    fullPage: true,
  });

  const adminComposer = await startInformationTechnologyChat();
  const adminSessionId = await sendInitialMessage(adminComposer, '申请 ERP 用户管理管理员权限');
  await waitForRuntime(
    adminSessionId,
    (runtime) => runtime.status === 'waiting'
      && runtime.current_node_id === 'node_confirm_access_request'
      && (runtime.operations?.length || 0) === 0,
    '高权限等待明确确认',
  );
  await adminComposer.fill('确认申请');
  await requesterPage.getByRole('button', { name: '发送' }).click();
  const approvalWaiting = await waitForRuntime(
    adminSessionId,
    (runtime) => runtime.status === 'waiting'
      && runtime.current_node_id === 'node_high_access_review'
      && (runtime.operations?.length || 0) === 0,
    '高权限等待角色候选审批',
  );

  const requesterItems = await api(
    requesterPage,
    '/api/work-items?tenant_id=tenant_demo&view=all',
  );
  const requesterView = requesterItems.find(
    (candidate) => candidate.session_id === adminSessionId && candidate.skill_id === skillId,
  );
  const offeredWorkItem = await waitForApproverWorkItem(adminSessionId);
  await approverPage.goto(`${baseUrl}/enterprise/work-items`);
  const workItemRow = approverPage.getByText(skillId, { exact: true }).first();
  await workItemRow.waitFor({ state: 'visible' });
  await workItemRow.click();
  await approverPage.getByRole('button', { name: '认领任务' }).click();
  await approverPage.getByPlaceholder('请填写本次处理结果和依据').fill(
    '已核验岗位职责和最小权限范围，同意本次高权限申请进入后续实施',
  );
  await approverPage.getByRole('button', { name: '批准并继续开通' }).click();

  const approvalCompleted = await waitForRuntime(
    adminSessionId,
    (runtime) => runtime.status === 'succeeded'
      && runtime.current_node_id === 'node_access_granted'
      && (runtime.operations?.length || 0) === 1,
    '审批恢复后完成受控开通',
  );
  await requesterPage.reload();
  await requesterPage.waitForTimeout(1200);
  await requesterPage.screenshot({
    path: `.dev/${adminSessionId}-permission-high-requester.png`,
    fullPage: true,
  });
  await approverPage.screenshot({
    path: `.dev/${adminSessionId}-permission-high-approver.png`,
    fullPage: true,
  });

  const readOperation = readCompleted.operations.find(
    (operation) => operation.operation_name === 'it.grant_permission',
  );
  const completedWorkItem = approvalCompleted.work_items.find(
    (item) => item.id === offeredWorkItem.id,
  );
  const highPermissionOperation = approvalCompleted.operations.find(
    (operation) => operation.operation_name === 'it.grant_permission',
  );
  console.log(JSON.stringify({
    readSessionId,
    readConfirmationNode: readConfirmationWaiting.current_node_id,
    ambiguousConfirmationNode: ambiguousConfirmation.current_node_id,
    readOperation,
    adminSessionId,
    approvalWaitingStatus: approvalWaiting.status,
    requesterAllowedActions: requesterView?.allowed_actions,
    roleCandidateSnapshot: offeredWorkItem.candidates,
    completedWorkItem,
    highPermissionOperation,
    finalNode: approvalCompleted.current_node_id,
    expectedAuthorizationDenialCount: expectedAuthorizationDenials.length,
    browserErrors,
  }, null, 2));

  if (readCompleted.skill_version !== '2.2.0') process.exitCode = 2;
  if (readOperation?.request?.employee_id !== 'E002') process.exitCode = 3;
  if (readOperation?.request?.system !== 'CRM') process.exitCode = 4;
  if (readOperation?.request?.access_level !== 'read') process.exitCode = 5;
  if (readOperation?.result?.status !== 'granted') process.exitCode = 6;
  if (!readOperation?.result?.grant_id?.startsWith('GRANT')) process.exitCode = 7;
  if ((requesterView?.allowed_actions || []).length !== 0) process.exitCode = 8;
  if (offeredWorkItem.allowed_actions.join(',') !== 'claim') process.exitCode = 9;
  if (offeredWorkItem.candidates.length !== 1) process.exitCode = 10;
  if (offeredWorkItem.candidates[0]?.user_id !== approverUsername) process.exitCode = 11;
  if (!offeredWorkItem.candidates[0]?.source_role_codes.includes('it_access_approver')) {
    process.exitCode = 12;
  }
  if (completedWorkItem?.outcome !== 'approved') process.exitCode = 13;
  if (completedWorkItem?.assignee_user_id !== approverUsername) process.exitCode = 14;
  if (!completedWorkItem?.comment?.includes('最小权限范围')) process.exitCode = 15;
  if (highPermissionOperation?.request?.employee_id !== 'E002') process.exitCode = 16;
  if (highPermissionOperation?.request?.access_level !== 'admin') process.exitCode = 17;
  if (highPermissionOperation?.result?.status !== 'granted') process.exitCode = 18;
  if (!highPermissionOperation?.result?.grant_id?.startsWith('GRANT')) process.exitCode = 19;
  if (approvalCompleted.current_node_id !== 'node_access_granted') process.exitCode = 20;
  if (browserErrors.length) process.exitCode = 21;
} finally {
  await requesterContext.close();
  await approverContext.close();
  await browser.close();
}

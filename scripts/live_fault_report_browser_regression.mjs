/**
 * @Time       : 2026/07/22 13:52
 * @Author     : zhanglp8181
 * @File       : live_fault_report_browser_regression.mjs
 * @CallChain  : 双 Chromium 账号 → 报修确认 → 工程师任务箱 → 报修人验收 → 工单关闭
 * @Description: 验证真实员工角色、非审批维修任务、异步恢复和报修人关闭的完整生命周期。
 */

import { chromium } from '../frontend-enterprise/node_modules/playwright/index.mjs';

const baseUrl = process.env.BROWSER_TEST_BASE_URL || 'http://127.0.0.1:5137';
const requesterUsername = process.env.BROWSER_TEST_USERNAME || 'user_demo';
const requesterPassword = process.env.BROWSER_TEST_PASSWORD || 'demo';
const engineerUsername = process.env.BROWSER_TEST_ENGINEER_USERNAME || 'it_engineer_demo';
const engineerPassword = process.env.BROWSER_TEST_ENGINEER_PASSWORD || 'demo';
const informationTechnologyAgentId = 'agent_258e75c664b34151';
const skillId = 'fault_report_v1';
const browserErrors = [];

const browser = await chromium.launch({ headless: true });
const requesterContext = await browser.newContext({ viewport: { width: 1440, height: 1100 } });
const engineerContext = await browser.newContext({ viewport: { width: 1440, height: 1100 } });
const requesterPage = await requesterContext.newPage();
const engineerPage = await engineerContext.newPage();

for (const [actor, page] of [['requester', requesterPage], ['engineer', engineerPage]]) {
  page.on('pageerror', (error) => browserErrors.push(`${actor} pageerror: ${error.message}`));
  page.on('console', (message) => {
    if (message.type() === 'error') browserErrors.push(`${actor} console: ${message.text()}`);
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

/** 等待故障 SOP Runtime 满足指定状态断言。 */
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
  throw new Error(`未等到故障报修 Runtime ${description}`);
}

/** 等待工程师任务箱出现当前会话的活动工作项。 */
async function waitForEngineerWorkItem(sessionId) {
  for (let attempt = 0; attempt < 120; attempt += 1) {
    const items = await api(
      engineerPage,
      '/api/work-items?tenant_id=tenant_demo&view=pending',
    );
    const item = items.find(
      (candidate) => candidate.session_id === sessionId && candidate.skill_id === skillId,
    );
    if (item) return item;
    await engineerPage.waitForTimeout(500);
  }
  throw new Error('未等到 IT 支持工程师工作项');
}

try {
  await login(requesterPage, requesterUsername, requesterPassword);
  await login(engineerPage, engineerUsername, engineerPassword);

  await requesterPage.goto(`${baseUrl}/workspace/gallery`);
  await requesterPage.getByRole('textbox', { name: '搜索数字员工' }).fill('IT');
  const agentCard = requesterPage.locator('.gongge-employee-card').filter({ hasText: /^IT/ }).first();
  await agentCard.getByRole('button', { name: '发起对话' }).click();
  await requesterPage.waitForURL(new RegExp(`/workspace/chat/draft/${informationTechnologyAgentId}$`));
  await requesterPage.waitForTimeout(2500);

  const requesterComposer = requesterPage.getByPlaceholder('输入消息，按 Enter 发送...');
  await requesterComposer.fill('VPN 无法连接，影响我远程办公，请帮我报修');
  await requesterPage.getByRole('button', { name: '发送' }).click();
  await requesterPage.waitForURL(/\/workspace\/chat\/(session_[^/?#]+)/, { timeout: 60_000 });
  const sessionId = requesterPage.url().match(/\/workspace\/chat\/(session_[^/?#]+)/)?.[1];
  if (!sessionId) throw new Error('无法取得报修会话 ID');

  await waitForRuntime(
    sessionId,
    (runtime) => runtime.status === 'waiting' && (runtime.operations?.length || 0) === 0,
    '等待提交确认且零调用',
  );
  await requesterComposer.fill('确认报修');
  await requesterPage.getByRole('button', { name: '发送' }).click();
  const engineerWaiting = await waitForRuntime(
    sessionId,
    (runtime) => runtime.status === 'waiting'
      && runtime.current_node_id === 'node_engineer_resolution'
      && (runtime.operations?.length || 0) === 1,
    '建单后等待工程师',
  );

  const offeredWorkItem = await waitForEngineerWorkItem(sessionId);
  await engineerPage.goto(`${baseUrl}/enterprise/work-items`);
  const workItemRow = engineerPage.getByText(skillId, { exact: true }).first();
  await workItemRow.waitFor({ state: 'visible' });
  await workItemRow.click();
  await engineerPage.getByRole('button', { name: '认领任务' }).click();
  await engineerPage.getByPlaceholder('请填写本次处理结果和依据').fill(
    '已重置 VPN 客户端配置并刷新访问证书，远程连接测试通过',
  );
  await engineerPage.getByRole('button', { name: '标记已解决' }).click();

  const requesterVerification = await waitForRuntime(
    sessionId,
    (runtime) => runtime.status === 'waiting'
      && runtime.current_node_id === 'node_confirm_resolution'
      && (runtime.work_items || []).some((item) => item.outcome === 'resolved'),
    '工程师解决后等待报修人验收',
  );
  await requesterPage.reload();
  await requesterPage.waitForTimeout(1000);
  const verificationComposer = requesterPage.getByPlaceholder('输入消息，按 Enter 发送...');
  await verificationComposer.fill('确认已恢复');
  await requesterPage.getByRole('button', { name: '发送' }).click();
  const completed = await waitForRuntime(
    sessionId,
    (runtime) => runtime.status === 'succeeded'
      && runtime.current_node_id === 'node_ticket_closed'
      && (runtime.operations?.length || 0) === 2,
    '报修人验收并关闭工单',
  );

  await requesterPage.screenshot({
    path: `.dev/${sessionId}-fault-report-lifecycle.png`,
    fullPage: true,
  });
  await engineerPage.screenshot({
    path: `.dev/${sessionId}-fault-report-engineer.png`,
    fullPage: true,
  });

  const createOperation = completed.operations.find(
    (operation) => operation.operation_name === 'it.ticket_create',
  );
  const closeOperation = completed.operations.find(
    (operation) => operation.operation_name === 'it.ticket_close',
  );
  const completedWorkItem = completed.work_items.find(
    (item) => item.id === offeredWorkItem.id,
  );
  console.log(JSON.stringify({
    sessionId,
    engineerWaitingStatus: engineerWaiting.status,
    requesterVerificationStatus: requesterVerification.status,
    roleCandidateSnapshot: offeredWorkItem.candidates,
    workItem: completedWorkItem,
    createOperation,
    closeOperation,
    finalNode: completed.current_node_id,
    browserErrors,
  }, null, 2));

  if (completed.skill_version !== '3.0.0') process.exitCode = 2;
  if (offeredWorkItem.allowed_actions.join(',') !== 'claim') process.exitCode = 3;
  if (offeredWorkItem.candidates.length !== 1) process.exitCode = 4;
  if (offeredWorkItem.candidates[0]?.user_id !== engineerUsername) process.exitCode = 5;
  if (!offeredWorkItem.candidates[0]?.source_role_codes.includes('it_support_engineer')) {
    process.exitCode = 6;
  }
  if (completedWorkItem?.outcome !== 'resolved') process.exitCode = 7;
  if (completedWorkItem?.assignee_user_id !== engineerUsername) process.exitCode = 8;
  if (!completedWorkItem?.comment?.includes('VPN 客户端配置')) process.exitCode = 9;
  if (createOperation?.request?.employee_id !== 'E002') process.exitCode = 10;
  if (closeOperation?.request?.requester_employee_id !== 'E002') process.exitCode = 11;
  if (closeOperation?.request?.ticket_id !== createOperation?.result?.ticket_id) {
    process.exitCode = 12;
  }
  if (closeOperation?.result?.status !== 'closed') process.exitCode = 13;
  if (completed.current_node_id !== 'node_ticket_closed') process.exitCode = 14;
  if (browserErrors.length) process.exitCode = 15;
} finally {
  await requesterContext.close();
  await engineerContext.close();
  await browser.close();
}

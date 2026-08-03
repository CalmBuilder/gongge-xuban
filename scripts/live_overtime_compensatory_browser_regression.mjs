/**
 * @Time       : 2026/07/27 18:55
 * @Author     : zhanglp8181
 * @File       : live_overtime_compensatory_browser_regression.mjs
 * @CallChain  : 双 Chromium 账号 → 加班政策/HR 回执 → 调休受理/HR 接管队列
 * @Description: 用真实模型验证加班调休成功链路和缺少事前审批的角色候选工作项。
 */

import { chromium } from '../frontend-enterprise/node_modules/playwright/index.mjs';

const baseUrl = process.env.BROWSER_TEST_BASE_URL || 'http://127.0.0.1:5137';
const requesterUsername = process.env.BROWSER_TEST_USERNAME;
const requesterPassword = process.env.BROWSER_TEST_PASSWORD;
const reviewerUsername = process.env.BROWSER_TEST_REVIEWER_USERNAME;
const reviewerPassword = process.env.BROWSER_TEST_REVIEWER_PASSWORD;
const humanResourcesAgentId = 'agent_9d3d1fdf171049ed';
const skillId = 'skill_overtime_compensatory_leave';
const browserErrors = [];
const expectedAuthorizationDenials = [];

if (!requesterUsername || !requesterPassword || !reviewerUsername || !reviewerPassword) {
  throw new Error('必须通过环境变量提供申请人和 HR 演示账号');
}

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
  page.on('requestfailed', (request) => {
    const errorText = request.failure()?.errorText || '';
    if (!errorText.includes('ERR_ABORTED')) {
      browserErrors.push(`${actor} requestfailed: ${request.method()} ${request.url()} ${errorText}`);
    }
  });
}

/** 登录指定演示账号，并写入跳过新手引导的本地状态。 */
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

/** 使用页面所属账号的短期令牌读取受保护接口。 */
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

/** 等待指定加班调休实例满足持久状态断言。 */
async function waitForRuntime(sessionId, predicate, description) {
  for (let attempt = 0; attempt < 300; attempt += 1) {
    const trace = await api(
      requesterPage,
      `/api/enterprise/traces/${sessionId}?tenant_id=tenant_demo`,
    );
    const runtime = trace?.sop_runtime?.find((candidate) => candidate.skill_id === skillId);
    if (runtime && predicate(runtime)) return runtime;
    await requesterPage.waitForTimeout(500);
  }
  throw new Error(`未等到加班调休 Runtime：${description}`);
}

/** 等待 HR 任务箱出现当前会话的可处理工作项。 */
async function waitForReviewerWorkItem(sessionId) {
  for (let attempt = 0; attempt < 180; attempt += 1) {
    const items = await api(reviewerPage, '/api/work-items?tenant_id=tenant_demo&view=pending');
    const item = items.find(
      (candidate) => candidate.session_id === sessionId && candidate.skill_id === skillId,
    );
    if (item) return item;
    await reviewerPage.waitForTimeout(500);
  }
  throw new Error('未等到 HR 加班调休核对任务');
}

/** 从广场发起一段全新的人事对话。 */
async function startHumanResourcesChat() {
  await requesterPage.goto(`${baseUrl}/workspace/gallery`);
  await requesterPage.getByRole('textbox', { name: '搜索数字员工' }).fill('人事');
  const card = requesterPage.locator('.gongge-employee-card').filter({ hasText: /^人事/ }).first();
  await card.getByRole('button', { name: '发起对话' }).click();
  await requesterPage.waitForURL(new RegExp(`/workspace/chat/draft/${humanResourcesAgentId}$`));
  await requesterPage.waitForTimeout(2000);
  return requesterPage.getByPlaceholder('输入消息，按 Enter 发送...');
}

/** 发送首条消息并返回正式会话标识。 */
async function sendInitialMessage(composer, message) {
  await composer.fill(message);
  await requesterPage.getByRole('button', { name: '发送' }).click();
  await requesterPage.waitForURL(/\/workspace\/chat\/(session_[^/?#]+)/, { timeout: 60_000 });
  const sessionId = requesterPage.url().match(/\/workspace\/chat\/(session_[^/?#]+)/)?.[1];
  if (!sessionId) throw new Error('无法取得加班调休会话 ID');
  return sessionId;
}

try {
  await login(requesterPage, requesterUsername, requesterPassword);
  await login(reviewerPage, reviewerUsername, reviewerPassword);

  const successComposer = await startHumanResourcesChat();
  const successSessionId = await sendInitialMessage(
    successComposer,
    '请只运行加班调休申请。我在2026-07-25休息日加班4小时，'
      + '已经在OA事前审批，原因是版本发布；计划于2026-07-30调休一天。',
  );
  const waitingConfirmation = await waitForRuntime(
    successSessionId,
    (runtime) => runtime.skill_version === '3.0.0'
      && runtime.current_node_id === 'confirm_compensatory_submit'
      && (runtime.operations?.length || 0) === 2,
    '成功路径等待明确确认',
  );
  await requesterPage.reload();
  await requesterPage.getByText('确认提交', { exact: false }).last().waitFor({
    state: 'visible',
    timeout: 30_000,
  });
  const confirmedComposer = requesterPage.getByPlaceholder('输入消息，按 Enter 发送...');
  await confirmedComposer.fill('确认提交');
  await requesterPage.getByRole('button', { name: '发送' }).click();
  const successCompleted = await waitForRuntime(
    successSessionId,
    (runtime) => runtime.status === 'succeeded'
      && runtime.current_node_id === 'compensatory_submitted_pending'
      && (runtime.operations?.length || 0) === 3,
    '调休申请提交待审批',
  );
  const submission = successCompleted.operations.find(
    (operation) => operation.operation_name === 'hr.leave_apply',
  );
  if (!submission?.result?.application_id?.startsWith('LEAVE-')) {
    throw new Error('成功路径未返回 LEAVE 申请单号');
  }
  if (submission?.result?.status !== 'pending') {
    throw new Error(`成功路径状态不是 pending：${submission?.result?.status}`);
  }
  await requesterPage.reload();
  await requesterPage
    .getByRole('main')
    .getByText(submission.result.application_id, { exact: false })
    .last()
    .waitFor({
      state: 'visible',
      timeout: 30_000,
    });
  await requesterPage.screenshot({
    path: `.dev/${successSessionId}-overtime-compensatory-success.png`,
    fullPage: true,
  });

  const reviewComposer = await startHumanResourcesChat();
  const reviewSessionId = await sendInitialMessage(
    reviewComposer,
    '请只运行加班调休申请。我在2026-07-26休息日加班4小时，'
      + '没有事前审批，原因是紧急修复；计划于2026-07-31调休一天。',
  );
  const reviewWaiting = await waitForRuntime(
    reviewSessionId,
    (runtime) => runtime.status === 'waiting'
      && runtime.current_node_id === 'hr_overtime_review'
      && (runtime.operations?.length || 0) === 2,
    '缺少事前审批等待 HR 核对',
  );
  const reviewWorkItem = await waitForReviewerWorkItem(reviewSessionId);
  if (!reviewWorkItem.candidates?.some(
    (candidate) => candidate.source_role_codes?.includes('hr_leave_specialist'),
  )) {
    throw new Error('HR 工作项未绑定 hr_leave_specialist 候选角色');
  }
  await requesterPage.reload();
  await requesterPage.getByText('HR', { exact: false }).last().waitFor({
    state: 'visible',
    timeout: 30_000,
  });
  await requesterPage.screenshot({
    path: `.dev/${reviewSessionId}-overtime-compensatory-requester.png`,
    fullPage: true,
  });
  await reviewerPage.goto(`${baseUrl}/enterprise/work-items`);
  await reviewerPage.getByText(skillId, { exact: true }).first().waitFor({
    state: 'visible',
    timeout: 30_000,
  });
  await reviewerPage.screenshot({
    path: `.dev/${reviewSessionId}-overtime-compensatory-reviewer.png`,
    fullPage: true,
  });

  if (browserErrors.length > 0) {
    throw new Error(`浏览器出现错误：${browserErrors.join(' | ')}`);
  }
  process.stdout.write(JSON.stringify({
    success: {
      session_id: successSessionId,
      application_id: submission.result.application_id,
      status: submission.result.status,
      operations: waitingConfirmation.operations.map((operation) => operation.operation_name)
        .concat('hr.leave_apply'),
    },
    review: {
      session_id: reviewSessionId,
      work_item_id: reviewWorkItem.id,
      status: reviewWaiting.status,
      current_node_id: reviewWaiting.current_node_id,
      candidates: reviewWorkItem.candidates,
    },
    expected_authorization_denials: expectedAuthorizationDenials,
    browser_errors: browserErrors,
  }, null, 2));
} finally {
  await browser.close();
}

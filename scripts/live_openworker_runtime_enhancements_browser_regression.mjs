/**
 * @Time       : 2026/08/13 12:20
 * @Author     : zhanglp8181
 * @File       : live_openworker_runtime_enhancements_browser_regression.mjs
 * @CallChain  : Chromium → 动态执行卡 → Skill catalog/Execution command/parallel batch API
 * @Description: 正反向验证运行中增加 Skill 与持久并行读取的真实页面闭环并保存验收截图。
 */

import { chromium } from '../frontend-enterprise/node_modules/playwright/index.mjs';
import fs from 'node:fs/promises';

const baseUrl = process.env.BROWSER_TEST_BASE_URL || 'http://127.0.0.1:5137';
const username = process.env.BROWSER_TEST_USERNAME || 'user_demo';
const password = process.env.BROWSER_TEST_PASSWORD || 'demo';
const instantSessionId = process.env.RUNTIME_DEMO_INSTANT_SESSION || 'session_f2f31266f64443f2';
const instantExecutionId = process.env.RUNTIME_DEMO_INSTANT_EXECUTION || 'sopinst_9afe476437244059';
const emptySessionId = process.env.RUNTIME_DEMO_EMPTY_SESSION || 'session_c055de9a39df444f';
const parallelSessionId = process.env.RUNTIME_DEMO_PARALLEL_SESSION || 'session_b901b543226247a0';
const parallelExecutionId = process.env.RUNTIME_DEMO_PARALLEL_EXECUTION || 'sopinst_4b3e9d8d4f3b48a4';
const cancelSessionId = process.env.RUNTIME_DEMO_CANCEL_SESSION || '';
const cancelExecutionId = process.env.RUNTIME_DEMO_CANCEL_EXECUTION || '';
const cancelSourceTurnId = process.env.RUNTIME_DEMO_CANCEL_SOURCE_TURN || '';
const cancelUnrelatedTurnId = process.env.RUNTIME_DEMO_CANCEL_UNRELATED_TURN || '';
const evidenceDir = 'docs/manuals/assets/openworker-runtime-enhancements';
const browserErrors = [];
const failedResponses = [];
let submittedCommandId = '';
let addSkillPostCount = 0;

if (!cancelSessionId || !cancelExecutionId || !cancelSourceTurnId || !cancelUnrelatedTurnId) {
  throw new Error(
    '精确 Dynamic 取消验收必须提供 RUNTIME_DEMO_CANCEL_SESSION、EXECUTION、SOURCE_TURN、UNRELATED_TURN',
  );
}

await fs.mkdir(evidenceDir, { recursive: true });
const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
const page = await context.newPage();
page.on('pageerror', (error) => browserErrors.push(`pageerror: ${error.message}`));
page.on('console', (message) => {
  if (message.type() === 'error') browserErrors.push(`console: ${message.text()}`);
});
page.on('response', (response) => {
  if (response.status() >= 400) failedResponses.push(`${response.status()} ${response.url()}`);
  if (response.request().method() === 'POST' && /\/api\/executions\/[^/]+\/commands$/.test(response.url())) {
    addSkillPostCount += 1;
    void response.json().then((body) => { submittedCommandId = body.command_id || ''; });
  }
});

async function login() {
  /** 登录现有普通演示用户，不在脚本中建立或修改账号。 */

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

async function api(path) {
  /** 使用浏览器内当前用户凭据读取权威 API 事实。 */

  return page.evaluate(async (target) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth'));
    const response = await fetch(target, { headers: { Authorization: `Bearer ${auth.token}` } });
    if (!response.ok) throw new Error(`${response.status} ${target}`);
    return response.json();
  }, path);
}

async function postApi(path, body) {
  /** 使用浏览器当前身份提交正式控制命令，并返回状态与响应。 */

  return page.evaluate(async ({ target, payload }) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth'));
    const response = await fetch(target, {
      method: 'POST',
      headers: { Authorization: `Bearer ${auth.token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    return { status: response.status, body: await response.json() };
  }, { target: path, payload: body });
}

async function openSession(sessionId) {
  /** 打开已由正式 Runtime 建立的聊天会话并等待执行卡加载。 */

  await page.goto(`${baseUrl}/workspace/chat/${sessionId}`);
  await page.getByLabel('动态任务控制').waitFor({ state: 'visible', timeout: 30_000 });
}

try {
  await login();

  await openSession(instantSessionId);
  const initialCard = await page.getByLabel('动态任务控制').innerText();
  await page.getByRole('button', { name: '运行中增加 Skill', exact: true }).click();
  await page.getByText('writing-for-agents', { exact: true }).waitFor();
  await page.screenshot({ path: `${evidenceDir}/01-runtime-skill-catalog.png`, fullPage: true });
  await page.getByRole('radio', { name: /writing-for-agents/ }).click();
  await page.getByRole('button', { name: '确认增加 Skill' }).click();
  await page.getByText(/Skill (等待安全边界加载|已固定修订并应用到新计划)/).waitFor({ timeout: 30_000 });

  let instantExecution;
  let instantCommand;
  for (let attempt = 0; attempt < 120; attempt += 1) {
    instantExecution = await api(`/api/executions/${instantExecutionId}?tenant_id=tenant_demo`);
    if (submittedCommandId) instantCommand = await api(`/api/executions/${instantExecutionId}/commands/${submittedCommandId}?tenant_id=tenant_demo`);
    if (instantExecution.plan_revision_number >= 2 && instantCommand?.status === 'applied') break;
    await page.waitForTimeout(500);
  }
  await page.reload();
  await page.getByText(/计划 v2/).waitFor({ timeout: 30_000 });
  const appliedCard = await page.getByLabel('动态任务控制').innerText();
  await page.screenshot({ path: `${evidenceDir}/02-runtime-skill-applied.png`, fullPage: true });

  await openSession(emptySessionId);
  const postsBeforeEmptyCatalog = addSkillPostCount;
  await page.getByRole('button', { name: '运行中增加 Skill', exact: true }).click();
  await page.getByText('当前会话没有可追加的 Skill').waitFor();
  const emptySubmitDisabled = await page.getByRole('button', { name: '确认增加 Skill' }).isDisabled();
  await page.screenshot({ path: `${evidenceDir}/03-runtime-skill-empty-negative.png`, fullPage: true });

  await openSession(parallelSessionId);
  const parallelCard = await page.getByLabel('动态任务控制').innerText();
  const parallelExecution = await api(`/api/executions/${parallelExecutionId}?tenant_id=tenant_demo`);
  await page.screenshot({ path: `${evidenceDir}/04-parallel-read-wave.png`, fullPage: true });

  const unrelatedResponse = await postApi(`/api/chat/sessions/${cancelSessionId}/cancel`, {
    tenant_id: 'tenant_demo', turn_id: cancelUnrelatedTurnId,
  });
  const afterUnrelated = await api(`/api/executions/${cancelExecutionId}?tenant_id=tenant_demo`);
  const exactResponse = await postApi(`/api/chat/sessions/${cancelSessionId}/cancel`, {
    tenant_id: 'tenant_demo', turn_id: cancelSourceTurnId,
  });
  const afterExact = await api(`/api/executions/${cancelExecutionId}?tenant_id=tenant_demo`);
  await page.goto(`${baseUrl}/workspace/chat/${cancelSessionId}`);
  await page.getByLabel('动态任务控制').waitFor({ state: 'visible', timeout: 30_000 });
  await page.screenshot({ path: `${evidenceDir}/07-dynamic-cancel-source-bridge.png`, fullPage: true });
  const dynamicCancel = {
    unrelatedStatus: unrelatedResponse.status,
    stateAfterUnrelated: afterUnrelated.status,
    exactStatus: exactResponse.status,
    stateAfterExact: afterExact.status,
  };

  const appliedUse = instantExecution?.skill_uses?.find(
    (use) => ['active', 'completed'].includes(use.status),
  );
  const answerStep = instantExecution?.steps?.find((step) => step.kind === 'answer');
  const parallelWave = parallelExecution.parallel_waves?.[0];
  const result = {
    instantSkill: {
      initialPlanVisible: initialCard.includes('计划 v1'),
      appliedPlanVisible: appliedCard.includes('计划 v2'),
      commandStatus: instantCommand?.status,
      planRevisionNumber: instantExecution?.plan_revision_number,
      planReason: instantExecution?.plan_reason,
      fixedUse: appliedUse && {
        revisionId: appliedUse.revision_id,
        checksumLength: appliedUse.content_checksum?.length,
        selectionMode: appliedUse.selection_mode,
      },
      answerReferencesUse: Boolean(
        appliedUse && answerStep?.guidance_skill_use_ids?.includes(appliedUse.id),
      ),
    },
    emptySkillNegative: {
      submitDisabled: emptySubmitDisabled,
      commandPosts: addSkillPostCount - postsBeforeEmptyCatalog,
    },
    parallelRead: {
      visibleTwoWayWave: parallelCard.includes('并行读取 · 2 路 · 已完成'),
      stableOrderVisible: parallelCard.includes('read_contract → read_partner'),
      apiParallelism: parallelExecution.parallel_waves?.[0]?.parallelism,
      apiWaveStatus: parallelExecution.parallel_waves?.[0]?.status,
      apiStableOrder: parallelExecution.parallel_waves?.[0]?.ordered_step_keys,
      itemCount: parallelWave?.item_count,
      settledItemCount: parallelWave?.settled_item_count,
      resultCount: parallelWave?.result_count,
      attemptCount: parallelWave?.attempt_count,
    },
    dynamicCancel,
    browserErrors,
    failedResponses,
  };
  console.log(JSON.stringify(result, null, 2));
  if (!result.instantSkill.initialPlanVisible || !result.instantSkill.appliedPlanVisible) process.exitCode = 2;
  if (result.instantSkill.commandStatus !== 'applied') process.exitCode = 3;
  if (result.instantSkill.planReason !== 'skill_added') process.exitCode = 3;
  if (result.instantSkill.fixedUse?.checksumLength !== 64 || !result.instantSkill.answerReferencesUse) process.exitCode = 3;
  if (!result.emptySkillNegative.submitDisabled || result.emptySkillNegative.commandPosts !== 0) process.exitCode = 4;
  if (!result.parallelRead.visibleTwoWayWave || !result.parallelRead.stableOrderVisible) process.exitCode = 5;
  if (result.parallelRead.apiParallelism !== 2 || result.parallelRead.apiWaveStatus !== 'succeeded') process.exitCode = 6;
  if (JSON.stringify(result.parallelRead.apiStableOrder) !== JSON.stringify(['read_contract', 'read_partner'])) process.exitCode = 6;
  if (result.parallelRead.itemCount !== 2 || result.parallelRead.settledItemCount !== 2) process.exitCode = 6;
  if (result.parallelRead.resultCount !== 2 || result.parallelRead.attemptCount !== 2) process.exitCode = 6;
  if (
    dynamicCancel.unrelatedStatus !== 200
      || !['created', 'running', 'waiting'].includes(dynamicCancel.stateAfterUnrelated)
      || dynamicCancel.exactStatus !== 200
      || dynamicCancel.stateAfterExact !== 'cancelled'
  ) process.exitCode = 7;
  if (browserErrors.length || failedResponses.length) process.exitCode = 8;
} catch (error) {
  console.error(JSON.stringify({
    url: page.url(),
    body: (await page.locator('body').innerText().catch(() => '')).slice(-2500),
    browserErrors,
    failedResponses,
  }, null, 2));
  throw error;
} finally {
  await context.close();
  await browser.close();
}

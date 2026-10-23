/**
 * @Time       : 2026/10/04
 * @Author     : zhanglp8181
 * @File       : staffdeck-final-f1-jrn03.fullstack.e2e.ts
 * @CallChain  : 两分支真实等待/Composer → 同库新PID → 原扫描/消费者/回执
 * @Description: 核午夜输入与UTC租约分离、同源记忆唯一恢复及原业务冻结身份。
 */
import { access, rm, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { expect, test, type Page, type TestInfo } from '@playwright/test';
import { criterion, facts, object, openCitation, request, send, type Row } from './final-f1-browser-helpers';
import { approveDynamic, pendingAttention, sendDynamicInput, startWaitingDynamic } from './final-f1-dta-journey';
import { startWaitingSop } from './final-f1-sop-journey';

const CONTROL = process.env.FULLSTACK_SD6B_CONTROL_DIR;
const FACT = 'F1 完整持久事实：办理后保存原始回执。';

test.setTimeout(300_000);

async function control(name: string, remove = false): Promise<void> {
  /** 只控制自有通知/领取/服务，不写数据库任务状态。 */
  if (!CONTROL) throw new Error('F1_OWNED_CONTROL_REQUIRED');
  if (remove) await rm(join(CONTROL, name), { force: true });
  else await writeFile(join(CONTROL, name), 'owned-jrn03-control');
}

async function clock(value: string): Promise<void> {
  /** 合成时钟只用于原输入TimeContext，数据库UTC、deadline和lease保持真实。 */
  if (!CONTROL) throw new Error('F1_OWNED_CONTROL_REQUIRED');
  await writeFile(join(CONTROL, 'clock.json'), JSON.stringify(value));
}

async function restart(page: Page, oldPid: number): Promise<number> {
  /** 真实supervisor停止旧子进程并复用同库，独立核旧PID消失和新PID健康。 */
  await control('restart.done', true);
  await control('restart.request');
  await expect.poll(async () => {
    try { await access(join(CONTROL || '', 'restart.done')); return true; } catch { return false; }
  }, { timeout: 30_000 }).toBe(true);
  await expect.poll(async () => {
    try { return (await page.request.get('/api/health')).status(); } catch { return 0; }
  }, { timeout: 90_000 }).toBe(200);
  const pid = (await facts(page)).pid;
  expect(pid).not.toBe(oldPid);
  let gone = false;
  try { process.kill(oldPid, 0); } catch { gone = true; }
  expect(gone).toBe(true);
  return pid;
}

async function completedTurn(page: Page, before: Row[], kind: 'SOP' | 'DTA'): Promise<{
  source: Row; assistant: Row; job: Row;
}> {
  /** 原source/assistant/complete/enqueued精确关联，禁止取第一条新job猜来源。 */
  let result: { source: Row; assistant: Row; job: Row } | undefined;
  await expect.poll(async () => {
    const state = await facts(page);
    const source = state.messages.find(row => row.role === 'user' && !before.some(old => old.id === row.id));
    if (!source) return false;
    const assistant = state.messages.find(row => row.role === 'assistant'
      && row.session_id === source.session_id && (object(row.metadata).user_message_id === source.id
        || object(row.metadata).turn_id === source.id));
    if (!assistant) return false;
    const job = state.jobs.find(row => row.source_input_id === source.id && row.assistant_message_id === assistant.id
      && row.agent_id === (kind === 'SOP' ? 'agent_f1_sop' : 'agent_f1_dta') && row.user_id === 'admin');
    const complete = state.events.find(row => row.event_type === 'complete' && row.user_message_id === source.id);
    const created = state.events.find(row => row.event_type === 'assistant_message_created'
      && row.user_message_id === source.id && row.assistant_message_id === assistant.id);
    const enqueued = state.events.find(row => row.event_type === 'async_job_enqueued'
      && row.source_message_id === source.id && row.assistant_message_id === assistant.id && row.job_id === job?.job_id);
    if (job && complete && created && enqueued) result = { source, assistant, job };
    return Boolean(result);
  }, { timeout: 60_000 }).toBe(true);
  if (!result) throw new Error('F1_EXACT_COMPLETION_JOB_MISSING');
  return result;
}

async function draft(page: Page, kind: 'SOP' | 'DTA', dynamic: boolean): Promise<void> {
  /** 真草稿入口和原引擎开关，无API写业务、认证或scope注入。 */
  await page.goto(`/workspace/chat/draft/${kind === 'SOP' ? 'agent_f1_sop' : 'agent_f1_dta'}`);
  const engine = page.getByRole('button', { name: '选择 DynamicTaskAgent 复杂任务引擎', exact: true });
  await expect(engine).toBeVisible();
  if ((await engine.getAttribute('aria-pressed') === 'true') !== dynamic) await engine.click();
}

async function memoryRecovery(page: Page, info: TestInfo, kind: 'SOP' | 'DTA', instance: Row,
  operation: Row): Promise<{ page: Page; pid: number; job: Row }> {
  /** 等待中另一个真实完成轮次提供稳定偏好，通知丢失后由新进程持久扫描结算。 */
  await control('pause');
  await control('drop-notification');
  const oldPid = (await facts(page)).pid;
  await draft(page, kind, false);
  const before = (await facts(page)).messages;
  await send(page, '请记住我的稳定服务偏好：' + FACT);
  const turn = await completedTurn(page, before, kind);
  expect(turn.job).toMatchObject({ status: 'pending', attempt_count: 0, model_dispatch_count: 0, applied_count: 0 });
  await page.reload();
  expect((await facts(page)).jobs.find(row => row.job_id === turn.job.job_id)?.status).toBe('pending');
  await criterion(info, `${kind}-lost-notification-pending`, { passed: true, source: turn.source,
    assistant: turn.assistant, job: turn.job, old_pid: oldPid, original_instance_id: instance.id });
  // 同一context保留正常登录和存储；关闭已结束聊天页面，真实断开客户端并释放旧DOM。
  const context = page.context();
  await page.close();
  page = await context.newPage();
  await page.goto('/');
  await clock('2026-10-03T16:01:00+00:00');
  const pid = await restart(page, oldPid);
  const held = await facts(page);
  expect(held.jobs.find(row => row.job_id === turn.job.job_id)).toMatchObject({ status: 'pending', model_dispatch_count: 0 });
  expect(held.operations.find(row => row.id === operation.id)).toMatchObject({ status: 'waiting_external',
    external_task_id: operation.external_task_id, request_hash: operation.request_hash,
    logical_action_id: operation.logical_action_id, external_submission_id: operation.external_submission_id });
  expect(object(object(held.instances.find(row => row.id === instance.id)).metadata).time_context)
    .toEqual(object(instance.metadata).time_context);
  await control('pause', true);
  // 通知保持丢失：只让原scanner领取，不补发唤醒。
  await expect.poll(async () => (await facts(page)).jobs.find(row => row.job_id === turn.job.job_id)?.status,
    { timeout: 60_000, intervals: [250, 500, 1000] }).toBe('completed');
  const settled = await facts(page);
  const job = object(settled.jobs.find(row => row.job_id === turn.job.job_id));
  expect(job).toMatchObject({ attempt_count: 1, model_dispatch_count: 1, applied_count: 1,
    source_input_id: turn.source.id, assistant_message_id: turn.assistant.id });
  for (const key of ['source_hash', 'source_sequence', 'user_generation', 'agent_generation',
    'user_permission_generation', 'agent_permission_generation', 'agent_capture_generation',
    'prompt_hash', 'model_contract_hash']) expect(job[key]).toBe(turn.job[key]);
  expect(job.fencing_token).toBe(Number(turn.job.fencing_token) + 1);
  const saved = settled.memories.filter(row => row.capture_job_id === job.job_id);
  expect(saved).toHaveLength(1);
  expect(saved[0]).toMatchObject({ captured_fact: true, source_hash: job.source_hash,
    source_sequence: job.source_sequence, agent_id: job.agent_id, user_id: 'admin' });
  const capture = settled.sdk.filter(row => row.phase === 'Memory Capture' && row.pid === pid
    && row.capture_current_user_content_hash === turn.source.content_hash
    && row.capture_current_assistant_content_hash === turn.assistant.content_hash);
  expect(capture).toHaveLength(1);
  expect(capture[0]?.actual_sdk_transport).toBe(true);
  expect(settled.events.filter(row => row.event_type === 'memory_saved' && row.job_id === job.job_id)).toHaveLength(1);
  await page.goto('/enterprise/memories');
  await page.getByLabel('切换当前员工', { exact: true }).click();
  await page.getByRole('menuitem', { name: new RegExp(`F1 ${kind}员工`) }).click();
  await expect.poll(() => page.evaluate(() => localStorage.getItem('gongge_enterprise_agent_scope')))
    .toBe(kind === 'SOP' ? 'agent_f1_sop' : 'agent_f1_dta');
  await page.goto('/enterprise/memories');
  await expect(page.getByRole('tab', { name: '记忆', exact: true })).toHaveAttribute('data-state', 'active');
  await page.getByRole('textbox', { name: '用户名', exact: true }).fill('admin');
  await page.getByRole('button', { name: '查询', exact: true }).click();
  await expect(page.getByRole('table', { name: '员工记忆' }).getByText(FACT, { exact: false }).first()).toBeVisible();
  await criterion(info, `${kind}-same-job-scanner-and-management`, { passed: true, old_pid: oldPid,
    new_pid: pid, job, source_input_id: turn.source.id, assistant_message_id: turn.assistant.id,
    management_complete: true, ledger: settled });
  return { page, pid, job };
}

async function consumeFact(page: Page, info: TestInfo, kind: 'SOP' | 'DTA'): Promise<void> {
  /** 午夜新输入核原Router实际选取完整事实；DTA另核原Planner/Action及最终答案。 */
  await draft(page, kind, kind === 'DTA');
  const before = (await facts(page)).messages;
  const text = 'F1事实消费：请解释我记住的稳定服务要求；只需回答，不提交新的配送。';
  if (kind === 'DTA') await sendDynamicInput(page, text); else await send(page, text);
  const turn = await completedTurn(page, before, kind);
  if (kind === 'DTA') expect(turn.assistant.captured_fact_in_answer).toBe(true);
  expect(object(turn.source.metadata).time_context).toMatchObject({ source_input_id: turn.source.id,
    anchor_at_utc: '2026-10-03T16:01:00Z', local_at: '2026-10-04T00:01:00+08:00', timezone: 'Asia/Shanghai' });
  const state = await facts(page);
  for (const phase of kind === 'SOP' ? ['Router'] : ['Dynamic Planner', 'Dynamic Action']) {
    const sdk = state.sdk.filter(row => row.phase === phase && row.source_input_id === turn.source.id);
    expect(sdk).toHaveLength(1);
    expect(sdk[0]).toMatchObject({ actual_sdk_transport: true, captured_memory_complete: true,
      memory_complete: [true, true, true] });
    if (kind === 'SOP') expect(sdk[0]?.captured_fact_selected).toBe(true);
  }
  if (kind === 'DTA') await expect(page.getByText(FACT, { exact: false }).last()).toBeVisible();
  await criterion(info, `${kind}-new-day-actual-fact-consumer`, { passed: true, source: turn.source,
    assistant: turn.assistant, ledger: state });
  await expect.poll(async () => (await facts(page)).jobs.find(row => row.job_id === turn.job.job_id)?.status,
    { timeout: 60_000 }).toBe('completed');
}

for (const kind of ['SOP', 'DTA'] as const) {
  test(`JRN03 ${kind}真实等待、跨午夜同库新PID及持久事实扫描`, async ({ page, browser }, info) => {
    let active = page;
    let completed = false;
    try {
      await clock('2026-10-03T15:59:00+00:00');
      await active.goto('/');
      const initial = await facts(active);
      const started = kind === 'SOP' ? await startWaitingSop(active, info) : await startWaitingDynamic(active, browser, info);
      active = started.page;
      const { instance, operation, remote } = started;
      const frozen = object(instance.metadata).time_context;
      expect(object(frozen)).toMatchObject({ anchor_at_utc: '2026-10-03T15:59:00Z', timezone: 'Asia/Shanghai' });
      expect(object(frozen).source_input_id).toBeTruthy();
      const recovered = await memoryRecovery(active, info, kind, instance, operation);
      active = recovered.page;
      await consumeFact(active, info, kind);
      await active.goto(`/workspace/chat/${String(instance.session_id)}`);
      await expect(active.getByPlaceholder('输入消息，按 Enter 发送...')).toBeVisible();
      await active.reload();
      const beforeCallback = await facts(active);
      expect(beforeCallback.provider.submits).toHaveLength(initial.provider.submits.length + 1);
      expect(beforeCallback.provider.submits.find(row => row.task_id === operation.external_task_id)?.post_count).toBe(1);
      expect(object(object(beforeCallback.instances.find(row => row.id === instance.id)).metadata).time_context).toEqual(frozen);
      expect((await active.request.get(`/__e2e/f1/provider/receipt/${String(operation.external_task_id)}`)).status()).toBe(404);
      expect(object(await request(active, `/__e2e/f1/provider/complete/${String(operation.external_task_id)}`, 'POST')).http_status).toBe(202);
      if (kind === 'DTA') {
        const approval = await pendingAttention(active, String(instance.id), 'tool_approval');
        active = await approveDynamic(active, browser, info, approval, [String(remote.receipt_id)]);
      }
      await expect.poll(async () => (await facts(active)).instances.find(row => row.id === instance.id)?.status,
        { timeout: 60_000 }).toBe('succeeded');
      const final = await facts(active);
      expect(final.pid).toBe(recovered.pid);
      expect(final.provider.submits).toHaveLength(initial.provider.submits.length + 1);
      expect(final.provider.finishes).toHaveLength(initial.provider.finishes.length + 1);
      expect(final.operations.find(row => row.id === operation.id)).toMatchObject({ status: 'succeeded',
        external_task_id: operation.external_task_id, request_hash: operation.request_hash,
        request_fingerprint: operation.request_fingerprint, external_submission_id: operation.external_submission_id });
      const current = object(final.instances.find(row => row.id === instance.id));
      expect(object(current.metadata).time_context).toEqual(frozen);
      for (const key of ['skill_version_id', 'definition_checksum', 'plan_revision_id', 'plan_checksum', 'capability_checksum']) {
        expect(current[key]).toBe(instance[key]);
      }
      const signals = final.signals.filter(row => row.execution_id === instance.id && row.signal_type === 'external_event');
      expect(signals).toHaveLength(1);
      expect(signals[0]).toMatchObject({ status: 'consumed' });
      expect(signals[0]?.consumed_at).toBeTruthy();
      const inbox = final.inbox.filter(row => row.operation_id === operation.id);
      const completion = inbox.filter(row => row.signal_id != null);
      expect(completion).toHaveLength(1);
      expect(completion[0]).toMatchObject({ status: 'applied', signal_id: signals[0]?.id,
        provider_event_id: `event-${String(operation.external_task_id)}` });
      expect(signals[0]?.causation_id).toBe(completion[0]?.id);
      for (const observation of inbox.filter(row => row.signal_id == null)) {
        expect(observation).toMatchObject({ status: 'applied', provider_task_id: operation.external_task_id });
        expect(String(observation.provider_event_id)).toMatch(/^poll:/);
      }
      expect(final.attempts.filter(row => row.operation_id === operation.id)).toHaveLength(1);
      expect(final.operations.find(row => row.id === operation.id)?.result).toEqual({ ...object(remote.request), receipt_id: remote.receipt_id });
      if (kind === 'DTA') expect(final.results.filter(row => row.execution_id === instance.id))
        .toMatchObject([{ status: 'verified', verification_passed: true }]);
      const knowledge = final.operations.filter(row => row.instance_id === instance.id
        && row.operation_name === 'knowledge.search');
      expect(knowledge).toHaveLength(1);
      expect(knowledge[0]?.status).toBe('succeeded');
      const sources = Array.isArray(knowledge[0]?.knowledge_sources)
        ? knowledge[0].knowledge_sources.map(object) : [];
      expect(sources.length).toBeGreaterThan(0);
      const citationMessages = final.messages.filter(row => row.session_id === instance.session_id
        && !initial.messages.some(old => old.id === row.id)
        && Array.isArray(row.citations) && row.citations.length > 0);
      expect(citationMessages.length).toBeGreaterThan(0);
      for (const message of citationMessages) {
        for (const citation of Array.isArray(message.citations) ? message.citations.map(object) : []) {
          expect(citation).toMatchObject({ tenant_valid: true, policy_complete: true });
          expect(citation.version_id).toBeTruthy();
          expect(sources.some(source => ['document_id', 'bucket_id', 'chunk_id', 'version_id', 'body_hash']
            .every(key => source[key] === citation[key]))).toBe(true);
        }
      }
      await active.reload();
      await openCitation(active, info, String(citationMessages[citationMessages.length - 1]?.id));
      const receipt = await active.context().newPage();
      await receipt.goto(`/__e2e/f1/provider/receipt/${String(operation.external_task_id)}`);
      await expect(receipt.locator('#receipt-id')).toHaveText(String(remote.receipt_id));
      for (const value of ['ALPHA91', 'CYGNUS77', '2026-10-04 09:00']) await expect(receipt.getByRole('table')).toContainText(value);
      await receipt.close();
      await active.reload();
      const refreshed = await facts(active);
      expect(refreshed.provider.submits).toHaveLength(initial.provider.submits.length + 1);
      expect(refreshed.jobs.find(row => row.job_id === recovered.job.job_id)).toMatchObject({
        attempt_count: 1, model_dispatch_count: 1, applied_count: 1 });
      expect(refreshed.memories.filter(row => row.capture_job_id === recovered.job.job_id)).toHaveLength(1);
      await criterion(info, `${kind}-original-consumer-late-complete`, { passed: true,
        original_execution_id: instance.id, operation_id: operation.id, provider_task_id: operation.external_task_id,
        source_input_id: object(frozen).source_input_id, signal_id: signals[0]?.id, ledger: refreshed });
      completed = true;
    } finally {
      await control('pause', true);
      await control('drop-notification', true);
      if (!active.isClosed()) {
        try { await criterion(info, `${kind}-terminal-observation`, { passed: completed, final: await facts(active) }); }
        catch (error) { await criterion(info, `${kind}-terminal-unavailable`, { passed: false,
          error_type: error instanceof Error ? error.name : 'unknown' }); }
      }
    }
  });
}

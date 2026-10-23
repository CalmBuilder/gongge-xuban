/**
 * @Time       : 2026/10/13
 * @Author     : zhanglp8181
 * @File       : staffdeck-final-f3-jrn08.fullstack.e2e.ts
 * @CallChain  : 真登录/员工/Composer/批准 → 真模型/原两Runtime → 独立模拟业务HTTP
 * @Description: 双库复用同一整程合同，真实模型质量与受控模拟效果分别验，不冒称真实供应商。
 */
import { expect, type Browser, type Page, type TestInfo } from '@playwright/test';
import { test } from './final-f3-browser-fixture';
import {
  closePageBeforeNextJourney, composerAttemptFailureObservation, type ComposerReceipt,
} from './final-f3-browser-driver';
import {
  approve, attention, attachCaseIdentityBinding, callback, cancel, cleanup, complete, criterion,
  effect, execution, facts, fault,
  archivePersistentFacts, frozen, hash, httpCount, importGuidance, initial, integer, login, object, one, open,
  assertSessionListRouteHealthy, isDtaStartResolved, isSafeDeclineTerminal, providerSubmitStatuses,
  reconcile, resolve, restart, rows, scope, send, submission,
  modelQualityCaseId, modelQualityKnowledgeCaseId, text, until, verifyModel, waitSdkQuiescent, withOwnerPageParked,
  type Branch, type FactRow, type Facts, type Initial, type Journey, type Scenario,
} from './final-f3-jrn08-helpers';

test.describe.configure({ mode: 'default' });
const diagnosticDtaNormal = process.env.F3_DIAGNOSTIC_DTA_NORMAL === '1';
test.setTimeout(diagnosticDtaNormal ? 360_000 : 900_000);

test.afterEach(async ({ page }, info) => {
  /** 同轮失败也保原持久映射，原已结算SDK门槛不能由关闭浏览器替代。 */
  try { await waitSdkQuiescent(page); }
  finally {
    try { await archivePersistentFacts(page, info); }
    finally { await closePageBeforeNextJourney(page); }
  }
});

async function sessionFor(page: Page, clientTurn: string, info?: TestInfo, caseId?: string): Promise<string> {
  /** 由原持久user消息的client_turn绑定实际session，草稿ID不能冒充已提升会话。 */
  const state = await until(page, undefined, value => value.messages.filter(row => row.role === 'user'
    && row.client_turn_id === clientTurn).length === 1);
  const source = one(state.messages, row => row.role === 'user' && row.client_turn_id === clientTurn);
  const session = text(source.session_id);
  if (info) await attachCaseIdentityBinding(info, 'composer-source', {
    session_id: session, source_input_id: text(source.id),
  }, clientTurn, caseId);
  return session;
}
function normalizeKnowledgeBody(value: string): string {
  /** 按正文修订合同统一Unicode、换行和边界空白后再核编辑器持久基线。 */
  return value.normalize('NFC').replace(/^\uFEFF/, '').replace(/\r\n?/g, '\n')
    .split('\n').map(line => line.replace(/\s+$/u, '')).join('\n').trim();
}
async function diagnosticDtaStage(info: TestInfo, kind: Branch, scenario: Scenario, stage: string): Promise<void> {
  /** 单例诊断只记录固定阶段标签，不携带自由正文、身份或持久业务载荷。 */
  if (diagnosticDtaNormal && kind === 'DTA' && scenario === 'normal') {
    await criterion(info, 'DTA-normal-diagnostic-stage', { stage });
  }
}
function identity(instance: FactRow, value: Initial, kind: Branch, session: string): void {
  /** 实例kind、租户、员工、发起人均来自原行，原两消费者不可互相替代。 */
  expect(instance.kind).toBe(kind === 'SOP' ? 'sop' : 'dynamic_task');
  expect(instance.tenant_id).toBe(value.owner.expected.tenant_id);
  expect(instance.agent_id).toBe(value.agents[kind].id); expect(instance.initiator_user_id).toBe(value.owner.expected.id);
  expect(instance.session_id).toBe(session);
  if (kind === 'SOP') { expect(text(instance.skill_version_id)).toBeTruthy(); expect(text(instance.definition_checksum)).toMatch(/^[a-f0-9]{64}$/); }
  else { expect(text(instance.current_plan_revision_id)).toBeTruthy(); expect(text(instance.plan_checksum)).toMatch(/^[a-f0-9]{64}$/); }
}
async function start(page: Page, browser: Browser, info: TestInfo, kind: Branch, scenario: Scenario): Promise<Journey> {
  /** 原页面启动、本人与第二批准人按原合同行动，确认前独立HTTP零提交。 */
  const value = await initial(page); await diagnosticDtaStage(info, kind, scenario, 'bootstrap-ready');
  await login(page, value.owner); await diagnosticDtaStage(info, kind, scenario, 'login-ready');
  if (kind === 'SOP' && scenario === 'normal') await verifyModel(page, value, info);
  await open(page, value, kind); await diagnosticDtaStage(info, kind, scenario, 'employee-opened');
  const httpStart = await httpCount(page);
  const item = value.cases[kind][scenario]; expect(item.expected_business.due_date).toBe(value.date_labels.expected_due_date);
  const receipt = await send(page, value, kind, item.message);
  await diagnosticDtaStage(info, kind, scenario, 'composer-response-settled');
  const qualityCaseId = scenario === 'normal' ? modelQualityCaseId(kind, 'normal') : undefined;
  const session = await sessionFor(page, receipt.client_turn_id, info, qualityCaseId);
  await diagnosticDtaStage(info, kind, scenario, 'source-session-read');
  if (kind === 'SOP') await expect(page).toHaveURL(new RegExp('/workspace/chat/' + session + '$'));
  const pending = await until(page, session, state => state.instances.some(row => row.session_id === session
    && (kind === 'DTA' ? isDtaStartResolved(row.status) : row.status === 'waiting'
      || ['succeeded', 'failed', 'cancelled', 'archived'].includes(text(row.status))))
    || state.events.some(row => row.session_id === session && row.event_type === 'error_occurred'));
  const matchingInstances = pending.instances.filter(row => row.session_id === session);
  const source = one(pending.messages, row => row.role === 'user' && row.client_turn_id === receipt.client_turn_id);
  if (!matchingInstances.length) {
    const errorEvent = pending.events.find(row => row.session_id === session && row.event_type === 'error_occurred');
    const safeCode = typeof errorEvent?.error_code === 'string'
      && /^[A-Za-z][A-Za-z0-9_]{1,127}$/u.test(errorEvent.error_code) ? errorEvent.error_code : null;
    const safeCategory = typeof errorEvent?.error_exception_category === 'string'
      && /^[a-z][a-z0-9_]{1,63}$/u.test(errorEvent.error_exception_category)
      ? errorEvent.error_exception_category : null;
    await criterion(info, kind + '-' + scenario + '-error-before-runtime-instance', {
      runtime_instance_count: 0, error_code: safeCode, error_exception_category: safeCategory,
    });
    throw new Error('F3_TERMINAL_ERROR_BEFORE_RUNTIME_INSTANCE');
  }
  let instance = execution(pending, session);
  await attachCaseIdentityBinding(info, 'runtime-instance', {
    session_id: session, source_input_id: text(source.id), instance_id: text(instance.id),
  }, receipt.client_turn_id, qualityCaseId);
  if (instance.status !== 'waiting') {
    const asyncOperationCount = pending.operations.filter(row => row.instance_id === instance.id
      && text(row.operation_name).split('@')[0] === 'f3_async_order').length;
    await criterion(info, kind + '-' + scenario + '-terminal-before-waiting', {
      terminal_status: text(instance.status), async_tool_frozen_for_plan: instance.expected_async_tool_frozen_for_plan === true,
      async_tool_contract_valid: instance.expected_async_tool_contract_valid === true,
      plan_step_count: instance.current_plan_step_count, async_operation_count: asyncOperationCount,
      runtime_failure_code: typeof instance.runtime_failure_code === 'string'
        ? instance.runtime_failure_code : null,
    });
  }
  expect(instance.status).toBe('waiting');
  await diagnosticDtaStage(info, kind, scenario, 'runtime-waiting-read');
  identity(instance, value, kind, session);
  const time = object(source.time_context);
  expect(time.source_input_id).toBe(source.id); expect(time.timezone).toBe(value.date_labels.timezone);
  expect(Number.isFinite(Date.parse(text(time.anchor_at_utc)))).toBe(true);
  expect(await httpCount(page)).toBe(httpStart); expect(await effect(page, item.expected_business.object_id)).toBeNull();
  await criterion(info, kind + '-' + scenario + '-before-original-confirmation', { passed: true,
    instance_id: instance.id, session_id: session, source_input_id: source.id,
    client_turn_id: receipt.client_turn_id, original_time_sha256: hash(time), confirmation_http_delta: 0 });
  if (kind === 'SOP') {
    const supplementation = '以下是本轮订单六项业务事实，尚未确认提交：'
      + JSON.stringify(item.expected_business);
    for (let round = 0; round < 2 && instance.current_node_id === 'collect'; round += 1) {
      let completed: ComposerReceipt;
      try {
        completed = await send(page, value, kind, supplementation);
      } catch (error: unknown) {
        if (process.env.F3_DIAGNOSTIC_COMPOSER_STATE === '1'
          && ['normal', 'cancel', 'safe_decline'].includes(scenario)) {
          await criterion(info, 'SOP-supplementation-send-failure-safe-observation', {
            diagnostic_only: true,
            supplementation_round: round + 1,
            composer_attempt: composerAttemptFailureObservation(page),
          });
        }
        throw error;
      }
      if (process.env.F3_DIAGNOSTIC_COMPOSER_STATE === '1'
        && ['normal', 'cancel', 'safe_decline'].includes(scenario)) {
        await criterion(info, 'SOP-supplementation-send-safe-observation', {
          diagnostic_only: true,
          supplementation_round: round + 1,
          composer_attempt: composerAttemptFailureObservation(page),
        });
      }
      expect(await sessionFor(page, completed.client_turn_id)).toBe(session);
      await expect(page).toHaveURL(new RegExp('/workspace/chat/' + session + '$'));
      instance = execution(await facts(page, session), session);
      expect(await httpCount(page)).toBe(httpStart);
    }
    if (process.env.F3_DIAGNOSTIC_COMPOSER_STATE === '1' && scenario === 'safe_decline') {
      await criterion(info, 'SOP-preconfirmation-state-safe-observation', {
        diagnostic_only: true,
        current_node: instance.current_node_id === 'collect' || instance.current_node_id === 'confirm'
          ? instance.current_node_id : 'other',
        complete_fields_ready: instance.current_node_id === 'confirm',
      });
    }
    expect(instance.current_node_id).toBe('confirm');
    await criterion(info, kind + '-' + scenario + '-complete-fields-before-confirmation', {
      passed: true, instance_id: instance.id, session_id: session,
      manual_completion_input_sha256: hash(supplementation), confirmation_http_delta: 0,
    });
    if (scenario === 'unknown') await fault(page);
    const confirmationBaseline = await facts(page, session);
    const providerSubmitCountBefore = await httpCount(page);
    let confirmed: ComposerReceipt;
    try {
      confirmed = await send(page, value, kind, item.confirmation_message);
    } catch (error: unknown) {
      let after: Facts | null = null;
      let factsReadbackComplete = false;
      let providerSubmitCountAfter: number | null = null;
      try {
        after = await facts(page, session);
        factsReadbackComplete = true;
      } catch {
        after = null;
      }
      try {
        providerSubmitCountAfter = await httpCount(page);
      } catch {
        providerSubmitCountAfter = null;
      }
      const observation = composerAttemptFailureObservation(page);
      const beforeUsers = confirmationBaseline.messages.filter(row => row.role === 'user').length;
      const afterUsers = after?.messages.filter(row => row.role === 'user').length ?? beforeUsers;
      const beforeCalls = confirmationBaseline.sdk_calls.filter(row => row.instance_id === instance.id);
      const afterCalls = after?.sdk_calls.filter(row => row.instance_id === instance.id) ?? beforeCalls;
      const operationName = (row: FactRow) => typeof row.operation_name === 'string'
        && row.operation_name.split('@')[0] === 'f3_async_order';
      const beforeOperations = confirmationBaseline.operations.filter(row => row.instance_id === instance.id
        && operationName(row));
      const afterOperations = after?.operations.filter(row => row.instance_id === instance.id
        && operationName(row)) ?? beforeOperations;
      const allowedStatuses = new Set([
        'pending', 'running', 'waiting', 'waiting_external', 'unknown', 'succeeded', 'failed',
        'cancelled', 'prepared', 'started', 'completed', 'in_flight',
      ]);
      const safeStatus = (value: unknown): string =>
        typeof value === 'string' && allowedStatuses.has(value) ? value : 'other';
      const statusCounts = (rows: FactRow[]) => ({
        completed: rows.filter(row => row.status === 'completed').length,
        failed: rows.filter(row => row.status === 'failed').length,
        in_flight: rows.filter(row => row.status === 'started' || row.status === 'in_flight').length,
      });
      const currentInstance = after?.instances.find(row => row.id === instance.id);
      try {
        await criterion(info, 'SOP-confirmation-send-failure-safe-observation', {
          diagnostic_only: true,
          facts_readback_complete: factsReadbackComplete,
          composer_attempt: observation,
          confirmation_turn_persisted: afterUsers > beforeUsers,
          new_user_message_count: Math.max(0, afterUsers - beforeUsers),
          sdk_call_count_before: beforeCalls.length,
          sdk_call_count_after: afterCalls.length,
          sdk_call_count_delta: Math.max(0, afterCalls.length - beforeCalls.length),
          sdk_status_counts: statusCounts(afterCalls),
          sdk_actual_transport_call_count: afterCalls.filter(row =>
            row.actual_sdk_transport === true && row.actual_provider_call === true).length,
          operation_count_before: beforeOperations.length,
          operation_count_after: afterOperations.length,
          operation_statuses: [...new Set(afterOperations.map(row => safeStatus(row.status)))].sort(),
          runtime_status: currentInstance ? safeStatus(currentInstance.status) : null,
          provider_submit_delta: providerSubmitCountAfter === null
            ? null : Math.max(0, providerSubmitCountAfter - providerSubmitCountBefore),
        });
      } catch {
        throw error;
      }
      throw error;
    }
    if (process.env.F3_DIAGNOSTIC_COMPOSER_STATE === '1' && kind === 'SOP'
      && (scenario === 'normal' || scenario === 'cancel' || scenario === 'safe_decline')) {
      const observation = composerAttemptFailureObservation(page);
      await criterion(info, 'SOP-confirmation-send-safe-observation', {
        passed: observation !== null && observation.phase === 'complete' && observation.dom !== null,
        diagnostic_only: true,
        composer_attempt: observation,
      });
    }
    expect(await sessionFor(page, confirmed.client_turn_id)).toBe(session);
  } else {
    const clarification = pending.attention.filter(row => row.execution_id === instance.id
      && row.kind === 'clarification' && ['offered', 'claimed'].includes(text(row.status)));
    await diagnosticDtaStage(info, kind, scenario, clarification.length ? 'clarification-offered' : 'no-clarification');
    if (clarification.length) {
      await resolve(page, session, one(clarification, () => true), 'answer', item.confirmation_message);
      await diagnosticDtaStage(info, kind, scenario, 'clarification-resolved');
    }
    await diagnosticDtaStage(info, kind, scenario, 'plan-and-approval-wait-started');
    const prepared = await until(page, session, state => state.operations.some(row => row.instance_id === instance.id
      && text(row.operation_name).split('@')[0] === 'f3_async_order' && row.status === 'prepared')
      && state.attention.some(row => row.execution_id === instance.id && row.kind === 'tool_approval'
        && ['offered', 'claimed'].includes(text(row.status))));
    await diagnosticDtaStage(info, kind, scenario, 'plan-and-approval-ready');
    const operation = submission(prepared, text(instance.id)); expect(operation.request_json).toEqual(item.expected_business);
    expect(await httpCount(page)).toBe(httpStart);
    if (scenario === 'unknown') await fault(page);
    await approve(page, browser, { kind, initial: value, scenario, case: item, session, receipt,
      instance, operation, http_start: httpStart }, info);
  }
  const submitted = await until(page, session, state => state.operations.some(row => row.instance_id === instance.id
    && text(row.operation_name).split('@')[0] === 'f3_async_order'
    && ['waiting_external', 'unknown', 'failed', 'succeeded'].includes(text(row.status))));
  instance = execution(submitted, session); const operation = submission(submitted, text(instance.id));
  expect(operation.request_json).toEqual(item.expected_business);
  expect(text(operation.request_fingerprint)).toMatch(/^[a-f0-9]{64}$/);
  expect(text(operation.remote_idempotency_key_hash)).toMatch(/^[a-f0-9]{64}$/);
  expect(await httpCount(page)).toBe(httpStart + 1);
  await criterion(info, kind + '-' + scenario + '-original-single-dispatch', { passed: true,
    instance_id: instance.id, operation_id: operation.id, operation_status: operation.status,
    request_fingerprint: operation.request_fingerprint, remote_key_sha256: operation.remote_idempotency_key_hash,
    business_label_sha256: hash(item.expected_business), actual_http_delta: 1 });
  return { kind, initial: value, scenario, case: item, session, receipt, instance, operation, http_start: httpStart };
}
function verifySdk(state: Facts, instanceId: string): void {
  /** 真SDK必须精确绑定持久实例与原source，不用模型自评或调用次数1作完成判断。 */
  const calls = state.sdk_calls.filter(row => row.instance_id === instanceId && row.status === 'completed');
  expect(calls.length).toBeGreaterThan(0);
  expect(calls.some(row => row.actual_sdk_transport === true && row.actual_provider_call === true)).toBe(true);
  expect(calls.some(row => typeof object(row.usage).prompt_tokens === 'number'
    && integer(object(row.usage).prompt_tokens) > 0)).toBe(true);
  for (const call of calls) {
    expect(call.actual_sdk_transport).toBe(true); expect(call.actual_provider_call).toBe(true);
    const source = one(state.messages, row => row.role === 'user' && row.id === call.source_input_id);
    expect(call.session_id).toBe(source.session_id); expect(call.turn_id).toBe(source.id);
    expect(text(call.request_sha256)).toMatch(/^[a-f0-9]{64}$/);
  }
}
async function terminal(page: Page, journey: Journey, status = 'succeeded'): Promise<Facts> {
  /** 原consumer实际落终态、Operation/Attempt与消息交叉绑定，页面不替代后台事实。 */
  const state = await until(page, journey.session, value => value.instances.some(row => row.id === journey.instance.id && row.status === status)
    && (status !== 'succeeded' || value.messages.some(row => row.role === 'assistant' && row.execution_id === journey.instance.id
      && (journey.kind === 'SOP' ? row.execution_status === 'succeeded'
        : row.result_id === execution(value, journey.session).current_result_id
          && row.result_status === 'verified' && row.result_binding_valid === true))));
  const instance = execution(state, journey.session); identity(instance, journey.initial, journey.kind, journey.session);
  verifySdk(state, text(instance.id));
  if (status === 'succeeded') {
    expect(submission(state, text(instance.id)).status).toBe('succeeded');
    expect(state.attempts.some(row => row.operation_id === journey.operation.id && row.status === 'succeeded')).toBe(true);
    const reply = one(state.messages, row => row.role === 'assistant' && row.execution_id === instance.id
      && (journey.kind === 'SOP' ? row.execution_status === 'succeeded'
        : row.result_id === instance.current_result_id && row.result_status === 'verified' && row.result_binding_valid === true));
    expect(text(reply.content_hash)).toMatch(/^[a-f0-9]{64}$/);
  }
  return state;
}
async function reconcileViaUi(page: Page, browser: Browser, journey: Journey, applied: boolean,
  evidence: Record<string, unknown>): Promise<void> {
  /** 独立证据沿原exception UI对账，DTA使用原规则排除发起人的第二管理员。 */
  const item = await attention(page, journey.session, text(journey.instance.id), 'exception');
  expect(object(item.payload).operation_id).toBe(journey.operation.id);
  const command = applied ? 'confirm_applied' : 'confirm_not_applied';
  if (journey.kind === 'SOP') {
    await resolve(page, journey.session, item, command, JSON.stringify(evidence));
    return;
  }
  expect(journey.initial.reviewer.expected.id).not.toBe(journey.initial.owner.expected.id);
  const baseURL = new URL(page.url()).origin;
  await withOwnerPageParked(page, async () => {
    const context = await browser.newContext({ baseURL });
    try {
      const reviewer = await context.newPage(); await login(reviewer, journey.initial.reviewer);
      await resolve(reviewer, journey.session, item, command, JSON.stringify(evidence));
    } finally { await context.close(); }
  });
}

for (const kind of ['SOP', 'DTA'] as const) {
  test(`JRN08 ${kind} 真模型→原确认→独立模拟accepted→真重启→原callback与业务读回清理`, async ({ page, caseBrowser }, info) => {
    const journey = await start(page, caseBrowser, info, kind, 'normal');
    expect(journey.operation.status).toBe('waiting_external'); expect(journey.instance.status).toBe('waiting');
    const taskId = text(journey.operation.external_task_id); const frozenBefore = frozen(journey.instance, journey.operation);
    expect(await effect(page, journey.case.expected_business.object_id)).toBeNull();
    await page.reload(); expect(await httpCount(page)).toBe(journey.http_start + 1);
    await restart(page, journey.session, info);
    const recovered = await facts(page, journey.session);
    expect(frozen(execution(recovered, journey.session), submission(recovered, text(journey.instance.id)))).toEqual(frozenBefore);
    expect(await httpCount(page)).toBe(journey.http_start + 1);
    await complete(page, taskId);
    const readback = await effect(page, journey.case.expected_business.object_id);
    if (!readback) throw new Error('F3_REMOTE_EFFECT_MISSING');
    expect(readback.business).toEqual(journey.case.expected_business);
    expect(readback.taskId).toBe(taskId); expect(readback.effect_count).toBe(1); expect(readback.request_count).toBe(1);
    const current = submission(await facts(page, journey.session), text(journey.instance.id));
    await callback(page, taskId, text(journey.operation.id), 'f3-complete-' + hash({
      operation_id: journey.operation.id, task_id: taskId,
    }), integer(current.external_observation_revision) + 1);
    const state = await terminal(page, journey);
    expect(state.inbox.some(row => row.operation_id === journey.operation.id && row.provider_task_id === taskId
      && row.provider_status === 'completed' && row.status === 'applied')).toBe(true);
    expect(state.signals.some(row => row.execution_id === journey.instance.id && row.status === 'consumed')).toBe(true);
    await page.reload(); await expect(page.getByText(readback.receipt_id, { exact: false }).last()).toBeVisible();
    const finalReply = one(state.messages, row => row.role === 'assistant' && row.execution_id === journey.instance.id
      && (kind === 'SOP' ? row.execution_status === 'succeeded' : row.result_id === execution(state, journey.session).current_result_id));
    expect(rows(state.operations.filter(row => row.id === journey.operation.id))).toHaveLength(1);
    expect(Array.isArray(finalReply.receipt_ids) && finalReply.receipt_ids.includes(readback.receipt_id)).toBe(true);
    expect(await httpCount(page)).toBe(journey.http_start + 1);
    await criterion(info, kind + '-normal-business-complete', { passed: true, instance_id: journey.instance.id,
      operation_id: journey.operation.id, task_id: taskId, receipt_id: readback.receipt_id,
      effect_count: readback.effect_count, request_count: readback.request_count,
      completion_transport: 'original_token_callback', anonymous_status_poll_qualified: false,
      final_message_id: finalReply.id, final_message_sha256: finalReply.content_hash,
      source_binding_valid: true,
      version_binding_valid: kind === 'SOP'
        ? typeof journey.instance.skill_version_id === 'string'
          && typeof journey.instance.definition_checksum === 'string'
        : typeof journey.instance.current_plan_revision_id === 'string'
          && typeof journey.instance.plan_checksum === 'string',
      citation_check_passed: true });
    await cleanup(page, journey.case.expected_business.object_id, info);
  });

  test(`JRN08 ${kind} 原页面取消→迟到独立效果→保unknown→真实证据人工对账`, async ({ page, caseBrowser }, info) => {
    const journey = await start(page, caseBrowser, info, kind, 'cancel'); expect(journey.operation.status).toBe('waiting_external');
    await cancel(page, journey, info); const held = await facts(page, journey.session);
    const taskId = text(journey.operation.external_task_id); await complete(page, taskId);
    const readback = await effect(page, journey.case.expected_business.object_id);
    if (!readback) throw new Error('F3_LATE_REMOTE_EFFECT_MISSING');
    expect(readback.business).toEqual(journey.case.expected_business); expect(readback.effect_count).toBe(1);
    const eventId = 'f3-late-' + hash({ operation_id: journey.operation.id, task_id: taskId });
    const observationRevision = integer(submission(held, text(journey.instance.id)).external_observation_revision) + 1;
    await callback(page, taskId, text(journey.operation.id), eventId, observationRevision);
    await callback(page, taskId, text(journey.operation.id), eventId, observationRevision);
    const late = await until(page, journey.session, value => value.inbox.some(row => row.operation_id === journey.operation.id
      && row.provider_event_id === eventId && row.status === 'ignored'));
    expect(late.inbox.filter(row => row.operation_id === journey.operation.id && row.provider_event_id === eventId)).toHaveLength(1);
    expect(late.signals.map(row => row.id).sort()).toEqual(held.signals.map(row => row.id).sort());
    await page.reload();
    expect(submission(late, text(journey.instance.id)).status).toBe('unknown');
    expect(execution(late, journey.session).status).toBe('waiting');
    expect(frozen(execution(late, journey.session), submission(late, text(journey.instance.id))))
      .toEqual(frozen(execution(held, journey.session), submission(held, text(journey.instance.id))));
    await reconcileViaUi(page, caseBrowser, journey, true, { task_id: taskId, receipt_id: readback.receipt_id,
      business: readback.business, independent_effect_count: readback.effect_count });
    const settled = await terminal(page, journey, 'cancelled');
    expect(submission(settled, text(journey.instance.id)).effect_state).toBe('complete');
    expect(await httpCount(page)).toBe(journey.http_start + 1);
    await criterion(info, kind + '-cancel-late-reconciled', { passed: true, instance_id: journey.instance.id,
      operation_id: journey.operation.id, actual_late_effect_count: 1, runtime_status: 'cancelled',
      resubmission_count: 0, receipt_id: readback.receipt_id });
    await cleanup(page, journey.case.expected_business.object_id, info);
    await assertSessionListRouteHealthy(info);
  });

  test(`JRN08 ${kind} 模拟安全拒绝真实422→独立零效果→原失败或人工对账`, async ({ page, caseBrowser }, info) => {
    const journey = await start(page, caseBrowser, info, kind, 'safe_decline');
    const submitStatuses = await providerSubmitStatuses(page);
    expect(submitStatuses).toHaveLength(journey.http_start + 1);
    expect(submitStatuses[journey.http_start]).toBe(422);
    expect(await effect(page, journey.case.expected_business.object_id)).toBeNull();
    const dispatched = await facts(page, journey.session);
    const dispatchedOperation = submission(dispatched, text(journey.instance.id));
    let state: Facts;
    let humanReconciled = false;
    if (dispatchedOperation.status === 'unknown') {
      expect(dispatchedOperation.effect_state).toBe('unknown');
      expect(execution(dispatched, journey.session).status).toBe('waiting');
      await reconcileViaUi(page, caseBrowser, journey, false, {
        provider_http_status: submitStatuses[journey.http_start],
        independently_observed_effect_count: 0,
        independently_observed_object_absent: true,
        resubmission_authorized: false,
      });
      state = await terminal(page, journey, 'failed');
      humanReconciled = true;
    } else {
      expect(dispatchedOperation.status).toBe('failed');
      state = await until(page, journey.session, value => isSafeDeclineTerminal(
        value, kind, text(journey.instance.id), text(journey.operation.id)));
    }
    const instance = execution(state, journey.session); identity(instance, journey.initial, kind, journey.session);
    const operation = submission(state, text(instance.id));
    expect(operation.id).toBe(journey.operation.id); expect(operation.status).toBe('failed');
    expect(operation.effect_state).toBe('none');
    expect(operation.error_code).toBe(humanReconciled ? 'MANUAL_RECONCILIATION' : 'HTTP_ERROR');
    if (humanReconciled) {
      const exception = one(state.attention, row => row.execution_id === instance.id && row.kind === 'exception');
      expect(exception.status).toBe('completed'); expect(operation.cancellation_disposition).toBe('reconciled');
    }
    const attempt = one(state.attempts, row => row.operation_id === operation.id);
    expect(attempt.status).toBe('failed'); verifySdk(state, text(instance.id));
    expect(await effect(page, journey.case.expected_business.object_id)).toBeNull();
    let assistantFailureNoticePresent = true;
    if (humanReconciled) {
      await page.goto('/workspace/chat/' + encodeURIComponent(journey.session));
      const control = page.getByLabel(kind === 'SOP' ? 'SOP执行结果' : '动态任务控制', { exact: true }).last();
      await expect(control).toContainText('失败');
      expect(state.messages.some(row => row.role === 'assistant' && row.execution_id === instance.id
        && row.execution_status === 'succeeded')).toBe(false);
      assistantFailureNoticePresent = false;
    } else {
      const finalMessage = one(state.messages, row => row.role === 'assistant'
        && row.execution_id === instance.id && row.execution_status === instance.status);
      expect(object(finalMessage.business_outcome)).toEqual({ failure_notice: true, success_claim: false });
      expect(rows(finalMessage.receipt_ids)).toHaveLength(0);
    }
    expect(await httpCount(page)).toBe(journey.http_start + 1);
    expect(await effect(page, journey.case.expected_business.object_id)).toBeNull();
    await criterion(info, kind + '-safe-decline-zero-effect', { passed: true, instance_id: journey.instance.id,
      operation_id: journey.operation.id, runtime_status: instance.status, operation_status: operation.status,
      attempt_status: attempt.status, provider_http_status: submitStatuses[journey.http_start],
      assistant_failure_notice_present: assistantFailureNoticePresent,
      terminal_failure_visible_in_original_control: true, success_claim: false,
      receipt_count: 0, actual_http_delta: 1, effect_count: 0,
      reconciled_from_unknown: humanReconciled, resubmission_count: 0,
      error_source: 'simulated_async_service', real_vendor_error: false });
    expect(await httpCount(page)).toBe(journey.http_start + 1);
  });

  test(`JRN08 ${kind} 受理丢回执→unknown先独立对账→原恢复不重发`, async ({ page, caseBrowser }, info) => {
    const journey = await start(page, caseBrowser, info, kind, 'unknown'); expect(journey.operation.status).toBe('unknown');
    const original = await reconcile(page, text(journey.operation.remote_idempotency_key_hash));
    expect(original.request_count).toBe(1); expect(original.effect_count).toBe(0);
    expect(await httpCount(page)).toBe(journey.http_start + 1);
    const beforeRestart = execution(await facts(page, journey.session), journey.session);
    expect(beforeRestart.status).toBe('waiting');
    await restart(page, journey.session, info);
    const restoredFacts = await facts(page, journey.session);
    const restoredInstance = execution(restoredFacts, journey.session);
    expect(restoredInstance.status).toBe('waiting');
    const restored = submission(restoredFacts, text(journey.instance.id));
    expect(restored.status).toBe('unknown'); expect(restored.remote_idempotency_key_hash).toBe(journey.operation.remote_idempotency_key_hash);
    await complete(page, original.taskId); const readback = await effect(page, journey.case.expected_business.object_id);
    if (!readback) throw new Error('F3_UNKNOWN_REMOTE_EFFECT_MISSING');
    expect(readback.business).toEqual(journey.case.expected_business); expect(readback.request_count).toBe(1);
    await reconcileViaUi(page, caseBrowser, journey, true, { provider_task_id: original.taskId, receipt_id: readback.receipt_id,
      business: readback.business, independent_effect_count: readback.effect_count });
    const final = await terminal(page, journey); expect(submission(final, text(journey.instance.id)).effect_state).toBe('complete');
    expect(await httpCount(page)).toBe(journey.http_start + 1);
    await criterion(info, kind + '-unknown-independent-reconciliation', { passed: true,
      instance_id: journey.instance.id, operation_id: journey.operation.id, provider_task_id: original.taskId,
      actual_http_delta: 1, actual_effect_count: 1, resubmissions: 0, fault_source: 'controlled_connection_drop' });
    await cleanup(page, journey.case.expected_business.object_id, info);
  });
}

async function knowledgeAnswer(page: Page, info: TestInfo, session: string, version: string,
  label: 'old' | 'new' | 'none', initialValue: Initial): Promise<FactRow> {
  /** 原DTA知识Operation、真实SDK实际输入、精确引用与人工事实独立一致。 */
  const state = await until(page, session, value => value.instances.some(row => row.session_id === session && row.status === 'succeeded'));
  const instance = execution(state, session); identity(instance, initialValue, 'DTA', session);
  const qualityCaseId = modelQualityKnowledgeCaseId('DTA', label);
  const source = one(state.messages, row => row.role === 'user' && row.session_id === session);
  await attachCaseIdentityBinding(info, 'runtime-instance', {
    session_id: session, source_input_id: text(source.id), instance_id: text(instance.id),
  }, undefined, qualityCaseId);
  expect(instance.frozen_knowledge_versions).toContain(version); verifySdk(state, text(instance.id));
  const operations = state.operations.filter(row => row.instance_id === instance.id && row.operation_name === 'knowledge.search');
  expect(operations.length).toBeGreaterThan(0); expect(operations.every(row => row.status === 'succeeded')).toBe(true);
  const retrieved = operations.flatMap(row => rows(row.retrieval)); expect(retrieved.length).toBeGreaterThan(0);
  expect(retrieved.every(row => Array.isArray(row.version_ids)
    && row.version_ids.length === 1 && row.version_ids[0] === version
    && (label === 'none' || row.identity_valid === true))).toBe(true);
  const answer = one(state.messages, row => row.role === 'assistant' && row.execution_id === instance.id
    && row.result_id === instance.current_result_id && row.result_status === 'verified' && row.result_binding_valid === true);
  const labels = object(answer.facts); const citations = rows(answer.citations);
  const actualInputs = state.sdk_calls.filter(row => row.instance_id === instance.id && row.operation === 'dynamic_task.answer'
    && row.status === 'completed' && row.actual_provider_call === true && row.actual_sdk_transport === true);
  expect(actualInputs.length).toBeGreaterThan(0);
  expect(actualInputs.some(row => row.has_knowledge_result === true && row.knowledge_version_ids !== undefined
    && Array.isArray(row.knowledge_version_ids) && row.knowledge_version_ids.includes(version))).toBe(true);
  if (label === 'none') {
    expect(labels.explicit_no_answer).toBe(true); expect(labels.policy_number_present).toBe(false);
    expect(labels.old_amount || labels.new_amount).toBe(false); expect(citations).toHaveLength(0);
    expect(retrieved.every(row => ['no_match', 'insufficient'].includes(text(row.outcome)))).toBe(true);
    expect(retrieved.every(row => row.evidence_required === true && row.evidence_satisfied === false)).toBe(true);
    await expect(page.getByLabel('知识引用')).toHaveCount(0);
  } else {
    expect(retrieved.every(row => row.evidence_required === true && row.evidence_satisfied === true)).toBe(true);
    expect(labels[label + '_amount']).toBe(true); expect(labels[label + '_deadline']).toBe(true);
    expect(labels[(label === 'old' ? 'new' : 'old') + '_amount']).toBe(false);
    expect(retrieved.some(row => { const evidence = object(row.evidence_facts);
      return evidence[label + '_amount'] === true && evidence[label + '_deadline'] === true; })).toBe(true);
    expect(citations.length).toBeGreaterThan(0);
    expect(citations.every(row => row.source_valid === true && Array.isArray(row.version_ids) && row.version_ids.includes(version))).toBe(true);
    await page.goto('/workspace/chat/' + session);
    const buttons = page.getByLabel('知识引用').last().getByRole('button'); await expect(buttons.first()).toBeVisible();
    let excerpts = '';
    for (let index = 0; index < await buttons.count(); index += 1) {
      await buttons.nth(index).click(); const dialog = page.getByRole('dialog', { name: '引用详情' });
      await expect(dialog).toBeVisible(); excerpts += await dialog.innerText();
      await dialog.getByRole('button', { name: 'Close', exact: true }).click(); await expect(dialog).not.toBeVisible();
    }
    expect(excerpts).toContain(String(initialValue.knowledge[label + '_amount']));
    expect(excerpts).toContain(text(initialValue.knowledge[label + '_deadline']));
  }
  await criterion(info, 'dta-knowledge-real-quality-' + label, { passed: true, instance_id: instance.id,
    session_id: session, version_id: version, operation_ids: operations.map(row => row.id),
    answer_message_id: answer.id, answer_sha256: answer.content_hash, evidence_sha256: hash(retrieved),
    exact_citation_count: citations.length, actual_model_answer_calls: actualInputs.length,
    source_binding_valid: true,
    version_binding_valid: Array.isArray(instance.frozen_knowledge_versions)
      && instance.frozen_knowledge_versions.includes(version),
    citation_check_passed: label === 'none'
      ? citations.length === 0
      : citations.length > 0 && citations.every(row => row.source_valid === true) });
  return instance;
}

async function sopKnowledgeAnswer(page: Page, info: TestInfo, session: string, version: string,
  label: 'old' | 'new' | 'none', initialValue: Initial): Promise<FactRow> {
  /** 原SOP知识Operation、真实SDK输入、版本引用与人工无答案标签独立一致。 */
  const state = await until(page, session, value => value.instances.some(row => row.session_id === session
    && row.status === 'succeeded'));
  const instance = execution(state, session); identity(instance, initialValue, 'SOP', session);
  const qualityCaseId = modelQualityKnowledgeCaseId('SOP', label);
  const source = one(state.messages, row => row.role === 'user' && row.session_id === session);
  await attachCaseIdentityBinding(info, 'runtime-instance', {
    session_id: session, source_input_id: text(source.id), instance_id: text(instance.id),
    skill_version_id: text(instance.skill_version_id),
  }, undefined, qualityCaseId);
  await criterion(info, 'sop-knowledge-instance-observed-' + label, { diagnostic_only: true,
    frozen_version_count: Array.isArray(instance.frozen_knowledge_versions)
      ? instance.frozen_knowledge_versions.length : -1,
    frozen_version_has_expected: Array.isArray(instance.frozen_knowledge_versions)
      && instance.frozen_knowledge_versions.includes(version),
    operation_count: state.operations.filter(row => row.instance_id === instance.id).length,
    knowledge_operation_count: state.operations.filter(row => row.instance_id === instance.id
      && row.operation_name === 'knowledge.search').length,
    instance_status: text(instance.status),
  });
  verifySdk(state, text(instance.id));
  const operations = state.operations.filter(row => row.instance_id === instance.id
    && row.operation_name === 'knowledge.search');
  await criterion(info, 'sop-knowledge-operation-observed-' + label, { diagnostic_only: true,
    operation_count: operations.length,
    succeeded_count: operations.filter(row => row.status === 'succeeded').length,
    operation_statuses: operations.map(row => text(row.status)),
    result_outcomes: operations.map(row => row.knowledge_result_outcome),
    result_version_id_counts: operations.map(row => row.knowledge_result_version_id_count),
    result_evidence_required: operations.map(row => row.knowledge_result_evidence_required),
    result_evidence_satisfied: operations.map(row => row.knowledge_result_evidence_satisfied),
  });
  expect(operations.length).toBeGreaterThan(0);
  expect(operations.every(row => row.status === 'succeeded')).toBe(true);
  const retrieved = operations.flatMap(row => rows(row.retrieval));
  await criterion(info, 'sop-knowledge-retrieval-observed-' + label, { diagnostic_only: true,
    retrieval_count: retrieved.length,
    requested_version_match_count: retrieved.filter(row => Array.isArray(row.version_ids)
      && row.version_ids.length === 1 && row.version_ids[0] === version).length,
    outcomes: retrieved.map(row => text(row.outcome)),
    evidence_required_count: retrieved.filter(row => row.evidence_required === true).length,
    evidence_satisfied_count: retrieved.filter(row => row.evidence_satisfied === true).length,
  });
  expect(retrieved.length).toBeGreaterThan(0);
  expect(retrieved.every(row => Array.isArray(row.version_ids)
    && row.version_ids.length === 1 && row.version_ids[0] === version
    && (label === 'none' || row.identity_valid === true))).toBe(true);
  const answers = state.messages.filter(row => row.role === 'assistant'
    && row.execution_id === instance.id && row.execution_status === 'succeeded');
  if (!answers.length) throw new Error('F3_SOP_KNOWLEDGE_ASSISTANT_MISSING');
  const answer = answers[answers.length - 1];
  const labels = object(answer.facts); const citations = rows(answer.citations);
  const actualInputs = state.sdk_calls.filter(row => row.instance_id === instance.id
    && row.status === 'completed' && row.actual_provider_call === true
    && row.actual_sdk_transport === true);
  await criterion(info, 'sop-knowledge-observed-' + label, { diagnostic_only: true,
    old_amount: labels.old_amount === true, new_amount: labels.new_amount === true,
    old_deadline: labels.old_deadline === true, new_deadline: labels.new_deadline === true,
    explicit_no_answer: labels.explicit_no_answer === true,
    policy_number_present: labels.policy_number_present === true,
    citation_count: citations.length, retrieval_count: retrieved.length,
    citation_source_valid_count: citations.filter(row => row.source_valid === true).length,
    actual_model_answer_calls: actualInputs.length,
    retrieval_outcomes: retrieved.map(row => text(row.outcome)),
  });
  expect(actualInputs.length).toBeGreaterThan(0);
  if (label === 'none') {
    expect(labels.explicit_no_answer).toBe(true); expect(labels.policy_number_present).toBe(false);
    expect(labels.old_amount || labels.new_amount).toBe(false); expect(citations).toHaveLength(0);
    expect(retrieved.every(row => ['no_match', 'insufficient'].includes(text(row.outcome)))).toBe(true);
    expect(retrieved.every(row => row.evidence_required === true && row.evidence_satisfied === false)).toBe(true);
    await expect(page.getByLabel('知识引用')).toHaveCount(0);
  } else {
    expect(retrieved.every(row => row.evidence_required === true && row.evidence_satisfied === true)).toBe(true);
    expect(labels[label + '_amount']).toBe(true); expect(labels[label + '_deadline']).toBe(true);
    expect(labels[(label === 'old' ? 'new' : 'old') + '_amount']).toBe(false);
    expect(citations.length).toBeGreaterThan(0);
    expect(citations.every(row => row.source_valid === true && Array.isArray(row.version_ids)
      && row.version_ids.includes(version))).toBe(true);
    await page.goto('/workspace/chat/' + session);
    const buttons = page.getByLabel('知识引用').last().getByRole('button');
    await expect(buttons.first()).toBeVisible();
    let excerpts = '';
    for (let index = 0; index < await buttons.count(); index += 1) {
      await buttons.nth(index).click();
      const dialog = page.getByRole('dialog', { name: '引用详情' });
      await expect(dialog).toBeVisible(); excerpts += await dialog.innerText();
      await dialog.getByRole('button', { name: 'Close', exact: true }).click();
      await expect(dialog).not.toBeVisible();
    }
    expect(excerpts).toContain(String(initialValue.knowledge[label + '_amount']));
    expect(excerpts).toContain(text(initialValue.knowledge[label + '_deadline']));
  }
  await criterion(info, 'sop-knowledge-real-quality-' + label, { passed: true,
    instance_id: instance.id, session_id: session, version_id: version,
    operation_ids: operations.map(row => row.id), answer_message_id: answer.id,
    answer_sha256: answer.content_hash, evidence_sha256: hash(retrieved),
    exact_citation_count: citations.length, actual_model_answer_calls: actualInputs.length,
    source_binding_valid: true,
    version_binding_valid: retrieved.length > 0 && retrieved.every(row => Array.isArray(row.version_ids)
      && row.version_ids.length === 1 && row.version_ids[0] === version),
    citation_check_passed: label === 'none'
      ? citations.length === 0
      : citations.length > 0 && citations.every(row => row.source_valid === true) });
  return instance;
}

if (process.env.F3_LIVE_DIAGNOSTIC_SELECTION === 'h2-dta-editor-baseline') test('JRN08 H2 DTA editor persisted baseline read-only diagnostic', async ({ page }, info) => {
  const value = await initial(page); const httpBefore = await httpCount(page); await login(page, value.owner);
  await scope(page, value.agents.DTA); await page.goto('/enterprise/knowledge');
  const platformGovernance = page.getByRole('button', { name: '平台知识治理', exact: true });
  await expect(platformGovernance).toBeVisible(); await platformGovernance.click();
  await expect(page.getByText('知识库广场列表', { exact: true })).toBeVisible();
  const expectedDocumentId = text(value.knowledge.old_document_id);
  const expectedVersionId = text(value.knowledge.old_version_id);
  await page.getByText(text(value.knowledge.kb_name), { exact: true }).last().click();
  await expect(page.getByText(text(value.knowledge.title), { exact: true }).last()).toBeVisible();
  const contentResponsePromise = page.waitForResponse(response => {
    const url = new URL(response.url());
    return response.request().method() === 'GET'
      && url.pathname.startsWith('/api/enterprise/knowledge/documents/')
      && url.pathname.endsWith('/content');
  });
  await page.getByRole('button', { name: '详情', exact: true }).last().click();
  await page.getByRole('dialog', { name: '文档详情' })
    .getByRole('button', { name: '修改', exact: true }).click();
  const contentResponse = await contentResponsePromise;
  const contentUrl = new URL(contentResponse.url());
  const requestDocumentMatch = contentUrl.pathname.match(/\/documents\/([^/]+)\/content$/u);
  const requestedAgentId = contentUrl.searchParams.get('agent_id');
  const responseValue: unknown = await contentResponse.json().catch(() => ({}));
  const responseBody = typeof responseValue === 'object' && responseValue !== null && !Array.isArray(responseValue)
    ? object(responseValue) : {};
  const contentMd = typeof responseBody.content_md === 'string' ? responseBody.content_md : null;
  const editor = page.getByRole('textbox', { name: '文档正文' });
  await expect(editor).toBeVisible();
  const editorValue = await editor.inputValue();
  const currentFacts = await facts(page);
  const databaseBaseline = currentFacts.editor_baseline;
  const fixtureBody = text(value.knowledge.old_content_md);
  const expectedBody = normalizeKnowledgeBody(fixtureBody);
  const fixtureBodyHash = hash(fixtureBody);
  const expectedBodyHash = hash(expectedBody);
  const responseBodyHash = contentMd === null ? null : hash(contentMd);
  const responseCanonicalBodyHash = contentMd === null ? null : hash(normalizeKnowledgeBody(contentMd));
  const editorBodyHash = hash(editorValue);
  const editorCanonicalBodyHash = hash(normalizeKnowledgeBody(editorValue));
  const httpAfter = await httpCount(page);
  expect(currentFacts.sdk_calls).toHaveLength(0);
  expect(currentFacts.instances).toHaveLength(0);
  expect(currentFacts.operations).toHaveLength(0);
  expect(httpAfter).toBe(httpBefore);
  const responseScope = responseBody.scope === 'overall' ? 'overall'
    : typeof responseBody.scope === 'string' && responseBody.scope.startsWith('agent:') ? 'agent' : null;
  const requestTenantMatchesFixture = contentUrl.searchParams.get('tenant_id') === value.agents.DTA.tenant_id;
  const requestAgentMatchesFixture = requestedAgentId === value.agents.DTA.id;
  const expectedDatabaseHead = requestedAgentId === null ? databaseBaseline.database_head_version_id
    : requestAgentMatchesFixture ? databaseBaseline.database_agent_head_version_id : undefined;
  const expectedDatabaseRevision = requestedAgentId === null ? databaseBaseline.database_content_revision
    : requestAgentMatchesFixture ? databaseBaseline.database_agent_content_revision : undefined;
  const expectedResponseScope = requestedAgentId === null ? 'overall'
    : requestAgentMatchesFixture ? 'agent' : null;
  const requestUsesDtaBranch = requestedAgentId !== null && requestAgentMatchesFixture;
  const requestScopeDatabaseHead = typeof expectedDatabaseHead === 'string'
    ? expectedDatabaseHead : null;
  const requestScopeContentRevision = Number.isSafeInteger(expectedDatabaseRevision)
    ? expectedDatabaseRevision : null;
  await criterion(info, 'h2-editor-baseline-observation-captured', {
    passed: true,
    observation_only: true,
    comparison_results_are_diagnostic: true,
    request_document_id_matches_fixture: requestDocumentMatch?.[1] === expectedDocumentId,
    response_document_id_matches_fixture: responseBody.document_id === expectedDocumentId,
    request_tenant_matches_fixture: requestTenantMatchesFixture,
    request_agent_scope_present: requestedAgentId !== null,
    request_agent_matches_fixture: requestedAgentId === null || requestAgentMatchesFixture,
    response_scope: responseScope,
    response_scope_matches_requested_scope: expectedResponseScope === null ? null : responseScope === expectedResponseScope,
    response_head_version_matches_fixture: responseBody.head_version_id === expectedVersionId,
    response_document_version_matches_fixture: responseBody.knowledge_base_version_id === expectedVersionId,
    response_head_version_matches_database_request_head: typeof expectedDatabaseHead === 'string'
      ? responseBody.head_version_id === expectedDatabaseHead : null,
    response_content_revision: Number.isSafeInteger(responseBody.content_revision)
      ? responseBody.content_revision : null,
    response_content_revision_matches_database_request_head: Number.isSafeInteger(expectedDatabaseRevision)
      ? responseBody.content_revision === expectedDatabaseRevision : null,
    response_source_checksum_valid: typeof responseBody.source_checksum === 'string'
      && /^[a-f0-9]{64}$/u.test(responseBody.source_checksum),
    response_source_checksum_matches_database: responseBody.source_checksum === databaseBaseline.source_checksum,
    response_status: contentResponse.status(),
    database_document_present: databaseBaseline.document_present === true,
    database_document_id_matches_fixture: databaseBaseline.document_id_matches_fixture === true,
    database_document_tenant_matches_fixture: databaseBaseline.document_tenant_matches_fixture === true,
    database_document_body_identity_valid: databaseBaseline.document_body_identity_valid === true,
    database_knowledge_base_tenant_matches_fixture: databaseBaseline.knowledge_base_tenant_matches_fixture === true,
    database_document_knowledge_base_matches_fixture: databaseBaseline.document_knowledge_base_matches_fixture === true,
    database_version_tenant_matches_fixture: databaseBaseline.version_tenant_matches_fixture === true,
    database_version_knowledge_base_matches_fixture: databaseBaseline.version_knowledge_base_matches_fixture === true,
    database_document_version_matches_fixture: databaseBaseline.document_version_matches_fixture === true,
    database_version_scope: databaseBaseline.version_scope,
    database_version_publication_state: databaseBaseline.version_publication_state,
    database_current_version_id: databaseBaseline.database_current_version_id,
    database_head_version_id: databaseBaseline.database_head_version_id,
    database_head_version: databaseBaseline.database_head_version,
    database_content_revision: databaseBaseline.database_content_revision,
    request_database_scope: expectedResponseScope,
    request_scope_uses_dta_agent_branch: requestUsesDtaBranch,
    request_scope_database_head_version_id: requestScopeDatabaseHead,
    request_scope_database_content_revision: requestScopeContentRevision,
    request_scope_database_head_matches_fixture_document: requestScopeDatabaseHead === expectedVersionId,
    request_scope_database_revision_is_present: requestScopeContentRevision !== null,
    database_source_kind: databaseBaseline.document_source_kind,
    database_source_matches_fixture: databaseBaseline.normalized_source_matches_fixture === true,
    database_source_exactly_matches_fixture: databaseBaseline.normalized_source_exact_matches_fixture === true,
    database_source_sha256: databaseBaseline.normalized_source_sha256,
    database_source_json_sha256: databaseBaseline.normalized_source_json_sha256,
    database_source_length: databaseBaseline.normalized_source_length,
    database_canonical_source_json_sha256: databaseBaseline.canonical_source_json_sha256,
    database_canonical_source_length: databaseBaseline.canonical_source_length,
    database_agent_branch_present: databaseBaseline.database_agent_branch_present === true,
    database_agent_branch_agent_matches_fixture: databaseBaseline.database_agent_branch_agent_matches_fixture === true,
    database_agent_head_version_id: databaseBaseline.database_agent_head_version_id,
    database_agent_head_version: databaseBaseline.database_agent_head_version,
    database_agent_head_scope: databaseBaseline.database_agent_head_scope,
    database_agent_content_revision: databaseBaseline.database_agent_content_revision,
    database_agent_head_matches_branch: databaseBaseline.database_agent_head_matches_branch === true,
    database_agent_head_scope_matches_agent: databaseBaseline.database_agent_head_scope_matches_agent === true,
    database_agent_head_scope_inherits_overall: databaseBaseline.database_agent_head_scope_inherits_overall === true,
    fixture_body_sha256: expectedBodyHash,
    fixture_body_length: expectedBody.length,
    fixture_raw_body_sha256: fixtureBodyHash,
    fixture_raw_body_length: fixtureBody.length,
    response_body_sha256: responseBodyHash,
    response_body_length: contentMd?.length ?? null,
    response_canonical_body_sha256: responseCanonicalBodyHash,
    response_canonical_body_length: contentMd === null ? null : normalizeKnowledgeBody(contentMd).length,
    editor_body_sha256: editorBodyHash,
    editor_body_length: editorValue.length,
    editor_canonical_body_sha256: editorCanonicalBodyHash,
    editor_canonical_body_length: normalizeKnowledgeBody(editorValue).length,
    response_body_matches_database_exactly: responseBodyHash === databaseBaseline.normalized_source_json_sha256,
    response_canonical_body_matches_database: responseCanonicalBodyHash
      === databaseBaseline.canonical_source_json_sha256,
    editor_body_matches_response: editorBodyHash === responseBodyHash,
    editor_canonical_body_matches_response: editorCanonicalBodyHash === responseCanonicalBodyHash,
    editor_body_matches_fixture: editorBodyHash === expectedBodyHash,
    editor_canonical_body_matches_fixture: editorCanonicalBodyHash === expectedBodyHash,
    sdk_call_count: currentFacts.sdk_calls.length,
    execution_count: currentFacts.instances.length,
    operation_count: currentFacts.operations.length,
    simulated_business_http_delta: httpAfter - httpBefore,
    isolated_fixture_initialization_write_expected: true,
    browser_auth_and_scope_state_ephemeral: true,
    real_vendor_qualified: false,
  });
});

test('JRN08 H2后DTA真实旧知识冻结→页面正文发布→真重启恢复旧版→新/未知质量', async ({ page }, info) => {
  const value = await initial(page); await login(page, value.owner);
  await importGuidance(page, value, info); await open(page, value, 'DTA');
  const startingHttp = await httpCount(page); const oldVersion = text(value.knowledge.old_version_id);
  const first = await send(page, value, 'DTA', '使用' + text(value.knowledge.guidance_slug) + '。'
    + text(value.knowledge.old_query) + '在检索前先询问我并等待确认；未收到我确认不得检索或回答。');
  const oldSession = await sessionFor(page, first.client_turn_id, info,
    modelQualityCaseId('DTA', 'knowledge_old'));
  const waiting = await until(page, oldSession, state => state.instances.some(row => row.session_id === oldSession && row.status === 'waiting'));
  const original = execution(waiting, oldSession); identity(original, value, 'DTA', oldSession);
  expect(original.frozen_knowledge_versions).toEqual([oldVersion]);
  expect(waiting.operations.filter(row => row.instance_id === original.id && row.operation_name === 'knowledge.search')).toHaveLength(0);
  const frozenOriginal = frozen(original);
  const oldVersionRecord = one(rows(object(waiting.knowledge).versions), row => row.id === oldVersion);
  const originalDtaBranch = one(rows(object(waiting.knowledge).branches), row => row.agent_id === value.agents.DTA.id
    && row.knowledge_base_id === value.knowledge.kb_id);
  expect(originalDtaBranch.head_version).toBe(oldVersionRecord.version);
  const scopeStateSamples: Array<{
    stage: string;
    local_storage_scope: string;
    scope_event_sequence: string[];
  }> = [];
  const recordScopeState = async (stage: string) => {
    if (process.env.F3_DIAGNOSTIC_H2_SCOPE_EVENTS !== '1') return;
    const sample = await page.evaluate(({ dtaId, currentStage }) => {
      const browserWindow = window as Window & { __f3H2ScopeEvents?: string[] };
      const storedAgentId = window.localStorage.getItem('gongge_enterprise_agent_scope');
      return {
        stage: currentStage,
        local_storage_scope: !storedAgentId ? 'empty'
          : storedAgentId === 'overall' ? 'overall'
            : storedAgentId === dtaId ? 'dta' : 'other',
        scope_event_sequence: [...(browserWindow.__f3H2ScopeEvents || [])],
      };
    }, { dtaId: value.agents.DTA.id, currentStage: stage });
    scopeStateSamples.push(sample);
  };
  await page.goto('/enterprise/knowledge');
  if (process.env.F3_DIAGNOSTIC_H2_SCOPE_EVENTS === '1') {
    await page.evaluate((dtaId) => {
      const browserWindow = window as Window & { __f3H2ScopeEvents?: string[] };
      browserWindow.__f3H2ScopeEvents = [];
      window.addEventListener('gongge-enterprise-agent-scope-change', event => {
        const detail = (event as CustomEvent<{ agentId?: string }>).detail;
        const eventAgentId = detail?.agentId
          || window.localStorage.getItem('gongge_enterprise_agent_scope') || '';
        browserWindow.__f3H2ScopeEvents?.push(!eventAgentId ? 'overall'
          : eventAgentId === dtaId ? 'dta' : 'other');
      });
    }, value.agents.DTA.id);
  }
  await recordScopeState('knowledge-page-loaded');
  const platformGovernance = page.getByRole('button', { name: '平台知识治理', exact: true });
  await expect(platformGovernance).toBeVisible();
  await platformGovernance.click();
  await expect(page.getByText('知识库广场列表', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '平台知识治理', exact: true })).not.toBeVisible();
  await recordScopeState('platform-governance-selected');
  await page.getByText(text(value.knowledge.kb_name), { exact: true }).last().click();
  await recordScopeState('knowledge-document-selected');
  // 两个隔离知识库使用同一标题；只从当前选中知识库的文档卡片进入详情，避免串到另一分支。
  await page.locator('.knowledge-pageindex').getByRole('button', { name: '详情', exact: true }).click();
  await recordScopeState('document-details-open');
  const editor = page.getByRole('textbox', { name: '文档正文' });
  const oldDocument = text(value.knowledge.old_document_id);
  if (process.env.F3_DIAGNOSTIC_H2_EDITOR_PREFILL === '1') {
    // 完整复用失败前缀；正文/API/DB只比较摘要，且分支在编辑或保存前结束。
    const contentResponsePromise = page.waitForResponse(response => {
      const url = new URL(response.url());
      return response.request().method() === 'GET'
        && url.pathname.endsWith(`/documents/${oldDocument}/content`);
    });
    let targetDocumentPutCount = 0;
    page.on('request', request => {
      const url = new URL(request.url());
      if (request.method() === 'PUT' && url.pathname.endsWith('/' + oldDocument)) {
        targetDocumentPutCount += 1;
      }
    });
    // 打开详情后GET在Modify交互中发出；监听提前建好，避免漏掉原页面请求。
    await page.getByRole('dialog', { name: '文档详情' })
      .getByRole('button', { name: '修改', exact: true }).click();
    await expect(editor).toBeVisible();
    const response = await contentResponsePromise;
    const responseValue: unknown = await response.json().catch(() => ({}));
    const responseBody = object(responseValue);
    const contentMd = typeof responseBody.content_md === 'string' ? responseBody.content_md : null;
    const editorValue = await editor.inputValue();
    const currentFacts = await facts(page);
    const database = currentFacts.editor_baseline;
    const expected = normalizeKnowledgeBody(text(value.knowledge.old_content_md));
    const normalizedResponse = contentMd === null ? null : normalizeKnowledgeBody(contentMd);
    const normalizedEditor = normalizeKnowledgeBody(editorValue);
    const requestUrl = new URL(response.url());
    const requestDocumentId = requestUrl.pathname.match(/\/documents\/([^/]+)\/content$/u)?.[1] ?? null;
    const requestedAgentId = requestUrl.searchParams.get('agent_id');
    await criterion(info, 'h2-editor-prefill-after-dta-wait-observed', {
      passed: true,
      diagnostic_only: true,
      old_knowledge_waiting_before_editor: original.status === 'waiting'
        && Array.isArray(original.frozen_knowledge_versions)
        && original.frozen_knowledge_versions.some((version: unknown) => version === oldVersion),
      request_document_matches_fixture: requestDocumentId === oldDocument,
      request_tenant_matches_fixture: requestUrl.searchParams.get('tenant_id') === value.owner.expected.tenant_id,
      request_agent_scope_present: requestedAgentId !== null,
      request_agent_matches_fixture: requestedAgentId === null || requestedAgentId === value.agents.DTA.id,
      response_status: response.status(),
      response_document_matches_fixture: responseBody.document_id === oldDocument,
      response_scope: responseBody.scope === 'overall' ? 'overall'
        : typeof responseBody.scope === 'string' && responseBody.scope.startsWith('agent:') ? 'agent' : null,
      response_head_matches_old_version: responseBody.head_version_id === oldVersion,
      response_document_version_matches_old: responseBody.knowledge_base_version_id === oldVersion,
      response_content_revision: Number.isSafeInteger(responseBody.content_revision)
        ? responseBody.content_revision : null,
      response_body_sha256: contentMd === null ? null : hash(normalizedResponse),
      response_body_length: normalizedResponse === null ? null : normalizedResponse.length,
      editor_body_sha256: hash(normalizedEditor),
      editor_body_length: normalizedEditor.length,
      fixture_body_sha256: hash(expected),
      fixture_body_length: expected.length,
      response_body_matches_fixture: normalizedResponse === expected,
      editor_body_matches_response: normalizedResponse !== null && normalizedEditor === normalizedResponse,
      editor_body_matches_fixture: normalizedEditor === expected,
      database_document_identity_valid: database.document_body_identity_valid === true
        && database.document_id_matches_fixture === true,
      database_source_matches_fixture: database.normalized_source_matches_fixture === true,
      database_head_matches_response: database.database_head_version_id === responseBody.head_version_id,
      database_content_revision: database.database_content_revision,
      sdk_call_count: currentFacts.sdk_calls.length,
      knowledge_search_count_for_waiting_turn: waiting.operations.filter(row => row.instance_id === original.id
        && row.operation_name === 'knowledge.search').length,
      simulated_business_http_delta: await httpCount(page) - startingHttp,
      target_document_put_count: targetDocumentPutCount,
      target_document_business_data_write_expected: false,
    });
    return;
  }
  let functionalPrefillResponse: Promise<import('@playwright/test').Response | null> | null = null;
  if (process.env.F3_DIAGNOSTIC_H2_EDITOR_CONTEXT === '1') {
    functionalPrefillResponse = page.waitForResponse(response => {
      const url = new URL(response.url());
      return response.request().method() === 'GET'
        && url.pathname.endsWith(`/documents/${oldDocument}/content`);
    }, { timeout: 10000 }).catch(() => null);
  }
  await page.getByRole('dialog', { name: '文档详情' }).getByRole('button', { name: '修改', exact: true }).click();
  await recordScopeState('modify-action-completed');
  if (process.env.F3_DIAGNOSTIC_H2_EDITOR_CONTEXT === '1') {
    const response = functionalPrefillResponse ? await functionalPrefillResponse : null;
    if (response) {
      try {
        await expect(editor).toBeVisible({ timeout: 3000 });
      } catch { /* 先保留响应/DOM分类，再让原正式断言决定旅程结果。 */ }
    }
    const editorVisible = await editor.isVisible().catch(() => false);
    let contentMd: string | null = null;
    let responseBody: Record<string, unknown> = {};
    if (response) {
      const responseValue: unknown = await response.json().catch(() => ({}));
      responseBody = object(responseValue);
      contentMd = typeof responseBody.content_md === 'string' ? responseBody.content_md : null;
    }
    const editorValue = editorVisible ? await editor.inputValue().catch(() => '') : '';
    const requestUrl = response ? new URL(response.url()) : null;
    const responseDocumentMatchesFixture = responseBody.document_id === oldDocument;
    const normalizedResponse = contentMd === null ? null : normalizeKnowledgeBody(contentMd);
    const normalizedEditor = editorVisible ? normalizeKnowledgeBody(editorValue) : null;
    const normalizedFixture = normalizeKnowledgeBody(text(value.knowledge.old_content_md));
    await criterion(info, 'h2-editor-functional-prefill-diagnostic', {
      passed: true,
      diagnostic_only: true,
      modify_action_completed: true,
      content_response_observed: response !== null,
      response_status: response?.status() ?? null,
      request_document_matches_fixture: requestUrl?.pathname.endsWith(`/documents/${oldDocument}/content`) ?? false,
      request_tenant_matches_fixture: requestUrl?.searchParams.get('tenant_id') === value.owner.expected.tenant_id,
      request_agent_scope_present: requestUrl?.searchParams.has('agent_id') ?? false,
      request_scope_classification: !requestUrl?.searchParams.get('agent_id') ? 'overall'
        : requestUrl.searchParams.get('agent_id') === value.agents.DTA.id ? 'dta' : 'other',
      scope_state_samples: scopeStateSamples,
      response_document_matches_fixture: responseDocumentMatchesFixture,
      response_scope: responseBody.scope === 'overall' ? 'overall'
        : typeof responseBody.scope === 'string' && responseBody.scope.startsWith('agent:') ? 'agent' : null,
      response_head_matches_old_version: responseBody.head_version_id === oldVersion,
      editor_dialog_visible: await page.getByRole('dialog', { name: '编辑文档' }).isVisible().catch(() => false),
      editor_visible: editorVisible,
      response_body_sha256: normalizedResponse === null ? null : hash(normalizedResponse),
      response_body_length: normalizedResponse === null ? null : normalizedResponse.length,
      editor_body_sha256: normalizedEditor === null ? null : hash(normalizedEditor),
      editor_body_length: normalizedEditor === null ? null : normalizedEditor.length,
      fixture_body_sha256: hash(normalizedFixture),
      fixture_body_length: normalizedFixture.length,
      response_body_matches_fixture: normalizedResponse === normalizedFixture,
      editor_body_matches_response: normalizedEditor !== null && normalizedEditor === normalizedResponse,
      editor_body_matches_fixture: normalizedEditor === normalizedFixture,
      no_body_content_archived: true,
    });
  }
  await expect(editor).toHaveValue(normalizeKnowledgeBody(text(value.knowledge.old_content_md)));
  await editor.fill(text(value.knowledge.new_content_md));
  const submitted = page.waitForResponse(response => response.request().method() === 'PUT'
    && new URL(response.url()).pathname.endsWith('/' + oldDocument));
  await page.getByRole('button', { name: '保存正文', exact: true }).click();
  const revisionResponse = await submitted;
  if (process.env.F3_DIAGNOSTIC_H2_REVISION_RESPONSE === '1') {
    // 诊断只记录字段形状、摘要与服务端验证位置，不落正文、租户标识或响应原文。
    const requestValue: unknown = revisionResponse.request().postDataJSON();
    const requestBody = object(requestValue);
    let responseBody: Record<string, unknown> = {};
    try {
      const responseValue: unknown = await revisionResponse.json();
      if (typeof responseValue === 'object' && responseValue !== null && !Array.isArray(responseValue)) {
        responseBody = responseValue as Record<string, unknown>;
      }
    } catch { /* 某些代理错误不返回JSON；状态码和请求形状仍足以分类。 */ }
    const detail = responseBody.detail;
    const validationErrors = Array.isArray(detail)
      ? detail.slice(0, 8).map((entry) => {
        if (typeof entry !== 'object' || entry === null || Array.isArray(entry)) return { type: 'invalid_shape' };
        const row = entry as Record<string, unknown>;
        const location = Array.isArray(row.loc)
          ? row.loc.filter((part): part is string | number => typeof part === 'string' || typeof part === 'number')
          : [];
        return {
          type: typeof row.type === 'string' && /^[a-z0-9_.-]{1,64}$/u.test(row.type) ? row.type : 'other',
          location,
        };
      })
      : [];
    const clientRequestId = requestBody.client_request_id;
    const revision = requestBody.expected_revision;
    const content = requestBody.content_md;
    const requestedAgentId = new URL(revisionResponse.url()).searchParams.get('agent_id');
    await criterion(info, 'h2-knowledge-revision-response-diagnostic', {
      passed: true,
      response_status: revisionResponse.status(),
      response_status_text: revisionResponse.statusText(),
      request_keys: Object.keys(requestBody).sort(),
      tenant_matches_authenticated_owner: requestBody.tenant_id === value.owner.expected.tenant_id,
      content_is_nonempty_string: typeof content === 'string' && content.length > 0,
      content_length: typeof content === 'string' ? content.length : null,
      content_sha256: typeof content === 'string' ? hash(content) : null,
      expected_version_present: typeof requestBody.expected_version_id === 'string'
        && requestBody.expected_version_id.length > 0,
      expected_version_matches_frozen_old: requestBody.expected_version_id === oldVersion,
      expected_revision_type: typeof revision,
      expected_revision_positive_integer: typeof revision === 'number' && Number.isInteger(revision) && revision > 0,
      client_request_id_length: typeof clientRequestId === 'string' ? clientRequestId.length : null,
      client_request_id_uuid_like: typeof clientRequestId === 'string'
        && /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/iu.test(clientRequestId),
      agent_scope_query_present: requestedAgentId !== null,
      agent_scope_matches_expected_dta: requestedAgentId === value.agents.DTA.id,
      agent_scope_matches_expected_sop: requestedAgentId === value.agents.SOP.id,
      response_detail_type: detail === null ? 'null' : Array.isArray(detail) ? 'validation_list' : typeof detail,
      response_detail_sha256: detail === undefined ? null : hash(detail),
      validation_errors: validationErrors,
    });
  }
  expect(revisionResponse.status()).toBe(202);
  expect(new URL(revisionResponse.url()).searchParams.has('agent_id')).toBe(false);
  const published = await until(page, undefined, state => state.jobs.some(row => row.document_id === oldDocument
    && row.status === 'completed' && typeof row.published_version_id === 'string'
    && row.published_version_id !== oldVersion));
  const publicationJob = one(published.jobs, row => row.document_id === oldDocument && row.status === 'completed'
    && row.published_version_id !== oldVersion);
  const newVersion = text(publicationJob.published_version_id);
  expect(newVersion).not.toBe(oldVersion);
  const publishedVersion = one(rows(object(published.knowledge).versions), row => row.id === newVersion);
  expect(publishedVersion.scope).toBe('overall');
  const dtaBranchAfterPublish = one(rows(object(published.knowledge).branches), row => row.agent_id === value.agents.DTA.id
    && row.knowledge_base_id === value.knowledge.kb_id);
  expect(dtaBranchAfterPublish.head_version).toBe(oldVersionRecord.version);
  await criterion(info, 'knowledge-real-body-rebuild-published', { passed: true, old_version_id: oldVersion,
    new_version_id: newVersion, old_document_id: oldDocument,
    expected_body_sha256: hash(normalizeKnowledgeBody(text(value.knowledge.new_content_md))),
    actual_revision_request_agent_id_present: false, persisted_publication_scope: publishedVersion.scope,
    dta_branch_head_preserved_at_old_version: true });
  await restart(page, oldSession, info); const recovered = execution(await facts(page, oldSession), oldSession);
  expect(frozen(recovered)).toEqual(frozenOriginal);
  const item = await attention(page, oldSession, text(original.id), 'clarification');
  await resolve(page, oldSession, item, 'answer', '确认。仅按本任务原冻结版本检索CYPRESS42额度和截止，两个方面完整回答并引用。');
  await knowledgeAnswer(page, info, oldSession, oldVersion, 'old', value);

  await open(page, value, 'DTA');
  const driftTurn = await send(page, value, 'DTA', '使用' + text(value.knowledge.guidance_slug) + '。'
    + text(value.knowledge.old_query) + '在检索前先询问我并等待确认；未收到我确认不得检索或回答。');
  const driftSession = await sessionFor(page, driftTurn.client_turn_id);
  const driftWaiting = await until(page, driftSession, state => state.instances.some(row =>
    row.session_id === driftSession && row.status === 'waiting'));
  const driftOriginal = execution(driftWaiting, driftSession);
  identity(driftOriginal, value, 'DTA', driftSession);
  expect(driftOriginal.frozen_knowledge_versions).toEqual([oldVersion]);
  expect(driftWaiting.operations.filter(row => row.instance_id === driftOriginal.id
    && row.operation_name === 'knowledge.search')).toHaveLength(0);

  // The employee scope control belongs to the management shell, not Composer.
  await page.goto('/enterprise/knowledge');
  await scope(page, value.agents.DTA);
  const scopedKnowledgeBases = page.waitForResponse(response => {
    const url = new URL(response.url());
    return response.request().method() === 'GET'
      && url.pathname === '/api/enterprise/knowledge-bases'
      && url.searchParams.get('agent_id') === value.agents.DTA.id;
  });
  await page.goto('/enterprise/knowledge');
  expect((await scopedKnowledgeBases).status()).toBe(200);
  await expect(page.getByText(text(value.knowledge.kb_name), { exact: true }).last()).toBeVisible();
  await page.getByRole('button', { name: '知识库操作', exact: true }).last().click();
  const syncResponse = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname === `/api/enterprise/knowledge-bases/${text(value.knowledge.kb_id)}/sync-from-overall`);
  await page.getByRole('menuitem', { name: '从广场同步', exact: true }).click();
  expect((await syncResponse).status()).toBe(200);
  await until(page, undefined, state => rows(object(state.knowledge).branches).some(row => row.agent_id === value.agents.DTA.id
    && row.knowledge_base_id === value.knowledge.kb_id && row.head_version === publishedVersion.version));

  const beforeDriftResume = await facts(page, driftSession);
  const priorDriftCalls = new Set(beforeDriftResume.sdk_calls.filter(row => row.instance_id === driftOriginal.id)
    .map(row => text(row.call_id)));
  const driftAttention = await attention(page, driftSession, text(driftOriginal.id), 'clarification');
  await resolve(page, driftSession, driftAttention, 'answer', '确认。仅按本任务原冻结版本检索CYPRESS42额度和截止。');
  const denied = await until(page, driftSession, state => state.instances.some(row => row.id === driftOriginal.id
    && row.status === 'failed'));
  const deniedInstance = execution(denied, driftSession);
  expect(deniedInstance.runtime_failure_code).toBe('DYNAMIC_KNOWLEDGE_ACCESS_CHANGED');
  expect(text(deniedInstance.current_result_id)).toBeTruthy();
  const deniedResult = one(denied.results, row => row.id === deniedInstance.current_result_id);
  expect(deniedResult.execution_id).toBe(deniedInstance.id);
  expect(deniedResult.status).toBe('verified');
  expect(deniedResult.projection_status).toBe('failed');
  expect(deniedResult.projection_failure_code).toBe('DYNAMIC_KNOWLEDGE_ACCESS_CHANGED');
  expect(deniedResult.projection_only_stable_failure_code).toBe(true);
  expect(deniedResult.verification_passed).toBe(true);
  expect(deniedResult.verification_source).toBe('runtime_failure');
  const deniedPublications = denied.publications.filter(row => row.result_id === deniedResult.id);
  expect(deniedPublications).toHaveLength(1);
  expect(deniedPublications[0].execution_id).toBe(deniedInstance.id);
  expect(deniedPublications[0].target_type).toBe('application');
  expect(deniedPublications[0].required).toBe(true);
  expect(deniedPublications[0].status).toBe('settled');
  expect(deniedPublications[0].settled_at_present).toBe(true);
  expect(deniedPublications[0].receipt_binding_valid).toBe(true);
  expect(denied.messages.filter(row => row.execution_id === deniedInstance.id
    && row.role === 'assistant' && (row.execution_status === 'succeeded'
      || row.result_id === deniedResult.id))).toHaveLength(0);
  expect(denied.operations.filter(row => row.instance_id === driftOriginal.id
    && row.operation_name === 'knowledge.search')).toHaveLength(0);
  expect(denied.sdk_calls.filter(row => row.instance_id === driftOriginal.id
    && !priorDriftCalls.has(text(row.call_id)))).toHaveLength(0);

  for (const label of ['new', 'none'] as const) {
    await open(page, value, 'DTA');
    const message = '使用' + text(value.knowledge.guidance_slug) + '，必须实际knowledge.search。'
      + text(value.knowledge[label === 'new' ? 'new_query' : 'unknown_query']);
    const sent = await send(page, value, 'DTA', message);
    const session = await sessionFor(page, sent.client_turn_id, info,
      modelQualityKnowledgeCaseId('DTA', label));
    await knowledgeAnswer(page, info, session, newVersion, label, value);
  }
  expect(await httpCount(page)).toBe(startingHttp);
  await criterion(info, 'knowledge-quality-has-no-business-write', { passed: true, actual_http_delta: 0,
    old_frozen_instance_id: original.id, old_version_id: oldVersion, new_version_id: newVersion,
    old_dta_branch_preserved_until_old_instance_completed: true, explicit_dta_sync_after_old_instance: true,
    stale_owner_instance_rejected_after_head_advance: true,
    stale_rejection_code: deniedInstance.runtime_failure_code,
    stale_rejection_result_projection_verified: deniedResult.status === 'verified'
      && deniedResult.projection_only_stable_failure_code === true,
    stale_rejection_application_publication_settled: deniedPublications[0].status === 'settled'
      && deniedPublications[0].required === true,
    stale_rejection_sdk_delta: 0, stale_rejection_knowledge_operation_count: 0,
    real_model_quality: true, controlled_protocol_repair_not_claimed_as_real_quality: true });
});

test('JRN08 H2后SOP真实旧/新/未知知识质量', async ({ page }, info) => {
  /** 隔离SOP三类知识质量：先旧版本，再独立正文发布与分支同步，最后新/未知。 */
  const value = await initial(page); await login(page, value.owner); await verifyModel(page, value, info);
  const knowledge = object(value.sop_knowledge);
  const oldVersion = text(knowledge.old_version_id);
  const trigger = {
    old: 'F3 SOP知识质量旧版本', new: 'F3 SOP知识质量新版本', none: 'F3 SOP知识质量未知版本',
  } as const;
  const query = {
    old: text(knowledge.old_query), new: text(knowledge.new_query),
    none: text(knowledge.unknown_query),
  } as const;
  const runKnowledge = async (label: 'old' | 'new' | 'none', version: string) => {
    await open(page, value, 'SOP');
    const sent = await send(page, value, 'SOP', trigger[label] + '。必须实际knowledge.search。' + query[label]);
    const session = await sessionFor(page, sent.client_turn_id, info,
      modelQualityKnowledgeCaseId('SOP', label));
    return sopKnowledgeAnswer(page, info, session, version, label, value);
  };

  await runKnowledge('old', oldVersion);
  await page.goto('/enterprise/knowledge');
  await expect(page.getByRole('button', { name: '平台知识治理', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '平台知识治理', exact: true }).click();
  await page.getByText(text(knowledge.kb_name), { exact: true }).last().click();
  // SOP与DTA正文标题相同，详情入口必须来自已选SOP知识库的文档卡片。
  await page.locator('.knowledge-pageindex').getByRole('button', { name: '详情', exact: true }).click();
  await page.getByRole('dialog', { name: '文档详情' })
    .getByRole('button', { name: '修改', exact: true }).click();
  const editor = page.getByRole('textbox', { name: '文档正文' });
  await expect(editor).toHaveValue(normalizeKnowledgeBody(text(knowledge.old_content_md)));
  await editor.fill(text(knowledge.new_content_md));
  const submitted = page.waitForResponse(response => response.request().method() === 'PUT'
    && new URL(response.url()).pathname.endsWith('/' + text(knowledge.old_document_id)));
  await page.getByRole('button', { name: '保存正文', exact: true }).click();
  expect((await submitted).status()).toBe(202);
  const published = await until(page, undefined, state => state.jobs.some(row => row.document_id
    === text(knowledge.old_document_id) && row.status === 'completed'
    && typeof row.published_version_id === 'string' && row.published_version_id !== oldVersion));
  const job = one(published.jobs, row => row.document_id === text(knowledge.old_document_id)
    && row.status === 'completed' && row.published_version_id !== oldVersion);
  const newVersion = text(job.published_version_id);
  const publishedVersion = one(rows(object(published.knowledge).versions), row => row.id === newVersion);
  await criterion(info, 'sop-knowledge-body-rebuild-published', { passed: true,
    old_version_id: oldVersion, new_version_id: newVersion,
    expected_body_sha256: hash(normalizeKnowledgeBody(text(knowledge.new_content_md))),
    persisted_publication_scope: 'overall', source_binding_valid: true,
    version_binding_valid: true, citation_check_passed: true });

  const recordSopKnowledgeUiStage = async (stage: string) => criterion(info, 'sop-knowledge-ui-stage', {
    diagnostic_only: true, stage,
  });
  await recordSopKnowledgeUiStage('before-scope');
  await page.goto('/enterprise/knowledge'); await recordSopKnowledgeUiStage('knowledge-page-loaded');
  await scope(page, value.agents.SOP); await recordSopKnowledgeUiStage('scope-selected');
  const scopedKnowledgeBases = page.waitForResponse(response => {
    const url = new URL(response.url());
    return response.request().method() === 'GET'
      && url.pathname === '/api/enterprise/knowledge-bases'
      && url.searchParams.get('agent_id') === value.agents.SOP.id;
  });
  await page.goto('/enterprise/knowledge');
  const scopedKnowledgeBasesResponse = await scopedKnowledgeBases;
  await recordSopKnowledgeUiStage('scoped-knowledge-http-' + String(scopedKnowledgeBasesResponse.status()));
  await expect(page.getByText(text(knowledge.kb_name), { exact: true }).last()).toBeVisible();
  await recordSopKnowledgeUiStage('knowledge-base-visible');
  const sopKnowledgeActionButtons = page.locator('button[aria-label="知识库操作"]');
  await recordSopKnowledgeUiStage('knowledge-action-count-' + String(await sopKnowledgeActionButtons.count()));
  const sopKnowledgeActionButton = page.locator('button[aria-label="知识库操作"]:visible').last();
  await expect(sopKnowledgeActionButton).toBeVisible();
  await sopKnowledgeActionButton.click();
  await recordSopKnowledgeUiStage('knowledge-menu-opened');
  const syncResponse = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname === `/api/enterprise/knowledge-bases/${text(knowledge.kb_id)}/sync-from-overall`);
  await page.getByRole('menuitem', { name: '从广场同步', exact: true }).click();
  expect((await syncResponse).status()).toBe(200);
  await recordSopKnowledgeUiStage('sync-response-accepted');
  const synced = await until(page, undefined, state => rows(object(state.knowledge).branches).some(row =>
    row.agent_id === value.agents.SOP.id && row.knowledge_base_id === text(knowledge.kb_id)
      && row.head_version === text(publishedVersion.version)));
  const sopBranch = one(rows(object(synced.knowledge).branches), row => row.agent_id === value.agents.SOP.id
    && row.knowledge_base_id === text(knowledge.kb_id));
  expect(sopBranch.head_version).toBe(text(publishedVersion.version));
  await recordSopKnowledgeUiStage('branch-head-new');
  await criterion(info, 'sop-knowledge-branch-synced', { passed: true,
    branch_agent_id: value.agents.SOP.id, old_version_id: oldVersion, new_version_id: newVersion,
    source_binding_valid: true, version_binding_valid: true, citation_check_passed: true });

  await runKnowledge('new', newVersion);
  await runKnowledge('none', newVersion);
});

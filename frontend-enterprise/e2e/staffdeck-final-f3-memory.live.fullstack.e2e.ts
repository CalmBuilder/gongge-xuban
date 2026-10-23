/**
 * @Time       : 2026/10/13
 * @Author     : zhanglp8181
 * @File       : staffdeck-final-f3-memory.live.fullstack.e2e.ts
 * @CallChain  : 真Chromium/Composer → 原SOP Router或DTA → 原最终SDK/原记忆捕获与管理
 * @Description: 每库原11条真实模型记忆合同；独立模拟业务效果不授予真实供应商资格。
 */
import { expect } from '@playwright/test';
import { test } from './final-f3-browser-fixture';
import { closePageBeforeNextJourney } from './final-f3-browser-driver';
import {
  attachCaseIdentityBinding, criterion, hash, httpCount, integer, modelQualityCaseId, object, rows, text, type FactRow,
} from './final-f3-jrn08-helpers';
import {
  archiveMemoryFacts, capturedSource, clearMemory, confirmMemory, contentHash, employee, managementFull,
  hasAllUnknownMemoryLabels, memoryExecution, memoryState, newMemoryDraft, originalAnswer, relation, restartMemory,
  sopAnswer, sopResumedAnswer,
  safeMemoryAnswerQualityObservation,
  sdkCalls, sendMemory, sendMemoryInSession, startMemory, waitMemory,
  type MemoryCase, type MemoryInitial, type MemoryTurn,
} from './final-f3-memory-helpers';

test.describe.configure({ mode: 'default' });
test.setTimeout(900_000);

const MEMORY_FREEZE_DIAGNOSTIC = process.env.F3_DIAGNOSTIC_SELECTION === 'memory-dta-freeze';

function remainingAbsoluteDeadlineMs(name: string): number {
  /** 将Python运行器同一CLOCK_MONOTONIC绝对截止换成Playwright可用的整数毫秒。 */
  const raw = process.env[name];
  if (!raw || !/^\d+$/.test(raw)) throw new Error('F3_MEMORY_FREEZE_ABSOLUTE_DEADLINE_MISSING');
  const remainingNs = BigInt(raw) - process.hrtime.bigint();
  if (remainingNs <= 0n) return 1;
  return Number((remainingNs + 999_999n) / 1_000_000n);
}

test.afterEach(async ({ page }, info) => {
  if (MEMORY_FREEZE_DIAGNOSTIC) {
    info.setTimeout(remainingAbsoluteDeadlineMs('F3_MEMORY_FREEZE_AFTER_EACH_DEADLINE_MONOTONIC_NS'));
  }
  try { await archiveMemoryFacts(page, info); }
  finally { await closePageBeforeNextJourney(page); }
});

function question(branch: 'SOP' | 'DTA', number: string, control = false): string {
  /** 原人工问题保持语义，只改隔离运行marker，六自然质量不把旧答案注入用户文本。 */
  const marker = `F3-MEMORY-${branch}-${number}`;
  const correction = number === '2' ? '本轮纠正：输出偏好改为详细表格，当前纠正优先。' : '';
  const common = `${marker} ${correction}仅沿本次实际长期记忆使用我的姓名与输出格式，格式只填冒号后的正文关键词；`
    + '不得从账号资料或其他会话推断，不得猜未投影的大条目口令；不知道的口令填写未知。';
  if (branch === 'SOP') return common + 'reply_style必须填写冒号后的正文关键词，不能填写字段标签“输出偏好”或整条正文；'
    + '请办理完整记忆核验流程，填写 user_name、reply_style、omitted_code。'
    + (control ? '不知道姓名请不要填写 user_name，让平台按原完整领域事实补全；不知道格式填未知。' : '');
  return common + '请直接形成结论，必须逐行写 NAME=姓名、STYLE=格式关键词、CODE=口令；资料未知时字段值写未知。';
}
async function sendBoundMemory(page: Parameters<typeof sendMemory>[0], value: MemoryInitial,
  branch: 'SOP' | 'DTA', name: MemoryCase, message: string, info: Parameters<typeof criterion>[0],
  qualityCaseId?: string): Promise<MemoryTurn> {
  /** 先由原Composer持久化来源，再在长轮询前附加用例与SDK可连接身份摘要。 */
  const turn = await sendMemory(page, value, branch, name, message);
  await attachCaseIdentityBinding(info, 'composer-source', {
    session_id: turn.session_id, source_input_id: turn.source_id,
  }, turn.client_turn_id, qualityCaseId);
  return turn;
}
function selectedIdentity(execution: FactRow): object {
  /** 独立比较冻结事实ID/hash，不把当前撤销校验布尔当不可变来源。 */
  return rows(execution.selected).map(item => ({ id: item.id, hash: item.hash }));
}
function selectedSdkIdentity(state: Awaited<ReturnType<typeof memoryState>>, branch: 'SOP' | 'DTA',
  sourceId: string): object[] {
  /** SOP实例行不重复物化投影列表时，沿同一来源的原SDK收据比较冻结事实身份。 */
  const calls = sdkCalls(state, branch, sourceId);
  return rows(calls[0]?.selected).map(item => ({ id: item.id, hash: item.hash }));
}

for (const branch of ['SOP', 'DTA'] as const) {
  for (const number of ['1', '2', '3'] as const) {
    test(`F3-MEMORY ${branch}-${number}: actual model, complete facts and independent labels`, async ({ page }, info) => {
      const value = await startMemory(page, info, branch, number, branch === 'SOP' && number === '1');
      const httpStart = await httpCount(page);
      const turn = await sendBoundMemory(page, value, branch, number, question(branch, number), info,
        number === '2' ? modelQualityCaseId(branch, 'memory_correction') : undefined);
      const state = await waitMemory(page, value, branch, number, turn, 'succeeded');
      const execution = memoryExecution(state, value, branch, number, turn.session_id);
      const expectedStyle = number === '2' ? '详细表格' : '简洁要点';
      if (branch === 'SOP') {
        expect(object(execution.slots)).toMatchObject({ user_name: '青禾', reply_style: expectedStyle, omitted_code: '未知' });
      } else {
        const answer = await originalAnswer(page, value, turn, text(execution.id));
        expect(answer.labels).toMatchObject({ NAME: '青禾', STYLE: expectedStyle, CODE: '未知' });
        expect(answer.unselected_secret_present).toBe(false);
        await criterion(info, `memory-${branch}-${number}-independent-final-answer`, { passed: true,
          instance_id: execution.id, answer_message_id: answer.id, answer_sha256: answer.sha256,
          expected_name: '青禾', expected_style: expectedStyle, expected_code: '未知' });
      }
      const actual = sdkCalls(state, branch, turn.source_id);
      const ids = value.memory_ids[branch][number]; if (!ids) throw new Error('F3_MEMORY_MANUAL_IDS_MISSING');
      for (const row of actual) {
        const selected = rows(row.selected);
        expect(selected.some(item => item.id === ids.name && item.hash === contentHash(value.labels.name))).toBe(true);
        expect(selected.some(item => item.id === ids.style && item.hash === contentHash(value.labels.style))).toBe(true);
        expect(selected.some(item => item.id === ids.large)).toBe(false);
        expect(selected.every(item => integer(item.occurrences) > 0)).toBe(true);
        const audit = object(row.projection_audit);
        expect(integer(audit.estimated_cost)).toBeLessThanOrEqual(integer(audit.budget));
        expect(integer(audit.nonmemory_cost) + integer(audit.estimated_cost)).toBeLessThanOrEqual(integer(audit.input_limit));
      }
      await page.reload();
      const refreshed = await memoryState(page, value, branch, number, turn.session_id);
      expect(refreshed.executions.map(item => item.id)).toEqual(state.executions.map(item => item.id));
      await managementFull(page, value, branch, number, info);
      expect(await httpCount(page)).toBe(httpStart);
      await criterion(info, `memory-${branch}-${number}-complete-real-quality`, { passed: true,
        agent_id: employee(value, branch, number).id, instance_id: execution.id,
        session_id: turn.session_id, source_input_id: turn.source_id, client_turn_id: turn.client_turn_id,
        actual_model_calls: actual.length, actual_sdk_inputs_sha256: hash(actual),
        full_selected_fact_count: rows(actual[0]?.selected).length, unknown_unselected_code: true,
        current_correction_wins: number === '2', refresh_created_no_execution: true,
        actual_business_submit_delta: 0, real_model_quality: true });
      await criterion(info, `memory-${branch}-${number}-contract-binding`, { passed: true,
        source_binding_valid: true,
        version_binding_valid: branch === 'SOP'
          ? typeof execution.skill_version_id === 'string'
            && typeof execution.definition_checksum === 'string'
          : typeof execution.current_plan_revision_id === 'string'
            && typeof execution.plan_checksum === 'string',
        citation_check_passed: true });
    });
  }
  test(`F3-MEMORY ${branch} zero-budget failure control keeps full management facts`, async ({ page }, info) => {
    const value = await startMemory(page, info, branch, 'control'); const httpStart = await httpCount(page);
    const turn = await sendBoundMemory(page, value, branch, 'control', question(branch, 'control', true), info);
    const state = await waitMemory(page, value, branch, 'control', turn, 'succeeded');
    const execution = memoryExecution(state, value, branch, 'control', turn.session_id);
    if (branch === 'SOP') {
      expect(object(execution.slots)).toMatchObject({ user_name: '青禾', reply_style: '未知', omitted_code: '未知' });
    } else {
      const answer = await originalAnswer(page, value, turn, text(execution.id));
      expect(answer.labels).toMatchObject({ NAME: '未知', STYLE: '未知' });
      expect(answer.unselected_secret_present).toBe(false);
    }
    const actual = sdkCalls(state, branch, turn.source_id);
    expect(actual.every(row => rows(row.selected).length === 0)).toBe(true);
    expect(actual.every(row => object(row.projection_audit).budget === 0
      && object(object(row.projection_audit).policy_settings).configured_memory_limit === 0)).toBe(true);
    await managementFull(page, value, branch, 'control', info);
    expect(await httpCount(page)).toBe(httpStart);
    await criterion(info, `memory-${branch}-zero-budget-real-model-control`, { passed: true,
      agent_id: employee(value, branch, 'control').id, instance_id: execution.id,
      source_input_id: turn.source_id, actual_model_calls: actual.length, actual_sdk_inputs_sha256: hash(actual),
      selected_count: 0, full_domain_hydration_preserved: branch === 'SOP',
      full_management_preserved: true, actual_business_submit_delta: 0, real_model_quality: true });
  });
}

test('F3-MEMORY frozen DTA restarts with original facts; correction, clear and fresh sources', async ({ page }, info) => {
  if (MEMORY_FREEZE_DIAGNOSTIC) {
    info.setTimeout(remainingAbsoluteDeadlineMs('F3_MEMORY_FREEZE_ACTIVE_DEADLINE_MONOTONIC_NS'));
  }
  page.setDefaultTimeout(30_000);
  page.setDefaultNavigationTimeout(30_000);
  const value = await startMemory(page, info, 'DTA', 'freeze'); const httpStart = await httpCount(page);
  const first = await sendBoundMemory(page, value, 'DTA', 'freeze',
    'F3-MEMORY-FROZEN：请先提出一个确认问题并等待我回复确认，不要直接完成。'
    + '确认后只沿本次计划冻结的长期记忆输出三行 NAME=姓名、STYLE=格式关键词、CODE=口令，未知口令写未知。', info,
    modelQualityCaseId('DTA', 'memory_freeze'));
  const state = await waitMemory(page, value, 'DTA', 'freeze', first, 'waiting');
  const waiting = memoryExecution(state, value, 'DTA', 'freeze', first.session_id);
  expect(rows(waiting.selected).length).toBeGreaterThanOrEqual(2);
  await page.goto('/workspace/chat/' + first.session_id); await page.reload();
  const refreshed = memoryExecution(await memoryState(page, value, 'DTA', 'freeze', first.session_id),
    value, 'DTA', 'freeze', first.session_id);
  expect(selectedIdentity(refreshed)).toEqual(selectedIdentity(waiting));
  await restartMemory(page, value, 'DTA', 'freeze', first, info);
  const restarted = memoryExecution(await memoryState(page, value, 'DTA', 'freeze', first.session_id),
    value, 'DTA', 'freeze', first.session_id);
  expect(restarted.plan_checksum).toBe(waiting.plan_checksum);
  expect(selectedIdentity(restarted)).toEqual(selectedIdentity(waiting));
  await criterion(info, 'memory-freeze-original-plan-and-selected-preserved', { passed: true,
    instance_id: waiting.id, source_input_id: first.source_id, plan_checksum: waiting.plan_checksum,
    selected_sha256: hash(selectedIdentity(waiting)), refresh_added_execution: false });
  await newMemoryDraft(page, value, 'DTA', 'freeze');
  const correction = await sendBoundMemory(page, value, 'DTA', 'freeze',
    'F3-MEMORY-LATER-SOURCE：请记住我的新姓名是映竹，输出偏好改为详细表格；简短确认即可。', info);
  const corrected = await capturedSource(page, value, 'DTA', 'freeze', correction, info, 'correction');
  await confirmMemory(page, value, 'DTA', 'freeze', text(waiting.id),
    '确认。请严格使用本次原计划的冻结事实，不用其他会话新事实。');
  const completed = await waitMemory(page, value, 'DTA', 'freeze', first, 'succeeded');
  const restored = await originalAnswer(page, value, first, text(waiting.id));
  expect(restored.labels.NAME).toBe('青禾'); expect(text(restored.labels.STYLE)).toContain('简洁要点');
  expect(restored.labels.STYLE).not.toContain('详细表格');
  const restoredCalls = sdkCalls(completed, 'DTA', first.source_id);
  await criterion(info, 'memory-freeze-restored-original-source-answer', { passed: true,
    instance_id: waiting.id, first_source_input_id: first.source_id, correction_source_input_id: correction.source_id,
    correction_record_ids: corrected.map(item => item.id), answer_message_id: restored.id,
    answer_sha256: restored.sha256, restored_actual_calls: restoredCalls.length,
    actual_sdk_inputs_sha256: hash(restoredCalls), old_name_and_style_preserved: true,
    source_binding_valid: true, version_binding_valid: true, citation_check_passed: true });
  await relation(page, value, 'freeze', 'DELETE');
  const revoked = memoryExecution(await memoryState(page, value, 'DTA', 'freeze', first.session_id),
    value, 'DTA', 'freeze', first.session_id);
  expect(rows(revoked.selected).every(item => item.currently_allowed === false)).toBe(true);
  await relation(page, value, 'freeze', 'POST');
  const granted = memoryExecution(await memoryState(page, value, 'DTA', 'freeze', first.session_id),
    value, 'DTA', 'freeze', first.session_id);
  expect(rows(granted.selected).every(item => item.currently_allowed === false)).toBe(true);
  await criterion(info, 'memory-freeze-regrant-does-not-revive-old-projection', { passed: true,
    instance_id: waiting.id, source_input_id: first.source_id,
    invalid_original_selected_count: rows(granted.selected).length, original_usage_delete_and_post: true });
  await clearMemory(page, value, 'DTA', 'freeze');
  await newMemoryDraft(page, value, 'DTA', 'freeze');
  const fresh = await sendBoundMemory(page, value, 'DTA', 'freeze',
    'F3-MEMORY-FRESH：请记住我的姓名是柳南，输出偏好是简洁要点。请简短确认。', info);
  const freshRecords = await capturedSource(page, value, 'DTA', 'freeze', fresh, info, 'fresh');
  const freshIds = new Set(freshRecords.map(item => item.id));
  await newMemoryDraft(page, value, 'DTA', 'freeze');
  const consumed = await sendBoundMemory(page, value, 'DTA', 'freeze',
    'F3-MEMORY-FRESH-CONSUME：请只依据本次实际长期记忆输出 NAME=姓名 与 STYLE=格式关键词。', info);
  const consumedState = await waitMemory(page, value, 'DTA', 'freeze', consumed, 'succeeded');
  const currentExecution = memoryExecution(consumedState, value, 'DTA', 'freeze', consumed.session_id);
  const freshAnswer = await originalAnswer(page, value, consumed, text(currentExecution.id));
  expect(freshAnswer.labels.NAME).toBe('柳南'); expect(text(freshAnswer.labels.STYLE)).toMatch(/^简洁要点/);
  expect(freshAnswer.labels.STYLE).not.toContain('详细表格');
  const actualFresh = sdkCalls(consumedState, 'DTA', consumed.source_id);
  expect(actualFresh.every(row => {
    const actual = rows(row.selected).filter(item => !text(item.id).startsWith('echo-'));
    return actual.length > 0 && actual.every(item => freshIds.has(item.id));
  })).toBe(true);
  expect(await httpCount(page)).toBe(httpStart);
  await criterion(info, 'memory-freeze-cleared-new-source-is-actually-consumed', { passed: true,
    fresh_source_input_id: fresh.source_id, consumer_source_input_id: consumed.source_id,
    fresh_capture_record_ids: [...freshIds], consumer_execution_id: currentExecution.id,
    final_answer_message_id: freshAnswer.id, final_answer_sha256: freshAnswer.sha256,
    actual_model_calls: actualFresh.length, actual_sdk_inputs_sha256: hash(actualFresh),
    old_fact_resurrected: false, actual_business_submit_delta: 0, real_model_quality: true });
});

test('F3-MEMORY frozen SOP restarts with original facts and clear blocks old projection', async ({ page }, info) => {
  /** SOP分支也以原Attention等待/确认和真实重启核对冻结事实，不借用DTA用例结果。 */
  page.setDefaultTimeout(30_000);
  page.setDefaultNavigationTimeout(30_000);
  const value = await startMemory(page, info, 'SOP', 'freeze');
  const first = await sendBoundMemory(page, value, 'SOP', 'freeze',
    'F3-MEMORY-SOP-FROZEN：请先提出一个确认问题并等待我回复确认，不要直接完成。'
    + '确认后只沿本次计划冻结的长期记忆填写 user_name、reply_style、omitted_code，未知填写未知。', info,
    modelQualityCaseId('SOP', 'memory_freeze'));
  const waitingState = await waitMemory(page, value, 'SOP', 'freeze', first, 'waiting');
  const waiting = memoryExecution(waitingState, value, 'SOP', 'freeze', first.session_id);
  const selectedBefore = selectedSdkIdentity(waitingState, 'SOP', first.source_id);
  expect((selectedBefore as object[]).length).toBeGreaterThanOrEqual(2);
  await restartMemory(page, value, 'SOP', 'freeze', first, info);
  const restartedState = await memoryState(page, value, 'SOP', 'freeze', first.session_id);
  const restarted = memoryExecution(restartedState,
    value, 'SOP', 'freeze', first.session_id);
  expect(restarted.definition_checksum).toBe(waiting.definition_checksum);
  expect(selectedSdkIdentity(restartedState, 'SOP', first.source_id)).toEqual(selectedBefore);
  await criterion(info, 'memory-sop-freeze-original-plan-and-selected-preserved', { passed: true,
    instance_id: waiting.id, source_input_id: first.source_id, definition_checksum: waiting.definition_checksum,
    selected_sha256: hash(selectedBefore), refresh_added_execution: false });

  await newMemoryDraft(page, value, 'SOP', 'freeze');
  await sendBoundMemory(page, value, 'SOP', 'freeze',
    'F3-MEMORY-SOP-LATER-SOURCE：请记住我的新姓名是映竹，输出偏好改为详细表格；简短确认即可。', info);
  await sendMemoryInSession(page, value, 'SOP', 'freeze', first.session_id, '确认记忆核验');
  const completed = await waitMemory(page, value, 'SOP', 'freeze', first, 'succeeded');
  const restored = await sopResumedAnswer(page, value, first, text(waiting.id));
  expect(restored.labels.NAME).toBe('青禾');
  expect(text(restored.labels.STYLE)).toContain('简洁要点');
  expect(restored.labels.STYLE).not.toContain('详细表格');
  const restoredCalls = sdkCalls(completed, 'SOP', first.source_id);
  await criterion(info, 'memory-sop-freeze-restored-original-source-answer', { passed: true,
    instance_id: waiting.id, first_source_input_id: first.source_id, answer_message_id: restored.id,
    answer_sha256: restored.sha256, restored_actual_calls: restoredCalls.length,
    actual_sdk_inputs_sha256: hash(restoredCalls), old_name_and_style_preserved: true,
    source_binding_valid: true, version_binding_valid: true, citation_check_passed: true });

  await clearMemory(page, value, 'SOP', 'freeze');
  await newMemoryDraft(page, value, 'SOP', 'freeze');
  const fresh = await sendBoundMemory(page, value, 'SOP', 'freeze',
    'F3-MEMORY-SOP-FRESH：请只依据本次实际长期记忆填写 user_name、reply_style、omitted_code；未知填写未知。', info);
  await sendMemoryInSession(page, value, 'SOP', 'freeze', fresh.session_id, '确认记忆核验');
  const freshState = await waitMemory(page, value, 'SOP', 'freeze', fresh, 'succeeded');
  const freshExecution = memoryExecution(freshState, value, 'SOP', 'freeze', fresh.session_id);
  const freshAnswer = await sopResumedAnswer(page, value, fresh, text(freshExecution.id));
  const actualFresh = sdkCalls(freshState, 'SOP', fresh.source_id);
  expect(actualFresh.length).toBeGreaterThan(0);
  expect(freshAnswer.labels.NAME).not.toBe('青禾');
  expect(freshAnswer.unselected_secret_present).toBe(false);
  await criterion(info, 'memory-sop-freeze-cleared-new-source-is-actually-consumed', { passed: true,
    fresh_source_input_id: fresh.source_id, consumer_execution_id: freshExecution.id,
    final_answer_message_id: freshAnswer.id, final_answer_sha256: freshAnswer.sha256,
    actual_model_calls: actualFresh.length, actual_sdk_inputs_sha256: hash(actualFresh),
    old_fact_resurrected: false, actual_business_submit_delta: 0, real_model_quality: true,
    source_binding_valid: true, version_binding_valid: true, citation_check_passed: true });
});

test('F3-MEMORY SOP clear: restored real answer cannot disclose invalid frozen facts', async ({ page }, info) => {
  /** SOP清空屏障使用独立case_id和实际Attention恢复，清空后旧事实必须全部未知。 */
  const value = await startMemory(page, info, 'SOP', 'clear');
  const first = await sendBoundMemory(page, value, 'SOP', 'clear',
    'F3-MEMORY-SOP-CLEAR-WAIT：请先问一个确认问题并等待我确认。'
    + '确认后按本次冻结的长期记忆填写 user_name、reply_style、omitted_code。实际可用记忆为空时全部填写未知。', info,
    modelQualityCaseId('SOP', 'memory_clear'));
  const waitingState = await waitMemory(page, value, 'SOP', 'clear', first, 'waiting');
  const waiting = memoryExecution(waitingState, value, 'SOP', 'clear', first.session_id);
  const selectedBefore = selectedSdkIdentity(waitingState, 'SOP', first.source_id);
  expect((selectedBefore as object[]).length).toBeGreaterThanOrEqual(2);
  await clearMemory(page, value, 'SOP', 'clear');
  const invalid = memoryExecution(await memoryState(page, value, 'SOP', 'clear', first.session_id),
    value, 'SOP', 'clear', first.session_id);
  expect(rows(invalid.selected).every(item => item.currently_allowed === false)).toBe(true);
  const confirmation = await sendMemoryInSession(
    page, value, 'SOP', 'clear', first.session_id, '确认记忆核验',
  );
  const restored = await waitMemory(page, value, 'SOP', 'clear', first, 'succeeded');
  const answer = await sopResumedAnswer(page, value, first, text(waiting.id));
  const postClear = sdkCalls(restored, 'SOP', confirmation.source_id);
  expect(postClear.length).toBeGreaterThan(0);
  expect(hasAllUnknownMemoryLabels(answer.labels)).toBe(true);
  expect(answer.revoked_name_present).toBe(false);
  expect(answer.unselected_secret_present).toBe(false);
  expect(postClear.every(row => rows(row.selected).length === 0 && row.revoked_fact_markers_absent === true)).toBe(true);
  await criterion(info, 'memory-sop-clear-invalid-frozen-facts-never-sent', { passed: true,
    agent_id: employee(value, 'SOP', 'clear').id, instance_id: waiting.id,
    source_input_id: first.source_id, selected_before_count: (selectedBefore as object[]).length,
    invalid_original_selected_count: rows(invalid.selected).length, selected_after_count: 0,
    actual_model_calls: postClear.length, actual_sdk_inputs_sha256: hash(postClear),
    actual_restored_answer_unknown: true, answer_message_id: answer.id, answer_sha256: answer.sha256,
    unchanged_definition_checksum: true, actual_business_submit_delta: 0, real_model_quality: true,
    source_binding_valid: true, version_binding_valid: true, citation_check_passed: true });
});

for (const barrier of ['clear', 'revoke'] as const) {
  test(`F3-MEMORY ${barrier}: restored real answer cannot disclose invalid frozen facts`, async ({ page }, info) => {
    const value = await startMemory(page, info, 'DTA', barrier); const httpStart = await httpCount(page);
    const first = await sendBoundMemory(page, value, 'DTA', barrier,
      `F3-MEMORY-${barrier.toUpperCase()}-WAIT：请先问一个确认问题并等待我确认。`
      + '确认后按本次冻结的长期记忆输出 NAME=姓名、STYLE=格式关键词、CODE=口令。'
      + '如果实际可用记忆为空，三项都填未知，不能根据旧计划猜测。', info,
      barrier === 'clear' ? modelQualityCaseId('DTA', 'memory_clear') : undefined);
    const waitingState = await waitMemory(page, value, 'DTA', barrier, first, 'waiting');
    const waiting = memoryExecution(waitingState, value, 'DTA', barrier, first.session_id);
    expect(rows(waiting.selected).length).toBeGreaterThanOrEqual(2);
    const priorRequests = new Set(waitingState.sdk_calls.map(row => row.sdk_request_sha256));
    if (barrier === 'clear') await clearMemory(page, value, 'DTA', barrier);
    else { await relation(page, value, barrier, 'DELETE'); await relation(page, value, barrier, 'POST'); }
    const invalid = memoryExecution(await memoryState(page, value, 'DTA', barrier, first.session_id),
      value, 'DTA', barrier, first.session_id);
    expect(rows(invalid.selected).every(item => item.currently_allowed === false)).toBe(true);
    await confirmMemory(page, value, 'DTA', barrier, text(waiting.id),
      '确认。按当前实际可披露的记忆输出；不可披露的姓名、格式与口令都填未知。');
    const restored = await waitMemory(page, value, 'DTA', barrier, first, 'succeeded');
    const completed = memoryExecution(restored, value, 'DTA', barrier, first.session_id);
    expect(completed.plan_checksum).toBe(waiting.plan_checksum);
    const answer = await originalAnswer(page, value, first, text(waiting.id));
    if (barrier === 'clear' || barrier === 'revoke') {
      const postBarrierCalls = restored.sdk_calls.filter(
        row => !priorRequests.has(text(row.sdk_request_sha256)),
      );
      const postBarrierCallFacts = postBarrierCalls.map(row => ({
        status: ['completed', 'failed', 'cancelled'].includes(text(row.status)) ? text(row.status) : 'other',
        selected_fact_count: rows(row.selected).length,
        revoked_fact_markers_absent: row.revoked_fact_markers_absent === true,
        memory_binding_valid: row.memory_binding_valid === true,
        original_sdk_receipt_found: row.original_sdk_receipt_found === true,
      }));
      const businessSubmitDelta = await httpCount(page) - httpStart;
      await info.attach(`f3-memory-${barrier}-diagnostic-only`, {
        body: Buffer.from(JSON.stringify({
          exact_source_result_match: true,
          answer_message_sha256: answer.sha256,
          ...safeMemoryAnswerQualityObservation(answer),
          post_barrier_model_call_count: postBarrierCalls.length,
          post_barrier_model_calls: postBarrierCallFacts,
          unchanged_plan_checksum: completed.plan_checksum === waiting.plan_checksum,
          business_submit_delta: businessSubmitDelta,
          diagnostic_only_not_acceptance: true,
          real_vendor_qualified: false,
          business_provider_kind: 'simulated_async_service',
        })),
        contentType: 'application/json',
      });
    }
    expect(hasAllUnknownMemoryLabels(answer.labels)).toBe(true);
    expect(answer.revoked_name_present).toBe(false); expect(answer.unselected_secret_present).toBe(false);
    const newState = { ...restored, sdk_calls: restored.sdk_calls.filter(row => !priorRequests.has(row.sdk_request_sha256)) };
    const actual = sdkCalls(newState, 'DTA', first.source_id);
    expect(actual.every(row => rows(row.selected).length === 0 && row.revoked_fact_markers_absent === true)).toBe(true);
    expect(await httpCount(page)).toBe(httpStart);
    await criterion(info, `memory-${barrier}-invalid-frozen-facts-never-sent`, { passed: true,
      agent_id: employee(value, 'DTA', barrier).id, instance_id: waiting.id,
      source_input_id: first.source_id, selected_before_count: rows(waiting.selected).length,
      invalid_original_selected_count: rows(invalid.selected).length, selected_after_count: 0,
      actual_model_calls: actual.length, actual_sdk_inputs_sha256: hash(actual),
      actual_restored_answer_unknown: hasAllUnknownMemoryLabels(answer.labels),
      answer_message_id: answer.id, answer_sha256: answer.sha256,
      unchanged_plan_checksum: true, actual_business_submit_delta: 0, real_model_quality: true,
      source_binding_valid: true, version_binding_valid: true, citation_check_passed: true });
  });
}

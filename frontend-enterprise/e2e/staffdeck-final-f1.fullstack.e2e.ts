/**
 * @Time       : 2026/10/03
 * @Author     : zhanglp8181
 * @File       : staffdeck-final-f1.fullstack.e2e.ts
 * @CallChain  : 真实登录/员工/Composer → 原Runtime/SDK/HTTP → 原账本与远端读回
 * @Description: 按JRN01→04逐类执行最终F1，各方言独立完整旅程，不拼阶段。
 */

import { expect, test, type Page, type TestInfo } from '@playwright/test';

import { criterion, facts, login, object, openCitation, request, send, waiting, type Row } from './final-f1-browser-helpers';

async function sopRound(page: Page, info: TestInfo, round: number): Promise<Row> {
  /** 单条原SOP补槽/确认/异步/刷新/回执闭环，提交次数逐轮精确对账。 */
  const before = await facts(page);
  const prior = before.instances.map(row => String(row.id));
  const previousMessages = before.messages.map(row => row.id);
  await send(page, round === 1
    ? 'F1-SOP：请按CYGNUS77政策办理明天09:00配送，数量3，使用我记住的客户；先补商品并等待我确认。'
    : 'F1-SOP：再次新办同样参数，商品ALPHA91、数量3、客户CYGNUS77、明天09:00配送；仍先等待我确认。');
  const instance = await waiting(page, prior);
  const executionId = String(instance.id);
  expect(instance).toMatchObject({ tenant_id: 'tenant_demo', agent_id: 'agent_f1_sop', initiator_user_id: 'admin', kind: 'sop' });
  const sourceState = await facts(page);
  const source = object(sourceState.messages.find(row => row.role === 'user' && !previousMessages.includes(row.id)));
  const sourceTime = object(object(source.metadata).time_context);
  expect(sourceTime).toMatchObject({ source_input_id: source.id, anchor_at_utc: '2026-10-03T15:59:00Z',
    timezone: 'Asia/Shanghai', timezone_source: 'client', local_at: '2026-10-03T23:59:00+08:00' });
  const router = sourceState.sdk.filter(row => row.phase === 'Router' && row.source_input_id === source.id);
  expect(router).toHaveLength(1);
  expect(router[0]?.memory_complete).toEqual([true, true, true]);
  expect(router[0]?.time_context).toEqual(sourceTime);
  expect(before.provider.submits.length).toBe(round - 1);
  await expect.poll(async () => (await facts(page)).provider.submits.length).toBe(round - 1);
  if (round === 1) {
    await send(page, '商品 ALPHA91');
    await expect.poll(async () => (await facts(page)).instances.find(row => row.id === executionId)?.current_node_id,
      { timeout: 30_000 }).toBe('confirm');
  } else {
    expect(instance.current_node_id).toBe('confirm');
  }
  const unconfirmed = await facts(page);
  expect(unconfirmed.provider.submits.length).toBe(round - 1);
  const knowledge = unconfirmed.operations.filter(row => row.instance_id === executionId && row.operation_name === 'knowledge.search');
  expect(knowledge).toHaveLength(1);
  expect(knowledge[0]?.status).toBe('succeeded');
  const sources = knowledge[0]?.knowledge_sources;
  expect(Array.isArray(sources) && sources.length > 0).toBe(true);
  const citations = unconfirmed.messages.filter(row => row.session_id === instance.session_id
    && !previousMessages.includes(row.id)).flatMap(row => Array.isArray(row.citations) ? row.citations.map(object) : []);
  expect(citations.length).toBeGreaterThan(0);
  for (const citation of citations) {
    expect(citation.tenant_valid).toBe(true);
    expect(citation.version_id).toBeTruthy();
    expect((sources as Row[]).some(row => row.document_id === citation.document_id
      && row.chunk_id === citation.chunk_id && row.bucket_id === citation.bucket_id
      && row.version_id === citation.version_id && row.body_hash === citation.body_hash)).toBe(true);
  }
  const citationMessageId = String([...unconfirmed.messages].reverse().find(row => row.session_id === instance.session_id
    && !previousMessages.includes(row.id) && Array.isArray(row.citations) && row.citations.length > 0)?.id);
  await openCitation(page, info, citationMessageId);
  await criterion(info, `round-${round}-before-confirmation`, { passed: true, instance_id: executionId,
    provider_post_count: round - 1, definition_version: instance.skill_version_id,
    knowledge_operation_id: knowledge[0]?.id });
  await send(page, '确认办理');
  await expect.poll(async () => (await facts(page)).operations.some(row => row.instance_id === executionId
    && row.operation_name === 'final_f1.submit' && row.status === 'waiting_external'), { timeout: 45_000 }).toBe(true);
  const accepted = await facts(page);
  const operation = object(accepted.operations.find(row => row.instance_id === executionId && row.operation_name === 'final_f1.submit'));
  const remote = object(accepted.provider.submits.find(row => row.task_id === operation.external_task_id));
  expect(remote.post_count).toBe(1);
  expect(accepted.provider.submits.length).toBe(round);
  await page.reload();
  await expect(page.getByPlaceholder('输入消息，按 Enter 发送...')).toBeVisible();
  const refreshed = await facts(page);
  expect(refreshed.operations.find(row => row.id === operation.id)).toMatchObject({ status: 'waiting_external', external_task_id: operation.external_task_id });
  expect(refreshed.provider.submits.length).toBe(round);
  expect(refreshed.provider.submits.every(row => row.post_count === 1)).toBe(true);
  expect((await page.request.get(`/__e2e/f1/provider/receipt/${String(operation.external_task_id)}`)).status()).toBe(404);
  const complete = object(await request(page, `/__e2e/f1/provider/complete/${String(operation.external_task_id)}`, 'POST'));
  expect(complete.http_status).toBe(202);
  await expect.poll(async () => (await facts(page)).instances.find(row => row.id === executionId)?.status,
    { timeout: 60_000, intervals: [250, 500, 1000] }).toBe('succeeded');
  const finished = await facts(page);
  const receiptPage = await page.context().newPage();
  await receiptPage.goto(`/__e2e/f1/provider/receipt/${String(operation.external_task_id)}`);
  await expect(receiptPage.getByRole('heading', { name: '配送回执', exact: true })).toBeVisible();
  await expect(receiptPage.locator('#receipt-id')).toHaveText(String(remote.receipt_id));
  for (const [label, value] of [['商品', 'ALPHA91'], ['数量', '3'], ['客户', 'CYGNUS77'],
    ['目标日期', '2026-10-04 09:00']]) {
    await expect(receiptPage.getByRole('row').filter({ has: receiptPage.getByRole('rowheader', { name: label, exact: true }) }))
      .toContainText(value);
  }
  await receiptPage.close();
  expect(finished.operations.find(row => row.id === operation.id)?.result).toMatchObject({ ...object(remote.request), receipt_id: remote.receipt_id });
  expect(finished.provider.submits.every(row => row.post_count === 1)).toBe(true);
  expect(finished.provider.finishes).toHaveLength(round);
  expect(finished.provider.finishes.find(row => row.receipt_id === remote.receipt_id)).toBeTruthy();
  expect(object(remote.request)).toMatchObject({ product_name: 'ALPHA91', quantity: 3,
    customer: 'CYGNUS77', due_date: '2026-10-04 09:00' });
  const signal = finished.signals.filter(row => row.execution_id === executionId && row.signal_type === 'external_event');
  expect(signal).toHaveLength(1);
  expect(signal[0]?.status).toBe('consumed');
  expect(signal[0]?.consumed_at).toBeTruthy();
  const callback = finished.inbox.filter(row => row.operation_id === operation.id
    && row.provider_event_id === `event-${String(operation.external_task_id)}`);
  expect(callback).toHaveLength(1);
  expect(callback[0]).toMatchObject({ status: 'applied', signal_id: signal[0]?.id });
  expect(signal[0]?.causation_id).toBe(callback[0]?.id);
  expect(operation.logical_action_id).toBeTruthy();
  expect(operation.request_fingerprint).toBeTruthy();
  expect(operation.external_submission_id).toBeTruthy();
  expect(finished.attempts.filter(row => row.operation_id === operation.id)).toHaveLength(1);
  expect(remote.callback_header_received).toBe(true);
  const followup = object(finished.operations.find(row => row.instance_id === executionId && row.operation_name === 'final_f1.finish'));
  expect(finished.events.filter(row => row.event_type === 'tool_call_started' && row.source === 'external_task_resume'
    && row.idempotency_key_hash === followup.idempotency_key_hash)).toHaveLength(1);
  expect(finished.instances.find(row => row.id === executionId)?.skill_version_id).toBe(instance.skill_version_id);
  expect(finished.instances.find(row => row.id === executionId)?.definition_checksum).toBe(instance.definition_checksum);
  const frozen = object(object(accepted.instances.find(row => row.id === executionId)).metadata).time_context;
  expect(object(object(finished.instances.find(row => row.id === executionId)).metadata).time_context).toEqual(frozen);
  await page.reload();
  await expect(page.getByPlaceholder('输入消息，按 Enter 发送...')).toBeVisible();
  await openCitation(page, info, citationMessageId);
  await criterion(info, `round-${round}-complete`, { passed: true, instance_id: executionId,
    operation_id: operation.id, provider_task_id: operation.external_task_id,
    receipt_id: remote.receipt_id, signal_id: signal[0]?.id,
    provider_post_count: round, provider_finish_count: round,
    same_version: finished.instances.find(row => row.id === executionId)?.skill_version_id === instance.skill_version_id,
    ledger: finished });
  return { instance_id: executionId, operation_id: operation.id, provider_task_id: operation.external_task_id,
    receipt_id: remote.receipt_id, request_hash: operation.request_hash };
}

test('JRN01 SOP真实补槽确认、异步刷新与同参数新办理', async ({ page }, info) => {
  test.setTimeout(300_000);
  await login(page, 'SOP');
  const initial = await facts(page);
  expect(initial.instances).toHaveLength(0);
  expect(initial.provider.submits).toHaveLength(0);
  await criterion(info, 'initial', { passed: true, real_provider_quality: false, initial });
  const first = await sopRound(page, info, 1);
  const second = await sopRound(page, info, 2);
  expect(second.instance_id).not.toBe(first.instance_id);
  expect(second.operation_id).not.toBe(first.operation_id);
  expect(second.provider_task_id).not.toBe(first.provider_task_id);
  expect(second.receipt_id).not.toBe(first.receipt_id);
  expect(second.request_hash).toBe(first.request_hash);
  const final = await facts(page);
  expect(final.memories.filter(row => row.complete_initial_fact)).toHaveLength(6);
  expect(final.provider.submits).toHaveLength(2);
  expect(final.provider.finishes).toHaveLength(2);
  await criterion(info, 'final-ledger', { passed: true, first, second, final });
});

test.afterEach(async ({ page }, info) => {
  /** 失败也尽力保留同轮原账本；旁证不得吞掉或改变原业务失败。 */
  try {
    await criterion(info, 'terminal-observation', { passed: info.status === 'passed', final: await facts(page) });
  } catch (error) {
    await criterion(info, 'terminal-observation-unavailable', { passed: false,
      error_type: error instanceof Error ? error.name : 'unknown' });
  }
});

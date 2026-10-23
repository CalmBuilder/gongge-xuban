/**
 * @Time       : 2026/10/04
 * @Author     : zhanglp8181
 * @File       : staffdeck-final-f1-jrn02.fullstack.e2e.ts
 * @CallChain  : Composer/工作项/批准 → 原DTA消费者 → verified成果/回执读回
 * @Description: JRN02全程核原冻结身份、新信息守卫、真实知识来源与独立业务效果。
 */
import { expect, test } from '@playwright/test';
import { criterion, facts, object, openCitation, request } from './final-f1-browser-helpers';
import { approveDynamic, pendingAttention, sendDynamicInput, startWaitingDynamic } from './final-f1-dta-journey';

test.setTimeout(300_000);

test('JRN02 普通DTA真实规划澄清批准、合法新信息及验证成果', async ({ page, browser }, info) => {
  let active = page;
  let completed = false;
  try {
    const started = await startWaitingDynamic(active, browser, info);
    active = started.page;
    const { instance, operation, remote } = started;
    await sendDynamicInput(active, '等待中补充合法备注：请完整保留原始回执；已批准的商品、数量、客户和日期均不变。');
    await expect.poll(async () => (await facts(active)).events.filter(row =>
      row.event_type === 'dynamic_task_chat_turn_deferred').length, { timeout: 30_000 }).toBe(1);
    const noted = await facts(active);
    expect(noted.instances.filter(row => row.kind === 'dynamic_task')).toHaveLength(1);
    expect(noted.instances.find(row => row.id === instance.id)?.plan_revision_id).toBe(instance.plan_revision_id);
    expect(noted.instances.find(row => row.id === instance.id)?.plan_checksum).toBe(instance.plan_checksum);
    expect(noted.operations.find(row => row.id === operation.id)).toMatchObject({ request_hash: operation.request_hash,
      logical_action_id: operation.logical_action_id, request_fingerprint: operation.request_fingerprint,
      external_task_id: operation.external_task_id, status: 'waiting_external' });
    expect(noted.events.filter(row => row.event_type === 'dynamic_task_chat_turn_deferred')).toHaveLength(1);
    await active.reload();
    await expect(active.getByPlaceholder('输入消息，按 Enter 发送...')).toBeVisible();
    expect((await facts(active)).provider.submits).toHaveLength(1);
    expect((await active.request.get(`/__e2e/f1/provider/receipt/${String(operation.external_task_id)}`)).status()).toBe(404);
    await criterion(info, 'dynamic-new-information-and-refresh', { passed: true, ledger: await facts(active) });
    expect(object(await request(active, `/__e2e/f1/provider/complete/${String(operation.external_task_id)}`, 'POST')).http_status).toBe(202);
    const finishApproval = await pendingAttention(active, String(instance.id), 'tool_approval');
    expect((await facts(active)).provider.finishes).toHaveLength(0);
    active = await approveDynamic(active, browser, info, finishApproval, [String(remote.receipt_id)]);
    await expect.poll(async () => (await facts(active)).instances.find(row => row.id === instance.id)?.status,
      { timeout: 60_000 }).toBe('succeeded');
    const final = await facts(active);
    expect(final.provider.submits).toHaveLength(1);
    expect(final.provider.finishes).toHaveLength(1);
    expect(final.provider.finishes[0]?.receipt_id).toBe(remote.receipt_id);
    expect(final.instances.find(row => row.id === instance.id)).toMatchObject({ plan_revision_id: instance.plan_revision_id,
      plan_checksum: instance.plan_checksum, capability_checksum: instance.capability_checksum });
    const signal = final.signals.filter(row => row.execution_id === instance.id && row.signal_type === 'external_event');
    expect(signal).toHaveLength(1);
    expect(signal[0]).toMatchObject({ status: 'consumed' });
    const result = final.results.filter(row => row.execution_id === instance.id);
    expect(result).toHaveLength(1);
    expect(result[0]).toMatchObject({ status: 'verified', verification_passed: true, contains_current_receipt: true });
    expect(Object.keys(object(result[0]?.criterion_evidence)).length).toBeGreaterThan(0);
    for (const refs of Object.values(object(result[0]?.criterion_evidence))) {
      expect(Array.isArray(refs) && refs.length > 0).toBe(true);
      expect((refs as string[]).every(key => final.nodes.some(row => row.instance_id === instance.id
        && row.node_id === key && row.status === 'succeeded'))).toBe(true);
    }
    const answer = object(final.messages.find(row => row.session_id === instance.session_id && row.role === 'assistant'
      && object(row.metadata).result_id === result[0]?.id));
    expect(answer.citations).toBeTruthy();
    const knowledge = final.operations.filter(row => row.instance_id === instance.id && row.operation_name === 'knowledge.search');
    expect(knowledge).toHaveLength(1);
    expect(knowledge[0]?.status).toBe('succeeded');
    await active.reload();
    await expect(active.getByText(String(remote.receipt_id), { exact: false }).last()).toBeVisible();
    await openCitation(active, info, String(answer.id));
    const receipt = await active.context().newPage();
    await receipt.goto(`/__e2e/f1/provider/receipt/${String(operation.external_task_id)}`);
    for (const text of ['ALPHA91', 'CYGNUS77', '2026-10-04 09:00']) await expect(receipt.getByRole('table')).toContainText(text);
    await expect(receipt.locator('#receipt-id')).toHaveText(String(remote.receipt_id));
    await receipt.close();
    const actions = final.sdk.filter(row => row.phase === 'Dynamic Action' && row.source_input_id
      === object(object(instance.metadata).time_context).source_input_id);
    expect(actions.length).toBeGreaterThanOrEqual(5);
    expect(actions.every(row => JSON.stringify(row.memory_complete) === '[true,true,true]')).toBe(true);
    expect(final.memories.filter(row => row.complete_initial_fact)).toHaveLength(6);
    await criterion(info, 'dynamic-final-ledger', { passed: true, instance_id: instance.id, result_id: result[0]?.id,
      message_id: answer.id, operation_id: operation.id, provider_task_id: operation.external_task_id, ledger: final });
    completed = true;
  } finally {
    if (!active.isClosed()) {
      try {
        await criterion(info, 'terminal-observation', { passed: completed, final: await facts(active) });
      } catch (error) {
        await criterion(info, 'terminal-observation-unavailable', { passed: false,
          error_type: error instanceof Error ? error.name : 'unknown' });
      }
    }
  }
});

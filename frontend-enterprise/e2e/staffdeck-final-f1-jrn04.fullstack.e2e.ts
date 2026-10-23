/**
 * @Time       : 2026/10/04
 * @Author     : zhanglp8181
 * @File       : staffdeck-final-f1-jrn04.fullstack.e2e.ts
 * @CallChain  : 两分支Composer/批准/等待 → UI取消或员工下线 → 迟到与重复回调 → 原审计
 * @Description: 每种关闭方式独立完整执行，记录未决远端效果，不复活或再派发后继写。
 */
import { expect, test, type Page, type TestInfo } from '@playwright/test';
import { criterion, facts, object, request, type Facts, type Row } from './final-f1-browser-helpers';
import { startWaitingDynamic } from './final-f1-dta-journey';
import { startWaitingSop } from './final-f1-sop-journey';

type Mode = 'cancel' | 'archive';

test.setTimeout(300_000);

async function closeWaiting(page: Page, info: TestInfo, kind: 'SOP' | 'DTA', mode: Mode,
  executionId: string): Promise<Facts> {
  /** 两种关闭均由真实页面执行；取消只用原revision命令，下线只用原员工资料入口。 */
  const before = await facts(page);
  const instance = object(before.instances.find(row => row.id === executionId));
  let issued: Row | undefined;
  if (mode === 'cancel') {
    // DTA批准后恢复页面时HTTP仍可在途；重新经原GET读取已持久waiting的当前revision。
    const loaded = page.waitForResponse(row => new URL(row.url()).pathname === `/api/executions/${executionId}`
      && row.request().method() === 'GET' && row.status() === 200);
    await page.reload();
    const authoritative = object(await (await loaded).json());
    expect(authoritative.status).toBe('waiting');
    const card = page.getByLabel(kind === 'SOP' ? 'SOP执行结果' : '动态任务控制', { exact: true }).last();
    const cancel = card.getByRole('button', { name: '取消任务', exact: true });
    await expect(cancel).toBeEnabled();
    const response = page.waitForResponse(row => new URL(row.url()).pathname
      === `/api/executions/${executionId}/commands` && row.request().method() === 'POST');
    await cancel.click();
    const received = await response;
    expect(received.status()).toBe(200);
    issued = object(received.request().postDataJSON());
    expect(issued.command_type).toBe('cancel');
    // 同一原执行在多条消息中各有一张卡；按本旅程刷新合同读取服务端最新取消命令。
    await page.reload();
    await expect(card.getByRole('status')).toHaveText('取消已提交，等待效果对账');
    await expect(cancel).toBeDisabled();
  } else {
    await page.goto('/enterprise/agents');
    const card = page.locator('.gongge-employee-card').filter({ hasText: `F1 ${kind}员工` });
    await expect(card).toHaveCount(1);
    await card.getByRole('button', { name: '员工操作', exact: true }).click();
    const response = page.waitForResponse(row => new URL(row.url()).pathname
      === `/api/enterprise/agents/${String(instance.agent_id)}` && row.request().method() === 'PUT');
    await page.getByRole('menuitem', { name: '下线', exact: true }).click();
    const received = await response;
    expect(received.status()).toBe(200);
    expect(object(received.request().postDataJSON()).status).toBe('archived');
    await expect.poll(async () => (await facts(page)).agents.find(row => row.id === instance.agent_id)?.status)
      .toBe('archived');
  }
  const closed = await facts(page);
  const current = object(closed.instances.find(row => row.id === instance.id));
  const operation = object(closed.operations.find(row => row.instance_id === instance.id
    && String(row.operation_name).split('@')[0] === 'final_f1.submit'));
  if (mode === 'cancel') {
    expect(operation).toMatchObject({ status: 'unknown', effect_state: 'unknown' });
    expect(current).toMatchObject({ status: 'waiting', cancellation_disposition: 'awaiting_reconciliation',
      effect_state: 'unknown', cancellation_requested_by: 'admin' });
    expect(current.cancellation_requested_at).toBeTruthy();
    const commands = closed.commands.filter(row => row.execution_id === instance.id && row.command_type === 'cancel');
    expect(commands).toHaveLength(1);
    expect(commands[0]).toMatchObject({ status: 'applied', actor_user_id: 'admin', result_cancelled: false,
      command_id: issued?.command_id, expected_revision: issued?.expected_revision });
    expect(commands[0]?.consumed_at).toBeTruthy();
    const audit = closed.events.filter(row => row.aggregate_id === instance.id && row.event_type === 'execution_cancelling');
    expect(audit).toHaveLength(1);
    expect(audit[0]).toMatchObject({ causation_id: commands[0]?.id, command_id: commands[0]?.command_id,
      cancellation_disposition: 'awaiting_reconciliation', effect_state: 'unknown' });
  } else {
    const old = object(before.agents.find(row => row.id === instance.agent_id));
    const agent = object(closed.agents.find(row => row.id === instance.agent_id));
    expect(agent).toMatchObject({ status: 'archived', profile_revision: Number(old.profile_revision) + 1,
      memory_capture_generation: Number(old.memory_capture_generation) + 1 });
    expect(operation.status).toBe('waiting_external');
    expect(current.status).toBe('waiting');
    expect(current.cancellation_disposition).toBe('none');
    expect(closed.commands.filter(row => row.execution_id === instance.id && row.command_type === 'cancel')).toHaveLength(0);
  }
  await criterion(info, `${kind}-${mode}-closed`, { passed: true, execution_id: instance.id,
    operation_id: operation.id, original: before, closed, issued_command_id: issued?.command_id ?? null,
    issued_expected_revision: issued?.expected_revision ?? null });
  return closed;
}

function assertHeld(before: Facts, after: Facts, executionId: string, operationId: string): void {
  /** 原关闭后账本不能变成成功、换版本/计划/时间、再派发或覆盖取消请求。 */
  const original = object(before.instances.find(row => row.id === executionId));
  const current = object(after.instances.find(row => row.id === executionId));
  for (const key of ['status', 'skill_version_id', 'definition_checksum', 'plan_revision_id',
    'plan_checksum', 'capability_checksum', 'metadata', 'cancellation_disposition',
    'cancellation_requested_at', 'cancellation_requested_by', 'effect_state']) {
    expect(current[key]).toEqual(original[key]);
  }
  const op = object(after.operations.find(row => row.id === operationId));
  const old = object(before.operations.find(row => row.id === operationId));
  for (const key of ['status', 'request', 'request_hash', 'request_fingerprint', 'external_task_id',
    'external_submission_id', 'logical_action_id', 'effect_state', 'result_hash']) expect(op[key]).toEqual(old[key]);
  expect(after.operations.filter(row => row.instance_id === executionId && String(row.operation_name).split('@')[0] === 'final_f1.finish'))
    .toHaveLength(0);
  expect(after.provider.submits).toHaveLength(before.provider.submits.length);
  expect(after.provider.submits.find(row => row.task_id === op.external_task_id)?.post_count).toBe(1);
  expect(after.provider.finishes).toEqual(before.provider.finishes);
  expect(after.attempts.filter(row => row.operation_id === operationId)).toHaveLength(1);
  expect(after.signals.filter(row => row.execution_id === executionId && row.signal_type === 'external_event')).toHaveLength(0);
  expect(after.results.filter(row => row.execution_id === executionId)).toHaveLength(0);
  expect(after.messages.filter(row => row.session_id === current.session_id).map(row => row.id))
    .toEqual(before.messages.filter(row => row.session_id === original.session_id).map(row => row.id));
  const source = object(object(original.metadata).time_context).source_input_id;
  expect(after.sdk.filter(row => row.source_input_id === source && row.phase !== 'Memory Capture').map(row => row.request_hash))
    .toEqual(before.sdk.filter(row => row.source_input_id === source && row.phase !== 'Memory Capture').map(row => row.request_hash));
}

for (const kind of ['SOP', 'DTA'] as const) {
  for (const mode of ['cancel', 'archive'] as const) {
    test(`JRN04 ${kind}等待后${mode}、迟到成功和重复回调保持关闭屏障`, async ({ page, browser }, info) => {
      let active = page;
      let completed = false;
      try {
        const started = kind === 'SOP' ? await startWaitingSop(active, info) : await startWaitingDynamic(active, browser, info);
        active = started.page;
        const { instance, operation, remote } = started;
        const executionId = String(instance.id);
        const closed = await closeWaiting(active, info, kind, mode, executionId);
        const path = `/__e2e/f1/provider/complete/${String(operation.external_task_id)}`;
        expect((await active.request.get(`/__e2e/f1/provider/receipt/${String(operation.external_task_id)}`)).status()).toBe(404);
        expect(object(await request(active, path, 'POST'))).toMatchObject({ http_status: 202, remote_effect_completed: true });
        const reason = mode === 'cancel' ? 'EXTERNAL_OPERATION_ALREADY_TERMINAL' : 'EXTERNAL_AGENT_UNAVAILABLE';
        await expect.poll(async () => (await facts(active)).inbox.find(row => row.operation_id === operation.id
          && row.provider_event_id === `event-${String(operation.external_task_id)}`)?.status,
        { timeout: 45_000 }).toBe('ignored');
        const first = await facts(active);
        expect(object(await request(active, path, 'POST'))).toMatchObject({ http_status: 202, remote_effect_completed: true });
        const duplicate = await facts(active);
        const event = duplicate.inbox.filter(row => row.operation_id === operation.id
          && row.provider_event_id === `event-${String(operation.external_task_id)}`);
        expect(event).toHaveLength(1);
        expect(event[0]).toMatchObject({ status: 'ignored', signal_id: null, error_code: reason,
          provider_task_id: operation.external_task_id, external_submission_id: operation.external_submission_id });
        const oldEvent = first.inbox.find(row => row.id === event[0]?.id);
        expect(event[0]).toEqual(oldEvent);
        expect(duplicate.provider.submits.find(row => row.task_id === operation.external_task_id))
          .toMatchObject({ post_count: 1, status: 'completed', effect_completed: true, receipt_id: remote.receipt_id,
            request: operation.request });
        assertHeld(closed, duplicate, executionId, String(operation.id));
        await criterion(info, `${kind}-${mode}-late-duplicate`, { passed: true, execution_id: instance.id,
          operation_id: operation.id, ignored_inbox_id: event[0]?.id, reason_code: reason, ledger: duplicate });
        await active.goto(`/workspace/chat/${String(instance.session_id)}`);
        await expect(active.getByPlaceholder('输入消息，按 Enter 发送...')).toBeVisible();
        await active.reload();
        if (mode === 'cancel') {
          const card = active.getByLabel(kind === 'SOP' ? 'SOP执行结果' : '动态任务控制', { exact: true }).last();
          await expect(card.getByRole('status')).toHaveText('取消已提交，等待效果对账');
          await expect(card.getByRole('button', { name: '取消任务', exact: true })).toBeDisabled();
        }
        const workResponse = active.waitForResponse(row => new URL(row.url()).pathname === '/api/attention-items'
          && row.request().method() === 'GET');
        await active.goto('/enterprise/work-items');
        expect((await workResponse).status()).toBe(200);
        const workReload = active.waitForResponse(row => new URL(row.url()).pathname === '/api/attention-items'
          && row.request().method() === 'GET');
        await active.reload();
        const listed = object(await (await workReload).json());
        const listedItems = Array.isArray(listed.items) ? listed.items.map(object) : [];
        const reviewed = await facts(active);
        if (mode === 'cancel') {
          const pending = reviewed.work_items.filter(row => row.instance_id === instance.id
            && row.attention_kind === 'exception' && ['offered', 'claimed'].includes(String(row.status)));
          expect(pending).toHaveLength(1);
          const button = active.getByRole('button').filter({ has: active.getByText(String(pending[0]?.title), { exact: true }) });
          const sameTitle = listedItems.filter(row => row.title === pending[0]?.title);
          const index = sameTitle.findIndex(row => row.id === pending[0]?.id);
          expect(index).toBeGreaterThanOrEqual(0);
          await expect(button).toHaveCount(sameTitle.length);
          await button.nth(index).click();
          const dialog = active.getByRole('dialog');
          await expect(dialog).toContainText(String(instance.id));
          await dialog.getByRole('button', { name: 'Close', exact: true }).click();
        }
        assertHeld(closed, reviewed, executionId, String(operation.id));
        // 单页暂离本地管理界面，直接打开独立provider真实已发生效果的只读回执。
        await active.goto(`/__e2e/f1/provider/receipt/${String(operation.external_task_id)}`);
        await expect(active.locator('#receipt-id')).toHaveText(String(remote.receipt_id));
        for (const value of ['ALPHA91', 'CYGNUS77', '2026-10-04 09:00']) await expect(active.getByRole('table')).toContainText(value);
        await expect(active.getByRole('table')).toContainText('3');
        await active.goto(`/workspace/chat/${String(instance.session_id)}`);
        const final = await facts(active);
        assertHeld(closed, final, executionId, String(operation.id));
        await criterion(info, `${kind}-${mode}-refresh-and-audit`, { passed: true, execution_id: instance.id,
          operation_id: operation.id, original_session_refreshed: true, work_items_refreshed: true,
          independent_remote_receipt_opened: true, ledger: final });
        completed = true;
      } finally {
        if (!active.isClosed()) {
          try { await criterion(info, `${kind}-${mode}-terminal-observation`, { passed: completed, final: await facts(active) }); }
          catch (error) { await criterion(info, `${kind}-${mode}-terminal-unavailable`, { passed: false,
            error_type: error instanceof Error ? error.name : 'unknown' }); }
        }
      }
    });
  }
}

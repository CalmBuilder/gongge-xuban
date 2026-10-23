/**
 * @Time       : 2026/10/04
 * @Author     : zhanglp8181
 * @File       : final-f1-sop-journey.ts
 * @CallChain  : 真实Composer/本人确认 → 原SOP/HTTP → 原waiting_external
 * @Description: 复用等待前半程，核当前版本、输入时间、确认屏障和唯一提交。
 */
import { expect, type Page, type TestInfo } from '@playwright/test';
import { criterion, facts, login, object, send, waiting, type Row } from './final-f1-browser-helpers';

export async function startWaitingSop(page: Page, info: TestInfo): Promise<{
  page: Page; instance: Row; operation: Row; remote: Row;
}> {
  /** 原发布SOP经真实本人确认进入异步等待，不预写Execution或远端成功。 */
  await login(page, 'SOP');
  const before = await facts(page);
  await send(page, 'F1-SOP：商品ALPHA91、数量3、明天09:00配送，使用我记住的客户；先查政策并等待我确认办理。');
  const instance = await waiting(page, before.instances.map(row => String(row.id)));
  expect(instance.current_node_id).toBe('confirm');
  expect(instance).toMatchObject({ kind: 'sop', tenant_id: 'tenant_demo', agent_id: 'agent_f1_sop', initiator_user_id: 'admin' });
  expect(instance.skill_version_id).toBeTruthy();
  expect(instance.definition_checksum).toBeTruthy();
  const pending = await facts(page);
  expect(pending.provider.submits).toHaveLength(before.provider.submits.length);
  const source = object(pending.messages.find(row => row.role === 'user' && !before.messages.some(old => old.id === row.id)));
  const time = object(object(source.metadata).time_context);
  expect(time).toMatchObject({ source_input_id: source.id, anchor_at_utc: '2026-10-03T15:59:00Z', timezone: 'Asia/Shanghai' });
  const router = pending.sdk.filter(row => row.phase === 'Router' && row.source_input_id === source.id);
  expect(router).toHaveLength(1);
  expect(router[0]?.memory_complete).toEqual([true, true, true]);
  expect(router[0]?.time_context).toEqual(time);
  await criterion(info, 'sop-before-confirmation', { passed: true, instance, source_input_id: source.id,
    provider_post_count: before.provider.submits.length });
  await send(page, '确认办理');
  await expect.poll(async () => (await facts(page)).operations.find(row => row.instance_id === instance.id
    && row.operation_name === 'final_f1.submit')?.status, { timeout: 45_000 }).toBe('waiting_external');
  const accepted = await facts(page);
  const current = object(accepted.instances.find(row => row.id === instance.id));
  const operation = object(accepted.operations.find(row => row.instance_id === instance.id && row.operation_name === 'final_f1.submit'));
  const remote = object(accepted.provider.submits.find(row => row.task_id === operation.external_task_id));
  expect(accepted.provider.submits).toHaveLength(before.provider.submits.length + 1);
  expect(remote).toMatchObject({ post_count: 1, effect_completed: false, callback_header_received: true });
  expect(operation.request).toEqual({ product_name: 'ALPHA91', quantity: 3, customer: 'CYGNUS77', due_date: '2026-10-04 09:00' });
  await criterion(info, 'sop-waiting-external', { passed: true, instance: current, operation, remote, ledger: accepted });
  return { page, instance: current, operation, remote };
}

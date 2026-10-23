/**
 * @Time       : 2026/10/03
 * @Author     : zhanglp8181
 * @File       : sd8-safe-observation.test.ts
 * @CallChain  : 无模型 Vitest → 页面断言/旁证故障 → 原异常身份
 * @Description: 验证诊断故障不遮盖失败，成功时必须有完整旁证。
 */
import { expect, it, vi } from 'vitest';
import { observeAssertion } from './sd8-safe-observation';

it('旁证失败保留原断言异常对象，不能换成诊断错误', async () => {
  const original = new Error('original-assertion');
  const assertion = vi.fn(async () => { throw original; });
  const observation = vi.fn(async () => { throw new Error('observation-failure'); });
  await expect(observeAssertion(assertion, observation)).rejects.toBe(original);
  expect(assertion).toHaveBeenCalledTimes(1);
  expect(observation).toHaveBeenCalledTimes(1);
});

it('旁证正常时同样保留原非Error失败对象', async () => {
  const original = { failed: true };
  await expect(observeAssertion(async () => { throw original; }, async () => {}))
    .rejects.toBe(original);
});

it('成功原结果对象保持，旁证仅执行一次', async () => {
  const original = { succeeded: true };
  const observation = vi.fn(async () => {});
  await expect(observeAssertion(async () => original, observation)).resolves.toBe(original);
  expect(observation).toHaveBeenCalledTimes(1);
});

it('原断言成功而旁证不完整时仍不能报告整程成功', async () => {
  const incomplete = new Error('observation-incomplete');
  await expect(observeAssertion(async () => true, async () => { throw incomplete; }))
    .rejects.toBe(incomplete);
});

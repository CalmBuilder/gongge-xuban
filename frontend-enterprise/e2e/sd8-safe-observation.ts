/**
 * @Time       : 2026/10/03
 * @Author     : zhanglp8181
 * @File       : sd8-safe-observation.ts
 * @CallChain  : SD8 页面原断言 → 只读安全旁证 → 原始成功/失败归属
 * @Description: 隔离诊断与业务断言，避免旁证异常替换原页面失败。
 */
export async function observeAssertion<T>(
  assertion: () => Promise<T>,
  observation: () => Promise<void>,
): Promise<T> {
  /** 原断言只执行一次，旁证按原始结果追加且不改变返回对象。 */
  let result: T;
  try {
    result = await assertion();
  } catch (original) {
    try {
      await observation();
    } catch {
      // 原失败已有权威归属；缺旁证不得替换该对象或重跑断言。
    }
    throw original;
  }
  await observation();
  return result;
}

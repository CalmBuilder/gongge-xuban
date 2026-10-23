/**
 * @Time       : 2026/09/27
 * @Author     : zhanglp8181
 * @File       : sd3-date-evidence.test.ts
 * @CallChain  : Vitest → SD-3 日期证据提取 → 真实模型验收断言
 * @Description: 验证日期验收只接受产品输出中的明确日历日期，不以预期答案补齐。
 */

import { describe, expect, it } from 'vitest';

import { extractExplicitCalendarDate } from './sd3-date-evidence';

describe('SD-3 产品日期证据', () => {
  it('rejects a relative-only answer without an explicit calendar date', () => {
    /** 验证只有“后天 10:00”时不能伪造出测试预期日期。 */

    expect(extractExplicitCalendarDate('目标日期（本轮新输入）：后天 10:00')).toBeNull();
  });

  it('returns an explicit wrong date so the caller can fail against its independent expectation', () => {
    /** 验证产品输出错误日期时保留错误事实，不能被提取器改成正确日期。 */

    const actual = extractExplicitCalendarDate(
      '目标日期（本轮新输入）：2048-12-31 10:00',
    );
    expect(actual).toBe('2048-12-31');
    expect(actual).not.toBe('2026-09-29');
  });

  it('accepts the explicit ISO or Chinese calendar date from product output', () => {
    /** 验证合法 ISO 和中文日期都能作为独立产品日期证据。 */

    expect(
      extractExplicitCalendarDate('目标日期（本轮新输入）：2026-09-29 10:00'),
    ).toBe('2026-09-29');
    expect(
      extractExplicitCalendarDate('目标日期（本轮新输入）：2026年9月29日 10:00'),
    ).toBe('2026-09-29');
  });

  it('keeps the current-input date when the same section also explains the old date', () => {
    /** 验证产品同时解释旧目标时仍读取本轮目标，而不是章节末尾的历史日期。 */

    expect(
      extractExplicitCalendarDate(
        '目标日期（本轮新输入）：2026-09-29 10:00；原目标 2026-09-28 09:00 已被本轮输入取代。',
      ),
    ).toBe('2026-09-29');
  });
});

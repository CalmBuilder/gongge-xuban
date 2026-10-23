/**
 * @Time       : 2026/09/27
 * @Author     : zhanglp8181
 * @File       : timezone.test.ts
 * @CallChain  : Vitest → timezone → 聊天输入时区声明与展示回退
 * @Description: 验证聊天只声明浏览器可确认的 IANA 时区，展示仍保留原有回退。
 */

import { afterEach, describe, expect, it, vi } from 'vitest';

import { getClientTimeZone, getDetectedClientTimeZone } from './timezone';

describe('客户端时区', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('returns the browser IANA zone for trusted chat declaration', () => {
    /** 验证正常浏览器环境返回可被 Intl 接受的 IANA 时区。 */

    expect(getDetectedClientTimeZone()).toBeTruthy();
    expect(getClientTimeZone()).toBe(getDetectedClientTimeZone());
  });

  it('omits the declaration when Intl cannot resolve a zone', () => {
    /** 验证浏览器时区识别失败时聊天层可省略声明，展示层仍保持兼容回退。 */

    vi.spyOn(Intl, 'DateTimeFormat').mockImplementation(() => {
      throw new Error('timezone unavailable');
    });

    expect(getDetectedClientTimeZone()).toBeUndefined();
    expect(getClientTimeZone()).toBe('Asia/Shanghai');
  });
});

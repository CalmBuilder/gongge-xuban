import { afterEach, expect, it, vi } from 'vitest';

afterEach(() => { vi.restoreAllMocks(); });

it('initializes the locale without storage access so the signed-out page can load', async () => {
  /** 导入和Provider初值都不得因浏览器禁存储失败，匿名页保留默认中文。 */
  vi.resetModules();
  vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
    throw new DOMException('synthetic storage denial', 'SecurityError');
  });
  const locale = await import('./index');
  expect(locale.getStoredLocale()).toBe('zh-CN');
});

it('keeps a legitimate saved English locale when storage is available', async () => {
  /** 正常英文持久偏好保持，降级不得覆盖合法的用户语言设置。 */
  vi.resetModules();
  localStorage.setItem('gongge_locale', 'en-US');
  const locale = await import('./index');
  expect(locale.getStoredLocale()).toBe('en-US');
});

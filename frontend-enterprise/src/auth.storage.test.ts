import { afterEach, expect, it, vi } from 'vitest';

import { clearEnterpriseAuthSession, ENTERPRISE_AUTH_STORAGE_KEY,
  getEnterpriseAuthSession, setEnterpriseAuthSession, type EnterpriseAuthSession } from './auth';

const session: EnterpriseAuthSession = { token: 'synthetic-test-session', user: {
  id: 'storage-owner', username: 'storage-owner', tenant_id: 'storage-tenant', role: 'member',
  membership_status: 'active', member_category_code: 'regular',
} };

afterEach(() => { vi.restoreAllMocks(); });

it('returns no authentication when browser storage reading is denied', () => {
  /** 浏览器拒绝读取时不恢复私密身份，也不让App初始化因SecurityError白屏。 */
  localStorage.setItem(ENTERPRISE_AUTH_STORAGE_KEY, JSON.stringify(session));
  vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
    throw new DOMException('synthetic storage denial', 'SecurityError');
  });
  expect(getEnterpriseAuthSession()).toBeNull();
});

it('keeps login failure when storage writing is denied', () => {
  /** 存储失败不能假报持久登录成功，不改为模块全局私密token回退。 */
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
    throw new DOMException('synthetic storage denial', 'SecurityError');
  });
  expect(() => setEnterpriseAuthSession(session)).toThrow('synthetic storage denial');
});

it('reads a valid session and treats malformed saved data as anonymous', () => {
  /** 正常持久认证保持，格式损坏仍按原合同默认拒绝。 */
  setEnterpriseAuthSession(session);
  expect(getEnterpriseAuthSession()).toEqual(session);
  localStorage.setItem(ENTERPRISE_AUTH_STORAGE_KEY, '{broken');
  expect(getEnterpriseAuthSession()).toBeNull();
  clearEnterpriseAuthSession();
  expect(localStorage.getItem(ENTERPRISE_AUTH_STORAGE_KEY)).toBeNull();
});

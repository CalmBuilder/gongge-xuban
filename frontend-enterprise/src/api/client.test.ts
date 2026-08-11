import { afterEach, expect, it, vi } from 'vitest';

import {
  clearEnterpriseAuthSession,
  setEnterpriseAuthSession,
  type EnterpriseAuthSession,
} from '../auth';
import { api, getRequestTenantId, LOGIN_TENANT_ID } from './client';

afterEach(() => {
  clearEnterpriseAuthSession();
  vi.restoreAllMocks();
});

it('keeps the bearer token when a caller adds an idempotency header', async () => {
  setEnterpriseAuthSession({
    token: 'token-a',
    user: {
      id: 'user-a',
      tenant_id: 'tenant-a',
      username: 'member',
      role: 'member',
      membership_status: 'active',
      member_category_code: 'employee',
    },
  });
  const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
    new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } }),
  );

  await api.postWithHeaders('/test', { value: 1 }, { 'Idempotency-Key': 'request-001' });

  expect(fetchMock).toHaveBeenCalledWith('/test', expect.objectContaining({
    headers: expect.objectContaining({
      Authorization: 'Bearer token-a',
      'Idempotency-Key': 'request-001',
    }),
  }));
});

it('reads the request tenant from the authenticated member instead of the login default', () => {
  const session: EnterpriseAuthSession = {
    token: 'signed-token',
    user: {
      id: 'user_1',
      tenant_id: 'tenant_from_auth_context',
      username: 'member',
      role: 'member',
      membership_status: 'active',
      member_category_code: 'employee',
    },
  };
  setEnterpriseAuthSession(session);
  expect(getRequestTenantId()).toBe('tenant_from_auth_context');
  expect(getRequestTenantId()).not.toBe(LOGIN_TENANT_ID);
});

it('rejects business requests without an authenticated enterprise context', () => {
  expect(() => getRequestTenantId()).toThrow('当前登录会话缺少企业上下文');
});

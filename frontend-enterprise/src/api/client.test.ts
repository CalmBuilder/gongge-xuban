import { afterEach, expect, it } from 'vitest';

import {
  clearEnterpriseAuthSession,
  setEnterpriseAuthSession,
  type EnterpriseAuthSession,
} from '../auth';
import { getRequestTenantId, LOGIN_TENANT_ID } from './client';

afterEach(() => {
  clearEnterpriseAuthSession();
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

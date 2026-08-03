import { render, screen } from '@testing-library/react';
import { expect, it } from 'vitest';

import { EnterpriseContextProvider, useEnterpriseContext } from './enterprise-context';

function ContextConsumer() {
  const context = useEnterpriseContext();
  return <span>{`${context.tenant.name}/${context.member.username}`}</span>;
}

it('provides the server-resolved tenant and member to authenticated pages', () => {
  render(
    <EnterpriseContextProvider
      value={{
        tenant: { id: 'tenant_a', name: '企业甲' },
        member: {
          id: 'member_a',
          tenant_id: 'tenant_a',
          username: 'zhangsan',
          role: 'member',
          membership_status: 'active',
          member_category_code: 'employee',
        },
        is_administrator: false,
      }}
    >
      <ContextConsumer />
    </EnterpriseContextProvider>,
  );

  expect(screen.getByText('企业甲/zhangsan')).toBeInTheDocument();
});

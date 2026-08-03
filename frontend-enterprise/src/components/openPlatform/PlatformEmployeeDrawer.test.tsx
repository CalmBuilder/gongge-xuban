import { fireEvent, render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';

import type { AgentProfileRead } from '../../types';
import PlatformEmployeeDrawer from './PlatformEmployeeDrawer';

const agent: AgentProfileRead = {
  id: 'agent-gallery',
  tenant_id: 'tenant_demo',
  name: '行政助理',
  description: '帮助完成行政工作',
  is_overall: false,
  status: 'active',
  metadata: { role_name: '行政' },
  resources: [],
  created_at: '',
  updated_at: '',
};

it('offers distinct use-and-chat and copy-and-customize actions', () => {
  const onUse = vi.fn();
  const onCopy = vi.fn();
  render(
    <PlatformEmployeeDrawer
      open
      agent={agent}
      platformTitle="数字员工广场"
      name="行政助理"
      role="行政"
      description="帮助完成行政工作"
      detailText="可直接使用或复制"
      workStyles={[]}
      stats={[]}
      onClose={vi.fn()}
      onUse={onUse}
      onCopy={onCopy}
    />,
  );

  fireEvent.click(screen.getByRole('button', { name: '添加使用并开始对话' }));
  fireEvent.click(screen.getByRole('button', { name: '复制并定制' }));
  expect(onUse).toHaveBeenCalledOnce();
  expect(onCopy).toHaveBeenCalledOnce();
});

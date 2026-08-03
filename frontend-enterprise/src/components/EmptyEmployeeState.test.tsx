import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, it, vi } from 'vitest';

import EmptyEmployeeState from './EmptyEmployeeState';

it('explains the empty state and keeps both next-step actions available', async () => {
  const user = userEvent.setup();
  const onCreate = vi.fn();
  const onBrowsePlatform = vi.fn();
  render(<EmptyEmployeeState isAdmin onCreate={onCreate} onBrowsePlatform={onBrowsePlatform} />);

  expect(screen.getByText('还没有数字员工')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '新建数字员工' }));
  await user.click(screen.getByRole('button', { name: '浏览开放广场' }));

  expect(onCreate).toHaveBeenCalledOnce();
  expect(onBrowsePlatform).toHaveBeenCalledOnce();
});

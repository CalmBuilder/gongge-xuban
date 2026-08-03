import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { expect, it } from 'vitest';

import ToolTypeSwitcher from './ToolTypeSwitcher';

/** 展示路由变化，验证类型选择会进入对应的新建流程。 */
function LocationProbe() {
  const location = useLocation();
  return <output aria-label="Current path">{location.pathname}</output>;
}

it('uses the project selection state and navigates to the MCP creation flow', () => {
  render(
    <MemoryRouter initialEntries={['/enterprise/tools/new']}>
      <ToolTypeSwitcher active="http" />
      <LocationProbe />
    </MemoryRouter>,
  );

  expect(screen.getByRole('link', { name: 'HTTP 工具' })).toHaveAttribute('aria-current', 'page');
  expect(screen.getByRole('link', { name: 'MCP 服务器' })).not.toHaveAttribute('aria-current');

  fireEvent.click(screen.getByRole('link', { name: 'MCP 服务器' }));

  expect(screen.getByLabelText('Current path')).toHaveTextContent('/enterprise/tools/mcp/new');
});

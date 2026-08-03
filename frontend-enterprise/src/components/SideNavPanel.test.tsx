import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { BriefcaseBusiness, Star, Users } from 'lucide-react';
import { expect, it } from 'vitest';

import SideNavPanel, { type SideNavPanelItem } from './SideNavPanel';

const items: SideNavPanelItem[] = [
  { key: 'all', label: '全部员工', description: '全部可管理的员工', count: 12, icon: Users },
  {
    key: 'expert',
    label: '专家',
    description: '按专业部门浏览',
    count: 3,
    icon: Star,
    children: [
      { key: '', label: '全部专家', count: 3 },
      { key: '工程研发', label: '工程研发', count: 2 },
    ],
  },
  { key: 'roles', label: '业务角色', count: 0, icon: BriefcaseBusiness },
];

function linkFor(key: string, childKey?: string) {
  return childKey ? `?view=${key}&dept=${encodeURIComponent(childKey)}` : `?view=${key}`;
}

function renderPanel(activeKey = 'all', activeChildKey = '') {
  return render(
    <MemoryRouter>
      <SideNavPanel
        title="员工视图"
        subtitle="按状态与类型浏览"
        icon={Users}
        items={items}
        activeKey={activeKey}
        activeChildKey={activeChildKey}
        linkFor={linkFor}
      />
    </MemoryRouter>,
  );
}

it('renders items with counts and marks the active one', () => {
  renderPanel('all');
  const active = screen.getByRole('link', { name: /全部员工/ });
  expect(active).toHaveAttribute('aria-current', 'page');
  expect(active).toHaveAttribute('href', '/?view=all');
  expect(screen.getByRole('link', { name: /业务角色/ })).not.toHaveAttribute('aria-current');
  expect(screen.getByText('12')).toBeInTheDocument();
});

it('expands child navigation only for the active item and encodes links', () => {
  renderPanel('expert', '工程研发');
  const allExperts = screen.getByRole('link', { name: /全部专家/ });
  expect(allExperts).toHaveAttribute('href', '/?view=expert');
  const department = screen.getByRole('link', { name: /工程研发/ });
  expect(department).toHaveAttribute('href', `/?view=expert&dept=${encodeURIComponent('工程研发')}`);
  expect(department).toHaveAttribute('aria-current', 'page');
});

it('hides child navigation when another item is active', () => {
  renderPanel('all');
  expect(screen.queryByRole('link', { name: /全部专家/ })).not.toBeInTheDocument();
});

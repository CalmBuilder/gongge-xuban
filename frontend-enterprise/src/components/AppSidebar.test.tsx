import { render, screen } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';

import { SidebarProvider } from '@/components/ui/sidebar';
import { TooltipProvider } from '@/components/ui/tooltip';

import AppSidebar from './AppSidebar';

beforeEach(() => {
  vi.stubGlobal('matchMedia', vi.fn().mockImplementation(() => ({
    matches: false,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  })));
});

function renderSidebar(
  governancePermissions: string[],
  isAdmin = false,
) {
  return render(
    <TooltipProvider>
      <SidebarProvider defaultOpen>
        <AppSidebar
          governancePermissions={governancePermissions}
          isAdmin={isAdmin}
          onNavigate={vi.fn()}
          onOpenChat={vi.fn()}
          onSelectAgent={vi.fn()}
          scopeAgents={[]}
          selected="/enterprise/platform"
          selectedAgentId=""
        />
      </SidebarProvider>
    </TooltipProvider>,
  );
}

it('按服务端治理权限显示组织管理导航，而不是依赖 admin 字段', () => {
  renderSidebar(['organization.read']);

  expect(screen.getByText('组织与岗位')).toBeInTheDocument();
  expect(screen.queryByText('成员管理')).not.toBeInTheDocument();
  expect(screen.queryByText('数据码表')).not.toBeInTheDocument();
  expect(screen.queryByText('组织角色')).not.toBeInTheDocument();
  expect(screen.queryByText('管理审计')).not.toBeInTheDocument();
  expect(screen.queryByText('模型配置')).not.toBeInTheDocument();
});

it('兼容管理员只在拥有对应治理权限时看到组织治理入口', () => {
  renderSidebar([
    'member.read',
    'organization.read',
    'reference_data.read',
    'authorization.read',
    'audit.read',
  ], true);

  expect(screen.getByText('成员管理')).toBeInTheDocument();
  expect(screen.getByText('组织与岗位')).toBeInTheDocument();
  expect(screen.getByText('数据码表')).toBeInTheDocument();
  expect(screen.getByText('组织角色')).toBeInTheDocument();
  expect(screen.getByText('管理审计')).toBeInTheDocument();
  expect(screen.getByText('模型配置')).toBeInTheDocument();
});

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

it('把开放平台发现与 Skill 管理、专家模板管理分开', () => {
  renderSidebar([]);

  expect(screen.getByText('开放广场平台')).toBeInTheDocument();
  expect(screen.getByText('Skill 管理')).toBeInTheDocument();
  expect(screen.queryByText('专家广场')).not.toBeInTheDocument();
  expect(screen.queryByText('专家模板管理')).not.toBeInTheDocument();
});

it('管理员通过平级入口管理平台内置专家模板', () => {
  renderSidebar([], true);

  expect(screen.getByText('专家模板管理')).toBeInTheDocument();
  expect(screen.getByText('Skill 管理')).toBeInTheDocument();
  expect(screen.queryByText('专家广场')).not.toBeInTheDocument();
});

it('keeps the lower management panel intrinsic and lets only primary navigation scroll', () => {
  renderSidebar([]);

  const header = document.querySelector('[data-sidebar="header"]');
  const content = document.querySelector('[data-sidebar="content"]');
  const footer = document.querySelector('[data-sidebar="footer"]');
  const primaryButton = screen.getByRole('button', { name: '开放广场平台' });

  expect(header).toHaveClass('min-h-0', 'flex-1', 'overflow-hidden');
  expect(header).not.toHaveClass('h-[var(--gg-sidebar-primary-slot)]', 'shrink-0');
  expect(content).toHaveClass('min-h-0', 'flex-none!', 'overflow-visible!', 'overscroll-contain');
  expect(content).not.toHaveClass('flex-1', 'overflow-y-auto');
  expect(footer).toHaveClass('min-h-[var(--gg-sidebar-footer-slot)]', 'shrink-0');
  expect(primaryButton).toHaveClass('h-[40px]', 'gap-[var(--gg-sidebar-control-gap)]', 'px-[16px]');
});

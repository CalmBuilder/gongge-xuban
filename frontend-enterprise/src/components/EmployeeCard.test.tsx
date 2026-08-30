import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type { AgentProfileRead, AgentResourceBindingRead, AgentResourceType } from '../types';
import EmployeeCard from './EmployeeCard';

function resource(resourceType: AgentResourceType, index: number): AgentResourceBindingRead {
  return {
    id: `resource-${index}`,
    tenant_id: 'tenant',
    agent_id: 'employee',
    resource_type: resourceType,
    resource_id: `${resourceType}-${index}`,
    status: 'active',
    metadata: {},
    created_at: '',
    updated_at: '',
  };
}

function employee(status: AgentProfileRead['status'] = 'active'): AgentProfileRead {
  return {
    id: 'employee',
    tenant_id: 'tenant',
    name: '财务助手',
    description: '负责预算核对与风险提示',
    is_overall: false,
    status,
    metadata: { role_name: '财务', avatar_preset: 'commerce-compass' },
    resources: [resource('knowledge_base', 1), resource('knowledge_base', 2), resource('general_skill', 3), resource('skill', 4)],
    created_at: '',
    updated_at: '',
  };
}

function renderCard(status: AgentProfileRead['status'] = 'active') {
  const callbacks = { onOpen: vi.fn(), onChat: vi.fn() };
  render(
    <EmployeeCard
      employee={employee(status)}
      canManage
      showMenu={false}
      relationLabels={['我拥有', '已添加', '企业发布']}
      onOpen={callbacks.onOpen}
      onChat={callbacks.onChat}
      onStatus={vi.fn()}
      onGallery={vi.fn()}
      onDelete={vi.fn()}
      onAvatar={vi.fn()}
      onEdit={vi.fn()}
    />,
  );
  return callbacks;
}

function expertEmployee(): AgentProfileRead {
  return {
    ...employee(),
    id: 'expert',
    name: '前端开发专家',
    metadata: {
      role_name: '工程研发', employee_type: 'expert', expert_source_code: 'agency-agents',
      expert_category: '工程研发', expert_subcategory: '前端与客户端', expert_tags: ['React'],
      upstream_url: 'https://github.com/msitarzewski/agency-agents/blob/abc/engineering/frontend.md',
      expert_capability_manifest: {
        schema_version: '1', capability_type: 'P2', readiness: 'partial',
        required_capabilities: ['prompt_reasoning', 'web_search'],
        resolved_capabilities: ['prompt_reasoning'], unresolved_requirements: ['web_search'],
        orchestration_required: false, core_execution_requires_external_capability: true,
        evidence: ['tools: WebSearch'],
      },
    },
  };
}

function renderExpertCard() {
  const onOpen = vi.fn();
  render(
    <EmployeeCard
      employee={expertEmployee()}
      canManage
      showMenu={false}
      onOpen={onOpen}
      onChat={vi.fn()}
      onStatus={vi.fn()}
      onGallery={vi.fn()}
      onDelete={vi.fn()}
      onAvatar={vi.fn()}
      onEdit={vi.fn()}
    />,
  );
  return onOpen;
}

describe('EmployeeCard', () => {
  it('opens from keyboard and keeps the chat action isolated', () => {
    const callbacks = renderCard();
    const card = screen.getByRole('button', { name: /财务助手/ });

    fireEvent.keyDown(card, { key: 'Enter' });
    fireEvent.click(screen.getByRole('button', { name: '发起对话' }));

    expect(callbacks.onOpen).toHaveBeenCalledTimes(1);
    expect(callbacks.onChat).toHaveBeenCalledTimes(1);
  });

  it('shows resource counts and disables chat while offline', () => {
    renderCard('archived');

    expect(screen.getByText('下线')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '发起对话' })).toBeDisabled();
    expect(screen.getByText('2')).toBeInTheDocument();
    expect(screen.getByText('资料')).toBeInTheDocument();
    expect(screen.getByText('技能')).toBeInTheDocument();
    expect(screen.getByText('SOP')).toBeInTheDocument();
  });

  it('shows relationship labels independently from employee status', () => {
    renderCard();
    expect(screen.getByText('我拥有')).toBeInTheDocument();
    expect(screen.getByText('已添加')).toBeInTheDocument();
    expect(screen.getByText('企业发布')).toBeInTheDocument();
  });

  it('shows expert source and readiness while isolating the source link', () => {
    const onOpen = renderExpertCard();
    expect(screen.getByText('专家能力分身')).toBeInTheDocument();
    expect(screen.getByText('Agency Agents')).toBeInTheDocument();
    expect(screen.getByText('前端与客户端')).toBeInTheDocument();
    expect(screen.getByText('部分能力待接入')).toBeInTheDocument();
    const link = screen.getByRole('link', { name: '查看原始来源' });
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noreferrer');
    fireEvent.click(link);
    expect(onOpen).not.toHaveBeenCalled();
  });

  it('does not render an untrusted expert source link', () => {
    const unsafe = expertEmployee();
    unsafe.metadata.upstream_url = 'https://example.com/fake';
    render(
      <EmployeeCard
        employee={unsafe} canManage showMenu={false} onOpen={vi.fn()} onChat={vi.fn()}
        onStatus={vi.fn()} onGallery={vi.fn()} onDelete={vi.fn()} onAvatar={vi.fn()}
        onEdit={vi.fn()}
      />,
    );
    expect(screen.queryByRole('link', { name: '查看原始来源' })).not.toBeInTheDocument();
  });

  it('can hide repeated source and department while preserving the direction', () => {
    render(
      <EmployeeCard
        employee={expertEmployee()} canManage showMenu={false}
        showExpertSource={false} showExpertDepartment={false}
        onOpen={vi.fn()} onChat={vi.fn()} onStatus={vi.fn()} onGallery={vi.fn()}
        onDelete={vi.fn()} onAvatar={vi.fn()} onEdit={vi.fn()}
      />,
    );
    expect(screen.queryByTestId('expert-source-badge')).not.toBeInTheDocument();
    expect(screen.queryByTestId('expert-department-badge')).not.toBeInTheDocument();
    expect(screen.getByText('前端与客户端')).toBeInTheDocument();
    expect(screen.getByText('部分能力待接入')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: '查看原始来源' })).toBeInTheDocument();
  });

  it('selects an expert without opening the card', () => {
    const onOpen = vi.fn();
    const onCheckedChange = vi.fn();
    render(
      <EmployeeCard
        employee={expertEmployee()} canManage showMenu={false} selectable checked={false}
        onCheckedChange={onCheckedChange} onOpen={onOpen} onChat={vi.fn()}
        onStatus={vi.fn()} onGallery={vi.fn()} onDelete={vi.fn()} onAvatar={vi.fn()}
        onEdit={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole('checkbox', { name: '选择前端开发专家' }));
    expect(onCheckedChange).toHaveBeenCalledWith(true);
    expect(onOpen).not.toHaveBeenCalled();
  });

  it('keeps the portrait inside a fixed identity slot when expert selection is enabled', () => {
    render(
      <EmployeeCard
        employee={expertEmployee()} canManage showMenu={false} selectable checked={false}
        onCheckedChange={vi.fn()} onOpen={vi.fn()} onChat={vi.fn()}
        onStatus={vi.fn()} onGallery={vi.fn()} onDelete={vi.fn()} onAvatar={vi.fn()}
        onEdit={vi.fn()}
      />,
    );

    const identity = document.querySelector('.gongge-employee-identity');
    expect(identity).toHaveClass('grid-cols-[56px_minmax(0,1fr)_auto]', 'overflow-hidden');
    expect(identity).not.toHaveClass('pl-[42px]');
    const avatarSlot = document.querySelector('[data-avatar-slot]');
    expect(avatarSlot).toHaveClass('employee-resource-avatar-slot', 'overflow-hidden');
    const avatar = avatarSlot?.querySelector('.employee-avatar');
    expect(avatar).toHaveClass('employee-resource-avatar');
    expect(avatar).toHaveStyle({
      width: '56px',
      height: '56px',
      borderRadius: 'var(--gg-radius-avatar-card)',
    });
    expect(avatar?.querySelector('img')).toHaveStyle({
      borderRadius: 'var(--gg-radius-avatar-card)',
      objectFit: 'cover',
    });
  });

  it('exposes expert classification editing from the card menu', () => {
    const onEditClassification = vi.fn();
    render(
      <EmployeeCard
        employee={expertEmployee()} canManage onEditClassification={onEditClassification}
        onOpen={vi.fn()} onChat={vi.fn()} onStatus={vi.fn()} onGallery={vi.fn()}
        onDelete={vi.fn()} onAvatar={vi.fn()} onEdit={vi.fn()}
      />,
    );
    const trigger = screen.getByRole('button', { name: '员工操作' });
    fireEvent.pointerDown(trigger);
    fireEvent.click(screen.getByRole('menuitem', { name: '编辑专家分类' }));
    expect(onEditClassification).toHaveBeenCalledOnce();
  });
});

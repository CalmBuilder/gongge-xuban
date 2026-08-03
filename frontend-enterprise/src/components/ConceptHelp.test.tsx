import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, it } from 'vitest';

import { ConceptHelp, ConceptNote } from './ConceptHelp';

it('explains that experts remain digital employees without inheriting a real identity', async () => {
  const user = userEvent.setup();
  render(<ConceptHelp topic="expert" />);

  await user.click(screen.getByRole('button', { name: '了解专家' }));

  expect(screen.getByText('专家是数字员工的能力分身形态')).toBeInTheDocument();
  expect(screen.getByText(/不复制真人身份和私人凭据/)).toBeInTheDocument();
  expect(screen.getByText(/不是第三种登录主体/)).toBeInTheDocument();
});

it('keeps the short note visible while the detailed plaza explanation is optional', async () => {
  const user = userEvent.setup();
  render(
    <ConceptNote topic="plaza">
      使用广场数字员工只建立使用关系
    </ConceptNote>,
  );

  expect(screen.getByText('使用广场数字员工只建立使用关系')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '了解数字员工广场' }));
  expect(screen.getByText(/不等于拥有、编辑或发布/)).toBeInTheDocument();
  expect(screen.getByText(/系统建立 AgentUsage/)).toBeInTheDocument();
  expect(screen.getByText(/owner_user_id 指向当前用户/)).toBeInTheDocument();
  expect(screen.getByText(/source_agent_id 指向广场来源/)).toBeInTheDocument();
});

it('shows the complete evolution path from a personal expert to an organizational digital employee', async () => {
  const user = userEvent.setup();
  render(<ConceptHelp topic="forms" triggerLabel="个人专家与组织数字员工" />);

  await user.click(screen.getByRole('button', { name: '了解个人专家与组织数字员工' }));

  expect(screen.getByText('个人专家与组织数字员工是同一运行主体的不同治理形态'))
    .toBeInTheDocument();
  expect(screen.getByText('用户的专家（能力分身）')).toBeInTheDocument();
  expect(screen.getByText('组织审核：明确责任、可见范围和授权')).toBeInTheDocument();
  expect(screen.getByText('发布到组织内数字员工广场')).toBeInTheDocument();
});

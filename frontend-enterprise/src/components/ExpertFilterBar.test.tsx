import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, it, vi } from 'vitest';

import ExpertFilterBar from './ExpertFilterBar';

const departments = [
  { value: '工程研发', label: '工程研发', count: 54 },
  { value: '市场营销', label: '市场营销', count: 36 },
];
const directions = [
  { value: 'AI 与智能体', label: 'AI 与智能体', count: 7 },
  { value: '数据与数据库', label: '数据与数据库', count: 5 },
];

function renderBar(sourceCount = 1, resultCount = 7) {
  const callbacks = {
    onSourceChange: vi.fn(),
    onDepartmentChange: vi.fn(),
    onDirectionChange: vi.fn(),
    onReset: vi.fn(),
  };
  render(
    <ExpertFilterBar
      sourceOptions={sourceCount === 1
        ? [{ value: 'agency-agents', label: 'Agency Agents', count: 263 }]
        : [
            { value: 'agency-agents', label: 'Agency Agents', count: 263 },
            { value: 'partner', label: 'Partner', count: 8 },
          ]}
      departmentOptions={departments}
      directionOptions={directions}
      source=""
      department="工程研发"
      direction="AI 与智能体"
      resultCount={resultCount}
      hasFilters
      onSourceChange={callbacks.onSourceChange}
      onDepartmentChange={callbacks.onDepartmentChange}
      onDirectionChange={callbacks.onDirectionChange}
      onReset={callbacks.onReset}
    />,
  );
  return callbacks;
}

it('hides a single source and keeps direction chips selectable', () => {
  const callbacks = renderBar();
  expect(screen.queryByText('来源：Agency Agents')).not.toBeInTheDocument();
  expect(screen.queryByLabelText('来源')).not.toBeInTheDocument();
  const selected = screen.getAllByRole('button', { name: 'AI 与智能体，7 位' });
  expect(selected[0]).toHaveAttribute('aria-pressed', 'true');
  fireEvent.click(screen.getAllByRole('button', { name: '数据与数据库，5 位' })[0]);
  expect(callbacks.onDirectionChange).toHaveBeenCalledWith('数据与数据库');
});

it('resets filters from the bar when results are visible', () => {
  const callbacks = renderBar(1, 7);
  fireEvent.click(screen.getByRole('button', { name: '清除筛选' }));
  expect(callbacks.onReset).toHaveBeenCalled();
});

it('shows a source selector for multiple sources and opens mobile filters', async () => {
  const user = userEvent.setup();
  const callbacks = renderBar(2);
  await user.click(screen.getByRole('combobox', { name: '来源' }));
  await user.click(await screen.findByRole('option', { name: /Partner/ }));
  expect(callbacks.onSourceChange).toHaveBeenCalledWith('partner');

  fireEvent.click(screen.getByRole('button', { name: '筛选专家' }));
  expect(screen.getByRole('dialog')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '重置筛选' }));
  expect(callbacks.onReset).toHaveBeenCalled();
});

import { fireEvent, render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';

import ExpertCategoryRail from './ExpertCategoryRail';

const options = [
  { value: '专业服务', label: '专业服务', count: 56 },
  { value: '工程研发', label: '工程研发', count: 54 },
];

it('shows counts and exposes the selected department', () => {
  const onChange = vi.fn();
  render(
    <ExpertCategoryRail
      options={options}
      value="工程研发"
      totalCount={263}
      onChange={onChange}
    />,
  );

  expect(screen.getAllByRole('button', { name: '全部专家，263 位' })).toHaveLength(2);
  const engineering = screen.getAllByRole('button', { name: '工程研发，54 位' });
  expect(engineering).toHaveLength(2);
  engineering.forEach((button) => expect(button).toHaveAttribute('aria-pressed', 'true'));
  fireEvent.click(screen.getAllByRole('button', { name: '专业服务，56 位' })[0]);
  expect(onChange).toHaveBeenCalledWith('专业服务');
});

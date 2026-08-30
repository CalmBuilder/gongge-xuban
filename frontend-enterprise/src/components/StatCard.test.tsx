import { render, screen } from '@testing-library/react';
import { expect, it } from 'vitest';

import { StatCard } from './StatCard';

it('uses the shared metric family and semantic success surface', () => {
  render(<StatCard value={12} label="已启用" tone="green" />);

  const card = screen.getByText('已启用').closest('[data-card-family="metric"]');
  expect(card).toBeInTheDocument();
  expect(card).toHaveClass('min-h-[136px]', 'border-[var(--gg-capability-line)]');
  expect(screen.getByText('12')).toHaveClass('gg-type-metric', 'text-[var(--gg-state-success)]');
});

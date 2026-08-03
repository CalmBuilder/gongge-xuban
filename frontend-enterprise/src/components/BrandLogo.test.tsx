import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import BrandLogo from './BrandLogo';

describe('BrandLogo', () => {
  it.each([
    ['login', '共格·序伴'],
    ['management', '共格'],
    ['workspace', '序伴'],
  ] as const)('renders the %s brand lockup', (context, wordmark) => {
    render(<BrandLogo context={context} />);

    expect(screen.getByText(wordmark)).toBeInTheDocument();
    expect(screen.getByRole('img')).toHaveAccessibleName(wordmark);
  });

  it('keeps a meaningful accessible name in mark-only mode', () => {
    render(<BrandLogo context="management" markOnly />);

    expect(screen.getByRole('img')).toHaveAccessibleName('共格');
    expect(screen.queryByText('共格')).not.toBeInTheDocument();
  });
});

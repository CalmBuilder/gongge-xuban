import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { PLAZA_RESOURCE_KINDS } from '@/assets/plaza/plaza-resource-icons';

import PlazaResourceArtwork from './PlazaResourceArtwork';

describe('PlazaResourceArtwork', () => {
  it.each(PLAZA_RESOURCE_KINDS)('renders %s as decorative dimensional artwork', (kind) => {
    const { container } = render(<PlazaResourceArtwork kind={kind} />);
    const artwork = container.querySelector(`img[data-plaza-resource-artwork="${kind}"]`);

    expect(artwork).not.toBeNull();
    expect(artwork).toHaveClass('size-[80px]', 'object-contain');
    expect(artwork).toHaveAttribute('aria-hidden', 'true');
    expect(artwork).toHaveAttribute('alt', '');
    expect(artwork?.getAttribute('src')).toBeTruthy();
  });

  it('scales the artwork for headers and drawers', () => {
    const { container, rerender } = render(<PlazaResourceArtwork kind="skills" size="micro" />);
    expect(container.querySelector('img')).toHaveClass('size-[32px]');

    rerender(<PlazaResourceArtwork kind="skills" size="compact" />);
    expect(container.querySelector('img')).toHaveClass('size-[36px]');

    rerender(<PlazaResourceArtwork kind="skills" size="drawer" />);
    expect(container.querySelector('img')).toHaveClass('size-[54px]');
  });
});

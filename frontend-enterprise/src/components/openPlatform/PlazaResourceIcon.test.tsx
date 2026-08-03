import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import PlazaResourceIcon from './PlazaResourceIcon';

const CATEGORY_CASES = [
  ['knowledge', 'bg-[#eaf7f0]', 'text-[#238b5a]', 'lucide-book-open-text'],
  ['general-skills', 'bg-[#eef3ff]', 'text-[#356ae6]', 'lucide-blocks'],
  ['skills', 'bg-[#eaf9fc]', 'text-[#0891b2]', 'lucide-list-checks'],
  ['tools', 'bg-[#fff4e5]', 'text-[#d97706]', 'lucide-wrench'],
] as const;

describe('PlazaResourceIcon', () => {
  it.each(CATEGORY_CASES)('renders %s as a semantic category mark', (kind, background, foreground, glyph) => {
    const { container } = render(<PlazaResourceIcon kind={kind} />);
    const mark = container.querySelector(`[data-plaza-resource-kind="${kind}"]`);

    expect(mark).toHaveClass(background, foreground, 'size-7', 'rounded-lg');
    expect(mark?.querySelector('svg')).toHaveClass('size-[17px]', glyph);
    expect(mark?.querySelector('svg')).toHaveAttribute('aria-hidden', 'true');
  });

  it('uses the larger optical size in resource drawers', () => {
    const { container } = render(<PlazaResourceIcon kind="knowledge" size="drawer" />);
    const mark = container.querySelector('[data-plaza-resource-kind="knowledge"]');

    expect(mark).toHaveClass('size-9', 'rounded-[10px]');
    expect(mark?.querySelector('svg')).toHaveClass('size-5');
  });

  it('provides shared compact and micro marks for page headers, dialogs, and navigation', () => {
    const { container, rerender } = render(<PlazaResourceIcon kind="general-skills" size="compact" />);
    expect(container.firstChild).toHaveClass('size-6', 'rounded-[7px]');
    expect(container.querySelector('svg')).toHaveClass('size-[14px]');

    rerender(<PlazaResourceIcon kind="skills" size="micro" />);
    expect(container.firstChild).toHaveClass('size-5', 'rounded-md');
    expect(container.querySelector('svg')).toHaveClass('size-3');
  });
});

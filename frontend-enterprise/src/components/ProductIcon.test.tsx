import { render } from '@testing-library/react';
import { expect, it } from 'vitest';

import ProductIcon from './ProductIcon';

it('renders the requested product icon', () => {
  const { container } = render(<ProductIcon name="chat" />);

  expect(container.querySelector('svg')).toHaveClass('product-icon-chat');
});

import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';

import { CatalogGrid } from './CatalogGrid';
import { DetailSurface } from './DetailSurface';
import { PageHeader } from './PageHeader';
import { PageShell } from './PageShell';
import { PageState } from './PageState';

describe('enterprise page primitives', () => {
  it('exposes the page family and keeps the heading hierarchy navigable', () => {
    render(
      <MemoryRouter>
        <PageShell template="catalog">
          <PageHeader
            backTo="/enterprise"
            backLabel="Back to enterprise workspace"
            title="Skill 管理"
            description="Review and manage the capability catalog"
            size="section"
          />
        </PageShell>
      </MemoryRouter>,
    );

    expect(document.querySelector('[data-page-template="catalog"]')).toBeInTheDocument();
    expect(document.querySelector('[data-typography-contract="v1"]')).toHaveClass('gg-typography-scope');
    expect(screen.getByRole('heading', { level: 1, name: 'Skill 管理' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Back to enterprise workspace' })).toHaveAttribute('href', '/enterprise');
  });

  it('uses a predictable family marker for resource, metric and info grids', () => {
    const { rerender } = render(<CatalogGrid family="resource"><article>Resource</article></CatalogGrid>);
    expect(document.querySelector('[data-card-family="resource"]')).toBeInTheDocument();

    rerender(<CatalogGrid family="metric"><article>Metric</article></CatalogGrid>);
    expect(document.querySelector('[data-card-family="metric"]')).toBeInTheDocument();

    rerender(<CatalogGrid family="info"><article>Info</article></CatalogGrid>);
    expect(document.querySelector('[data-card-family="info"]')).toBeInTheDocument();
  });

  it('announces errors and marks the detail container type for assistive technology audits', () => {
    render(
      <>
        <PageState kind="error" title="Load failed" description="Retry later" />
        <DetailSurface container="drawer"><p>Detail content</p></DetailSurface>
      </>,
    );

    expect(screen.getByRole('alert')).toHaveAttribute('aria-live', 'assertive');
    expect(document.querySelector('[data-detail-container="drawer"]')).toBeInTheDocument();
  });
});

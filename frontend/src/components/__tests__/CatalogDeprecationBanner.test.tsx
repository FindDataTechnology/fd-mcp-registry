import React from 'react';
import { render, screen } from '@testing-library/react';
import CatalogDeprecationBanner from '../CatalogDeprecationBanner';

/**
 * unify-supply-spine-lineage 3.4: the console's catalogue screens are an
 * operator view. The assertions that matter are (a) it says so, and (b) it
 * points at the supply domain's front door — a banner that says "go elsewhere"
 * without naming elsewhere is worse than none.
 */
describe('CatalogDeprecationBanner', () => {
  it('names the execution-plane view and links the supply front door', () => {
    render(<CatalogDeprecationBanner />);
    const note = screen.getByTestId('catalog-deprecation-banner');
    expect(note).toBeInTheDocument();
    expect(note).toHaveTextContent('执行面视图');

    const link = screen.getByRole('link');
    expect(link).toHaveAttribute('href', 'https://facet.finddatatech.cloud');
    expect(link.getAttribute('href')).not.toMatch(/mcp\.finddatatech\.cloud/);
  });

  it('is standing, not dismissible', () => {
    render(<CatalogDeprecationBanner />);
    expect(screen.queryByRole('button')).toBeNull();
  });
});

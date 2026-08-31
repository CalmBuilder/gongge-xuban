import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const stylesPath = resolve(process.cwd(), 'src/styles.css');
const styles = readFileSync(stylesPath, 'utf8');

describe('Gongge design tokens', () => {
  it.each([
    ['--gg-ink', '#18213D'],
    ['--gg-cobalt', '#3157E8'],
    ['--gg-cyan', '#7BE7F5'],
    ['--gg-cloud', '#F5F7FC'],
    ['--gg-paper', '#FFFFFF'],
    ['--gg-slate', '#68738E'],
  ])('defines %s as %s', (token, value) => {
    expect(styles).toContain(`${token}: ${value}`);
  });

  it('defines the shared control radius and reduced-motion fallback', () => {
    expect(styles).toContain('--gg-radius-control: 8px');
    expect(styles).toContain('--gg-interaction: var(--gg-cobalt)');
    expect(styles).toContain('--gg-text-secondary: #526B86');
    expect(styles).toContain('@media (prefers-reduced-motion: reduce)');
  });

  it('locks the shared type scale and page grid contracts', () => {
    expect(styles).toContain('--gg-type-card-title-size: 16px');
    expect(styles).toContain('--gg-type-body-line: 22px');
    expect(styles).toContain('--gg-type-control-size: 13px');
    expect(styles).toContain('--gg-resource-card-min-width: 264px');
    expect(styles).toContain('--gg-resource-card-min-height: 288px');
    expect(styles).toContain('--gg-layout-gap: 16px');
    expect(styles).toContain('.gg-resource-grid');
    expect(styles).toContain('.gg-metric-grid');
    expect(styles).toContain('.gg-info-grid');
    expect(styles).toContain('--gg-sidebar-primary-divider-gap: 12px');
    expect(styles).toContain('--gg-sidebar-lower-top-gap: 16px');
    expect(styles).toContain('--gg-sidebar-lower-bottom-gap: 8px');
    expect(styles).toContain('--gg-sidebar-primary-item-gap: 6px');
  });
});

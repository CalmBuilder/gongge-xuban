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
    expect(styles).toContain('--gg-radius-control: 12px');
    expect(styles).toContain('@media (prefers-reduced-motion: reduce)');
  });
});

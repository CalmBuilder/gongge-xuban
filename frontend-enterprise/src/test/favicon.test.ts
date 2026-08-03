import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { describe, expect, it } from 'vitest';

const html = readFileSync(resolve(process.cwd(), 'index.html'), 'utf8');

describe('browser favicon', () => {
  it('bundles the same Gongge brand mark through the served assets path', () => {
    expect(html).toContain('href="/src/assets/brand/gongge-mark.svg"');
    expect(html).not.toContain('href="/gongge-xuban-mark.svg"');
  });
});

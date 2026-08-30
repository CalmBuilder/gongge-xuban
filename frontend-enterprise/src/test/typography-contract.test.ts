import { readdirSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { describe, expect, it } from 'vitest';

const sourceRoot = resolve(process.cwd(), 'src');

/**
 * 递归收集需要执行企业端文字契约检查的源码文件。
 */
function collectSourceFiles(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const entryPath = resolve(directory, entry.name);
    if (entry.isDirectory()) return collectSourceFiles(entryPath);
    if (!entry.isFile() || !/\.(ts|tsx)$/.test(entry.name) || entry.name.endsWith('.test.ts') || entry.name.endsWith('.test.tsx')) {
      return [];
    }
    return [entryPath];
  });
}

const rawSizePattern = /(?<![\w:-])text-(?:\[(?:\d+(?:\.\d+)?px)\]|xs|sm|base|lg|xl|2xl|3xl|4xl|5xl|6xl|7xl|8xl|9xl)(?![\w-])/g;
const rawLeadingPattern = /(?<![\w:-])leading-(?:\[(?:\d+(?:\.\d+)?(?:px|em|rem)?|1\.\d+)\]|none|tight|snug|normal|relaxed|loose|[3-9]|10|11|12)(?![\w-])/g;

describe('enterprise typography contract', () => {
  it('does not allow page code to introduce an unregistered text size or line-height', () => {
    const violations = collectSourceFiles(sourceRoot).flatMap((filePath) => {
      const source = readFileSync(filePath, 'utf8');
      return [...source.matchAll(rawSizePattern), ...source.matchAll(rawLeadingPattern)].map((match) => ({
        file: filePath.replace(`${sourceRoot}/`, ''),
        token: match[0],
      }));
    });

    expect(violations).toEqual([]);
  });

  it('keeps the shared scale, page roots and floating surfaces under the contract', () => {
    const styles = readFileSync(resolve(sourceRoot, 'styles.css'), 'utf8');
    const app = readFileSync(resolve(sourceRoot, 'App.tsx'), 'utf8');
    const floatingSurfaceFiles = [
      'components/ui/dialog.tsx',
      'components/ui/alert-dialog.tsx',
      'components/ui/sheet.tsx',
      'components/ui/popover.tsx',
      'components/ui/dropdown-menu.tsx',
      'components/ui/select.tsx',
      'components/ui/tooltip.tsx',
      'components/ui/hover-card.tsx',
    ];

    expect(styles).toContain('--gg-type-page-title-size: 24px');
    expect(styles).toContain('--gg-type-section-title-size: 18px');
    expect(styles).toContain('--gg-type-card-title-size: 16px');
    expect(styles).toContain('--gg-type-body-size: 14px');
    expect(styles).toContain('--gg-type-control-size: 13px');
    expect(styles).toContain('--gg-type-meta-size: 12px');
    expect(styles).toContain('--gg-type-caption-size: 11px');
    expect(styles).toContain('.gg-type-markdown');
    expect(styles).toContain('.gg-type-citation-markdown');
    expect(styles).toContain('body .gg-typography-scope');
    expect(app).toContain('data-typography-contract="v1"');
    for (const relativePath of floatingSurfaceFiles) {
      expect(readFileSync(resolve(sourceRoot, relativePath), 'utf8')).toContain('gg-typography-scope');
    }
  });
});

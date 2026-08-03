import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const appSource = readFileSync(resolve(process.cwd(), 'src/App.tsx'), 'utf8');
const knowledgeSource = readFileSync(resolve(process.cwd(), 'src/pages/KnowledgePage.tsx'), 'utf8');

describe('enterprise selection styling', () => {
  it('uses the shared cobalt token for segmented selections', () => {
    expect(appSource).toContain('aria-pressed={agentForm.sourceMode === option.value}');
    expect(appSource).toContain('bg-[var(--gg-cobalt)] text-white');
    expect(appSource).not.toContain('bg-[#18181a] text-white');
  });

  it('uses the shared cobalt token for selected resource cards', () => {
    expect(knowledgeSource).toContain("'border-[var(--gg-cobalt)] ring-2");
    expect(knowledgeSource).not.toContain('ring-2 ring-[#18181a]');
  });
});

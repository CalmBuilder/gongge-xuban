import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

const SRC_ROOT = join(process.cwd(), 'src');

function source(path: string): string {
  return readFileSync(join(SRC_ROOT, path), 'utf8');
}

const openPlatformSource = readFileSync(
  join(process.cwd(), 'src/pages/OpenPlatformPage.tsx'),
  'utf8',
);
const detailSource = readFileSync(
  join(process.cwd(), 'src/components/openPlatform/PlatformKindDetailView.tsx'),
  'utf8',
);

describe('plaza resource icon integration', () => {
  it('shares the 3D glass category artwork across plaza cards, detail views, and drawers', () => {
    expect(openPlatformSource).toContain('import PlazaResourceArtwork');
    expect(detailSource).toContain('import PlazaResourceArtwork');
    expect(openPlatformSource).not.toContain('PLAZA_RESOURCE_ICONS');
    expect(detailSource).not.toContain('PLAZA_RESOURCE_ICONS');
    expect(openPlatformSource).not.toContain('platformResourceIcon');
    expect(detailSource).not.toContain('platformResourceIcon');
    expect(openPlatformSource).toContain('<PlazaResourceArtwork kind={detailItem.kind} size="drawer" />');
    expect(detailSource).toContain('<PlazaResourceArtwork kind={kind} />');
  });

  it('uses the shared semantic mark in every resource page header and import flow', () => {
    const resourcePages = [
      ['pages/KnowledgePage.tsx', 'knowledge'],
      ['pages/GeneralSkillsPage.tsx', 'general-skills'],
      ['pages/SkillsPage.tsx', 'skills'],
      ['pages/ToolsPage.tsx', 'tools'],
    ] as const;

    for (const [path, kind] of resourcePages) {
      const pageSource = source(path);
      expect(pageSource).toContain('import PlazaResourceIcon');
      expect(pageSource).toContain(`<PlazaResourceIcon kind="${kind}" size="compact" />`);
    }
  });

  it('uses the shared semantic mark in navigation, platform headers, and capability cards', () => {
    const sidebarSource = source('components/AppSidebar.tsx');
    const workRecordSource = source('pages/dashboard/WorkRecordTab.tsx');
    const dashboardSource = source('pages/dashboard/DashboardPage.tsx');
    const conversationLogsSource = source('pages/dashboard/ConversationLogsTab.tsx');
    const tutorialSource = source('pages/TutorialPage.tsx');
    const resourceCardSource = source('components/openPlatform/PlatformResourceCard.tsx');

    expect(sidebarSource).toContain('import PlazaResourceArtwork');
    expect(sidebarSource).toContain('<PlazaResourceArtwork kind="knowledge" size="micro" />');
    expect(sidebarSource).toContain('<PlazaResourceArtwork kind="general-skills" size="micro" />');
    expect(sidebarSource).toContain('<PlazaResourceArtwork kind="skills" size="micro" />');
    expect(sidebarSource).toContain('<PlazaResourceArtwork kind="tools" size="micro" />');
    expect(detailSource).toContain('<PlazaResourceArtwork kind={kind} size="compact" />');
    expect(workRecordSource).toContain('<PlazaResourceIcon kind={resourceKind} />');
    expect(dashboardSource).toContain('<PlazaResourceIcon kind="knowledge" />');
    expect(dashboardSource).toContain('<PlazaResourceIcon kind="general-skills" />');
    expect(dashboardSource).toContain('<PlazaResourceIcon kind="skills" />');
    expect(dashboardSource).toContain('<PlazaResourceIcon kind="tools" />');
    expect(conversationLogsSource).toContain('<PlazaResourceIcon kind="general-skills" size="micro" />');
    expect(conversationLogsSource).toContain('<PlazaResourceIcon kind="knowledge" size="micro" />');
    expect(conversationLogsSource).toContain('<PlazaResourceIcon kind="tools" size="micro" />');
    expect(tutorialSource).toContain('<PlazaResourceIcon kind={feature.resourceKind} />');
    expect(resourceCardSource).toContain('<PlazaResourceIcon kind="knowledge" />');
    expect(openPlatformSource).not.toMatch(/(FileSearch|Solution|Profile|Tool)Outlined/);
  });

  it('does not import the retired plaza illustration SVGs anywhere in runtime TSX', () => {
    const runtimeFiles = [
      'pages/KnowledgePage.tsx',
      'pages/GeneralSkillsPage.tsx',
      'pages/SkillsPage.tsx',
      'pages/ToolsPage.tsx',
      'pages/OpenPlatformPage.tsx',
      'components/AppSidebar.tsx',
      'pages/dashboard/WorkRecordTab.tsx',
      'pages/dashboard/DashboardPage.tsx',
      'pages/dashboard/ConversationLogsTab.tsx',
      'pages/TutorialPage.tsx',
      'components/openPlatform/PlatformResourceCard.tsx',
    ];
    const retiredAsset = /plaza-(knowledge|skill|sop|tool)\.svg\?react/;

    for (const path of runtimeFiles) expect(source(path)).not.toMatch(retiredAsset);
  });
});

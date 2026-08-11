import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { api } from '@/api/client';
import type { GeneralSkillRead } from '@/types';
import { SkillGovernanceDialog } from './SkillGovernanceDialog';

vi.mock('@/api/client', () => ({
  api: { get: vi.fn(), patch: vi.fn(), post: vi.fn() },
  getRequestTenantId: () => 'tenant-test',
}));

vi.mock('@/components/ui/app-toast', () => ({
  notify: { success: vi.fn(), error: vi.fn() },
}));

const row: GeneralSkillRead = {
  id: 'genskill_test',
  tenant_id: 'tenant-test',
  slug: 'test-skill',
  name: '售后处理 Skill',
  skill_markdown: '# Test',
  skill_files: [],
  metadata: {},
  status: 'published',
  permissions: {},
  runtime_config: {},
  current_published_revision_id: 'gsrev_2',
  row_version: 4,
  binding_id: 'agentres_test',
  binding_status: 'active',
  binding_row_version: 3,
  revision_policy: 'pinned',
  pinned_revision_id: 'gsrev_1',
  invocation_policy: 'model_allowed',
  created_at: '2026-08-12T00:00:00Z',
  updated_at: '2026-08-12T00:00:00Z',
};

const revisions = [
  {
    id: 'gsrev_2',
    skill_id: row.id,
    revision_number: 2,
    content_checksum: '2'.repeat(64),
    manifest_checksum: 'a'.repeat(64),
    status: 'published' as const,
    row_version: 3,
    created_at: '2026-08-12T00:00:00Z',
    published_at: '2026-08-12T00:00:00Z',
  },
  {
    id: 'gsrev_1',
    skill_id: row.id,
    revision_number: 1,
    content_checksum: '1'.repeat(64),
    manifest_checksum: 'b'.repeat(64),
    status: 'superseded' as const,
    row_version: 2,
    created_at: '2026-08-11T00:00:00Z',
    published_at: '2026-08-11T00:00:00Z',
  },
];

describe('SkillGovernanceDialog', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.get).mockResolvedValue(revisions);
    vi.mocked(api.patch).mockResolvedValue({});
    vi.mocked(api.post).mockResolvedValue({});
  });

  it('loads immutable revisions and saves the complete binding contract', async () => {
    const onChanged = vi.fn();
    render(
      <SkillGovernanceDialog row={row} agentId="agent-test" onClose={vi.fn()} onChanged={onChanged} />,
    );

    expect(await screen.findByText('v2 · published')).toBeInTheDocument();
    expect(screen.getByText('v1 · superseded')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '保存策略' }));

    await waitFor(() => expect(api.patch).toHaveBeenCalledWith(
      '/api/enterprise/general-skill-governance/bindings/agentres_test',
      {
        agent_id: 'agent-test',
        status: 'active',
        revision_policy: 'pinned',
        pinned_revision_id: 'gsrev_1',
        invocation_policy: 'model_allowed',
        expected_row_version: 3,
      },
    ));
    expect(onChanged).toHaveBeenCalledOnce();
  });

  it('rolls back with both skill and revision optimistic-lock versions', async () => {
    render(
      <SkillGovernanceDialog row={row} agentId="agent-test" onClose={vi.fn()} onChanged={vi.fn()} />,
    );

    fireEvent.click(await screen.findByRole('button', { name: '回滚到此版本' }));

    await waitFor(() => expect(api.post).toHaveBeenCalledWith(
      '/api/enterprise/general-skill-governance/skills/genskill_test/rollback',
      {
        target_revision_id: 'gsrev_1',
        expected_skill_row_version: 4,
        expected_target_row_version: 2,
      },
    ));
  });
});

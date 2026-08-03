import { describe, expect, it, vi } from 'vitest';

import {
  addMissingDistillWorkspaceId,
  createDistillWorkspaceParams,
  distillCacheIdentity,
} from './distill-workspace';

describe('distill workspace isolation', () => {
  it('creates a fresh workspace parameter for each new SOP', () => {
    const randomUUID = vi
      .spyOn(window.crypto, 'randomUUID')
      .mockReturnValueOnce('00000000-0000-4000-8000-000000000001')
      .mockReturnValueOnce('00000000-0000-4000-8000-000000000002');

    const first = createDistillWorkspaceParams('agent/a');
    const second = createDistillWorkspaceParams('agent/a');

    expect(first.get('mode')).toBe('create');
    expect(first.get('agent_id')).toBe('agent/a');
    expect(first.get('workspace_id')).not.toBe(second.get('workspace_id'));
    randomUUID.mockRestore();
  });

  it('isolates create caches while keeping edit caches keyed by skill', () => {
    expect(distillCacheIdentity('', 'create', 'workspace-a')).toBe('create:workspace-a');
    expect(distillCacheIdentity('', 'create', 'workspace-b')).toBe('create:workspace-b');
    expect(distillCacheIdentity('skill-1', 'create', 'workspace-a')).toBe('skill-1');
  });

  it('marks a create route without workspace identity as pending', () => {
    expect(distillCacheIdentity('', 'create', '')).toBe('create:pending');
  });

  it('upgrades a legacy create route before cache hydration', () => {
    const randomUUID = vi
      .spyOn(window.crypto, 'randomUUID')
      .mockReturnValue('00000000-0000-4000-8000-000000000003');
    const original = new URLSearchParams('mode=create&agent_id=agent%2Fa');

    const upgraded = addMissingDistillWorkspaceId(original);

    expect(original.has('workspace_id')).toBe(false);
    expect(upgraded?.get('agent_id')).toBe('agent/a');
    expect(upgraded?.get('workspace_id')).toBe('00000000-0000-4000-8000-000000000003');
    randomUUID.mockRestore();
  });

  it('does not replace an existing create or edit identity', () => {
    expect(addMissingDistillWorkspaceId(new URLSearchParams('mode=create&workspace_id=workspace-a'))).toBeNull();
    expect(addMissingDistillWorkspaceId(new URLSearchParams('skill_id=skill-1'))).toBeNull();
  });
});

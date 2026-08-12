import { createClientId } from './client-id';

export function createDistillWorkspaceId(): string {
  return createClientId();
}

export function createDistillWorkspaceParams(agentId = ''): URLSearchParams {
  const params = new URLSearchParams({
    mode: 'create',
    workspace_id: createDistillWorkspaceId(),
  });
  if (agentId) params.set('agent_id', agentId);
  return params;
}

export function addMissingDistillWorkspaceId(params: URLSearchParams): URLSearchParams | null {
  if (params.get('skill_id') || params.get('mode') !== 'create' || params.get('workspace_id')) {
    return null;
  }
  const nextParams = new URLSearchParams(params);
  nextParams.set('workspace_id', createDistillWorkspaceId());
  return nextParams;
}

export function distillCacheIdentity(skillId: string, mode: string, workspaceId: string): string {
  if (skillId) return skillId;
  if (mode === 'create') return `create:${workspaceId || 'pending'}`;
  return mode || 'new';
}

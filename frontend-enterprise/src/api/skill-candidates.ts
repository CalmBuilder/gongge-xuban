import type { SkillCard } from '@/types';
import type { CandidateResetRead, SkillCandidateRead, SkillCandidateReceipt,
  SkillEditorContextRead } from '@/types/skill-candidate';
import { api, getRequestTenantId } from './client';

const ROOT = '/api/enterprise/skills/feedback-candidates';
const query = () => new URLSearchParams({ tenant_id: getRequestTenantId() }).toString();

export function createSkillCandidate(materialId: string, agentId: string, requestKey: string) {
  return api.post<SkillCandidateRead>(ROOT, {
    tenant_id: getRequestTenantId(), agent_id: agentId, material_ids: [materialId],
    request_key: requestKey,
  });
}
export function readSkillCandidate(id: string) {
  return api.get<SkillCandidateRead>(`${ROOT}/${encodeURIComponent(id)}?${query()}`);
}
export function readSkillEditorContext() {
  return api.get<SkillEditorContextRead>(`${ROOT}/editor-context?${query()}`);
}
export function saveSkillCandidate(candidate: SkillCandidateRead, content: SkillCard) {
  return api.put<SkillCandidateRead>(`${ROOT}/${encodeURIComponent(candidate.id)}`, {
    tenant_id: getRequestTenantId(), expected_revision: candidate.revision, content,
    confirm_base_difference: candidate.base_difference_confirmed,
  });
}
export function rejectSkillCandidate(candidate: SkillCandidateRead) {
  return api.post<SkillCandidateRead>(`${ROOT}/${encodeURIComponent(candidate.id)}/reject`, {
    tenant_id: getRequestTenantId(), expected_revision: candidate.revision,
  });
}
export function resetSkillCandidate(id: string, revision: number, agentId: string,
  skillId: string | null, requestKey: string) {
  return api.post<CandidateResetRead>(`${ROOT}/${encodeURIComponent(id)}/reset`, {
    tenant_id: getRequestTenantId(), expected_revision: revision, agent_id: agentId || null,
    skill_id: skillId, request_key: requestKey, discard_candidate: true,
  });
}
export function publishSkillCandidate(candidate: SkillCandidateRead, requestKey: string,
  confirmBaseDifference: boolean) {
  return api.post<SkillCandidateReceipt>(`${ROOT}/${encodeURIComponent(candidate.id)}/publish`, {
    tenant_id: getRequestTenantId(), expected_revision: candidate.revision,
    expected_base_version_id: candidate.base_skill_version_id,
    expected_base_checksum: candidate.base_checksum, request_key: requestKey,
    confirm: true, confirm_base_difference: confirmBaseDifference,
  });
}

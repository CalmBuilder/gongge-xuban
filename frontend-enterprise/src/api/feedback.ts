import type { FeedbackMaterialMode, FeedbackMaterialRead } from '@/types/feedback';
import { api, getRequestTenantId } from './client';

const ROOT = '/api/enterprise/feedback';

export function previewFeedbackMaterial(feedbackId: string, targetAgentId: string, note: string, requestKey: string) {
  return api.post<FeedbackMaterialRead>(`${ROOT}/${encodeURIComponent(feedbackId)}/materials/preview`, {
    tenant_id: getRequestTenantId(), target_agent_id: targetAgentId, note, request_key: requestKey,
  });
}

export function listFeedbackMaterials(agentId: string, mode: FeedbackMaterialMode) {
  const query = new URLSearchParams({ tenant_id: getRequestTenantId(), agent_id: agentId, mode });
  return api.get<FeedbackMaterialRead[]>(`${ROOT}/materials?${query.toString()}`);
}

export function readFeedbackMaterial(id: string) {
  const query = new URLSearchParams({ tenant_id: getRequestTenantId() });
  return api.get<FeedbackMaterialRead>(`${ROOT}/materials/${encodeURIComponent(id)}?${query.toString()}`);
}

export function shareFeedbackMaterial(material: FeedbackMaterialRead) {
  return api.post<FeedbackMaterialRead>(`${ROOT}/materials/${encodeURIComponent(material.id)}/share`, {
    tenant_id: getRequestTenantId(), expected_revision: material.feedback_revision,
    material_checksum: material.material_checksum,
  });
}

export function withdrawFeedbackMaterial(material: FeedbackMaterialRead) {
  return api.post<FeedbackMaterialRead>(`${ROOT}/materials/${encodeURIComponent(material.id)}/withdraw`, {
    tenant_id: getRequestTenantId(), expected_revision: material.feedback_revision,
  });
}

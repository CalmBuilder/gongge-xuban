export type FeedbackMaterialStatus = 'preview' | 'shared' | 'withdrawn' | 'invalid';

export interface FeedbackSource {
  status: 'verified' | 'missing';
  branch: 'sop' | 'dynamic_task' | 'unknown';
  message_id: string;
  turn_id: string | null;
  execution_id: string | null;
  skill_id: string | null;
  skill_version_id: string | null;
  skill_version: string | null;
  definition_checksum: string | null;
  agent_id: string | null;
  missing_reason: string | null;
}

export interface FeedbackMaterialRead {
  id: string;
  feedback_id: string;
  feedback_revision: number;
  target_agent_id: string;
  status: FeedbackMaterialStatus;
  material_checksum: string;
  material: {
    source: FeedbackSource;
    facts: {
      rating: string;
      user_request: string;
      assistant_reply: string;
      operations_known: boolean;
      operations: Array<{ id: string; status: string; tool_id: string | null }>;
    };
    user_note: string;
    inference: { bucket: string; summary: string; reason: string } | null;
  };
  created_at: string;
  updated_at: string;
}

export type FeedbackMaterialMode = 'owned' | 'managed';
export interface FeedbackPreviewSelection { feedbackId: string; targetAgentId: string }

export const FEEDBACK_MATERIAL_STATUS_LABELS: Record<FeedbackMaterialStatus, string> = {
  preview: '待分享', shared: '已分享', withdrawn: '已撤回', invalid: '已失效',
};

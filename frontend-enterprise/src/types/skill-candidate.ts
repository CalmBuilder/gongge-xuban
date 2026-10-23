import type { SkillCard, SkillRead } from '@/types';
import type { FeedbackMaterialRead } from './feedback';

export type SkillCandidateStatus = 'draft' | 'ready' | 'rejected' | 'closed' | 'published';
export interface SkillCandidateReceipt {
  candidate_id: string;
  skill_id: string;
  scope_agent_id: string;
  skill_version_id: string;
  version: string;
  definition_checksum: string;
}
export interface SkillCandidateRead {
  id: string;
  tenant_id: string;
  skill_id: string;
  scope_agent_id: string;
  revision: number;
  status: SkillCandidateStatus;
  base_skill_version_id: string;
  base_checksum: string;
  base_version: string | null;
  base_difference: boolean;
  base_difference_confirmed: boolean;
  base_content: SkillCard | null;
  content: SkillCard | null;
  materials: FeedbackMaterialRead[];
  changed_paths: string[];
  published: SkillCandidateReceipt | null;
}
export interface SkillEditorContextRead {
  candidate_id: string | null;
  candidate_revision: number | null;
  status: string | null;
  scope_agent_id: string;
  ordinary: boolean;
  scope_profile_id: string | null;
  editor_context: string | null;
}
export interface CandidateResetRead {
  candidate_id: string;
  revision: number;
  editor_context: string;
  current_skill: SkillRead | null;
}

export type HumanParticipantCoverage = {
  node_id: string;
  role_codes: string[];
  action_permission_codes: string[];
  exclude_initiator: boolean;
  declared_direct_user_count: number;
  direct_user_count: number;
  eligible_candidate_count: number;
  source_counts: Record<string, number>;
  participant_scope_resolver: string;
  participant_scope_org_unit_id: string | null;
  contextual_scope: boolean;
  context_count: number;
  covered_context_count: number;
  uncovered_org_unit_ids: string[];
  issue_codes: string[];
};

export type SopDependencyAssessment = {
  readiness: 'ready' | 'attention_required' | 'blocked';
  issue_codes: string[];
  human_task_count: number;
  tool_operation_count: number;
  knowledge_task_count: number;
  bound_agent_count: number;
  executable_agent_count: number;
  human_participants: HumanParticipantCoverage[];
};

export type SopDependencyCoverageEntry = {
  skill_id: string;
  name: string;
  current_version: string;
  requester_policy: string;
  requester_policy_explicit: boolean;
  dependency_assessment: SopDependencyAssessment;
};

export type SopDependencyCoverageReport = {
  tenant_id: string;
  total: number;
  readiness_counts: Record<'ready' | 'attention_required' | 'blocked', number>;
  entries: SopDependencyCoverageEntry[];
};

export type PositionRoleSopImpact = {
  skillId: string;
  skillName: string;
  version: string;
  readiness: SopDependencyAssessment['readiness'];
  participant: HumanParticipantCoverage;
};

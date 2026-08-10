/**
 * @Time       : 2026/08/11 23:58
 * @Author     : zhanglp8181
 * @File       : standing-approvals.ts
 * @CallChain  : Standing Approval API → scheduled task management dialog
 * @Description: 定义调度任务精确长期批准候选、规则和撤销契约。
 */

export type StandingApprovalCandidate = {
  thread_binding_id: string;
  profile_id: string;
  profile_display_name: string;
  target_label: string;
  tool_snapshot_checksum: string;
  target_hash: string;
};

export type StandingApprovalRule = {
  id: string;
  tenant_id: string;
  agent_id: string;
  source_schedule_id: string;
  source_schedule_checksum: string;
  profile_id: string;
  binding_id: string;
  tool_id: string;
  tool_snapshot_checksum: string;
  risk_class: string;
  target_type: string;
  canonical_target: string;
  target_hash: string;
  argument_constraints: { content?: { equals?: string } };
  valid_from: string;
  valid_to: string;
  status: 'active' | 'revoked';
  revision: number;
  created_by_user_id: string;
  revoked_by_user_id?: string | null;
  revoked_at?: string | null;
  created_at: string;
  updated_at: string;
};

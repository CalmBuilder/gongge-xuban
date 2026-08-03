export type ManagementAuditOutcome = 'success' | 'denied' | 'failure';
export type ManagementAuditActionKind = 'create' | 'update' | 'delete' | 'read' | 'execute';

export type ManagementAuditLog = {
  id: string;
  tenant_id: string;
  actor_user_id: string | null;
  actor_type: string;
  actor_display_name: string | null;
  action: string;
  action_kind: ManagementAuditActionKind;
  outcome: ManagementAuditOutcome;
  resource_type: string;
  resource_id: string | null;
  target_org_unit_id: string | null;
  permission_code: string | null;
  permission_source: string | null;
  request_id: string | null;
  correlation_id: string | null;
  before: Record<string, unknown>;
  after: Record<string, unknown>;
  detail: Record<string, unknown>;
  created_at: string;
};

export type ManagementAuditPageResult = {
  items: ManagementAuditLog[];
  total: number;
  page: number;
  page_size: number;
};

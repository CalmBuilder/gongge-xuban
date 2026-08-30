export type DynamicTaskOperationalAlert = {
  code: string;
  severity: 'warning' | 'critical';
  current: number;
  threshold: number | null;
  enabled: boolean;
  triggered: boolean;
};

export type DynamicTaskOperationalSnapshot = {
  tenant_id: string;
  observed_at: string;
  thresholds_configured: boolean;
  quota_limits_configured: boolean;
  runtime_capacity_limits_configured: boolean;
  runtime_capacity_available: boolean;
  base_execution_available: boolean;
  base_execution_reason: string;
  high_risk_external_write_available: boolean;
  high_risk_external_write_reason: string;
  high_risk_destructive_available: boolean;
  high_risk_destructive_reason: string;
  quota_limits: Record<string, number>;
  quota_leases: Record<string, number>;
  executions: Record<string, number>;
  signals: Record<string, number>;
  operations: Record<string, number>;
  publications: Record<string, number>;
  attentions: Record<string, number>;
  oldest_waiting_age_seconds: number;
  alerts: DynamicTaskOperationalAlert[];
};

/**
 * @Time       : 2026/08/10 20:30
 * @Author     : zhanglp8181
 * @File       : connections.ts
 * @CallChain  : ConnectionProfile API → connection client → ConnectionsPage/AttentionCenter
 * @Description: 定义连接档案、Agent 绑定、健康与凭据轮换的前端共享契约。
 */

export type ConnectionStatus = 'active' | 'disabled' | 'reauth_required';
export type ConnectionHealthStatus = 'unverified' | 'healthy' | 'degraded' | 'unhealthy';
export type ConnectionProvider = 'slack' | 'wecom';

export type ConnectionProfileRead = {
  id: string;
  tenant_id: string;
  provider: ConnectionProvider;
  account_id: string;
  display_name: string;
  required_scopes: string[];
  granted_scopes: string[];
  tool_allowlist: string[];
  status: ConnectionStatus;
  health_status: ConnectionHealthStatus;
  health_error_code?: string | null;
  rate_limited_until?: string | null;
  last_checked_at?: string | null;
  last_healthy_at?: string | null;
  secret_revision: number;
  revision: number;
  has_secret: boolean;
  callback_configured: boolean;
  created_at: string;
  updated_at: string;
};

export type ConnectionBindingRead = {
  id: string;
  tenant_id: string;
  agent_id: string;
  profile_id: string;
  allowed_scopes: string[];
  allowed_actions: string[];
  enabled: boolean;
  revision: number;
  created_at: string;
  updated_at: string;
};

export type ConnectorInboundRouteRead = {
  id: string;
  tenant_id: string;
  provider: ConnectionProvider;
  profile_id: string;
  agent_id: string;
  enabled: boolean;
  revision: number;
};

export type ConnectorInboundEventRead = {
  id: string;
  profile_id: string;
  event_type: string;
  status: 'pending' | 'failed' | 'dead_letter';
  attempt_count: number;
  last_error_code?: string | null;
  principal_bound: boolean;
  created_at: string;
};

export type ConnectorPrincipalBindingRead = {
  id: string;
  tenant_id: string;
  provider: ConnectionProvider;
  profile_id: string;
  user_id: string;
  enabled: boolean;
  revision: number;
};

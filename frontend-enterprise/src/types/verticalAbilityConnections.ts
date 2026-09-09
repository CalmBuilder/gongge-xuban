/**
 * @Time       : 2026/09/03 16:45
 * @Author     : zhanglp8181
 * @File       : verticalAbilityConnections.ts
 * @CallChain  : 网站连接中心 → API client → 用户网站连接契约
 * @Description: 定义网站连接卡片、认证方式和脱敏连接状态，前端不持有任何连接密文。
 */

export type VerticalAbilityConnectionAuthMethod =
  | 'api_token'
  | 'cookie'
  | 'browser_session'
  | 'oauth';

export type VerticalAbilityConnectionTransport = 'local_loopback' | 'browser_relay';

export type VerticalAbilityConnectionImplementationStatus = 'available' | 'adapter_pending';

export type VerticalAbilityConnectionStatus =
  | 'pending'
  | 'connected'
  | 'needs_reauth'
  | 'revoked'
  | 'not_connected';

export type VerticalAbilityConnectionHealthStatus =
  | 'unverified'
  | 'healthy'
  | 'degraded'
  | 'unhealthy';

export type VerticalAbilityConnectionField = {
  key: string;
  label: string;
  placeholder: string;
  description: string;
  secret: boolean;
  required: boolean;
};

export type VerticalAbilityConnectionTransportOption = {
  transport: VerticalAbilityConnectionTransport;
  label: string;
  description: string;
};

export type VerticalAbilityConnectionPlatform = {
  platform_id: string;
  platform_name: string;
  description: string;
  capability_ids: string[];
  auth_methods: VerticalAbilityConnectionAuthMethod[];
  auth_method_labels: Record<string, string>;
  fields_by_method: Record<string, VerticalAbilityConnectionField[]>;
  browser_transports: VerticalAbilityConnectionTransportOption[];
  setup_url?: string | null;
  setup_link_label?: string | null;
  implementation_status: VerticalAbilityConnectionImplementationStatus;
  implementation_message: string;
  status: VerticalAbilityConnectionStatus;
  status_message: string;
  connection_ids: string[];
};

export type VerticalAbilityConnection = {
  id: string;
  platform_id: string;
  platform_name: string;
  account_label: string;
  account_identity?: string | null;
  auth_method: VerticalAbilityConnectionAuthMethod;
  transport: VerticalAbilityConnectionTransport;
  status: Exclude<VerticalAbilityConnectionStatus, 'not_connected'>;
  health_status: VerticalAbilityConnectionHealthStatus;
  health_error_code?: string | null;
  has_credentials: boolean;
  revision: number;
  last_checked_at?: string | null;
  last_healthy_at?: string | null;
  updated_at: string;
  message: string;
  verification_success?: boolean | null;
};

export type VerticalAbilityConnectionCatalog = {
  platforms: VerticalAbilityConnectionPlatform[];
  connections: VerticalAbilityConnection[];
};

export type VerticalAbilityConnectionUpsert = {
  platform_id: string;
  account_label: string;
  auth_method: VerticalAbilityConnectionAuthMethod;
  credentials: Record<string, string>;
  expected_revision: number;
};

export type VerticalAbilityBrowserSessionConnect = {
  platform_id: string;
  account_label: string;
  expected_revision: number;
};

export type VerticalAbilityRelayPairingCreate = {
  platform_id: string;
  account_label: string;
  expected_revision: number;
  transport: VerticalAbilityConnectionTransport;
};

export type VerticalAbilityRelayPairing = {
  session_id: string;
  platform_id: string;
  account_label: string;
  transport: VerticalAbilityConnectionTransport;
  status: 'pending' | 'connected' | 'disconnected' | 'revoked' | 'expired';
  pairing_token?: string | null;
  expires_at?: string | null;
  last_seen_at?: string | null;
  connected_at?: string | null;
  pairing_token_available: boolean;
  message: string;
};

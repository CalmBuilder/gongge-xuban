/**
 * @Time       : 2026/08/10 20:30
 * @Author     : zhanglp8181
 * @File       : connections.ts
 * @CallChain  : ConnectionsPage/AttentionCenter → connection API client → FastAPI
 * @Description: 集中封装连接档案、绑定、健康、停用和重新授权请求。
 */

import { api, getRequestTenantId } from './client';
import type {
  ConnectionBindingRead,
  ConnectionProfileRead,
  ConnectionProvider,
  ConnectorInboundEventRead,
  ConnectorInboundRouteRead,
  ConnectorPrincipalBindingRead,
} from '@/types/connections';

export function listConnectionProfiles(): Promise<ConnectionProfileRead[]> {
  /** 读取当前登录租户的连接档案。 */

  return api.get(`/api/enterprise/connection-profiles?tenant_id=${getRequestTenantId()}`);
}

export function createSlackConnection(input: {
  displayName: string;
  token: string;
}): Promise<ConnectionProfileRead> {
  /** 创建并即时验证一个只读 Slack 工作区连接。 */

  return api.post('/api/enterprise/connection-profiles', {
    tenant_id: getRequestTenantId(),
    command_id: crypto.randomUUID(),
    expected_revision: 0,
    provider: 'slack',
    display_name: input.displayName,
    token: input.token,
    required_scopes: ['channels:read'],
  });
}

export function createWeComConnection(input: {
  displayName: string;
  corpId: string;
  agentId: string;
  corpSecret: string;
  callbackToken: string;
  callbackEncodingAesKey: string;
}): Promise<ConnectionProfileRead> {
  /** 创建并即时验证一个企业微信自建应用连接。 */

  return api.post('/api/enterprise/connection-profiles', {
    tenant_id: getRequestTenantId(),
    command_id: crypto.randomUUID(),
    expected_revision: 0,
    provider: 'wecom',
    display_name: input.displayName,
    corp_id: input.corpId,
    agent_id: input.agentId,
    corp_secret: input.corpSecret,
    callback_token: input.callbackToken,
    callback_encoding_aes_key: input.callbackEncodingAesKey,
    required_scopes: ['application:read'],
  });
}

export function startSlackOAuth(input: {
  flowType: 'create' | 'reauthorize' | 'reauthorize_attention';
  displayName?: string;
  profileId?: string;
  attentionId?: string;
  expectedProfileRevision: number;
  expectedAttentionRevision?: number;
}): Promise<{ authorize_url: string; expires_at: string }> {
  /** 创建短期一次性 OAuth state；client secret 和 token 均只存在于服务端。 */

  return api.post('/api/enterprise/connection-profiles/slack/oauth/start', {
    tenant_id: getRequestTenantId(),
    command_id: crypto.randomUUID(),
    flow_type: input.flowType,
    display_name: input.displayName,
    profile_id: input.profileId,
    attention_id: input.attentionId,
    expected_profile_revision: input.expectedProfileRevision,
    expected_attention_revision: input.expectedAttentionRevision,
  });
}

export function checkConnectionHealth(profileId: string): Promise<ConnectionProfileRead> {
  /** 对指定档案执行真实身份与 scope 探测。 */

  return api.post(
    `/api/enterprise/connection-profiles/${profileId}/health?tenant_id=${getRequestTenantId()}`,
  );
}

export function reauthorizeConnection(
  profileId: string,
  expectedRevision: number,
  token: string,
): Promise<ConnectionProfileRead> {
  /** 以 CAS 将新 token 写成同一账号的下一密钥修订。 */

  return api.post(`/api/enterprise/connection-profiles/${profileId}/reauthorize`, {
    tenant_id: getRequestTenantId(),
    command_id: crypto.randomUUID(),
    expected_revision: expectedRevision,
    token,
  });
}

export function reauthorizeWeComConnection(
  profileId: string,
  expectedRevision: number,
  credentials: { corpId: string; agentId: string; corpSecret: string },
): Promise<ConnectionProfileRead> {
  /** 以 CAS 将企业微信三元凭据写成同一应用的下一密钥修订。 */

  return api.post(`/api/enterprise/connection-profiles/${profileId}/reauthorize`, {
    tenant_id: getRequestTenantId(),
    command_id: crypto.randomUUID(),
    expected_revision: expectedRevision,
    corp_id: credentials.corpId,
    agent_id: credentials.agentId,
    corp_secret: credentials.corpSecret,
  });
}

export function reauthorizeConnectionAttention(input: {
  profileId: string;
  attentionId: string;
  profileRevision: number;
  attentionRevision: number;
  token: string;
  commandId: string;
}): Promise<ConnectionProfileRead> {
  /** 原子轮换凭据并决定 reauth Attention，避免“已换 token 但未唤醒任务”的事务缝隙。 */

  return api.post(
    `/api/enterprise/connection-profiles/${input.profileId}`
      + `/reauthorize-attention/${input.attentionId}`,
    {
      tenant_id: getRequestTenantId(),
      expected_revision: input.profileRevision,
      attention_expected_revision: input.attentionRevision,
      command_id: input.commandId,
      token: input.token,
    },
  );
}

export function reauthorizeWeComConnectionAttention(input: {
  profileId: string;
  attentionId: string;
  profileRevision: number;
  attentionRevision: number;
  corpId: string;
  agentId: string;
  corpSecret: string;
  commandId: string;
}): Promise<ConnectionProfileRead> {
  /** 原子轮换企业微信凭据并决定 reauth Attention。 */

  return api.post(
    `/api/enterprise/connection-profiles/${input.profileId}`
      + `/reauthorize-attention/${input.attentionId}`,
    {
      tenant_id: getRequestTenantId(),
      expected_revision: input.profileRevision,
      attention_expected_revision: input.attentionRevision,
      command_id: input.commandId,
      corp_id: input.corpId,
      agent_id: input.agentId,
      corp_secret: input.corpSecret,
    },
  );
}

export function disableConnection(
  profileId: string,
  expectedRevision: number,
): Promise<ConnectionProfileRead> {
  /** 以 CAS 停用整个连接档案。 */

  return api.post(`/api/enterprise/connection-profiles/${profileId}/disable`, {
    tenant_id: getRequestTenantId(),
    command_id: crypto.randomUUID(),
    expected_revision: expectedRevision,
  });
}

export function listConnectionBindings(profileId: string): Promise<ConnectionBindingRead[]> {
  /** 读取指定连接档案的全部 Agent 绑定。 */

  return api.get(
    `/api/enterprise/connection-profiles/${profileId}/bindings?tenant_id=${getRequestTenantId()}`,
  );
}

export function createConnectionBinding(
  profileId: string,
  expectedProfileRevision: number,
  agentId: string,
  provider: ConnectionProvider = 'slack',
): Promise<ConnectionBindingRead> {
  /** 为指定 Agent 建立 provider 对应的最小只读 scope 绑定。 */

  return api.post(`/api/enterprise/connection-profiles/${profileId}/bindings`, {
    tenant_id: getRequestTenantId(),
    command_id: crypto.randomUUID(),
    expected_revision: expectedProfileRevision,
    agent_id: agentId,
    allowed_scopes: [provider === 'wecom' ? 'application:read' : 'channels:read'],
  });
}

export function setConnectionBindingState(
  profileId: string,
  binding: Pick<ConnectionBindingRead, 'id' | 'revision'>,
  enabled: boolean,
): Promise<ConnectionBindingRead> {
  /** 以绑定修订号启停单个 Agent 的账号使用权。 */

  return api.post(
    `/api/enterprise/connection-profiles/${profileId}/bindings/${binding.id}/state`,
    {
      tenant_id: getRequestTenantId(),
      command_id: crypto.randomUUID(),
      expected_revision: binding.revision,
      enabled,
    },
  );
}

export function setConnectionBindingActions(
  profile: Pick<ConnectionProfileRead, 'id' | 'revision'>,
  binding: Pick<ConnectionBindingRead, 'id' | 'revision'>,
  enabled: boolean,
): Promise<ConnectionBindingRead> {
  /** 以档案/绑定双修订显式启停审批后企业微信发送动作。 */

  return api.post(
    `/api/enterprise/connection-profiles/${profile.id}/bindings/${binding.id}/actions`,
    {
      tenant_id: getRequestTenantId(),
      command_id: crypto.randomUUID(),
      expected_profile_revision: profile.revision,
      expected_binding_revision: binding.revision,
      allowed_actions: enabled ? ['wecom.message_send'] : [],
    },
  );
}

export function getConnectorInboundRoute(
  profileId: string,
): Promise<ConnectorInboundRouteRead | null> {
  /** 读取企业微信档案当前唯一入站 Agent 路由。 */

  return api.get(
    `/api/enterprise/connection-profiles/${profileId}/inbound-route`
      + `?tenant_id=${getRequestTenantId()}`,
  );
}

export function setConnectorInboundRoute(
  profile: Pick<ConnectionProfileRead, 'id' | 'revision'>,
  agentId: string,
): Promise<ConnectorInboundRouteRead> {
  /** 将入站消息明确路由到已经绑定该档案的数字员工。 */

  return api.post(`/api/enterprise/connection-profiles/${profile.id}/inbound-route`, {
    tenant_id: getRequestTenantId(),
    command_id: crypto.randomUUID(),
    expected_revision: profile.revision,
    agent_id: agentId,
  });
}

export function listConnectorInboundEvents(
  profileId: string,
): Promise<ConnectorInboundEventRead[]> {
  /** 读取不含正文和外部用户标识的待授权入站事件。 */

  return api.get(
    `/api/enterprise/connection-profiles/${profileId}/inbound-events`
      + `?tenant_id=${getRequestTenantId()}`,
  );
}

export function bindConnectorPrincipal(
  eventId: string,
  userId: string,
): Promise<ConnectorPrincipalBindingRead> {
  /** 从已验签事件将外部发送者授权给活动平台用户。 */

  return api.post('/api/enterprise/connection-profiles/inbound/principal-bindings', {
    tenant_id: getRequestTenantId(),
    command_id: crypto.randomUUID(),
    event_id: eventId,
    user_id: userId,
  });
}

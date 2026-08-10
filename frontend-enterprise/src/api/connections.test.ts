/**
 * @Time       : 2026/08/10 11:45
 * @Author     : zhanglp8181
 * @File       : connections.test.ts
 * @CallChain  : Vitest → connection API client → mocked shared API client
 * @Description: 验证 OAuth 与企业微信建档请求的租户、凭据和 CAS 信封。
 */

import { beforeEach, expect, it, vi } from 'vitest';

import { api } from './client';
import {
  bindConnectorPrincipal,
  createWeComConnection,
  setConnectionBindingActions,
  setConnectorInboundRoute,
  startSlackOAuth,
} from './connections';

vi.mock('./client', () => ({
  getRequestTenantId: () => 'tenant_demo',
  api: { post: vi.fn() },
}));

beforeEach(() => {
  /** 清理每项契约测试的请求记录。 */

  vi.mocked(api.post).mockReset();
  vi.mocked(api.post).mockResolvedValue({
    authorize_url: 'https://slack.com/oauth/v2/authorize?state=test',
    expires_at: '2026-08-10T12:10:00Z',
  });
});

it('为 reauth Attention 发送目标资源和双 CAS 修订且不携带凭据', async () => {
  /** 验证浏览器只创建服务端 state，不接触 client secret、token 或任意 callback。 */

  await startSlackOAuth({
    flowType: 'reauthorize_attention',
    profileId: 'profile_a',
    attentionId: 'attention_a',
    expectedProfileRevision: 7,
    expectedAttentionRevision: 3,
  });

  const [path, body] = vi.mocked(api.post).mock.calls[0];
  expect(path).toBe('/api/enterprise/connection-profiles/slack/oauth/start');
  expect(body).toMatchObject({
    tenant_id: 'tenant_demo',
    flow_type: 'reauthorize_attention',
    profile_id: 'profile_a',
    attention_id: 'attention_a',
    expected_profile_revision: 7,
    expected_attention_revision: 3,
  });
  expect(body).toHaveProperty('command_id');
  expect(body).not.toHaveProperty('token');
  expect(body).not.toHaveProperty('client_secret');
  expect(body).not.toHaveProperty('redirect_uri');
});

it('企业微信建档提交固定 provider 和最小只读 scope', async () => {
  /** 验证浏览器不允许调用方改写 provider、scope 或服务端命令标识。 */

  await createWeComConnection({
    displayName: '序伴集成测试',
    corpId: 'corp-a',
    agentId: '1000002',
    corpSecret: 'secret-a',
    callbackToken: 'callback-token',
    callbackEncodingAesKey: 'abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG',
  });

  const [path, body] = vi.mocked(api.post).mock.calls[0];
  expect(path).toBe('/api/enterprise/connection-profiles');
  expect(body).toMatchObject({
    tenant_id: 'tenant_demo',
    expected_revision: 0,
    provider: 'wecom',
    display_name: '序伴集成测试',
    corp_id: 'corp-a',
    agent_id: '1000002',
    corp_secret: 'secret-a',
    callback_token: 'callback-token',
    callback_encoding_aes_key: 'abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG',
    required_scopes: ['application:read'],
  });
  expect(body).toHaveProperty('command_id');
  expect(body).not.toHaveProperty('token');
});

it('消息接入只提交已验签事件和平台资源身份', async () => {
  /** 验证客户端不接受原始企业微信发送者 ID，并为路由保留 profile CAS。 */

  await setConnectorInboundRoute({ id: 'profile_a', revision: 7 }, 'agent_a');
  await bindConnectorPrincipal('event_a', 'user_a');

  expect(vi.mocked(api.post).mock.calls[0]).toEqual([
    '/api/enterprise/connection-profiles/profile_a/inbound-route',
    expect.objectContaining({
      tenant_id: 'tenant_demo',
      expected_revision: 7,
      agent_id: 'agent_a',
    }),
  ]);
  expect(vi.mocked(api.post).mock.calls[1]).toEqual([
    '/api/enterprise/connection-profiles/inbound/principal-bindings',
    expect.objectContaining({
      tenant_id: 'tenant_demo',
      event_id: 'event_a',
      user_id: 'user_a',
    }),
  ]);
  expect(JSON.stringify(vi.mocked(api.post).mock.calls)).not.toContain('sender_ref');
});

it('受控发送动作提交档案与绑定双 CAS 且只允许固定动作', async () => {
  /** 验证浏览器不能提交任意工具名，也不会把外部目标或正文混入授权请求。 */

  await setConnectionBindingActions(
    { id: 'profile_a', revision: 7 },
    { id: 'binding_a', revision: 3 },
    true,
  );

  const [path, body] = vi.mocked(api.post).mock.calls[0];
  expect(path).toBe(
    '/api/enterprise/connection-profiles/profile_a/bindings/binding_a/actions',
  );
  expect(body).toMatchObject({
    tenant_id: 'tenant_demo',
    expected_profile_revision: 7,
    expected_binding_revision: 3,
    allowed_actions: ['wecom.message_send'],
  });
  expect(body).toHaveProperty('command_id');
  expect(body).not.toHaveProperty('content');
  expect(body).not.toHaveProperty('recipient_ref');
});

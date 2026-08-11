/**
 * @Time       : 2026/08/10 21:30
 * @Author     : zhanglp8181
 * @File       : ConnectionsPage.test.tsx
 * @CallChain  : Vitest → ConnectionsPage → connection client contracts
 * @Description: 验证连接说明、企业微信完整引导、档案脱敏、健康探测和 Agent 绑定撤权。
 */

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, expect, it, vi } from 'vitest';

import {
  checkConnectionHealth,
  bindConnectorPrincipal,
  createConnectionBinding,
  createSlackConnection,
  createWeComConnection,
  getConnectorInboundRoute,
  listConnectorInboundEvents,
  listConnectionBindings,
  listConnectionProfiles,
  setConnectionBindingActions,
  setConnectionBindingState,
  setConnectorInboundRoute,
  startSlackOAuth,
} from '@/api/connections';
import { api } from '@/api/client';
import { I18nProvider } from '@/i18n';

import ConnectionsPage from './ConnectionsPage';

vi.mock('@/api/connections', () => ({
  checkConnectionHealth: vi.fn(),
  bindConnectorPrincipal: vi.fn(),
  createConnectionBinding: vi.fn(),
  createSlackConnection: vi.fn(),
  createWeComConnection: vi.fn(),
  disableConnection: vi.fn(),
  getConnectorInboundRoute: vi.fn(),
  listConnectorInboundEvents: vi.fn(),
  listConnectionBindings: vi.fn(),
  listConnectionProfiles: vi.fn(),
  reauthorizeConnection: vi.fn(),
  reauthorizeWeComConnection: vi.fn(),
  setConnectionBindingActions: vi.fn(),
  setConnectionBindingState: vi.fn(),
  setConnectorInboundRoute: vi.fn(),
  startSlackOAuth: vi.fn(),
}));

vi.mock('@/api/client', () => ({
  ApiError: class ApiError extends Error {
    status = 409;
  },
  getRequestTenantId: () => 'tenant_demo',
  api: { get: vi.fn() },
}));

const profile = {
  id: 'profile_a',
  tenant_id: 'tenant_demo',
  provider: 'slack' as const,
  account_id: 'T-A',
  display_name: '合同工作区',
  required_scopes: ['channels:read'],
  granted_scopes: ['channels:read'],
  tool_allowlist: ['slack.channel_info'],
  status: 'active' as const,
  health_status: 'healthy' as const,
  health_error_code: null,
  rate_limited_until: null,
  last_checked_at: '2026-08-10T12:00:00Z',
  last_healthy_at: '2026-08-10T12:00:00Z',
  secret_revision: 2,
  revision: 5,
  has_secret: true,
  callback_configured: false,
  created_at: '2026-08-10T11:00:00Z',
  updated_at: '2026-08-10T12:00:00Z',
};

const binding = {
  id: 'binding_a',
  tenant_id: 'tenant_demo',
  agent_id: 'agent_a',
  profile_id: 'profile_a',
  allowed_scopes: ['channels:read'],
  allowed_actions: [],
  enabled: true,
  revision: 3,
  created_at: '2026-08-10T11:00:00Z',
  updated_at: '2026-08-10T12:00:00Z',
};

function renderPage() {
  /** 使用真实国际化上下文渲染连接管理页。 */

  return render(
    <I18nProvider>
      <ConnectionsPage currentUser={{
        id: 'admin',
        tenant_id: 'tenant_demo',
        username: 'admin',
        role: 'admin',
        membership_status: 'active',
        member_category_code: 'employee',
      }} />
    </I18nProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(listConnectionProfiles).mockResolvedValue([profile]);
  vi.mocked(api.get).mockResolvedValue([
    { id: 'agent_a', tenant_id: 'tenant_demo', name: '合同数字员工', status: 'active' },
  ]);
  vi.mocked(listConnectionBindings).mockResolvedValue([binding]);
  vi.mocked(getConnectorInboundRoute).mockResolvedValue(null);
  vi.mocked(listConnectorInboundEvents).mockResolvedValue([]);
  vi.mocked(createSlackConnection).mockResolvedValue(profile);
  vi.mocked(createWeComConnection).mockResolvedValue({
    ...profile,
    provider: 'wecom',
    account_id: 'wecom_app_masked',
    required_scopes: ['application:read'],
    granted_scopes: ['application:read'],
    tool_allowlist: ['wecom.application_info'],
  });
  vi.mocked(checkConnectionHealth).mockResolvedValue(profile);
  vi.mocked(createConnectionBinding).mockResolvedValue(binding);
  vi.mocked(setConnectionBindingState).mockResolvedValue({ ...binding, enabled: false, revision: 4 });
  vi.mocked(setConnectionBindingActions).mockResolvedValue({
    ...binding,
    allowed_actions: ['wecom.message_send'],
    revision: 4,
  });
  vi.mocked(setConnectorInboundRoute).mockResolvedValue({
    id: 'route_a',
    tenant_id: 'tenant_demo',
    provider: 'wecom',
    profile_id: 'profile_a',
    agent_id: 'agent_a',
    enabled: true,
    revision: 1,
  });
  vi.mocked(bindConnectorPrincipal).mockResolvedValue({
    id: 'principal_a',
    tenant_id: 'tenant_demo',
    provider: 'wecom',
    profile_id: 'profile_a',
    user_id: 'user_a',
    enabled: true,
    revision: 1,
  });
  vi.mocked(startSlackOAuth).mockResolvedValue({
    authorize_url: 'https://slack.com/oauth/v2/authorize?state=test',
    expires_at: '2026-08-10T12:10:00Z',
  });
});

it('展示账号身份链但不显示 secret reference 或 token', async () => {
  /** 验证页面只展示 workspace、scope 和密钥修订元数据。 */

  renderPage();

  expect(await screen.findByText('合同工作区')).toBeInTheDocument();
  expect(screen.getByText('Slack · T-A')).toBeInTheDocument();
  expect(screen.getByText('v2')).toBeInTheDocument();
  expect(screen.getByText('channels:read')).toBeInTheDocument();
  expect(screen.queryByText(/xoxb/)).not.toBeInTheDocument();
  expect(screen.queryByText(/secret_ref/)).not.toBeInTheDocument();
});

it('用完整指南说明连接边界、接入步骤和数字员工演示场景', async () => {
  /** 验证用户能从页面理解为什么连接，并从指南直接进入企业微信建档。 */

  const user = userEvent.setup();
  renderPage();
  await screen.findByText('合同工作区');

  expect(screen.getByText('让员工在企业微信里发起任务，由受控的数字员工继续处理')).toBeInTheDocument();
  expect(screen.getByText(/不会自动读取通讯录/)).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '接入与演示指南' }));

  expect(await screen.findByText('企业微信连接：从创建到一次真实任务')).toBeInTheDocument();
  expect(screen.getByText('连接后可以')).toBeInTheDocument();
  expect(screen.getByText('连接后不会')).toBeInTheDocument();
  expect(screen.getByText('完整示例：创建企业微信连接并测试成功')).toBeInTheDocument();
  expect(screen.getByText('演示场景：让数字员工查询企业微信应用信息')).toBeInTheDocument();
  expect(screen.getByText('“测试动态任务：查询当前企业微信应用信息”')).toBeInTheDocument();
  expect(screen.getByText(/验签消息 → 确认发送者映射/)).toBeInTheDocument();
  expect(screen.getByText(/当前机器内网地址是/)).toHaveTextContent('192.168.124.236');
  expect(screen.getByText(/当前机器内网地址是/)).toHaveTextContent('103.62.49.138');
  expect(screen.getByText('curl -4 https://api.ipify.org')).toBeInTheDocument();
  expect(screen.getByText('真实成功回调 URL 是这样获得的')).toBeInTheDocument();
  expect(screen.getByText('cloudflared tunnel --url http://127.0.0.1:5137')).toBeInTheDocument();
  expect(screen.getByText('http://192.168.124.236:5137')).toBeInTheDocument();
  expect(screen.getByText('connprofile_36a56f3b292f49a8')).toBeInTheDocument();
  expect(screen.getByText(/企业微信显示“保存成功”才表示这个 URL 真实可用/)).toBeInTheDocument();
  expect(screen.getByText(/Quick Tunnel 每次重启通常会得到新域名/)).toHaveTextContent('103.62.49.138');
  expect(screen.getAllByText('103.62.49.138')).toHaveLength(4);
  expect(screen.getAllByText(/connprofile_36a56f3b292f49a8\/callback/)).toHaveLength(3);
  expect(screen.getByText('bbfca6••••••••••••••••••84c3f7')).toBeInTheDocument();
  expect(screen.getByText('URL、Token、EncodingAESKey 分别怎么生成')).toBeInTheDocument();
  expect(screen.getByText(/生成 16 字节，再编码为 32 位小写十六进制字符串/)).toBeInTheDocument();
  expect(screen.getByText(/不是公网 HTTPS 来源/)).toBeInTheDocument();

  await user.click(screen.getByRole('button', { name: '开始创建连接' }));
  expect(await screen.findByRole('dialog')).toHaveTextContent('连接企业微信');
  expect(screen.getByLabelText('企业 ID（CorpID）')).toBeInTheDocument();
});

it('流程节点可点击定位数字员工场景并展示三张真实界面截图', async () => {
  /** 验证首页流程不是静态装饰，用户可进入绑定、路由和使用说明并放大截图。 */

  const user = userEvent.setup();
  renderPage();
  await screen.findByText('合同工作区');

  await user.click(screen.getByRole('button', { name: '查看已绑定数字员工步骤' }));
  expect(await screen.findByText('场景角色与接入方式')).toBeInTheDocument();
  expect(screen.getByText(/选择本次真实使用的“平台能力演示助手”/)).toBeInTheDocument();
  expect(screen.getByText(/员工继续在企业微信应用会话中用自然语言提问/)).toBeInTheDocument();
  expect(screen.getByAltText('企业微信连接健康后的真实连接卡片')).toBeInTheDocument();
  expect(screen.getByAltText('在真实数字员工绑定弹窗中授权企业微信连接')).toHaveClass('w-auto', 'max-w-full');
  expect(screen.getByAltText('在真实消息接入弹窗中配置数字员工路由和发送者授权')).toHaveClass('w-auto', 'max-w-full');
  expect(screen.getAllByRole('link', { name: /放大查看实际截图/ })).toHaveLength(3);
});

it('默认创建企业微信连接并生成档案专属回调配置', async () => {
  /** 验证企业微信主入口同时提交三元凭据和浏览器安全随机生成的回调密钥。 */

  const user = userEvent.setup();
  renderPage();
  await screen.findByText('合同工作区');
  await user.click(screen.getByRole('button', { name: '连接企业微信' }));
  await user.type(screen.getByLabelText('连接显示名称'), '序伴集成测试');
  await user.type(screen.getByLabelText('企业 ID（CorpID）'), 'corp-a');
  await user.type(screen.getByLabelText('应用 AgentId'), '1000002');
  await user.type(screen.getByLabelText('应用 Secret'), 'wecom-new-secret');
  await user.click(screen.getByRole('button', { name: '验证并连接' }));

  await waitFor(() => expect(createWeComConnection).toHaveBeenCalledWith(expect.objectContaining({
    displayName: '序伴集成测试',
    corpId: 'corp-a',
    agentId: '1000002',
    corpSecret: 'wecom-new-secret',
    callbackToken: expect.stringMatching(/^[a-f0-9]{32}$/),
    callbackEncodingAesKey: expect.stringMatching(/^[A-Za-z0-9+/]{43}$/),
  })));
  expect(createSlackConnection).not.toHaveBeenCalled();
  expect(await screen.findByText('配置企业微信接收消息服务器')).toBeInTheDocument();
  expect(screen.getByText(/\/api\/connectors\/wecom\/profile_a\/callback/)).toBeInTheDocument();
});

it('创建弹窗提供 Slack OAuth 且在跳转前要求显示名称', async () => {
  /** 验证 OAuth 是明确用户交互入口，并保留建档显示语义。 */

  const user = userEvent.setup();
  renderPage();
  await screen.findByText('合同工作区');
  await user.click(screen.getByRole('button', { name: '连接企业微信' }));
  expect(screen.queryByRole('button', { name: '通过 Slack OAuth' })).not.toBeInTheDocument();
  await user.click(screen.getByRole('combobox', { name: '连接类型' }));
  await user.click(screen.getByRole('option', { name: 'Slack 工作区' }));

  expect(screen.getByRole('button', { name: '通过 Slack OAuth' })).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '通过 Slack OAuth' }));
  expect(startSlackOAuth).not.toHaveBeenCalled();
});

it('健康检查和单个 Agent 绑定撤权使用当前服务端修订', async () => {
  /** 验证管理动作按 profile/binding 的权威 ID 与 revision 提交。 */

  const user = userEvent.setup();
  renderPage();
  await screen.findByText('合同工作区');
  await user.click(screen.getByRole('button', { name: '健康检查' }));
  await waitFor(() => expect(checkConnectionHealth).toHaveBeenCalledWith('profile_a'));

  await user.click(screen.getByRole('button', { name: 'Agent 绑定' }));
  expect(await screen.findByText('合同数字员工')).toBeInTheDocument();
  await user.click(screen.getByRole('switch', { name: '合同数字员工绑定状态' }));

  await waitFor(() => expect(setConnectionBindingState).toHaveBeenCalledWith(
    'profile_a',
    binding,
    false,
  ));
});

it('企业微信审批后发送开关提交档案和绑定双修订', async () => {
  /** 验证管理端只显式授权固定写动作，不把只读 scope 当作发送许可。 */

  const user = userEvent.setup();
  const wecomProfile = {
    ...profile,
    provider: 'wecom' as const,
    required_scopes: ['application:read'],
    granted_scopes: ['application:read'],
    tool_allowlist: ['wecom.application_info'],
  };
  vi.mocked(listConnectionProfiles).mockResolvedValue([wecomProfile]);
  renderPage();
  await screen.findByText('合同工作区');
  await user.click(screen.getByRole('button', { name: 'Agent 绑定' }));
  await user.click(await screen.findByRole('switch', { name: '合同数字员工审批后发送' }));

  await waitFor(() => expect(setConnectionBindingActions).toHaveBeenCalledWith(
    wecomProfile,
    binding,
    true,
  ));
});

it('消息接入先保存唯一 Agent 路由，再从安全事件投影授权平台用户', async () => {
  /** 验证管理端不显示或提交原始企业微信 UserID，只提交事件 ID 与平台用户 ID。 */

  const user = userEvent.setup();
  vi.mocked(listConnectionProfiles).mockResolvedValue([{
    ...profile,
    provider: 'wecom',
    required_scopes: ['application:read'],
    granted_scopes: ['application:read'],
    tool_allowlist: ['wecom.application_info'],
    callback_configured: true,
  }]);
  vi.mocked(listConnectorInboundEvents).mockResolvedValue([{
    id: 'event_a',
    profile_id: 'profile_a',
    event_type: 'text',
    status: 'failed',
    attempt_count: 1,
    last_error_code: 'CONNECTOR_PRINCIPAL_UNRESOLVED',
    principal_bound: false,
    created_at: '2026-08-10T12:00:00Z',
  }]);
  vi.mocked(api.get).mockImplementation(async (path: string) => {
    if (path.startsWith('/api/auth/users')) {
      return [{
        id: 'user_a',
        username: 'zhang',
        display_name: '张丽鹏',
        membership_status: 'active',
      }];
    }
    return [{ id: 'agent_a', tenant_id: 'tenant_demo', name: '合同数字员工', status: 'active' }];
  });
  renderPage();
  await screen.findByText('合同工作区');
  await user.click(screen.getByRole('button', { name: '消息接入' }));

  await user.click(await screen.findByRole('combobox', { name: '接收消息的数字员工' }));
  await user.click(screen.getByRole('option', { name: '合同数字员工' }));
  await user.click(screen.getByRole('button', { name: '保存消息路由' }));
  await waitFor(() => expect(setConnectorInboundRoute).toHaveBeenCalledWith(
    expect.objectContaining({ id: 'profile_a', revision: 5 }),
    'agent_a',
  ));

  await user.click(screen.getByRole('combobox', { name: '选择事件event_a对应用户' }));
  await user.click(screen.getByRole('option', { name: '张丽鹏' }));
  await user.click(screen.getByRole('button', { name: '授权并恢复' }));
  await waitFor(() => expect(bindConnectorPrincipal).toHaveBeenCalledWith('event_a', 'user_a'));
  expect(screen.queryByText(/external-user/)).not.toBeInTheDocument();
});

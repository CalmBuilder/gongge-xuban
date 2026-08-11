/**
 * @Time       : 2026/08/10 21:20
 * @Author     : zhanglp8181
 * @File       : AttentionCenter.test.tsx
 * @CallChain  : Vitest → AttentionCenter → Attention/Execution API 契约
 * @Description: 验证动态澄清和连接重授权的专用办理、CAS 命令及异常响应降级。
 */

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, expect, it, vi } from 'vitest';

import { api } from '@/api/client';
import { I18nProvider } from '@/i18n';

import AttentionCenter from './AttentionCenter';

vi.mock('@/api/client', () => ({
  getRequestTenantId: () => 'tenant_demo',
  api: { get: vi.fn(), post: vi.fn() },
}));

const clarification = {
  id: 'attention_1',
  execution_id: 'execution_1',
  session_id: 'session_1',
  kind: 'clarification',
  title: '确认报告范围',
  payload: { question: '报告需要覆盖哪个区域？', options: ['华东', '全国'] },
  available_commands: ['answer', 'cancel'],
  resolution: {},
  status: 'offered',
  revision: 3,
  created_at: '2026-08-04T00:00:00Z',
  updated_at: '2026-08-04T00:01:00Z',
};

const reauth = {
  ...clarification,
  id: 'attention_reauth',
  kind: 'reauth',
  title: '重新授权合同工作区',
  payload: {
    profile_id: 'profile_slack',
    profile_revision: 8,
    secret_revision: 2,
    account_id: 'T-A',
    reason_code: 'CONNECTION_TOKEN_EXPIRED',
  },
  available_commands: ['reauthorize'],
  revision: 4,
};

const wecomReauth = {
  ...reauth,
  id: 'attention_wecom_reauth',
  title: '重新授权企业微信测试应用',
  payload: {
    ...reauth.payload,
    provider: 'wecom',
    profile_id: 'profile_wecom',
    account_id: 'wecom_app_masked',
    reason_code: 'CONNECTION_INVALID_AUTH',
  },
};

const toolApproval = {
  ...clarification,
  id: 'attention_write',
  kind: 'tool_approval',
  title: '批准企业微信消息发送',
  payload: {
    content: '审批后发送的精确消息',
    content_checksum: 'abcdef0123456789',
    canonical_target: 'wecom_thread:opaque-thread',
  },
  available_commands: ['allow_once', 'deny'],
  revision: 5,
};

const workspaceApproval = {
  ...toolApproval,
  id: 'attention_workspace_write',
  title: '批准受管代码工作区变更',
  payload: {
    operation_name: 'workspace.refund.apply',
    workspace: {
      workspace_id: 'refund-demo',
      base_ref: 'main',
      handler: 'apply_file',
    },
    arguments: {
      path: 'refund.py',
      expected_sha256: 'a'.repeat(64),
      content: "STATUS = 'approval_required'\n",
    },
  },
  revision: 7,
};

const writeException = {
  ...clarification,
  id: 'attention_write_exception',
  kind: 'exception',
  title: '核对企业微信消息是否送达',
  payload: {
    operation_name: 'wecom.message_send@profile_a',
    error_code: 'WECOM_DELIVERY_UNKNOWN',
  },
  available_commands: ['confirm_applied', 'confirm_not_applied'],
  revision: 6,
};

function renderCenter() {
  /** 使用真实国际化上下文渲染由共享 Textarea 组成的办理弹窗。 */

  return render(
    <I18nProvider>
      <AttentionCenter />
    </I18nProvider>,
  );
}

beforeEach(() => {
  vi.mocked(api.get).mockReset();
  vi.mocked(api.post).mockReset();
  vi.mocked(api.get).mockImplementation(async (path: string) => {
    if (path.startsWith('/api/executions/')) {
      return {
        id: 'execution_1',
        status: 'waiting',
        revision: 7,
        effect_state: 'none',
        goal: '生成合同风险简报',
        budget: { max_model_calls: 8 },
        usage: { model_calls: 3 },
        steps: [
          { step_key: 'read', title: '读取合同', kind: 'tool.read', status: 'completed' },
          { step_key: 'clarify', title: '确认区域', kind: 'clarification', status: 'waiting' },
        ],
      };
    }
    return { items: [clarification], total: 1 };
  });
  vi.mocked(api.post).mockResolvedValue({ status: 'completed' });
});

it('展示动态澄清问题并读取原执行的权威状态', async () => {
  /** 验证事项详情不会根据聊天文本猜测执行状态。 */

  const user = userEvent.setup();
  renderCenter();

  await user.click(await screen.findByRole('button', { name: /确认报告范围/ }));

  expect(screen.getAllByText('报告需要覆盖哪个区域？')).toHaveLength(2);
  expect(screen.getByText('状态 waiting · 修订 7')).toBeInTheDocument();
  expect(screen.getByText('生成合同风险简报')).toBeInTheDocument();
  expect(screen.getByText('模型调用 3/8')).toBeInTheDocument();
  expect(screen.getByText('读取合同')).toBeInTheDocument();
  expect(screen.getAllByText('确认区域').length).toBeGreaterThan(0);
  expect(screen.getByRole('button', { name: '华东' })).toBeInTheDocument();
  expect(api.get).toHaveBeenCalledWith(
    '/api/executions/execution_1?tenant_id=tenant_demo',
  );
});

it('用事项修订号和唯一命令号提交答案后继续原执行', async () => {
  /** 验证 answer 命令携带 tenant、CAS revision 和用户补充内容。 */

  const user = userEvent.setup();
  renderCenter();
  await user.click(await screen.findByRole('button', { name: /确认报告范围/ }));
  await user.click(screen.getByRole('button', { name: '华东' }));
  await user.click(screen.getByRole('button', { name: '补充并继续' }));

  await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
  const [path, body] = vi.mocked(api.post).mock.calls[0];
  expect(path).toBe('/api/attention-items/attention_1/resolve');
  expect(body).toMatchObject({
    tenant_id: 'tenant_demo',
    command: 'answer',
    expected_revision: 3,
    comment: '华东',
  });
  expect(body).toHaveProperty('command_id');
  expect(String((body as Record<string, unknown>).command_id)).not.toHaveLength(0);
});

it('取消命令不伪造补充内容且保留 CAS 保护', async () => {
  /** 验证 cancel 使用同一受控办理入口并省略空 comment。 */

  const user = userEvent.setup();
  renderCenter();
  await user.click(await screen.findByRole('button', { name: /确认报告范围/ }));
  await user.click(screen.getByRole('button', { name: '取消任务' }));

  await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
  expect(vi.mocked(api.post).mock.calls[0][1]).toMatchObject({
    tenant_id: 'tenant_demo',
    command: 'cancel',
    expected_revision: 3,
  });
  expect(vi.mocked(api.post).mock.calls[0][1]).not.toHaveProperty('comment');
});

it('原子提交新凭据和 reauth Attention 后恢复原执行', async () => {
  /** 验证页面不把 token 放入通用 comment，而是调用原子连接恢复命令。 */

  vi.mocked(api.get).mockImplementation(async (path: string) => {
    if (path.startsWith('/api/executions/')) {
      return {
        id: 'execution_1',
        status: 'waiting',
        revision: 9,
        effect_state: 'none',
      };
    }
    return { items: [reauth], total: 1 };
  });
  const user = userEvent.setup();
  renderCenter();

  await user.click(await screen.findByRole('button', { name: /重新授权合同工作区/ }));
  expect(screen.getByRole('button', { name: '通过 Slack OAuth' })).toBeInTheDocument();
  await user.type(screen.getByLabelText('新的 Slack Bot Token'), 'xoxb-new-secret');
  await user.click(screen.getByRole('button', { name: '验证并恢复任务' }));

  await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
  const [path, body] = vi.mocked(api.post).mock.calls[0];
  expect(path).toBe(
    '/api/enterprise/connection-profiles/profile_slack/reauthorize-attention/attention_reauth',
  );
  expect(body).toMatchObject({
    tenant_id: 'tenant_demo',
    expected_revision: 8,
    attention_expected_revision: 4,
    token: 'xoxb-new-secret',
  });
  expect(body).toHaveProperty('command_id');
});

it('企业微信重授权不显示 Slack OAuth 并原子提交三元凭据', async () => {
  /** 验证企业微信等待任务使用同一 Attention CAS 闭环且不混用 OAuth/token 语义。 */

  vi.mocked(api.get).mockImplementation(async (path: string) => {
    if (path.startsWith('/api/executions/')) {
      return { id: 'execution_1', status: 'waiting', revision: 9, effect_state: 'none' };
    }
    return { items: [wecomReauth], total: 1 };
  });
  const user = userEvent.setup();
  renderCenter();

  await user.click(await screen.findByRole('button', { name: /重新授权企业微信测试应用/ }));
  expect(screen.queryByRole('button', { name: '通过 Slack OAuth' })).not.toBeInTheDocument();
  await user.type(screen.getByLabelText('企业 ID（CorpID）'), 'corp-a');
  await user.type(screen.getByLabelText('应用 AgentId'), '1000002');
  await user.type(screen.getByLabelText('应用 Secret'), 'wecom-new-secret');
  await user.click(screen.getByRole('button', { name: '验证并恢复任务' }));

  await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
  const [path, body] = vi.mocked(api.post).mock.calls[0];
  expect(path).toBe(
    '/api/enterprise/connection-profiles/profile_wecom/reauthorize-attention/attention_wecom_reauth',
  );
  expect(body).toMatchObject({
    tenant_id: 'tenant_demo',
    expected_revision: 8,
    attention_expected_revision: 4,
    corp_id: 'corp-a',
    agent_id: '1000002',
    corp_secret: 'wecom-new-secret',
  });
  expect(body).not.toHaveProperty('token');
  expect(body).toHaveProperty('command_id');
});

it('一次性写审批展示精确正文并仅提交批准命令', async () => {
  /** 验证审批人看到冻结内容与摘要，浏览器不重新提交正文或外部收件人。 */

  vi.mocked(api.get).mockImplementation(async (path: string) => {
    if (path.startsWith('/api/executions/')) {
      return { id: 'execution_1', status: 'waiting', revision: 10, effect_state: 'none' };
    }
    return { items: [toolApproval], total: 1 };
  });
  const user = userEvent.setup();
  renderCenter();
  await user.click(await screen.findByRole('button', { name: /批准企业微信消息发送/ }));

  expect(screen.getByText('审批后发送的精确消息')).toBeInTheDocument();
  expect(screen.getByText('abcdef012345')).toBeInTheDocument();
  expect(screen.queryByText(/opaque-thread/)).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '仅批准本次发送' }));

  await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
  const [path, body] = vi.mocked(api.post).mock.calls[0];
  expect(path).toBe('/api/attention-items/attention_write/resolve');
  expect(body).toMatchObject({
    tenant_id: 'tenant_demo',
    command: 'allow_once',
    expected_revision: 5,
  });
  expect(body).not.toHaveProperty('content');
  expect(body).not.toHaveProperty('recipient_ref');
});

it('受管代码审批展示工作区与精确参数并使用通用操作文案', async () => {
  /** 验证代码审批不会伪装企业微信发送，且浏览器仍只提交 CAS 决定。 */

  vi.mocked(api.get).mockImplementation(async (path: string) => {
    if (path.startsWith('/api/executions/')) {
      return { id: 'execution_1', status: 'waiting', revision: 12, effect_state: 'none' };
    }
    return { items: [workspaceApproval], total: 1 };
  });
  const user = userEvent.setup();
  renderCenter();
  await user.click(await screen.findByRole('button', { name: /批准受管代码工作区变更/ }));

  expect(screen.getByLabelText('待批准受管代码操作')).toHaveTextContent('refund-demo');
  expect(screen.getByLabelText('待批准受管代码操作')).toHaveTextContent('apply_file');
  expect(screen.getByLabelText('待批准受管代码操作')).toHaveTextContent('refund.py');
  expect(screen.queryByText('目标：当前企业微信会话')).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '仅批准本次操作' }));

  await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
  expect(vi.mocked(api.post).mock.calls[0]).toEqual([
    '/api/attention-items/attention_workspace_write/resolve',
    expect.objectContaining({
      tenant_id: 'tenant_demo',
      command: 'allow_once',
      expected_revision: 7,
    }),
  ]);
  expect(vi.mocked(api.post).mock.calls[0][1]).not.toHaveProperty('arguments');
});

it('未知外部效果必须填写证据后人工收敛且页面不提供重发', async () => {
  /** 验证 unknown 只能确认效果存在/不存在，空证据不会产生恢复命令。 */

  vi.mocked(api.get).mockImplementation(async (path: string) => {
    if (path.startsWith('/api/executions/')) {
      return { id: 'execution_1', status: 'waiting', revision: 11, effect_state: 'unknown' };
    }
    return { items: [writeException], total: 1 };
  });
  const user = userEvent.setup();
  renderCenter();
  await user.click(await screen.findByRole('button', { name: /核对企业微信消息是否送达/ }));

  expect(screen.queryByRole('button', { name: /重发/ })).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '确认已送达' }));
  expect(api.post).not.toHaveBeenCalled();
  await user.type(
    screen.getByRole('textbox', { name: '外部对账证据（必填）' }),
    '企业微信客户端已核对到唯一消息',
  );
  await user.click(screen.getByRole('button', { name: '确认已送达' }));

  await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
  expect(vi.mocked(api.post).mock.calls[0][1]).toMatchObject({
    command: 'confirm_applied',
    expected_revision: 6,
    comment: '企业微信客户端已核对到唯一消息',
  });
});

it('过滤仍由原任务箱办理的 SOP 事项并安全忽略畸形响应', async () => {
  /** 防止统一页面改变既有 SOP claim/quorum 语义或因旧响应崩溃。 */

  vi.mocked(api.get).mockResolvedValue({
    items: [{ ...clarification, id: 'sop_1', kind: 'sop_human_task' }, { id: 'broken' }],
    total: 2,
  });
  renderCenter();

  expect(await screen.findByText('当前没有需要你处理的事项')).toBeInTheDocument();
  expect(screen.queryByText('确认报告范围')).not.toBeInTheDocument();
});

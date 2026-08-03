/**
 * @Time       : 2026/08/04 07:20
 * @Author     : zhanglp8181
 * @File       : AttentionCenter.test.tsx
 * @CallChain  : Vitest → AttentionCenter → Attention/Execution API 契约
 * @Description: 验证动态澄清事项展示、CAS 办理命令、取消与异常响应降级。
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

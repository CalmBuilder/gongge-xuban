/**
 * @Time       : 2026/08/04 17:12
 * @Author     : zhanglp8181
 * @File       : DynamicExecutionControl.test.tsx
 * @CallChain  : Vitest → 动态执行卡 → Execution command API
 * @Description: 验证显式追加约束、CAS 信封、命令状态和终态控制隐藏。
 */

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, expect, it, vi } from 'vitest';

import { api } from '@/api/client';
import { I18nProvider } from '@/i18n';

import DynamicExecutionControl from './DynamicExecutionControl';

vi.mock('@/api/client', () => ({
  getRequestTenantId: () => 'tenant_demo',
  api: { get: vi.fn(), post: vi.fn(), blob: vi.fn() },
}));

beforeEach(() => {
  vi.mocked(api.get).mockReset();
  vi.mocked(api.post).mockReset();
  vi.mocked(api.blob).mockReset();
  vi.mocked(api.get).mockResolvedValue({
    id: 'execution_1',
    kind: 'dynamic_task',
    status: 'running',
    revision: 12,
    agent_id: 'agent_demo',
    session_id: 'session_demo',
    plan_revision_number: 2,
    goal: '生成合同风险简报',
    current_step_key: 'read_contract',
    parallel_waves: [],
  });
});

it('从当前会话可用目录给运行中任务增加固定 Skill', async () => {
  /** 页面只提交服务端 Skill ID；正文、修订和授权均由运行时重新固定与校验。 */

  vi.mocked(api.get).mockImplementation(async (path: string) => {
    if (path.includes('/general-skills?agent_id=')) {
      return {
        session_id: 'session_demo',
        agent_id: 'agent_demo',
        items: [
          {
            skill_id: 'skill_writing',
            revision_id: 'revision_writing_3',
            revision_number: 3,
            name: 'writing-for-agents',
            description: '把复杂任务写成可执行规范',
            invocation_policy: 'model_allowed',
            revision_policy: 'pinned',
            enabled: true,
          },
          {
            skill_id: 'skill_muted',
            revision_id: 'revision_muted',
            revision_number: 1,
            name: '已静音 Skill',
            description: '不应允许追加',
            invocation_policy: 'user_only',
            revision_policy: 'pinned',
            enabled: false,
          },
        ],
      };
    }
    return {
      id: 'execution_1', kind: 'dynamic_task', status: 'running', revision: 12,
      agent_id: 'agent_demo', session_id: 'session_demo', plan_revision_number: 2,
      goal: '生成合同风险简报', parallel_waves: [],
    };
  });
  vi.mocked(api.post).mockResolvedValue({
    command_id: 'add_skill_1', command_type: 'add_skill', status: 'pending',
  });
  const user = userEvent.setup();
  renderControl();

  await user.click(await screen.findByRole('button', { name: '运行中增加 Skill' }));
  expect(await screen.findByText('writing-for-agents')).toBeInTheDocument();
  expect(screen.queryByText('已静音 Skill')).not.toBeInTheDocument();
  await user.click(screen.getByRole('radio', { name: /writing-for-agents/ }));
  await user.click(screen.getByRole('button', { name: '确认增加 Skill' }));

  await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
  expect(vi.mocked(api.post).mock.calls[0][1]).toMatchObject({
    tenant_id: 'tenant_demo',
    command_type: 'add_skill',
    expected_revision: 12,
    payload: { skill_id: 'skill_writing', trigger: 'user' },
  });
  expect(await screen.findByText('Skill 等待安全边界加载')).toBeInTheDocument();
});

it('没有可用 Skill 时禁止提交，并展示真实并行波次与计划版本', async () => {
  /** 空目录不能构造 add_skill 命令；并行提示必须来自持久批次而非前端计时推测。 */

  vi.mocked(api.get).mockImplementation(async (path: string) => {
    if (path.includes('/general-skills?agent_id=')) {
      return { session_id: 'session_demo', agent_id: 'agent_demo', items: [] };
    }
    return {
      id: 'execution_1', kind: 'dynamic_task', status: 'running', revision: 18,
      agent_id: 'agent_demo', session_id: 'session_demo', plan_revision_number: 4,
      goal: '并行核验合同与供应商',
      parallel_waves: [{
        id: 'wave_1', status: 'succeeded', parallelism: 2,
        ordered_step_keys: ['read_contract', 'read_partner'],
      }],
    };
  });
  const user = userEvent.setup();
  renderControl();

  expect(await screen.findByText(/计划 v4/)).toBeInTheDocument();
  expect(screen.getByText('并行读取 · 2 路 · 已完成')).toBeInTheDocument();
  expect(screen.getByText('read_contract → read_partner')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '运行中增加 Skill' }));
  expect(await screen.findByText('当前会话没有可追加的 Skill')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '确认增加 Skill' })).toBeDisabled();
  expect(api.post).not.toHaveBeenCalled();
});

it('终态执行展示权威 Artifact 并通过鉴权 API 下载', async () => {
  /** 下载只能使用 artifact id，不能把服务端存储路径暴露给浏览器。 */

  vi.mocked(api.get).mockResolvedValue({
    id: 'execution_1',
    kind: 'dynamic_task',
    status: 'succeeded',
    revision: 20,
    goal: '生成合同风险简报',
    artifacts: [
      { id: 'artifact_1', filename: '续约风险简报.md', mime_type: 'text/markdown', size_bytes: 128 },
    ],
  });
  vi.mocked(api.blob).mockResolvedValue(new Blob(['brief'], { type: 'text/markdown' }));
  const createObjectURL = vi.fn(() => 'blob:artifact');
  const revokeObjectURL = vi.fn();
  vi.stubGlobal('URL', { ...URL, createObjectURL, revokeObjectURL });
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
  const user = userEvent.setup();
  renderControl();

  await user.click(await screen.findByRole('button', { name: /续约风险简报.md/ }));

  expect(api.blob).toHaveBeenCalledWith(
    '/api/artifacts/artifact_1/download?tenant_id=tenant_demo',
  );
  expect(createObjectURL).toHaveBeenCalledTimes(1);
  expect(click).toHaveBeenCalledTimes(1);
  await waitFor(() => expect(revokeObjectURL).toHaveBeenCalledTimes(1));
  click.mockRestore();
  vi.unstubAllGlobals();
});

it('正式SOP复用执行卡展示并下载XLSX且不暴露Dynamic控制', async () => {
  /** SOP与Dynamic共用Artifact权限边界，但SOP不得出现追加Skill或重规划按钮。 */

  vi.mocked(api.get).mockResolvedValue({
    id: 'execution_sop_1',
    kind: 'sop',
    status: 'succeeded',
    revision: 8,
    session_id: 'session_sop',
    artifacts: [
      {
        id: 'artifact_sales',
        filename: '销售核验报告.xlsx',
        mime_type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        size_bytes: 4096,
      },
    ],
  });
  vi.mocked(api.blob).mockResolvedValue(new Blob(['PK'], { type: 'application/octet-stream' }));
  const createObjectURL = vi.fn(() => 'blob:sop-artifact');
  const revokeObjectURL = vi.fn();
  vi.stubGlobal('URL', { ...URL, createObjectURL, revokeObjectURL });
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
  const user = userEvent.setup();
  renderControl();

  expect(await screen.findByLabelText('SOP执行结果')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '运行中增加 Skill' })).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: /销售核验报告.xlsx/ }));

  expect(api.blob).toHaveBeenCalledWith(
    '/api/artifacts/artifact_sales/download?tenant_id=tenant_demo',
  );
  expect(click).toHaveBeenCalledTimes(1);
  click.mockRestore();
  vi.unstubAllGlobals();
});

function renderControl() {
  /** 使用真实 i18n 上下文覆盖共享 Textarea 与按钮渲染。 */

  return render(
    <I18nProvider>
      <DynamicExecutionControl executionId="execution_1" />
    </I18nProvider>,
  );
}

it('以当前 Execution revision 提交显式约束并展示等待应用状态', async () => {
  /** 用户文本必须进入 steer 命令 payload，不能退化成普通聊天消息。 */

  vi.mocked(api.post).mockResolvedValue({
    command_id: 'steer_1',
    command_type: 'steer',
    status: 'pending',
  });
  const user = userEvent.setup();
  renderControl();

  await user.click(await screen.findByRole('button', { name: /追加约束/ }));
  await user.type(screen.getByPlaceholderText(/只分析 2026 年内到期合同/), '排除已终止合同');
  await user.click(screen.getByRole('button', { name: '提交约束' }));

  await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
  const [path, body] = vi.mocked(api.post).mock.calls[0];
  expect(path).toBe('/api/executions/execution_1/commands');
  expect(body).toMatchObject({
    tenant_id: 'tenant_demo',
    command_type: 'steer',
    expected_revision: 12,
    payload: { instruction: '排除已终止合同' },
  });
  expect(body).toHaveProperty('command_id');
  expect(await screen.findByText('约束等待安全边界应用')).toBeInTheDocument();
});

it('展示冲突处置且终态 Execution 不再提供修改按钮', async () => {
  /** 冲突必须明确要求用户基于新修订重试，成功终态不能继续发命令。 */

  vi.mocked(api.post).mockResolvedValue({
    command_id: 'steer_conflict',
    command_type: 'steer',
    status: 'conflicted',
    reason_code: 'STEER_PLAN_REVISION_CONFLICT',
  });
  const user = userEvent.setup();
  const view = renderControl();
  await user.click(await screen.findByRole('button', { name: /追加约束/ }));
  await user.type(screen.getByRole('textbox'), '切换处理顺序');
  await user.click(screen.getByRole('button', { name: '提交约束' }));
  expect(await screen.findByText('当前计划已变化，请确认最新状态后重新提交')).toBeInTheDocument();

  vi.mocked(api.get).mockResolvedValue({
    id: 'execution_1',
    kind: 'dynamic_task',
    status: 'succeeded',
    revision: 20,
    goal: '生成合同风险简报',
  });
  view.unmount();
  renderControl();
  expect(await screen.findByText('已完成')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /追加约束/ })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '取消任务' })).not.toBeInTheDocument();
});

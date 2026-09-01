import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { I18nProvider } from '../i18n';
import PersonaPage, { validateUiConfigForm } from './PersonaPage';

const client = vi.hoisted(() => ({
  get: vi.fn(),
  put: vi.fn(),
}));

vi.mock('../api/client', async (importOriginal) => {
  const original = await importOriginal<typeof import('../api/client')>();
  return {
    ...original,
    api: client,
    getRequestTenantId: () => 'tenant_demo',
  };
});

const uiConfig = {
  tenant_id: 'tenant_demo',
  show_thinking_trace: true,
  show_skill_trace: true,
  show_tool_trace: true,
  reflection_max_rounds: 1,
  agent_loop_max_actions: 6,
  context_token_budget: 128000,
  context_compaction_trigger_ratio: 0.7,
  context_recent_round_limit: 6,
  long_summary_token_budget: 4000,
  medium_summary_token_budget: 4000,
  updated_at: '2026-09-01T00:00:00Z',
};

function renderPersonaPage() {
  /** 渲染不依赖路由的岗位人设页，复用真实配置表单和 UI 组件。 */

  return render(
    <I18nProvider>
      <PersonaPage />
    </I18nProvider>,
  );
}

beforeEach(() => {
  client.get.mockReset();
  client.put.mockReset();
  client.get.mockImplementation(async (path: string) => {
    if (path.includes('/api/enterprise/agents')) return [];
    if (path.includes('/api/enterprise/ui-config')) return uiConfig;
    return {
      tenant_id: 'tenant_demo',
      system_prompt: '组织默认岗位人设',
      updated_at: '2026-09-01T00:00:00Z',
    };
  });
  client.put.mockResolvedValue(uiConfig);
});

describe('岗位人设页上下文配置', () => {
  it('loads and saves the widened context and summary budget contract', async () => {
    /** 正向验证页面展示 128K 默认值，并把新配置字段完整提交给后端。 */

    const user = userEvent.setup();
    renderPersonaPage();

    const contextBudget = await screen.findByRole('spinbutton', { name: '上下文最大 token' });
    expect(contextBudget).toHaveValue(128000);
    expect(screen.getByRole('spinbutton', { name: '长期摘要 token' })).toHaveValue(4000);
    expect(screen.getByRole('spinbutton', { name: '近期摘要 token' })).toHaveValue(4000);
    expect(screen.getByText(/实际模型可能更小/)).toBeInTheDocument();

    await user.clear(contextBudget);
    await user.type(contextBudget, '131072');
    await user.click(screen.getByRole('button', { name: '保存设置' }));

    await waitFor(() => {
      expect(client.put).toHaveBeenCalledWith(
        '/api/enterprise/ui-config',
        expect.objectContaining({
          context_token_budget: 131072,
          context_compaction_trigger_ratio: 0.7,
          context_recent_round_limit: 6,
          long_summary_token_budget: 4000,
          medium_summary_token_budget: 4000,
        }),
      );
    });
  });

  it('blocks summary budgets whose sum exceeds the context budget', async () => {
    /** 反向验证页面在提交前拒绝摘要预算总量越界，不调用保存接口。 */

    const user = userEvent.setup();
    renderPersonaPage();

    const contextBudget = await screen.findByRole('spinbutton', { name: '上下文最大 token' });
    const longSummaryBudget = screen.getByRole('spinbutton', { name: '长期摘要 token' });
    const mediumSummaryBudget = screen.getByRole('spinbutton', { name: '近期摘要 token' });
    await user.clear(contextBudget);
    await user.type(contextBudget, '4096');
    await user.clear(longSummaryBudget);
    await user.type(longSummaryBudget, '3000');
    await user.clear(mediumSummaryBudget);
    await user.type(mediumSummaryBudget, '2000');
    await user.click(screen.getByRole('button', { name: '保存设置' }));

    expect(client.put).not.toHaveBeenCalled();
  });
});

describe('validateUiConfigForm', () => {
  it('accepts the documented minimums and maximums', () => {
    /** 正向验证前端表单边界与后端契约的上下限保持一致。 */

    expect(validateUiConfigForm({
      show_thinking_trace: true,
      show_skill_trace: true,
      show_tool_trace: true,
      reflection_max_rounds: '0',
      agent_loop_max_actions: '20',
      context_token_budget: '262144',
      context_compaction_trigger_ratio: '0.10',
      context_recent_round_limit: '50',
      long_summary_token_budget: '32768',
      medium_summary_token_budget: '32768',
    })).toBeNull();
  });

  it('rejects non-integers, out-of-range values, and an overflowing sum', () => {
    /** 反向验证前端不允许非法数字或两项摘要合计超过上下文预算。 */

    const valid = {
      show_thinking_trace: true,
      show_skill_trace: true,
      show_tool_trace: true,
      reflection_max_rounds: '1',
      agent_loop_max_actions: '6',
      context_token_budget: '4096',
      context_compaction_trigger_ratio: '0.70',
      context_recent_round_limit: '1',
      long_summary_token_budget: '3000',
      medium_summary_token_budget: '2000',
    };

    expect(validateUiConfigForm(valid)).toContain('摘要预算');
    expect(validateUiConfigForm({ ...valid, context_token_budget: '262145' })).toContain('安全范围');
    expect(validateUiConfigForm({ ...valid, context_compaction_trigger_ratio: '0.09' })).toContain('安全范围');
    expect(validateUiConfigForm({ ...valid, long_summary_token_budget: '127' })).toContain('安全范围');
  });
});

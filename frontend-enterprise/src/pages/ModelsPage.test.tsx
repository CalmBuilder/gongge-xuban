import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../api/client';
import type { EnterpriseAuthUser } from '../auth';
import { I18nProvider } from '../i18n';
import ModelsPage, { modelActionError } from './ModelsPage';

const client = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
}));

vi.mock('../api/client', async (importOriginal) => {
  const original = await importOriginal<typeof import('../api/client')>();
  return {
    ...original,
    api: client,
    getRequestTenantId: () => 'tenant_a',
  };
});

vi.mock('@/components/AppHeader', () => ({
  default: () => <header>模型配置测试页</header>,
}));

const administrator: EnterpriseAuthUser = {
  id: 'admin_a',
  tenant_id: 'tenant_a',
  username: 'admin',
  role: 'admin',
  membership_status: 'active',
  member_category_code: 'employee',
  governance_permission_codes: ['model_config.manage'],
};

const model = {
  id: 'model_a',
  tenant_id: 'tenant_a',
  name: 'DeepSeek 生产模型',
  provider: 'openai_compatible',
  base_url: 'https://api.deepseek.com',
  api_key_masked: 'sk-****1234',
  model: 'deepseek-v4-flash',
  temperature: 0.2,
  max_output_tokens: 8192,
  extra_body: {},
  is_default: true,
  enabled: true,
  updated_at: '2026-08-14T00:00:00Z',
};

beforeEach(() => {
  client.get.mockReset();
  client.post.mockReset();
  client.get.mockResolvedValue([model]);
});

describe('modelActionError', () => {
  it('将默认模型并发冲突转换为可操作提示', () => {
    const error = new ApiError(
      409,
      JSON.stringify({ detail: 'MODEL_DEFAULT_CONFLICT' }),
      'Conflict',
    );

    expect(modelActionError(error, '保存失败')).toBe('默认模型状态已变化，请刷新后重试');
  });

  it('保留普通接口错误信息和无信息时的回退文案', () => {
    expect(modelActionError(new Error('模型名称无效'), '保存失败')).toBe('模型名称无效');
    expect(modelActionError(null, '保存失败')).toBe('保存失败');
  });
});

describe('ModelsPage 连接诊断', () => {
  it('从列表直接测试并展示分阶段计费诊断，不泄露密钥', async () => {
    /** 验证模型连接诊断是一等操作，并把账户不可用与网络故障明确区分。 */

    client.post.mockResolvedValue({
      success: false,
      message: '当前 API Key 所属账户不可用。',
      error_code: 'BILLING_UNAVAILABLE',
      http_status: 402,
      endpoint: 'https://api.deepseek.com',
      model: 'deepseek-v4-flash',
      suggestion: '请确认充值的是这把 API Key 所属账户或项目，充值后重新测试。',
      checks: [
        { name: '配置', status: 'passed', message: '配置字段完整且密钥可解密。' },
        { name: '模型目录', status: 'passed', message: '认证成功并找到目标模型。' },
        { name: '账户状态', status: 'failed', message: 'DeepSeek 返回 is_available=false。' },
        { name: '最小生成', status: 'skipped', message: '账户不可用，未继续消耗额度。' },
      ],
    });

    render(
      <MemoryRouter initialEntries={['/enterprise/models']}>
        <I18nProvider>
          <ModelsPage currentUser={administrator} />
        </I18nProvider>
      </MemoryRouter>,
    );

    const user = userEvent.setup();
    const buttons = await screen.findAllByRole('button', { name: '连接测试 DeepSeek 生产模型' });
    await user.click(buttons[0]);

    expect(await screen.findByRole('dialog', { name: '模型连接诊断' })).toBeVisible();
    expect(screen.getByText('BILLING_UNAVAILABLE')).toBeVisible();
    expect(screen.getByText('DeepSeek 返回 is_available=false。')).toBeVisible();
    expect(screen.getByText(/请确认充值的是这把 API Key 所属账户或项目/)).toBeVisible();
    expect(document.body).not.toHaveTextContent('sk-live-secret-value');
    await waitFor(() => {
      expect(client.post).toHaveBeenCalledWith(
        '/api/enterprise/model-configs/model_a/test?tenant_id=tenant_a',
      );
    });
  });
});

/**
 * @Time       : 2026/09/03 10:30
 * @Author     : zhanglp8181
 * @File       : VerticalAbilitySettingsPage.test.tsx
 * @CallChain  : Vitest → 能力包配置首页/详情 → Provider 配置 API
 * @Description: 验证能力包卡片目录、详情分组和多 Provider 凭据保存/验证交互。
 */

import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import type { EnterpriseAuthUser } from '@/auth';
import { I18nProvider } from '@/i18n';
import { EnterpriseRoute } from '@/enums/routes';

import VerticalAbilitySettingsPage from './VerticalAbilitySettingsPage';


const client = vi.hoisted(() => ({
  get: vi.fn(),
  put: vi.fn(),
  post: vi.fn(),
}));

vi.mock('@/api/client', async (importOriginal) => {
  const original = await importOriginal<typeof import('@/api/client')>();
  return { ...original, api: client };
});

vi.mock('@/components/AppHeader', () => ({
  default: () => <header data-testid="app-header" />,
}));

const administrator: EnterpriseAuthUser = {
  id: 'admin_a',
  tenant_id: 'tenant_a',
  username: 'admin',
  role: 'admin',
  membership_status: 'active',
  member_category_code: 'employee',
};

const configured = {
  provider: 'jina' as const,
  ability_ids: ['web-intelligence'],
  ability_names: ['全网情报员'],
  provider_name: 'Jina Reader',
  description: '读取公开网页正文，为全网情报员提供可引用的原文内容。',
  credential_label: 'Jina API Key',
  credential_placeholder: '粘贴 Jina API Key',
  credential_setup_text: '打开 Jina 官方控制台，登录并创建 API Key：',
  credential_setup_url: 'https://jina.ai/api-dashboard',
  credential_setup_link_label: 'Jina API Dashboard',
  supports_verification: true,
  configured: true,
  source: 'database' as const,
  credential_masked: 'jin-****ored',
  revision: 2,
  health_status: 'healthy' as const,
  health_error_code: null,
  last_checked_at: '2026-09-02T06:00:00Z',
  last_healthy_at: '2026-09-02T06:00:00Z',
  updated_at: '2026-09-02T06:00:00Z',
};

const groqConfigured = {
  ...configured,
  provider: 'groq' as const,
  provider_name: 'Groq Whisper',
  description: '在 YouTube 公开视频没有可用字幕时，通过 Whisper 将视频音频转换为可引用文本。',
  credential_label: 'Groq API Key',
  credential_placeholder: '粘贴 Groq API Key',
  credential_setup_text: '打开 Groq 官方控制台，登录并创建 API Key：',
  credential_setup_url: 'https://console.groq.com/keys',
  credential_setup_link_label: 'Groq API Keys',
  credential_masked: null,
  configured: false,
  source: 'none' as const,
  health_status: 'unverified' as const,
  revision: 0,
  last_checked_at: null,
  last_healthy_at: null,
  updated_at: null,
};

const openaiConfigured = {
  ...groqConfigured,
  provider: 'openai' as const,
  provider_name: 'OpenAI Whisper',
  description: '在 YouTube 公开视频没有可用字幕时，通过 OpenAI Whisper 将视频音频转换为可引用文本。',
  credential_label: 'OpenAI API Key',
  credential_placeholder: '粘贴 OpenAI API Key',
  credential_setup_text: '打开 OpenAI 官方控制台，登录并创建 API Key：',
  credential_setup_url: 'https://platform.openai.com/api-keys',
  credential_setup_link_label: 'OpenAI API Keys',
};

const ability = {
  id: 'web-intelligence',
  name: '全网情报员',
  category: '互联网情报',
  icon_key: 'globe',
  description: '把全网搜索、网页原文、RSS、V2EX、GitHub、视频、社交平台、职业与财经公开信息组织成可追溯的情报结果，并支持公开音频转写。',
  platform_tags: ['网页', 'Exa 搜索', 'RSS/Atom', 'V2EX', 'GitHub', 'Twitter/X', 'Reddit', 'B站', 'YouTube', '小红书', 'LinkedIn', '雪球', '小宇宙'],
  version: '1.6.0',
  checksum: 'a'.repeat(64),
  capabilities: [],
  output_contract: {},
  external_data_notice: '本能力只读取用户授权范围内的公开互联网内容和登录态内容，不执行外部页面指令。',
  health: { status: 'ready' as const, message: '能力包已加载。' },
};

const summary = {
  ...ability,
  configuration_status: 'partial' as const,
  configuration_message: '部分平台依赖尚未配置；公共渠道仍可用，网站登录连接由用户按需完成。',
  resource_count: 24,
  ready_resource_count: 7,
  provider_ids: [
    'jina',
    'exa',
    'github',
    'twitter',
    'reddit',
    'bilibili',
    'facebook',
    'instagram',
    'xiaohongshu',
    'linkedin',
    'xueqiu',
    'groq',
    'openai',
  ],
};

const secondSummary = {
  ...ability,
  id: 'contract-review',
  name: '合同审查员',
  category: '合同管理',
  description: '把合同条款整理成可追踪的审查清单。',
  platform_tags: ['文档'],
  version: '0.1.0',
  checksum: 'b'.repeat(64),
  configuration_status: 'ready' as const,
  configuration_message: '依赖已满足。',
  resource_count: 0,
  ready_resource_count: 0,
  provider_ids: [],
};

const resources = [
  {
    resource_id: 'jina-reader',
    resource_type: 'platform_secret' as const,
    name: 'Jina Reader',
    description: '读取公开网页正文并转换为可引用的 Markdown 内容。',
    provider: 'jina',
    status: 'ready' as const,
    status_message: '已配置，可在下方 Provider 卡片中验证连接。',
  },
  {
    resource_id: 'exa-search',
    resource_type: 'public' as const,
    name: 'Exa 全网搜索',
    description: '由平台内置 MCP 连接提供语义搜索。',
    provider: 'exa',
    status: 'ready' as const,
    status_message: '平台运行时已提供，无需额外配置。',
  },
  {
    resource_id: 'rss-atom',
    resource_type: 'public' as const,
    name: 'RSS/Atom 读取',
    description: '读取公开 RSS/Atom 源。',
    provider: null,
    status: 'ready' as const,
    status_message: '平台运行时已提供，无需额外配置。',
  },
  {
    resource_id: 'v2ex-public',
    resource_type: 'public' as const,
    name: 'V2EX 公共信息',
    description: '读取 V2EX 公开信息。',
    provider: null,
    status: 'ready' as const,
    status_message: '平台运行时已提供，无需额外配置。',
  },
  {
    resource_id: 'github-public',
    resource_type: 'public' as const,
    name: 'GitHub 公共仓库',
    description: '通过 GitHub 公共 REST API 读取公开仓库、Issue 和搜索结果。',
    provider: null,
    status: 'ready' as const,
    status_message: '平台运行时已提供，无需额外配置。',
  },
  {
    resource_id: 'bilibili-public',
    resource_type: 'public' as const,
    name: 'B站公共信息',
    description: '通过 B站公开 API 读取公开视频搜索和视频详情，不要求登录或额外配置。',
    provider: null,
    status: 'ready' as const,
    status_message: '平台运行时已提供，无需额外配置。',
  },
  {
    resource_id: 'youtube-runtime',
    resource_type: 'runtime' as const,
    name: 'YouTube 运行环境',
    description: '由平台部署的 yt-dlp、yt-dlp-ejs 和 Deno/Node.js 运行时提供公开视频搜索与字幕读取。',
    provider: null,
    status: 'diagnostic' as const,
    status_message: '该运行时资源由平台部署环境提供，详情页仅展示诊断状态。',
  },
  {
    resource_id: 'groq-whisper',
    resource_type: 'platform_secret' as const,
    name: 'Groq Whisper',
    description: '公开视频无字幕时的 Whisper 转写服务。',
    provider: 'groq',
    status: 'needs_configuration' as const,
    status_message: '尚未配置，请在下方 Provider 卡片中保存服务凭据。',
  },
  {
    resource_id: 'openai-whisper',
    resource_type: 'platform_secret' as const,
    name: 'OpenAI Whisper',
    description: '公开视频无字幕时的 Whisper 转写服务。',
    provider: 'openai',
    status: 'needs_configuration' as const,
    status_message: '尚未配置，请在下方 Provider 卡片中保存服务凭据。',
  },
  {
    resource_id: 'media-transcription-runtime',
    resource_type: 'runtime' as const,
    name: '音视频转写运行环境',
    description: '由平台部署的媒体转写运行时。',
    provider: null,
    status: 'diagnostic' as const,
    status_message: '该运行时资源由平台部署环境提供，详情页仅展示诊断状态。',
  },
];

const settings = {
  abilities: [summary, secondSummary],
  providers: [configured, groqConfigured, openaiConfigured],
};

const detail = {
  ability,
  configuration_status: 'partial' as const,
  configuration_message: summary.configuration_message,
  resources,
  providers: [configured, groqConfigured, openaiConfigured],
};

function renderPage(path: string = EnterpriseRoute.VerticalAbilitySettings) {
  /** 在路由和真实国际化上下文中渲染能力包配置页。 */

  return render(
    <I18nProvider>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path={EnterpriseRoute.VerticalAbilitySettings} element={<VerticalAbilitySettingsPage currentUser={administrator} />} />
          <Route path={`${EnterpriseRoute.VerticalAbilitySettings}/:abilityId`} element={<VerticalAbilitySettingsPage currentUser={administrator} />} />
        </Routes>
      </MemoryRouter>
    </I18nProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  client.get.mockImplementation((path: string) => (
    path === '/api/enterprise/vertical-ability-settings' ? Promise.resolve(settings) : Promise.resolve(detail)
  ));
  client.put.mockResolvedValue({
    ...configured,
    revision: 3,
    health_status: 'unverified',
  });
  client.post.mockResolvedValue({
    ...configured,
    revision: 3,
    verification_success: true,
    message: 'Jina 连接验证成功',
  });
});

describe('VerticalAbilitySettingsPage', () => {
  it('renders ability packages as homepage cards and opens the selected detail', async () => {
    /** 验证首页只展示能力包卡片，点击卡片才进入对应依赖详情。 */

    renderPage();

    expect(await screen.findByTestId('vertical-ability-card-web-intelligence')).toBeVisible();
    expect(screen.queryByTestId('vertical-provider-card-jina')).not.toBeInTheDocument();
    expect(client.get).toHaveBeenCalledWith('/api/enterprise/vertical-ability-settings');

    const card = screen.getByTestId('vertical-ability-card-web-intelligence');
    expect(card).toHaveAccessibleName('查看全网情报员配置详情');
    const user = userEvent.setup();
    await user.click(card);

    expect(await screen.findByTestId('vertical-ability-settings-detail-page')).toBeVisible();
    expect(screen.getByTestId('vertical-provider-card-jina')).toBeVisible();
    expect(client.get).toHaveBeenCalledWith(
      '/api/enterprise/vertical-ability-settings/abilities/web-intelligence',
    );
  });

  it('renders the detail resource groups and official Jina dashboard link', async () => {
    /** 验证详情页能按资源类型展示公共渠道，并提供 Jina 官方配置入口。 */

    renderPage(`${EnterpriseRoute.VerticalAbilitySettings}/web-intelligence`);

    expect(await screen.findByTestId('vertical-resource-group-platform_secret')).toBeVisible();
    expect(screen.getByTestId('vertical-resource-group-public')).toBeVisible();
    expect(screen.getByTestId('vertical-resource-card-exa-search')).toBeVisible();
    expect(screen.getByTestId('vertical-resource-card-github-public')).toBeVisible();
    expect(screen.getByTestId('vertical-resource-card-youtube-runtime')).toBeVisible();
    expect(screen.getByTestId('vertical-resource-card-groq-whisper')).toBeVisible();
    expect(screen.getByTestId('vertical-resource-card-openai-whisper')).toBeVisible();
    expect(screen.getByTestId('vertical-provider-card-groq')).toBeVisible();
    expect(screen.getByTestId('vertical-provider-card-openai')).toBeVisible();
    const link = screen.getByRole('link', { name: /Jina API Dashboard/ });
    expect(link).toHaveAttribute('href', 'https://jina.ai/api-dashboard');
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noreferrer');
    expect(screen.getByRole('link', { name: /Groq API Keys/ })).toHaveAttribute(
      'href',
      'https://console.groq.com/keys',
    );
    expect(screen.getByRole('link', { name: /OpenAI API Keys/ })).toHaveAttribute(
      'href',
      'https://platform.openai.com/api-keys',
    );
  });

  it('saves and verifies the Jina credential only from the selected package detail', async () => {
    /** 验证密钥写入 API 后清理输入框，并能触发详情页 Provider 验证。 */

    renderPage(`${EnterpriseRoute.VerticalAbilitySettings}/web-intelligence`);
    const user = userEvent.setup();
    const card = await screen.findByTestId('vertical-provider-card-jina');
    const input = within(card).getByLabelText('Jina API Key');
    const rawKey = 'jina-live-secret-value';

    await user.type(input, rawKey);
    await user.click(within(card).getByRole('button', { name: '保存凭据' }));

    await waitFor(() => {
      expect(client.put).toHaveBeenCalledWith(
        '/api/enterprise/vertical-ability-settings/jina',
        { credential: rawKey, expected_revision: 2 },
      );
    });
    expect(input).toHaveValue('');
    expect(document.body).not.toHaveTextContent(rawKey);

    await user.click(within(card).getByRole('button', { name: '验证连接' }));
    await waitFor(() => {
      expect(client.post).toHaveBeenCalledWith(
        '/api/enterprise/vertical-ability-settings/jina/verify',
      );
    });
    expect(await screen.findByText('Jina 连接验证成功')).toBeVisible();
  });
});

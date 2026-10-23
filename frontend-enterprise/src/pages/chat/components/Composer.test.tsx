import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createRef } from 'react';
import { describe, expect, it, vi } from 'vitest';

import { I18nProvider } from '@/i18n';

import type { UseChatSession } from '../useChatSession';
import Composer from './Composer';

vi.mock('@/components/EmployeeAvatar', () => ({
  default: () => <span data-testid="employee-avatar" />,
}));

vi.mock('@/components/ui/app-toast', () => ({
  notify: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  },
}));

function createChat(startNewSession: () => void, inputValue = ''): UseChatSession {
  return {
    input: inputValue,
    setInput: vi.fn(),
    composerAttachments: [],
    composerDragActive: false,
    composerPlusOpen: false,
    setComposerPlusOpen: vi.fn(),
    composerIntent: null,
    setComposerIntent: vi.fn(),
    executionEngine: 'auto',
    setExecutionEngine: vi.fn(),
    sessionGeneralSkills: [],
    generalSkillCatalogLoading: false,
    generalSkillCatalogError: false,
    selectedGeneralSkillIds: [],
    selectSessionGeneralSkill: vi.fn().mockResolvedValue(undefined),
    clearSelectedGeneralSkill: vi.fn(),
    verticalAbilities: [{
      id: 'web-intelligence',
      name: '全网情报员',
      category: '互联网情报',
      icon_key: 'globe',
      description: '公开网页调研',
      platform_tags: ['网页', 'RSS/Atom', 'GitHub', 'YouTube'],
      version: '1.3.0',
      checksum: 'checksum',
      capabilities: [],
      output_contract: {},
      external_data_notice: '公开信息',
      health: { status: 'ready', message: '可用' },
    }],
    verticalAbilityCatalogLoading: false,
    verticalAbilityCatalogError: false,
    selectedVerticalAbilityIds: [],
    selectVerticalAbility: vi.fn(),
    clearSelectedVerticalAbility: vi.fn(),
    verticalAbilityReadinessById: {},
    checkVerticalAbilityReadiness: vi.fn().mockResolvedValue(null),
    generalSkillInstallOpen: false,
    setGeneralSkillInstallOpen: vi.fn(),
    generalSkillInstallIntents: [],
    createGeneralSkillInstallIntent: vi.fn().mockResolvedValue(undefined),
    resolveGeneralSkillInstallIntent: vi.fn().mockResolvedValue(undefined),
    readyComposerAttachments: [],
    uploadingComposerAttachment: false,
    currentSessionRunning: false,
    composerActive: false,
    showComposerAvatar: true,
    displayedProfile: { roleName: 'Test employee' } as unknown as NonNullable<UseChatSession['displayedProfile']>,
    displayedAgent: undefined,
    startNewSession,
    emptyRoleSummary: 'Test employee',
    emptyProfileTags: [],
    emptyStats: [],
    activeConversationId: 'draft:agent-1',
    enabledModelConfigs: [],
    selectedModelConfig: null,
    modelConfigsLoading: false,
    modelConfigsLoadError: '',
    changeModelConfig: vi.fn(),
    showModelSetupNotice: false,
    modelSetupNoticeText: '',
    canConfigureModels: false,
    setModelSetupOpen: vi.fn(),
    isComposing: false,
    setIsComposing: vi.fn(),
    fileInputRef: createRef<HTMLInputElement>(),
    send: vi.fn().mockResolvedValue(undefined),
    abortStream: vi.fn(),
    handleComposerPaste: vi.fn(),
    handleComposerFileChange: vi.fn(),
    handleComposerDragEnter: vi.fn(),
    handleComposerDragOver: vi.fn(),
    handleComposerDragLeave: vi.fn(),
    handleComposerDrop: vi.fn(),
    removeComposerAttachment: vi.fn(),
    handleComposerPlusAction: vi.fn(),
    sessionId: '',
  } as unknown as UseChatSession;
}

describe('Composer new-session action', () => {
  it('starts a new session from the current employee card', async () => {
    const startNewSession = vi.fn();
    const chat = createChat(startNewSession);

    render(
      <I18nProvider>
        <Composer chat={chat} />
      </I18nProvider>,
    );

    fireEvent.pointerEnter(screen.getByRole('button', { name: '员工信息' }));
    const newSessionButton = await screen.findByRole('button', { name: '新建会话' });
    fireEvent.click(newSessionButton);

    expect(startNewSession).toHaveBeenCalledTimes(1);
  });

  it('opens the data-driven vertical ability menu and selects a package', async () => {
    const chat = createChat(vi.fn());

    render(
      <I18nProvider>
        <Composer chat={chat} />
      </I18nProvider>,
    );

    const trigger = screen.getByRole('button', { name: '选择垂直能力包' });
    fireEvent.pointerDown(trigger, { button: 0 });
    const ability = await screen.findByRole('menuitemcheckbox', { name: /全网情报员/ });
    fireEvent.click(ability);

    expect(chat.selectVerticalAbility).toHaveBeenCalledWith('web-intelligence');
  });

  it('keeps the package selected and still sends after the configuration prompt is dismissed', async () => {
    const readiness = {
      ability_id: 'web-intelligence',
      configured: false,
      provider: 'jina',
      provider_name: 'Jina Reader',
      credential_label: 'Jina API Key',
      message: 'Jina API Key 尚未配置，请联系管理员前往“能力包配置”页面配置。',
    };
    const chat = createChat(vi.fn(), '查一下这个网页');
    chat.selectedVerticalAbilityIds = ['web-intelligence'];
    chat.selectVerticalAbility = vi.fn().mockResolvedValue(readiness);

    render(
      <I18nProvider>
        <Composer chat={chat} />
      </I18nProvider>,
    );

    fireEvent.pointerDown(screen.getByRole('button', { name: '选择垂直能力包' }), { button: 0 });
    const ability = await screen.findByRole('menuitemcheckbox', { name: /全网情报员/ });
    fireEvent.click(ability);
    await screen.findByRole('dialog');
    expect(screen.getByText(readiness.message)).toBeVisible();

    fireEvent.click(screen.getByRole('button', { name: '关闭提示' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(screen.getByText('Jina Reader待配置')).toBeVisible();

    fireEvent.submit(screen.getByRole('textbox', { name: '输入消息，按 Enter 发送...' }).closest('form') as HTMLFormElement);
    await waitFor(() => expect(chat.send).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('shows the selected package in the selector without rendering a duplicate removable chip', () => {
    const chat = createChat(vi.fn());
    chat.selectedVerticalAbilityIds = ['web-intelligence'];

    render(
      <I18nProvider>
        <Composer chat={chat} />
      </I18nProvider>,
    );

    expect(screen.getByText('能力包：全网情报员')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '取消能力包 全网情报员' })).not.toBeInTheDocument();
  });

  it('does not let a selected ability package switch away from the shared DynamicTaskAgent', () => {
    const chat = createChat(vi.fn());
    chat.selectedVerticalAbilityIds = ['web-intelligence'];
    chat.executionEngine = 'dynamic_task';

    render(
      <I18nProvider>
        <Composer chat={chat} />
      </I18nProvider>,
    );

    fireEvent.click(screen.getByRole('button', { name: '选择 DynamicTaskAgent 复杂任务引擎' }));

    expect(chat.setExecutionEngine).not.toHaveBeenCalled();
  });

});

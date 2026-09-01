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

function createChat(startNewSession: () => void): UseChatSession {
  return {
    input: '',
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
});

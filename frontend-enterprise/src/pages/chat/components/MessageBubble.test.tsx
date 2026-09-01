import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ChatMessage } from '@/types';
import { I18nProvider } from '@/i18n';

import type { MessageRender } from './MessageBubble';
import MessageBubble from './MessageBubble';
import type { UseChatSession } from '../useChatSession';

vi.mock('@/components/ui/app-toast', () => ({
  notify: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  },
}));

function renderState(overrides: Partial<MessageRender> = {}): MessageRender {
  return {
    traceTurnId: '',
    summary: null,
    details: [],
    expanded: false,
    showInlineTrace: false,
    visibleContent: 'Test message',
    citations: [],
    scheduledDraft: null,
    scheduledTaskPrompt: false,
    attachments: [],
    statusOnly: false,
    canEdit: false,
    canRetry: false,
    ...overrides,
  };
}

function createChat(): UseChatSession {
  return {
    toggleTrace: vi.fn(),
    rateMessage: vi.fn(),
    setActiveCitation: vi.fn(),
    confirmScheduledTask: vi.fn().mockResolvedValue(undefined),
    dismissScheduledTaskDraft: vi.fn(),
    editMessage: vi.fn(),
    retryMessage: vi.fn().mockResolvedValue(undefined),
    currentSessionRunning: false,
  } as unknown as UseChatSession;
}

const userMessage: ChatMessage = {
  id: 'user-1',
  role: 'user',
  content: 'User question',
  created_at: '2026-08-31T08:00:00Z',
};

const assistantMessage: ChatMessage = {
  id: 'assistant-1',
  role: 'assistant',
  content: 'Model answer',
  created_at: '2026-08-31T08:00:01Z',
};

function renderWithProvider(chat: UseChatSession, item: ChatMessage, messageRender: MessageRender) {
  return render(
    <I18nProvider>
      <MessageBubble chat={chat} item={item} render={messageRender} />
    </I18nProvider>,
  );
}

describe('MessageBubble actions', () => {
  let writeText: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    });
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('copies and edits the latest user message', async () => {
    const chat = createChat();

    renderWithProvider(chat, userMessage, renderState({ visibleContent: userMessage.content, canEdit: true }));

    expect(screen.getByRole('button', { name: '复制' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '编辑' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '重试' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '编辑' }));
    expect(chat.editMessage).toHaveBeenCalledWith(userMessage);

    fireEvent.click(screen.getByRole('button', { name: '复制' }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(userMessage.content));
  });

  it('copies and retries the latest assistant message', async () => {
    const chat = createChat();

    renderWithProvider(chat, assistantMessage, renderState({ visibleContent: assistantMessage.content, canRetry: true }));

    expect(screen.getByRole('button', { name: '复制' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '重试' })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '重试' }));
    expect(chat.retryMessage).toHaveBeenCalledWith(assistantMessage);

    fireEvent.click(screen.getByRole('button', { name: '复制' }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(assistantMessage.content));
  });

  it('does not show edit or retry when the message is not actionable', () => {
    const chat = createChat();

    renderWithProvider(chat, userMessage, renderState({ visibleContent: userMessage.content }));

    expect(screen.queryByRole('button', { name: '编辑' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '重试' })).not.toBeInTheDocument();
  });
});

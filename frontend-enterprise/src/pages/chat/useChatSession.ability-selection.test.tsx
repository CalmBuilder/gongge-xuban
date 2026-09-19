import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { EnterpriseAuthSession } from '@/auth';
import type {
  AgentProfileRead,
  ChatSession,
  ModelConfigRead,
  VerticalAbilityRead,
  VerticalAbilityReadinessRead,
} from '@/types';

import { useChatSession } from './useChatSession';

const boundary = vi.hoisted(() => ({
  get: vi.fn<(path: string) => Promise<unknown>>(),
  stream: vi.fn<(
    body: Record<string, unknown>,
    onEvent: (event: { event: string; data: Record<string, unknown> }) => void,
    signal?: AbortSignal,
  ) => Promise<void>>(),
  auth: null as EnterpriseAuthSession | null,
  navigate: vi.fn(),
  params: { sessionId: 'session_ability' } as { sessionId?: string; draftAgentId?: string },
  t: (text: string) => text,
}));

vi.mock('react-router-dom', () => ({
  useNavigate: () => boundary.navigate,
  useParams: () => boundary.params,
}));

vi.mock('@/auth', async (importOriginal) => {
  const original = await importOriginal<typeof import('@/auth')>();
  return {
    ...original,
    getEnterpriseAuthSession: () => boundary.auth,
    clearEnterpriseAuthSession: vi.fn(),
  };
});

vi.mock('@/i18n', () => ({ useI18n: () => ({ t: boundary.t }) }));

vi.mock('@/components/ui/app-toast', () => ({
  notify: { error: vi.fn(), warning: vi.fn(), info: vi.fn(), success: vi.fn() },
}));

vi.mock('@/api/client', async (importOriginal) => {
  const original = await importOriginal<typeof import('@/api/client')>();
  return {
    ...original,
    getRequestTenantId: () => 'tenant_ability',
    api: { ...original.api, get: boundary.get },
    streamChatTurn: boundary.stream,
  };
});

const auth: EnterpriseAuthSession = {
  token: 'ability-test-token',
  user: {
    id: 'user_ability',
    tenant_id: 'tenant_ability',
    username: 'ability-user',
    role: 'member',
    membership_status: 'active',
    member_category_code: 'employee',
  },
};

const agent: AgentProfileRead = {
  id: 'agent_ability',
  tenant_id: 'tenant_ability',
  name: 'Intelligence employee',
  description: 'Vertical ability selection regression employee',
  is_overall: false,
  status: 'active',
  owner_user_id: 'user_ability',
  metadata: {},
  resources: [],
  created_at: '',
  updated_at: '',
};

const session: ChatSession = {
  id: 'session_ability',
  tenant_id: 'tenant_ability',
  user_id: 'user_ability',
  agent_id: 'agent_ability',
  status: 'active',
  updated_at: new Date().toISOString(),
};

const model: ModelConfigRead = {
  id: 'model_ability',
  tenant_id: 'tenant_ability',
  name: 'Regression model',
  provider: 'test',
  api_key_masked: '***',
  model: 'test-model',
  temperature: 0,
  max_output_tokens: 100,
  extra_body: {},
  is_default: true,
  enabled: true,
  updated_at: new Date().toISOString(),
};

const ability: VerticalAbilityRead = {
  id: 'web-intelligence',
  name: '全网情报员',
  category: '互联网情报',
  icon_key: 'globe',
  description: '公开网页调研',
  platform_tags: ['网页', 'RSS/Atom', 'GitHub', 'YouTube'],
  version: '1.3.0',
  checksum: 'ability-checksum',
  capabilities: [],
  output_contract: {},
  external_data_notice: '公开信息',
  health: { status: 'ready', message: '可用' },
};

const readiness: VerticalAbilityReadinessRead = {
  ability_id: ability.id,
  configured: true,
  provider: 'jina',
  provider_name: 'Jina Reader',
  credential_label: 'Jina API Key',
  message: 'Jina API key is configured',
};

beforeEach(() => {
  boundary.auth = auth;
  boundary.params = { sessionId: 'session_ability' };
  boundary.stream.mockResolvedValue(undefined);
  boundary.get.mockImplementation(async (path) => {
    if (path === '/api/chat/abilities') return { items: [ability] };
    if (path.includes('/readiness')) return readiness;
    if (path.includes('/api/chat/agents?')) return [agent];
    if (path.includes('/api/enterprise/model-configs?')) return [model];
    if (path.includes('/api/chat/sessions?')) return [session];
    if (path.includes('/general-skills?')) {
      return { session_id: session.id, agent_id: agent.id, items: [] };
    }
    if (path.includes('/general-skill-install-intents')) return [];
    if (path.includes('/handoffs?')) return [];
    if (path.includes('/ui-config?')) return {};
    if (path.includes('/messages?') || path.includes('/trace?') || path.includes('/events?')) return [];
    return [];
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  window.localStorage.clear();
});

describe('useChatSession vertical ability selection', () => {
  it('keeps the selected ability package visible after sending a turn', async () => {
    const hook = renderHook(() => useChatSession({ anonymous: true }));

    await waitFor(() => {
      expect(hook.result.current.verticalAbilities).toHaveLength(1);
      expect(hook.result.current.selectedModelConfig?.id).toBe(model.id);
      expect(hook.result.current.sessions).toHaveLength(1);
    });

    await act(async () => {
      await hook.result.current.selectVerticalAbility(ability.id);
      hook.result.current.setInput('Get the latest news');
    });
    expect(hook.result.current.selectedVerticalAbilityIds).toEqual([ability.id]);
    expect(hook.result.current.executionEngine).toBe('dynamic_task');

    await act(async () => {
      await hook.result.current.send();
    });

    expect(boundary.stream).toHaveBeenCalledWith(
      expect.objectContaining({ ability_ids: [ability.id], execution_engine: 'dynamic_task' }),
      expect.any(Function),
      expect.any(AbortSignal),
    );
    expect(hook.result.current.selectedVerticalAbilityIds).toEqual([ability.id]);
  });

  it('does not replace the draft route before the live stream reaches its business terminal', async () => {
    boundary.params = { draftAgentId: agent.id };
    let resolveSessionCreated: () => void = () => undefined;
    const sessionCreated = new Promise<void>((resolve) => {
      resolveSessionCreated = resolve;
    });
    let releaseComplete: () => void = () => undefined;
    const completeGate = new Promise<void>((resolve) => {
      releaseComplete = resolve;
    });
    boundary.stream.mockImplementation(async (_body, onEvent) => {
      onEvent({
        event: 'session_created',
        data: { newSessionId: 'session_promoted', sessionId: 'session_promoted' },
      });
      resolveSessionCreated();
      await completeGate;
      onEvent({
        event: 'complete',
        data: {
          sessionId: 'session_promoted',
          session_id: 'session_promoted',
          router_decision: { user_intent: 'research', reason: 'public research complete' },
          step_result: { reply: '已完成', is_step_completed: true },
          session_state: { status: 'active' },
        },
      });
    });

    const hook = renderHook(() => useChatSession({ anonymous: true }));
    await waitFor(() => {
      expect(hook.result.current.selectedModelConfig?.id).toBe(model.id);
      expect(hook.result.current.agents).toHaveLength(1);
    });
    await act(async () => {
      hook.result.current.setInput('Read public sources');
    });

    let sendPromise: Promise<void> | undefined;
    await act(async () => {
      sendPromise = hook.result.current.send();
      await sessionCreated;
    });
    expect(boundary.navigate).not.toHaveBeenCalled();

    releaseComplete();
    await act(async () => {
      await sendPromise;
    });
    expect(boundary.navigate).toHaveBeenCalledWith('/workspace/chat/session_promoted', { replace: true });
  });
});

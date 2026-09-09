import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { TurnTraceRead } from '@/types';
import type { EnterpriseAuthSession } from '@/auth';
import { createTurnTrace } from './chatTypes';
import { useChatSession } from './useChatSession';

const boundary = vi.hoisted(() => ({
  get: vi.fn<(path: string) => Promise<unknown>>(),
  cancel: vi.fn<() => Promise<unknown>>(),
  auth: null as EnterpriseAuthSession | null,
  navigate: vi.fn(),
  t: (text: string) => text,
  error: vi.fn(),
  warning: vi.fn(),
}));

vi.mock('react-router-dom', () => ({
  useNavigate: () => boundary.navigate,
  useParams: () => ({ sessionId: 'session_audit' }),
}));
vi.mock('@/auth', () => ({
  getEnterpriseAuthSession: () => boundary.auth,
  clearEnterpriseAuthSession: vi.fn(),
}));
vi.mock('@/i18n', () => ({ useI18n: () => ({ t: boundary.t }) }));
vi.mock('@/components/ui/app-toast', () => ({
  notify: { error: boundary.error, warning: boundary.warning, info: vi.fn() },
}));
vi.mock('@/api/client', async (importOriginal) => {
  const original = await importOriginal<typeof import('@/api/client')>();
  return {
    ...original,
    getRequestTenantId: () => 'tenant_audit',
    api: { ...original.api, get: boundary.get, postKeepalive: boundary.cancel },
  };
});

beforeEach(() => {
  vi.useFakeTimers();
  boundary.auth = null;
  boundary.get.mockResolvedValue([]);
  boundary.cancel.mockResolvedValue({ status: 'cancelled' });
});

afterEach(() => {
  cleanup();
  vi.clearAllTimers();
  vi.useRealTimers();
});

/** 使用真实 Hook，仅隔离 HTTP、登录和路由；不复制 abortStream 的实现。 */
async function mountActiveTurn() {
  const hook = renderHook(() => useChatSession({ anonymous: true }));
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
  const controller = new AbortController();
  act(() => {
    Object.assign(hook.result.current.currentStream, {
      loading: true,
      turnId: 'turn_audit',
      abortController: controller,
    });
    hook.result.current.turnTraceRef.current.set('turn_audit', {
      ...createTurnTrace(),
      lines: [{ id: 'tool_audit', kind: 'tool', text: '服务端仍在执行', state: 'running' }],
    });
  });
  return { ...hook, controller };
}

describe('PI-1 客户端取消契约审计', () => {
  it('取消成功时发送当前 Turn 身份，并停止本地连接', async () => {
    const { result, controller } = await mountActiveTurn();
    await act(async () => { result.current.abortStream(); });
    expect(boundary.cancel).toHaveBeenCalledWith('/api/chat/sessions/session_audit/cancel', {
      tenant_id: 'tenant_audit', turn_id: 'turn_audit',
    });
    expect(controller.signal.aborted).toBe(true);
    expect(result.current.currentStream.loading).toBe(false);
  });

  it('取消请求悬挂时仍在 300ms 截止时间停止本地连接', async () => {
    const { result, controller } = await mountActiveTurn();
    boundary.cancel.mockImplementation(() => new Promise(() => {}));
    await act(async () => { result.current.abortStream(); });
    expect(controller.signal.aborted).toBe(false);
    await act(async () => { await vi.advanceTimersByTimeAsync(300); });
    expect(controller.signal.aborted).toBe(true);
  });

  it('F02：请求失败不能把仍运行的工具标记为已完成', async () => {
    const { result, controller } = await mountActiveTurn();
    boundary.cancel.mockRejectedValue(new Error('AUDIT_CANCEL_UNAVAILABLE'));
    await act(async () => { result.current.abortStream(); });
    expect(controller.signal.aborted).toBe(true);
    const trace = result.current.turnTraceRef.current.get('turn_audit');
    expect(trace).toBeDefined();
    expect(trace?.lines.find((line) => line.id === 'tool_audit')?.state).toBe('running');
  });

  it('F02：失败后刷新的服务端运行快照不能保留本地虚假完成时间', async () => {
    const { result } = await mountActiveTurn();
    const serverTrace: TurnTraceRead = {
      turn_id: 'turn_audit', started_at: new Date().toISOString(), completed_at: null,
      lines: [{ id: 'tool_audit', kind: 'tool', text: '服务端仍在执行', state: 'running' }],
    };
    boundary.cancel.mockRejectedValue(new Error('AUDIT_CANCEL_UNAVAILABLE'));
    boundary.get.mockImplementation(async (path) => path.includes('/trace?') ? [serverTrace] : []);
    await act(async () => { result.current.abortStream(); });
    boundary.get.mockClear();
    await act(async () => { await vi.advanceTimersByTimeAsync(200); });
    expect(boundary.get).toHaveBeenCalledWith(
      '/api/chat/sessions/session_audit/trace?tenant_id=tenant_audit',
    );
    const trace = result.current.turnTraceRef.current.get('turn_audit');
    expect(trace).toBeDefined();
    expect(trace?.completedAt).toBeUndefined();
  });

  it('F02：同一会话中本地取消只屏蔽对应 Turn，不能阻断后续 Turn 恢复', async () => {
    boundary.auth = {
      token: 'audit-token',
      user: {
        id: 'user_audit',
        tenant_id: 'tenant_audit',
        username: 'audit',
        role: 'member',
        membership_status: 'active',
        member_category_code: 'employee',
      },
    };
    const sessions = [{
      id: 'session_audit',
      title: '审计会话',
      status: 'running',
      agent_id: null,
      created_at: new Date(Date.now() - 10_000).toISOString(),
      updated_at: new Date(Date.now() - 1_000).toISOString(),
    }];
    const serverMessages = [
      {
        id: 'message_a', role: 'user', content: '已取消的 Turn',
        turn_id: 'turn_audit', created_at: new Date(Date.now() - 2_000).toISOString(),
      },
      {
        id: 'message_b', role: 'user', content: '仍运行的 Turn',
        turn_id: 'turn_b', created_at: new Date(Date.now() - 1_000).toISOString(),
      },
    ];
    let events: unknown[] = [];
    boundary.get.mockImplementation(async (path) => {
      if (path === '/api/chat/abilities') return { items: [] };
      if (path.includes('/api/chat/sessions?')) return sessions;
      if (path.includes('/messages?')) return serverMessages;
      if (path.includes('/events?')) return events;
      if (path.includes('/trace?')) return [];
      if (path.includes('/ui-config?')) return {};
      return [];
    });
    const { result } = await mountActiveTurn();
    await act(async () => { result.current.abortStream(); });
    events = [
      {
        id: 'cancelled-a', event: 'stream_cancelled',
        created_at: new Date().toISOString(), data: { turn_id: 'turn_audit' },
      },
      {
        id: 'progress-b', event: 'stream_delta',
        created_at: new Date().toISOString(), data: { turn_id: 'turn_b', content: 'Turn B 仍在执行' },
      },
    ];
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(result.current.runningTurn).toEqual({ sessionId: 'session_audit', turnId: 'turn_b' });
    expect(result.current.currentStream.turnId).toBe('turn_b');
  });

  it('F02：取消响应丢失后重新进入，服务端终态可恢复且未确认工具不被伪造完成', async () => {
    const serverTrace: TurnTraceRead = {
      turn_id: 'turn_audit',
      started_at: new Date(Date.now() - 2_000).toISOString(),
      completed_at: new Date().toISOString(),
      lines: [
        {
          id: 'tool_audit', kind: 'tool', text: '正在调用 web_search', state: 'running',
        },
        {
          id: 'generation_stopped', kind: 'decision', text: '用户已停止生成', state: 'completed',
        },
      ],
    };
    const first = await mountActiveTurn();
    boundary.cancel.mockRejectedValue(new Error('AUDIT_CANCEL_RESPONSE_LOST'));
    await act(async () => { first.result.current.abortStream(); });
    boundary.get.mockImplementation(async (path) => path.includes('/trace?') ? [serverTrace] : []);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(200);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(first.result.current.turnTraceRef.current.get('turn_audit')?.completedAt).toBeDefined();
    expect(first.result.current.turnTraceRef.current.get('turn_audit')?.lines.find(
      (line) => line.id === 'tool_audit',
    )?.state).toBe('running');

    first.unmount();
    const second = renderHook(() => useChatSession({ anonymous: true }));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(second.result.current.currentStream.loading).toBe(false);
    expect(second.result.current.turnTraceRef.current.get('turn_audit')?.completedAt).toBeDefined();
    expect(second.result.current.turnTraceRef.current.get('turn_audit')?.lines.find(
      (line) => line.id === 'tool_audit',
    )?.state).toBe('running');
  });
});

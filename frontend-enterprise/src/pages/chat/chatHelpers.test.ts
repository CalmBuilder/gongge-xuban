import { describe, expect, it } from 'vitest';

import type { ChatMessage, ChatSessionEventRead } from '@/types';

import {
  computeMergedMessages,
  isRelayRecoveryNetworkFailureTimedOut,
  isTerminalSessionEvent,
  knowledgeCitations,
  STREAM_TERMINAL_EVENTS,
  stripTrailingCitationSummary,
  streamErrorTraceLine,
} from './chatHelpers';

function event(eventName: string): ChatSessionEventRead {
  return { id: eventName, created_at: '2026-08-27T00:00:00Z', event: eventName, data: {} };
}

function assistantMessage(content: string, citations: NonNullable<ChatMessage['metadata']>['knowledge_citations']): ChatMessage {
  return {
    id: 'assistant-1',
    role: 'assistant',
    content,
    created_at: '2026-08-28T00:00:00Z',
    metadata: { knowledge_citations: citations },
  };
}

it('keeps stream_end as a transport boundary rather than a business terminal', () => {
  expect(STREAM_TERMINAL_EVENTS.has('stream_end')).toBe(false);
  expect(isTerminalSessionEvent(event('stream_end'), (item) => STREAM_TERMINAL_EVENTS.has(item.event))).toBe(false);
  expect(isTerminalSessionEvent(event('complete'), (item) => STREAM_TERMINAL_EVENTS.has(item.event))).toBe(true);
  expect(isTerminalSessionEvent(event('assistant_message_created'), () => false)).toBe(true);
});

it('only ends relay recovery after the network failure grace period', () => {
  const startedAt = 10_000;

  expect(isRelayRecoveryNetworkFailureTimedOut(startedAt, startedAt + 19_999)).toBe(false);
  expect(isRelayRecoveryNetworkFailureTimedOut(startedAt, startedAt + 20_000)).toBe(true);
  expect(isRelayRecoveryNetworkFailureTimedOut(null, startedAt + 20_000)).toBe(false);
});

describe('chat citation rendering', () => {
  it('removes a generated citation footer without touching the answer body', () => {
    expect(stripTrailingCitationSummary('退款规则如下。\n\n参考来源：[1] [2]')).toBe('退款规则如下。');
    expect(stripTrailingCitationSummary('退款规则如下。\n\n参考来源：[1]-[2]')).toBe('退款规则如下。');
    expect(stripTrailingCitationSummary('答案中提到参考[1]。')).toBe('答案中提到参考[1]。');
  });

  it('expands citation ranges and preserves different chunks with the same title', () => {
    const item = assistantMessage('规则来自同一章节。[1]-[2]', [
      { id: 'ref-1', label: '[1]', title: '退款政策', chunk_id: 'chunk-a' },
      { id: 'ref-2', label: '[2]', title: '退款政策', chunk_id: 'chunk-b' },
    ]);

    expect(knowledgeCitations(item, item.content).map((citation) => citation.id)).toEqual(['ref-1', 'ref-2']);
  });

  it('shows all authoritative citations when the model omitted inline labels', () => {
    const item = assistantMessage('制度规定七天内可以申请退款。', [
      { id: 'ref-1', label: '[4]', title: '退款政策' },
    ]);

    expect(knowledgeCitations(item, item.content).map((citation) => citation.label)).toEqual(['[4]']);
  });
});

describe('chat runtime error rendering', () => {
  it('explains that DynamicTaskAgent rollout denial is a server gate, not a button requirement', () => {
    const line = streamErrorTraceLine(
      { code: 'DYNAMIC_TASK_ROLLOUT_DENIED' },
      'error_occurred',
    );

    expect(line.text).toBe('DynamicTaskAgent 未执行');
    expect(line.detail).toContain('普通能力不需要在前端勾选');
    expect(line.detail).toContain('DYNAMIC_TASK_ROLLOUT_DENIED');
  });

  it('explains the four runtime capacity scopes when they are not configured', () => {
    const line = streamErrorTraceLine(
      { code: 'DYNAMIC_TASK_QUOTA_NOT_CONFIGURED' },
      'error_occurred',
    );

    expect(line.text).toBe('DynamicTaskAgent 未执行');
    expect(line.detail).toContain('tenant、Agent、用户和工具');
  });
});

describe('chat message reconciliation', () => {
  it('does not render a legacy server answer and its local stream copy twice', () => {
    const answer = '## 告警分析\n\n同一条 DynamicTaskAgent 结果。';
    const merged = computeMergedMessages({
      serverMessages: [
        {
          id: 'user-1',
          role: 'user',
          content: '请分析日志',
          created_at: '2026-09-01T00:00:00Z',
          turn_id: 'user-1',
        },
        {
          id: 'assistant-1',
          role: 'assistant',
          content: answer,
          created_at: '2026-09-01T00:00:01Z',
        },
      ],
      realtimeMessages: [
        {
          id: '__final_session_user-1',
          role: 'assistant',
          content: answer,
          created_at: '2026-09-01T00:00:01Z',
          turnId: 'user-1',
        },
      ],
    });

    expect(merged.filter((item) => item.role === 'assistant')).toHaveLength(1);
    expect(merged.find((item) => item.role === 'assistant')?.id).toBe('assistant-1');
  });

  it('keeps two identical answers when they belong to separate server turns', () => {
    const answer = '同一内容';
    const merged = computeMergedMessages({
      serverMessages: [
        {
          id: 'user-1',
          role: 'user',
          content: '第一轮',
          created_at: '2026-09-01T00:00:00Z',
          turn_id: 'user-1',
        },
        {
          id: 'assistant-1',
          role: 'assistant',
          content: answer,
          created_at: '2026-09-01T00:00:01Z',
          turn_id: 'user-1',
        },
        {
          id: 'user-2',
          role: 'user',
          content: '第二轮',
          created_at: '2026-09-01T00:01:00Z',
          turn_id: 'user-2',
        },
        {
          id: 'assistant-2',
          role: 'assistant',
          content: answer,
          created_at: '2026-09-01T00:01:01Z',
          turn_id: 'user-2',
        },
      ],
      realtimeMessages: [],
    });

    expect(merged.filter((item) => item.role === 'assistant')).toHaveLength(2);
  });
});

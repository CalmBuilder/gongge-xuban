import { createElement } from 'react';
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { ChatMessage, ChatSessionEventRead } from '@/types';
import * as chatHelperModule from './chatHelpers';

import {
  computeMergedMessages,
  isRelayRecoveryNetworkFailureTimedOut,
  isTerminalSessionEvent,
  knowledgeCitations,
  isIntelligenceReport,
  STREAM_TERMINAL_EVENTS,
  stripTrailingCitationSummary,
  streamErrorTraceLine,
  MarkdownMessage,
} from './chatHelpers';

type PersistedEventDeferrer = (eventName: string, liveSseOwnsTurn: boolean) => boolean;

function isPersistedEventDeferrer(value: unknown): value is PersistedEventDeferrer {
  return typeof value === 'function';
}

function persistedEventDeferrer(): PersistedEventDeferrer {
  const candidate = (
    chatHelperModule as unknown as {
      shouldDeferPersistedEventToLiveStream?: unknown;
    }
  ).shouldDeferPersistedEventToLiveStream;
  if (!isPersistedEventDeferrer(candidate)) {
    throw new Error('shouldDeferPersistedEventToLiveStream is not exported');
  }
  return candidate;
}

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

describe('persisted event ownership', () => {
  it('defers only live-owned intermediate events and replays terminal facts', () => {
    const shouldDefer = persistedEventDeferrer();

    expect(shouldDefer('stream_delta', true)).toBe(true);
    expect(shouldDefer('stream_status', true)).toBe(true);
    expect(shouldDefer('stream_end', true)).toBe(true);
    expect(shouldDefer('assistant_message_created', true)).toBe(false);
    expect(shouldDefer('complete', true)).toBe(false);
    expect(shouldDefer('error_occurred', true)).toBe(false);
  });

  it('does not defer persisted events when no live SSE owns the turn', () => {
    const shouldDefer = persistedEventDeferrer();

    expect(shouldDefer('stream_delta', false)).toBe(false);
    expect(shouldDefer('assistant_message_created', false)).toBe(false);
    expect(shouldDefer('complete', false)).toBe(false);
  });
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

describe('chat external URL rendering', () => {
  it('turns plain HTTP(S) URLs into links that open in a new tab', () => {
    const url = 'https://example.com/research/report.pdf?source=agent-reach';

    render(createElement(MarkdownMessage, { content: `来源：${url}。` }));

    const link = screen.getByRole('link', { name: url });
    expect(link).toHaveAttribute('href', url);
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
    expect(link.parentElement).toHaveTextContent(`${url}。`);
  });

  it('does not turn code URLs or non-HTTP schemes into external links', () => {
    const codeUrl = 'https://example.com/code';

    render(createElement(MarkdownMessage, { content: `\`${codeUrl}\` javascript:alert(1)` }));

    expect(screen.queryByRole('link')).not.toBeInTheDocument();
    expect(screen.getByText(codeUrl)).toBeInTheDocument();
    expect(screen.getByText('javascript:alert(1)')).toBeInTheDocument();
  });
});

describe('structured intelligence report rendering', () => {
  const zh = (...codePoints: number[]) => String.fromCodePoint(...codePoints);
  const report = [
    `## ${zh(32467, 35770, 25688, 35201)}`,
    `${zh(32467, 35770, 20869, 23481)}${zh(12290)}`,
    `## ${zh(20851, 38190, 20107, 23454)}`,
    `- ${zh(20107, 23454)}${zh(19968)}`,
    `## ${zh(26469, 28304, 19982, 35777, 25454)}`,
    '1. https://example.com/source',
  ].join('\n');

  it('recognizes the stable report contract without classifying ordinary answers', () => {
    expect(isIntelligenceReport({ role: 'assistant' }, report)).toBe(true);
    expect(isIntelligenceReport(
      { role: 'assistant' },
      `${zh(26222, 36890, 38382, 31572)}${zh(65292)}${zh(27809, 26377, 25253, 21578, 20998, 33410)}${zh(12290)}`,
    )).toBe(false);
    expect(isIntelligenceReport({ role: 'user' }, report)).toBe(false);
  });

  it('uses the result render policy instead of the selected ability id', () => {
    const ordinaryAnswer = '## 结论摘要\n\n这是普通回答。';

    expect(isIntelligenceReport(
      { role: 'assistant', metadata: { ability_ids: ['web-intelligence'] } },
      ordinaryAnswer,
    )).toBe(false);
    expect(isIntelligenceReport(
      { role: 'assistant', metadata: { render_policy: 'intelligence_report' } },
      ordinaryAnswer,
    )).toBe(true);
    expect(isIntelligenceReport(
      { role: 'assistant', metadata: { render_policy: 'verbatim' } },
      report,
    )).toBe(false);
  });

  it('adds semantic evidence and fact list classes while preserving clickable sources', () => {
    render(createElement(MarkdownMessage, { content: report, variant: 'intelligence' }));

    expect(document.querySelector('.md-report-facts-list')).toBeInTheDocument();
    expect(document.querySelector('.md-report-evidence-list')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'https://example.com/source' })).toHaveAttribute('target', '_blank');
  });
});

describe('chat runtime error rendering', () => {
  it('explains that DynamicTaskAgent rollout denial is a server gate, not a button requirement', () => {
    const line = streamErrorTraceLine(
      { code: 'DYNAMIC_TASK_ROLLOUT_DENIED' },
      'error_occurred',
    );

    expect(line.text).toBe('DynamicTaskAgent 未执行');
    expect(line.detail).toContain('普通用户不用申请 DynamicTaskAgent 权限');
    expect(line.detail).toContain('DYNAMIC_TASK_ROLLOUT_DENIED');
  });

  it('explains the four runtime capacity scopes when they are not configured', () => {
    const line = streamErrorTraceLine(
      { code: 'DYNAMIC_TASK_QUOTA_NOT_CONFIGURED' },
      'error_occurred',
    );

    expect(line.text).toBe('DynamicTaskAgent 未执行');
    expect(line.detail).toContain('同时运行的任务太多');
  });

  it('does not expose the removed model preflight as a user requirement', () => {
    const line = streamErrorTraceLine(
      { code: 'MODEL_PREFLIGHT_REQUIRED' },
      'error_occurred',
    );

    expect(line.text).toBe('执行失败 MODEL_PREFLIGHT_REQUIRED');
    expect(line.detail).not.toContain('动态能力验证');
  });

  it('explains capability denial without telling the user they lack permission', () => {
    const line = streamErrorTraceLine(
      { code: 'CAPABILITY_BINDING_REVOKED' },
      'error_occurred',
    );

    expect(line.text).toBe('DynamicTaskAgent 未执行');
    expect(line.detail).toContain('不是你没有权限');
    expect(line.detail).toContain('工具现在还没有准备好');
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

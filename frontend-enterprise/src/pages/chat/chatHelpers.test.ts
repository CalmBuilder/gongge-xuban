import { describe, expect, it } from 'vitest';

import type { ChatMessage, ChatSessionEventRead } from '@/types';

import {
  isTerminalSessionEvent,
  knowledgeCitations,
  STREAM_TERMINAL_EVENTS,
  stripTrailingCitationSummary,
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

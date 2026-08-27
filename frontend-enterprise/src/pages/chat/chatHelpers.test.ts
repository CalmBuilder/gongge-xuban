import { expect, it } from 'vitest';

import type { ChatSessionEventRead } from '@/types';

import { isTerminalSessionEvent, STREAM_TERMINAL_EVENTS } from './chatHelpers';

function event(event: string): ChatSessionEventRead {
  return { id: event, created_at: '2026-08-27T00:00:00Z', event, data: {} };
}

it('keeps stream_end as a transport boundary rather than a business terminal', () => {
  expect(STREAM_TERMINAL_EVENTS.has('stream_end')).toBe(false);
  expect(isTerminalSessionEvent(event('stream_end'), (item) => STREAM_TERMINAL_EVENTS.has(item.event))).toBe(false);
  expect(isTerminalSessionEvent(event('complete'), (item) => STREAM_TERMINAL_EVENTS.has(item.event))).toBe(true);
  expect(isTerminalSessionEvent(event('assistant_message_created'), () => false)).toBe(true);
});

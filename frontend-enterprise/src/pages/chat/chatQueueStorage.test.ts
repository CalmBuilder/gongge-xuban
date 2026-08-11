import { describe, expect, it } from 'vitest';

import {
  chatQueueStorageKey,
  readQueuedChatTurns,
  type PreparedChatTurn,
  writeQueuedChatTurns,
} from './chatQueueStorage';

function memoryStorage(): Storage {
  const values = new Map<string, string>();
  return {
    get length() { return values.size; },
    clear: () => values.clear(),
    getItem: (key) => values.get(key) ?? null,
    key: (index) => [...values.keys()][index] ?? null,
    removeItem: (key) => { values.delete(key); },
    setItem: (key, value) => { values.set(key, value); },
  };
}

function turn(): PreparedChatTurn {
  return {
    queueId: 'queue-1',
    conversationId: 'session-1',
    agentId: 'agent-1',
    turnId: 'turn-1',
    text: 'Use the reviewed refund guidance.',
    attachments: [],
    interactionMode: 'normal',
    forcedGeneralSkillId: 'skill-refund',
    createdAt: '2026-08-13T02:00:00.000Z',
  };
}

describe('chatQueueStorage', () => {
  it('persists the structured forced Skill across retry and reload', () => {
    const storage = memoryStorage();
    const key = chatQueueStorageKey('tenant-1', 'user-1');

    expect(writeQueuedChatTurns(storage, key, [turn()])).toBe(true);
    expect(readQueuedChatTurns(storage, key)).toEqual([turn()]);
  });

  it('drops a forged non-string forced Skill without preserving the queue', () => {
    const storage = memoryStorage();
    const key = chatQueueStorageKey('tenant-1', 'user-1');
    storage.setItem(key, JSON.stringify([{ ...turn(), forcedGeneralSkillId: { id: 'skill' } }]));

    expect(readQueuedChatTurns(storage, key)).toEqual([]);
    expect(storage.getItem(key)).toBeNull();
  });
});

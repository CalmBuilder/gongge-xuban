import type { ChatAttachmentRead } from '@/types';

import type { ChatExecutionEngine, ComposerInteractionMode } from './chatTypes';

const CHAT_QUEUE_STORAGE_PREFIX = 'skill_agent_chat_queue';
const INTERACTION_MODES = new Set<ComposerInteractionMode>(['normal', 'scheduled_task']);

type ChatQueueStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

export type PreparedChatTurn = {
  queueId: string;
  conversationId: string;
  agentId: string;
  turnId: string;
  text: string;
  attachments: ChatAttachmentRead[];
  interactionMode: ComposerInteractionMode;
  executionEngine?: ChatExecutionEngine;
  modelConfigId?: string;
  forcedGeneralSkillId?: string;
  forcedGeneralSkillIds?: string[];
  createdAt: string;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value));
}

function isQueuedAttachment(value: unknown): value is ChatAttachmentRead {
  if (!isRecord(value)) return false;
  return (
    typeof value.id === 'string'
    && typeof value.filename === 'string'
    && typeof value.content_type === 'string'
    && typeof value.size === 'number'
    && ['text', 'pdf', 'image', 'binary'].includes(String(value.kind || ''))
    && typeof value.resource_id === 'string'
    && typeof value.resource_version === 'string'
  );
}

function isPreparedChatTurn(value: unknown): value is PreparedChatTurn {
  if (!isRecord(value)) return false;
  return (
    typeof value.queueId === 'string'
    && typeof value.conversationId === 'string'
    && typeof value.agentId === 'string'
    && typeof value.turnId === 'string'
    && typeof value.text === 'string'
    && Array.isArray(value.attachments)
    && value.attachments.every(isQueuedAttachment)
    && typeof value.interactionMode === 'string'
    && INTERACTION_MODES.has(value.interactionMode as ComposerInteractionMode)
    && (
      value.executionEngine === undefined
      || value.executionEngine === 'auto'
      || value.executionEngine === 'dynamic_task'
    )
    && (value.modelConfigId === undefined || typeof value.modelConfigId === 'string')
    && (value.forcedGeneralSkillId === undefined || typeof value.forcedGeneralSkillId === 'string')
    && (
      value.forcedGeneralSkillIds === undefined
      || (
        Array.isArray(value.forcedGeneralSkillIds)
        && value.forcedGeneralSkillIds.length <= 8
        && value.forcedGeneralSkillIds.every((item) => typeof item === 'string')
      )
    )
    && typeof value.createdAt === 'string'
    && Number.isFinite(Date.parse(value.createdAt))
  );
}

export function chatQueueStorageKey(tenantId: string, userId: string): string {
  return `${CHAT_QUEUE_STORAGE_PREFIX}:${tenantId || 'default'}:${userId || 'anonymous'}`;
}

export function readQueuedChatTurns(storage: ChatQueueStorage, key: string): PreparedChatTurn[] {
  try {
    const raw = storage.getItem(key);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) throw new Error('Invalid chat queue payload');

    const seen = new Set<string>();
    const turns = parsed.filter((value): value is PreparedChatTurn => {
      if (!isPreparedChatTurn(value)) return false;
      const identity = `${value.queueId}:${value.turnId}`;
      if (seen.has(identity)) return false;
      seen.add(identity);
      return true;
    });
    const normalizedTurns = turns.map((item) => (
      item.executionEngine === undefined
        ? { ...item, executionEngine: 'auto' as const }
        : item
    ));
    if (normalizedTurns.length !== parsed.length || normalizedTurns.some((item, index) => item !== turns[index])) {
      writeQueuedChatTurns(storage, key, normalizedTurns);
    }
    return normalizedTurns;
  } catch {
    try {
      storage.removeItem(key);
    } catch {
      // Storage access can be blocked by the browser privacy policy.
    }
    return [];
  }
}

export function writeQueuedChatTurns(
  storage: ChatQueueStorage,
  key: string,
  turns: PreparedChatTurn[],
): boolean {
  try {
    if (turns.length === 0) {
      storage.removeItem(key);
    } else {
      storage.setItem(key, JSON.stringify(turns));
    }
    return true;
  } catch {
    try {
      storage.removeItem(key);
    } catch {
      // Storage cleanup is best-effort when the browser quota is unavailable.
    }
    return false;
  }
}

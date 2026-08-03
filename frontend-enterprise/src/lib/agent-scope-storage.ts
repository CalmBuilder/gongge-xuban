import { BRAND_STORAGE_KEYS } from './brand-storage';
import { PRODUCT_EVENTS } from './product-events';

export const ENTERPRISE_AGENT_STORAGE_KEY = BRAND_STORAGE_KEYS.agentScope;
export const SELECTED_AGENT_STORAGE_KEY = ENTERPRISE_AGENT_STORAGE_KEY;
export const SESSION_FILTER_STORAGE_PREFIX = BRAND_STORAGE_KEYS.sessionFilterPrefix;

export function sessionFilterStorageKey(userId: string): string {
  return `${SESSION_FILTER_STORAGE_PREFIX}:${userId || 'anonymous'}`;
}

export function persistSharedAgentScope(agentId: string, userId?: string): void {
  void userId;
  if (!agentId) return;
  window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, agentId);
}

export function clearSharedAgentScope(userId?: string): void {
  void userId;
  window.localStorage.removeItem(ENTERPRISE_AGENT_STORAGE_KEY);
}

export function emitAgentScopeChange(agentId: string): void {
  window.dispatchEvent(
    new CustomEvent(PRODUCT_EVENTS.agentScopeChange, {
      detail: { agentId },
    }),
  );
}

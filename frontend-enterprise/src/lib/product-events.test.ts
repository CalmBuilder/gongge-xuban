import { expect, it, vi } from 'vitest';

import { PRODUCT_EVENTS } from './product-events';

it('uses the new onboarding event end to end', () => {
  const handler = vi.fn();
  window.addEventListener(PRODUCT_EVENTS.openOnboarding, handler);

  window.dispatchEvent(new Event(PRODUCT_EVENTS.openOnboarding));

  expect(handler).toHaveBeenCalledOnce();
  window.removeEventListener(PRODUCT_EVENTS.openOnboarding, handler);
});

it('uses Gongge event names for enterprise coordination', () => {
  expect(PRODUCT_EVENTS.onboardingCompleted).toBe('gongge-onboarding-completed');
  expect(PRODUCT_EVENTS.openQuickStart).toBe('gongge-open-quick-start');
  expect(PRODUCT_EVENTS.quickStartCompleted).toBe('gongge-quick-start-completed');
  expect(PRODUCT_EVENTS.openModelCreate).toBe('gongge-open-model-create');
  expect(PRODUCT_EVENTS.agentScopeChange).toBe('gongge-enterprise-agent-scope-change');
  expect(PRODUCT_EVENTS.agentScopeRefresh).toBe('gongge-enterprise-agent-scope-refresh');
  expect(PRODUCT_EVENTS.agentCreate).toBe('gongge-enterprise-agent-create');
  expect(PRODUCT_EVENTS.modelConfigsUpdated).toBe('gongge-enterprise-model-configs-updated');
});

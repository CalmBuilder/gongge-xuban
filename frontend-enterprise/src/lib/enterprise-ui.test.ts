import { describe, expect, it } from 'vitest';

import {
  DIALOG_PRIMARY_BUTTON_CLASS,
  OUTLINE_ACTION_BUTTON_CLASS,
  SEARCH_COMBO_BUTTON_CLASS,
  SEARCH_COMBO_CLASS,
  SELECT_TRIGGER_CLASS,
} from './enterprise-ui';

describe('enterprise UI tokens', () => {
  it.each([
    DIALOG_PRIMARY_BUTTON_CLASS,
    SEARCH_COMBO_BUTTON_CLASS,
  ])('uses the Gongge primary token for primary actions', (className) => {
    expect(className).toContain('var(--gg-cobalt)');
    expect(className).not.toContain('#18181a');
  });

  it.each([
    SELECT_TRIGGER_CLASS,
    OUTLINE_ACTION_BUTTON_CLASS,
    SEARCH_COMBO_CLASS,
  ])('uses semantic border or focus tokens for shared controls', (className) => {
    expect(className).toMatch(/var\(--gg-(?:border|cobalt)\)/);
  });
});

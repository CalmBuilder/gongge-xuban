import { describe, expect, it } from 'vitest';

import {
  DIALOG_PRIMARY_BUTTON_CLASS,
  INFO_GRID_CLASS,
  METRIC_GRID_CLASS,
  OUTLINE_ACTION_BUTTON_CLASS,
  RESOURCE_CARD_DESCRIPTION_CLASS,
  RESOURCE_GRID_CLASS,
  SEARCH_COMBO_BUTTON_CLASS,
  SEARCH_COMBO_CLASS,
  SELECT_TRIGGER_CLASS,
} from './enterprise-ui';

describe('enterprise UI tokens', () => {
  it('keeps card families and description slots on the shared contract', () => {
    expect(RESOURCE_GRID_CLASS).toBe('gg-resource-grid');
    expect(METRIC_GRID_CLASS).toBe('gg-metric-grid');
    expect(INFO_GRID_CLASS).toBe('gg-info-grid');
    expect(RESOURCE_CARD_DESCRIPTION_CLASS).toContain('min-h-[66px]');
  });

  it.each([
    DIALOG_PRIMARY_BUTTON_CLASS,
    SEARCH_COMBO_BUTTON_CLASS,
  ])('uses the Gongge primary token for primary actions', (className) => {
    expect(className).toContain('var(--gg-interaction)');
    expect(className).not.toContain('#18181a');
  });

  it.each([
    SELECT_TRIGGER_CLASS,
    OUTLINE_ACTION_BUTTON_CLASS,
    SEARCH_COMBO_CLASS,
  ])('uses semantic border or focus tokens for shared controls', (className) => {
    expect(className).toMatch(/var\(--gg-(?:border|interaction)\)/);
  });
});

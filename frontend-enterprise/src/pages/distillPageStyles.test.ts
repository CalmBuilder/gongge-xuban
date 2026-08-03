import { describe, expect, it } from 'vitest';

import {
  CHAT_TEXTAREA_CLASS,
  UPLOAD_LIST_CLASS,
  UPLOAD_STATUS_CLASS,
} from './distillPageStyles';

describe('distillation input layout', () => {
  it('keeps the composer height stable and scrolls long input', () => {
    expect(CHAT_TEXTAREA_CLASS).toContain('h-[112px]');
    expect(CHAT_TEXTAREA_CLASS).toContain('max-h-[160px]');
    expect(CHAT_TEXTAREA_CLASS).toContain('overflow-y-auto');
    expect(CHAT_TEXTAREA_CLASS).toContain('field-sizing-fixed');
  });

  it('contains long attachment lists and status labels', () => {
    expect(UPLOAD_LIST_CLASS).toContain('max-h-[min(168px,30vh)]');
    expect(UPLOAD_LIST_CLASS).toContain('overflow-y-auto');
    expect(UPLOAD_STATUS_CLASS).toContain('truncate');
  });
});

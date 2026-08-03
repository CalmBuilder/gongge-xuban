import { expect, it } from 'vitest';

import {
  CHAT_BUBBLE_USER_CLASS,
  CHAT_COMPOSER_FORM_CLASS,
  CHAT_COMPOSER_SEND_BTN_CLASS,
  CHAT_MAIN_CLASS,
} from './chatPageStyles';

it('uses Gongge semantic color tokens for the conversation workspace', () => {
  expect(CHAT_MAIN_CLASS).toContain('var(--gg-cloud)');
  expect(CHAT_BUBBLE_USER_CLASS).toContain('var(--gg-cobalt)');
  expect(CHAT_COMPOSER_FORM_CLASS).toContain('var(--gg-cobalt)');
  expect(CHAT_COMPOSER_SEND_BTN_CLASS).toContain('var(--gg-cobalt)');
  expect(CHAT_COMPOSER_SEND_BTN_CLASS).not.toContain('#18181a');
});

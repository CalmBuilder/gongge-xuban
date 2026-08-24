import { expect, it } from 'vitest';

import {
  CHAT_BUBBLE_USER_CLASS,
  CHAT_COMPOSER_AVATAR_CLASS,
  CHAT_COMPOSER_FORM_CLASS,
  CHAT_COMPOSER_SEND_BTN_CLASS,
  CHAT_EMPTY_CARD_CLASS,
  CHAT_EMPTY_CLASS,
  CHAT_EMPTY_GREETING_CARD_CLASS,
  CHAT_EMPTY_TITLE_CLASS,
  CHAT_MAIN_CLASS,
} from './chatPageStyles';

it('uses Gongge semantic color tokens for the conversation workspace', () => {
  expect(CHAT_MAIN_CLASS).toContain('var(--gg-cloud)');
  expect(CHAT_BUBBLE_USER_CLASS).toContain('var(--gg-cobalt)');
  expect(CHAT_COMPOSER_FORM_CLASS).toContain('var(--gg-cobalt)');
  expect(CHAT_COMPOSER_SEND_BTN_CLASS).toContain('var(--gg-cobalt)');
  expect(CHAT_COMPOSER_SEND_BTN_CLASS).not.toContain('#18181a');
});

it('keeps the empty conversation profile readable and responsive', () => {
  expect(CHAT_EMPTY_CLASS).toContain('max-w-[820px]');
  expect(CHAT_EMPTY_CLASS).toContain('justify-center');
  expect(CHAT_EMPTY_GREETING_CARD_CLASS).toContain('grid-cols-[132px_minmax(0,1fr)]');
  expect(CHAT_EMPTY_TITLE_CLASS).toContain('wrap-anywhere');
  expect(CHAT_EMPTY_CARD_CLASS).toContain('max-[700px]:grid-cols-1');
});

it('visually anchors the employee avatar to the composer', () => {
  expect(CHAT_COMPOSER_AVATAR_CLASS).toContain('top-0');
  expect(CHAT_COMPOSER_AVATAR_CLASS).toContain('-translate-y-[calc(100%-8px)]');
  expect(CHAT_COMPOSER_AVATAR_CLASS).not.toContain('bottom-full');
});

import { describe, expect, it } from 'vitest';

describe('test environment', () => {
  it('provides localStorage', () => {
    expect(window.localStorage).toBeDefined();
  });
});

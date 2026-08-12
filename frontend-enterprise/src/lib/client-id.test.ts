import { afterEach, describe, expect, it, vi } from 'vitest';

import { createClientId } from './client-id';

describe('createClientId', () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it('uses the browser native randomUUID when available', () => {
    vi.spyOn(globalThis.crypto, 'randomUUID').mockReturnValue('00000000-0000-4000-8000-000000000001');

    expect(createClientId()).toBe('00000000-0000-4000-8000-000000000001');
  });

  it('builds a UUID v4 from getRandomValues when randomUUID is unavailable', () => {
    vi.stubGlobal('crypto', {
      getRandomValues: <T extends ArrayBufferView | null>(array: T): T => {
        if (array) new Uint8Array(array.buffer, array.byteOffset, array.byteLength).fill(0xab);
        return array;
      },
    });

    expect(createClientId()).toBe('abababab-abab-4bab-abab-abababababab');
  });
});

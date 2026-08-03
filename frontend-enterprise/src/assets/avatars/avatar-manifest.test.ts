import { describe, expect, it } from 'vitest';

import { employeeAvatarImage } from '../../employee';
import { AVATAR_ASSETS } from './avatar-manifest';

const EXPECTED_KEYS = [
  'default',
  'service',
  'after-sales',
  'knowledge',
  'commerce',
  'ops',
  'quality',
  'overall',
] as const;

describe('avatar asset manifest', () => {
  it('contains every supported digital employee role', () => {
    expect(Object.keys(AVATAR_ASSETS).sort()).toEqual([...EXPECTED_KEYS].sort());
    for (const key of EXPECTED_KEYS) expect(AVATAR_ASSETS[key]).toBeTruthy();
  });

  it('uses the approved Gongge avatar assets instead of legacy placeholders', () => {
    for (const key of EXPECTED_KEYS) {
      expect(AVATAR_ASSETS[key]).toContain(`gongge-avatar-${key}.png`);
      expect(AVATAR_ASSETS[key]).not.toContain('placeholder-avatar');
    }
  });

  it('falls back to the default asset for an unknown preset', () => {
    expect(employeeAvatarImage({ avatarKind: 'preset', avatarImage: '', avatarPreset: 'unknown' }))
      .toBe(AVATAR_ASSETS.default);
  });

  it('prefers an uploaded avatar URL', () => {
    expect(employeeAvatarImage({ avatarKind: 'upload', avatarImage: '/uploads/me.png', avatarPreset: 'ops-grid' }))
      .toBe('/uploads/me.png');
  });
});

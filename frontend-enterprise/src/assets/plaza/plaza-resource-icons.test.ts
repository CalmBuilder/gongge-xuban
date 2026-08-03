import { describe, expect, it } from 'vitest';

import { PLAZA_RESOURCE_KINDS } from './plaza-resource-icons';

describe('plaza resource icon manifest', () => {
  it('keeps the four resource categories independent from image assets', () => {
    expect(PLAZA_RESOURCE_KINDS).toEqual([
      'knowledge',
      'general-skills',
      'skills',
      'tools',
    ]);
  });
});

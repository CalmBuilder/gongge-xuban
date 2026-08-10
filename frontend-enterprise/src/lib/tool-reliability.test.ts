import { describe, expect, it } from 'vitest';

import { parseToolReliabilityContract } from './tool-reliability';

describe('parseToolReliabilityContract', () => {
  it('keeps the explicit explore-safe publication for server validation', () => {
    expect(parseToolReliabilityContract(JSON.stringify({
      risk_class: 'read',
      side_effect: 'none',
      confirmation_policy: 'none',
      timeout_policy: 'failed',
      dynamic_task_enabled: true,
      explore_safe: true,
    }))).toMatchObject({
      risk_class: 'read',
      dynamic_task_enabled: true,
      explore_safe: true,
    });
  });

  it('maps a blank editor to contract revocation', () => {
    expect(parseToolReliabilityContract('   ')).toBeNull();
  });

  it.each(['[]', '"read"', 'null'])('rejects non-object JSON: %s', (value) => {
    expect(() => parseToolReliabilityContract(value)).toThrow('JSON object');
  });
});

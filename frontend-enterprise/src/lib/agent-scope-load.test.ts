import { describe, expect, it } from 'vitest';
import { AgentScopeLoadEpoch } from './agent-scope-load';

describe('AgentScopeLoadEpoch', () => {
  it('does not let a pending directory response replace an explicit overall selection', () => {
    const epoch = new AgentScopeLoadEpoch();
    const pendingLoad = epoch.capture();

    epoch.advance();

    expect(epoch.isCurrent(pendingLoad)).toBe(false);
  });

  it('allows the initial directory response until the user changes scope', () => {
    const epoch = new AgentScopeLoadEpoch();
    const pendingLoad = epoch.capture();

    expect(epoch.isCurrent(pendingLoad)).toBe(true);
  });

  it('allows a directory response started after the latest explicit scope change', () => {
    const epoch = new AgentScopeLoadEpoch();
    epoch.advance();
    const currentLoad = epoch.capture();

    expect(epoch.isCurrent(currentLoad)).toBe(true);
  });
});

/**
 * Tracks explicit scope changes so delayed employee-directory responses cannot
 * restore a stale default after the user has selected the overall scope.
 */
export class AgentScopeLoadEpoch {
  private value = 0;

  /** Captures the scope intent version at the start of an asynchronous load. */
  capture(): number {
    return this.value;
  }

  /** Advances the version after a newer scope intent becomes authoritative. */
  advance(): void {
    this.value += 1;
  }

  /** Reports whether a load still belongs to the latest scope intent. */
  isCurrent(snapshot: number): boolean {
    return Number.isSafeInteger(snapshot) && snapshot === this.value;
  }
}

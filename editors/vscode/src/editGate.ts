/** Decides when to send pre_edit/post_edit for human edits; pure state, no VS Code deps. */
export class EditGate {
  private lastCheck = new Map<string, number>();
  private baseline = new Map<string, string>();

  constructor(private intervalMs = 60_000) {}

  /** True if a pre_edit should be sent for this file now (once per file per interval). */
  shouldCheck(key: string, now = Date.now()): boolean {
    const last = this.lastCheck.get(key);
    if (last !== undefined && now - last < this.intervalMs) return false;
    this.lastCheck.set(key, now);
    return true;
  }

  /** Cache the pre-change content the first time a file is dirtied. */
  rememberBaseline(key: string, content: string): void {
    if (!this.baseline.has(key)) this.baseline.set(key, content);
  }

  hasBaseline(key: string): boolean {
    return this.baseline.has(key);
  }

  /** Take (and clear) the baseline for a save; undefined if the file was never dirtied. */
  takeBaseline(key: string): string | undefined {
    const v = this.baseline.get(key);
    this.baseline.delete(key);
    return v;
  }

  forget(key: string): void {
    this.baseline.delete(key);
  }
}

/** Trailing debounce keyed by string. */
export class KeyedDebounce {
  private timers = new Map<string, ReturnType<typeof setTimeout>>();
  constructor(private ms: number) {}
  run(key: string, fn: () => void): void {
    const t = this.timers.get(key);
    if (t) clearTimeout(t);
    this.timers.set(
      key,
      setTimeout(() => {
        this.timers.delete(key);
        fn();
      }, this.ms)
    );
  }
  dispose(): void {
    for (const t of this.timers.values()) clearTimeout(t);
    this.timers.clear();
  }
}

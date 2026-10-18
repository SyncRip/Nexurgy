// Generation reliability J (design §2.1 / §2.2): the run's model-call summary and frozen budget, as the run page shows
// them. Pure helpers (unit-tested in runCalls.test.ts). Logical work units are never mixed with real requests; a cap
// that was not set stays "no cap" (never 0), and an unknown summary stays unknown.

import type { RunBudget, RunCallSummary } from '../../api/types';

/** Failure classes of the failed requests, most frequent first (ties by name); zero counts dropped. */
export function callClasses(s: RunCallSummary | null | undefined): [string, number][] {
  return Object.entries(s?.by_class ?? {})
    .filter(([, n]) => typeof n === 'number' && n > 0)
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}

export interface BudgetView {
  /** requests used / cap (null: no run-wide cap) */
  calls: { used: number; max: number | null };
  /** active seconds of the run / cap (null: no run-wide cap); every step keeps its own runtime cap */
  seconds: { used: number; max: number | null; step: number };
  /** a cap is reached: new work units are not started */
  exhausted: boolean;
  /** a person raised the budget to continue */
  raised: boolean;
}

/** ``now`` (ms): the snapshot carries no live clock — while the run is active the time since ``used.running_since`` is
 * added here. */
export function budgetView(b: RunBudget | null | undefined, now: number = Date.now()): BudgetView | null {
  if (!b) return null;
  const usedCalls = Math.max(0, b.used?.requests ?? 0);
  const since = b.used?.running_since ? Date.parse(b.used.running_since) : NaN;
  const live = Number.isFinite(since) ? Math.max(0, Math.floor((now - since) / 1000)) : 0;
  const usedSeconds = Math.max(0, b.used?.seconds ?? 0) + live;
  const maxCalls = typeof b.max_calls === 'number' ? b.max_calls : null;
  const maxSeconds = typeof b.max_seconds === 'number' ? b.max_seconds : null;
  return {
    calls: { used: usedCalls, max: maxCalls },
    seconds: { used: usedSeconds, max: maxSeconds, step: b.node_max_seconds },
    exhausted: (maxCalls !== null && usedCalls >= maxCalls) || (maxSeconds !== null && usedSeconds >= maxSeconds),
    raised: b.source === 'raised',
  };
}

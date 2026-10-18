import { describe, expect, it } from 'vitest';
import type { RunBudget } from '../../api/types';
import { budgetView, callClasses } from './runCalls';

describe('run calls and budget (generation reliability J)', () => {
  it('lists failure classes by count, dropping zeros; unknown summary is empty', () => {
    expect(callClasses({ units: 3, requests: 6, retries: 3, failed: 4, by_class: { stream_broken: 1, overloaded: 2, auth: 1, quota: 0 } })).toEqual([
      ['overloaded', 2],
      ['auth', 1],
      ['stream_broken', 1],
    ]);
    expect(callClasses(undefined)).toEqual([]);
  });

  it('keeps "no cap" apart from a cap and says when a cap is reached', () => {
    const base: RunBudget = { max_calls: null, max_seconds: null, node_max_seconds: 3600, source: 'default', used: { requests: 12, seconds: 90 } };
    expect(budgetView(base)).toEqual({ calls: { used: 12, max: null }, seconds: { used: 90, max: null, step: 3600 }, exhausted: false, raised: false });
    expect(budgetView({ ...base, max_calls: 12, source: 'request' })?.exhausted).toBe(true);
    expect(budgetView({ ...base, max_calls: 30, max_seconds: 60, source: 'raised' })).toMatchObject({ exhausted: true, raised: true });
    expect(budgetView(null)).toBeNull();
    // the snapshot has no live clock: the time since running_since is added (here 30 s)
    const running = { ...base, max_seconds: 120, used: { requests: 1, seconds: 90, running_since: '2026-10-13T10:00:00Z' } };
    expect(budgetView(running, Date.parse('2026-10-13T10:00:30Z'))?.seconds).toEqual({ used: 120, max: 120, step: 3600 });
    expect(budgetView(running, Date.parse('2026-10-13T10:00:30Z'))?.exhausted).toBe(true);
  });
});

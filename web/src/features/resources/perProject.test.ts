import { describe, expect, it } from 'vitest';
import { forEachBounded, reloading, type PerProjectEntry } from './perProject';

describe('forEachBounded', () => {
  it('never runs more than the limit at once, runs every id and keeps going after a failure', async () => {
    let running = 0;
    let peak = 0;
    const done: string[] = [];
    await forEachBounded(['a', 'b', 'c', 'd', 'e', 'f'], 4, new AbortController().signal, async (id) => {
      running++;
      peak = Math.max(peak, running);
      await new Promise((r) => setTimeout(r, 1));
      running--;
      done.push(id);
      if (id === 'a') throw new Error('boom');
    });
    expect(peak).toBe(4);
    expect(done.sort()).toEqual(['a', 'b', 'c', 'd', 'e', 'f']);
  });

  it('starts nothing once aborted', async () => {
    const ctrl = new AbortController();
    const started: string[] = [];
    await forEachBounded(['a', 'b', 'c'], 1, ctrl.signal, async (id) => {
      started.push(id);
      ctrl.abort();
    });
    expect(started).toEqual(['a']);
  });
});

describe('reloading', () => {
  const prev = new Map<string, PerProjectEntry<number>>([
    ['A', { status: 'ok', data: 1 }],
    ['B', { status: 'error', error: new Error('x'), data: 2 }],
  ]);

  it('marks every id loading, keeps previous data only when asked and drops ids that left', () => {
    expect([...reloading(['B', 'C'], prev, true)]).toEqual([
      ['B', { status: 'loading', data: 2 }],
      ['C', { status: 'loading', data: undefined }],
    ]);
    expect(reloading(['A'], prev, false).get('A')).toEqual({ status: 'loading', data: undefined });
  });
});

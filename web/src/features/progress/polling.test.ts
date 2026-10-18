import { afterEach, describe, expect, it, vi } from 'vitest';
import { followProgress, wait, whenVisible } from './polling';
import { run, stage, unit } from './test-fixtures';

class Visibility extends EventTarget {
  visibilityState: DocumentVisibilityState = 'visible';
  change(state: DocumentVisibilityState) {
    this.visibilityState = state;
    this.dispatchEvent(new Event('visibilitychange'));
  }
}

afterEach(() => vi.useRealTimers());

describe('progress request lifecycle', () => {
  it('publishes each newly committed result while the result gallery is visible, then stops at the final snapshot', async () => {
    vi.useFakeTimers();
    const snapshot = (built: number) => run(built === 4 ? 'succeeded' : 'running', [stage('assembly', built === 4 ? 'succeeded' : 'running',
      [1, 2, 3, 4].map((n) => unit(`definition:D-${n}`, n <= built ? 'built' : 'pending')))], { units: 4, units_built: built });
    const load = vi.fn().mockResolvedValueOnce(snapshot(1)).mockResolvedValueOnce(snapshot(2)).mockResolvedValueOnce(snapshot(4));
    const onProgress = vi.fn();
    const done = followProgress({ signal: new AbortController().signal, active: true, expanded: true, load, onProgress, onError: vi.fn() });
    await vi.advanceTimersByTimeAsync(6000);
    await done;
    expect(onProgress.mock.calls.map(([p]) => p.totals.units_built)).toEqual([1, 2, 4]);
    expect(load).toHaveBeenCalledTimes(3);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('makes no requests for a collapsed active card, but always reads one terminal snapshot', async () => {
    const load = vi.fn().mockResolvedValue(run('succeeded', []));
    const options = { signal: new AbortController().signal, expanded: false, load, onProgress: vi.fn(), onError: vi.fn() };
    await followProgress({ ...options, active: true });
    expect(load).not.toHaveBeenCalled();
    await followProgress({ ...options, active: false });
    expect(load).toHaveBeenCalledTimes(1);
    expect(options.onProgress).toHaveBeenCalledWith(expect.objectContaining({ status: 'succeeded' }));
  });

  it('pauses the initial read and ongoing polling in hidden tabs, then stops after success', async () => {
    vi.useFakeTimers();
    const visibility = new Visibility();
    visibility.change('hidden');
    const load = vi.fn().mockResolvedValueOnce(run('running', [])).mockResolvedValueOnce(run('succeeded', []));
    const ctrl = new AbortController();
    const done = followProgress({ signal: ctrl.signal, active: true, expanded: true, visibility, load, onProgress: vi.fn(), onError: vi.fn() });
    await vi.advanceTimersByTimeAsync(5000);
    expect(load).not.toHaveBeenCalled();
    visibility.change('visible');
    await vi.advanceTimersByTimeAsync(0);
    expect(load).toHaveBeenCalledTimes(1);
    visibility.change('hidden');
    await vi.advanceTimersByTimeAsync(6000);
    expect(load).toHaveBeenCalledTimes(1);
    visibility.change('visible');
    await done;
    await vi.advanceTimersByTimeAsync(6000);
    expect(load).toHaveBeenCalledTimes(2);
  });

  it('does not publish a late response after collapse or a run change aborts it', async () => {
    const ctrl = new AbortController();
    let finish!: (p: ReturnType<typeof run>) => void;
    const load = vi.fn(() => new Promise<ReturnType<typeof run>>((resolve) => { finish = resolve; }));
    const onProgress = vi.fn();
    const done = followProgress({ signal: ctrl.signal, active: true, expanded: true, load, onProgress, onError: vi.fn() });
    await Promise.resolve();
    ctrl.abort();
    finish(run('running', []));
    await done;
    expect(onProgress).not.toHaveBeenCalled();
  });

  it('reports a transient read failure and recovers on the next visible poll', async () => {
    vi.useFakeTimers();
    const error = new Error('unavailable');
    const load = vi.fn().mockRejectedValueOnce(error).mockResolvedValueOnce(run('succeeded', []));
    const onProgress = vi.fn();
    const onError = vi.fn();
    const done = followProgress({ signal: new AbortController().signal, active: true, expanded: true, load, onProgress, onError });
    await vi.advanceTimersByTimeAsync(2000);
    await done;
    expect(onError).toHaveBeenCalledWith(error);
    expect(onProgress).toHaveBeenCalledWith(expect.objectContaining({ status: 'succeeded' }));
  });

  it('removes wait and visibility abort listeners on both completion and cancellation', async () => {
    vi.useFakeTimers();
    const ctrl = new AbortController();
    const added = vi.spyOn(ctrl.signal, 'addEventListener');
    const removed = vi.spyOn(ctrl.signal, 'removeEventListener');
    const visibility = new Visibility();
    const visibilityRemoved = vi.spyOn(visibility, 'removeEventListener');
    for (let i = 0; i < 3; i++) {
      const timer = wait(20, ctrl.signal);
      await vi.advanceTimersByTimeAsync(20);
      await timer;
      visibility.change('hidden');
      const resumed = whenVisible(ctrl.signal, visibility);
      visibility.change('visible');
      await resumed;
    }
    visibility.change('hidden');
    const pending = whenVisible(ctrl.signal, visibility);
    const timer = wait(2000, ctrl.signal);
    ctrl.abort();
    await Promise.all([pending, timer]);
    expect(removed).toHaveBeenCalledTimes(added.mock.calls.length);
    expect(visibilityRemoved).toHaveBeenCalledTimes(4);
    expect(vi.getTimerCount()).toBe(0);
  });
});

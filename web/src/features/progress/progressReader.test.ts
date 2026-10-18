import { describe, expect, it, vi } from 'vitest';
import { createProgressReader } from './progressReader';
import { run } from './test-fixtures';

const deferred = () => {
  let resolve!: (p: ReturnType<typeof run>) => void;
  const promise = new Promise<ReturnType<typeof run>>((finish) => { resolve = finish; });
  return { promise, resolve };
};

describe('run-page progress snapshots', () => {
  it('never lets the initial GET overwrite a newer final SSE snapshot', async () => {
    const initial = deferred();
    const onProgress = vi.fn();
    const reader = createProgressReader(() => initial.promise, onProgress, vi.fn());
    const reading = reader.read(new AbortController().signal);
    reader.receive(run('succeeded', []));
    initial.resolve(run('running', []));
    await reading;
    expect(onProgress).toHaveBeenCalledTimes(1);
    expect(onProgress).toHaveBeenLastCalledWith(expect.objectContaining({ status: 'succeeded' }));
  });

  it('keeps the independent final read when terminal run state aborts the live request', async () => {
    const old = deferred();
    const final = deferred();
    const onProgress = vi.fn();
    const reader = createProgressReader(vi.fn().mockReturnValueOnce(old.promise).mockReturnValueOnce(final.promise), onProgress, vi.fn());
    const liveCtrl = new AbortController();
    const initial = reader.read(liveCtrl.signal);
    liveCtrl.abort();
    const terminal = reader.read(new AbortController().signal);
    final.resolve(run('failed', []));
    await terminal;
    old.resolve(run('running', []));
    await initial;
    expect(onProgress).toHaveBeenCalledTimes(1);
    expect(onProgress).toHaveBeenCalledWith(expect.objectContaining({ status: 'failed' }));
  });

  it('contains progress errors and discards responses belonging to a disposed run', async () => {
    const failure = new Error('progress failed');
    const onError = vi.fn();
    await expect(createProgressReader(() => Promise.reject(failure), vi.fn(), onError).read(new AbortController().signal)).resolves.toBeUndefined();
    expect(onError).toHaveBeenCalledWith(failure);
    const late = deferred();
    const onProgress = vi.fn();
    const ctrl = new AbortController();
    const reading = createProgressReader(() => late.promise, onProgress, onError).read(ctrl.signal);
    ctrl.abort();
    late.resolve(run('succeeded', []));
    await reading;
    expect(onProgress).not.toHaveBeenCalled();
  });
});

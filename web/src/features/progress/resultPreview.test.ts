import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { observeResultPreview, type ResultPreviewState } from './resultPreview';

const mocks = vi.hoisted(() => ({ thumbnail: vi.fn(), page: vi.fn(), blob: vi.fn() }));
vi.mock('../parts/modelThumbnail', () => ({ loadModelThumbnail: mocks.thumbnail }));
vi.mock('../../api/assemblyDrawings', () => ({ fetchPageFile: mocks.page }));
vi.mock('../../api/endpoints', () => ({ getBlob: mocks.blob }));
const model = { kind: 'model', partId: 'part', revisionId: 'revision' } as const;
const drawing = { kind: 'assembly_svg', drawingId: 'package', pageKey: 'GA-1', sha: 'svg-sha' } as const;
const flush = async () => { for (let i = 0; i < 20; i += 1) await Promise.resolve(); };

beforeEach(() => {
  vi.resetAllMocks();
  vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:saved-svg');
  vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {});
});
afterEach(() => vi.restoreAllMocks());

describe('saved result preview lifecycle', () => {
  it('uses the shared exact model thumbnail and labels missing or failed geometry separately', async () => {
    const states: ResultPreviewState[] = [];
    mocks.thumbnail.mockResolvedValueOnce('data:image/png,part').mockResolvedValueOnce(null).mockRejectedValueOnce(new Error('offline'));
    const stop = observeResultPreview(model, (s) => states.push(s));
    await vi.waitFor(() => expect(states.at(-1)).toEqual({ status: 'ready', url: 'data:image/png,part' }));
    stop();
    observeResultPreview(model, (s) => states.push(s));
    await vi.waitFor(() => expect(states.at(-1)).toEqual({ status: 'missing' }));
    observeResultPreview(model, (s) => states.push(s));
    await vi.waitFor(() => expect(states.at(-1)?.status).toBe('error'));
    expect(mocks.thumbnail).toHaveBeenCalledWith('part', 'revision');
    expect(URL.createObjectURL).not.toHaveBeenCalled();
  });

  it('authorizes assembly pages through the package endpoint and revokes the one URL shared with the dialog', async () => {
    mocks.page.mockResolvedValue({ blob: new Blob(['<svg/>']), sha256: 'svg-sha' });
    const state = vi.fn();
    const stop = observeResultPreview(drawing, state);
    await flush();
    expect(mocks.page).toHaveBeenCalledWith('package', 'GA-1', 'svg', { signal: expect.any(AbortSignal) });
    expect(mocks.blob).not.toHaveBeenCalled();
    expect(state).toHaveBeenLastCalledWith({ status: 'ready', url: 'blob:saved-svg' });
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1);
    stop();
    stop();
    expect(URL.revokeObjectURL).toHaveBeenCalledTimes(1);
  });

  it('loads a part SVG through its authenticated saved blob', async () => {
    mocks.blob.mockResolvedValue(new Blob(['<svg/>']));
    const state = vi.fn();
    const stop = observeResultPreview({ kind: 'drawing_svg', sha: 'part-svg' }, state);
    await flush();
    expect(mocks.blob).toHaveBeenCalledWith('part-svg', { signal: expect.any(AbortSignal) });
    expect(mocks.page).not.toHaveBeenCalled();
    expect(state).toHaveBeenLastCalledWith({ status: 'ready', url: 'blob:saved-svg' });
    stop();
  });

  it('aborts stale package responses before allocating or publishing an object URL', async () => {
    let finish!: (value: unknown) => void;
    mocks.page.mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    const state = vi.fn();
    const stop = observeResultPreview(drawing, state);
    stop();
    finish({ blob: new Blob(['old svg']), sha256: 'svg-sha' });
    await flush();
    expect(mocks.page.mock.calls[0][3].signal.aborted).toBe(true);
    expect(state).toHaveBeenCalledTimes(1);
    expect(URL.createObjectURL).not.toHaveBeenCalled();
  });

  it('discards late model results after a revision or run is replaced', async () => {
    let finish!: (value: string) => void;
    mocks.thumbnail.mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    const state = vi.fn();
    const stop = observeResultPreview(model, state);
    await vi.waitFor(() => expect(mocks.thumbnail).toHaveBeenCalled());
    stop();
    finish('data:image/png,old');
    await flush();
    expect(state).toHaveBeenCalledTimes(1);
  });

  it('rejects mismatched saved SVG bytes and permits a transient request to be retried', async () => {
    const state = vi.fn();
    mocks.page.mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce({ blob: new Blob(['<svg/>']), sha256: 'different-sha' })
      .mockResolvedValueOnce({ blob: new Blob(['<svg/>']), sha256: 'svg-sha' });
    observeResultPreview(drawing, state);
    await flush();
    expect(state.mock.lastCall?.[0].status).toBe('error');
    observeResultPreview(drawing, state);
    await flush();
    expect(state.mock.lastCall?.[0].status).toBe('error');
    expect(URL.createObjectURL).not.toHaveBeenCalled();
    const stop = observeResultPreview(drawing, state);
    await flush();
    expect(state.mock.lastCall?.[0].status).toBe('ready');
    stop();
  });
});

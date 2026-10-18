import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ModelRevision } from '../../api/types';
import type { MeshDoc } from '../../three/viewer';

type ThumbnailRevision = Pick<ModelRevision, 'id' | 'part_id' | 'status' | 'mesh_sha' | 'glb_sha'>;

const mocks = vi.hoisted(() => ({
  getRevision: vi.fn<(id: string) => Promise<ThumbnailRevision>>(),
  getBlob: vi.fn<(sha: string) => Promise<Blob>>(),
  loadMeshDoc: vi.fn<(sha: string) => Promise<MeshDoc>>(),
  parseGlb: vi.fn<(buffer: ArrayBuffer) => Promise<void>>(),
  createViewer: vi.fn(),
  setMesh: vi.fn(),
  snapshot: vi.fn<() => string>(),
  dispose: vi.fn(),
  current: '',
}));

vi.mock('../../api/endpoints', () => ({ getRevision: mocks.getRevision, getBlob: mocks.getBlob }));
vi.mock('../part/model/hooks', () => ({ loadMeshDoc: mocks.loadMeshDoc }));
vi.mock('../../three/viewer', () => ({
  MeshViewer: class {
    constructor() { mocks.createViewer(); }
    setGridVisible() {}
    setBBoxVisible() {}
    setView() {}
    setMesh(mesh: MeshDoc | null) {
      mocks.setMesh(mesh);
      mocks.current = mesh?.format ?? '';
    }
    async setGlb(buffer: ArrayBuffer) {
      await mocks.parseGlb(buffer);
      mocks.current = 'glb';
    }
    snapshot() { return mocks.snapshot(); }
    dispose() { mocks.dispose(); }
  },
}));

function revision(overrides: Partial<ThumbnailRevision> = {}): ThumbnailRevision {
  return { id: 'r1', part_id: 'p1', status: 'ready', mesh_sha: 'mesh1', glb_sha: null, ...overrides };
}

function mesh(format: string): MeshDoc {
  return {
    format, bbox: {}, edges: [],
    faces: [{ id: 1, surface: 'mesh', center: [0, 0, 0], area: 1, positions: [0, 0, 0, 1, 0, 0, 0, 1, 0], indices: [0, 1, 2] }],
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

let hosts: { remove: ReturnType<typeof vi.fn> }[];

beforeEach(() => {
  vi.resetModules();
  vi.resetAllMocks();
  vi.useFakeTimers();
  mocks.current = '';
  mocks.getRevision.mockResolvedValue(revision());
  mocks.loadMeshDoc.mockImplementation(async (sha) => mesh(sha));
  mocks.parseGlb.mockResolvedValue(undefined);
  mocks.snapshot.mockImplementation(() => `data:image/png,${mocks.current}`);
  hosts = [];
  vi.stubGlobal('document', {
    createElement: () => {
      const host = { style: { cssText: '' }, setAttribute: vi.fn(), remove: vi.fn() };
      hosts.push(host);
      return host;
    },
    body: { appendChild: vi.fn() },
  });
});

afterEach(async () => {
  await vi.runOnlyPendingTimersAsync();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('model thumbnails', () => {
  it('deduplicates concurrent requests but renders a new image for a new revision', async () => {
    const { loadModelThumbnail } = await import('./modelThumbnail');
    mocks.getRevision.mockImplementation(async (id) => revision({ id, mesh_sha: `mesh-${id}` }));
    const first = loadModelThumbnail('p1', 'r1');
    expect(loadModelThumbnail('p1', 'r1')).toBe(first);
    expect(await first).toBe('data:image/png,mesh-r1');
    expect(await loadModelThumbnail('p1', 'r2')).toBe('data:image/png,mesh-r2');
    expect(await loadModelThumbnail('p1', 'r1')).toBe('data:image/png,mesh-r1');
    expect(mocks.getRevision).toHaveBeenCalledTimes(2);
    expect(mocks.loadMeshDoc).toHaveBeenCalledTimes(2);
    expect(mocks.snapshot).toHaveBeenCalledTimes(2);
    expect(mocks.createViewer).toHaveBeenCalledTimes(1);
  });

  it.each([
    { id: 'other-revision' },
    { part_id: 'other-part' },
    { status: 'failed' as const },
    { mesh_sha: null, glb_sha: null },
  ])('does not preview mismatched or unavailable geometry: %j', async (overrides) => {
    const { loadModelThumbnail } = await import('./modelThumbnail');
    mocks.getRevision.mockResolvedValue(revision(overrides));
    expect(await loadModelThumbnail('p1', 'r1')).toBeNull();
    expect(mocks.loadMeshDoc).not.toHaveBeenCalled();
    expect(mocks.getBlob).not.toHaveBeenCalled();
    expect(mocks.createViewer).not.toHaveBeenCalled();
  });

  it('leaves empty meshes available for the drawing fallback', async () => {
    const { loadModelThumbnail } = await import('./modelThumbnail');
    mocks.loadMeshDoc.mockResolvedValue({ bbox: {}, faces: [], edges: [] });
    expect(await loadModelThumbnail('p1', 'r1')).toBeNull();
    expect(mocks.snapshot).not.toHaveBeenCalled();
  });

  it('keeps an asynchronous GLB and the next mesh from capturing each other', async () => {
    const { loadModelThumbnail } = await import('./modelThumbnail');
    const parsing = deferred<void>();
    const started = deferred<void>();
    mocks.getRevision.mockImplementation(async (id) => revision({ id, mesh_sha: id === 'glb' ? null : 'mesh1', glb_sha: id === 'glb' ? 'glb1' : null }));
    mocks.getBlob.mockResolvedValue(new Blob(['glb']));
    mocks.parseGlb.mockImplementation(() => { started.resolve(); return parsing.promise; });
    const glbPreview = loadModelThumbnail('p1', 'glb');
    await started.promise;
    const meshPreview = loadModelThumbnail('p1', 'r1');
    await vi.advanceTimersByTimeAsync(0);
    expect(mocks.snapshot).not.toHaveBeenCalled();
    expect(mocks.setMesh).not.toHaveBeenCalled();
    parsing.resolve();
    expect(await glbPreview).toBe('data:image/png,glb');
    expect(await meshPreview).toBe('data:image/png,mesh1');
    expect(mocks.getBlob).toHaveBeenCalledWith('glb1');
    expect(mocks.createViewer).toHaveBeenCalledTimes(1);
  });

  it('can retry failed requests and continue rendering after a snapshot failure', async () => {
    const { loadModelThumbnail } = await import('./modelThumbnail');
    mocks.getRevision.mockRejectedValueOnce(new Error('offline'));
    await expect(loadModelThumbnail('p1', 'r1')).rejects.toThrow('offline');
    mocks.snapshot.mockImplementationOnce(() => { throw new Error('context unavailable'); });
    await expect(loadModelThumbnail('p1', 'r1')).rejects.toThrow('context unavailable');
    expect(mocks.setMesh).toHaveBeenLastCalledWith(null);
    expect(await loadModelThumbnail('p1', 'r1')).toBe('data:image/png,mesh1');
    expect(mocks.getRevision).toHaveBeenCalledTimes(3);
  });

  it('releases the shared viewer after idle time while retaining static previews', async () => {
    const { loadModelThumbnail } = await import('./modelThumbnail');
    mocks.getRevision.mockImplementation(async (id) => revision({ id }));
    await loadModelThumbnail('p1', 'r1');
    await vi.advanceTimersByTimeAsync(60_000);
    expect(mocks.dispose).toHaveBeenCalledTimes(1);
    expect(hosts[0].remove).toHaveBeenCalledTimes(1);
    expect(await loadModelThumbnail('p1', 'r1')).toBe('data:image/png,mesh1');
    expect(mocks.createViewer).toHaveBeenCalledTimes(1);
    await loadModelThumbnail('p1', 'r2');
    expect(mocks.createViewer).toHaveBeenCalledTimes(2);
    expect(hosts).toHaveLength(2);
  });
});

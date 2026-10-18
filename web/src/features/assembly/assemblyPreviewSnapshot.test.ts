import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Scene, SceneDefinition } from '../../api/assemblies';
import type { MeshDoc } from '../../three/viewer';
import { loadSavedAssemblyPreview } from './assemblyPreviewSnapshot';

const mocks = vi.hoisted(() => ({
  revision: vi.fn(), scene: vi.fn(), mesh: vi.fn(), viewer: vi.fn(), occurrences: vi.fn(),
  snapshot: vi.fn(), dispose: vi.fn(), grid: vi.fn(),
}));
vi.mock('../../api/assemblies', () => ({ getAssemblyRevision: mocks.revision, getScene: mocks.scene }));
vi.mock('../part/model/hooks', () => ({ loadMeshDoc: mocks.mesh }));
vi.mock('./AssemblyViewer', () => ({ AssemblyViewer: class {
  constructor(...args: unknown[]) { mocks.viewer(...args); }
  setOccurrences(...args: unknown[]) { mocks.occurrences(...args); }
  setGridVisible(value: boolean) { mocks.grid(value); }
  snapshot() { return mocks.snapshot(); }
  dispose() { mocks.dispose(); }
} }));

const world = [[1, 0, 0, 20], [0, 1, 0, -30], [0, 0, 1, 5], [0, 0, 0, 1]];
const target = { assemblyId: 'cart', revisionId: 'saved-a1' };
const mesh: MeshDoc = { bbox: {}, edges: [], faces: [
  { id: 1, surface: 'plane', center: [0, 0, 0], area: 1, positions: [0, 0, 0, 1, 0, 0, 0, 1, 0], indices: [0, 1, 2] },
] };
const definition = (sha: string | null): SceneDefinition => ({
  part_id: 'part-1', number: 'P1', name: 'wheel', rev_no: 1, label: 'R1',
  glb_sha: null, mesh_sha: sha, bbox: null, material: null,
});
const occurrence = (path: string[], part_key = 'wheel-r1') => ({ path, part_key, names: path, world });
const scene = (): Scene => ({
  revision_id: 'saved-a1', tree: [], definitions: { 'wheel-r1': definition('mesh-1'), unused: definition('unused') },
  occurrences: [occurrence(['left', 'wheel']), occurrence(['right', 'wheel'])],
});
const load = (signal = new AbortController().signal) => loadSavedAssemblyPreview(target, {} as HTMLElement, 'light', signal);

beforeEach(() => {
  vi.resetAllMocks();
  mocks.revision.mockResolvedValue({ id: 'saved-a1', assembly_id: 'cart' });
  mocks.scene.mockResolvedValue(scene());
  mocks.mesh.mockResolvedValue(mesh);
  mocks.snapshot.mockReturnValue('data:image/png,saved-scene');
});

describe('saved assembly preview', () => {
  it('loads the exact revision and each used mesh once, retaining every occurrence transform', async () => {
    const signal = new AbortController().signal;
    expect(await load(signal)).toEqual({ status: 'ready', url: 'data:image/png,saved-scene', total: 2, missing: 0 });
    expect(mocks.revision).toHaveBeenCalledWith('saved-a1', { signal });
    expect(mocks.scene).toHaveBeenCalledWith('saved-a1', { signal });
    expect(mocks.mesh).toHaveBeenCalledTimes(1);
    expect(mocks.mesh).toHaveBeenCalledWith('mesh-1', signal);
    expect(mocks.occurrences).toHaveBeenCalledWith(new Map([['wheel-r1', mesh]]), [
      { key: 'left/wheel', partKey: 'wheel-r1', world }, { key: 'right/wheel', partKey: 'wheel-r1', world },
    ], { fit: true });
    expect(mocks.dispose).toHaveBeenCalledTimes(1);
  });

  it.each([{ id: 'latest-a2', assembly_id: 'cart' }, { id: 'saved-a1', assembly_id: 'different-cart' }])(
    'rejects a mismatched saved identity: %j', async (revision) => {
      mocks.revision.mockResolvedValue(revision);
      await expect(load()).rejects.toThrow();
      expect(mocks.scene).not.toHaveBeenCalled();
      expect(mocks.viewer).not.toHaveBeenCalled();
    },
  );

  it('rejects a scene for another revision without requesting its meshes', async () => {
    mocks.scene.mockResolvedValue({ ...scene(), revision_id: 'latest-a2' });
    await expect(load()).rejects.toThrow();
    expect(mocks.mesh).not.toHaveBeenCalled();
  });

  it('reports an empty scene without producing a blank successful snapshot', async () => {
    mocks.scene.mockResolvedValue({ ...scene(), occurrences: [] });
    expect(await load()).toEqual({ status: 'empty', total: 0, missing: 0 });
    expect(mocks.mesh).not.toHaveBeenCalled();
    expect(mocks.viewer).not.toHaveBeenCalled();
  });

  it('labels partial previews by missing instance count, including unavailable definitions', async () => {
    mocks.scene.mockResolvedValue({ ...scene(), occurrences: [...scene().occurrences, occurrence(['unknown'], 'absent')] });
    expect(await load()).toEqual({ status: 'ready', url: 'data:image/png,saved-scene', total: 3, missing: 1 });
    expect(mocks.occurrences.mock.calls[0][1]).toHaveLength(2);
  });

  it.each(['missing-sha', 'fetch-failed', 'empty-mesh', 'invalid-mesh'])('reports unavailable geometry: %s', async (kind) => {
    if (kind === 'missing-sha') mocks.scene.mockResolvedValue({ ...scene(), definitions: { 'wheel-r1': definition(null) } });
    if (kind === 'fetch-failed') mocks.mesh.mockRejectedValue(new Error('not found'));
    if (kind === 'empty-mesh') mocks.mesh.mockResolvedValue({ ...mesh, faces: [] });
    if (kind === 'invalid-mesh') mocks.mesh.mockResolvedValue({ ...mesh, faces: [{ ...mesh.faces[0], indices: [0, 1, 99] }] });
    expect(await load()).toEqual({ status: 'unavailable', total: 2, missing: 2 });
    expect(mocks.viewer).not.toHaveBeenCalled();
  });

  it('aborts before network access and drops a late mesh after cancellation', async () => {
    const early = new AbortController(); early.abort();
    await expect(load(early.signal)).rejects.toMatchObject({ name: 'AbortError' });
    expect(mocks.revision).not.toHaveBeenCalled();
    const controller = new AbortController();
    mocks.mesh.mockImplementation(async () => { controller.abort(); return mesh; });
    await expect(load(controller.signal)).rejects.toMatchObject({ name: 'AbortError' });
    expect(mocks.viewer).not.toHaveBeenCalled();
  });

  it('releases WebGL resources when rendering a snapshot fails', async () => {
    mocks.snapshot.mockImplementation(() => { throw new Error('context lost'); });
    await expect(load()).rejects.toThrow('context lost');
    expect(mocks.dispose).toHaveBeenCalledTimes(1);
  });
});

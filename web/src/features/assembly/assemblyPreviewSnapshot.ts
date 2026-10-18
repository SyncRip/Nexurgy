import { getAssemblyRevision, getScene } from '../../api/assemblies';
import type { ResolvedTheme } from '../../prefs/ThemeProvider';
import type { MeshDoc } from '../../three/viewer';
import { loadMeshDoc } from '../part/model/hooks';
import { pathKey } from './model';

export type SavedAssemblyTarget = { assemblyId: string; revisionId: string };
export type AssemblyPreviewSnapshot =
  | { status: 'ready'; url: string; total: number; missing: number }
  | { status: 'empty' | 'unavailable'; total: number; missing: number };

function hasGeometry(doc: MeshDoc): boolean {
  return Array.isArray(doc.faces) && doc.faces.some((f) => f.positions?.length >= 9 && f.indices?.length >= 3)
    && doc.faces.every((f) => Array.isArray(f.positions) && f.positions.length % 3 === 0 && f.positions.every(Number.isFinite)
      && Array.isArray(f.indices) && f.indices.length % 3 === 0
      && f.indices.every((i) => Number.isInteger(i) && i >= 0 && i < f.positions.length / 3));
}

/** Read the saved scene and authenticated part meshes; the temporary viewer lives only while taking its PNG. */
export async function loadSavedAssemblyPreview(
  target: SavedAssemblyTarget, host: HTMLElement, theme: ResolvedTheme, signal: AbortSignal,
): Promise<AssemblyPreviewSnapshot> {
  signal.throwIfAborted();
  const revision = await getAssemblyRevision(target.revisionId, { signal });
  signal.throwIfAborted();
  if (revision.id !== target.revisionId || revision.assembly_id !== target.assemblyId) throw new Error('Saved assembly identity mismatch');
  const scene = await getScene(target.revisionId, { signal });
  signal.throwIfAborted();
  if (scene.revision_id !== target.revisionId) throw new Error('Saved assembly scene mismatch');
  const total = scene.occurrences.length;
  if (!total) return { status: 'empty', total: 0, missing: 0 };

  const docs = new Map<string, MeshDoc>();
  await Promise.all([...new Set(scene.occurrences.map((o) => o.part_key))].map(async (key) => {
    const sha = scene.definitions[key]?.mesh_sha;
    if (!sha) return;
    try {
      const doc = await loadMeshDoc(sha, signal);
      signal.throwIfAborted();
      if (hasGeometry(doc)) docs.set(key, doc);
    } catch {
      signal.throwIfAborted(); // Missing meshes are a partial preview; cancellation never becomes one.
    }
  }));
  signal.throwIfAborted();
  const occurrences = scene.occurrences.filter((o) => docs.has(o.part_key))
    .map((o) => ({ key: pathKey(o.path), partKey: o.part_key, world: o.world }));
  const missing = total - occurrences.length;
  if (!occurrences.length) return { status: 'unavailable', total, missing };

  const { AssemblyViewer } = await import('./AssemblyViewer');
  signal.throwIfAborted();
  const viewer = new AssemblyViewer(host, { theme });
  try {
    viewer.setGridVisible(false);
    viewer.setOccurrences(docs, occurrences, { fit: true });
    return { status: 'ready', url: viewer.snapshot(), total, missing };
  } finally {
    viewer.dispose();
  }
}

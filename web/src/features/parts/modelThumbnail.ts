import { getBlob, getRevision } from '../../api/endpoints';
import { MeshViewer, type MeshDoc } from '../../three/viewer';
import { loadMeshDoc } from '../part/model/hooks';

const previews = new Map<string, Promise<string | null>>();
const CACHE_LIMIT = 96;
let renderer: { viewer: MeshViewer; host: HTMLDivElement } | undefined;
let idleTimer: ReturnType<typeof setTimeout> | undefined;
let renderQueue: Promise<unknown> = Promise.resolve();

function releaseRenderer() {
  clearTimeout(idleTimer);
  renderer?.viewer.dispose();
  renderer?.host.remove();
  renderer = undefined;
}

function getViewer(): MeshViewer {
  if (renderer) return renderer.viewer;
  const host = document.createElement('div');
  host.setAttribute('aria-hidden', 'true');
  host.style.cssText = 'position:fixed;left:-10000px;top:0;width:300px;height:164px;pointer-events:none;contain:strict';
  document.body.appendChild(host);
  try {
    const viewer = new MeshViewer(host, { theme: 'light' });
    viewer.setGridVisible(false);
    viewer.setBBoxVisible(false);
    renderer = { viewer, host };
    return viewer;
  } catch (error) {
    host.remove();
    throw error;
  }
}

/** One offscreen viewer serves every card; only its static PNG stays in the list. */
function snapshot(source: { mesh: MeshDoc } | { glb: ArrayBuffer }): Promise<string> {
  const job = renderQueue.then(async () => {
    clearTimeout(idleTimer);
    const viewer = getViewer();
    try {
      if ('mesh' in source) viewer.setMesh(source.mesh, { fit: true });
      else await viewer.setGlb(source.glb, { fit: true });
      viewer.setView('iso');
      return viewer.snapshot();
    } finally {
      viewer.setMesh(null);
      idleTimer = setTimeout(releaseRenderer, 60_000);
    }
  });
  // A failed model must not prevent the next card from rendering.
  renderQueue = job.catch(() => undefined);
  return job;
}

/** Exact-revision previews, fetched with the same authenticated API as the model tab. */
export function loadModelThumbnail(partId: string, revisionId: string): Promise<string | null> {
  const key = `${partId}:${revisionId}`;
  const hit = previews.get(key);
  if (hit) return hit;
  const result = (async () => {
    const revision = await getRevision(revisionId);
    if (revision.id !== revisionId || revision.part_id !== partId || revision.status !== 'ready') return null;
    if (revision.mesh_sha) {
      const mesh = await loadMeshDoc(revision.mesh_sha);
      if (!mesh.faces.some((face) => face.positions.length >= 9 && face.indices.length >= 3)) return null;
      return snapshot({ mesh });
    }
    if (revision.glb_sha) {
      const blob = await getBlob(revision.glb_sha);
      return snapshot({ glb: await blob.arrayBuffer() });
    }
    return null;
  })();
  previews.set(key, result);
  while (previews.size > CACHE_LIMIT) previews.delete(previews.keys().next().value!);
  void result.catch(() => previews.delete(key));
  return result;
}

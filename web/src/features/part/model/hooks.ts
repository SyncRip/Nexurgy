// Data hooks for the model tab: revision meshes (authenticated blob fetch,
// small in-memory cache) and the debounced live preview through the kernel.

import { useEffect, useRef, useState } from 'react';
import { isAbortError, isApiError } from '../../../api/client';
import { getBlob, previewCad } from '../../../api/endpoints';
import type { BuildSummary, ModelOp, ModelRevision, StepBase } from '../../../api/types';
import type { MeshDoc } from '../../../three/viewer';
import { canonical } from '../../editor/ops';

const meshCache = new Map<string, MeshDoc>();
const glbCache = new Map<string, ArrayBuffer>();

function remember<T>(map: Map<string, T>, key: string, value: T, limit = 10) {
  map.delete(key);
  map.set(key, value);
  while (map.size > limit) map.delete(map.keys().next().value as string);
}

export async function loadMeshDoc(sha: string, signal?: AbortSignal): Promise<MeshDoc> {
  const hit = meshCache.get(sha);
  if (hit) return hit;
  const blob = await getBlob(sha, { signal });
  const doc = JSON.parse(await blob.text()) as MeshDoc;
  remember(meshCache, sha, doc);
  return doc;
}

export interface RevisionMesh {
  doc: MeshDoc | null;
  glb: ArrayBuffer | null;
  loading: boolean;
  error: unknown;
}

/** mesh_json of a revision (or the GLB of a GLB mesh reference that has no mesh_json). */
export function useRevisionMesh(rev: ModelRevision | null): RevisionMesh {
  const meshSha = rev?.mesh_sha ?? null;
  const glbSha = !meshSha && rev?.mesh_only ? (rev?.glb_sha ?? null) : null;
  const key = meshSha ?? glbSha ?? '';
  // Remember which blob the loaded data belongs to, so a stale model is never
  // returned for a newly selected revision (the camera fits on new models).
  const [state, setState] = useState<RevisionMesh & { key: string }>({ key: '', doc: null, glb: null, loading: false, error: null });
  useEffect(() => {
    if (!key) {
      setState({ key: '', doc: null, glb: null, loading: false, error: null });
      return;
    }
    if (meshSha && meshCache.has(meshSha)) {
      setState({ key, doc: meshCache.get(meshSha)!, glb: null, loading: false, error: null });
      return;
    }
    if (glbSha && glbCache.has(glbSha)) {
      setState({ key, doc: null, glb: glbCache.get(glbSha)!, loading: false, error: null });
      return;
    }
    const ctrl = new AbortController();
    setState({ key, doc: null, glb: null, loading: true, error: null });
    (async () => {
      try {
        if (meshSha) {
          const doc = await loadMeshDoc(meshSha, ctrl.signal);
          if (!ctrl.signal.aborted) setState({ key, doc, glb: null, loading: false, error: null });
        } else if (glbSha) {
          const blob = await getBlob(glbSha, { signal: ctrl.signal });
          const buf = await blob.arrayBuffer();
          remember(glbCache, glbSha, buf, 4);
          if (!ctrl.signal.aborted) setState({ key, doc: null, glb: buf, loading: false, error: null });
        }
      } catch (e) {
        if (ctrl.signal.aborted || isAbortError(e)) return;
        setState({ key, doc: null, glb: null, loading: false, error: e });
      }
    })();
    return () => ctrl.abort();
  }, [key, meshSha, glbSha]);
  if (state.key !== key) return { doc: null, glb: null, loading: !!key, error: null };
  return { doc: state.doc, glb: state.glb, loading: state.loading, error: state.error };
}

export interface InvalidOpsItem {
  loc: string;
  msg: string;
}

export interface PreviewState {
  status: 'idle' | 'pending' | 'building' | 'ok' | 'failed' | 'invalid' | 'error';
  result: BuildSummary | null;
  /** Mesh of this build (on failure: the last good state before the failing op), if any. */
  mesh: MeshDoc | null;
  invalid: InvalidOpsItem[];
  error: unknown;
}

const IDLE: PreviewState = { status: 'idle', result: null, mesh: null, invalid: [], error: null };

/**
 * Debounced POST /cad/preview of the working spec. Disabled → idle (the
 * saved revision's own mesh is shown instead). Responses for an outdated
 * spec are dropped.
 */
export function usePreview(partId: string, base: StepBase | null, ops: ModelOp[], enabled: boolean, delayMs = 450): PreviewState & { lastGoodMesh: MeshDoc | null } {
  const [state, setState] = useState<PreviewState>(IDLE);
  const lastGood = useRef<MeshDoc | null>(null);
  const key = enabled ? canonical({ base, ops }) : '';
  const specRef = useRef({ base, ops });
  specRef.current = { base, ops };

  useEffect(() => {
    if (!enabled) {
      setState(IDLE);
      return;
    }
    const ctrl = new AbortController();
    setState((s) => ({ ...s, status: 'pending' }));
    const timer = setTimeout(async () => {
      setState((s) => ({ ...s, status: 'building' }));
      try {
        const { base: b, ops: o } = specRef.current;
        const res = await previewCad({ part_id: partId, base: b, ops: o }, { signal: ctrl.signal });
        if (ctrl.signal.aborted) return;
        const mesh = (res.mesh as unknown as MeshDoc | null) ?? null;
        if (mesh && mesh.faces?.length) lastGood.current = mesh;
        setState({ status: res.result.ok ? 'ok' : 'failed', result: res.result, mesh, invalid: [], error: null });
      } catch (e) {
        if (ctrl.signal.aborted || isAbortError(e)) return;
        if (isApiError(e) && e.code === 'invalid_ops') {
          const errors = ((e.details.errors as InvalidOpsItem[] | undefined) ?? []).map((x) => ({ loc: String(x.loc ?? ''), msg: String(x.msg ?? '') }));
          setState({ status: 'invalid', result: null, mesh: null, invalid: errors, error: e });
        } else {
          setState({ status: 'error', result: null, mesh: null, invalid: [], error: e });
        }
      }
    }, delayMs);
    return () => {
      clearTimeout(timer);
      ctrl.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, enabled, partId]);

  return { ...state, lastGoodMesh: lastGood.current };
}

import { fetchPageFile } from '../../api/assemblyDrawings';
import { getBlob } from '../../api/endpoints';

export type ResultPreviewTarget =
  | { kind: 'model'; partId: string; revisionId: string }
  | { kind: 'assembly_svg'; drawingId: string; pageKey: string; sha: string }
  | { kind: 'drawing_svg'; sha: string };

export type ResultPreviewState =
  | { status: 'loading' | 'missing' | 'error' }
  | { status: 'ready'; url: string };

export function resultPreviewIdentity(target: ResultPreviewTarget | null): string {
  if (!target) return '';
  switch (target.kind) {
    case 'model': return `model/${target.partId}/${target.revisionId}`;
    case 'assembly_svg': return `assembly/${target.drawingId}/${target.pageKey}/${target.sha}`;
    case 'drawing_svg': return `drawing/${target.sha}`;
  }
}

/** One card owns its image lifetime; the zoom dialog uses the same URL, with no second request or renderer. */
export function observeResultPreview(target: ResultPreviewTarget, onState: (state: ResultPreviewState) => void): () => void {
  const controller = new AbortController();
  const { signal } = controller;
  let objectUrl: string | null = null;
  onState({ status: 'loading' });
  void (async () => {
    if (target.kind === 'model') {
      const { loadModelThumbnail } = await import('../parts/modelThumbnail');
      if (signal.aborted) return;
      const url = await loadModelThumbnail(target.partId, target.revisionId);
      if (!signal.aborted) onState(url ? { status: 'ready', url } : { status: 'missing' });
      return;
    }
    let blob: Blob;
    if (target.kind === 'assembly_svg') {
      const file = await fetchPageFile(target.drawingId, target.pageKey, 'svg', { signal });
      if (file.sha256 && file.sha256 !== target.sha) throw new Error('Saved page identity mismatch');
      blob = file.blob;
    } else {
      blob = await getBlob(target.sha, { signal });
    }
    if (signal.aborted) return;
    objectUrl = URL.createObjectURL(blob);
    onState({ status: 'ready', url: objectUrl });
  })().catch(() => {
    if (!signal.aborted) onState({ status: 'error' });
  });
  return () => {
    controller.abort();
    if (objectUrl) URL.revokeObjectURL(objectUrl);
    objectUrl = null;
  };
}

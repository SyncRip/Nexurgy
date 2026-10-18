import { lazy, Suspense, useEffect, useRef, useState } from 'react';
import type { CadResult } from '../../api/assistant';
import { blobUrl, type BlobHandle } from '../../api/blobs';
import { getBlob } from '../../api/endpoints';
import { useI18n } from '../../i18n/I18nProvider';
import { useTheme } from '../../prefs/ThemeProvider';
import type { MeshDoc, MeshViewer } from '../../three/viewer';
import type { SavedAssemblyTarget } from '../assembly/assemblyPreviewSnapshot';

const SavedDrawingPreview = lazy(() => import('../drafting/SavedDrawingPreview'));
const SavedAssemblyPreview = lazy(() => import('../assembly/SavedAssemblyPreview'));
export type SavedDrawingTarget = { documentId: string; versionId: string };

export function savedDrawingPreviewTarget(result: Pick<CadResult, 'object_type' | 'object_id' | 'version_id'>): SavedDrawingTarget | undefined {
  return result.object_type === 'drawing_document' && result.object_id && result.version_id
    ? { documentId: result.object_id, versionId: result.version_id } : undefined;
}

export function savedAssemblyPreviewTarget(result: Pick<CadResult, 'object_type' | 'object_id' | 'version_id'>): SavedAssemblyTarget | undefined {
  return result.object_type === 'assembly' && result.object_id && result.version_id
    ? { assemblyId: result.object_id, revisionId: result.version_id } : undefined;
}

type Artifact = CadResult['artifacts'][number];
const IMAGE_KINDS = ['svg', 'png', 'jpg', 'jpeg', 'webp'];
export function resultPreviewArtifact(artifacts: readonly Artifact[]): Artifact | null {
  return artifacts.find((file) => IMAGE_KINDS.includes(file.kind.toLowerCase()))
    ?? artifacts.find((file) => file.kind === 'mesh')
    ?? artifacts.find((file) => file.kind === 'glb') ?? null;
}

/** Thumbnail from an authenticated saved artifact; no geometry or image is invented. */
export function CadResultPreview({ artifacts, label, drawing, assembly }: {
  artifacts: Artifact[]; label: string; drawing?: SavedDrawingTarget; assembly?: SavedAssemblyTarget;
}) {
  const { t } = useI18n();
  const { resolved } = useTheme();
  const root = useRef<HTMLDivElement>(null);
  const host = useRef<HTMLDivElement>(null);
  const artifact = assembly ? null : resultPreviewArtifact(artifacts);
  const [visible, setVisible] = useState(false);
  const [state, setState] = useState<{ key: string; url?: string; failed?: boolean }>({ key: '' });
  const key = artifact ? `${artifact.kind}:${artifact.sha256}:${resolved}` : '';
  useEffect(() => {
    if (!root.current) return;
    if (typeof IntersectionObserver === 'undefined') { setVisible(true); return; }
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) { setVisible(true); observer.disconnect(); }
    }, { rootMargin: '160px' });
    observer.observe(root.current);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    if (!visible || !artifact || !host.current) return;
    const controller = new AbortController();
    let image: BlobHandle | null = null;
    let viewer: MeshViewer | null = null;
    const selected = artifact;
    const render = async () => {
      try {
        if (IMAGE_KINDS.includes(selected.kind.toLowerCase())) {
          image = await blobUrl(selected.sha256, { signal: controller.signal });
          if (controller.signal.aborted) { image.revoke(); return; }
          setState({ key, url: image.url });
          return;
        }
        const blob = await getBlob(selected.sha256, { signal: controller.signal });
        const data = selected.kind === 'mesh' ? JSON.parse(await blob.text()) as MeshDoc : await blob.arrayBuffer();
        const { MeshViewer: Viewer } = await import('../../three/viewer');
        if (controller.signal.aborted || !host.current) return;
        viewer = new Viewer(host.current, { theme: resolved });
        viewer.setGridVisible(false);
        if (selected.kind === 'mesh') viewer.setMesh(data as MeshDoc, { fit: true });
        else await viewer.setGlb(data as ArrayBuffer, { fit: true });
        if (!controller.signal.aborted && viewer) setState({ key, url: viewer.snapshot() });
      } catch {
        if (!controller.signal.aborted) setState({ key, failed: true });
      } finally {
        viewer?.dispose();
        viewer = null;
      }
    };
    void render();
    return () => { controller.abort(); image?.revoke(); viewer?.dispose(); viewer = null; };
  }, [visible, key, resolved]);
  const current = state.key === key ? state : null;
  return <div className="as-cad-preview" ref={root}>
    <div className="as-cad-preview-host" ref={host} aria-hidden />
    {assembly ? visible ? <Suspense fallback={<span className="faint small">{t('core.result.assemblyLoading')}</span>}><SavedAssemblyPreview {...assembly} label={label} /></Suspense> : <span className="faint small">{t('core.result.assemblyLoading')}</span>
      : !artifact && drawing ? visible ? <Suspense fallback={<span className="faint small">{t('common.loading')}</span>}><SavedDrawingPreview documentId={drawing.documentId} versionId={drawing.versionId} label={label} /></Suspense> : <span className="faint small">{t('common.loading')}</span>
      : current?.url ? <img src={current.url} alt={t('core.result.preview', { version: label })} />
      : <span className="faint small">{t(!artifact || current?.failed ? 'core.result.noPreview' : 'common.loading')}</span>}
  </div>;
}

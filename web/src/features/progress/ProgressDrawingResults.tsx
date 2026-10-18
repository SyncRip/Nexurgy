import { AlertTriangle, CheckCircle2, FileText, Minus, Plus, RotateCcw } from 'lucide-react';
import { useState } from 'react';
import { Link } from 'react-router-dom';
import type { ProgressDrawing } from '../../api/progress';
import { Dialog } from '../../components/ui/Dialog';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { drawingResultIdentity, loadDrawingResults, type DrawingResultPage } from './drawingResults';
import { ResultPreview, useResultPreview } from './useResultPreview';

/** Kept mounted by its immutable descriptor, so progress polls and diagnostic disclosure do not refetch pages. */
export function ProgressDrawingResults({ drawing }: { drawing: ProgressDrawing }) {
  const { t } = useI18n();
  const identity = drawingResultIdentity(drawing);
  const resource = useResource((signal) => loadDrawingResults(drawing, signal), [identity]);
  const saved = resource.data?.identity === identity ? resource.data : undefined;
  if (!saved) return <div className="prog-drawing-pending" role="status">
    <FileText aria-hidden />
    <span>{t(resource.error ? 'prog.result.drawingError' : 'prog.result.drawingLoading')}</span>
    {!!resource.error && <button type="button" className="linkish small" disabled={resource.loading} onClick={resource.reload}>{t('common.retry')}</button>}
  </div>;
  return <section className="prog-drawing-results" aria-label={t('prog.result.drawings')}>
    <div className="prog-drawing-heading">
      <strong>{t('prog.result.drawings')}{saved.label ? ` · ${saved.label}` : ''}</strong>
      {saved.sourcePath && <Link className="linkish small" to={saved.sourcePath}>{t('prog.result.source', { revision: saved.sourceLabel || '—' })}</Link>}
    </div>
    {saved.issues.length > 0 && <div className="prog-result-warnings">
      {saved.issues.map((issue) => <span key={issue}><AlertTriangle aria-hidden />{t(`prog.result.${issue}`)}</span>)}
    </div>}
    {saved.pages.length ? <div className="prog-result-grid">
      {saved.pages.map((page) => <DrawingPageResult key={`${identity}/${page.key}`} page={page} packageLabel={saved.label} sourceLabel={saved.sourceLabel} />)}
    </div> : <p className="prog-note">{t('prog.result.noPages')}</p>}
  </section>;
}

function DrawingPageResult({ page, packageLabel, sourceLabel }: { page: DrawingResultPage; packageLabel: string; sourceLabel: string }) {
  const { t } = useI18n();
  const preview = useResultPreview(page.preview);
  const [open, setOpen] = useState(false);
  const [zoom, setZoom] = useState(1);
  const title = page.title || t('prog.result.page', { n: page.index });
  const pageLabel = t('prog.result.page', { n: page.index });
  const versionLabel = [packageLabel, sourceLabel].filter(Boolean).join(' · ');
  const incomplete = page.status === 'partial' || page.status === 'failed';
  const status = page.status === 'failed' ? t('prog.result.pageFailed') : page.status === 'partial' ? t('prog.result.pagePartial')
    : page.preview ? t('prog.result.pageSaved') : t('prog.result.missing');
  return <article className="prog-result-card prog-drawing-card" data-page-key={page.key}>
    <button type="button" className="prog-result-image" disabled={preview.state.status !== 'ready'}
      aria-label={t('prog.result.enlarge', { page: title })} onClick={() => { setZoom(1); setOpen(true); }}>
      <ResultPreview state={preview.state} label={title} onError={preview.imageFailed} />
    </button>
    <strong className="prog-result-name">{title}</strong>
    <div className="prog-result-meta"><span>{pageLabel}</span>{versionLabel && <span>{versionLabel}</span>}</div>
    <span className={`prog-result-status ${incomplete ? 'warn' : ''}`}>{incomplete ? <AlertTriangle aria-hidden /> : page.preview ? <CheckCircle2 aria-hidden /> : <FileText aria-hidden />}{status}</span>
    {preview.state.status === 'error' && <button type="button" className="linkish small" onClick={preview.retry}>{t('common.retry')}</button>}
    {open && <Dialog title={title} subtitle={[pageLabel, versionLabel].filter(Boolean).join(' · ')} size="lg" onClose={() => setOpen(false)}
      footer={<>
        {page.path && <Link className="btn sm" to={page.path} onClick={() => setOpen(false)}>{t('prog.result.openDrawing')}</Link>}
        <button type="button" className="btn" onClick={() => setOpen(false)}>{t('dialog.close')}</button>
      </>}>
      <div className="prog-drawing-tools" role="group" aria-label={t('prog.result.zoom')}>
        <button type="button" className="btn sm" aria-label={t('prog.result.zoomOut')} disabled={zoom <= 0.5} onClick={() => setZoom((n) => Math.max(0.5, n - 0.25))}><Minus aria-hidden /></button>
        <span className="mono small" role="status">{Math.round(zoom * 100)}%</span>
        <button type="button" className="btn sm" aria-label={t('prog.result.zoomIn')} disabled={zoom >= 3} onClick={() => setZoom((n) => Math.min(3, n + 0.25))}><Plus aria-hidden /></button>
        <button type="button" className="btn sm" onClick={() => setZoom(1)}><RotateCcw aria-hidden />{t('prog.result.fit')}</button>
      </div>
      <div className="prog-drawing-zoom" tabIndex={0} aria-label={t('prog.result.drawings')}>
        {preview.state.status === 'ready' ? <img src={preview.state.url} alt={title} style={{ width: `${zoom * 100}%` }} onError={preview.imageFailed} />
          : <ResultPreview state={preview.state} label={title} onError={preview.imageFailed} />}
      </div>
      {preview.state.status === 'error' && <button type="button" className="btn sm" onClick={preview.retry}>{t('common.retry')}</button>}
    </Dialog>}
  </article>;
}

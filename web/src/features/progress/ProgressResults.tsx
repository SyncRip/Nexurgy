import { Box, CheckCircle2, Circle, FileText, PauseCircle, XCircle } from 'lucide-react';
import { Link } from 'react-router-dom';
import type { ProgressDrawing, ProgressUnit, RunProgress } from '../../api/progress';
import { partOutputPath } from '../../app/versionLinks';
import { useI18n } from '../../i18n/I18nProvider';
import { translated as tk, unitLabel } from './labels';
import { unitTone } from './model';
import { drawingResultIdentity } from './drawingResults';
import { ProgressDrawingResults } from './ProgressDrawingResults';
import { ResultPreview, useResultPreview } from './useResultPreview';
import './progress.css';

/** A primary card has already represented this exact output in its authorized run/project. */
export interface RepresentedPartRevision { runId: string; partId: string; revisionId: string }
export interface RepresentedDrawing { runId: string; drawing: Extract<ProgressDrawing, { kind: 'drawing' }> }

/** Stable unit keys retain previews as later parts commit; a shared successful revision appears only once. */
export function progressParts(progress: RunProgress, represented: readonly RepresentedPartRevision[] = []): { key: string; unit: ProgressUnit }[] {
  const seen = new Set<string>();
  return progress.stages.flatMap((stage) => stage.units.flatMap((unit) => {
    if (unit.kind !== 'definition' && unit.kind !== 'model' && !unit.revision) return [];
    const key = `${progress.run_id}/${stage.node_id}/${unit.key}`;
    const saved = unit.revision;
    const successful = (unit.status === 'built' || unit.status === 'done') && !unit.error;
    if (successful && saved && represented.some((item) => item.runId === progress.run_id && item.partId === saved.part_id && item.revisionId === saved.id)) return [];
    const identity = successful && saved ? `${saved.part_id}/${saved.id}` : key;
    if (seen.has(identity)) return [];
    seen.add(identity);
    return [{ key, unit }];
  }));
}

export function ProgressResults({ progress, representedParts = [], representedDrawings = [] }: {
  progress: RunProgress; representedParts?: readonly RepresentedPartRevision[]; representedDrawings?: readonly RepresentedDrawing[];
}) {
  const { t } = useI18n();
  const parts = progressParts(progress, representedParts);
  const drawings = [...new Map((progress.drawings ?? []).map((drawing) => [drawingResultIdentity(drawing), drawing])).values()]
    .filter((drawing) => !representedDrawings.some((item) => item.runId === progress.run_id
      && drawingResultIdentity(item.drawing) === drawingResultIdentity(drawing)));
  const pendingDrawings = progress.stages.filter((stage) => ['drawing', 'assembly_drawing'].includes(stage.node_type)
    && !(progress.drawings ?? []).some((drawing) => drawing.node_id === stage.node_id));
  if (!parts.length && !drawings.length && !pendingDrawings.length && !progress.truncated) return null;
  return <section className="prog-results" aria-label={t('prog.result.title')}>
    <h3 className="prog-results-title">{t('prog.result.title')}</h3>
    {progress.truncated && <p className="prog-note">{t('prog.truncated')}</p>}
    {parts.length > 0 && <div className="prog-result-grid prog-part-grid">
      {parts.map(({ key, unit }) => <PartResult key={key} unit={unit} />)}
    </div>}
    {drawings.map((drawing) => <ProgressDrawingResults key={`${progress.run_id}/${drawingResultIdentity(drawing)}`} drawing={drawing} />)}
    {pendingDrawings.map((stage) => <p key={stage.node_id} className="prog-drawing-pending" role="status">
      <FileText aria-hidden />{t('prog.result.drawings')} · {t(stage.status === 'failed' || stage.status === 'partial' ? 'prog.result.drawingFailed'
        : stage.status === 'running' ? 'prog.result.drawingRunning' : stage.status === 'succeeded' ? 'prog.result.noPages'
        : stage.status === 'cancelled' || stage.status === 'skipped' ? 'prog.result.drawingStopped' : 'prog.result.drawingPending')}
    </p>)}
  </section>;
}

function PartResult({ unit }: { unit: ProgressUnit }) {
  const { t } = useI18n();
  const title = unit.title || unitLabel(t, unit.key, unit.kind);
  const rev = unit.revision;
  const preview = useResultPreview(rev ? { kind: 'model', partId: rev.part_id, revisionId: rev.id } : null);
  const tone = unitTone(unit.status);
  const Icon = tone === 'err' ? XCircle : tone === 'warn' ? PauseCircle : rev && unit.status === 'built' ? CheckCircle2 : Circle;
  const built = unit.status === 'built' || unit.status === 'done';
  const status = built && !rev ? t('prog.result.noModel')
    : built && rev ? t('prog.result.saved') : tk(t, `prog.unit.${unit.status}`, unit.status);
  return <article className="prog-result-card" data-unit-key={unit.key}>
    {rev ? <Link className="prog-result-image" to={partOutputPath(rev.part_id, 'model', rev.id)} aria-label={t('prog.result.openModel', { name: title, revision: rev.label })}>
      <ResultPreview state={preview.state} label={`${title} · ${rev.label}`} onError={preview.imageFailed} />
    </Link> : <div className="prog-result-image">
      <span className="prog-result-placeholder"><Box aria-hidden /><span>{status}</span></span>
    </div>}
    <strong className="prog-result-name">{title}</strong>
    <div className="prog-result-meta">
      {unit.quantity !== null && <span>×{unit.quantity}</span>}
      {rev && <span className="mono" title={rev.id}>{rev.label}</span>}
    </div>
    <span className={`prog-result-status ${tone}`}><Icon aria-hidden />{status}</span>
    {rev && preview.state.status === 'error' && <button type="button" className="linkish small" onClick={preview.retry}>{t('common.retry')}</button>}
  </article>;
}

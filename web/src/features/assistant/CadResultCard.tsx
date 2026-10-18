import { Download,Eye,RotateCcw } from 'lucide-react';
import { useState } from 'react';
import { Link } from 'react-router-dom';
import { undoCadOperation,type AiOperation,type AiResultItem,type CadResult } from '../../api/assistant';
import { downloadBlob } from '../../api/blobs';
import type { CheckSummary } from '../../api/types';
import { assemblyRevisionPath,partOutputPath } from '../../app/versionLinks';
import { useI18n } from '../../i18n/I18nProvider';
import { DrawingPackageCard } from '../cad/DrawingPackage';
import { AcceptanceChip, CheckSummaryView, GeomCheckChip } from '../cad/Verdicts';
import { ProgressDrawingResults } from '../progress/ProgressDrawingResults';
import { useAssistantSession } from './AssistantProvider';
import { AsError,tk } from './common';
import { CadResultPreview, savedAssemblyPreviewTarget, savedDrawingPreviewTarget } from './CadResultPreview';
import { ResultActions, ResultIdentity, ResultMetadata } from './ResultCard';
import type { SavedDrawingPresentation } from './savedResultPresentation';

export function cadResultPath(result: Pick<CadResult, 'object_type' | 'object_id' | 'version_id'>): string {
  switch (result.object_type) {
    case 'part_model': return partOutputPath(result.object_id, 'model', result.version_id);
    case 'part_drawing': return partOutputPath(result.object_id, 'drawings', result.version_id);
    case 'assembly': return assemblyRevisionPath(result.object_id, result.version_id);
    case 'drawing_document': return `/drafting/${encodeURIComponent(result.object_id)}?version=${encodeURIComponent(result.version_id)}`;
  }
}
function line(value: Record<string, unknown>): string {
  const text = value.text ?? value.description ?? value.summary ?? value.message ?? value.requirement;
  return typeof text === 'string' ? text : Object.entries(value).map(([key, v]) => `${key}: ${typeof v === 'string' ? v : JSON.stringify(v)}`).join(' · ');
}
export function CadResultCard({ op, result, archived, item, checkSummary, drawing }: {
  op: AiOperation; result: CadResult; archived: boolean; item?: AiResultItem | null; checkSummary?: CheckSummary | null;
  drawing?: SavedDrawingPresentation | null;
}) {
  const { t } = useI18n();
  const { store, keepDockOnLeave } = useAssistantSession();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const assembly = savedAssemblyPreviewTarget(result);
  const typeLabel = assembly ? t('core.result.assembly')
    : tk(t, `tk.assetType.${result.object_type === 'part_drawing' ? 'drawing' : result.object_type}`, result.object_type);
  const name = item?.ref.title || (op.part?.id === result.object_id ? op.part.name : '') || typeLabel;
  const title = `${name} · ${result.version_label}`;
  const openLabel = t(assembly ? 'core.result.openAssembly' : 'core.result.openWorkspace');
  const path = cadResultPath(result);
  const preview = <CadResultPreview key={`${result.object_type}:${result.object_id}:${result.version_id}`}
    artifacts={result.artifacts} label={title} drawing={savedDrawingPreviewTarget(result)} assembly={assembly} />;
  const undo = async () => {
    setBusy(true); setError(null);
    try { await undoCadOperation(op.id, result.undo.expected_head); await store.refresh(op.conversation_id); }
    catch (e) { setError(e); }
    finally { setBusy(false); }
  };
  const checkNeedsAttention = !!checkSummary && (checkSummary.status === 'failed' || checkSummary.status === 'pending'
    || checkSummary.status === 'unknown' || !!checkSummary.problems?.length || !!checkSummary.notes?.uncovered?.length || !!checkSummary.notes?.unknown?.length);
  return <section className="as-result as-cad-result" aria-label={title} data-result-type={result.object_type}
    data-result-object={result.object_id} data-result-version={result.version_id}>
    <div className="as-result-h">
      {item ? <ResultIdentity op={op} item={item} /> : <><span className="as-result-type">{typeLabel}</span>
        <strong className="as-result-name">{name}</strong><span className="as-result-ver mono">{result.version_label}</span></>}
    </div>
    <div className={`as-cad-delivery-body${drawing ? ' as-cad-drawing-delivery' : ''}`}>
      {drawing ? <ProgressDrawingResults drawing={drawing.drawing} />
        : <Link className="as-cad-preview-link" to={path} onClick={keepDockOnLeave} aria-label={`${openLabel} · ${title}`}>{preview}</Link>}
      <div className="as-cad-delivery-main">
        {item && <div className="as-cad-state"><span className="faint small">{t('core.result.saved')}</span><AcceptanceChip /></div>}
        {checkSummary && checkSummary.status !== item?.check_status && <GeomCheckChip status={checkSummary.status} short showNone />}
        {checkSummary?.reason && checkSummary.status !== 'passed' && <p className="as-cad-check-note small">{checkSummary.reason}</p>}
        {item?.availability && item.availability !== 'ready' && <p className="as-cad-check-note small">{tk(t, `as.avail.${item.availability}`, item.availability)}</p>}
        {item ? <ResultActions key={item.ref.key} op={op} item={item} archived={archived} viewPath={path} viewLabel={openLabel} viewPrimary />
          : <div className="as-result-actions"><Link className="btn sm primary" to={path} onClick={keepDockOnLeave}><Eye />{openLabel}</Link></div>}
        {!!result.artifacts.length && <details className="as-cad-files"><summary>{t('core.result.artifacts')} · {result.artifacts.length}</summary>
          <div className="as-result-actions" aria-label={t('core.result.artifacts')}>{result.artifacts.map((file) => <button key={`${file.kind}:${file.sha256}`} type="button" className="btn sm" onClick={() => void downloadBlob(file.sha256, file.filename ?? file.kind).catch(setError)}><Download />{file.filename ?? file.kind}</button>)}</div>
        </details>}
        {result.undo.restored ? <Link className="linkish" to={cadResultPath({ ...result, version_id: result.undo.restored.version_id })} onClick={keepDockOnLeave}>{t('core.result.undone', { version: result.undo.restored.version_label })}</Link>
          : result.undo.eligible && <button type="button" className="btn sm ghost" disabled={busy || archived} onClick={() => void undo()}><RotateCcw />{t('core.result.undo')}</button>}
      </div>
    </div>
    <div className="as-cad-details">
      {drawing && <details open={drawing.brief.annotation_status === 'failed' || drawing.brief.dwg_status === 'failed' || drawing.brief.dwg_status === 'unavailable'}>
        <summary>{t('cad.dw.package')}</summary>
        <DrawingPackageCard brief={drawing.brief} partId={result.object_id} partNumber={op.part?.number} showAcceptance={false} />
      </details>}
      {(['requirements', 'changes'] as const).map((key) => result[key].length > 0 && <details key={key}><summary>{t(`core.result.${key}`)} · {result[key].length}</summary><ul>{result[key].map((entry, i) => <li key={i}>{line(entry)}</li>)}</ul></details>)}
      {(checkSummary !== undefined || Object.keys(result.checks).length > 0) && <details open={checkNeedsAttention}><summary>{t('core.result.checkDetails')}</summary>
        {checkSummary !== undefined && <CheckSummaryView summary={checkSummary} acceptance={false} />}
        {Object.keys(result.checks).length > 0 && <details className="as-cad-raw-checks"><summary>{t('core.result.checks')}</summary><dl>{Object.entries(result.checks).map(([name, value]) => <div key={name}><dt>{name}</dt><dd>{typeof value === 'string' ? value : JSON.stringify(value)}</dd></div>)}</dl></details>}
      </details>}
      <details><summary>{t('core.result.details')}</summary>
        {item && <ResultMetadata op={op} item={item} />}
        <p className="faint small mono">{result.base_version_id ? `${result.base_version_id} → ${result.version_id}` : result.version_id}</p>
        {!result.undo.restored && !result.undo.eligible && <p className="faint small">{t('core.result.undoUnavailable', { reason: tk(t, `core.undo.${result.undo.reason}`, result.undo.reason ?? '') })}</p>}
      </details>
    </div>
    <AsError error={error} />
  </section>;
}

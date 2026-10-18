// Drawing package building blocks: whole-package download (package.zip with
// auth), per-page file downloads (every format present), the package checks
// panel (annotation, DWG, coverage) and the compact package card used by the
// assistant result and the run page. The checks belong to the whole package;
// they are never inferred from the page on screen.

import { ClipboardList, Download, ExternalLink, FileText, Loader2, Package } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { downloadBlob, downloadPackageZip } from '../../api/blobs';
import { getBlob, getDrawing } from '../../api/endpoints';
import type { Drawing, DrawingBrief, DrawingPage } from '../../api/types';
import { partOutputPath } from '../../app/versionLinks';
import { Chip } from '../../components/ui/Badges';
import { ErrorText } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { useI18n } from '../../i18n/I18nProvider';
import { annotationState, coverageOf, drawingPages, dwgInfo, extraPageTitle, formatLabel, isLegacyDrawing, packageFileName, pageFileName, pageFiles } from './status';
import { AcceptanceChip, AnnotationChip, dyn, DwgChip, DwgReason } from './Verdicts';
import { ArtifactVerificationPanel } from '../turbo/ArtifactVerification';
import './cad.css';

/** Where "View drawing" goes: the part's Drawings tab with that exact drawing selected (S6 version link). */
export function drawingPath(partId: string, drawingId: string, page?: number): string {
  return partOutputPath(partId, 'drawings', drawingId, { page: page || null });
}

export function PackageDownloadButton({ drawingId, fileName, label, className = 'btn sm' }: { drawingId: string; fileName: string; label?: string; className?: string }) {
  const { t } = useI18n();
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  return (
    <button
      type="button"
      className={className}
      disabled={busy}
      title={t('cad.dw.packageDlHint')}
      onClick={async () => {
        setBusy(true);
        try {
          await downloadPackageZip(drawingId, { fallbackName: fileName });
        } catch (e) {
          toast(
            <span>
              {t('error.downloadFailed')} <ErrorText error={e} />
            </span>,
            { tone: 'err' },
          );
        } finally {
          setBusy(false);
        }
      }}
    >
      {busy ? <Loader2 className="spin-icon" aria-hidden /> : <Package aria-hidden />} {busy ? t('cad.dw.packageBusy') : (label ?? t('cad.dw.packageDl'))}
    </button>
  );
}

/** One button per file of the page (SVG / DXF / DWG with its release). */
export function PageFileButtons({ page, partNumber, revNo }: { page: DrawingPage; partNumber: string; revNo: number | null | undefined }) {
  const { t } = useI18n();
  const toast = useToast();
  const [busy, setBusy] = useState<string | null>(null);
  const files = pageFiles(page);
  return (
    <span className="cad-files" role="group" aria-label={t('cad.dw.downloadPage', { index: page.index })}>
      {files.map((f) => (
        <button
          key={`${f.format}:${f.sha256}`}
          type="button"
          className="btn sm"
          disabled={busy === f.sha256}
          aria-label={t('cad.dw.downloadFile', { format: formatLabel(f), index: page.index })}
          title={`${f.path} · ${f.sha256.slice(0, 12)}`}
          onClick={async () => {
            setBusy(f.sha256);
            try {
              await downloadBlob(f.sha256, pageFileName(partNumber, revNo, page, f));
            } catch (e) {
              toast(
                <span>
                  {t('error.downloadFailed')} <ErrorText error={e} />
                </span>,
                { tone: 'err' },
              );
            } finally {
              setBusy(null);
            }
          }}
        >
          <Download aria-hidden /> {formatLabel(f)}
        </button>
      ))}
    </span>
  );
}

interface ReportItem {
  id?: string;
  check?: string;
  page?: number | null;
  status?: string;
  message?: string;
  expected?: unknown;
  measured?: unknown;
}

const show = (v: unknown) => (v === null || v === undefined ? '' : typeof v === 'string' ? v : JSON.stringify(v));

/** Annotation report items that are not passed (loaded on request from the report blob). */
function AnnotationItems({ sha }: { sha: string }) {
  const { t } = useI18n();
  const [items, setItems] = useState<ReportItem[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    const ctrl = new AbortController();
    getBlob(sha, { signal: ctrl.signal })
      .then((b) => b.text())
      .then((txt) => {
        const data = JSON.parse(txt) as { items?: ReportItem[] };
        setItems((data.items ?? []).filter((i) => i.status && i.status !== 'passed'));
      })
      .catch((e) => {
        if (!ctrl.signal.aborted) setError(e);
      });
    return () => ctrl.abort();
  }, [sha]);
  if (error) return <ErrorText error={error} />;
  if (!items) return <span className="faint small">{t('cad.dw.reportLoading')}</span>;
  if (!items.length) return <span className="faint small">{t('cad.dw.reportEmpty')}</span>;
  return (
    <ul className="cad-list small">
      {items.slice(0, 30).map((it, i) => (
        <li key={i}>
          <Chip tone={it.status === 'failed' ? 'err' : 'warn'} className="sm">
            {dyn(t, `cad.req.item.${it.status}`, String(it.status))}
          </Chip>{' '}
          <span className="mono">{t('cad.dw.reportItem', { page: it.page ?? '?', check: it.check ?? '' })}</span> {it.message}
          {it.expected !== undefined && it.expected !== null ? (
            <span className="faint">
              {' '}
              · {show(it.expected)} → {show(it.measured)}
            </span>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

/** Package checks: annotation (with failed items on request), DWG output, manufacturing coverage. */
export function PackageChecksPanel({ drawing }: { drawing: Drawing }) {
  const { t } = useI18n();
  const [showItems, setShowItems] = useState(false);
  const legacy = isLegacyDrawing(drawing);
  const ann = annotationState(drawing);
  const annChecks = drawing.checks?.annotation ?? null;
  const dwg = dwgInfo(drawing);
  const cov = coverageOf(drawing);
  const covEmpty = !cov.covered.length && !cov.uncovered.length && !cov.unknown.length;
  return (
    <section className="cad-block cad-pkg-checks" aria-label={t('cad.dw.checks')}>
      <div className="cad-block-h">
        <ClipboardList aria-hidden /> {t('cad.dw.checks')}
      </div>
      {legacy ? (
        <p className="faint small">{t('cad.dw.legacy')}</p>
      ) : (
        <>
          <div className="cad-chips">
            <AnnotationChip state={ann} failed={annChecks?.failed} />
            <DwgChip info={dwg} />
            <AcceptanceChip />
          </div>
          <DwgReason info={dwg} />
          <ArtifactVerificationPanel key={drawing.id} kind="drawing" id={drawing.id} verification={drawing.verification} canCheck={!!drawing.manifest} hasDwg={dwg.state === 'unverified' || dwg.state === 'passed'} />
          {annChecks && (annChecks.failed > 0 || annChecks.warnings > 0) && (
            <p className="small">
              {[annChecks.failed ? t('cad.ann.failedN', { n: annChecks.failed }) : null, annChecks.warnings ? t('cad.ann.warningsN', { n: annChecks.warnings }) : null].filter(Boolean).join(' · ')}
            </p>
          )}
          {annChecks?.report_sha256 && (
            <div className="small">
              {showItems ? (
                <AnnotationItems sha={annChecks.report_sha256} />
              ) : (
                <button type="button" className="linkish small" onClick={() => setShowItems(true)}>
                  {t('cad.dw.reportLoad')}
                </button>
              )}
            </div>
          )}
          <div className="small">
            <div className="cad-sub">{t('cad.dw.coverage')}</div>
            {covEmpty ? (
              <p className="faint">{drawing.manifest?.source?.requirements_sha256 ? t('cad.dw.coverageEmpty') : t('cad.dw.coverageNone')}</p>
            ) : (
              <ul className="cad-list">
                {cov.covered.map((x, i) => (
                  <li key={`c${i}`}>
                    <Chip tone="ok" className="sm">
                      {t('cad.sum.covered')}
                    </Chip>{' '}
                    <span className="mono">{x.requirement_id}</span> {x.text}
                    {x.where ? <span className="faint"> · {t('cad.dw.coverageWhere', { where: x.where })}</span> : null}
                  </li>
                ))}
                {cov.uncovered.map((x, i) => (
                  <li key={`u${i}`}>
                    <Chip tone="warn" className="sm">
                      {t('cad.sum.uncovered')}
                    </Chip>{' '}
                    <span className="mono">{x.requirement_id}</span> {x.text}
                    {x.reason ? <span className="faint"> · {x.reason}</span> : null}
                  </li>
                ))}
                {cov.unknown.map((x, i) => (
                  <li key={`k${i}`}>
                    <Chip tone="pending" className="sm">
                      {t('cad.sum.unknown')}
                    </Chip>{' '}
                    <span className="mono">{x.requirement_id}</span> {x.text}
                    {x.reason ? <span className="faint"> · {x.reason}</span> : null}
                  </li>
                ))}
              </ul>
            )}
          </div>
          <p className="faint small">{t('cad.dw.wholeNote')}</p>
        </>
      )}
    </section>
  );
}

/**
 * Compact package card (assistant result, run artifacts): pages, annotation check, DWG output (with the
 * reason), "View drawing" and "Download package". Details come from GET /drawings/{id}; the brief from the
 * run is shown while it loads or when it cannot be loaded.
 */
export function DrawingPackageCard({
  brief,
  partId,
  partNumber,
  showAcceptance = true,
  showPages = false,
}: {
  brief: DrawingBrief;
  partId: string | null | undefined;
  partNumber?: string | null;
  showAcceptance?: boolean;
  /** List every page with its per-format downloads (run page). */
  showPages?: boolean;
}) {
  const { t } = useI18n();
  const [drawing, setDrawing] = useState<Drawing | null>(null);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    const ctrl = new AbortController();
    setDrawing(null);
    setError(null);
    getDrawing(brief.drawing_id, { signal: ctrl.signal })
      .then(setDrawing)
      .catch((e) => {
        if (!ctrl.signal.aborted) setError(e);
      });
    return () => ctrl.abort();
  }, [brief.drawing_id]);
  const pages = drawing ? drawingPages(drawing) : [];
  const count = drawing?.page_count ?? (pages.length || brief.pages);
  const ann = annotationState(drawing, brief);
  const dwg = dwgInfo(drawing, brief);
  const pid = partId ?? drawing?.part_id ?? null;
  const fileName = packageFileName(partNumber ?? '', drawing?.rev_no, brief.drawing_id);
  return (
    <section className="cad-block" aria-label={t('cad.as.drawings')}>
      <div className="cad-block-h">
        <FileText aria-hidden /> {t('cad.as.drawings')}
        <span className="faint small">
          {drawing?.rev_no ? `R${drawing.rev_no} · ` : ''}
          {t('cad.dw.pagesN', { n: count })}
          {pages.length > 0 ? ` (${pages.map((p) => dyn(t, `cad.dw.role.${p.role}`, p.role)).join(' · ')})` : ''}
        </span>
      </div>
      <div className="cad-chips">
        <AnnotationChip state={ann} failed={drawing?.checks?.annotation?.failed} />
        <DwgChip info={dwg} />
        {showAcceptance && <AcceptanceChip />}
      </div>
      <DwgReason info={dwg} />
      {error ? <p className="faint small">{t('cad.as.drawingLoadFailed')}</p> : null}
      {showPages && pages.length > 0 && (
        <ul className="cad-list small">
          {pages.map((p) => (
            <li key={p.index}>
              <span className="strong">{t('cad.dw.pageLabel', { index: p.index, role: dyn(t, `cad.dw.role.${p.role}`, p.role) })}</span>
              {extraPageTitle(p, dyn(t, `cad.dw.role.${p.role}`, p.role)) ? <span className="faint"> · {p.title}</span> : null}
              <div>
                <PageFileButtons page={p} partNumber={partNumber ?? ''} revNo={drawing?.rev_no} />
              </div>
            </li>
          ))}
        </ul>
      )}
      <div className="cad-actions">
        {pid && (
          <Link className="btn sm" to={drawingPath(pid, brief.drawing_id)}>
            <ExternalLink aria-hidden /> {t('cad.dw.view')}
          </Link>
        )}
        <PackageDownloadButton drawingId={brief.drawing_id} fileName={fileName} />
      </div>
      {brief.manifest_sha256 && <p className="faint small mono">{t('cad.dw.manifest', { sha: brief.manifest_sha256.slice(0, 12) })}</p>}
    </section>
  );
}

// One assembly drawing package: status chips, stale / unverified / partial explanations, page previews (SVG),
// per-page DXF / SVG / DWG downloads (a failed format is shown as missing, never replaced), the whole-package zip,
// the BOM table and balloon list, the manifest, checks and errors. Layout edits open the overlay editor.

import { Download, FileWarning, Loader2, Move, Package } from 'lucide-react';
import { useEffect, useState } from 'react';
import {
  continueAssemblyDrawing,
  DRAWING_FORMATS,
  fetchPackageZip,
  fetchPageFile,
  getAssemblyDrawing,
  type AssemblyDrawingBrief,
  type AssemblyDrawingPackage,
  type DrawingFormat,
  type DrawingPage,
} from '../../api/assemblyDrawings';
import { saveBlob } from '../../api/blobs';
import { ContinueAction } from '../../components/engineering/ContinueAction';
import { CheckStatusChip, PolicyChip, StaleChip, UnverifiedChip } from '../../components/engineering/EngineeringChips';
import { checkFailed, unverifiedBesideCheck } from '../turbo/model';
import { blockedReason } from '../../components/engineering/recovery';
import { Chip } from '../../components/ui/Badges';
import { ErrorState, ErrorText, SkeletonRows } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { useResource } from '../../hooks/useResource';
import { useI18n, type TFunction } from '../../i18n/I18nProvider';
import type { MessageKey } from '../../i18n/en';
import { missingFormats, pageFileName, safeCheck, safeCompleteness } from './model';
import { OverlayDialog } from './OverlayDialog';

const FORMAT_LABEL: Record<DrawingFormat, string> = { dxf: 'DXF', svg: 'SVG', dwg: 'DWG R2000' };
const DOWNLOAD_ERRORS = { page_format_missing: 'asmd.err.pageFormat', package_file_mismatch: 'asmd.err.hash', package_manifest_mismatch: 'asmd.err.hash' } as const;

export function CompletenessChip({ value }: { value: string }) {
  const { t } = useI18n();
  const c = safeCompleteness(value);
  return (
    <Chip tone={c === 'complete' ? 'ok' : 'warn'} icon={c === 'complete' ? undefined : <FileWarning aria-hidden />} title={t(`asmd.completeness.${c}.hint`)}>
      {t(`asmd.completeness.${c}`)}
    </Chip>
  );
}

/** The chips of a package row / header: completeness, drawing checks, policy, unverified, stale, superseded. */
export function PackageChips({ pkg }: { pkg: AssemblyDrawingBrief }) {
  const { t } = useI18n();
  const up = pkg.verification.upstream;
  return (
    <span className="asmd-chips">
      <CompletenessChip value={pkg.completeness} />
      {/* generation supplement B-03: the drawn revision is a partial assembly (a separate fact from the package's own completeness) */}
      {pkg.coverage?.status === 'partial' && <Chip tone="warn">{t('cov.drawings.partial')}</Chip>}
      <CheckStatusChip status={safeCheck(pkg.check_status)} label={t('asmd.checks.short')} />
      <PolicyChip policy={pkg.execution_policy === 'turbo' ? 'turbo' : 'standard'} />
      {unverifiedBesideCheck(pkg.verification, pkg.check_status) && (
        <UnverifiedChip title={pkg.verification.drawing_unverified === false && up?.unverified ? t('asmd.unverified.upstream', { rev: `A${up.rev_no}` }) : undefined} />
      )}
      {pkg.stale && <StaleChip title={t('asmd.stale.hint')} />}
      {pkg.superseded_by && <Chip tone="pending">{t('asmd.superseded')}</Chip>}
    </span>
  );
}

function pageTitle(t: TFunction, p: DrawingPage): string {
  if (p.role === 'assembly') return t('asmd.page.ga', { sheet: p.sheet });
  if (p.role === 'bom') return t('asmd.page.bom', { sheet: p.sheet });
  return t('asmd.page.detail', { item: p.item ?? '?', sheet: p.sheet });
}

function PagePreview({ pkgId, page }: { pkgId: string; page: DrawingPage }) {
  const { t } = useI18n();
  const [url, setUrl] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const hasSvg = page.files.some((f) => f.format === 'svg');
  useEffect(() => {
    if (!hasSvg) return;
    const ctrl = new AbortController();
    let made: string | null = null;
    setUrl(null);
    setError(null);
    fetchPageFile(pkgId, page.key, 'svg', { signal: ctrl.signal })
      .then(({ blob }) => {
        made = URL.createObjectURL(blob);
        setUrl(made);
      })
      .catch((e) => {
        if (!ctrl.signal.aborted) setError(e);
      });
    return () => {
      ctrl.abort();
      if (made) URL.revokeObjectURL(made);
    };
  }, [pkgId, page.key, hasSvg]);
  if (!hasSvg) return <p className="callout slim warn">{t('asmd.preview.missing')}</p>;
  if (error) return <ErrorText error={error} />;
  return (
    <div className="asmd-preview" aria-busy={!url}>
      {url ? <img src={url} alt={t('asmd.preview.alt', { page: page.key })} /> : <Loader2 className="spin-icon" aria-label={t('common.loading')} />}
    </div>
  );
}

function PageDownloads({ pkgId, page, stem }: { pkgId: string; page: DrawingPage; stem: string }) {
  const { t } = useI18n();
  const toast = useToast();
  const [busy, setBusy] = useState<DrawingFormat | null>(null);
  const missing = missingFormats(page);
  const download = async (format: DrawingFormat) => {
    setBusy(format);
    try {
      const { blob, filename } = await fetchPageFile(pkgId, page.key, format);
      saveBlob(blob, filename ?? pageFileName(stem, page, format));
    } catch (e) {
      toast(
        <span>
          {t('error.downloadFailed')} <ErrorText error={e} overrides={DOWNLOAD_ERRORS} />
        </span>,
        { tone: 'err' },
      );
    } finally {
      setBusy(null);
    }
  };
  return (
    <div className="asmd-files" role="group" aria-label={t('asmd.files.page', { page: page.key })}>
      {DRAWING_FORMATS.map((f) => {
        const file = page.files.find((x) => x.format === f);
        if (!file)
          return (
            <Chip key={f} tone="err" icon={<FileWarning aria-hidden />} title={t('asmd.files.missingHint')}>
              {t('asmd.files.missing', { format: FORMAT_LABEL[f] })}
            </Chip>
          );
        return (
          <button key={f} type="button" className="btn sm" disabled={busy !== null} onClick={() => void download(f)} title={`${file.path} · ${file.sha256.slice(0, 12)}`}>
            {busy === f ? <Loader2 className="spin-icon" aria-hidden /> : <Download aria-hidden />} {FORMAT_LABEL[f]}
          </button>
        );
      })}
      {missing.length > 0 && page.errors.length > 0 && (
        <span className="faint small">
          {page.errors.map((e) => `${e.format ? `${e.format.toUpperCase()}: ` : ''}${e.code}`).join(' · ')}
        </span>
      )}
    </div>
  );
}

function BomAndBalloons({ pkg }: { pkg: AssemblyDrawingPackage }) {
  const { t } = useI18n();
  const rows = pkg.pages.flatMap((p) => p.bom_rows ?? []);
  const ga = pkg.pages.find((p) => p.role === 'assembly');
  const balloons = [...(ga?.balloons ?? [])].sort((a, b) => a.item.localeCompare(b.item, undefined, { numeric: true }));
  const nameOf = new Map(rows.map((r) => [r.item, r.name]));
  if (!rows.length && !balloons.length) return null;
  return (
    <div className="asmd-two">
      <section className="panel">
        <div className="panel-h">
          <strong>{t('asmd.bom.title')}</strong>
          <span className="faint small">{t(`asmd.bom.view.${pkg.config.bom_view === 'hierarchical' ? 'hierarchical' : 'flattened'}`)}</span>
        </div>
        <div className="table-wrap">
          <table className="t dense">
            <thead>
              <tr>
                <th>{t('asmd.bom.item')}</th>
                <th>{t('asmd.bom.name')}</th>
                <th>{t('asmd.bom.number')}</th>
                <th className="r">{t('asmd.bom.qty')}</th>
                <th>{t('asmd.bom.material')}</th>
                <th>{t('asmd.bom.procurement')}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={`${r.item}-${r.def_key}`}>
                  <td className="mono">{r.item}</td>
                  <td>{r.name}</td>
                  <td className="mono small">{r.number ?? '—'}</td>
                  <td className="r mono">{r.shown_quantity}</td>
                  <td className="small">{r.material ?? <span className="faint">{t('asmd.unknown')}</span>}</td>
                  <td className="small">{t(`asmd.proc.${['make', 'buy', 'standard'].includes(r.procurement) ? r.procurement : 'unknown'}` as MessageKey)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
      <section className="panel">
        <div className="panel-h">
          <strong>{t('asmd.balloons.title')}</strong>
          <span className="faint small">{t('asmd.balloons.count', { n: balloons.length })}</span>
        </div>
        <ul className="panel-b asmd-balloons small">
          {balloons.map((b) => (
            <li key={b.id}>
              <span className="asmd-balloon mono" aria-hidden>
                {b.item}
              </span>{' '}
              {nameOf.get(b.item) ?? b.def_key} <span className="faint">· ×{b.quantity} · {t('asmd.balloons.view', { view: b.view })}</span>
              <div className="faint mono asmd-path">{b.occurrence.join(' / ')}</div>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}

function ManifestPanel({ pkg }: { pkg: AssemblyDrawingPackage }) {
  const { t, fmt } = useI18n();
  const nameByDef = new Map(pkg.pages.flatMap((p) => p.bom_rows ?? []).map((r) => [r.def_key, r.name]));
  const gen = pkg.generation as { seconds?: number; isolated?: boolean; hard_limit_s?: number | null; dwg_runtime?: { libredwg_version?: string | null; independent_reader?: string | null; available?: boolean } };
  return (
    <section className="panel asmd-manifest">
      <div className="panel-h">
        <strong>{t('asmd.manifest.title')}</strong>
        <span className="faint small mono" title={pkg.manifest_sha256}>
          {t('asmd.manifest.sha', { sha: pkg.manifest_sha256.slice(0, 12) })}
        </span>
      </div>
      <div className="panel-b stack-sm small">
        <div className="asmd-kv">
          <span>{t('asmd.manifest.source')}</span>
          <span>
            {pkg.rev_label} · {t('asmd.manifest.bom', { sha: (pkg.bom_sha256 ?? '—').slice(0, 12) })}
          </span>
          <span>{t('asmd.manifest.created')}</span>
          <span>{fmt.dateTime(pkg.created_at)}</span>
          {pkg.overlay && (
            <>
              <span>{t('asmd.manifest.overlay')}</span>
              <span>{t('asmd.manifest.overlayOf', { version: pkg.overlay.base_version, n: pkg.overlay.edits.length, pages: pkg.overlay.pages.join(', ') })}</span>
            </>
          )}
          <span>{t('asmd.manifest.generation')}</span>
          <span>
            {t('asmd.manifest.seconds', { s: gen.seconds ?? '—' })}
            {gen.dwg_runtime ? ` · LibreDWG ${gen.dwg_runtime.libredwg_version ?? '—'}${gen.dwg_runtime.independent_reader ? ` + ${gen.dwg_runtime.independent_reader}` : ''}` : ''}
          </span>
        </div>
        <div>
          <div className="asmd-sub">{t('asmd.checks.title')}</div>
          <ul className="asmd-plain">
            {Object.entries(pkg.checks).map(([name, c]) => (
              <li key={name}>
                <span className="mono">{name}</span> <CheckStatusChip status={safeCheck(c.status)} />
                {c.failed ? <span className="faint"> · {t('asmd.checks.failed', { n: c.failed })}</span> : null}
              </li>
            ))}
          </ul>
          <p className="faint">{t('asmd.checks.dwgReader')}</p>
        </div>
        <div>
          <div className="asmd-sub">{t('asmd.docs.title')}</div>
          <ul className="asmd-plain">
            {pkg.documents.map((d) => (
              <li key={d.key}>
                <span className="mono">{d.key}</span> · {t(d.role === 'assembly' ? 'asmd.docs.ga' : 'asmd.docs.detail', { item: d.item ?? '' })} ·{' '}
                {t('asmd.docs.sheets', { n: d.sheets })}
                {d.error ? (
                  <span className="asmd-err">
                    {' '}
                    · {d.error.code}: {d.error.message}
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
        </div>
        {pkg.errors.length > 0 && (
          <div>
            <div className="asmd-sub">{t('asmd.errors.title')}</div>
            <ul className="asmd-plain">
              {pkg.errors.map((e, i) => (
                <li key={i} className="asmd-err">
                  <span className="mono">{e.page ?? e.document}</span>
                  {e.format ? ` · ${e.format.toUpperCase()}` : ''} · {e.code}
                  {e.message ? <span className="faint"> · {e.message}</span> : null}
                </li>
              ))}
            </ul>
          </div>
        )}
        {pkg.warnings.length > 0 && (
          <div>
            <div className="asmd-sub">{t('asmd.warnings.title')}</div>
            <ul className="asmd-plain">
              {pkg.warnings.map((w, i) => (
                <li key={i}>
                  <span className="mono">{w.code}</span> <span className="faint">· {w.message}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
        {pkg.assumptions.length > 0 && (
          <div>
            <div className="asmd-sub">{t('asmd.assumptions.title')}</div>
            <ul className="asmd-plain">
              {pkg.assumptions.map((a) => (
                <li key={a.id}>
                  <span className="mono">{a.id}</span> {nameByDef.get(a.subject) ?? a.subject} · {String(a.adopted)} <span className="faint">· {a.basis}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
        <details>
          <summary>{t('asmd.manifest.raw')}</summary>
          <pre className="asmd-raw mono">{JSON.stringify(pkg.manifest, null, 2)}</pre>
        </details>
      </div>
    </section>
  );
}

export function PackageView({
  id,
  canDesign,
  stem,
  onOpenRevision,
  onSaved,
  onContinued,
  onOpenPackage,
}: {
  id: string;
  canDesign: boolean;
  /** File name stem, e.g. the assembly number. */
  stem: string;
  onOpenRevision: (rid: string) => void;
  onSaved: (pkg: AssemblyDrawingPackage) => void;
  /** S3: a continuation of this partial package was made. */
  onContinued?: (pkg: AssemblyDrawingPackage) => void;
  onOpenPackage?: (id: string) => void;
}) {
  const { t } = useI18n();
  const toast = useToast();
  const res = useResource((signal) => getAssemblyDrawing(id, { signal }), [id]);
  const pkg = res.data && res.data.id === id ? res.data : undefined;
  const [pageKey, setPageKey] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [zipping, setZipping] = useState(false);
  if (res.error && !pkg) return <ErrorState error={res.error} onRetry={res.reload} />;
  if (!pkg) return <SkeletonRows rows={5} cols={3} />;
  const page = pkg.pages.find((p) => p.key === pageKey) ?? pkg.pages[0];
  const fileStem = `${stem}_${pkg.rev_label}_${pkg.label}`;
  const newer = pkg.stale_reasons.find((r) => r.code === 'newer_assembly_revision');
  const updates = pkg.stale_reasons.find((r) => r.code === 'component_updates');
  const up = pkg.verification.upstream;
  const downloadZip = async () => {
    setZipping(true);
    try {
      const { blob, filename } = await fetchPackageZip(pkg.id);
      saveBlob(blob, filename ?? `${fileStem}_package.zip`);
    } catch (e) {
      toast(
        <span>
          {t('error.downloadFailed')} <ErrorText error={e} overrides={DOWNLOAD_ERRORS} />
        </span>,
        { tone: 'err' },
      );
    } finally {
      setZipping(false);
    }
  };
  return (
    <div className="stack asmd-pkg">
      <header className="asmd-pkg-h">
        <div className="asmd-pkg-title">
          <strong className="mono">{pkg.label}</strong>
          <span className="faint small">
            {pkg.rev_label} · {t('asmd.pages', { n: pkg.page_count })}
          </span>
          <PackageChips pkg={pkg} />
        </div>
        <div className="asmd-actions">
          <button type="button" className="btn sm" onClick={() => void downloadZip()} disabled={zipping}>
            {zipping ? <Loader2 className="spin-icon" aria-hidden /> : <Package aria-hidden />} {t('asmd.zip')}
          </button>
          {canDesign && !pkg.superseded_by && (
            <button type="button" className="btn sm" onClick={() => setEditing(true)}>
              <Move aria-hidden /> {t('asmd.overlay.open')}
            </button>
          )}
        </div>
      </header>
      {pkg.stale && (
        <div className="callout slim warn">
          <div className="grow">
            {newer && t('asmd.stale.newer', { rev: `A${newer.latest_rev_no ?? '?'}` })} {updates && t('asmd.stale.updates', { n: updates.count ?? 0 })}{' '}
            {t('asmd.stale.keep')}
          </div>
          {newer?.latest_revision_id && (
            <button type="button" className="btn sm" onClick={() => onOpenRevision(newer.latest_revision_id!)}>
              {t('asmd.stale.open')}
            </button>
          )}
        </div>
      )}
      {pkg.superseded_by && <p className="callout slim info">{t('asmd.superseded.hint')}</p>}
      {pkg.completeness !== 'complete' && (
        <div className="callout slim warn">
          <div className="grow stack-sm">
            <span>{t('asmd.partial.hint')}</span>
            {pkg.failure?.failed.length ? (
              <span className="small mono">
                {pkg.failure.failed.map((f) => [f.page ?? f.document, f.format?.toUpperCase(), f.code].filter(Boolean).join(' · ')).join('; ')}
              </span>
            ) : null}
            <ContinueAction
              failure={pkg.failure}
              canWrite={canDesign}
              blocked={blockedReason(t, pkg.failure?.not_continuable)}
              onContinue={async () => {
                const next = await continueAssemblyDrawing(pkg.id);
                res.reload();
                onContinued?.(next);
              }}
            />
            {pkg.failure?.continued_by && onOpenPackage ? (
              <button type="button" className="btn sm ghost" onClick={() => onOpenPackage(pkg.failure!.continued_by!)}>
                {t('asmd.continuation.open')}
              </button>
            ) : null}
          </div>
        </div>
      )}
      {pkg.resumed_from_id && (
        <p className="faint small">
          {t('asmd.continuation.of', {
            kept: Object.values(pkg.progress?.pages ?? {}).filter((v) => v === 'reused').length,
            redone: Object.values(pkg.progress?.pages ?? {}).filter((v) => v !== 'reused').length,
          })}{' '}
          {onOpenPackage ? (
            <button type="button" className="linkish small" onClick={() => onOpenPackage(pkg.resumed_from_id!)}>
              {t('asmd.continuation.openSource')}
            </button>
          ) : null}
        </p>
      )}
      {pkg.verification.unverified && (
        <p className="faint small">
          {/* S6: a failed check reads failed even in Turbo (never "skipped") */}
          {checkFailed(pkg.verification, pkg.check_status) ? t('asmd.unverified.checks') : pkg.execution_policy === 'turbo' ? t('asmd.unverified.turbo') : pkg.verification.drawing_unverified ? t('asmd.unverified.checks') : t('asmd.unverified.upstream', { rev: `A${up?.rev_no ?? '?'}` })}
        </p>
      )}
      <nav className="asmd-pagetabs" aria-label={t('asmd.pages.nav')}>
        {pkg.pages.map((p) => (
          <button key={p.key} type="button" className={`asmd-pagetab ${p.key === page?.key ? 'active' : ''}`} aria-pressed={p.key === page?.key} onClick={() => setPageKey(p.key)}>
            <span className="mono">{p.key}</span>
            <span className="small">{pageTitle(t, p)}</span>
            {p.status !== 'complete' && (
              <Chip tone={p.status === 'failed' ? 'err' : 'warn'} className="sm">
                {t(`asmd.page.status.${p.status === 'failed' ? 'failed' : 'partial'}`)}
              </Chip>
            )}
          </button>
        ))}
      </nav>
      {page ? (
        <section className="asmd-page" aria-label={pageTitle(t, page)}>
          <div className="asmd-page-h">
            <strong>{page.title}</strong>
            <PageDownloads pkgId={pkg.id} page={page} stem={fileStem} />
          </div>
          <PagePreview pkgId={pkg.id} page={page} />
          {page.section && (
            <p className="faint small">
              {t('asmd.section.info', { letter: page.section.letter, plane: page.section.plane, offset: page.section.offset, cut: page.section.cut.length, whole: page.section.not_sectioned.length })}
            </p>
          )}
        </section>
      ) : (
        <p className="callout slim warn">{t('asmd.pages.none')}</p>
      )}
      <BomAndBalloons pkg={pkg} />
      <ManifestPanel pkg={pkg} />
      {editing && (
        <OverlayDialog
          pkg={pkg}
          onClose={() => setEditing(false)}
          onSaved={(next) => {
            setEditing(false);
            onSaved(next);
          }}
        />
      )}
    </div>
  );
}

export { FORMAT_LABEL };

import { FocusNotice } from '../todos/FocusNotice';
import { exactVersion } from '../assistant/exactVersion';
import { ApprovalActions } from './common';
// Part workspace — Drawings tab: cloud-generated drawing packages bound to an
// exact revision. Generate (views / scale / projection / section), switch
// between the package pages (role + title), review each page's SVG with zoom
// and pan, download every format of a page (SVG / DXF / DWG with its release)
// or the whole package (package.zip), and read the package checks (annotation,
// DWG output, manufacturing coverage). Drawings from older revisions are
// outdated; drawings from before the package still render as one page.
// Customer drawing files can be handed to the AI assistant: PDF / image →
// "Analyze drawing" (extraction), DXF / DWG → "Rebuild model from drawing".
// ?drawing=<id>&page=<n> selects a package page (links from assistant results).

import { FilePlus2,FileText,Maximize,Minus,Plus,RefreshCw,ScrollText } from 'lucide-react';
import { useEffect,useMemo,useRef,useState,type ReactNode } from 'react';
import { Link,useSearchParams } from 'react-router-dom';
import { listAssetVersions,listProjectAssets } from '../../api/assets';
import { blobUrl,type BlobHandle } from '../../api/blobs';
import { createDrawing,listDrawings,listFiles,listRevisions } from '../../api/endpoints';
import type { Drawing,DrawingConfig,DrawingPage,DrawingSection,DrawingView,Part,Project } from '../../api/types';
import { paths } from '../../app/nav';
import { isHistorical } from '../../app/versionLinks';
import { UnverifiedChip } from '../../components/engineering/EngineeringChips';
import { Chip } from '../../components/ui/Badges';
import { HistoricalBanner } from '../../components/ui/HistoricalBanner';
import { EmptyState,ErrorNotice,ErrorState,SkeletonRows,Spinner } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import type { PartTabProps } from '../../pages/part/registry';
import { useAssistant,useAssistantPageContext } from '../assistant/AssistantProvider';
import { CadDrawingFileAiActions,DrawingAssetReconstructButton,DrawingFileAiActions } from '../assistant/LegacyOperationActions';
import { isDrawingFileKind } from '../assistant/refs';
import { PackageChecksPanel,PackageDownloadButton,PageFileButtons } from '../cad/DrawingPackage';
import { SectionPicker } from '../cad/SectionPicker';
import { annotationState,cadDrawingFormat,drawingPages,dwgInfo,extraPageTitle,packageFileName,sectionMode,selectPage,stateTone } from '../cad/status';
import { dyn } from '../cad/Verdicts';
import { showsUnverified,type VerificationReasons } from '../turbo/model';
import { CheckField,SelectField } from './model/fields';
import './workspace.css';

const ALL_VIEWS: DrawingView['name'][] = ['front', 'top', 'right', 'iso'];
const SCALES: { value: string; label: string }[] = [
  { value: 'auto', label: 'auto' },
  { value: '5', label: '5:1' },
  { value: '2', label: '2:1' },
  { value: '1', label: '1:1' },
  { value: '0.5', label: '1:2' },
  { value: '0.2', label: '1:5' },
  { value: '0.1', label: '1:10' },
];

function scaleLabel(s: DrawingConfig['scale']): string {
  if (s === undefined || s === 'auto') return 'auto';
  return s >= 1 ? `${Math.round(s * 100) / 100}:1` : `1:${Math.round((1 / s) * 100) / 100}`;
}

export default function DrawingsTab({ part, project, caps }: PartTabProps) {
  const { t, fmt } = useI18n();
  const toast = useToast();
  const canDesign = caps.design && !part.archived_at;
  const drawingsRes = useResource((signal) => listDrawings(part.id, { signal }), [part.id]);
  const revsRes = useResource((signal) => listRevisions(part.id, { signal }), [part.id]);
  const revisions = revsRes.data ?? [];
  const latest = revisions[0] ?? null;
  const drawable = revisions.filter((r) => !r.mesh_only && r.status === 'ready');
  // ?drawing= / ?page= select a package page (links from the assistant); clicks keep them in the URL.
  const [search, setSearch] = useSearchParams();
  const selId = search.get('drawing');
  const pageParam = Number(search.get('page')) || null;
  const selectDrawing = (id: string, page?: number | null) => {
    const next = new URLSearchParams(search);
    next.set('drawing', id);
    if (page) next.set('page', String(page));
    else next.delete('page');
    setSearch(next, { replace: true });
  };
  const drawings = drawingsRes.data ?? [];
  const sel = exactVersion(drawings, selId);

  // Arriving with ?drawing= (assistant "View drawing") on a one-column layout: bring the viewer into view once.
  const mainRef = useRef<HTMLElement>(null);
  const arrivedWith = useRef(selId);
  const loaded = !!drawingsRes.data && !!revsRes.data;
  useEffect(() => {
    if (!loaded || !arrivedWith.current) return;
    arrivedWith.current = null;
    const narrow = typeof window.matchMedia === 'function' && window.matchMedia('(max-width: 1100px)').matches;
    if (narrow) mainRef.current?.scrollIntoView({ block: 'start' });
  }, [loaded]);

  // A drawing operation of the assistant finished for this part: show the new package.
  const { onOperationEvent } = useAssistant();
  const reloadDrawingsRef = useRef(drawingsRes.reload);
  reloadDrawingsRef.current = drawingsRes.reload;
  useEffect(
    () =>
      onOperationEvent((e) => {
        const op = e.operation;
        const result = op.result?.cad_result;
        if (result?.object_type === 'part_drawing' && result.object_id === part.id && e.projectId === project.id) reloadDrawingsRef.current();
        else if (e.type === 'succeeded' && op.part?.id === part.id && (op.kind === 'drawing_generate' || op.kind === 'drawing_reconstruct')) reloadDrawingsRef.current();
      }),
    [onOperationEvent, part.id, project.id],
  );

  // Asset version of each drawing (for "Rebuild model from drawing" on a package): only with design permission.
  const assetsRes = useResource(
    async (signal) => {
      const assets = await listProjectAssets(project.id, { type: 'drawing', part_id: part.id }, { signal });
      const versions = await Promise.all(assets.map((a) => listAssetVersions(a.id, { signal })));
      const map = new Map<string, string>();
      for (const v of versions.flat()) if (v.target?.kind === 'drawing') map.set(v.target.id, v.id);
      return map;
    },
    [project.id, part.id, drawings.length],
    { enabled: drawings.length > 0 },
  );

  const shownAsset = sel ? assetsRes.data?.get(sel.id) : null;
  useAssistantPageContext({ projectId: project.id, partId: part.id, label: `${part.number} · ${sel?.id.slice(0, 8) ?? t('part.tab.drawings')}`,
    object: { type: 'part_drawing', id: part.id, version: sel?.id ?? null },
    target: shownAsset ? { kind: 'asset_version', object_id: shownAsset, project_id: project.id } : null });

  // generate form
  const [revId, setRevId] = useState('');
  const [views, setViews] = useState<DrawingView['name'][]>(ALL_VIEWS);
  const [scale, setScale] = useState('auto');
  const [projection, setProjection] = useState<'third' | 'first'>('third');
  const [hidden, setHidden] = useState(true);
  const [dims, setDims] = useState(true);
  const [section, setSection] = useState<DrawingSection>('auto');
  const sectionInvalid = sectionMode(section) === 'plane' && !(typeof section === 'object' && Number.isFinite(section.offset));
  const [busy, setBusy] = useState(false);
  const [genError, setGenError] = useState<unknown>(null);
  const targetRev = drawable.find((r) => r.id === revId) ?? drawable[0] ?? null;

  const generate = async () => {
    if (!targetRev) return;
    setBusy(true);
    setGenError(null);
    try {
      const d = await createDrawing(targetRev.id, { views, scale: scale === 'auto' ? 'auto' : Number(scale), projection, hidden_lines: hidden, dimensions: dims, section });
      toast(t('ws.dw.generated', { rev: `R${targetRev.rev_no}` }));
      drawingsRes.reload();
      selectDrawing(d.id);
    } catch (e) {
      setGenError(e);
    } finally {
      setBusy(false);
    }
  };

  if ((drawingsRes.error && !drawingsRes.data) || (revsRes.error && !revsRes.data))
    return (
      <ErrorState
        error={drawingsRes.error ?? revsRes.error}
        onRetry={() => {
          drawingsRes.reload();
          revsRes.reload();
        }}
      />
    );
  if (!drawingsRes.data || !revsRes.data) return <SkeletonRows rows={4} />;

  const outdated = (d: Drawing) => !!latest && !!d.model_revision_id && d.model_revision_id !== latest.id;
  const missingAsked = !!selId && !drawings.some((d) => d.id === selId);
  const drawingLabel = (d: Drawing) => `${d.rev_no ? `R${d.rev_no}` : t(`ws.dw.kind.${d.kind}`)} · ${fmt.dateTime(d.created_at)}`;

  return (
    <div className="ws-split">
      <aside className="stack-sm ws-split-side">
        {canDesign && (
          <section className="panel">
            <div className="panel-h">
              <FilePlus2 aria-hidden className="measure-ico" />
              <h3>{t('ws.dw.generate')}</h3>
            </div>
            <div className="panel-b stack-sm">
              {!drawable.length ? (
                <p className="faint small">{revisions.length ? t('ws.dw.onlyMesh') : t('ws.dw.noRevision')}</p>
              ) : (
                <>
                  <SelectField<string>
                    label={t('ws.dw.revision')}
                    value={targetRev?.id ?? ''}
                    options={drawable.map((r) => ({ value: r.id, label: `${r.label}${r.id === latest?.id ? ` · ${t('ws.rev.latest')}` : ''}` }))}
                    onChange={setRevId}
                  />
                  <div className="ws-f">
                    <span className="ws-f-label">{t('ws.dw.views')}</span>
                    <div className="row">
                      {ALL_VIEWS.map((v) => (
                        <CheckField key={v} label={t(`ws.vw.view.${v}`)} checked={views.includes(v)} onChange={(on) => setViews((xs) => (on ? ALL_VIEWS.filter((x) => xs.includes(x) || x === v) : xs.filter((x) => x !== v)))} />
                      ))}
                    </div>
                  </div>
                  <div className="ws-row2">
                    <SelectField<string> label={t('ws.dw.scale')} value={scale} options={SCALES.map((s) => ({ value: s.value, label: s.value === 'auto' ? t('ws.dw.scaleAuto') : s.label }))} onChange={setScale} />
                    <SelectField<'third' | 'first'>
                      label={t('ws.dw.projection')}
                      value={projection}
                      options={[
                        { value: 'third', label: t('ws.dw.third') },
                        { value: 'first', label: t('ws.dw.first') },
                      ]}
                      onChange={setProjection}
                    />
                  </div>
                  <div className="row">
                    <CheckField label={t('ws.dw.hidden')} checked={hidden} onChange={setHidden} />
                    <CheckField label={t('ws.dw.dims')} checked={dims} onChange={setDims} />
                  </div>
                  <div className="ws-f">
                    <span className="ws-f-label">{t('cad.dw.section')}</span>
                    <SectionPicker value={section} onChange={setSection} disabled={busy} />
                    <span className="ws-f-hint">{t('cad.dw.section.hint')}</span>
                  </div>
                  <button type="button" className="btn primary" disabled={busy || !views.length || !targetRev || sectionInvalid} onClick={() => void generate()}>
                    {busy ? <Spinner /> : <FilePlus2 />} {busy ? t('ws.dw.generating') : t('ws.dw.generateFor', { rev: targetRev ? `R${targetRev.rev_no}` : '—' })}
                  </button>
                  <ErrorNotice error={genError} />
                </>
              )}
            </div>
          </section>
        )}
        <section className="panel">
          <div className="panel-h">
            <FileText aria-hidden className="measure-ico" />
            <h3>{t('ws.dw.list')}</h3>
            <span className="grow" />
            <button type="button" className="btn sm icon ghost" onClick={drawingsRes.reload} aria-label={t('common.retry')}>
              <RefreshCw />
            </button>
          </div>
          {!drawings.length ? (
            <div className="panel-b">
              <EmptyState compact title={t('ws.dw.none')} body={canDesign ? t('ws.dw.noneBody') : t('ws.dw.noneViewer')} />
            </div>
          ) : (
            <ul className="ws-list" role="listbox" aria-label={t('ws.dw.list')}>
              {drawings.map((d) => (
                <li key={d.id}>
                  <button type="button" role="option" aria-selected={sel?.id === d.id} className={`ws-list-item ${sel?.id === d.id ? 'active' : ''}`} onClick={() => selectDrawing(d.id)}>
                    <span className="row">
                      <span className="mono strong">{d.rev_no ? `R${d.rev_no}` : t(`ws.dw.kind.${d.kind}`)}</span>
                      {outdated(d) ? <Chip tone="stale">{t('ws.dw.outdated')}</Chip> : d.model_revision_id ? <Chip tone="ok">{t('ws.dw.current')}</Chip> : null}
                      <span className="grow" />
                      <span className="faint small">{fmt.relative(d.created_at)}</span>
                    </span>
                    <span className="faint small">
                      {(d.config.views ?? []).map((v) => t(`ws.vw.view.${v}`)).join(' · ')} · {scaleLabel(d.config.scale)} · {d.config.projection === 'first' ? t('ws.dw.first') : t('ws.dw.third')}
                    </span>
                    <ListVerdicts drawing={d} />
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>
        <CustomerDrawings part={part} project={project} analyzeBlocked={part.archived_at ? t('ar.dw.partArchived') : caps.design ? null : t('ar.dw.noDesign')} />
      </aside>
      <section className="ws-split-main stack-sm" ref={mainRef}>
      <FocusNotice kind="assumption" version={sel?.id ?? part.id} />
        {/* S6: the exact drawing a link asked for; a newer package exists → say so (the one-column layout scrolls here). */}
        {missingAsked && <p className="callout err slim">{t('ver.missing')}</p>}
        {sel && isHistorical(sel.id, drawings[0]?.id) && (
          <HistoricalBanner current={drawingLabel(drawings[0]!)} onOpenCurrent={() => selectDrawing(drawings[0]!.id)} />
        )}
        {sel && <ApprovalActions partId={part.id} subjectType="drawing" subjectId={sel.id} stage="design" can={canDesign} onDone={drawingsRes.reload} />}
        {sel ? (
          <DrawingViewer
            drawing={sel}
            partNumber={part.number}
            outdatedBy={outdated(sel) ? latest?.label ?? null : null}
            pageIndex={sel.id === selId ? pageParam : null}
            onPage={(index) => selectDrawing(sel.id, index)}
            reconstruct={
              caps.design ? (
                <DrawingAssetReconstructButton
                  projectId={project.id}
                  assetVersionId={assetsRes.data?.get(sel.id) ?? null}
                  scope={`${part.number} ${sel.rev_no ? `R${sel.rev_no}` : ''} ${t('ws.dw.list')}`.trim()}
                  blocked={part.archived_at ? t('ar.dw.partArchived') : assetsRes.loading && !assetsRes.data ? t('common.loading') : null}
                  compact
                />
              ) : null
            }
          />
        ) : (
          <div className="panel">
            <EmptyState title={t('ws.dw.none')} body={canDesign ? t('ws.dw.noneBody') : t('ws.dw.noneViewer')} icon={<FileText />} />
          </div>
        )}
      </section>
    </div>
  );
}

/** Compact verdicts of a list row: pages, annotation check, DWG output. */
function ListVerdicts({ drawing }: { drawing: Drawing }) {
  const { t } = useI18n();
  const pages = drawingPages(drawing).length;
  const ann = annotationState(drawing);
  const dwg = dwgInfo(drawing);
  if (ann === 'legacy') return <span className="faint small">{t('cad.dw.legacy')}</span>;
  return (
    <span className="cad-chips">
      <span className="faint small">{t('cad.dw.pagesN', { n: drawing.page_count ?? pages })}</span>
      <Chip tone={stateTone(ann)} className="sm">
        {t(`cad.ann.${ann}`)}
      </Chip>
      <Chip tone={stateTone(dwg.state)} className="sm">
        {dwg.state === 'passed' ? t('cad.dwg.passed', { release: dwg.release ?? 'R2000' }) : t(`cad.dwg.${dwg.state}`)}
      </Chip>
      {showsUnverified(drawing.verification as VerificationReasons | undefined) && <UnverifiedChip />}
    </span>
  );
}

function pageRole(t: ReturnType<typeof useI18n>['t'], page: DrawingPage): string {
  return dyn(t, `cad.dw.role.${page.role}`, page.role);
}

function pageTitle(t: ReturnType<typeof useI18n>['t'], page: DrawingPage): string | null {
  return extraPageTitle(page, pageRole(t, page));
}

function DrawingViewer({
  drawing,
  partNumber,
  outdatedBy,
  pageIndex,
  onPage,
  reconstruct,
}: {
  drawing: Drawing;
  partNumber: string;
  outdatedBy: string | null;
  /** Page asked for in the URL (null: the first page). */
  pageIndex: number | null;
  onPage: (index: number) => void;
  /** "Rebuild model from drawing" entry (design permission only). */
  reconstruct: ReactNode;
}) {
  const { t, fmt } = useI18n();
  const pages = useMemo(() => drawingPages(drawing), [drawing]);
  const page = selectPage(pages, pageIndex);
  const svgSha = page?.files.find((f) => f.format === 'svg')?.sha256 ?? null;
  const [handle, setHandle] = useState<BlobHandle | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const drag = useRef<{ x: number; y: number; px: number; py: number } | null>(null);
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setHandle(null);
    setError(null);
    setZoom(1);
    setPan({ x: 0, y: 0 });
    if (!svgSha) return;
    const ctrl = new AbortController();
    let h: BlobHandle | null = null;
    blobUrl(svgSha, { signal: ctrl.signal })
      .then((x) => {
        h = x;
        if (ctrl.signal.aborted) x.revoke();
        else setHandle(x);
      })
      .catch((e) => {
        if (!ctrl.signal.aborted) setError(e);
      });
    return () => {
      ctrl.abort();
      h?.revoke();
    };
  }, [svgSha]);

  // Wheel zoom around the cursor (non-passive listener so the page does not scroll).
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const r = el.getBoundingClientRect();
      const cx = e.clientX - r.left - r.width / 2;
      const cy = e.clientY - r.top - r.height / 2;
      setZoom((z) => {
        const nz = Math.min(12, Math.max(0.25, z * (e.deltaY < 0 ? 1.15 : 1 / 1.15)));
        setPan((p) => ({ x: cx - ((cx - p.x) * nz) / z, y: cy - ((cy - p.y) * nz) / z }));
        return nz;
      });
    };
    el.addEventListener('wheel', onWheel, { passive: false });
    return () => el.removeEventListener('wheel', onWheel);
  }, [handle]);

  const meta = useMemo(() => drawing.views.map((v) => `${t(`ws.vw.view.${v.name}`)} ${scaleLabel(v.scale)}`).join(' · '), [drawing.views, t]);
  const legacy = !drawing.pages?.length;

  return (
    <div className="panel ws-dw">
      <div className="panel-h">
        <h3 className="mono">{drawing.rev_no ? `R${drawing.rev_no}` : '—'}</h3>
        <span className="faint small">{fmt.dateTime(drawing.created_at)}</span>
        <span className="faint small ellip">{meta}</span>
        <span className="grow" />
        <div className="row">
          <button type="button" className="btn sm icon" onClick={() => setZoom((z) => Math.max(0.25, z / 1.25))} aria-label={t('ws.dw.zoomOut')}>
            <Minus />
          </button>
          <span className="mono small ws-zoom">{Math.round(zoom * 100)}%</span>
          <button type="button" className="btn sm icon" onClick={() => setZoom((z) => Math.min(12, z * 1.25))} aria-label={t('ws.dw.zoomIn')}>
            <Plus />
          </button>
          <button
            type="button"
            className="btn sm icon"
            onClick={() => {
              setZoom(1);
              setPan({ x: 0, y: 0 });
            }}
            aria-label={t('ws.dw.fit')}
          >
            <Maximize />
          </button>
        </div>
      </div>

      {pages.length > 1 && (
        <div className="cad-pages" role="group" aria-label={t('cad.dw.pages')}>
          {pages.map((p) => (
            <button key={p.index} type="button" className="cad-page-btn" aria-pressed={p.index === page?.index} onClick={() => onPage(p.index)} title={p.title || pageRole(t, p)}>
              <span className="mono">{t('cad.dw.pageLabel', { index: p.index, role: pageRole(t, p) })}</span>
              {pageTitle(t, p) ? <span className="cad-page-title faint">{pageTitle(t, p)}</span> : null}
            </button>
          ))}
        </div>
      )}

      <div className="cad-pkg-bar">
        {page && (
          <>
            <span className="small strong">
              {pages.length > 1 ? t('cad.dw.page', { index: page.index }) : pageRole(t, page)}
              {pageTitle(t, page) && pages.length <= 1 ? <span className="faint"> · {pageTitle(t, page)}</span> : null}
            </span>
            <PageFileButtons page={page} partNumber={partNumber} revNo={drawing.rev_no} />
          </>
        )}
        <span className="grow" />
        <PackageDownloadButton drawingId={drawing.id} fileName={packageFileName(partNumber, drawing.rev_no, drawing.id)} />
        {reconstruct}
      </div>
      {drawing.manifest_sha256 && <p className="faint small mono ws-dw-note">{t('cad.dw.manifest', { sha: drawing.manifest_sha256.slice(0, 12) })}</p>}

      {outdatedBy && (
        <div className="callout warn slim ws-dw-note">
          <span>{t('ws.dw.outdatedNote', { rev: `R${drawing.rev_no}`, latest: outdatedBy })}</span>
        </div>
      )}
      <div
        className="ws-dw-canvas"
        ref={box}
        onPointerDown={(e) => {
          (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
          drag.current = { x: e.clientX, y: e.clientY, px: pan.x, py: pan.y };
        }}
        onPointerMove={(e) => {
          const d = drag.current;
          if (d) setPan({ x: d.px + e.clientX - d.x, y: d.py + e.clientY - d.y });
        }}
        onPointerUp={() => (drag.current = null)}
        onPointerCancel={() => (drag.current = null)}
        onDoubleClick={() => {
          setZoom(1);
          setPan({ x: 0, y: 0 });
        }}
      >
        {error ? (
          <ErrorNotice error={error} />
        ) : !svgSha ? (
          <p className="faint">{t('ws.dw.noSvg')}</p>
        ) : !handle ? (
          <Spinner label={t('common.loading')} />
        ) : (
          <img
            src={handle.url}
            alt={`${t('ws.dw.alt', { rev: `R${drawing.rev_no ?? '?'}` })}${page && pages.length > 1 ? ` · ${t('cad.dw.page', { index: page.index })}` : ''}`}
            draggable={false}
            style={{ transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})` }}
          />
        )}
      </div>
      {legacy ? <p className="faint small ws-dw-note">{t('cad.dw.legacy')}</p> : <PackageChecksPanel drawing={drawing} />}
    </div>
  );
}

/** Customer drawing files of this part (latest version each), with the assistant entries. */
function CustomerDrawings({ part, project, analyzeBlocked }: { part: Part; project: Project; analyzeBlocked: string | null }) {
  const { t, fmt } = useI18n();
  const reconstructBlocked = part.archived_at ? t('ar.dw.partArchived') : t('cad.dw.reconstructNoDesign');
  const filesRes = useResource((signal) => listFiles(project.id, { part_id: part.id }, { signal }), [project.id, part.id]);
  const files = (filesRes.data ?? []).filter((f) => (isDrawingFileKind(f.kind) || cadDrawingFormat(f.versions[0]?.original_name, f.kind)) && f.versions.length && !f.archived_at);
  return (
    <section className="panel" aria-labelledby="ar-dw-h">
      <div className="panel-h">
        <ScrollText aria-hidden className="measure-ico" />
        <h3 id="ar-dw-h">{t('ar.dw.title')}</h3>
      </div>
      <div className="panel-b stack-sm">
        {filesRes.error && !filesRes.data ? (
          <ErrorNotice error={filesRes.error} onRetry={filesRes.reload} />
        ) : !filesRes.data ? (
          <SkeletonRows rows={2} />
        ) : !files.length ? (
          <p className="faint small">
            {t('ar.dw.none')} <Link to={paths.part(part.id, 'files')}>{t('ar.dw.toFiles')}</Link>
          </p>
        ) : (
          <ul className="ar-files">
            {files.map((f) => {
              const cad = cadDrawingFormat(f.versions[0].original_name, f.kind);
              return (
                <li key={f.id} className="ar-file">
                  <span className="small">
                    <span className="strong ar-file-name">{f.name}</span>{' '}
                    <span className="mono faint">
                      v{f.versions[0].version} · {cad ? t(cad === 'dwg' ? 'cad.dw.kindDwg' : 'cad.dw.kindDxf') : t(f.kind === 'drawing_pdf' ? 'ar.dw.kindPdf' : 'ar.dw.kindImage')} · {fmt.relative(f.versions[0].created_at)}
                    </span>
                  </span>
                  {cad ? (
                    <CadDrawingFileAiActions projectId={project.id} file={f} blocked={analyzeBlocked ? reconstructBlocked : null} />
                  ) : (
                    <DrawingFileAiActions projectId={project.id} file={f} analyzeBlocked={analyzeBlocked} />
                  )}
                </li>
              );
            })}
          </ul>
        )}
        <p className="faint small">{t('ar.dw.note')}</p>
      </div>
    </section>
  );
}

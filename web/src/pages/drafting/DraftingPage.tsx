import { FocusNotice } from '../../features/todos/FocusNotice';
import { useAssistant,useAssistantPageContext } from '../../features/assistant/AssistantProvider';
// /drafting/:documentId — standalone 2D drawing workspace (W6). No 3D model is needed: the document belongs to a
// project and every save is a new immutable version. Edits collect as pending commands (previewed locally) and are
// saved against the head version; a version saved by someone else in between is an explicit conflict (409).

import {
ArrowUpRight,
Blocks,
Circle,
Copy,
Crosshair,
Diameter,
Download,
DraftingCompass,
Grid3x3,
Hand,
History,
Layers,
ListChecks,
Magnet,
MousePointer2,
Move,
Radius,
RotateCw,
Ruler,
RulerDimensionLine,
Save,
ScanText,
Slash,
SlidersHorizontal,
Spline,
Table,
Trash2,
TriangleRight,
Type,
Undo2,
} from 'lucide-react';
import { useCallback,useEffect,useMemo,useRef,useState,type ReactNode } from 'react';
import { useNavigate,useParams,useSearchParams } from 'react-router-dom';
import { isApiError } from '../../api/client';
import {
calibrateUnderlay,
downloadDrawingFile,
getDrawingDocument,
getDrawingVersion,
listDrawingVersions,
restoreDrawingVersion,
saveDrawingVersion,
type DraftCommand,
type DraftContent,
type DrawingDocument,
type Pt,
type VersionBrief,
type VersionFull,
} from '../../api/drafting';
import { getProject } from '../../api/endpoints';
import { paths } from '../../app/nav';
import { projectCapabilities } from '../../auth/permissions';
import { useCrumbs,useShellProject } from '../../components/shell/ShellContext';
import { Dialog } from '../../components/ui/Dialog';
import { ErrorNotice,ErrorState,Spinner } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { VersionChips } from '../../features/drafting/chips';
import { applyLocal } from '../../features/drafting/commands';
import '../../features/drafting/drafting.css';
import { DraftingCanvas,type CanvasRequests,type Tool } from '../../features/drafting/DraftingCanvas';
import { fmtNum } from '../../features/drafting/geometry';
import { ChecksPanel,HistoryPanel,LayersPanel,PropertiesPanel,ProvenancePanel,SheetPanel,type PanelCtx } from '../../features/drafting/Panels';
import { SNAP_KINDS,type Snap,type SnapKind } from '../../features/drafting/snaps';
import { isTypingTarget } from '../../hooks/keyboard';
import { useResource } from '../../hooks/useResource';
import type { MessageKey } from '../../i18n/en';
import { useI18n } from '../../i18n/I18nProvider';

type PanelId = 'props' | 'layers' | 'provenance' | 'sheet' | 'history' | 'checks';

const TOOL_GROUPS: { id: string; tools: { tool: Tool; icon: ReactNode; key: MessageKey }[] }[] = [
  {
    id: 'nav',
    tools: [
      { tool: 'select', icon: <MousePointer2 />, key: 'dr.tool.select' },
      { tool: 'pan', icon: <Hand />, key: 'dr.tool.pan' },
    ],
  },
  {
    id: 'draw',
    tools: [
      { tool: 'line', icon: <Slash />, key: 'dr.tool.line' },
      { tool: 'polyline', icon: <Spline />, key: 'dr.tool.polyline' },
      { tool: 'circle', icon: <Circle />, key: 'dr.tool.circle' },
      { tool: 'arc', icon: <DraftingCompass />, key: 'dr.tool.arc' },
      { tool: 'text', icon: <Type />, key: 'dr.tool.text' },
      { tool: 'leader', icon: <ArrowUpRight />, key: 'dr.tool.leader' },
      { tool: 'hatch', icon: <Grid3x3 />, key: 'dr.tool.hatch' },
      { tool: 'insert', icon: <Blocks />, key: 'dr.tool.insert' },
    ],
  },
  {
    id: 'dim',
    tools: [
      { tool: 'dim_linear', icon: <RulerDimensionLine />, key: 'dr.tool.dim_linear' },
      { tool: 'dim_aligned', icon: <Ruler />, key: 'dr.tool.dim_aligned' },
      { tool: 'dim_angular', icon: <TriangleRight />, key: 'dr.tool.dim_angular' },
      { tool: 'dim_diameter', icon: <Diameter />, key: 'dr.tool.dim_diameter' },
      { tool: 'dim_radius', icon: <Radius />, key: 'dr.tool.dim_radius' },
    ],
  },
  {
    id: 'edit',
    tools: [
      { tool: 'move', icon: <Move />, key: 'dr.tool.move' },
      { tool: 'copy', icon: <Copy />, key: 'dr.tool.copy' },
      { tool: 'rotate', icon: <RotateCw />, key: 'dr.tool.rotate' },
    ],
  },
];

const PANELS: { id: PanelId; icon: ReactNode; key: MessageKey }[] = [
  { id: 'props', icon: <SlidersHorizontal />, key: 'dr.panel.props' },
  { id: 'layers', icon: <Layers />, key: 'dr.panel.layers' },
  { id: 'provenance', icon: <ScanText />, key: 'dr.panel.provenance' },
  { id: 'sheet', icon: <Table />, key: 'dr.panel.sheet' },
  { id: 'history', icon: <History />, key: 'dr.panel.history' },
  { id: 'checks', icon: <ListChecks />, key: 'dr.panel.checks' },
];

interface Ask {
  kind: 'text' | 'leader' | 'rotate' | 'calibrate';
  resolve: (v: string | number | null) => void;
}

function AskDialog({ ask, onDone }: { ask: Ask; onDone: () => void }) {
  const { t } = useI18n();
  const numeric = ask.kind === 'rotate' || ask.kind === 'calibrate';
  const [value, setValue] = useState(ask.kind === 'rotate' ? '90' : '');
  const close = (v: string | number | null) => {
    ask.resolve(v);
    onDone();
  };
  const submit = () => {
    if (numeric) {
      const n = parseFloat(value);
      close(Number.isFinite(n) ? n : null);
    } else close(value.trim() ? value : null);
  };
  return (
    <Dialog
      title={t(`dr.ask.${ask.kind}`)}
      size="sm"
      onClose={() => close(null)}
      onSubmit={submit}
      footer={
        <>
          <button type="button" className="btn" onClick={() => close(null)}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary">
            {t('dr.ask.ok')}
          </button>
        </>
      }
    >
      {numeric ? (
        <label className="field">
          <span>{t(ask.kind === 'rotate' ? 'dr.ask.rotate.label' : 'dr.ask.calibrate.label')}</span>
          <input className="input" type="number" step="any" inputMode="decimal" value={value} onChange={(e) => setValue(e.target.value)} data-autofocus />
        </label>
      ) : (
        <label className="field">
          <span>{t('dr.ask.text.label')}</span>
          <textarea className="input" rows={3} value={value} onChange={(e) => setValue(e.target.value)} data-autofocus />
        </label>
      )}
    </Dialog>
  );
}

export default function DraftingPage() {
  const { documentId } = useParams();
  return <DraftingWorkspace key={documentId} />;
}

function DraftingWorkspace() {
  const { documentId } = useParams();
  const { t } = useI18n();
  const toast = useToast();
  const navigate = useNavigate();
  const res = useResource((signal) => getDrawingDocument(documentId!, { signal }), [documentId], { enabled: !!documentId });
  const doc = res.data?.id === documentId ? res.data : undefined;
  const projectRes = useResource((signal) => getProject(doc!.project_id, { signal }), [doc?.project_id], { enabled: !!doc?.project_id });
  const project = projectRes.data?.id === doc?.project_id ? projectRes.data : undefined;
  const caps = projectCapabilities(project);
  useShellProject(project);
  useCrumbs([
    { label: t('area.resources'), to: paths.materials(doc?.project_id, { type: 'drawing', subtype: 'drawing_document' }) },
    { label: doc ? doc.number : '…', mono: true },
  ]);

  const [search, setSearch] = useSearchParams();
  const requestedVersion = search.get('version');
  const head = doc?.head ?? null;
  const [viewing, setViewing] = useState<VersionFull | null>(null);
  const shown: VersionFull | null = requestedVersion ? [viewing, head].find((version) => version?.id === requestedVersion) ?? null : head;
  const [pending, setPending] = useState<DraftCommand[]>([]);
  const [selection, setSelection] = useState<string[]>([]);
  const [tool, setTool] = useState<Tool>('select');
  const [snapKinds, setSnapKinds] = useState<Set<SnapKind>>(() => new Set(SNAP_KINDS));
  const [activeLayer, setActiveLayer] = useState('0');
  const [activeBlock, setActiveBlock] = useState<string | null>(null);
  const [panel, setPanel] = useState<PanelId>(() => search.has('assumption') ? 'checks' : 'props');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [conflict, setConflict] = useState<{ head_version_no?: number } | null>(null);
  // New saves resolve the project's current policy on the server.
  const [ask, setAsk] = useState<Ask | null>(null);
  const [fitKey, setFitKey] = useState(0);
  const [status, setStatus] = useState<{ cursor: Pt | null; snap: Snap | null; step: number }>({ cursor: null, snap: null, step: 0 });
  const versions = useResource((signal) => listDrawingVersions(documentId!, { signal }), [documentId, head?.id], { enabled: !!documentId });

  const [versionMissing, setVersionMissing] = useState(false);
  useEffect(() => {
    if (!doc || !versions.data) return;
    if (!requestedVersion || requestedVersion === doc.head?.id) { setViewing(null); setVersionMissing(false); return; }
    const exact = versions.data.find((v) => v.id === requestedVersion);
    if (!exact) { setVersionMissing(true); return; }
    setVersionMissing(false);
    let active = true;
    void getDrawingVersion(doc.id, exact.version_no).then((value) => { if (active) { setViewing(value); setSelection([]); setVersionMissing(false); } }, (e: unknown) => { if (active) { setError(e); setVersionMissing(true); } });
    return () => { active = false; };
  }, [doc?.id, doc?.head?.id, versions.data, requestedVersion]);
  useAssistantPageContext(doc && shown && !versionMissing && (!requestedVersion || shown.id === requestedVersion) ? {
    projectId: doc.project_id, label: `${doc.number} · ${shown.label}${selection.length ? ` · ${selection.length}` : ''}`,
    object: { type: 'drawing_document', id: doc.id, version: shown.id },
    target: pending.length ? null : { kind: 'drawing_document_version', object_id: shown.id, project_id: doc.project_id, ...(selection.length ? { entities: { ids: selection } } : {}) },
    unavailable: pending.length ? t('core.context.unavailable') : null,
  } : null);

  const { onOperationEvent } = useAssistant();
  const liveContext = useRef({ pending, reload: res.reload, shown, setViewing }); liveContext.current = { pending, reload: res.reload, shown, setViewing };
  useEffect(() => onOperationEvent((event) => {
    const result = event.operation.result?.cad_result;
    if (result?.object_type !== 'drawing_document' || result.object_id !== documentId || event.projectId !== doc?.project_id) return;
    const current = liveContext.current;
    if (current.pending.length) { toast(t('core.result.newer'), { tone: 'info' }); return; }
    if (requestedVersion && current.shown) current.setViewing(current.shown);
    current.reload();
  }), [onOperationEvent, documentId, doc?.project_id, requestedVersion, toast, t]);

  const canEdit = !!doc && !!shown && caps.design && shown.id === head?.id && !doc.archived_at;
  useEffect(() => {
    // default layer for new entities when a document opens: OUTLINE when it exists
    if (head) setActiveLayer(head.layers.find((l) => l.name === 'OUTLINE')?.name ?? '0');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [doc?.id]);
  useEffect(() => {
    setFitKey((k) => k + 1);
  }, [doc?.id]);

  const preview = useMemo<{ content: DraftContent | null; error: string | null }>(() => {
    if (!shown) return { content: null, error: null };
    if (viewing || !pending.length) return { content: shown, error: null };
    try {
      return { content: applyLocal(shown, pending), error: null };
    } catch (e) {
      return { content: shown, error: e instanceof Error ? e.message : String(e) };
    }
  }, [shown, viewing, pending]);
  const content = preview.content;

  const push = useCallback((cmds: DraftCommand[]) => {
    setPending((p) => [...p, ...cmds]);
    setError(null);
  }, []);

  const requests = useMemo<CanvasRequests>(
    () => ({
      text: (kind) => new Promise((resolve) => setAsk({ kind, resolve: (v) => resolve(v == null ? null : String(v)) })),
      number: (kind) => new Promise((resolve) => setAsk({ kind, resolve: (v) => resolve(typeof v === 'number' ? v : null) })),
    }),
    [],
  );

  const afterSave = (next: DrawingDocument<VersionFull>, message: string) => {
    res.setData(next);
    setPending([]);
    setConflict(null);
    setViewing(null);
    const q = new URLSearchParams(search); q.delete('version'); setSearch(q, { replace: true });
    toast(message);
  };

  const save = async (base?: string) => {
    if (!doc || !head || !pending.length) return;
    setSaving(true);
    setError(null);
    try {
      const next = await saveDrawingVersion(doc.id, { base_version_id: base ?? head.id, commands: pending });
      afterSave(next, t('dr.saved', { v: next.head?.label ?? '' }));
    } catch (e) {
      if (isApiError(e) && e.code === 'version_conflict') setConflict(e.details as { head_version_no?: number });
      else setError(e);
    } finally {
      setSaving(false);
    }
  };

  const reapply = async () => {
    if (!doc) return;
    try {
      const latest = await getDrawingDocument(doc.id);
      res.setData(latest);
      if (latest.head) await save(latest.head.id);
    } catch (e) {
      setError(e);
    }
  };

  const discard = () => {
    setPending([]);
    setConflict(null);
    res.reload();
  };

  const onCalibrate = async (underlayId: string, p1: Pt, p2: Pt, distance: number) => {
    if (!doc || !head) return;
    if (pending.length) {
      setError(new Error(t('dr.cal.savePending')));
      return;
    }
    try {
      const next = await calibrateUnderlay(doc.id, { base_version_id: head.id, underlay_id: underlayId, p1_px: p1, p2_px: p2, distance_mm: distance });
      afterSave(next, t('dr.cal.saved'));
      setFitKey((k) => k + 1);
      setTool('select');
    } catch (e) {
      if (isApiError(e) && e.code === 'version_conflict') setConflict(e.details as { head_version_no?: number });
      else setError(e);
    }
  };

  const restore = async (v: VersionBrief) => {
    if (!doc || !head) return;
    try {
      const next = await restoreDrawingVersion(doc.id, { base_version_id: head.id, version_id: v.id });
      afterSave(next, t('dr.history.restored', { v: v.label, n: next.head?.label ?? '' }));
    } catch (e) {
      if (isApiError(e) && e.code === 'version_conflict') setConflict(e.details as { head_version_no?: number });
      else setError(e);
    }
  };

  const view = async (v: VersionBrief) => {
    if (pending.length && !window.confirm(t('pflow.leaveDirty'))) return;
    setPending([]);
    setSelection([]);
    const params = new URLSearchParams(search);
    params.set('version', v.id);
    setSearch(params);
  };

  const download = async (v: { id: string; label: string }, what: 'dxf' | 'dwg' | 'package') => {
    try {
      await downloadDrawingFile(v.id, what, `${doc?.number ?? 'drawing'}_${v.label}.${what === 'package' ? 'zip' : what}`);
    } catch (e) {
      setError(e);
    }
  };

  // page shortcuts: Delete, Ctrl/Cmd+Z (pending undo), Ctrl/Cmd+S (save)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (isTypingTarget(e.target) || document.querySelector('[role="dialog"][aria-modal="true"]')) return;
      if ((e.key === 'Delete' || e.key === 'Backspace') && canEdit && selection.length) {
        e.preventDefault();
        push([{ op: 'delete_entities', ids: selection }]);
        setSelection([]);
      } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z' && pending.length) {
        e.preventDefault();
        setPending((p) => p.slice(0, -1));
      } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
        e.preventDefault();
        void save();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });

  if (!documentId) return null;
  if (res.error && !doc) return <div className="page"><ErrorState error={res.error} onRetry={res.reload} /></div>;
  if (versionMissing) return <div className="page"><p className="callout err">{t('ver.missing')}</p></div>;
  if (!doc || !shown || !content) return <div className="page"><Spinner label={t('dr.loading')} /></div>;

  const ctx: PanelCtx = {
    content,
    version: shown,
    canEdit,
    selection,
    setSelection: (ids) => setSelection(ids),
    push,
    activeLayer,
    setActiveLayer,
    activeBlock,
    setActiveBlock,
    setTool,
  };
  const toolHint = t(`dr.hint.${tool}`);

  return (
    <div className="page dr-page">
      <FocusNotice kind="assumption" version={shown.id} />
      <header className="dr-head">
        <div className="dr-titles">
          <span className="eyebrow-sm">{t('dr.eyebrow')}</span>
          <h1>
            <span className="mono">{doc.number}</span> {doc.title}
          </h1>
          <VersionChips v={shown} />
        </div>
        <div className="dr-head-actions">
          <span className="mono dr-version-label">{shown.label}</span>
          <button type="button" className="btn sm" onClick={() => void download(shown, 'dxf')}>
            <Download aria-hidden /> DXF
          </button>
          <button type="button" className="btn sm" disabled={!shown.files.some((f) => f.format === 'dwg')} title={shown.dwg_status === 'unverified' ? t('dr.dwg.unverifiedHint') : shown.dwg_status !== 'passed' ? t('dr.dwg.missing') : undefined} onClick={() => void download(shown, 'dwg')}>
            <Download aria-hidden /> DWG
          </button>
          <button type="button" className="btn sm ghost" onClick={() => void download(shown, 'package')}>
            {t('dr.download.package')}
          </button>
        </div>
      </header>

      {viewing && (
        <div className="callout dr-banner" role="status">
          {t('dr.viewing', { v: viewing.label, head: head?.label ?? '' })}
          <button type="button" className="btn sm" onClick={() => { const q = new URLSearchParams(search); q.delete('version'); setSearch(q); setViewing(null); }}>
            {t('dr.viewing.back')}
          </button>
          {caps.design && (
            <button type="button" className="btn sm primary" onClick={() => void restore(viewing)}>
              {t('dr.history.restore')}
            </button>
          )}
        </div>
      )}
      {conflict && (
        <div className="callout dr-banner is-warn" role="alert">
          {t('dr.conflict409', { n: conflict.head_version_no ?? '?' })}
          <button type="button" className="btn sm" onClick={discard}>
            {t('dr.conflict409.discard')}
          </button>
          <button type="button" className="btn sm primary" onClick={() => void reapply()}>
            {t('dr.conflict409.reapply', { n: pending.length })}
          </button>
        </div>
      )}
      {shown.output_status === 'partial' && <div className="callout dr-banner is-warn">{t('dr.partial', { reason: String((shown.dwg as { message?: string; reason?: string }).message ?? (shown.dwg as { reason?: string }).reason ?? '') })}</div>}
      {error != null && <ErrorNotice error={error} />}
      {preview.error && <div className="callout dr-banner is-warn">{t('dr.previewError', { e: preview.error })}</div>}

      <div className="dr-toolbar" role="toolbar" aria-label={t('dr.toolbar')}>
        {TOOL_GROUPS.map((g) => (
          <div key={g.id} className="dr-tools">
            {g.tools.map(({ tool: id, icon, key }) => {
              const needsEdit = id !== 'select' && id !== 'pan';
              const needsSel = id === 'move' || id === 'copy' || id === 'rotate';
              return (
                <button
                  key={id}
                  type="button"
                  className={`btn sm icon${tool === id ? ' active' : ''}`}
                  aria-pressed={tool === id}
                  title={t(key)}
                  aria-label={t(key)}
                  disabled={(needsEdit && !canEdit) || (needsSel && !selection.length) || (id === 'insert' && !content.blocks.length)}
                  onClick={() => setTool(id)}
                >
                  {icon}
                </button>
              );
            })}
          </div>
        ))}
        <div className="dr-tools">
          {content.underlays.length > 0 && (
            <button type="button" className={`btn sm icon${tool === 'calibrate' ? ' active' : ''}`} title={t('dr.tool.calibrate')} aria-label={t('dr.tool.calibrate')} disabled={!canEdit} onClick={() => setTool('calibrate')}>
              <Crosshair />
            </button>
          )}
          <button type="button" className="btn sm icon" title={t('dr.action.delete')} aria-label={t('dr.action.delete')} disabled={!canEdit || !selection.length} onClick={() => { push([{ op: 'delete_entities', ids: selection }]); setSelection([]); }}>
            <Trash2 />
          </button>
          <details className="dr-snaps">
            <summary className="btn sm" title={t('dr.snap.title')}>
              <Magnet aria-hidden /> {t('dr.snap.short')}
            </summary>
            <div className="dr-snaps-menu">
              {SNAP_KINDS.map((k) => (
                <label key={k} className="check">
                  <input
                    type="checkbox"
                    checked={snapKinds.has(k)}
                    onChange={(e) =>
                      setSnapKinds((s) => {
                        const n = new Set(s);
                        if (e.target.checked) n.add(k);
                        else n.delete(k);
                        return n;
                      })
                    }
                  />{' '}
                  {t(`dr.snap.${k}`)}
                </label>
              ))}
            </div>
          </details>
        </div>
        <div className="dr-save">
          {canEdit && (
            <>
              <button type="button" className="btn sm icon" title={t('dr.undoPending')} aria-label={t('dr.undoPending')} disabled={!pending.length} onClick={() => setPending((p) => p.slice(0, -1))}>
                <Undo2 />
              </button>
              {null}
              <button type="button" className="btn sm primary" disabled={!pending.length || saving} onClick={() => void save()}>
                <Save aria-hidden /> {saving ? t('common.working') : pending.length ? t('dr.saveN', { n: pending.length }) : t('dr.saved.none')}
              </button>
            </>
          )}
          {!canEdit && !viewing && <span className="faint small">{t(doc.archived_at ? 'dr.readonly.archived' : 'dr.readonly')}</span>}
        </div>
      </div>

      <div className="dr-main">
        <div className="dr-stage">
          <DraftingCanvas
            content={content}
            versionId={shown.id}
            tool={tool}
            selection={selection}
            onSelect={(ids, additive) => setSelection((s) => (additive ? [...new Set([...s, ...ids])] : ids))}
            onCommand={push}
            onCalibrate={(u, a, b, d) => void onCalibrate(u, a, b, d)}
            requests={requests}
            snapKinds={snapKinds}
            readOnly={!canEdit}
            activeLayer={activeLayer}
            activeBlock={activeBlock}
            fitKey={fitKey}
            onStatus={setStatus}
          />
          <div className="dr-status" aria-live="polite">
            <span className="mono">{status.cursor ? `x ${fmtNum(status.cursor[0], 3)}  y ${fmtNum(status.cursor[1], 3)}` : '—'}</span>
            {status.snap && <span className="dr-snapname">{t(`dr.snap.${status.snap.kind}`)}</span>}
            <span className="faint">{toolHint}</span>
            <span className="faint">{t('dr.layerActive', { name: activeLayer })}</span>
            {selection.length > 0 && <span>{t('dr.selected', { n: selection.length })}</span>}
          </div>
        </div>
        <aside className="dr-side">
          <div className="dr-panel-tabs" role="tablist">
            {PANELS.map((p) => (
              <button key={p.id} type="button" role="tab" aria-selected={panel === p.id} className={`dr-tab${panel === p.id ? ' is-active' : ''}`} onClick={() => setPanel(p.id)} title={t(p.key)}>
                {p.icon}
                <span>{t(p.key)}</span>
              </button>
            ))}
          </div>
          {panel === 'props' && <PropertiesPanel ctx={ctx} />}
          {panel === 'layers' && <LayersPanel ctx={ctx} />}
          {panel === 'provenance' && <ProvenancePanel ctx={ctx} />}
          {panel === 'sheet' && <SheetPanel ctx={ctx} />}
          {panel === 'history' && (
            <HistoryPanel versions={versions.data} headId={head?.id ?? ''} viewingId={shown.id} canEdit={caps.design && !doc.archived_at} onView={(v) => void view(v)} onRestore={(v) => void restore(v)} onDownload={(v, w) => void download(v, w)} />
          )}
          {panel === 'checks' && <ChecksPanel version={shown} onSelect={(ids) => setSelection(ids)} />}
        </aside>
      </div>
      {ask && <AskDialog ask={ask} onDone={() => setAsk(null)} />}
      <button type="button" className="linkish small dr-back" onClick={() => navigate(paths.materials(doc.project_id, { type: 'drawing', subtype: 'drawing_document', item: `drawing_document:${doc.id}` }))}>
        {t('dr.backToList')}
      </button>
    </div>
  );
}

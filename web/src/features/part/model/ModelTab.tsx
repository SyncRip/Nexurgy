import { FocusNotice } from '../../todos/FocusNotice';
import { exactVersion,shouldFollowResult } from '../../assistant/exactVersion';
// Part workspace — Model tab: revision bar, 3D viewport, simplified CAD
// editor (feature tree + parameter panel, live kernel preview, undo / redo,
// save as revision), import, inspection and revision compare. Saved revisions
// and their selected faces/edges become removable context in the shared assistant.
// The editor is reached through the
// EditorAdapter (features/editor).

import { AlertTriangle,CheckCircle2,GitCompare,History,Info,Loader2,PencilRuler,ScanSearch,XCircle } from 'lucide-react';
import { useCallback,useEffect,useMemo,useRef,useState,useSyncExternalStore,type ReactNode } from 'react';
import { useSearchParams } from 'react-router-dom';
import { getRevision,listRevisions } from '../../../api/endpoints';
import { getRevisionDatums,revisionFeatureCheck,revisionTemplate } from '../../../api/geometry';
import type { ModelOp,ModelOpType,ModelRevision } from '../../../api/types';
import { isHistorical } from '../../../app/versionLinks';
import { ConfirmDialog } from '../../../components/ui/Dialog';
import { HistoricalBanner } from '../../../components/ui/HistoricalBanner';
import { EmptyState,ErrorState,ErrorText,SkeletonRows } from '../../../components/ui/States';
import { useToast } from '../../../components/ui/Toast';
import { isTypingTarget } from '../../../hooks/keyboard';
import { useResource } from '../../../hooks/useResource';
import { useI18n } from '../../../i18n/I18nProvider';
import type { PartTabProps } from '../../../pages/part/registry';
import type { EdgePick,FacePick,MeshDoc,Pick,ToolMode,Vec3 } from '../../../three/viewer';
import { useAssistant,useAssistantPageContext } from '../../assistant/AssistantProvider';
import { cadSelectionRef,matchHoleCandidates,partRef,partRevisionRef,type HoleMatch } from '../../assistant/refs';
import { RevisionChecksPanel } from '../../cad/RevisionChecks';
import { AcceptanceChip,GeomCheckChip } from '../../cad/Verdicts';
import { approxEdgeSelection,holePositions,isBodyOp,isEdgeOp,isHole,opIdFromLoc,opsEqual,roundVec3,validateOps } from '../../editor/ops';
import { SimpleEditorAdapter } from '../../editor/simpleAdapter';
import { readDraft,type StoredDraft } from '../../editor/store';
import { TemplateDialog } from '../../editor/TemplateDialog';
import { SimulatedBadge } from '../common';
import '../workspace.css';
import { AiSummary } from './AiSummary';
import { ComparePanel } from './ComparePanel';
import { draftBeforeLink } from './draftGate';
import { FeatureTree,type OpStatus } from './FeatureTree';
import { usePreview,useRevisionMesh } from './hooks';
import { ImportDialog } from './ImportDialog';
import { InspectPanel,type InspectAi,type InspectMetrics } from './InspectPanel';
import { OpEditor,type ServerIssue } from './OpEditor';
import { RevisionBar,isSimulated } from './RevisionBar';
import { SaveDialog } from './SaveDialog';
import { Viewport,type ViewportHandle } from './Viewport';

type SideTab = 'edit' | 'inspect' | 'compare';
type PickFor = { kind: 'hole_new' } | { kind: 'hole'; opId: string } | { kind: 'near'; opId: string } | null;
type Switch = { kind: 'rev'; rev: ModelRevision } | { kind: 'blank' };

export default function ModelTab({ part, project, caps, reloadPart }: PartTabProps) {
  const { t, fmt } = useI18n();
  const toast = useToast();
  const canDesign = caps.design && !part.archived_at;
  const revsRes = useResource((signal) => listRevisions(part.id, { signal }), [part.id]);
  const revisions = useMemo(() => revsRes.data ?? [], [revsRes.data]);

  const adapter = useMemo(() => new SimpleEditorAdapter({ partId: part.id, canDesign }), [part.id]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => () => adapter.dispose(), [adapter]);
  useEffect(() => adapter.setCanDesign(canDesign), [adapter, canDesign]);
  const st = useSyncExternalStore(adapter.store.subscribe, adapter.store.getState);

  // ?rev=<revision id> opens that revision (links from the assistant's result cards).
  const [search, setSearch] = useSearchParams();
  const revParam = search.get('rev');
  const updateRevisionQuery = useCallback((id: string | null) => {
    setSearch((current) => {
      const next = new URLSearchParams(current);
      if (id) next.set('rev', id);
      else next.delete('rev');
      return next;
    }, { replace: true });
  }, [setSearch]);
  const initialRev = useRef(revParam);
  const [selId, setSelId] = useState<string | null>(() => revParam);
  const [blankMode, setBlankMode] = useState(false);
  const blank = blankMode || (revsRes.data !== undefined && revisions.length === 0);
  const selected = blank ? null : exactVersion(revisions, selId);
  const [draftOffer, setDraftOffer] = useState<StoredDraft | null>(null);
  /** Revision a link (?rev=) asked for, held back until the unsaved draft on offer is restored or discarded. */
  const [deferredRev, setDeferredRev] = useState<ModelRevision | null>(null);
  const [pendingSwitch, setPendingSwitch] = useState<Switch | null>(null);
  const [saveOpen, setSaveOpen] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [templateOpen, setTemplateOpen] = useState(false);
  const [side, setSide] = useState<SideTab>(search.has('assumption') ? 'inspect' : canDesign ? 'edit' : 'inspect');
  const [mode, setMode] = useState<ToolMode>('select');
  const [pickFor, setPickFor] = useState<PickFor>(null);
  const [selPick, setSelPick] = useState<FacePick | EdgePick | null>(null);
  const [showResult, setShowResult] = useState(false);
  const viewport = useRef<ViewportHandle>(null);
  const loadedKey = useRef<string | null>(null);
  const firstLoad = useRef(true);

  // ─── load the selected revision into the editor ───────────────────────────
  // A new adapter (new part, or a hot reload) starts empty: load it again.
  useEffect(() => {
    loadedKey.current = null;
    firstLoad.current = true;
  }, [adapter]);

  useEffect(() => {
    if (!revsRes.data) return;
    if (firstLoad.current) {
      firstLoad.current = false;
      // Re-open the revision an unsaved draft belongs to. A URL that asked for another
      // revision must not silently replace that draft: offer it first (restore / discard);
      // the asked revision opens after a discard.
      const askedRev = initialRev.current ? (revisions.find((r) => r.id === initialRev.current) ?? null) : null;
      if (canDesign && askedRev) {
        const hold = draftBeforeLink(readDraft(adapter.draftKey), askedRev.id, revisions);
        if (hold) {
          setDeferredRev(askedRev);
          if (hold.kind === 'blank') setBlankMode(true);
          else setSelId(hold.id);
          return;
        }
      }
      if (canDesign && !initialRev.current) {
        const blankDraft = adapter.pendingDraft(null);
        if (blankDraft) {
          // The draft is a blank design: open the blank editor (it offers the draft).
          if (revisions.length) {
            setBlankMode(true);
            return;
          }
        } else {
          try {
            const raw = localStorage.getItem(adapter.draftKey);
            const parsed = raw ? (JSON.parse(raw) as StoredDraft) : null;
            if (parsed?.revisionId && revisions.some((r) => r.id === parsed.revisionId) && parsed.revisionId !== revisions[0]?.id) {
              setSelId(parsed.revisionId);
              return;
            }
          } catch {
            /* ignore */
          }
        }
      }
    }
    const key = blank ? 'blank' : (selected?.id ?? 'none');
    if (loadedKey.current === key) return;
    loadedKey.current = key;
    const draft = adapter.pendingDraft(selected);
    void adapter.loadModel(selected, { nextOpId: revisions[0]?.next_op_id ?? null });
    setDraftOffer(draft && !opsEqual(draft.ops, selected?.ops ?? []) ? draft : null);
    setSelPick(null);
    setPickFor(null);
    setMode('select');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [adapter, revsRes.data, selected?.id, blank]);

  // Keep a fresh copy of the selected revision (e.g. after reload) without resetting edits.
  // A draft still on offer counts as unsaved edits: switching away and editing would overwrite it.
  const requestSwitch = useCallback(
    (s: Switch) => {
      if (s.kind === 'rev' && !blank && s.rev.id === selected?.id) return;
      if (s.kind === 'blank' && blank) return;
      if (st.dirty || draftOffer) setPendingSwitch(s);
      else if (s.kind === 'blank') { setBlankMode(true); updateRevisionQuery(null); }
      else {
        setBlankMode(false);
        setSelId(s.rev.id);
        updateRevisionQuery(s.rev.id);
      }
    },
    [st.dirty, draftOffer, blank, selected?.id, updateRevisionQuery],
  );

  const doSwitch = (s: Switch) => {
    adapter.store.clearDraft();
    setDraftOffer(null);
    setDeferredRev(null);
    loadedKey.current = null;
    if (s.kind === 'blank') setBlankMode(true);
    else {
      setBlankMode(false);
      setSelId(s.rev.id);
    }
    updateRevisionQuery(s.kind === 'rev' ? s.rev.id : null);
    setPendingSwitch(null);
  };

  // ?rev= changed while the page is open (e.g. "View" on a result card): switch
  // to it like a click on the revision bar (unsaved edits ask first). Keep the
  // exact version in the URL for reload/share. A new revision may need one list reload.
  const revMiss = useRef<string | null>(null);
  useEffect(() => {
    if (!revParam || !revsRes.data) return;
    const rev = revsRes.data.find((r) => r.id === revParam);
    if (!rev && revMiss.current !== revParam) {
      revMiss.current = revParam;
      revsRes.reload();
      return;
    }
    if (rev) {
      // A draft on offer decides first (restore / discard); the link's revision waits for a discard.
      if (draftOffer && (blank || rev.id !== selected?.id)) setDeferredRev(rev);
      else requestSwitch({ kind: 'rev', rev });
    } else { setBlankMode(false); setSelId(revParam); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [revParam, revsRes.data]);

  // ─── unified assistant ────────────────────────────────────────────────────
  const { onOperationEvent } = useAssistant();
  const latestRev = revisions[0] ?? null;

  // Leaving the page with unsaved edits (the draft is also kept locally).
  useEffect(() => {
    if (!st.dirty) return;
    const onBefore = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = '';
    };
    window.addEventListener('beforeunload', onBefore);
    return () => window.removeEventListener('beforeunload', onBefore);
  }, [st.dirty]);

  // ─── validation + preview ─────────────────────────────────────────────────
  const hasBase = !!st.base;
  const activeBodies = st.ops.filter((o) => isBodyOp(o) && !o.suppressed).length;
  const hasGeometry = hasBase || activeBodies > 0;
  const clientIssues = useMemo(() => validateOps(st.ops, hasBase), [st.ops, hasBase]);
  const clientInvalid = Object.keys(clientIssues).length;
  const previewOn = canDesign && st.editable && st.dirty && hasGeometry && clientInvalid === 0;
  const preview = usePreview(part.id, st.base, st.ops, previewOn);
  const revMesh = useRevisionMesh(selected);
  // W3: datums for datum planes (the draft's from the preview, else the revision's), template provenance, feature check
  const revDatums = useResource((signal) => (selected && !selected.mesh_only ? getRevisionDatums(selected.id, { signal }) : Promise.resolve({ datums: [] })), [selected?.id]);
  const datums = (st.dirty ? preview.result?.datums : undefined) ?? revDatums.data?.datums ?? [];
  const templateMeta = revisionTemplate(selected);
  const featureCheck = st.dirty ? (preview.result?.feature_check ?? null) : revisionFeatureCheck(selected);

  const selectedOp = st.ops.find((o) => o.id === st.selectedOpId) ?? null;
  const selectedIdx = selectedOp ? st.ops.indexOf(selectedOp) : -1;
  // Fillet / chamfer edit: show the model *before* the op so its edges are visible.
  const beforeOps = useMemo(() => (selectedOp && isEdgeOp(selectedOp) ? st.ops.slice(0, selectedIdx) : []), [selectedOp, selectedIdx, st.ops]);
  const beforeHasGeometry = hasBase || beforeOps.some((o) => isBodyOp(o) && !o.suppressed);
  const beforeValid = useMemo(() => Object.keys(validateOps(beforeOps, hasBase)).length === 0, [beforeOps, hasBase]);
  const edgeContext = !!selectedOp && isEdgeOp(selectedOp) && canDesign && beforeHasGeometry && beforeValid && (!showResult || pickFor?.kind === 'near');
  const before = usePreview(part.id, st.base, beforeOps, edgeContext, 250);

  let mesh: MeshDoc | null = null;
  let glb: ArrayBuffer | null = null;
  let dimmed = false;
  if (edgeContext) {
    mesh = before.mesh ?? before.lastGoodMesh;
    dimmed = !before.mesh;
  } else if (!st.dirty) {
    mesh = revMesh.doc;
    glb = revMesh.glb;
  } else if (hasGeometry) {
    if (preview.mesh && (preview.status === 'ok' || preview.status === 'failed')) mesh = preview.mesh;
    else {
      mesh = preview.lastGoodMesh ?? revMesh.doc;
      dimmed = true;
    }
  }

  // A different mesh invalidates face / edge indices of the old selection.
  useEffect(() => setSelPick(null), [mesh]);

  // ─── per-op status and issues ─────────────────────────────────────────────
  const features = st.dirty ? (preview.result?.features ?? []) : (selected?.features ?? []);
  const serverIssues = useMemo(() => {
    const map: Record<string, ServerIssue[]> = {};
    const global: ServerIssue[] = [];
    if (st.dirty && preview.status === 'invalid') {
      for (const e of preview.invalid) {
        const id = opIdFromLoc(e.loc, st.ops);
        if (id) (map[id] ??= []).push({ code: 'invalid_ops', message: e.msg });
        else global.push({ code: 'invalid_ops', message: e.msg });
      }
    }
    if (st.dirty && preview.result) {
      for (const e of preview.result.errors) {
        if (e.op_id && st.ops.some((o) => o.id === e.op_id)) (map[e.op_id] ??= []).push({ code: e.code, message: e.message });
        else global.push({ code: e.code, message: e.message });
      }
    }
    return { map, global };
  }, [st.dirty, st.ops, preview.status, preview.invalid, preview.result]);

  const statuses: Record<string, OpStatus> = {};
  for (const op of st.ops) {
    const f = features.find((x) => x.op_id === op.id);
    if (clientIssues[op.id] || (serverIssues.map[op.id] && preview.status === 'invalid')) statuses[op.id] = 'invalid';
    else if (serverIssues.map[op.id]) statuses[op.id] = 'error';
    else if (op.suppressed) statuses[op.id] = 'suppressed';
    else if (st.dirty && (preview.status === 'pending' || preview.status === 'building')) statuses[op.id] = 'pending';
    else if (f) statuses[op.id] = f.status;
  }

  // ─── actions ──────────────────────────────────────────────────────────────
  const bbox = mesh && 'min' in (mesh.bbox as object) ? (mesh.bbox as { min: number[]; max: number[] }) : null;

  const addOp = (type: ModelOpType) => {
    if (type === 'hole') {
      setPickFor({ kind: 'hole_new' });
      setMode('pick_face');
      setSide('edit');
      return;
    }
    const id = adapter.store.addOp(type, {}, bbox);
    if (id) setSide('edit');
  };

  const startPick = (p: PickFor) => {
    if (!p) return;
    setPickFor(p);
    setMode(p.kind === 'near' ? 'pick_edge' : 'pick_face');
    if (p.kind === 'near') setShowResult(false);
  };

  const cancelPick = () => {
    setPickFor(null);
    setMode('select');
  };

  const onPick = (p: Pick | null) => {
    if (pickFor) {
      if (pickFor.kind === 'near') {
        if (p?.kind !== 'edge') return;
        const near = roundVec3(p.point);
        adapter.store.updateOp(pickFor.opId, (op) => (isEdgeOp(op) ? { ...op, edges: { ...op.edges, near, max_count: op.edges.max_count ?? 1 } } : op));
        cancelPick();
        return;
      }
      if (p?.kind !== 'face') return;
      if (!p.planar) {
        toast(t('ws.hole.needPlanar'), { tone: 'info' });
        return;
      }
      const at = roundVec3(p.point);
      const direction = roundVec3(p.normal.map((c) => -c), 6);
      if (pickFor.kind === 'hole_new') adapter.store.addOp('hole', { at, direction } as Partial<ModelOp>, bbox);
      else adapter.store.updateOp(pickFor.opId, (op) => (isHole(op) ? { ...op, at, direction } : op));
      cancelPick();
      return;
    }
    if (p && (p.kind === 'face' || p.kind === 'edge')) {
      setSelPick(p);
      adapter.select({ kind: p.kind, id: p.index }, { point: p.point, info: { id: p.id } });
      if (side === 'edit' && !selectedOp) setSide('inspect');
    } else {
      setSelPick(null);
      adapter.select(null);
    }
  };

  /** Show a revision the server just created (inserted locally first, then the list is refreshed). */
  const openNewRevision = (rev: ModelRevision) => {
    revsRes.setData((prev) => [rev, ...(prev ?? []).filter((r) => r.id !== rev.id)]);
    loadedKey.current = null;
    setBlankMode(false);
    setSelId(rev.id);
    updateRevisionQuery(rev.id);
    revsRes.reload();
    reloadPart();
  };

  const onSaved = (rev: ModelRevision) => {
    toast(t('ws.save.done', { rev: rev.label }));
    setSaveOpen(false);
    openNewRevision(rev);
  };

  const onImported = (rev: ModelRevision) => {
    setImportOpen(false);
    toast(t(rev.mesh_only ? 'ws.import.doneMesh' : 'ws.import.done', { rev: rev.label }));
    openNewRevision(rev);
  };

  /** A revision the assistant created for this part: refresh, and open it unless there are unsaved edits. */
  const onAiRevision = (rev: ModelRevision) => {
    // A draft still on offer is unsaved work too: do not switch away from it.
    if (!draftOffer && shouldFollowResult(selected?.id ?? null, latestRev?.id ?? null, adapter.store.getState().dirty)) {
      openNewRevision(rev);
      toast(t('ar.model.newRevision', { rev: rev.label }), { tone: 'info' });
    } else {
      revsRes.setData((prev) => [rev, ...(prev ?? []).filter((r) => r.id !== rev.id)]);
      reloadPart();
      toast(t('ar.model.newRevisionDirty', { rev: rev.label }), { tone: 'info' });
    }
  };
  const onAiRevisionRef = useRef(onAiRevision);
  onAiRevisionRef.current = onAiRevision;
  const reloadRevsRef = useRef(revsRes.reload);
  reloadRevsRef.current = revsRes.reload;

  // Operation results: a new revision of this part (whichever conversation made it).
  // Only the page follows the result; the assistant's target never changes here.
  const handledOps = useRef(new Set<string>());
  useEffect(() => {
    const controller = new AbortController();
    const unsubscribe = onOperationEvent((e) => {
        if ((e.type !== 'succeeded' && e.type !== 'result') || (e.projectId && e.projectId !== project.id)) return;
        const res = e.operation.result;
        const cad = res?.cad_result;
        const eventId = cad?.event_id ?? e.operation.id;
        if (handledOps.current.has(eventId)) return;
        let versionId: string | undefined;
        if (cad?.object_type === 'part_model' && cad.object_id === part.id) versionId = cad.undo.restored?.version_id ?? cad.version_id;
        else if (res && res.kind !== 'task_draft' && Array.isArray(res.items)) versionId = res.items.find((i) => i.ref.kind === 'part_revision' && (i.ref.part_id ? i.ref.part_id === part.id : e.operation.part?.id === part.id))?.ref.object_id;
        if (!versionId) return;
        handledOps.current.add(eventId);
        getRevision(versionId, { signal: controller.signal })
          .then((rev) => {
            if (!controller.signal.aborted && rev.part_id === part.id) onAiRevisionRef.current(rev);
          })
          .catch(() => { if (!controller.signal.aborted) reloadRevsRef.current(); });
      });
    return () => { controller.abort(); unsubscribe(); };
  }, [onOperationEvent, part.id, project.id]);

  const saveDisabledReason =
    clientInvalid > 0
      ? t('ws.save.fixFields', { n: clientInvalid })
      : !hasGeometry
        ? t('ws.save.empty')
        : preview.status === 'pending' || preview.status === 'building'
          ? t('ws.save.waitPreview')
          : preview.status === 'failed' || preview.status === 'invalid'
            ? t('ws.save.buildFailed')
            : null;

  // ─── keyboard ─────────────────────────────────────────────────────────────
  const keyState = useRef({ st, selectedOp, pickFor, mode, saveDisabledReason });
  keyState.current = { st, selectedOp, pickFor, mode, saveDisabledReason };
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (document.querySelector('[role="dialog"][aria-modal="true"], [role="alertdialog"][aria-modal="true"]')) return;
      const k = keyState.current;
      const mod = e.metaKey || e.ctrlKey;
      const typing = isTypingTarget(e.target);
      if (mod && e.key.toLowerCase() === 's') {
        e.preventDefault();
        if (k.st.editable && k.st.dirty && !k.saveDisabledReason) setSaveOpen(true);
        return;
      }
      if (typing) return;
      if (mod && e.key.toLowerCase() === 'z') {
        e.preventDefault();
        if (e.shiftKey) adapter.store.redo();
        else adapter.store.undo();
        return;
      }
      if (mod && e.key.toLowerCase() === 'y') {
        e.preventDefault();
        adapter.store.redo();
        return;
      }
      if (mod || e.altKey) return;
      if (e.key === 'Escape') {
        if (k.pickFor) cancelPick();
        else if (k.mode === 'measure') setMode('select');
        else {
          setSelPick(null);
          adapter.store.selectOp(null);
        }
      } else if ((e.key === 'Delete' || e.key === 'Backspace') && k.selectedOp && k.st.editable) {
        e.preventDefault();
        adapter.store.removeOp(k.selectedOp.id);
      } else if (e.key === 'f' || e.key === 'F') viewport.current?.fit();
      else if (e.key === '0') viewport.current?.setView('iso');
      else if (e.key === '1') viewport.current?.setView('front');
      else if (e.key === '2') viewport.current?.setView('top');
      else if (e.key === '3') viewport.current?.setView('right');
      else if (e.key === 'm' || e.key === 'M') setMode((m) => (m === 'measure' ? 'select' : 'measure'));
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [adapter]);

  // ─── selection → AI reference ─────────────────────────────────────────────
  // Only a face / edge of the saved revision on screen can be referenced: ids
  // belong to that build (build_key + mesh_sha). Preview geometry cannot (U07).
  const selRev = !blank && selected && !st.dirty && !edgeContext ? selected : null;
  const selDisabled = !selPick ? null : st.dirty ? t('ar.model.selDirty') : edgeContext ? t('ar.model.selPreview') : !selRev ? t('ar.model.selNoRevision') : null;
  const describeHole = useCallback(
    (m: HoleMatch) =>
      t('ar.model.holeReason', {
        r: fmt.number(m.faceRadius, { maximumFractionDigits: 3 }),
        what: t(m.feature === 'counterbore' ? 'ar.model.holeCb' : 'ar.model.holeBore', { d: fmt.number(m.diameter, { maximumFractionDigits: 3 }) }),
        delta: fmt.number(m.radiusDelta, { maximumFractionDigits: 3 }),
        dist: fmt.number(m.axisDistance, { maximumFractionDigits: 3 }),
        inst: m.count > 1 ? t('ar.model.holeInst', { i: m.instance, n: m.count }) : '',
      }),
    [t, fmt],
  );
  const holeCands = useMemo(() => {
    if (!selPick || selPick.kind !== 'face' || !selRev) return [];
    const faceAxis = mesh?.faces[selPick.index]?.axis ?? null;
    return matchHoleCandidates(selPick, selRev.holes ?? [], { faceAxis, describe: describeHole });
  }, [selPick, selRev, mesh, describeHole]);
  // One candidate is preselected; with several the user picks one (or none = geometry only).
  const pickKey = selPick ? `${selPick.kind}:${selPick.index}:${selPick.point.join(',')}` : '';
  const [resolvedChoice, setResolvedChoice] = useState<{ key: string; opId: string | null } | null>(null);
  const resolvedOpId = resolvedChoice?.key === pickKey ? resolvedChoice.opId : holeCands.length === 1 ? holeCands[0].op_id : null;
  const selScope =
    selPick && selected
      ? `${t(selPick.kind === 'face' ? 'ar.model.selScopeFace' : 'ar.model.selScopeEdge', { rev: selected.label, id: String(selPick.id) })}${resolvedOpId ? ` · ${resolvedOpId}` : ''}`
      : t('ws.inspect.preview');
  const selInput = () => (selPick && selRev ? cadSelectionRef(project.id, selRev, selPick, { candidates: holeCands, resolvedOpId }) : null);
  useAssistantPageContext({
    projectId: project.id, partId: part.id,
    object: { type: 'part_model', id: part.id, version: selected?.id ?? null },
    label: selPick ? selScope : `${part.number} · ${selected?.label ?? t('ws.rev.blank')}`,
    target: selId && !selected && !blank ? null : selPick ? (selDisabled ? null : selInput()) : selected ? partRevisionRef(project.id, selected.id) : partRef(project.id, part.id),
    unavailable: selDisabled,
  });
  const inspectAi: InspectAi | null = selPick
    ? {
        input: selInput,
        scope: selScope,
        disabledReason: selDisabled,
        revisionLabel: selRev?.label ?? null,
        candidates: holeCands,
        resolvedOpId,
        onResolve: (opId) => setResolvedChoice({ key: pickKey, opId }),
        cylinder: selPick.kind === 'face' && selPick.surface === 'cylinder',
      }
    : null;

  // ─── viewport decorations ─────────────────────────────────────────────────
  const candidates = useMemo(() => {
    if (!edgeContext || !selectedOp || !isEdgeOp(selectedOp) || !mesh) return [];
    const size = bbox ? Math.hypot(bbox.max[0] - bbox.min[0], bbox.max[1] - bbox.min[1], bbox.max[2] - bbox.min[2]) : 100;
    return approxEdgeSelection(mesh.edges, selectedOp.edges, size);
  }, [edgeContext, selectedOp, mesh, bbox]);

  const markers = useMemo(() => {
    if (selectedOp && isHole(selectedOp)) return { points: holePositions(selectedOp), direction: (selectedOp.direction ?? [0, 0, -1]) as Vec3 };
    if (selectedOp && isEdgeOp(selectedOp) && selectedOp.edges.near) return { points: [selectedOp.edges.near as Vec3], direction: null };
    // Hole candidates of the picked face: the chosen one, or all of them while the user decides.
    const shown = (selRev?.holes ?? []).filter((h) => (resolvedOpId ? h.op_id === resolvedOpId : holeCands.some((c) => c.op_id === h.op_id)));
    if (shown.length) {
      const points = shown.flatMap((h) => (h.positions?.length ? h.positions : [h.entry]).filter((p) => p?.length >= 3).map((p) => [p[0], p[1], p[2]] as Vec3));
      return { points, direction: shown.length === 1 && shown[0].axis?.length >= 3 ? ([shown[0].axis[0], shown[0].axis[1], shown[0].axis[2]] as Vec3) : null };
    }
    return { points: [] as Vec3[], direction: null };
  }, [selectedOp, selRev, resolvedOpId, holeCands]);

  const selectionHl = useMemo(() => (selPick ? (selPick.kind === 'face' ? { faces: [selPick.index] } : { edges: [selPick.index] }) : null), [selPick]);

  // ─── render ───────────────────────────────────────────────────────────────
  if (revsRes.error && !revsRes.data) return <ErrorState error={revsRes.error} onRetry={revsRes.reload} />;
  if (!revsRes.data) return <SkeletonRows rows={4} />;

  const readOnlyNote = selected?.mesh_only ? t('ws.meshRef.note') : !canDesign ? t('ws.readOnly') : null;

  let status: ReactNode = null;
  if (edgeContext) {
    status = <span className="chip measure">{t('ws.status.beforeOp', { id: selectedOp!.id })}</span>;
  } else if (!st.dirty) {
    status = selected ? (
      <span className={`chip ${selected.mesh_only ? 'pending' : 'ok'}`}>
        {selected.mesh_only ? <Info aria-hidden /> : <CheckCircle2 aria-hidden />} {selected.label} · {selected.mesh_only ? t('ws.meshRef.short') : t('ws.status.saved')}
      </span>
    ) : null;
  } else if (!hasGeometry) {
    status = null;
  } else if (clientInvalid) {
    status = (
      <span className="chip warn">
        <AlertTriangle aria-hidden /> {t('ws.status.fixFields', { n: clientInvalid })}
      </span>
    );
  } else if (preview.status === 'pending' || preview.status === 'building') {
    status = (
      <span className="chip measure">
        <Loader2 className="spin-icon" aria-hidden /> {t('ws.status.building')}
      </span>
    );
  } else if (preview.status === 'ok') {
    status = (
      <span className="chip ok" title={preview.result?.kernel_version}>
        <CheckCircle2 aria-hidden /> {t('ws.status.previewOk')}
      </span>
    );
  } else if (preview.status === 'failed') {
    const first = preview.result?.errors[0];
    status = (
      <span className="chip err" title={first?.message}>
        <XCircle aria-hidden /> {first?.op_id ? t('ws.status.failedAt', { id: first.op_id }) : t('ws.status.failed')}
      </span>
    );
  } else if (preview.status === 'invalid') {
    status = (
      <span className="chip warn">
        <AlertTriangle aria-hidden /> {t('ws.status.invalid')}
      </span>
    );
  } else if (preview.status === 'error') {
    status = (
      <span className="chip err">
        <XCircle aria-hidden /> {t('ws.status.unavailable')}
      </span>
    );
  }

  const banner = pickFor ? (
    <>
      {pickFor.kind === 'near' ? t('ws.pick.edge') : t('ws.pick.face')}
      <button type="button" className="btn sm ghost" onClick={cancelPick}>
        {t('common.cancel')} (Esc)
      </button>
    </>
  ) : edgeContext ? (
    <>
      {t('ws.pick.edgeContext', { id: selectedOp!.id })}
      <button type="button" className="btn sm ghost" onClick={() => setShowResult(true)}>
        {t('ws.pick.showResult')}
      </button>
    </>
  ) : null;

  const metrics: InspectMetrics | null = st.dirty ? (preview.result?.metrics ?? null) : selected && Object.keys(selected.metrics ?? {}).length ? (selected.metrics as InspectMetrics) : null;

  const empty =
    selected?.mesh_only && !revMesh.doc && !revMesh.glb ? (
      t('ws.vw.noPreview')
    ) : !hasGeometry ? (
      <div className="vw-empty-card">
        <PencilRuler aria-hidden />
        <strong>{canDesign ? t('ws.empty.title') : t('rev.none')}</strong>
        <span className="faint">{canDesign ? t('ws.empty.body') : t('ws.empty.viewer')}</span>
      </div>
    ) : null;

  if (!blank && selId && !selected && revsRes.data) return <div className="callout err"><p>{t('ver.missing')}</p>{latestRev && <button type="button" className="btn" onClick={() => requestSwitch({ kind: 'rev', rev: latestRev })}>{t('ver.history.open')}</button>}</div>;

  return (
    <div className="ws-model">
      <FocusNotice kind="assumption" version={selected?.id ?? part.id} />
      <RevisionBar
        revisions={revisions}
        selectedId={selected?.id ?? null}
        blank={blank}
        partNumber={part.number}
        canDesign={canDesign}
        ai={{ projectId: project.id, partId: part.id, dirty: st.dirty }}
        onSelect={(rev) => requestSwitch({ kind: 'rev', rev })}
        onBlank={() => requestSwitch({ kind: 'blank' })}
        onImport={() => setImportOpen(true)}
      />

      {draftOffer && (
        <div className="callout info slim ws-draft">
          <History aria-hidden />
          <div className="grow">
            <div>{t('ws.draft.found', { time: fmt.dateTime(draftOffer.savedAt), n: draftOffer.ops.length })}</div>
            {deferredRev && <div className="faint">{t('ws.draft.deferred', { rev: deferredRev.label })}</div>}
          </div>
          <button
            type="button"
            className="btn sm primary"
            onClick={() => {
              void adapter.loadModel(selected, { nextOpId: revisions[0]?.next_op_id ?? null, draft: draftOffer });
              updateRevisionQuery(selected?.id ?? null);
              setDraftOffer(null);
              setDeferredRev(null);
            }}
          >
            {t('ws.draft.restore')}
          </button>
          <button
            type="button"
            className="btn sm"
            onClick={() => {
              adapter.store.clearDraft();
              setDraftOffer(null);
              // The link's revision waited for this decision.
              if (deferredRev) {
                setBlankMode(false);
                setSelId(deferredRev.id);
                updateRevisionQuery(deferredRev.id);
                setDeferredRev(null);
              }
            }}
          >
            {t('ws.draft.discard')}
          </button>
        </div>
      )}

      {/* S6: a link (or the revision bar) opened a revision that is not the current one: say so, offer the current. */}
      {!blank && selected && latestRev && isHistorical(selected.id, latestRev.id) && (
        <HistoricalBanner current={latestRev.label} onOpenCurrent={() => requestSwitch({ kind: 'rev', rev: latestRev })} />
      )}

      {selected && isSimulated(selected) && !st.dirty && (
        <div className="callout warn slim">
          <SimulatedBadge />
          <span>{t('ws.simulated.revision', { rev: selected.label })}</span>
        </div>
      )}

      <div className="ws-model-main">
        <aside className="panel ws-left" aria-label={t('ws.tree.title')}>
          <FeatureTree
            ops={st.ops}
            base={st.base}
            selectedOpId={st.selectedOpId}
            statuses={statuses}
            editable={st.editable}
            hasBody={hasGeometry}
            canUndo={st.canUndo}
            canRedo={st.canRedo}
            dirty={st.dirty}
            saveDisabledReason={saveDisabledReason}
            readOnlyNote={readOnlyNote}
            onSelect={(id) => {
              adapter.store.selectOp(id);
              if (id) setSide('edit');
              setShowResult(false);
            }}
            onSuppress={(id, v) => adapter.store.setSuppressed(id, v)}
            onRemove={(id) => adapter.store.removeOp(id)}
            onAdd={addOp}
            onUndo={() => adapter.store.undo()}
            onRedo={() => adapter.store.redo()}
            onSave={() => setSaveOpen(true)}
            onRevert={() => {
              adapter.store.revert();
              adapter.store.clearDraft();
            }}
            onTemplate={canDesign && st.editable ? () => setTemplateOpen(true) : undefined}
            template={templateMeta}
            featureCheck={featureCheck}
          />
        </aside>

        <section className="ws-center">
          <Viewport
            ref={viewport}
            mesh={mesh}
            glb={glb}
            fitToken={blank ? 'blank' : (selected?.id ?? 'none')}
            mode={mode}
            onModeChange={(m) => {
              setPickFor(null);
              setMode(m);
            }}
            selection={selectionHl}
            candidates={candidates}
            markers={markers}
            onPick={onPick}
            banner={banner}
            status={status}
            empty={empty}
            loading={!st.dirty && revMesh.loading && !mesh && !glb}
            dimmed={dimmed}
          />
          {revMesh.error && !st.dirty ? (
            <div className="callout err slim">
              <XCircle aria-hidden /> <ErrorText error={revMesh.error} />
            </div>
          ) : null}
          {serverIssues.global.length > 0 && hasGeometry && (
            <div className="callout err slim" role="alert">
              <XCircle aria-hidden />
              <div>
                {serverIssues.global.map((g, i) => (
                  <div key={i}>
                    {g.code && <code className="error-code">{g.code}</code>} {g.message}
                  </div>
                ))}
              </div>
            </div>
          )}
          {preview.status === 'error' && st.dirty && (
            <div className="callout err slim">
              <XCircle aria-hidden /> <ErrorText error={preview.error} />
            </div>
          )}
          <p className="ws-kbd-help faint small">{t('ws.kbd.help')}</p>
        </section>

        <aside className="panel ws-right">
          <div className="ws-side-tabs" role="tablist" aria-label={t('ws.side.label')}>
            {(
              [
                ['edit', <PencilRuler key="i" />, t('ws.side.edit')],
                ['inspect', <ScanSearch key="i" />, t('ws.side.inspect')],
                ['compare', <GitCompare key="i" />, t('ws.side.compare')],
              ] as [SideTab, ReactNode, string][]
            ).map(([id, icon, label]) => (
              <button key={id} type="button" role="tab" aria-selected={side === id} className={`ws-side-tab ${side === id ? 'active' : ''}`} onClick={() => setSide(id)}>
                {icon}
                <span>{label}</span>
              </button>
            ))}
          </div>
          <div className="ws-side-body" role="tabpanel">
            {side === 'edit' &&
              (selectedOp ? (
                <OpEditor
                  key={selectedOp.id}
                  op={selectedOp}
                  ops={st.ops}
                  editable={st.editable}
                  hasBase={hasBase}
                  issues={clientIssues[selectedOp.id] ?? []}
                  serverIssues={serverIssues.map[selectedOp.id] ?? []}
                  update={(fn, key) => adapter.store.updateOp(selectedOp.id, fn, { coalesce: key })}
                  remove={() => adapter.store.removeOp(selectedOp.id)}
                  pickFace={() => (pickFor?.kind === 'hole' ? cancelPick() : startPick({ kind: 'hole', opId: selectedOp.id }))}
                  pickEdge={() => (pickFor?.kind === 'near' ? cancelPick() : startPick({ kind: 'near', opId: selectedOp.id }))}
                  picking={pickFor?.kind === 'hole' ? 'face' : pickFor?.kind === 'near' ? 'edge' : null}
                  datums={datums}
                />
              ) : (
                <EditIntro canDesign={canDesign} revision={selected} meshOnly={!!selected?.mesh_only} onShowChecks={() => setSide('inspect')} />
              ))}
            {side === 'inspect' && (
              <InspectPanel
                sourceLabel={st.dirty ? t('ws.inspect.preview') : (selected?.label ?? '—')}
                metrics={metrics}
                holes={st.dirty ? (preview.result?.holes ?? []) : (selected?.holes ?? [])}
                detected={st.dirty ? (preview.result?.detected_holes ?? []) : (selected?.detected_holes ?? [])}
                warnings={st.dirty ? (preview.result?.warnings ?? []) : (selected?.warnings ?? [])}
                kernelVersion={st.dirty ? (preview.result?.kernel_version ?? null) : (selected?.kernel_version ?? null)}
                meshOnly={!!selected?.mesh_only && !st.dirty}
                selection={selPick}
                ai={inspectAi}
              />
            )}
            {side === 'inspect' && selected && !blank && <RevisionChecksPanel rev={selected} dirty={st.dirty} />}
            {side === 'compare' && <ComparePanel revision={selected} revisions={revisions} />}
          </div>
        </aside>
      </div>

      {saveOpen && (
        <SaveDialog
          parentLabel={selected?.label ?? null}
          ops={st.ops}
          onClose={() => setSaveOpen(false)}
          onSave={async (note) => {
            const rev = await adapter.saveRevision(note);
            onSaved(rev);
            return rev;
          }}
        />
      )}
      {importOpen && <ImportDialog part={part} project={project} onClose={() => setImportOpen(false)} onImported={onImported} />}
      {templateOpen && (
        <TemplateDialog
          partId={part.id}
          parentId={selected?.mesh_only ? null : (selected?.id ?? null)}
          dirty={st.dirty}
          onClose={() => setTemplateOpen(false)}
          onCreated={(rev) => {
            setTemplateOpen(false);
            toast(t('geo.tpl.done', { rev: rev.label }));
            openNewRevision(rev);
          }}
        />
      )}
      {pendingSwitch && (
        <ConfirmDialog
          title={t('ws.discard.title')}
          body={t('ws.discard.body')}
          confirmLabel={t('ws.discard.confirm')}
          danger
          onConfirm={() => doSwitch(pendingSwitch)}
          onClose={() => { setPendingSwitch(null); updateRevisionQuery(blank ? null : selected?.id ?? null); }}
        />
      )}
    </div>
  );
}

function EditIntro({ canDesign, revision, meshOnly, onShowChecks }: { canDesign: boolean; revision: ModelRevision | null; meshOnly: boolean; onShowChecks: () => void }) {
  const { t } = useI18n();
  const hasChecks = !!revision && (!!revision.check_status || !!revision.checks?.step_audit || !!revision.checks?.profile);
  if (meshOnly) {
    return (
      <div className="stack-sm">
        <div className="callout pending">
          <Info aria-hidden />
          <div>
            <strong>{t('ws.meshRef.title')}</strong>
            <p className="small">{t('ws.meshRef.body')}</p>
          </div>
        </div>
      </div>
    );
  }
  return (
    <div className="stack-sm ws-intro">
      {canDesign ? (
        <>
          <EmptyState compact title={t('ws.intro.title')} body={t('ws.intro.body')} icon={<PencilRuler />} />
          <ul className="ws-plain small muted">
            <li>{t('ws.intro.l1')}</li>
            <li>{t('ws.intro.l2')}</li>
            <li>{t('ws.intro.l3')}</li>
            <li>{t('ws.intro.l4')}</li>
          </ul>
        </>
      ) : (
        <p className="faint small">{t('ws.readOnly')}</p>
      )}
      {revision && hasChecks && (
        <section className="ws-sec stack-sm">
          <div className="cad-chips">
            <GeomCheckChip status={revision.check_status} showNone />
            <AcceptanceChip />
          </div>
          <button type="button" className="linkish small" onClick={onShowChecks}>
            {t('cad.req.title')}
          </button>
        </section>
      )}
      {revision?.source === 'ai' && revision.ai_summary && (
        <section className="ws-sec">
          <h4>{t('ws.intro.aiSummary', { rev: revision.label })}</h4>
          <AiSummary rev={revision} />
        </section>
      )}
      {revision?.note && (
        <p className="small">
          <span className="faint">{t('ws.intro.note')}: </span>
          {revision.note}
        </p>
      )}
    </div>
  );
}

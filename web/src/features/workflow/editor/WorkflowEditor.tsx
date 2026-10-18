import { useAssistantPageContext } from '../../assistant/AssistantProvider';
// The template draft editor (the "Configure" tab of a template): palette, React
// Flow canvas, inspector, save / validate, and entry points to run and publish
// (handled by the template page). Server state (version, validation) is
// authoritative; the canvas keeps a local draft (also mirrored to localStorage
// while unsaved). Editing needs the template permission (owner or template manager).

import {
applyEdgeChanges,
applyNodeChanges,
Background,
BackgroundVariant,
Controls,
MiniMap,
Panel,
ReactFlow,
useReactFlow,
type Connection,
type EdgeChange,
type FinalConnectionState,
type NodeChange,
} from '@xyflow/react';
import { Archive,ArchiveRestore,ArrowLeft,CircleCheck,Group,LayoutTemplate,ListChecks,MoreHorizontal,Play,Plus,Rocket,RotateCcw,Save,TriangleAlert } from 'lucide-react';
import { useCallback,useEffect,useImperativeHandle,useMemo,useRef,useState,type DragEvent,type Ref } from 'react';
import { Link } from 'react-router-dom';
import { useFeature } from '../../../api/capabilities';
import { isApiError } from '../../../api/client';
import { archiveWorkflow,getWorkflow,updateWorkflow,validateWorkflow } from '../../../api/endpoints';
import type { GraphError,Project,Workflow,WorkflowGraph,WorkflowGroup,WorkflowNodeType } from '../../../api/types';
import { paths } from '../../../app/nav';
import { projectCapabilities } from '../../../auth/permissions';
import { ArchivedChip } from '../../../components/ui/Badges';
import { ConfirmDialog,Dialog } from '../../../components/ui/Dialog';
import { LimitedTextarea } from '../../../components/ui/LimitedText';
import { Menu,type MenuItem } from '../../../components/ui/Menu';
import { ErrorNotice } from '../../../components/ui/States';
import { useToast } from '../../../components/ui/Toast';
import { isTypingTarget } from '../../../hooks/keyboard';
import { useI18n } from '../../../i18n/I18nProvider';
import { useTheme } from '../../../prefs/ThemeProvider';
import { CanvasProvider,type CanvasCtx } from '../canvas/CanvasContext';
import { NODE_TYPES } from '../canvas/NodeCard';
import {
computeFrames,
edgeClass,
flowToGraph,
frameGroupId,
graphToFlow,
GRID,
isFrameId,
type AnyFlowNode,
type FrameNode,
type WfEdge,
type WfNode,
} from '../flow';
import { edgeHighlightClass,nodeHighlightClass,type CanvasHighlight } from '../highlight';
import { nodeDisplayName } from '../labels';
import { useLookups } from '../lookups';
import { compatible,defMap,errorsByNode,GROUP_COLORS,localValidate,newEdgeId,newGroupId,newNodeId,wouldCreateCycle } from '../model';
import { GroupInspector,NodeInspector,ReadOnlyNote,SelectionInspector,ShortcutHelp } from './Inspector';
import { IssuesList } from './IssuesList';
import { DND_MIME,Palette } from './Palette';

const DRAFT_PREFIX = 'partroute.wfdraft.';

interface Draft {
  version: number;
  name: string;
  description: string;
  graph: WorkflowGraph;
  at: string;
}

function keyOf(graph: WorkflowGraph, name: string, description: string): string {
  return JSON.stringify([graph, name, description]);
}

function readDraft(id: string): Draft | null {
  try {
    const raw = localStorage.getItem(DRAFT_PREFIX + id);
    return raw ? (JSON.parse(raw) as Draft) : null;
  } catch {
    return null;
  }
}

function writeDraft(id: string, d: Draft | null): void {
  try {
    if (d) localStorage.setItem(DRAFT_PREFIX + id, JSON.stringify(d));
    else localStorage.removeItem(DRAFT_PREFIX + id);
  } catch {
    /* storage full or private mode: the draft lives in memory only */
  }
}

type ConnProblem = 'self' | 'type' | 'taken' | 'cycle' | 'duplicate' | 'port';

export interface WorkflowEditorProps {
  initial: Workflow;
  project: Project;
  defs: WorkflowNodeType[];
  /** Open the run dialog (called after unsaved changes were saved). */
  onRequestRun?: () => void;
  /** Open the publish dialog (called after unsaved changes were saved). */
  onRequestPublish?: () => void;
  /** Every new server state of the draft (save, reload, archive, discard). */
  onWorkflowChange?: (w: Workflow) => void;
  onDirtyChange?: (dirty: boolean) => void;
  /** Newer server state from outside the editor (policy saved on another tab). */
  external?: Workflow;
  /** Part canvas: the back arrow returns to the runs instead of the template list (null: no back arrow). */
  back?: { label: string; onClick: () => void } | null;
  // ── AI work panel (all optional; other callers keep the behaviour above) ──
  /** A node selected from outside (the panel URL): selected and centred when it changes. */
  selectedNodeId?: string | null;
  /** A node was clicked (null: the empty canvas). */
  onSelectNode?: (nodeId: string | null) => void;
  /** Work-panel target lock; canvas selection itself remains interactive. */
  lockAssistantTarget?: boolean;
  /** Plan / attention highlights on nodes and edges. */
  highlight?: CanvasHighlight;
  /** Narrow frame: the palette opens on request and there is no minimap. */
  compact?: boolean;
  /** Unsaved state and save(), for a frame that saves on the person's behalf (e.g. "save and send"). */
  controllerRef?: Ref<WorkflowEditorController>;
  /** After every successful save (not after a reload, discard or adopted external version). */
  onSaved?: (w: Workflow) => void;
}

export interface WorkflowEditorController {
  dirty: boolean;
  /** Save unsaved edits (resolves with the current workflow when there are none); rejects when nothing was saved. */
  save(): Promise<Workflow>;
}

export function WorkflowEditor({
  initial,
  project,
  defs,
  onRequestRun,
  onRequestPublish,
  onWorkflowChange,
  onDirtyChange,
  external,
  back,
  selectedNodeId,
  onSelectNode,
  lockAssistantTarget = false,
  highlight,
  compact = false,
  controllerRef,
  onSaved,
}: WorkflowEditorProps) {
  const collaborationEnabled = useFeature('collaboration');
  const { t, fmt } = useI18n();
  const toast = useToast();
  const { resolved } = useTheme();
  const rf = useReactFlow<AnyFlowNode, WfEdge>();
  const defsMap = useMemo(() => defMap(defs), [defs]);
  const lookups = useLookups(project.id);
  const caps = projectCapabilities(project);

  const init = useMemo(() => graphToFlow(initial.graph, defsMap), [initial, defsMap]);
  const [wf, setWf] = useState<Workflow>(initial);
  const [nodes, setNodes] = useState<WfNode[]>(init.nodes);
  const [edges, setEdges] = useState<WfEdge[]>(init.edges);
  const [groups, setGroups] = useState<WorkflowGroup[]>(init.groups);
  const [name, setName] = useState(initial.name);
  const [description, setDescription] = useState(initial.description);
  const [savedKey, setSavedKey] = useState(() => keyOf(flowToGraph(init.nodes, init.edges, init.groups), initial.name, initial.description));
  const [serverErrors, setServerErrors] = useState<GraphError[]>(initial.validation);
  const [checkedAt, setCheckedAt] = useState<string | null>(initial.updated_at);
  const [activeGroupId, setActiveGroupId] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [validating, setValidating] = useState(false);
  const [saveError, setSaveError] = useState<unknown>(null);
  const [conflict, setConflict] = useState<{ current?: number } | null>(null);
  const [conflictBusy, setConflictBusy] = useState(false);
  const [archiveOpen, setArchiveOpen] = useState(false);
  const [archiveBusy, setArchiveBusy] = useState(false);
  const [archiveError, setArchiveError] = useState<unknown>(null);
  const [draftOffer, setDraftOffer] = useState<Draft | null>(null);
  const [nameTouched, setNameTouched] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(!compact);

  // Drafts are edited by template managers (owner or template_manager); members with a
  // working role still run published versions.
  const frozenGraph = !collaborationEnabled && wf.graph.nodes.some((node) => node.type === 'business_task');
  const canManage = (wf.can_manage ?? caps.workflow) && caps.workflow && !frozenGraph;
  const readOnly = !canManage || !!wf.archived_at;
  const canRun = caps.workflow && !wf.archived_at && !frozenGraph;
  const wrapRef = useRef<HTMLDivElement>(null);
  const nodesRef = useRef(nodes);
  nodesRef.current = nodes;
  const edgesRef = useRef(edges);
  edgesRef.current = edges;

  const graph = useMemo(() => flowToGraph(nodes, edges, groups), [nodes, edges, groups]);
  const currentKey = useMemo(() => keyOf(graph, name.trim(), description), [graph, name, description]);
  const dirty = currentKey !== savedKey;
  const aiNode = nodes.find((node) => node.selected)?.id;
  const canSaveBeforeSend = !!controllerRef && !readOnly;
  useAssistantPageContext({ projectId: project.id, label: `${wf.name} · v${wf.version}${aiNode ? ` · ${aiNode}` : ''}`,
    object: { type: 'workflow', id: wf.id, version: wf.version },
    target: dirty && !canSaveBeforeSend ? null : { kind: 'workflow', object_id: wf.id, project_id: project.id, version: wf.version, ...(aiNode && (canSaveBeforeSend || wf.graph.nodes.some((node) => node.id === aiNode)) ? { node: { node_id: aiNode } } : {}) },
    unavailable: dirty ? t(canSaveBeforeSend ? 'fc.composer.unsaved' : 'core.context.unsavedWorkflow') : null }, lockAssistantTarget);

  // Keep the template page in sync with the server state and unsaved changes.
  const changeRef = useRef(onWorkflowChange);
  changeRef.current = onWorkflowChange;
  useEffect(() => changeRef.current?.(wf), [wf]);
  const dirtyRef = useRef(onDirtyChange);
  dirtyRef.current = onDirtyChange;
  useEffect(() => dirtyRef.current?.(dirty && !readOnly), [dirty, readOnly]);

  // Structure-only key so dragging does not re-run validation on every frame.
  const structKey = useMemo(() => JSON.stringify([graph.nodes.map((n) => [n.id, n.type, n.params]), graph.edges]), [graph]);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const localErrors = useMemo(() => localValidate(graph, defsMap), [structKey, defsMap]);
  const errors = dirty ? localErrors : serverErrors;
  const edgeTarget = useCallback((id: string) => edgesRef.current.find((e) => e.id === id)?.target, []);
  const errorKey = JSON.stringify(errors);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const errMap = useMemo(() => errorsByNode(errors, edgeTarget), [errorKey, edgeTarget]);
  const connectedInputs = useMemo(() => {
    const m = new Map<string, Set<string>>();
    for (const e of edges) m.set(e.target, new Set([...(m.get(e.target) ?? []), e.targetHandle ?? '']));
    return m;
  }, [edges]);

  const ctx = useMemo<CanvasCtx>(() => ({ defs: defsMap, mode: 'edit', readOnly, errorsByNode: errMap, connectedInputs, lookups }), [defsMap, readOnly, errMap, connectedInputs, lookups]);

  const frames = useMemo(() => computeFrames(groups, nodes, activeGroupId, !readOnly), [groups, nodes, activeGroupId, readOnly]);
  const framesRef = useRef<FrameNode[]>(frames);
  framesRef.current = frames;
  const nodeHl = highlight?.nodes;
  const edgeHl = highlight?.edges;
  const displayNodes = useMemo<AnyFlowNode[]>(
    () => [...frames, ...(nodeHl ? nodes.map((n) => (nodeHl[n.id] ? { ...n, className: nodeHighlightClass(nodeHl[n.id]) } : n)) : nodes)],
    [frames, nodes, nodeHl],
  );
  const displayEdges = useMemo<WfEdge[]>(
    () => (edgeHl ? edges.map((e) => (edgeHl[e.id] ? { ...e, className: `${e.className ?? ''}${edgeHighlightClass(edgeHl[e.id])}` } : e)) : edges),
    [edges, edgeHl],
  );

  // ── drafts ───────────────────────────────────────────────────────────────
  useEffect(() => {
    const d = readDraft(initial.id);
    if (!d) return;
    if (d.version === initial.version && keyOf(d.graph, d.name.trim(), d.description) !== savedKey) setDraftOffer(d);
    else writeDraft(initial.id, null);
    // only on first load
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    // Mirror unsaved edits to localStorage. Drafts are removed explicitly (save, discard,
    // reload); a draft equal to the saved version is ignored on the next load.
    if (readOnly || !dirty) return;
    const h = window.setTimeout(() => writeDraft(wf.id, { version: wf.version, name, description, graph, at: new Date().toISOString() }), 600);
    return () => window.clearTimeout(h);
  }, [dirty, graph, name, description, wf.id, wf.version, readOnly]);

  useEffect(() => {
    if (!dirty || readOnly) return;
    const onBefore = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = '';
    };
    window.addEventListener('beforeunload', onBefore);
    return () => window.removeEventListener('beforeunload', onBefore);
  }, [dirty, readOnly]);

  const applyGraph = useCallback(
    (g: WorkflowGraph) => {
      const f = graphToFlow(g, defsMap);
      setNodes(f.nodes);
      setEdges(f.edges);
      setGroups(f.groups);
      setActiveGroupId(null);
    },
    [defsMap],
  );

  const applyServer = useCallback(
    (w: Workflow) => {
      const f = graphToFlow(w.graph, defsMap);
      setWf(w);
      setNodes(f.nodes);
      setEdges(f.edges);
      setGroups(f.groups);
      setName(w.name);
      setDescription(w.description);
      setSavedKey(keyOf(flowToGraph(f.nodes, f.edges, f.groups), w.name, w.description));
      setServerErrors(w.validation);
      setCheckedAt(new Date().toISOString());
      setActiveGroupId(null);
    },
    [defsMap],
  );

  // A newer draft saved elsewhere (e.g. the input policy on the overview tab): adopt it.
  // With unsaved canvas edits only the version moves on, so the next save does not conflict.
  useEffect(() => {
    if (!external || external.id !== wf.id || external.version <= wf.version) return;
    if (dirty) setWf((w) => ({ ...external, graph: w.graph }));
    else applyServer(external);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [external]);

  // ── save / validate ──────────────────────────────────────────────────────
  const savedRef = useRef(onSaved);
  savedRef.current = onSaved;
  const save = useCallback(
    async (force = false): Promise<Workflow | null> => {
      if (readOnly) return null;
      const trimmed = name.trim();
      if (!trimmed) {
        setNameTouched(true);
        return null;
      }
      setSaving(true);
      setSaveError(null);
      const sentGraph = graph;
      const sentKey = keyOf(sentGraph, trimmed, description);
      try {
        const res = await updateWorkflow(wf.id, { name: trimmed, description, graph: sentGraph, expected_version: force ? undefined : wf.version });
        setWf(res);
        setServerErrors(res.validation);
        setCheckedAt(new Date().toISOString());
        setSavedKey(sentKey);
        setDraftOffer(null);
        writeDraft(wf.id, null);
        toast(t('wf.savedToast', { version: res.version }));
        savedRef.current?.(res);
        return res;
      } catch (e) {
        if (isApiError(e) && e.code === 'version_conflict') setConflict({ current: (e.details as { current_version?: number }).current_version });
        else setSaveError(e);
        return null;
      } finally {
        setSaving(false);
      }
    },
    [readOnly, name, graph, description, wf.id, wf.version, toast, t],
  );

  const validate = async () => {
    if (dirty && !readOnly) {
      await save();
      return;
    }
    setValidating(true);
    setSaveError(null);
    try {
      const res = await validateWorkflow(wf.id);
      setServerErrors(res.errors);
      setCheckedAt(new Date().toISOString());
      toast(res.errors.length ? t('wf.validatedIssues', { n: res.errors.length }) : t('wf.validatedOk'), { tone: res.errors.length ? 'info' : 'ok' });
    } catch (e) {
      setSaveError(e);
    } finally {
      setValidating(false);
    }
  };

  const resolveConflict = async (mode: 'reload' | 'overwrite') => {
    setConflictBusy(true);
    try {
      if (mode === 'reload') {
        const latest = await getWorkflow(wf.id);
        applyServer(latest);
        writeDraft(wf.id, null);
        toast(t('wf.conflict.reloaded', { version: latest.version }));
        setConflict(null);
      } else {
        setConflict(null);
        await save(true);
      }
    } catch (e) {
      setSaveError(e);
      setConflict(null);
    } finally {
      setConflictBusy(false);
    }
  };

  const doArchive = async () => {
    setArchiveBusy(true);
    setArchiveError(null);
    try {
      const res = await archiveWorkflow(wf.id, !!wf.archived_at);
      setWf((w) => ({ ...w, archived_at: res.archived_at, version: res.version }));
      toast(res.archived_at ? t('wf.archivedToast', { name: res.name }) : t('wf.restoredToast', { name: res.name }));
      setArchiveOpen(false);
    } catch (e) {
      setArchiveError(e);
    } finally {
      setArchiveBusy(false);
    }
  };

  // ── graph editing ────────────────────────────────────────────────────────
  const connectionProblem = useCallback(
    (c: { source: string | null; target: string | null; sourceHandle?: string | null; targetHandle?: string | null }): ConnProblem | null => {
      const { source, target } = c;
      if (!source || !target) return 'port';
      if (source === target) return 'self';
      const ns = nodesRef.current;
      const es = edgesRef.current;
      const sn = ns.find((n) => n.id === source);
      const tn = ns.find((n) => n.id === target);
      const sp = sn && defsMap.get(sn.data.nodeType)?.outputs.find((p) => p.name === c.sourceHandle);
      const tp = tn && defsMap.get(tn.data.nodeType)?.inputs.find((p) => p.name === c.targetHandle);
      if (!sp || !tp) return 'port';
      if (!compatible(sp.type, tp.type)) return 'type';
      if (es.some((e) => e.source === source && e.target === target && e.sourceHandle === c.sourceHandle && e.targetHandle === c.targetHandle)) return 'duplicate';
      if (!tp.multiple && es.some((e) => e.target === target && e.targetHandle === c.targetHandle)) return 'taken';
      if (wouldCreateCycle(es, source, target)) return 'cycle';
      return null;
    },
    [defsMap],
  );

  const isValidConnection = useCallback((c: Connection | WfEdge) => !readOnly && connectionProblem(c) === null, [connectionProblem, readOnly]);

  const onConnect = useCallback(
    (c: Connection) => {
      if (readOnly || connectionProblem(c)) return;
      setEdges((es) => {
        const sn = nodesRef.current.find((n) => n.id === c.source);
        const tn = nodesRef.current.find((n) => n.id === c.target);
        const st = sn && defsMap.get(sn.data.nodeType)?.outputs.find((p) => p.name === c.sourceHandle)?.type;
        const tt = tn && defsMap.get(tn.data.nodeType)?.inputs.find((p) => p.name === c.targetHandle)?.type;
        const portType = st && st !== 'any' ? st : (tt ?? 'any');
        const id = newEdgeId(c.source, c.sourceHandle ?? '', c.target, c.targetHandle ?? '', new Set(es.map((e) => e.id)));
        return [...es, { id, source: c.source, sourceHandle: c.sourceHandle, target: c.target, targetHandle: c.targetHandle, data: { portType }, className: edgeClass(portType) }];
      });
    },
    [connectionProblem, defsMap, readOnly],
  );

  const onConnectEnd = useCallback(
    (_e: MouseEvent | TouchEvent, s: FinalConnectionState) => {
      if (readOnly || s.isValid !== false || !s.fromHandle || !s.toHandle) return;
      const fromSource = s.fromHandle.type === 'source';
      const conn = fromSource
        ? { source: s.fromHandle.nodeId, sourceHandle: s.fromHandle.id ?? null, target: s.toHandle.nodeId, targetHandle: s.toHandle.id ?? null }
        : { source: s.toHandle.nodeId, sourceHandle: s.toHandle.id ?? null, target: s.fromHandle.nodeId, targetHandle: s.fromHandle.id ?? null };
      const p = connectionProblem(conn);
      if (p) toast(t(`wf.conn.${p}`), { tone: 'info' });
    },
    [connectionProblem, readOnly, toast, t],
  );

  const onNodesChange = useCallback((changes: NodeChange<AnyFlowNode>[]) => {
    const real: NodeChange<WfNode>[] = [];
    const deltas: { members: Set<string>; dx: number; dy: number }[] = [];
    for (const c of changes) {
      const id = c.type === 'add' || c.type === 'replace' ? c.item.id : c.id;
      if (isFrameId(id)) {
        if (c.type === 'position' && c.position) {
          const f = framesRef.current.find((x) => x.id === id);
          if (f) {
            const dx = c.position.x - f.position.x;
            const dy = c.position.y - f.position.y;
            if (dx || dy) deltas.push({ members: new Set(f.data.group.node_ids), dx, dy });
            framesRef.current = framesRef.current.map((x) => (x.id === id ? { ...x, position: c.position! } : x));
          }
        }
        continue;
      }
      real.push(c as NodeChange<WfNode>);
    }
    if (deltas.length) {
      setNodes((ns) =>
        ns.map((n) => {
          let { x, y } = n.position;
          for (const d of deltas) {
            if (d.members.has(n.id)) {
              x += d.dx;
              y += d.dy;
            }
          }
          return x === n.position.x && y === n.position.y ? n : { ...n, position: { x, y } };
        }),
      );
    }
    if (real.length) setNodes((ns) => applyNodeChanges(real, ns));
  }, []);

  const onEdgesChange = useCallback((changes: EdgeChange<WfEdge>[]) => setEdges((es) => applyEdgeChanges(changes, es)), []);

  const onNodesDelete = useCallback((deleted: AnyFlowNode[]) => {
    const ids = new Set(deleted.map((n) => n.id));
    setGroups((gs) => gs.map((g) => ({ ...g, node_ids: g.node_ids.filter((id) => !ids.has(id)) })).filter((g) => g.node_ids.length > 0));
  }, []);

  const addNode = useCallback(
    (type: string, at?: { x: number; y: number }) => {
      if (readOnly || !defsMap.has(type) || defsMap.get(type)?.available === false || (type === 'business_task' && !collaborationEnabled)) return;
      let pos = at;
      if (!pos) {
        // Click-to-add: start at the viewport centre and step to the first free spot.
        const r = wrapRef.current?.getBoundingClientRect();
        const center = r ? rf.screenToFlowPosition({ x: r.left + r.width / 2, y: r.top + r.height / 2 }) : { x: 0, y: 0 };
        const boxes = nodesRef.current.map((n) => ({ x: n.position.x, y: n.position.y, w: n.measured?.width ?? 248, h: n.measured?.height ?? 150 }));
        const free = (x: number, y: number) => boxes.every((b) => x + 248 + GRID <= b.x || x >= b.x + b.w + GRID || y + 150 + GRID <= b.y || y >= b.y + b.h + GRID);
        const start = { x: center.x - 124, y: center.y - 75 };
        pos = start;
        search: for (let col = 0; col < 8; col++) {
          for (let row = 0; row < 5; row++) {
            const cand = { x: start.x + col * (248 + GRID * 3), y: start.y + row * (150 + GRID * 2) };
            if (free(cand.x, cand.y)) {
              pos = cand;
              break search;
            }
          }
        }
      }
      const snapped = { x: Math.round(pos.x / GRID) * GRID, y: Math.round(pos.y / GRID) * GRID };
      setNodes((ns) => {
        const id = newNodeId(type, new Set(ns.map((n) => n.id)));
        return [...ns.map((n) => (n.selected ? { ...n, selected: false } : n)), { id, type: 'wf', position: snapped, data: { nodeType: type, params: {}, label: null }, selected: true }];
      });
      setActiveGroupId(null);
    },
    [readOnly, rf, defsMap, collaborationEnabled],
  );

  const onDrop = useCallback(
    (e: DragEvent) => {
      const type = e.dataTransfer.getData(DND_MIME);
      if (!type || readOnly) return;
      e.preventDefault();
      addNode(type, rf.screenToFlowPosition({ x: e.clientX - 110, y: e.clientY - 24 }));
    },
    [addNode, readOnly, rf],
  );

  const selectedNodes = nodes.filter((n) => n.selected);
  const activeGroup = activeGroupId ? groups.find((g) => g.id === activeGroupId) ?? null : null;

  const groupsRef = useRef(groups);
  groupsRef.current = groups;
  const groupSelection = useCallback(() => {
    if (readOnly) return;
    const ids = nodesRef.current.filter((n) => n.selected).map((n) => n.id);
    if (!ids.length) return;
    const gs = groupsRef.current;
    const id = newGroupId(new Set(gs.map((g) => g.id)));
    const color = GROUP_COLORS[gs.length % GROUP_COLORS.length];
    const pruned = gs.map((g) => ({ ...g, node_ids: g.node_ids.filter((x) => !ids.includes(x)) })).filter((g) => g.node_ids.length);
    setGroups([...pruned, { id, label: t('wf.group.defaultLabel', { n: gs.length + 1 }), node_ids: ids, color }]);
    setNodes((ns) => ns.map((n) => (n.selected ? { ...n, selected: false } : n)));
    setActiveGroupId(id);
  }, [readOnly, t]);

  const updateGroup = (id: string, patch: Partial<WorkflowGroup>) => setGroups((gs) => gs.map((g) => (g.id === id ? { ...g, ...patch } : g)));

  const setParam = (id: string, pname: string, value: unknown) =>
    setNodes((ns) => ns.map((n) => (n.id === id ? { ...n, data: { ...n.data, params: { ...n.data.params, [pname]: value } } } : n)));
  const setLabel = (id: string, label: string) => setNodes((ns) => ns.map((n) => (n.id === id ? { ...n, data: { ...n.data, label } } : n)));

  const focusNode = useCallback(
    (id: string) => {
      const n = nodesRef.current.find((x) => x.id === id);
      if (!n) return;
      setActiveGroupId(null);
      setNodes((ns) => ns.map((x) => (x.id === id ? (x.selected ? x : { ...x, selected: true }) : x.selected ? { ...x, selected: false } : x)));
      const w = n.measured?.width ?? 248;
      const h = n.measured?.height ?? 140;
      void rf.setCenter(n.position.x + w / 2, n.position.y + h / 2, { zoom: Math.max(rf.getZoom(), 0.9), duration: 300 });
    },
    [rf],
  );

  // keyboard: save, group
  const saveRef = useRef(save);
  saveRef.current = save;

  // ── outside frame (AI work panel): controller and selection ─────────────
  const editable = dirty && !readOnly;
  useImperativeHandle(
    controllerRef,
    () => ({
      dirty: editable,
      save: async () => {
        if (!editable) return wf;
        const saved = await saveRef.current();
        if (!saved) throw new Error(t('wp.editor.notSaved'));
        return saved;
      },
    }),
    [editable, wf, t],
  );

  const selectRef = useRef(onSelectNode);
  selectRef.current = onSelectNode;
  const firstSelect = useRef(true);
  useEffect(() => {
    if (selectedNodeId === undefined) return;
    const first = firstSelect.current;
    firstSelect.current = false;
    const ns = nodesRef.current;
    if (selectedNodeId === null) {
      if (ns.some((n) => n.selected)) setNodes((xs) => xs.map((n) => (n.selected ? { ...n, selected: false } : n)));
      return;
    }
    // Already part of the selection (the person clicked it): keep a multi-selection as it is.
    if (ns.find((n) => n.id === selectedNodeId)?.selected) return;
    if (!first) {
      focusNode(selectedNodeId);
      return;
    }
    // Opened with a node in the URL: centre it after the initial fit.
    const h = window.setTimeout(() => focusNode(selectedNodeId), 320);
    return () => window.clearTimeout(h);
  }, [selectedNodeId, focusNode]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const mod = e.metaKey || e.ctrlKey;
      if (!mod) return;
      if (e.key === 's' || e.key === 'S') {
        e.preventDefault();
        if (!readOnly) void saveRef.current();
      } else if ((e.key === 'g' || e.key === 'G') && !isTypingTarget(e.target)) {
        e.preventDefault();
        groupSelection();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [readOnly, groupSelection]);

  const nodeInfo = useCallback(
    (id: string) => {
      const n = nodesRef.current.find((x) => x.id === id);
      return n ? { type: n.data.nodeType, name: nodeDisplayName(t, { type: n.data.nodeType, label: n.data.label }) } : undefined;
    },
    [t],
  );

  const saveThen = async (next?: () => void) => {
    if (!next) return;
    if (dirty && !readOnly) {
      const saved = await save();
      if (!saved) return;
    }
    next();
  };

  const menuItems: MenuItem[] = [];
  if (canManage) {
    menuItems.push(
      wf.archived_at
        ? { id: 'restore', label: t('common.restore'), icon: <ArchiveRestore />, onSelect: () => setArchiveOpen(true) }
        : { id: 'archive', label: t('common.archive'), icon: <Archive />, danger: true, onSelect: () => setArchiveOpen(true) },
    );
  }
  if (dirty && !readOnly) {
    menuItems.push({
      id: 'discard',
      label: t('wf.discard'),
      icon: <RotateCcw />,
      separator: menuItems.length > 0,
      onSelect: () => {
        writeDraft(wf.id, null);
        applyServer({ ...wf, validation: serverErrors });
      },
    });
  }

  // ── inspector ────────────────────────────────────────────────────────────
  let inspector;
  if (activeGroup) {
    inspector = (
      <GroupInspector
        group={activeGroup}
        readOnly={readOnly}
        memberNames={activeGroup.node_ids.map((id) => ({ id, name: nodeInfo(id)?.name ?? id }))}
        selectedCount={selectedNodes.filter((n) => !activeGroup.node_ids.includes(n.id)).length}
        onChange={(p) => updateGroup(activeGroup.id, p)}
        onAddSelected={() => {
          const add = selectedNodes.map((n) => n.id).filter((id) => !activeGroup.node_ids.includes(id));
          setGroups((gs) =>
            gs
              .map((g) => (g.id === activeGroup.id ? { ...g, node_ids: [...g.node_ids, ...add] } : { ...g, node_ids: g.node_ids.filter((x) => !add.includes(x)) }))
              .filter((g) => g.node_ids.length),
          );
        }}
        onRemoveMember={(id) => {
          setGroups((gs) => gs.map((g) => (g.id === activeGroup.id ? { ...g, node_ids: g.node_ids.filter((x) => x !== id) } : g)).filter((g) => g.node_ids.length));
        }}
        onUngroup={() => {
          setGroups((gs) => gs.filter((g) => g.id !== activeGroup.id));
          setActiveGroupId(null);
        }}
      />
    );
  } else if (selectedNodes.length === 1) {
    const n = selectedNodes[0];
    inspector = (
      <NodeInspector
        key={n.id}
        nodeId={n.id}
        def={defsMap.get(n.data.nodeType)}
        params={n.data.params}
        label={n.data.label}
        errors={errMap.get(n.id) ?? []}
        env={{ projectId: project.id, nodeType: n.data.nodeType, readOnly, lookups, connectedInputs: connectedInputs.get(n.id) ?? new Set() }}
        onParam={(p, v) => setParam(n.id, p, v)}
        onLabel={(l) => setLabel(n.id, l)}
        onDelete={() => void rf.deleteElements({ nodes: [{ id: n.id }] })}
      />
    );
  } else if (selectedNodes.length > 1) {
    inspector = (
      <SelectionInspector count={selectedNodes.length} readOnly={readOnly} onGroup={groupSelection} onDelete={() => void rf.deleteElements({ nodes: selectedNodes.map((n) => ({ id: n.id })) })} />
    );
  } else {
    inspector = (
      <div className="wf-insp-section stack">
        <div className="field">
          <label htmlFor="wf-desc">{t('wf.field.description')}</label>
          <LimitedTextarea id="wf-desc" className="input" rows={3} limit="description" value={description} readOnly={readOnly} placeholder={t('wf.field.descriptionPlaceholder')} onChange={(e) => setDescription(e.target.value)} />
        </div>
        {wf.template_key && (
          <p className="faint small row">
            <LayoutTemplate aria-hidden className="inline-ico" /> {t('wf.fromTemplate', { key: wf.template_key })}
          </p>
        )}
        <p className="muted small">{t('wf.inspector.empty')}</p>
        <ShortcutHelp />
      </div>
    );
  }

  const nameError = nameTouched && !name.trim() ? t('wf.field.nameRequired') : null;

  return (
    <div className={`wf-editor ${compact ? 'compact' : ''}`}>
      <header className="wf-editor-bar">
        {back === null ? null : back ? (
          <button type="button" className="btn ghost icon sm" onClick={back.onClick} aria-label={back.label} title={back.label}>
            <ArrowLeft />
          </button>
        ) : (
          <Link to={paths.project(project.id, 'workflows')} className="btn ghost icon sm" aria-label={t('wf.backToList')} title={t('wf.backToList')}>
            <ArrowLeft />
          </Link>
        )}
        <div className="wf-title-edit">
          <input
            className={`wf-name-input ${nameError ? 'is-invalid' : ''}`}
            value={name}
            maxLength={200}
            readOnly={readOnly}
            aria-label={t('wf.field.name')}
            aria-invalid={!!nameError || undefined}
            onBlur={() => setNameTouched(true)}
            onChange={(e) => setName(e.target.value)}
          />
          <span className="wf-bar-meta">
            <span className="mono faint" title={t('wf.versionTitle')}>
              v{wf.version}
            </span>
            {wf.archived_at && <ArchivedChip />}
            {readOnly ? null : dirty ? (
              <span className="wf-dirty" role="status">
                <span className="dot" aria-hidden /> {t('wf.unsaved')}
              </span>
            ) : (
              <span className="faint small" role="status">
                {t('wf.savedAt', { time: fmt.relative(wf.updated_at) })}
              </span>
            )}
            <button type="button" className={`chip ${errors.length ? 'err' : 'ok'} wf-issue-chip`} onClick={() => setNodes((ns) => ns.map((n) => (n.selected ? { ...n, selected: false } : n)))} title={t('wf.issues.show')}>
              {errors.length ? <TriangleAlert aria-hidden /> : <CircleCheck aria-hidden />}
              {errors.length ? t('wf.issuesCount', { n: errors.length }) : t('wf.issues.none')}
            </button>
          </span>
        </div>
        <span className="grow" />
        <div className="wf-bar-actions">
          {compact && !readOnly && (
            <button type="button" className={`btn sm ${paletteOpen ? 'active' : ''}`} aria-pressed={paletteOpen} onClick={() => setPaletteOpen((v) => !v)}>
              <Plus /> {t('wf.palette.title')}
            </button>
          )}
          <button type="button" className="btn sm" onClick={() => void validate()} disabled={validating || saving}>
            <ListChecks /> {validating ? t('common.working') : dirty && !readOnly ? t('wf.saveAndValidate') : t('wf.validate')}
          </button>
          {!readOnly && (
            <button type="button" className={`btn sm ${dirty ? 'primary' : ''}`} onClick={() => void save()} disabled={saving || !dirty} title={t('wf.keys.saveKeys')}>
              <Save /> {saving ? t('common.saving') : t('common.save')}
            </button>
          )}
          {canManage && onRequestPublish && !wf.archived_at && (
            <button type="button" className="btn sm" onClick={() => void saveThen(onRequestPublish)} disabled={saving}>
              <Rocket /> {dirty ? t('tpl.editor.saveAndPublish') : t('tpl.publish.action')}
            </button>
          )}
          {onRequestRun && (
            <button
              type="button"
              className="btn sm primary wf-run-btn"
              onClick={() => void saveThen(onRequestRun)}
              disabled={!canRun || saving}
              title={!canRun ? (wf.archived_at ? t('wf.readonly.archived') : t('wf.readonly.role')) : undefined}
            >
              <Play /> {dirty && !readOnly ? t('tpl.editor.saveAndRun') : t('wf.run')}
            </button>
          )}
          {menuItems.length > 0 && <Menu label={t('common.moreActions')} trigger={<MoreHorizontal />} items={menuItems} />}
        </div>
      </header>
      {nameError && <p className="field-error wf-bar-error">{nameError}</p>}
      {(saveError || draftOffer || readOnly) && (
        <div className="wf-editor-notices">
          {readOnly && (frozenGraph ? <p className="callout" role="status">{t('features.historyOnly')}</p> : <ReadOnlyNote reason={wf.archived_at ? 'archived' : 'role'} />)}
          <ErrorNotice error={saveError} />
          {draftOffer && (
            <div className="callout info slim">
              <RotateCcw aria-hidden />
              <span className="grow">{t('wf.draft.found', { time: fmt.dateTime(draftOffer.at) })}</span>
              <button
                type="button"
                className="btn sm"
                onClick={() => {
                  applyGraph(draftOffer.graph);
                  setName(draftOffer.name);
                  setDescription(draftOffer.description);
                  setDraftOffer(null);
                }}
              >
                {t('wf.draft.restore')}
              </button>
              <button
                type="button"
                className="btn ghost sm"
                onClick={() => {
                  writeDraft(wf.id, null);
                  setDraftOffer(null);
                }}
              >
                {t('wf.draft.discard')}
              </button>
            </div>
          )}
        </div>
      )}
      <div className="wf-editor-body">
        {paletteOpen && (
          <Palette
            defs={defs}
            disabled={readOnly}
            onAdd={(type) => {
              addNode(type);
              if (compact) setPaletteOpen(false);
            }}
          />
        )}
        <div className="wf-canvas" ref={wrapRef} onDragOver={(e) => {
          if (readOnly) return;
          e.preventDefault();
          e.dataTransfer.dropEffect = 'copy';
        }} onDrop={onDrop}>
          <CanvasProvider value={ctx}>
            <ReactFlow<AnyFlowNode, WfEdge>
              nodes={displayNodes}
              edges={displayEdges}
              nodeTypes={NODE_TYPES}
              onNodesChange={onNodesChange}
              onEdgesChange={onEdgesChange}
              onNodesDelete={onNodesDelete}
              onConnect={onConnect}
              onConnectEnd={onConnectEnd}
              isValidConnection={isValidConnection}
              onNodeClick={(_e, n) => {
                setActiveGroupId(isFrameId(n.id) ? frameGroupId(n.id) : null);
                if (!isFrameId(n.id)) selectRef.current?.(n.id);
              }}
              onPaneClick={() => {
                setActiveGroupId(null);
                selectRef.current?.(null);
              }}
              snapToGrid
              snapGrid={[GRID, GRID]}
              fitView
              fitViewOptions={{ padding: 0.15, maxZoom: 1 }}
              minZoom={0.15}
              maxZoom={2}
              deleteKeyCode={readOnly ? null : ['Backspace', 'Delete']}
              nodesDraggable={!readOnly}
              nodesConnectable={!readOnly}
              colorMode={resolved}
              defaultEdgeOptions={{ interactionWidth: 18 }}
            >
              <Background variant={BackgroundVariant.Dots} gap={GRID} size={1.2} />
              <Controls showInteractive={false} />
              {!compact && (
                <MiniMap pannable zoomable style={{ width: 168, height: 112 }} nodeColor={(n) => (n.type === 'frame' ? 'transparent' : `var(--wf-cat-${defsMap.get((n as WfNode).data.nodeType)?.category ?? 'unknown'})`)} nodeStrokeWidth={0} ariaLabel={t('wf.minimap')} />
              )}
              {selectedNodes.length > 1 && !readOnly && (
                <Panel position="top-center" className="wf-sel-panel">
                  <span className="small">{t('wf.selection.count', { n: selectedNodes.length })}</span>
                  <button type="button" className="btn sm" onClick={groupSelection}>
                    <Group /> {t('wf.group.create')}
                  </button>
                </Panel>
              )}
              {nodes.length === 0 && (
                <Panel position="top-center" className="wf-empty-panel">
                  <span>{readOnly ? t('wf.canvas.emptyReadOnly') : t('wf.canvas.empty')}</span>
                </Panel>
              )}
            </ReactFlow>
          </CanvasProvider>
        </div>
        <aside className="wf-inspector" aria-label={t('wf.inspector.title')}>
          <div className="wf-insp-scroll">{inspector}</div>
          <IssuesList errors={errors} source={dirty ? 'local' : 'server'} checkedAt={checkedAt} nodeInfo={nodeInfo} edgeTarget={edgeTarget} onFocus={focusNode} />
        </aside>
      </div>

      {conflict && (
        <Dialog
          title={t('wf.conflict.title')}
          size="sm"
          role="alertdialog"
          busy={conflictBusy}
          onClose={() => setConflict(null)}
          footer={
            <>
              <button type="button" className="btn" onClick={() => setConflict(null)} disabled={conflictBusy}>
                {t('wf.conflict.keepEditing')}
              </button>
              <button type="button" className="btn danger" onClick={() => void resolveConflict('overwrite')} disabled={conflictBusy}>
                {t('wf.conflict.overwrite')}
              </button>
              <button type="button" className="btn primary" onClick={() => void resolveConflict('reload')} disabled={conflictBusy} data-autofocus>
                {t('wf.conflict.reload')}
              </button>
            </>
          }
        >
          <p className="muted">{t('wf.conflict.body', { mine: wf.version, theirs: conflict.current ?? '?' })}</p>
        </Dialog>
      )}
      {archiveOpen && (
        <ConfirmDialog
          title={wf.archived_at ? t('wf.restore.title', { name: wf.name }) : t('wf.archive.title', { name: wf.name })}
          body={wf.archived_at ? t('wf.restore.body') : t('wf.archive.body')}
          confirmLabel={wf.archived_at ? t('common.restore') : t('common.archive')}
          danger={!wf.archived_at}
          busy={archiveBusy}
          error={<ErrorNotice error={archiveError} />}
          onConfirm={() => void doArchive()}
          onClose={() => {
            setArchiveOpen(false);
            setArchiveError(null);
          }}
        />
      )}
    </div>
  );
}


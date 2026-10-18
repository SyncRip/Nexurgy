// Read-only canvas of a run's graph snapshot, coloured by node status.

import { Background, BackgroundVariant, Controls, MiniMap, Panel, ReactFlow, ReactFlowProvider, useReactFlow, useStore, type NodeChange } from '@xyflow/react';
import { Maximize, Minus, Plus } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import type { WorkflowRun } from '../../../api/types';
import { useI18n } from '../../../i18n/I18nProvider';
import type { Lookups } from '../lookups';
import { useTheme } from '../../../prefs/ThemeProvider';
import { CanvasProvider, type CanvasCtx } from '../canvas/CanvasContext';
import { NODE_TYPES } from '../canvas/NodeCard';
import { computeFrames, edgeClass, graphToFlow, isFrameId, type AnyFlowNode, type WfEdge } from '../flow';
import { edgeHighlightClass, nodeHighlightClass, type CanvasHighlight } from '../highlight';
import { layoutIfOverlapping, type DefMap } from '../model';

interface Props {
  run: WorkflowRun;
  defs: DefMap;
  lookups: Lookups;
  selected: string | null;
  onSelect: (id: string | null) => void;
  /** Part canvas: a floating toolbar (its own buttons + zoom) instead of the corner controls and minimap. */
  tools?: ReactNode;
  /** Part canvas: the card's "add to AI context" tool. */
  /** AI work panel: attention / plan highlights on nodes and edges. */
  highlight?: CanvasHighlight;
  /** Narrow frame (the work panel): no minimap. */
  compact?: boolean;
}

/** Part canvas fit (prototype fitCanvas): never below 0.6 so cards stay readable; a wider graph starts at the left and pans. */
export function canvasFit(b: { x: number; y: number; width: number; height: number }, w: number, h: number): { x: number; y: number; zoom: number } {
  const zoom = Math.max(0.6, Math.min(1, (w - 40) / (b.width + 60), (h - 110) / Math.max(b.height, 1)));
  const x = b.width * zoom <= w - 40 ? (w - b.width * zoom) / 2 - b.x * zoom : 24 - b.x * zoom;
  const contentH = b.height * zoom;
  const y = contentH > h - 90 ? 64 - b.y * zoom : Math.max(64, (h - contentH) / 2) - b.y * zoom;
  return { x, y, zoom };
}

function useCanvasFit() {
  const rf = useReactFlow<AnyFlowNode, WfEdge>();
  const width = useStore((st) => st.width);
  const height = useStore((st) => st.height);
  return useCallback(
    (duration = 0) => {
      const real = rf.getNodes().filter((n) => !isFrameId(n.id));
      if (!real.length || !width || !height) return false;
      void rf.setViewport(canvasFit(rf.getNodesBounds(real), width, height), { duration });
      return true;
    },
    [rf, width, height],
  );
}

/** Zoom out · fit · zoom in, styled as one toolbar group. */
function ZoomGroup({ onFit }: { onFit: () => void }) {
  const { t } = useI18n();
  const rf = useReactFlow();
  return (
    <div className="cv-grp">
      <button type="button" onClick={() => void rf.zoomOut({ duration: 150 })} aria-label={t('cv.tools.zoomOut')} title={t('cv.tools.zoomOut')}>
        <Minus aria-hidden />
      </button>
      <button type="button" onClick={onFit} aria-label={t('cv.tools.fit')} title={t('cv.tools.fit')}>
        <Maximize aria-hidden />
      </button>
      <button type="button" onClick={() => void rf.zoomIn({ duration: 150 })} aria-label={t('cv.tools.zoomIn')} title={t('cv.tools.zoomIn')}>
        <Plus aria-hidden />
      </button>
    </div>
  );
}

function Inner({ run, defs, lookups, selected, onSelect, tools, highlight, compact }: Props) {
  const { t } = useI18n();
  const { resolved } = useTheme();
  const rf = useReactFlow<AnyFlowNode, WfEdge>();
  const graph = useMemo(() => layoutIfOverlapping(run.graph), [run.graph]);
  const base = useMemo(() => graphToFlow(graph, defs), [graph, defs]);
  const nodeRuns = useMemo(() => new Map(run.nodes.map((n) => [n.node_id, n])), [run.nodes]);
  const status = (id: string) => nodeRuns.get(id)?.status;

  // Measured sizes (kept so group frames fit their members).
  const [dims, setDims] = useState<Record<string, { width: number; height: number }>>({});
  const onNodesChange = useCallback((changes: NodeChange<AnyFlowNode>[]) => {
    const upd: Record<string, { width: number; height: number }> = {};
    for (const c of changes) if (c.type === 'dimensions' && c.dimensions && !isFrameId(c.id)) upd[c.id] = c.dimensions;
    if (Object.keys(upd).length) setDims((d) => ({ ...d, ...upd }));
  }, []);

  const nodeHl = highlight?.nodes;
  const edgeHl = highlight?.edges;
  const nodes = useMemo<AnyFlowNode[]>(() => {
    const real = base.nodes.map((n) => ({ ...n, selected: n.id === selected, draggable: false, connectable: false, measured: dims[n.id], ...(nodeHl?.[n.id] ? { className: nodeHighlightClass(nodeHl[n.id]) } : {}) }));
    return [...computeFrames(base.groups, real, null, false), ...real];
  }, [base, selected, dims, nodeHl]);

  const edges = useMemo<WfEdge[]>(
    () =>
      base.edges.map((e) => {
        const s = status(e.source);
        const tgt = status(e.target);
        const extra = [s === 'succeeded' ? 'st-done' : '', tgt === 'running' ? 'st-active' : '', s === 'failed' || tgt === 'skipped' ? 'st-broken' : ''].filter(Boolean).join(' ');
        return { ...e, animated: tgt === 'running', className: `${edgeClass(e.data?.portType ?? 'any', extra)}${edgeHighlightClass(edgeHl?.[e.id])}`, selectable: false };
      }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [base, nodeRuns, edgeHl],
  );

  const connectedInputs = useMemo(() => {
    const m = new Map<string, Set<string>>();
    for (const e of graph.edges) m.set(e.target, (m.get(e.target) ?? new Set()).add(e.target_port));
    return m;
  }, [graph.edges]);
  // Part canvas: fit once the cards are measured (and again when the pane gets its size).
  const fit = useCanvasFit();
  // Every card measured (useNodesInitialized never settles with the group frames).
  const ready = base.nodes.length > 0 && base.nodes.every((n) => dims[n.id]);
  const fitted = useRef(false);
  useEffect(() => {
    if (tools === undefined || !ready || fitted.current) return;
    if (fit()) fitted.current = true;
  }, [tools, ready, fit]);

  const ctx = useMemo<CanvasCtx>(
    () => ({
      defs,
      mode: 'run',
      readOnly: true,
      errorsByNode: new Map(),
      connectedInputs,
      nodeRuns,
      lookups,
      onOpenNode: onSelect,
    }),
    [defs, connectedInputs, nodeRuns, lookups, onSelect],
  );

  useEffect(() => {
    if (!selected) return;
    const n = base.nodes.find((x) => x.id === selected);
    if (!n) return;
    const internal = rf.getInternalNode(selected);
    const w = internal?.measured?.width ?? 248;
    const h = internal?.measured?.height ?? 140;
    void rf.setCenter(n.position.x + w / 2, n.position.y + h / 2, { zoom: Math.max(rf.getZoom(), 0.8), duration: 300 });
  }, [selected, base, rf]);

  return (
    <CanvasProvider value={ctx}>
      <ReactFlow<AnyFlowNode, WfEdge>
        nodes={nodes}
        edges={edges}
        nodeTypes={NODE_TYPES}
        onNodesChange={onNodesChange}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable
        deleteKeyCode={null}
        onNodeClick={(_e, n) => !isFrameId(n.id) && onSelect(n.id)}
        onPaneClick={() => onSelect(null)}
        fitView={tools === undefined}
        fitViewOptions={{ padding: 0.15, maxZoom: 1 }}
        minZoom={0.15}
        maxZoom={2}
        colorMode={resolved}
        proOptions={{ hideAttribution: false }}
      >
        <Background variant={BackgroundVariant.Dots} gap={16} size={1.2} />
        {tools !== undefined ? (
          <Panel position="top-left" className="cv-tools">
            {tools}
            <ZoomGroup onFit={() => fit(200)} />
          </Panel>
        ) : (
          <>
            <Controls showInteractive={false} />
            {!compact && <MiniMap pannable zoomable style={{ width: 168, height: 112 }} nodeClassName={(n) => (n.type === 'frame' ? 'mm-frame' : `mm-ns-${status(n.id) ?? 'pending'}`)} nodeStrokeWidth={0} ariaLabel={t('wf.minimap')} />}
          </>
        )}
      </ReactFlow>
    </CanvasProvider>
  );
}

export function RunCanvas(props: Props) {
  return (
    <ReactFlowProvider>
      <Inner {...props} />
    </ReactFlowProvider>
  );
}

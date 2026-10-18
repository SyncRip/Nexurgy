// Read-only React Flow canvas of a collaboration run (UI02): one custom
// `business` node per business task, laid out by dependency depth, edges from
// the frozen graph snapshot. Loaded lazily by the run page (default export) so
// the list view and phones never download React Flow until the graph is shown.

import '@xyflow/react/dist/style.css';
import '../workflow/workflow.css';
import { Background, BackgroundVariant, Controls, Handle, MarkerType, MiniMap, Position, ReactFlow, ReactFlowProvider, useReactFlow, type Edge, type Node, type NodeProps } from '@xyflow/react';
import { memo, useEffect, useMemo } from 'react';
import type { NodeTaskOut } from '../../api/collaboration';
import type { WorkflowGraph } from '../../api/types';
import { useI18n } from '../../i18n/I18nProvider';
import { useTheme } from '../../prefs/ThemeProvider';
import { depths } from '../workflow/model';
import { NodeTaskCard } from './NodeTaskCard';

const COL_W = 320;
const ROW_H = 300;
const CARD_W = 252;
const CARD_H = 250;

export interface BusinessNodeData extends Record<string, unknown> {
  seq: number;
  title: string;
  task: NodeTaskOut | undefined;
}
export type BusinessFlowNode = Node<BusinessNodeData, 'business'>;

function BusinessTaskNodeInner({ data, selected }: NodeProps<BusinessFlowNode>) {
  return (
    <div className="cl-flow-node">
      <Handle type="target" position={Position.Left} id="inputs" isConnectable={false} />
      <NodeTaskCard seq={data.seq} title={data.title} task={data.task} selected={!!selected} />
      <Handle type="source" position={Position.Right} id="outputs" isConnectable={false} />
    </div>
  );
}

export const BusinessTaskNode = memo(BusinessTaskNodeInner);
const NODE_TYPES = { business: BusinessTaskNode };

/** Columns by dependency depth, rows in the server's (topological) node order. */
export function layoutBusinessGraph(graph: WorkflowGraph, order: string[]): Map<string, { x: number; y: number }> {
  const d = depths(
    graph.nodes.map((n) => n.id),
    graph.edges,
  );
  const rank = new Map(order.map((id, i) => [id, i]));
  const cols = new Map<number, string[]>();
  for (const n of graph.nodes) {
    const c = d.get(n.id) ?? 0;
    cols.set(c, [...(cols.get(c) ?? []), n.id]);
  }
  const pos = new Map<string, { x: number; y: number }>();
  for (const [c, ids] of cols) {
    ids.sort((a, b) => (rank.get(a) ?? 1e9) - (rank.get(b) ?? 1e9) || a.localeCompare(b));
    ids.forEach((id, i) => pos.set(id, { x: c * COL_W, y: i * ROW_H }));
  }
  return pos;
}

interface CanvasProps {
  graph: WorkflowGraph;
  tasks: NodeTaskOut[];
  /** Node id → display number (server order). */
  seqOf: Map<string, number>;
  selected: string | null;
  onSelect: (id: string | null) => void;
}

function Inner({ graph, tasks, seqOf, selected, onSelect }: CanvasProps) {
  const { t } = useI18n();
  const { resolved } = useTheme();
  const rf = useReactFlow<BusinessFlowNode, Edge>();
  const byNode = useMemo(() => new Map(tasks.map((x) => [x.node_id, x])), [tasks]);
  const order = useMemo(() => tasks.map((x) => x.node_id), [tasks]);
  const pos = useMemo(() => layoutBusinessGraph(graph, order), [graph, order]);

  const nodes = useMemo<BusinessFlowNode[]>(
    () =>
      graph.nodes.map((n) => {
        const task = byNode.get(n.id);
        const title = task?.title || (typeof n.params?.title === 'string' ? (n.params.title as string) : '') || n.label || n.id;
        return {
          id: n.id,
          type: 'business',
          position: pos.get(n.id) ?? { x: 0, y: 0 },
          data: { seq: seqOf.get(n.id) ?? 0, title, task },
          selected: n.id === selected,
          draggable: false,
          connectable: false,
          ariaLabel: title,
        };
      }),
    [graph.nodes, byNode, pos, seqOf, selected],
  );

  const edges = useMemo<Edge[]>(
    () =>
      graph.edges.map((e) => {
        const src = byNode.get(e.source)?.phase;
        const tgt = byNode.get(e.target)?.phase;
        const done = src === 'accepted';
        const active = done && (tgt === 'executing' || tgt === 'in_review' || tgt === 'submitted' || tgt === 'rework');
        return {
          id: e.id,
          source: e.source,
          target: e.target,
          sourceHandle: 'outputs',
          targetHandle: 'inputs',
          type: 'smoothstep',
          animated: active,
          selectable: false,
          className: `wf-edge pt-any ${done ? 'st-done' : ''} ${active ? 'st-active' : ''}`,
          markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16 },
        };
      }),
    [graph.edges, byNode],
  );

  useEffect(() => {
    if (!selected) return;
    const p = pos.get(selected);
    if (!p) return;
    void rf.setCenter(p.x + CARD_W / 2, p.y + CARD_H / 2, { zoom: Math.max(rf.getZoom(), 0.8), duration: 300 });
  }, [selected, pos, rf]);

  return (
    <ReactFlow<BusinessFlowNode, Edge>
      nodes={nodes}
      edges={edges}
      nodeTypes={NODE_TYPES}
      nodesDraggable={false}
      nodesConnectable={false}
      elementsSelectable
      deleteKeyCode={null}
      onNodeClick={(_e, n) => onSelect(n.id)}
      onPaneClick={() => onSelect(null)}
      fitView
      fitViewOptions={{ padding: 0.15, maxZoom: 1 }}
      minZoom={0.2}
      maxZoom={1.6}
      colorMode={resolved}
    >
      <Background variant={BackgroundVariant.Dots} gap={16} size={1.2} />
      <Controls showInteractive={false} />
      <MiniMap pannable zoomable style={{ width: 160, height: 104 }} nodeClassName={(n) => `mm-ph-${byNode.get(n.id)?.phase ?? 'none'}`} nodeStrokeWidth={0} ariaLabel={t('wf.minimap')} />
    </ReactFlow>
  );
}

export default function CollaborationCanvas(props: CanvasProps) {
  return (
    <div className="cl-canvas wf-run-canvas">
      <ReactFlowProvider>
        <Inner {...props} />
      </ReactFlowProvider>
    </div>
  );
}

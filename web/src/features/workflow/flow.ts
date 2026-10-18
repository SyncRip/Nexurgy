// Conversion between the workflow graph JSON (design.md §7) and React Flow
// nodes / edges, plus group frames (rendered as background nodes sized to the
// bounding box of their member nodes, so they move with them).

import type { Edge, Node } from '@xyflow/react';
import type { PortType, WorkflowGraph, WorkflowGroup } from '../../api/types';
import { edgePortType, type DefMap } from './model';

export interface WfNodeData extends Record<string, unknown> {
  nodeType: string;
  params: Record<string, unknown>;
  label: string | null;
}
export type WfNode = Node<WfNodeData, 'wf'>;

export interface FrameData extends Record<string, unknown> {
  group: WorkflowGroup;
  active: boolean;
}
export type FrameNode = Node<FrameData, 'frame'>;

export interface WfEdgeData extends Record<string, unknown> {
  portType: PortType;
}
export type WfEdge = Edge<WfEdgeData>;

export type AnyFlowNode = WfNode | FrameNode;

export const FRAME_PREFIX = 'frame::';
export const GRID = 16;
const FRAME_PAD = 32;
const FRAME_HEAD = 32;
const DEFAULT_W = 248;
const DEFAULT_H = 140;

export function edgeClass(portType: PortType, extra = ''): string {
  return `wf-edge pt-${portType}${extra ? ` ${extra}` : ''}`;
}

export function graphToFlow(graph: WorkflowGraph, defs: DefMap): { nodes: WfNode[]; edges: WfEdge[]; groups: WorkflowGroup[] } {
  const typeOf = new Map(graph.nodes.map((n) => [n.id, n.type]));
  const nodes: WfNode[] = graph.nodes.map((n) => ({
    id: n.id,
    type: 'wf',
    position: { x: n.position?.x ?? 0, y: n.position?.y ?? 0 },
    data: { nodeType: n.type, params: { ...(n.params ?? {}) }, label: n.label ?? null },
  }));
  const edges: WfEdge[] = graph.edges.map((e) => {
    const portType = edgePortType(e, (id) => typeOf.get(id), defs);
    return {
      id: e.id,
      source: e.source,
      sourceHandle: e.source_port,
      target: e.target,
      targetHandle: e.target_port,
      data: { portType },
      className: edgeClass(portType),
    };
  });
  return { nodes, edges, groups: (graph.groups ?? []).map((g) => ({ ...g, node_ids: [...g.node_ids] })) };
}

export function flowToGraph(nodes: WfNode[], edges: WfEdge[], groups: WorkflowGroup[]): WorkflowGraph {
  const ids = new Set(nodes.map((n) => n.id));
  return {
    nodes: nodes.map((n) => {
      const out: WorkflowGraph['nodes'][number] = {
        id: n.id,
        type: n.data.nodeType,
        position: { x: Math.round(n.position.x), y: Math.round(n.position.y) },
        params: n.data.params,
      };
      if (n.data.label && n.data.label.trim()) out.label = n.data.label.trim();
      return out;
    }),
    edges: edges.map((e) => ({ id: e.id, source: e.source, source_port: e.sourceHandle ?? '', target: e.target, target_port: e.targetHandle ?? '' })),
    groups: groups
      .map((g) => ({ ...g, node_ids: g.node_ids.filter((id) => ids.has(id)) }))
      .filter((g) => g.node_ids.length > 0)
      .map((g) => (g.color ? g : { id: g.id, label: g.label, node_ids: g.node_ids })),
  };
}

interface Sized {
  id: string;
  position: { x: number; y: number };
  measured?: { width?: number; height?: number };
  width?: number;
  height?: number;
}

/** Frame nodes for groups, sized to their members' bounding box. */
export function computeFrames(groups: WorkflowGroup[], nodes: Sized[], activeGroupId: string | null, draggable: boolean): FrameNode[] {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const frames: FrameNode[] = [];
  for (const g of groups) {
    const members = g.node_ids.map((id) => byId.get(id)).filter((n): n is Sized => !!n);
    if (!members.length) continue;
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    for (const n of members) {
      const w = n.measured?.width ?? n.width ?? DEFAULT_W;
      const h = n.measured?.height ?? n.height ?? DEFAULT_H;
      minX = Math.min(minX, n.position.x);
      minY = Math.min(minY, n.position.y);
      maxX = Math.max(maxX, n.position.x + w);
      maxY = Math.max(maxY, n.position.y + h);
    }
    const width = maxX - minX + FRAME_PAD * 2;
    const height = maxY - minY + FRAME_PAD * 2 + FRAME_HEAD;
    frames.push({
      id: FRAME_PREFIX + g.id,
      type: 'frame',
      position: { x: minX - FRAME_PAD, y: minY - FRAME_PAD - FRAME_HEAD },
      data: { group: g, active: g.id === activeGroupId },
      width,
      height,
      style: { width, height },
      zIndex: -1,
      selectable: false,
      connectable: false,
      deletable: false,
      focusable: false,
      draggable,
      className: 'wf-frame-node',
    });
  }
  return frames;
}

export function isFrameId(id: string): boolean {
  return id.startsWith(FRAME_PREFIX);
}

export function frameGroupId(id: string): string {
  return id.slice(FRAME_PREFIX.length);
}

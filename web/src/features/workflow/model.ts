// Pure workflow helpers shared by the canvas editor and the run views.
//
// Rules here mirror server/app/workflow/{nodes,graph}.py and
// server/app/api/workflows.py. The server stays the authority: local checks
// only give immediate feedback while editing; saving returns the server's
// validation, and runs refuse invalid graphs.

import type {
  GraphError,
  LocalizedText,
  NodeStatus,
  PortType,
  RunStatus,
  WorkflowEdge,
  WorkflowGraph,
  WorkflowNode,
  WorkflowNodeType,
} from '../../api/types';
import type { Lang } from '../../i18n/core';

export const CATEGORIES = ['input', 'cad', 'manufacturing', 'review', 'output'] as const;
export type Category = (typeof CATEGORIES)[number];

export const PORT_TYPES: readonly PortType[] = ['text', 'file', 'model', 'drawing', 'spec', 'plan', 'quote', 'engineering_spec', 'assembly', 'drawing_package', 'delivery', 'manufacturing_plan', 'manufacturing_quote', 'any'];

/** Node types that need the run to be attached to a part (api/workflows.py PART_NODES). */
export const PART_NODE_TYPES: ReadonlySet<string> = new Set(['model_input', 'model_3d', 'drawing_reconstruct', 'drawing', 'drawing_intake', 'process_plan', 'quote', 'export']);

/** Graph-level rule per node type (nodes.py NodeDef.rule; not part of the catalog payload). */
export const NODE_RULES: Readonly<Record<string, string>> = {
  text_input: 'text_required',
  file_input: 'file_required',
  model_input: 'revision_required',
  model_3d: 'model_3d_inputs',
  drawing_reconstruct: 'drawing_reconstruct_inputs',
  process_plan: 'model_or_spec',
};

/** Defaults the server fills with default_factory (absent from params_schema). */
export const FACTORY_DEFAULTS: Readonly<Record<string, Readonly<Record<string, unknown>>>> = {
  drawing: { views: ['front', 'top', 'right', 'iso'] },
  quote: { quantities: [1, 10, 50] },
  assembly_quote: { quantities: [1, 10, 50] },
  export: { include: ['step', 'svg', 'dxf', 'plan', 'quote'] },
};

export const TERMINAL_RUN: ReadonlySet<RunStatus> = new Set(['succeeded', 'partial', 'failed', 'cancelled']);
export const TERMINAL_NODE: ReadonlySet<NodeStatus> = new Set(['succeeded', 'failed', 'cancelled', 'skipped']);

export const GROUP_COLORS = ['measure', 'accent', 'ok', 'warn', 'stale', 'pending'] as const;
export type GroupColor = (typeof GROUP_COLORS)[number];

export function groupColor(c: string | null | undefined): GroupColor {
  return (GROUP_COLORS as readonly string[]).includes(c ?? '') ? (c as GroupColor) : 'measure';
}

export function localized(text: LocalizedText | null | undefined, lang: Lang): string {
  if (!text) return '';
  if (typeof text === 'string') return text;
  return text[lang] || text.en || text.zh || '';
}

export function compatible(src: PortType, dst: PortType): boolean {
  return src === dst || src === 'any' || dst === 'any';
}

export type DefMap = ReadonlyMap<string, WorkflowNodeType>;

export function defMap(defs: WorkflowNodeType[]): Map<string, WorkflowNodeType> {
  return new Map(defs.map((d) => [d.type, d]));
}

export function isCategory(c: string): c is Category {
  return (CATEGORIES as readonly string[]).includes(c);
}

// ─── graph algorithms ──────────────────────────────────────────────────────

interface EdgeLike {
  source: string;
  target: string;
}

/** Kahn's algorithm with sorted queues (same order as graph.py topo_order); null on a cycle. */
export function topoOrder(nodeIds: string[], edges: EdgeLike[]): string[] | null {
  const indeg = new Map(nodeIds.map((id) => [id, 0]));
  const out = new Map<string, string[]>();
  for (const e of edges) {
    if (!indeg.has(e.source) || !indeg.has(e.target)) continue;
    out.set(e.source, [...(out.get(e.source) ?? []), e.target]);
    indeg.set(e.target, (indeg.get(e.target) ?? 0) + 1);
  }
  const queue = [...indeg.entries()].filter(([, d]) => d === 0).map(([id]) => id).sort();
  const order: string[] = [];
  while (queue.length) {
    const n = queue.shift()!;
    order.push(n);
    for (const m of [...(out.get(n) ?? [])].sort()) {
      indeg.set(m, (indeg.get(m) ?? 0) - 1);
      if (indeg.get(m) === 0) queue.push(m);
    }
  }
  return order.length === indeg.size ? order : null;
}

/** True when adding source → target would close a cycle (target already reaches source). */
export function wouldCreateCycle(edges: EdgeLike[], source: string, target: string): boolean {
  if (source === target) return true;
  const out = new Map<string, string[]>();
  for (const e of edges) out.set(e.source, [...(out.get(e.source) ?? []), e.target]);
  const seen = new Set<string>();
  const stack = [target];
  while (stack.length) {
    const cur = stack.pop()!;
    if (cur === source) return true;
    if (seen.has(cur)) continue;
    seen.add(cur);
    stack.push(...(out.get(cur) ?? []));
  }
  return false;
}

/** Longest-path depth per node (layout of graphs saved without positions). */
export function depths(nodeIds: string[], edges: EdgeLike[]): Map<string, number> {
  const order = topoOrder(nodeIds, edges) ?? nodeIds;
  const depth = new Map(nodeIds.map((id) => [id, 0]));
  for (const id of order) {
    for (const e of edges) {
      if (e.source === id && depth.has(e.target)) depth.set(e.target, Math.max(depth.get(e.target)!, depth.get(id)! + 1));
    }
  }
  return depth;
}

/** Give overlapping nodes (e.g. workspace runs saved without positions) a layered layout. */
export function layoutIfOverlapping(graph: WorkflowGraph): WorkflowGraph {
  const seen = new Set<string>();
  let overlap = false;
  for (const n of graph.nodes) {
    const k = `${Math.round(n.position?.x ?? 0)}:${Math.round(n.position?.y ?? 0)}`;
    if (seen.has(k)) overlap = true;
    seen.add(k);
  }
  if (!overlap) return graph;
  const d = depths(
    graph.nodes.map((n) => n.id),
    graph.edges,
  );
  const perLayer = new Map<number, number>();
  return {
    ...graph,
    nodes: graph.nodes.map((n) => {
      const layer = d.get(n.id) ?? 0;
      const row = perLayer.get(layer) ?? 0;
      perLayer.set(layer, row + 1);
      return { ...n, position: { x: layer * 320, y: row * 200 } };
    }),
  };
}

// ─── ids ───────────────────────────────────────────────────────────────────

const ID_RE = /[^A-Za-z0-9_.:-]/g;

export function newNodeId(type: string, existing: ReadonlySet<string>): string {
  const base = type.replace(ID_RE, '_').slice(0, 50) || 'node';
  for (let i = 1; ; i++) {
    const id = `${base}_${i}`;
    if (!existing.has(id)) return id;
  }
}

export function newEdgeId(source: string, sourcePort: string, target: string, targetPort: string, existing: ReadonlySet<string>): string {
  const id = `${source}.${sourcePort}->${target}.${targetPort}`;
  if (id.length <= 80 && !existing.has(id)) return id;
  for (let i = 1; ; i++) {
    const alt = `e_${Date.now().toString(36)}_${i}`;
    if (!existing.has(alt)) return alt;
  }
}

export function newGroupId(existing: ReadonlySet<string>): string {
  for (let i = 1; ; i++) {
    const id = `g_${i}`;
    if (!existing.has(id)) return id;
  }
}

// ─── port typing ───────────────────────────────────────────────────────────

/** Type carried by an edge: the source port's type, or the target's when the source is `any`. */
export function edgePortType(edge: Pick<WorkflowEdge, 'source' | 'source_port' | 'target' | 'target_port'>, nodeType: (id: string) => string | undefined, defs: DefMap): PortType {
  const sd = defs.get(nodeType(edge.source) ?? '');
  const st = sd?.outputs.find((p) => p.name === edge.source_port)?.type;
  if (st && st !== 'any') return st;
  const td = defs.get(nodeType(edge.target) ?? '');
  const tt = td?.inputs.find((p) => p.name === edge.target_port)?.type;
  return tt ?? st ?? 'any';
}

// ─── local validation (mirror of graph.validate_graph) ─────────────────────

function err(code: string, message: string, where: Record<string, string> = {}): GraphError {
  return { code, message, ...where };
}

/**
 * Structural checks that need no server data: missing inputs, node rules,
 * port compatibility, single-connection inputs, cycles. Parameter schemas
 * (invalid_params) are only checked by the server.
 */
export function localValidate(graph: WorkflowGraph, defs: DefMap): GraphError[] {
  const errors: GraphError[] = [];
  if (!graph.nodes.length) errors.push(err('empty_graph', 'add at least one node'));
  const nodes = new Map<string, WorkflowNode>();
  for (const n of graph.nodes) {
    if (nodes.has(n.id)) errors.push(err('duplicate_node', `node id ${n.id} is used twice`, { node_id: n.id }));
    nodes.set(n.id, n);
    if (!defs.has(n.type)) errors.push(err('unknown_node_type', `unknown node type ${n.type}`, { node_id: n.id }));
  }
  const incoming = new Map<string, number>();
  const connected = new Map<string, Set<string>>();
  for (const e of graph.edges) {
    const src = nodes.get(e.source);
    const dst = nodes.get(e.target);
    if (!src || !dst) {
      errors.push(err('dangling_edge', 'edge connects a missing node', { edge_id: e.id }));
      continue;
    }
    if (e.source === e.target) {
      errors.push(err('self_loop', 'a node cannot connect to itself', { edge_id: e.id }));
      continue;
    }
    const sp = defs.get(src.type)?.outputs.find((p) => p.name === e.source_port);
    const dp = defs.get(dst.type)?.inputs.find((p) => p.name === e.target_port);
    if (!sp || !dp) {
      errors.push(err('unknown_port', 'unknown port', { edge_id: e.id }));
      continue;
    }
    if (!compatible(sp.type, dp.type)) errors.push(err('type_mismatch', `${sp.type} cannot connect to ${dp.type}`, { edge_id: e.id, node_id: e.target }));
    const key = `${e.target}\u0000${e.target_port}`;
    incoming.set(key, (incoming.get(key) ?? 0) + 1);
    if ((incoming.get(key) ?? 0) > 1 && !dp.multiple) errors.push(err('port_taken', `input ${e.target_port} accepts one connection`, { edge_id: e.id, node_id: e.target, port: e.target_port }));
    connected.set(e.target, new Set([...(connected.get(e.target) ?? []), e.target_port]));
  }
  for (const n of graph.nodes) {
    const d = defs.get(n.type);
    if (!d) continue;
    const conn = connected.get(n.id) ?? new Set<string>();
    for (const p of d.inputs) {
      if (p.required && !conn.has(p.name)) errors.push(err('missing_input', `connect input '${p.name}'`, { node_id: n.id, port: p.name }));
    }
    errors.push(...ruleErrors(n, NODE_RULES[n.type], conn));
  }
  if (graph.nodes.length && topoOrder(graph.nodes.map((n) => n.id), graph.edges) === null) {
    errors.push(err('cycle', 'the graph contains a cycle; workflows must be acyclic'));
  }
  return errors;
}

function str(v: unknown): string {
  return typeof v === 'string' ? v : v === null || v === undefined ? '' : String(v);
}

function ruleErrors(n: WorkflowNode, rule: string | undefined, connected: Set<string>): GraphError[] {
  const p = n.params ?? {};
  if (rule === 'text_required' && !str(p.text).trim()) return [err('missing_param', 'enter the text', { node_id: n.id, param: 'text' })];
  if (rule === 'file_required' && !p.file_version_id) return [err('missing_param', 'choose a file', { node_id: n.id, param: 'file_version_id' })];
  if (rule === 'revision_required' && !p.revision_id) return [err('missing_param', 'choose a model revision', { node_id: n.id, param: 'revision_id' })];
  if (rule === 'model_3d_inputs') {
    const hasText = connected.has('text') || !!str(p.text).trim();
    const hasGeom = connected.has('file') || connected.has('model');
    if (!hasText && !hasGeom) return [err('missing_input', 'connect a file or model, or enter a request', { node_id: n.id })];
    if (connected.has('file') && connected.has('model')) return [err('ambiguous_input', 'connect either a file or a model, not both', { node_id: n.id })];
  }
  if (rule === 'drawing_reconstruct_inputs') {
    const sources = Number(connected.has('file')) + Number(connected.has('drawing')) + Number(!!p.drawing_id);
    if (sources === 0) return [err('missing_input', 'connect a DXF / DWG file or a drawing', { node_id: n.id })];
    if (sources > 1) return [err('ambiguous_input', 'connect either a file or a drawing (or set drawing_id), not several', { node_id: n.id })];
  }
  if (rule === 'model_or_spec' && connected.has('model') === connected.has('spec')) return [err('missing_input', 'connect exactly one of model or spec', { node_id: n.id })];
  return [];
}

/** Apply per-node parameter overrides (what POST /workflows/{id}/runs does before validating). */
export function withOverrides(graph: WorkflowGraph, overrides: Record<string, Record<string, unknown>>): WorkflowGraph {
  return { ...graph, nodes: graph.nodes.map((n) => (overrides[n.id] ? { ...n, params: { ...n.params, ...overrides[n.id] } } : n)) };
}

// ─── JSON schema helpers (pydantic model_json_schema output) ───────────────

export interface ParamSchema {
  type?: string;
  title?: string;
  default?: unknown;
  enum?: unknown[];
  const?: unknown;
  format?: string;
  minimum?: number;
  maximum?: number;
  maxLength?: number;
  minItems?: number;
  maxItems?: number;
  items?: ParamSchema;
  anyOf?: ParamSchema[];
}

export interface ParamSpec {
  name: string;
  schema: ParamSchema;
  /** Effective default (schema default or the server's default_factory value). */
  defaultValue: unknown;
  nullable: boolean;
}

export function paramSpecs(def: WorkflowNodeType): ParamSpec[] {
  const props = ((def.params_schema as { properties?: Record<string, ParamSchema> }).properties ?? {}) as Record<string, ParamSchema>;
  return Object.entries(props).map(([name, schema]) => {
    const nullable = !!schema.anyOf?.some((s) => s.type === 'null');
    const factory = FACTORY_DEFAULTS[def.type]?.[name];
    return { name, schema, defaultValue: schema.default !== undefined ? schema.default : factory, nullable };
  });
}

/** Value the node runs with: the saved param, else the default. */
export function effectiveParam(spec: ParamSpec, params: Record<string, unknown>): unknown {
  return Object.prototype.hasOwnProperty.call(params, spec.name) ? params[spec.name] : spec.defaultValue;
}

// ─── error helpers ─────────────────────────────────────────────────────────

export function errorsByNode(errors: GraphError[], edgeTarget: (edgeId: string) => string | undefined): Map<string, GraphError[]> {
  const m = new Map<string, GraphError[]>();
  for (const e of errors) {
    const id = e.node_id ?? (e.edge_id ? edgeTarget(e.edge_id) : undefined);
    if (!id) continue;
    m.set(id, [...(m.get(id) ?? []), e]);
  }
  return m;
}

export function formatDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return '—';
  if (ms < 1000) return `${Math.round(ms)} ms`;
  const s = ms / 1000;
  if (s < 60) return `${s < 10 ? s.toFixed(1) : Math.round(s)} s`;
  const m = Math.floor(s / 60);
  const rs = Math.round(s % 60);
  if (m < 60) return `${m} min ${rs} s`;
  const h = Math.floor(m / 60);
  return `${h} h ${m % 60} min`;
}

export function durationBetween(start: string | null | undefined, end: string | null | undefined, now = Date.now()): number | null {
  if (!start) return null;
  const a = new Date(start).getTime();
  const b = end ? new Date(end).getTime() : now;
  if (Number.isNaN(a) || Number.isNaN(b)) return null;
  return Math.max(0, b - a);
}

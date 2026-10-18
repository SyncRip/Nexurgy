// Pure helpers of the AI work panel (.trellis/tasks/archive/2026-10/10-12-ai-work-panel/design.md §4): the panel state in the URL,
// plan previews and run attention highlights, the chat-target line and the node reference the panel adds for a
// selection. No React here (panel/model.test.ts).

import type { AiRef,AiRefInput,AiWorkflowDiff } from '../../../api/assistant';
import type { UUID,WorkflowEdge,WorkflowGraph,WorkflowRun } from '../../../api/types';
import { paths } from '../../../app/nav';
import { partOutputPath } from '../../../app/versionLinks';
import type { CanvasHighlight,EdgeHighlight,NodeHighlight } from '../../workflow/highlight';
import { NODE_PARAM,PANEL_PARAM,panelParam,parsePanelParam,WIDE_PARAM,type PanelObject,type WorkPanelState } from './WorkPanelContext';

// ─── URL state ─────────────────────────────────────────────────────────────

/** The panel state a query string describes (node and wide only count while an object is open). */
export function readPanelState(q: URLSearchParams): WorkPanelState {
  const object = parsePanelParam(q.get(PANEL_PARAM));
  if (!object) return { object: null, nodeId: null, wide: false };
  const node = q.get(NODE_PARAM);
  return { object, nodeId: node && node.length <= 60 ? node : null, wide: q.get(WIDE_PARAM) === '1' };
}

export function sameObject(a: PanelObject | null, b: PanelObject | null): boolean {
  return (a ? panelParam(a) : null) === (b ? panelParam(b) : null);
}

export interface PanelPatch {
  /** undefined: keep; null: close (node and wide go too). */
  object?: PanelObject | null;
  /** undefined: keep (another object: none). */
  nodeId?: string | null;
  wide?: boolean;
}

/**
 * The next query string for a panel change. Other parameters are kept. `push` when another object opens (so Back
 * returns to the previous one); every other change replaces the entry.
 */
export function nextPanelSearch(cur: URLSearchParams, patch: PanelPatch): { search: URLSearchParams; push: boolean; changed: boolean } {
  const now = readPanelState(cur);
  const q = new URLSearchParams(cur);
  const object = patch.object === undefined ? now.object : patch.object;
  if (!object) {
    q.delete(PANEL_PARAM);
    q.delete(NODE_PARAM);
    q.delete(WIDE_PARAM);
    return { search: q, push: false, changed: q.toString() !== cur.toString() };
  }
  const other = !sameObject(object, now.object);
  const nodeId = patch.nodeId !== undefined ? patch.nodeId : other ? null : now.nodeId;
  const wide = patch.wide ?? now.wide;
  q.set(PANEL_PARAM, panelParam(object));
  if (nodeId) q.set(NODE_PARAM, nodeId);
  else q.delete(NODE_PARAM);
  if (wide) q.set(WIDE_PARAM, '1');
  else q.delete(WIDE_PARAM);
  return { search: q, push: other, changed: q.toString() !== cur.toString() };
}

// ─── highlights ────────────────────────────────────────────────────────────

/** What a plan preview needs: the planned graph, its diff, the base graph when the plan has one (edits). */
export interface PlanGraphs {
  diff: AiWorkflowDiff;
  graph_after: WorkflowGraph;
  graph_before?: WorkflowGraph | null;
  focus_node_id?: string | null;
}

export const GHOST_PREFIX = 'ghost:';

const conn = (e: Pick<WorkflowEdge, 'source' | 'source_port' | 'target' | 'target_port'>) => `${e.source}.${e.source_port}>${e.target}.${e.target_port}`;

/**
 * The planned graph with what the plan removes drawn back in as ghosts (removed nodes from the base graph, removed
 * edges with ids prefixed `ghost:`), and the highlights: added > changed (params or name) > focus per node; added /
 * removed per edge. Without a base graph (workflow_create) removed nodes have no position and are not drawn.
 */
export function planPreview(p: PlanGraphs): { graph: WorkflowGraph; highlight: CanvasHighlight } {
  const nodes: Record<string, NodeHighlight> = {};
  const edges: Record<string, EdgeHighlight> = {};
  const after = p.graph_after;
  const afterIds = new Set(after.nodes.map((n) => n.id));
  if (p.focus_node_id && afterIds.has(p.focus_node_id)) nodes[p.focus_node_id] = 'focus';
  for (const c of p.diff.changed_params) if (afterIds.has(c.node_id)) nodes[c.node_id] = 'changed';
  for (const r of p.diff.renamed) if (afterIds.has(r.node_id)) nodes[r.node_id] = 'changed';
  for (const n of p.diff.added_nodes) if (afterIds.has(n.id)) nodes[n.id] = 'added';

  const added = new Set(p.diff.added_edges.map(conn));
  for (const e of after.edges) if (added.has(conn(e))) edges[e.id] = 'added';

  const ghostNodes = (p.graph_before?.nodes ?? []).filter((n) => !afterIds.has(n.id) && p.diff.removed_nodes.some((r) => r.id === n.id));
  for (const n of ghostNodes) nodes[n.id] = 'removed';
  const drawn = new Set([...afterIds, ...ghostNodes.map((n) => n.id)]);
  const afterConn = new Set(after.edges.map(conn));
  const ghostEdges = p.diff.removed_edges
    .filter((e) => drawn.has(e.source) && drawn.has(e.target) && !afterConn.has(conn(e)))
    .map((e) => ({ ...e, id: `${GHOST_PREFIX}${e.id || conn(e)}` }));
  for (const e of ghostEdges) edges[e.id] = 'removed';

  return {
    graph: { ...after, nodes: [...after.nodes, ...ghostNodes], edges: [...after.edges, ...ghostEdges] },
    highlight: { nodes, edges },
  };
}

/** Run canvas: steps waiting for a person or failed. */
export function runHighlight(run: Pick<WorkflowRun, 'nodes'>): CanvasHighlight {
  const nodes: Record<string, NodeHighlight> = {};
  for (const n of run.nodes) if (n.status === 'waiting_human' || n.status === 'failed') nodes[n.node_id] = 'attention';
  return { nodes };
}

// ─── selection ↔ chat target ───────────────────────────────────────────────

/** An object whose nodes can become the chat target (workflow pane, run pane). */
export interface NodeSource {
  kind: 'workflow' | 'run';
  objectId: UUID;
  projectId: UUID;
  version?: number;
}

export function nodeRefInput(src: NodeSource, nodeId: string): AiRefInput & { project_id: UUID } {
  return { kind: src.kind, object_id: src.objectId, project_id: src.projectId, node: { node_id: nodeId }, ...(src.kind === 'workflow' && src.version !== undefined ? { version: src.version } : {}) };
}

/** The reference points at this node of this object (a workflow ref at any pinned version). */
export function refIsNode(ref: Pick<AiRef, 'kind' | 'object_id' | 'node'> | null | undefined, src: Pick<NodeSource, 'kind' | 'objectId'>, nodeId: string): boolean {
  return !!ref && ref.kind === src.kind && ref.object_id === src.objectId && (ref.node?.node_id ?? null) === nodeId;
}

/**
 * After the panel added `added` as the target for a selection: the node reference it added before (`prev`) goes
 * when it is no longer the target (at most one auto-added node reference), and the panel owns `added` only when it
 * was not in the composer already (a reference the person added stays theirs).
 */
export function autoRefChange(prev: string | null, added: string, before: readonly string[]): { remove: string | null; auto: string | null } {
  const existed = before.includes(added);
  return {
    remove: prev && prev !== added && before.includes(prev) ? prev : null,
    auto: existed && prev !== added ? null : added,
  };
}

export type TargetLine =
  /** No chat target. */
  | 'none'
  /** The target is the selected node and follows the selection. */
  | 'following'
  /** A node is selected but the target is something else (locked, or changed in the composer). */
  | 'not_following'
  /** Nothing selected (or locked on the selected node itself). */
  | 'plain';

export function targetLine(target: Pick<AiRef, 'kind' | 'object_id' | 'node'> | null, sel: { src: NodeSource; nodeId: string } | null, locked: boolean): TargetLine {
  if (!target) return 'none';
  if (!sel) return 'plain';
  if (!refIsNode(target, sel.src, sel.nodeId)) return 'not_following';
  return locked ? 'plain' : 'following';
}

// ─── links ─────────────────────────────────────────────────────────────────

/** "Open full page" of a panel object (null when the page needs facts the panel does not have yet). */
export function fullPageHref(o: PanelObject, facts: { partId?: UUID | null; workflowId?: UUID | null } = {}): string | null {
  const partFlow = (partId: UUID, workflowId?: UUID | null) => `${paths.part(partId, 'workflow')}${workflowId ? `?flow=${encodeURIComponent(workflowId)}` : ''}`;
  switch (o.kind) {
    case 'workflow':
      return facts.partId ? partFlow(facts.partId, o.id) : paths.workflow(o.id);
    case 'plan':
      return facts.partId ? partFlow(facts.partId, facts.workflowId) : facts.workflowId ? paths.workflow(facts.workflowId) : null;
    case 'run':
      return paths.run(o.id);
    case 'revision':
      return facts.partId ? partOutputPath(facts.partId, 'model', o.id) : null;
    case 'task':
      return paths.task(o.id);
    case 'draft':
      // A draft lives only in its conversation (no page of its own).
      return null;
  }
}

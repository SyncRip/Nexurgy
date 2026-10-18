// Pure helpers of the conversation's flow cards (.trellis/tasks/archive/2026-10/10-12-ai-work-panel design §5): a run's state as a
// short digest, what needs a person (blocking items) and what it produced (results), all in graph order. Built only
// from GET /runs/{id}; FlowCard.tsx localises the structured data. Unit-tested in flowCard.test.ts.

import type { ApprovalStage, NodeReview, NodeRun, NodeStatus, RunStatus, UUID, WorkflowRun } from '../../api/types';
import { durationBetween, TERMINAL_RUN, topoOrder } from '../workflow/model';

type Obj = Record<string, unknown>;

function obj(v: unknown): Obj | null {
  return typeof v === 'object' && v !== null && !Array.isArray(v) ? (v as Obj) : null;
}

function str(v: unknown): string | null {
  return typeof v === 'string' && v ? v : null;
}

/** Input nodes only hold what the run started from: never a step worth naming. */
const INPUT_TYPES: ReadonlySet<string> = new Set(['text_input', 'file_input', 'model_input']);
/** Where the run is: in progress, waiting for a person or failed. */
const ATTENTION: ReadonlySet<NodeStatus> = new Set(['running', 'ready', 'waiting_human', 'failed']);
const DONE: ReadonlySet<NodeStatus> = new Set(['succeeded', 'skipped']);

export interface FlowNode {
  nodeId: string;
  type: string;
  label: string | null;
  /** A node without a node run yet has not started (pending). */
  status: NodeStatus;
}

type RunGraphPart = Pick<WorkflowRun, 'graph' | 'nodes'>;

/** The run's graph nodes in topological order (graph.py topo_order; the snapshot order if it had a cycle). */
export function orderedNodes(run: RunGraphPart): FlowNode[] {
  const nodes = run.graph?.nodes ?? [];
  const ids = nodes.map((n) => n.id);
  const order = topoOrder(ids, run.graph?.edges ?? []) ?? ids;
  const graphNode = new Map(nodes.map((n) => [n.id, n]));
  const nodeRun = new Map(run.nodes.map((n) => [n.node_id, n]));
  return order.map((id) => {
    const g = graphNode.get(id)!;
    const nr = nodeRun.get(id);
    return { nodeId: id, type: nr?.node_type ?? g.type, label: g.label?.trim() || null, status: nr?.status ?? 'pending' };
  });
}

// ─── digest ────────────────────────────────────────────────────────────────

export const DIGEST_MAX = 3;

export interface RunDigest {
  /** At most DIGEST_MAX steps around where the run is, in graph order. */
  segments: FlowNode[];
  /** Steps not named ("n more…"). */
  more: number;
  /** Every step succeeded (or was skipped): the card says so instead of naming steps. */
  allDone: boolean;
  /** Steps (input nodes excluded). */
  total: number;
}

/**
 * One deterministic sentence of where the run is ("modeling done; design review waiting; quote not started"):
 * every step that needs attention (running, waiting, failed) first, then the steps next to the first of them —
 * the one before it, then the one after it — so a reader sees what finished, what waits and what comes next.
 */
export function runDigest(run: RunGraphPart): RunDigest {
  const steps = orderedNodes(run).filter((n) => !INPUT_TYPES.has(n.type));
  const total = steps.length;
  if (total > 0 && steps.every((n) => DONE.has(n.status))) return { segments: [], more: 0, allDone: true, total };
  let f = steps.findIndex((n) => ATTENTION.has(n.status));
  if (f < 0) f = steps.findIndex((n) => !DONE.has(n.status));
  if (f < 0) f = 0;
  const rank = (i: number): number[] => [ATTENTION.has(steps[i].status) ? 0 : 1, Math.abs(i - f), i < f ? 0 : 1, i];
  const cmp = (a: number[], b: number[]) => {
    for (let k = 0; k < a.length; k++) if (a[k] !== b[k]) return a[k] - b[k];
    return 0;
  };
  const picked = steps
    .map((_, i) => i)
    .sort((a, b) => cmp(rank(a), rank(b)))
    .slice(0, DIGEST_MAX)
    .sort((a, b) => a - b);
  return { segments: picked.map((i) => steps[i]), more: total - picked.length, allDone: false, total };
}

// ─── blocking items ────────────────────────────────────────────────────────

interface PendingBase {
  nodeId: string;
  type: string;
  label: string | null;
  /** One waiting episode / failure (forms remount per key, so answers never leak between attempts). */
  key: string;
  attempt: number;
  maxAttempts: number;
}

export type PendingItem =
  | (PendingBase & { kind: 'clarification'; canDecide: boolean; questions: string[] })
  | (PendingBase & {
      kind: 'approval';
      canDecide: boolean;
      stage: ApprovalStage;
      instructions: string | null;
      /** The approval request's target hash: sent back as expected_hash so a changed subject is refused. */
      targetHash: string | null;
    })
  | (PendingBase & { kind: 'manual_entry'; canDecide: boolean; reason: string | null })
  /** waiting_human without review details (an older server): handled in the panel. */
  | (PendingBase & { kind: 'waiting' })
  | (PendingBase & { kind: 'failed'; code: string | null; message: string | null; canRetry: boolean });

function pendingOf(run: Pick<WorkflowRun, 'status' | 'cancel_requested'>, n: FlowNode, nr: NodeRun): PendingItem | null {
  const base: PendingBase = { nodeId: n.nodeId, type: n.type, label: n.label, key: `${n.nodeId}:${nr.attempt}:${nr.status}`, attempt: nr.attempt, maxAttempts: nr.max_attempts };
  if (nr.status === 'failed') {
    return { ...base, kind: 'failed', code: nr.error?.code ?? null, message: nr.error?.message ?? null, canRetry: !run.cancel_requested && run.status !== 'cancelled' };
  }
  if (nr.status !== 'waiting_human') return null;
  const r: NodeReview | undefined = nr.review ?? undefined;
  if (!r) return { ...base, kind: 'waiting' };
  if (r.kind === 'clarification') return { ...base, kind: 'clarification', canDecide: r.can_decide, questions: r.clarification?.questions ?? [] };
  if (r.kind === 'manual_entry') return { ...base, kind: 'manual_entry', canDecide: r.can_decide, reason: r.manual_entry?.reason ?? null };
  return { ...base, kind: 'approval', canDecide: r.can_decide, stage: r.stage ?? 'design', instructions: str(r.instructions), targetHash: r.target_hash ?? null };
}

/** What needs a person: waiting steps by review kind, then failed steps, in graph order. */
export function pendingItems(run: RunGraphPart & Pick<WorkflowRun, 'status' | 'cancel_requested'>): PendingItem[] {
  const nodeRun = new Map(run.nodes.map((n) => [n.node_id, n]));
  const out: PendingItem[] = [];
  for (const n of orderedNodes(run)) {
    const nr = nodeRun.get(n.nodeId);
    const item = nr ? pendingOf(run, n, nr) : null;
    if (item) out.push(item);
  }
  return out;
}

// ─── results ───────────────────────────────────────────────────────────────

export interface ResultItem {
  nodeId: string;
  nodeType: string;
  nodeLabel: string | null;
  outputKey: string;
  attempt: number;
  assetVersionId: UUID;
  assetId: UUID | null;
  assetType: string | null;
  name: string | null;
  /** The exact version ("R3", "v2"); null when the version no longer exists. */
  versionLabel: string | null;
  availability: string | null;
  /** Quality / provenance labels (simulated, estimated …). */
  labels: string[];
  /** Model revisions: the requirement check of the step that produced it (null = none recorded). */
  check: string | null;
  /** The object the version is (model revision, drawing, process plan, quote …). */
  target: { kind: string; id: UUID; partId: UUID | null } | null;
  missing: boolean;
}

/** run.outputs per step, only the step's latest attempt, in graph order. */
export function resultItems(run: RunGraphPart & Pick<WorkflowRun, 'outputs'>): ResultItem[] {
  const outputs = run.outputs ?? [];
  if (!outputs.length) return [];
  const latest = new Map<string, number>();
  for (const o of outputs) latest.set(o.node_id, Math.max(latest.get(o.node_id) ?? 0, o.attempt));
  const order = orderedNodes(run);
  const pos = new Map(order.map((n, i) => [n.nodeId, i]));
  const flow = new Map(order.map((n) => [n.nodeId, n]));
  const nodeRun = new Map(run.nodes.map((n) => [n.node_id, n]));
  return outputs
    .map((o, i) => ({ o, i }))
    .filter(({ o }) => o.attempt === latest.get(o.node_id))
    .sort((a, b) => (pos.get(a.o.node_id) ?? Infinity) - (pos.get(b.o.node_id) ?? Infinity) || a.i - b.i)
    .map(({ o }): ResultItem => {
      const v = o.version;
      const nr = nodeRun.get(o.node_id);
      const fn = flow.get(o.node_id);
      const target = obj(v?.target);
      const assetType = str(v?.asset_type);
      const check = assetType === 'part_model' && nr && nr.attempt === o.attempt ? str(obj(obj(nr.outputs)?.requirements_check)?.status) : null;
      const versionNo = typeof v?.version_no === 'number' ? v.version_no : null;
      return {
        nodeId: o.node_id,
        nodeType: fn?.type ?? nr?.node_type ?? '',
        nodeLabel: fn?.label ?? null,
        outputKey: o.output_key,
        attempt: o.attempt,
        assetVersionId: o.asset_version_id,
        assetId: str(v?.asset_id),
        assetType,
        name: str(v?.asset_name),
        versionLabel: v && !v.missing ? (str(v.label) ?? (versionNo !== null ? `v${versionNo}` : null)) : null,
        availability: str(v?.availability),
        labels: Array.isArray(v?.labels) ? v.labels.filter((l): l is string => typeof l === 'string') : [],
        check,
        target: target && str(target.kind) && str(target.id) ? { kind: target.kind as string, id: target.id as string, partId: str(target.part_id) } : null,
        missing: !v || v.missing === true,
      };
    });
}

// ─── finished run ──────────────────────────────────────────────────────────

export interface RunOutcome {
  status: RunStatus;
  total: number;
  done: number;
  failed: number;
  results: number;
  durationMs: number | null;
}

/** The deterministic summary of a finished run (null while it is still going). */
export function runOutcome(run: RunGraphPart & Pick<WorkflowRun, 'status' | 'outputs' | 'started_at' | 'finished_at'>): RunOutcome | null {
  if (!TERMINAL_RUN.has(run.status)) return null;
  const steps = orderedNodes(run).filter((n) => !INPUT_TYPES.has(n.type));
  return {
    status: run.status,
    total: steps.length,
    done: steps.filter((n) => n.status === 'succeeded').length,
    failed: steps.filter((n) => n.status === 'failed').length,
    results: resultItems(run).length,
    durationMs: durationBetween(run.started_at, run.finished_at),
  };
}

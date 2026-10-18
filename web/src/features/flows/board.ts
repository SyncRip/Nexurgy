// Pure helpers of the AI workflow board (R1.2 prototype #flows.runs): which
// lane a run belongs to, why it needs someone, its node progress and where
// its card links to. No React; unit-tested in board.test.ts.
//
// Governance: the lanes describe *runs*. A finished run is not a finished
// task (tasks still need a submission and an acceptance), and the board never
// offers a decision — cards only lead to the run / approval pages.

import type { ApprovalRequest } from '../../api/approvals';
import type { ApprovalStage, NodeRun, NodeStatus, RunStatus, RunSummary, Workflow } from '../../api/types';
import { paths } from '../../app/nav';

export const LANES = ['draft', 'running', 'attention', 'done'] as const;
export type Lane = (typeof LANES)[number];

const RUN_LANE: Record<RunStatus, Exclude<Lane, 'draft'>> = {
  queued: 'running',
  running: 'running',
  waiting: 'attention',
  failed: 'attention',
  partial: 'attention',
  succeeded: 'done',
  cancelled: 'done',
};

/** Runs never are drafts: a draft is a collaboration plan that has not started. */
export function laneOfRun(run: Pick<RunSummary, 'status'>): Exclude<Lane, 'draft'> {
  return RUN_LANE[run.status] ?? 'running';
}

/** A collaboration draft that was never started (starting publishes it and creates a run). */
export function isOpenDraft(w: Pick<Workflow, 'id' | 'archived_at' | 'published_count'>, startedWorkflowIds: ReadonlySet<string>): boolean {
  return !w.archived_at && !(w.published_count ?? 0) && !startedWorkflowIds.has(w.id);
}

export type BoardItem = { kind: 'run'; run: RunSummary } | { kind: 'draft'; workflow: Workflow };

const time = (iso: string | null | undefined) => (iso ? Date.parse(iso) || 0 : 0);

/** Board columns: drafts first lane, runs by status; newest activity first in every lane. */
export function buildBoard(runs: readonly RunSummary[], drafts: readonly Workflow[]): Record<Lane, BoardItem[]> {
  const started = new Set(runs.map((r) => r.workflow_id).filter((x): x is string => !!x));
  const out: Record<Lane, BoardItem[]> = { draft: [], running: [], attention: [], done: [] };
  for (const w of drafts) if (isOpenDraft(w, started)) out.draft.push({ kind: 'draft', workflow: w });
  for (const r of runs) out[laneOfRun(r)].push({ kind: 'run', run: r });
  const at = (x: BoardItem) => time(x.kind === 'run' ? x.run.updated_at ?? x.run.created_at : x.workflow.updated_at);
  for (const lane of LANES) out[lane].sort((a, b) => at(b) - at(a));
  return out;
}

// ─── node progress ─────────────────────────────────────────────────────────

/** Left-to-right order of the progress cells: done work first, untouched nodes last. */
export const CELL_ORDER: readonly NodeStatus[] = ['succeeded', 'running', 'ready', 'waiting_human', 'failed', 'cancelled', 'skipped', 'pending'];

export interface Progress {
  total: number;
  succeeded: number;
  /** One status per node in CELL_ORDER (empty when there are more nodes than `maxCells`). */
  cells: NodeStatus[];
}

export function progressOf(counts: RunSummary['node_counts'], maxCells = 16): Progress {
  let total = 0;
  for (const s of CELL_ORDER) total += Math.max(0, counts[s] ?? 0);
  const cells: NodeStatus[] = [];
  if (total <= maxCells) for (const s of CELL_ORDER) for (let i = 0; i < Math.max(0, counts[s] ?? 0); i++) cells.push(s);
  return { total, succeeded: Math.max(0, counts.succeeded ?? 0), cells };
}

// ─── why a run needs someone ───────────────────────────────────────────────

export type NeedReason =
  /** A pending engineering approval of one of its nodes. */
  | { kind: 'approval'; stage: ApprovalStage | null; assignee: string | null; more: number }
  /** A collaboration node waiting for its acceptance decision. */
  | { kind: 'acceptance'; title: string; assignee: string | null; more: number }
  /** A node asking questions before it can continue (model_3d / reconstruction clarification). */
  | { kind: 'question'; text: string; more: number }
  /** A drawing intake waiting for someone to enter the data by hand. */
  | { kind: 'manual_entry' }
  /** Waiting nodes whose reason is not loaded. */
  | { kind: 'waiting'; n: number }
  | { kind: 'failed'; n: number }
  | { kind: 'cancelling' };

/** Waiting runs whose reason needs the run detail (no pending request names it). */
export function runsNeedingDetail(runs: readonly Pick<RunSummary, 'id' | 'status'>[], byRun: ReadonlyMap<string, unknown>, max = 12): string[] {
  return runs
    .filter((r) => r.status === 'waiting' && !byRun.has(r.id))
    .slice(0, max)
    .map((r) => r.id);
}

/** Pending requests per run (engineering approvals carry run_id; collaboration acceptances their node_task). */
export function requestsByRun(requests: readonly ApprovalRequest[]): Map<string, ApprovalRequest[]> {
  const out = new Map<string, ApprovalRequest[]>();
  for (const r of requests) {
    if (r.status !== 'pending') continue;
    const runId = r.run_id ?? r.node_task?.run_id;
    if (!runId) continue;
    out.set(runId, [...(out.get(runId) ?? []), r]);
  }
  return out;
}

/**
 * The first reason a run needs someone. `nodes` (from the run detail) names the
 * questions or manual entry of waiting nodes that have no approval request.
 */
export function needOf(
  run: Pick<RunSummary, 'status' | 'node_counts' | 'cancel_requested'>,
  requests: readonly ApprovalRequest[] = [],
  nodes?: readonly Pick<NodeRun, 'status' | 'review'>[],
): NeedReason | null {
  if (run.cancel_requested && (run.status === 'queued' || run.status === 'running' || run.status === 'waiting')) return { kind: 'cancelling' };
  if (run.status === 'failed' || run.status === 'partial') return { kind: 'failed', n: run.node_counts.failed ?? 0 };
  if (run.status !== 'waiting') return null;
  const pending = requests.filter((r) => r.status === 'pending');
  const first = pending[0];
  if (first?.kind === 'engineering') return { kind: 'approval', stage: first.stage, assignee: first.assignee?.name ?? null, more: pending.length - 1 };
  if (first) return { kind: 'acceptance', title: first.title, assignee: first.assignee?.name ?? null, more: pending.length - 1 };
  const waiting = (nodes ?? []).filter((n) => n.status === 'waiting_human');
  for (const n of waiting) {
    const c = n.review?.kind === 'clarification' ? n.review.clarification : null;
    if (!c) continue;
    const answered = new Set((c.answers ?? []).map((a) => a.question));
    const open = (c.questions ?? []).filter((q) => !answered.has(q));
    if (open.length) return { kind: 'question', text: open[0], more: open.length - 1 };
  }
  if (waiting.some((n) => n.review?.kind === 'manual_entry')) return { kind: 'manual_entry' };
  return { kind: 'waiting', n: run.node_counts.waiting_human ?? 0 };
}

// ─── links ─────────────────────────────────────────────────────────────────

/**
 * Where a card opens: collaboration runs on their collaboration page, part runs on the
 * part canvas with that run selected, everything else on the run page.
 */
export function runHref(run: Pick<RunSummary, 'id' | 'part_id'>, collaborationRunIds: ReadonlySet<string>): string {
  if (collaborationRunIds.has(run.id)) return paths.collaborationRun(run.id);
  if (run.part_id) return `${paths.part(run.part_id, 'workflow')}?run=${encodeURIComponent(run.id)}`;
  return paths.run(run.id);
}

/** Runs per workflow (template) id, for "used N times". */
export function runsPerWorkflow(runs: readonly Pick<RunSummary, 'workflow_id'>[]): Map<string, number> {
  const out = new Map<string, number>();
  for (const r of runs) if (r.workflow_id) out.set(r.workflow_id, (out.get(r.workflow_id) ?? 0) + 1);
  return out;
}

/** Count label of a list loaded with `limit + 1` rows ("100+" when there are more). */
export function countLabel(n: number, limit: number): string {
  return n > limit ? `${limit}+` : String(n);
}

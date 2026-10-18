// Project task board (R1.2 §09, prototype #project.tasks): the manager's view
// of every task — column, the one next step that moves it on, its latest
// workflow run and the stage strip above it. Pure functions, unit-tested.
//
// Governance: a finished workflow run never moves a task (G1) — only a
// submission and its acceptance do. The "accept" step only opens the decision
// page; nothing here approves anything (G3).

import type { Milestone, Task, TaskStatus } from '../../api/pm';
import type { RunSummary } from '../../api/types';

export const BOARD_COLUMNS = ['todo', 'in_progress', 'in_review', 'done'] as const;
export type BoardColumn = (typeof BOARD_COLUMNS)[number];

/** The list endpoint also returns the predecessors that are not done yet. */
export type BoardTask = Task & { waiting_on?: { id: string; key: string; status: string }[] };

/** Board column of a status; cancelled tasks are not on the board. */
export function boardColumn(status: TaskStatus): BoardColumn | null {
  return status === 'cancelled' ? null : status;
}

export function groupByColumn<T extends Pick<Task, 'status'>>(tasks: readonly T[]): Record<BoardColumn, T[]> {
  const out: Record<BoardColumn, T[]> = { todo: [], in_progress: [], in_review: [], done: [] };
  for (const t of tasks) {
    const col = boardColumn(t.status);
    if (col) out[col].push(t);
  }
  return out;
}

/** "Tasks I own or accept" filter. */
export function ownsOrReviews(task: Pick<Task, 'owner_id' | 'reviewer_id'>, userId: string | null | undefined): boolean {
  return !!userId && (task.owner_id === userId || task.reviewer_id === userId);
}

/** The pending acceptance request of a task, as the approvals list returns it. */
export interface AcceptanceRef {
  requestId: string;
  /** The server says the current user may decide it now. */
  canDecide: boolean;
  assigneeId: string | null;
  /** Decided by the independent reviewing agent, not a person. */
  agent: boolean;
}

export type BoardNext =
  | { kind: 'done' }
  | { kind: 'cancelled' }
  /** My turn to accept: the button opens the decision page (never approves). */
  | { kind: 'accept'; requestId: string }
  | { kind: 'awaitAcceptance'; reviewerId: string | null; agent: boolean }
  | { kind: 'blocked'; reason: string }
  | { kind: 'waitingOn'; keys: string[] }
  | { kind: 'start'; mine: boolean; ownerId: string | null }
  /** Deliverables are selected: submit them for acceptance on the task page. */
  | { kind: 'submit'; count: number; mine: boolean }
  /** In progress without deliverables: mark a workflow result as a deliverable first. */
  | { kind: 'pickDeliverables' }
  /** In progress, deliverables not known (loading or failed). */
  | { kind: 'working' };

export interface NextContext {
  userId: string | null | undefined;
  /** Selected deliverables of an in-progress task; null = unknown. */
  deliverables: number | null;
  acceptance?: AcceptanceRef | null;
}

export function boardNext(task: Pick<BoardTask, 'status' | 'owner_id' | 'reviewer_id' | 'blocked_reason' | 'waiting_on'>, ctx: NextContext): BoardNext {
  switch (task.status) {
    case 'done':
      return { kind: 'done' };
    case 'cancelled':
      return { kind: 'cancelled' };
    case 'in_review': {
      const a = ctx.acceptance;
      if (a?.canDecide && !a.agent) return { kind: 'accept', requestId: a.requestId };
      return { kind: 'awaitAcceptance', reviewerId: a?.assigneeId ?? task.reviewer_id, agent: !!a?.agent };
    }
    default:
      break;
  }
  if (task.blocked_reason) return { kind: 'blocked', reason: task.blocked_reason };
  const waiting = task.waiting_on ?? [];
  if (waiting.length) return { kind: 'waitingOn', keys: waiting.map((w) => w.key) };
  const mine = !!ctx.userId && task.owner_id === ctx.userId;
  if (task.status === 'todo') return { kind: 'start', mine, ownerId: task.owner_id };
  if (ctx.deliverables === null) return { kind: 'working' };
  return ctx.deliverables > 0 ? { kind: 'submit', count: ctx.deliverables, mine } : { kind: 'pickDeliverables' };
}

/**
 * Latest run of each task (by creation time). When the run list was cut at its
 * page size, a task missing here may still have older runs — callers must not
 * claim "no run" then.
 */
export function latestRunByTask(runs: readonly RunSummary[]): Map<string, RunSummary> {
  const out = new Map<string, RunSummary>();
  for (const r of runs) {
    if (!r.task_id) continue;
    const cur = out.get(r.task_id);
    if (!cur || (r.created_at ?? '') > (cur.created_at ?? '')) out.set(r.task_id, r);
  }
  return out;
}

// ─── project strip ──────────────────────────────────────────────────────────

export type StageState = 'done' | 'current' | 'todo';

export interface StageStep {
  id: string;
  seq: number;
  name: string;
  state: StageState;
}

/** Stages in order: accepted ones are done, the project's current stage is current. */
export function stageSteps(milestones: readonly Pick<Milestone, 'id' | 'seq' | 'name' | 'status'>[], currentId: string | null | undefined): StageStep[] {
  return [...milestones]
    .sort((a, b) => a.seq - b.seq)
    .map((m) => ({ id: m.id, seq: m.seq, name: m.name, state: m.status === 'accepted' ? 'done' : m.id === currentId ? 'current' : 'todo' }));
}

/** 1-based position of the current stage (0 when there is none, e.g. all accepted). */
export function currentStageIndex(steps: readonly StageStep[]): number {
  return steps.findIndex((s) => s.state === 'current') + 1;
}

export interface TaskTally {
  accepted: number;
  awaiting: number;
  /** Every task except cancelled ones. */
  total: number;
}

export function taskTally(byStatus: Partial<Record<TaskStatus, number>>): TaskTally {
  const n = (s: TaskStatus) => byStatus[s] ?? 0;
  return { accepted: n('done'), awaiting: n('in_review'), total: n('todo') + n('in_progress') + n('in_review') + n('done') };
}

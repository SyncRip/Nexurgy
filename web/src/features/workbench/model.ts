// Workbench "My tasks" (R1.2 §05): the open tasks I own or review in the
// current project, each with the one next step that moves it on. The next
// step only points to the task page — finishing a task still goes through a
// submission and its acceptance there (G1).

import type { Task, TaskPriority } from '../../api/pm';

export type NextStep =
  /** Owner: the task is flagged as blocked. */
  | 'unblock'
  /** Owner: not started yet. */
  | 'start'
  /** Owner: in progress — run the workflow and submit the result for acceptance. */
  | 'execute'
  /** Owner: submitted, waiting for the reviewer. */
  | 'awaitReview'
  /** Reviewer: a submission waits for my acceptance. */
  | 'review'
  | 'open';

export interface WorkItem {
  task: Task;
  step: NextStep;
  /** I am the reviewer (and not the owner). */
  reviewing: boolean;
  overdue: boolean;
}

const OPEN = new Set(['todo', 'in_progress', 'in_review']);

export function taskNextStep(task: Pick<Task, 'status' | 'owner_id' | 'reviewer_id' | 'blocked_reason'>, userId: string): NextStep {
  const owner = task.owner_id === userId;
  if (task.status === 'in_review' && task.reviewer_id === userId && !owner) return 'review';
  if (!owner) return 'open';
  if (task.blocked_reason && task.status !== 'in_review') return 'unblock';
  switch (task.status) {
    case 'todo':
      return 'start';
    case 'in_progress':
      return 'execute';
    case 'in_review':
      return 'awaitReview';
    default:
      return 'open';
  }
}

const PRIORITY_RANK: Record<TaskPriority, number> = { urgent: 0, high: 1, normal: 2, low: 3 };
/** Steps that need me now come first; waiting comes last. */
const STEP_RANK: Record<NextStep, number> = { review: 0, unblock: 1, execute: 2, start: 3, open: 4, awaitReview: 5 };

function localDay(d: Date): string {
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

/**
 * Due dates are calendar days (the server stores midnight UTC): compare the
 * day itself with today's local day, so a task due today is never overdue
 * whatever the time zone.
 */
/** A due date as a local-noon timestamp of its calendar day (shared with the project task list). */
export { dueDayForDisplay } from '../../lib/dueDate';

export function isOverdue(due: string | null, now: Date): boolean {
  const day = due?.match(/^(\d{4}-\d{2}-\d{2})/)?.[1];
  return !!day && day < localDay(now);
}

/**
 * Merge the tasks I own and the ones I review (deduplicated), keep the open
 * ones, and order them: what needs me now, overdue, priority, due date, number.
 */
export function workItems(owned: readonly Task[], reviewing: readonly Task[], userId: string, now = new Date()): WorkItem[] {
  const seen = new Map<string, Task>();
  for (const t of [...owned, ...reviewing]) if (OPEN.has(t.status) && !seen.has(t.id)) seen.set(t.id, t);
  const items = [...seen.values()]
    .map((task) => {
      const step = taskNextStep(task, userId);
      return { task, step, reviewing: step === 'review', overdue: isOverdue(task.due_date, now) };
    })
    .filter((i) => i.step !== 'open');
  return items.sort(
    (a, b) =>
      STEP_RANK[a.step] - STEP_RANK[b.step] ||
      Number(b.overdue) - Number(a.overdue) ||
      PRIORITY_RANK[a.task.priority] - PRIORITY_RANK[b.task.priority] ||
      (a.task.due_date ?? '9999').localeCompare(b.task.due_date ?? '9999') ||
      a.task.number - b.task.number,
  );
}

// ─── "Waiting for me" (prototype todos()) ──────────────────────────────────

/**
 * Row types of the workbench inbox. Acceptance and approvals come from the
 * approval centre (kind task_acceptance vs engineering / stage review). The
 * prototype's "knowledge confirmation" has no list endpoint yet (only
 * POST /knowledge-usages/{id}/confirm), so the page shows it as not connected.
 */
export type PendingType = 'acceptance' | 'approval';

export const PENDING_ORDER: readonly PendingType[] = ['acceptance', 'approval'];

export function pendingTypeOf(kind: string): PendingType {
  return kind === 'task_acceptance' ? 'acceptance' : 'approval';
}

/** Acceptance first, then approvals; the server's order (newest first) inside each type. */
export function sortPending<T extends { kind: string }>(rows: readonly T[]): T[] {
  return PENDING_ORDER.flatMap((type) => rows.filter((r) => pendingTypeOf(r.kind) === type));
}

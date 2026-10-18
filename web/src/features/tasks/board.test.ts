import { describe, expect, it } from 'vitest';
import type { Milestone } from '../../api/pm';
import type { RunSummary } from '../../api/types';
import { boardColumn, boardNext, currentStageIndex, groupByColumn, latestRunByTask, ownsOrReviews, stageSteps, taskTally, type BoardTask } from './board';

const ME = 'me';

function task(over: Partial<BoardTask>): BoardTask {
  return {
    id: 't',
    project_id: 'p',
    number: 1,
    key: 'T-1',
    title: 'x',
    objective: '',
    owner_id: ME,
    reviewer_id: 'boss',
    milestone_id: null,
    part_id: null,
    priority: 'normal',
    status: 'todo',
    blocked_reason: null,
    start_date: null,
    due_date: null,
    current_baseline_id: null,
    version: 1,
    created_by: ME,
    created_at: null,
    updated_at: null,
    ...over,
  };
}

describe('board columns', () => {
  it('maps statuses to the four columns and leaves cancelled out', () => {
    expect(boardColumn('todo')).toBe('todo');
    expect(boardColumn('in_review')).toBe('in_review');
    expect(boardColumn('cancelled')).toBeNull();
    const g = groupByColumn([task({ id: 'a', status: 'done' }), task({ id: 'b', status: 'cancelled' }), task({ id: 'c', status: 'in_progress' })]);
    expect(g.done.map((t) => t.id)).toEqual(['a']);
    expect(g.in_progress.map((t) => t.id)).toEqual(['c']);
    expect(g.todo).toEqual([]);
  });

  it('filters tasks I own or accept', () => {
    expect(ownsOrReviews(task({ owner_id: ME }), ME)).toBe(true);
    expect(ownsOrReviews(task({ owner_id: 'x', reviewer_id: ME }), ME)).toBe(true);
    expect(ownsOrReviews(task({ owner_id: 'x', reviewer_id: 'y' }), ME)).toBe(false);
    expect(ownsOrReviews(task({}), null)).toBe(false);
  });
});

describe('next step on a card', () => {
  const ctx = { userId: ME, deliverables: null };
  it('only opens the decision page when it is my turn to accept', () => {
    const acc = { requestId: 'r1', canDecide: true, assigneeId: ME, agent: false };
    expect(boardNext(task({ status: 'in_review', owner_id: 'x', reviewer_id: ME }), { ...ctx, acceptance: acc })).toEqual({ kind: 'accept', requestId: 'r1' });
    expect(boardNext(task({ status: 'in_review' }), { ...ctx, acceptance: { ...acc, canDecide: false, assigneeId: 'boss' } })).toEqual({ kind: 'awaitAcceptance', reviewerId: 'boss', agent: false });
    // No request visible: fall back to the task's reviewer.
    expect(boardNext(task({ status: 'in_review' }), ctx)).toEqual({ kind: 'awaitAcceptance', reviewerId: 'boss', agent: false });
    // The independent reviewing agent decides: never a human button.
    expect(boardNext(task({ status: 'in_review' }), { ...ctx, acceptance: { ...acc, agent: true } })).toEqual({ kind: 'awaitAcceptance', reviewerId: ME, agent: true });
  });

  it('follows status, blockers and predecessors', () => {
    expect(boardNext(task({ status: 'done' }), ctx).kind).toBe('done');
    expect(boardNext(task({ status: 'cancelled' }), ctx).kind).toBe('cancelled');
    expect(boardNext(task({ status: 'todo' }), ctx)).toEqual({ kind: 'start', mine: true, ownerId: ME });
    expect(boardNext(task({ status: 'todo', owner_id: 'x' }), ctx)).toEqual({ kind: 'start', mine: false, ownerId: 'x' });
    expect(boardNext(task({ status: 'in_progress', blocked_reason: 'no stock' }), ctx)).toEqual({ kind: 'blocked', reason: 'no stock' });
    expect(boardNext(task({ status: 'todo', waiting_on: [{ id: 'p', key: 'T-2', status: 'in_progress' }] }), ctx)).toEqual({ kind: 'waitingOn', keys: ['T-2'] });
  });

  it('asks for deliverables before a submission (a finished run is not a finished task)', () => {
    expect(boardNext(task({ status: 'in_progress' }), ctx).kind).toBe('working');
    expect(boardNext(task({ status: 'in_progress' }), { ...ctx, deliverables: 0 }).kind).toBe('pickDeliverables');
    expect(boardNext(task({ status: 'in_progress' }), { ...ctx, deliverables: 2 })).toEqual({ kind: 'submit', count: 2, mine: true });
    expect(boardNext(task({ status: 'in_progress', owner_id: 'x' }), { ...ctx, deliverables: 1 })).toEqual({ kind: 'submit', count: 1, mine: false });
  });
});

describe('latest run of a task', () => {
  const run = (id: string, task_id: string | null, created_at: string) => ({ id, task_id, created_at, node_counts: {} }) as unknown as RunSummary;
  it('keeps the newest run per task and ignores runs without a task', () => {
    const m = latestRunByTask([run('a', 't1', '2026-10-01T00:00:00Z'), run('b', 't1', '2026-10-03T00:00:00Z'), run('c', null, '2026-10-05T00:00:00Z'), run('d', 't2', '2026-10-02T00:00:00Z')]);
    expect(m.get('t1')?.id).toBe('b');
    expect(m.get('t2')?.id).toBe('d');
    expect(m.size).toBe(2);
  });
});

describe('project strip', () => {
  const ms = (id: string, seq: number, status: Milestone['status']) => ({ id, seq, name: `S${seq}`, status });
  it('marks accepted stages done and the current one', () => {
    const steps = stageSteps([ms('c', 3, 'open'), ms('a', 1, 'accepted'), ms('b', 2, 'in_review'), ms('d', 4, 'open')], 'b');
    expect(steps.map((s) => `${s.seq}:${s.state}`)).toEqual(['1:done', '2:current', '3:todo', '4:todo']);
    expect(currentStageIndex(steps)).toBe(2);
    expect(currentStageIndex(stageSteps([ms('a', 1, 'accepted')], null))).toBe(0);
  });

  it('counts accepted, awaiting and all tasks but cancelled', () => {
    expect(taskTally({ todo: 2, in_progress: 1, in_review: 1, done: 3, cancelled: 4 })).toEqual({ accepted: 3, awaiting: 1, total: 7 });
    expect(taskTally({})).toEqual({ accepted: 0, awaiting: 0, total: 0 });
  });
});

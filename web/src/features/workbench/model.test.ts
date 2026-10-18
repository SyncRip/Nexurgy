import { describe, expect, it } from 'vitest';
import type { Task } from '../../api/pm';
import { dueDayForDisplay, isOverdue, pendingTypeOf, sortPending, taskNextStep, workItems } from './model';

const ME = 'me';

function task(over: Partial<Task>): Task {
  return {
    id: over.id ?? `t${over.number ?? 1}`,
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

describe('task next step', () => {
  it('follows the status for the owner', () => {
    expect(taskNextStep(task({ status: 'todo' }), ME)).toBe('start');
    expect(taskNextStep(task({ status: 'in_progress' }), ME)).toBe('execute');
    expect(taskNextStep(task({ status: 'in_review' }), ME)).toBe('awaitReview');
    expect(taskNextStep(task({ status: 'done' }), ME)).toBe('open');
  });
  it('puts a blocker first, except while the reviewer has it', () => {
    expect(taskNextStep(task({ status: 'in_progress', blocked_reason: 'no material' }), ME)).toBe('unblock');
    expect(taskNextStep(task({ status: 'in_review', blocked_reason: 'x' }), ME)).toBe('awaitReview');
  });
  it('asks the reviewer to review — never the owner reviewing their own task', () => {
    expect(taskNextStep(task({ status: 'in_review', owner_id: 'other', reviewer_id: ME }), ME)).toBe('review');
    expect(taskNextStep(task({ status: 'in_review', owner_id: ME, reviewer_id: ME }), ME)).toBe('awaitReview');
    expect(taskNextStep(task({ status: 'in_progress', owner_id: 'other', reviewer_id: ME }), ME)).toBe('open');
  });
});

describe('workbench task list', () => {
  const now = new Date(2026, 9, 9, 12);
  it('merges owned and reviewed tasks without duplicates and drops closed ones', () => {
    const a = task({ id: 'a', number: 1, status: 'in_progress' });
    const b = task({ id: 'b', number: 2, status: 'in_review', owner_id: 'other', reviewer_id: ME });
    const c = task({ id: 'c', number: 3, status: 'done' });
    const d = task({ id: 'd', number: 4, status: 'in_review', owner_id: 'other', reviewer_id: 'someone' });
    expect(workItems([a, c], [b, b, d], ME, now).map((i) => i.task.id)).toEqual(['b', 'a']);
  });
  it('orders by what needs me, overdue, priority, due date', () => {
    const items = workItems(
      [
        task({ id: 'wait', number: 1, status: 'in_review' }),
        task({ id: 'late', number: 2, status: 'todo', due_date: '2026-10-01' }),
        task({ id: 'urgent', number: 3, status: 'todo', priority: 'urgent' }),
        task({ id: 'plain', number: 4, status: 'todo' }),
        task({ id: 'blocked', number: 5, status: 'in_progress', blocked_reason: 'x' }),
      ],
      [],
      ME,
      now,
    );
    expect(items.map((i) => i.task.id)).toEqual(['blocked', 'late', 'urgent', 'plain', 'wait']);
    expect(items.find((i) => i.task.id === 'late')?.overdue).toBe(true);
  });
  it('treats today as not overdue', () => {
    expect(isOverdue('2026-10-09', now)).toBe(false);
    expect(isOverdue('2026-10-08', now)).toBe(true);
    expect(isOverdue(null, now)).toBe(false);
    expect(isOverdue('garbage', now)).toBe(false);
    expect(isOverdue('2026-10-09T00:00:00Z', now)).toBe(false);
  });
});

describe('due date display', () => {
  it('keeps the calendar day', () => {
    expect(dueDayForDisplay('2026-10-07T00:00:00Z')).toBe('2026-10-07T12:00:00');
    expect(new Date(dueDayForDisplay('2026-10-07')).getDate()).toBe(7);
  });
});

describe('waiting-for-me rows', () => {
  it('types acceptance apart from approvals and lists acceptance first, keeping order inside a type', () => {
    expect(pendingTypeOf('task_acceptance')).toBe('acceptance');
    expect(pendingTypeOf('engineering')).toBe('approval');
    expect(pendingTypeOf('milestone_review')).toBe('approval');
    const rows = [
      { id: 'e1', kind: 'engineering' },
      { id: 'a1', kind: 'task_acceptance' },
      { id: 'm1', kind: 'milestone_review' },
      { id: 'a2', kind: 'task_acceptance' },
    ];
    expect(sortPending(rows).map((r) => r.id)).toEqual(['a1', 'a2', 'e1', 'm1']);
  });
});

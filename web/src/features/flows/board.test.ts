import { describe, expect, it } from 'vitest';
import type { ApprovalRequest } from '../../api/approvals';
import type { RunSummary, Workflow } from '../../api/types';
import { buildBoard, countLabel, isOpenDraft, laneOfRun, needOf, progressOf, requestsByRun, runHref, runsNeedingDetail, runsPerWorkflow } from './board';

const run = (id: string, status: RunSummary['status'], extra: Partial<RunSummary> = {}): RunSummary => ({
  id,
  project_id: 'p',
  part_id: null,
  workflow_id: null,
  workflow_version: null,
  origin: 'workflow',
  title: id,
  status,
  cancel_requested: false,
  node_counts: {},
  created_at: '2026-10-01T00:00:00Z',
  started_at: null,
  finished_at: null,
  updated_at: '2026-10-01T00:00:00Z',
  ...extra,
});

const wf = (id: string, extra: Partial<Workflow> = {}): Workflow => ({
  id,
  project_id: 'p',
  name: id,
  description: '',
  graph: { nodes: [], edges: [], groups: [] },
  version: 1,
  template_key: null,
  created_at: '2026-10-01T00:00:00Z',
  updated_at: '2026-10-01T00:00:00Z',
  archived_at: null,
  validation: [],
  ...extra,
});

const req = (extra: Partial<ApprovalRequest>): ApprovalRequest =>
  ({ id: 'q', kind: 'engineering', stage: 'quote', status: 'pending', title: 'Quote review', assignee: null, run_id: 'r1', node_task: null, ...extra }) as ApprovalRequest;

describe('board lanes', () => {
  it('maps every run status to a lane', () => {
    expect(laneOfRun({ status: 'queued' })).toBe('running');
    expect(laneOfRun({ status: 'running' })).toBe('running');
    expect(laneOfRun({ status: 'waiting' })).toBe('attention');
    expect(laneOfRun({ status: 'failed' })).toBe('attention');
    expect(laneOfRun({ status: 'succeeded' })).toBe('done');
    expect(laneOfRun({ status: 'cancelled' })).toBe('done');
  });

  it('keeps only collaboration drafts that never started', () => {
    expect(isOpenDraft(wf('d1'), new Set())).toBe(true);
    expect(isOpenDraft(wf('d1', { published_count: 1 }), new Set())).toBe(false);
    expect(isOpenDraft(wf('d1'), new Set(['d1']))).toBe(false);
    expect(isOpenDraft(wf('d1', { archived_at: '2026-10-02T00:00:00Z' }), new Set())).toBe(false);
  });

  it('builds the columns, newest activity first', () => {
    const board = buildBoard(
      [
        run('old', 'waiting', { updated_at: '2026-10-01T00:00:00Z' }),
        run('new', 'failed', { updated_at: '2026-10-03T00:00:00Z' }),
        run('ok', 'succeeded'),
        run('collab', 'running', { workflow_id: 'started' }),
      ],
      [wf('open'), wf('started')],
    );
    expect(board.draft.map((x) => (x.kind === 'draft' ? x.workflow.id : ''))).toEqual(['open']);
    expect(board.running.map((x) => (x.kind === 'run' ? x.run.id : ''))).toEqual(['collab']);
    expect(board.attention.map((x) => (x.kind === 'run' ? x.run.id : ''))).toEqual(['new', 'old']);
    expect(board.done).toHaveLength(1);
  });
});

describe('node progress', () => {
  it('orders cells done → active → untouched', () => {
    const p = progressOf({ pending: 2, succeeded: 3, waiting_human: 1 });
    expect(p).toEqual({ total: 6, succeeded: 3, cells: ['succeeded', 'succeeded', 'succeeded', 'waiting_human', 'pending', 'pending'] });
  });

  it('drops the cells (bar only) for large graphs and ignores bad counts', () => {
    expect(progressOf({ succeeded: 20, pending: 5 }, 16).cells).toEqual([]);
    expect(progressOf({ succeeded: -1 }).total).toBe(0);
  });
});

describe('why a run needs someone', () => {
  it('names the pending approval stage and its assignee', () => {
    const byRun = requestsByRun([req({ assignee: { id: 'u', name: 'Zhao' } }), req({ id: 'q2', stage: 'process' }), req({ id: 'x', status: 'approved' })]);
    expect(byRun.get('r1')).toHaveLength(2);
    expect(needOf(run('r1', 'waiting', { node_counts: { waiting_human: 2 } }), byRun.get('r1'))).toEqual({ kind: 'approval', stage: 'quote', assignee: 'Zhao', more: 1 });
  });

  it('maps collaboration acceptances through their node task', () => {
    const byRun = requestsByRun([req({ kind: 'task_acceptance', stage: null, run_id: null, title: 'Doc', node_task: { run_id: 'c1', node_id: 'n', execution_mode: 'semi_auto', phase: 'in_review' } })]);
    expect(needOf(run('c1', 'waiting'), byRun.get('c1'))).toEqual({ kind: 'acceptance', title: 'Doc', assignee: null, more: 0 });
  });

  it('names the open question or manual entry of a waiting node', () => {
    const asking = [
      { status: 'succeeded' as const },
      { status: 'waiting_human' as const, review: { kind: 'clarification' as const, can_decide: true, clarification: { questions: ['Hole depth?', 'Thread?'], answers: [{ question: 'Hole depth?', answer: '12' }] } } },
    ];
    expect(needOf(run('r', 'waiting', { node_counts: { waiting_human: 1 } }), [], asking)).toEqual({ kind: 'question', text: 'Thread?', more: 0 });
    const entry = [{ status: 'waiting_human' as const, review: { kind: 'manual_entry' as const, can_decide: true } }];
    expect(needOf(run('r', 'waiting'), [], entry)).toEqual({ kind: 'manual_entry' });
    expect(runsNeedingDetail([run('a', 'waiting'), run('b', 'waiting'), run('c', 'failed')], new Map([['b', []]]))).toEqual(['a']);
  });

  it('falls back to node counts, and is quiet for healthy runs', () => {
    expect(needOf(run('r', 'waiting', { node_counts: { waiting_human: 1 } }))).toEqual({ kind: 'waiting', n: 1 });
    expect(needOf(run('r', 'failed', { node_counts: { failed: 2 } }))).toEqual({ kind: 'failed', n: 2 });
    expect(needOf(run('r', 'running', { cancel_requested: true }))).toEqual({ kind: 'cancelling' });
    expect(needOf(run('r', 'running'))).toBeNull();
    expect(needOf(run('r', 'succeeded'))).toBeNull();
  });
});

describe('card links', () => {
  it('opens collaboration runs, part runs and plain runs on their own pages', () => {
    expect(runHref(run('c1', 'running', { part_id: 'p1' }), new Set(['c1']))).toBe('/collaboration/runs/c1');
    expect(runHref(run('r1', 'running', { part_id: 'p1' }), new Set())).toBe('/parts/p1/workflow?run=r1');
    expect(runHref(run('r2', 'running'), new Set())).toBe('/runs/r2');
  });

  it('counts runs per template', () => {
    const m = runsPerWorkflow([run('a', 'succeeded', { workflow_id: 'w1' }), run('b', 'failed', { workflow_id: 'w1' }), run('c', 'running')]);
    expect(m.get('w1')).toBe(2);
    expect(m.size).toBe(1);
  });

  it('labels capped counts', () => {
    expect(countLabel(3, 100)).toBe('3');
    expect(countLabel(101, 100)).toBe('100+');
  });
});

import { describe, expect, it } from 'vitest';
import type { RunSummary } from '../../api/types';
import { WORKSPACE_GROUP, groupOfRun, groupRuns, pickRun } from './workflowModel';

function run(id: string, over: Partial<RunSummary>): RunSummary {
  return {
    id,
    project_id: 'p',
    part_id: 'part',
    workflow_id: null,
    workflow_version: null,
    origin: 'workflow',
    title: `run ${id}`,
    status: 'succeeded',
    cancel_requested: false,
    node_counts: {},
    created_at: '2026-10-01T00:00:00Z',
    started_at: null,
    finished_at: null,
    updated_at: '2026-10-01T00:00:00Z',
    ...over,
  };
}

describe('part runs grouped by workflow', () => {
  const runs = [
    run('a1', { workflow_id: 'wA', created_at: '2026-10-01T08:00:00Z' }),
    run('b1', { workflow_id: 'wB', created_at: '2026-10-02T08:00:00Z' }),
    run('a2', { workflow_id: 'wA', created_at: '2026-10-03T08:00:00Z', rerun_of: 'a1' }),
    run('ws', { origin: 'workspace', title: 'AI modeling', created_at: '2026-09-30T08:00:00Z' }),
  ];
  const names = new Map([['wA', 'Design to quote']]);

  it('groups by workflow, newest group and run first', () => {
    const g = groupRuns(runs, names);
    expect(g.map((x) => x.key)).toEqual(['wA', 'wB', WORKSPACE_GROUP]);
    expect(g[0].runs.map((r) => r.id)).toEqual(['a2', 'a1']);
    expect(g[0].latest.id).toBe('a2');
  });

  it('names groups by workflow, falling back to the latest run title', () => {
    const g = groupRuns(runs, names);
    expect(g[0].name).toBe('Design to quote');
    expect(g[1].name).toBe('run b1');
    expect(g[2].name).toBe('AI modeling');
    expect(g[2].workflowId).toBeNull();
  });

  it('keeps every run — history is never collapsed', () => {
    expect(groupRuns(runs, names).reduce((n, x) => n + x.runs.length, 0)).toBe(runs.length);
    expect(groupRuns([], names)).toEqual([]);
  });

  it('picks the requested run or the latest one', () => {
    const g = groupRuns(runs, names);
    expect(pickRun(g[0], 'a1')?.id).toBe('a1');
    expect(pickRun(g[0], 'b1')?.id).toBe('a2');
    expect(pickRun(g[0], null)?.id).toBe('a2');
    expect(pickRun(undefined, 'a1')).toBeUndefined();
    expect(groupOfRun(g, 'a1')?.key).toBe('wA');
    expect(groupOfRun(g, 'zz')).toBeUndefined();
  });
});

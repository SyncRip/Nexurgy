import { describe, expect, it } from 'vitest';
import type { RunSummary, Workflow } from '../../../api/types';
import { legend, partFlows, pickFlow, runNumber } from './model';

const run = (id: string, over: Partial<RunSummary>): RunSummary => ({
  id,
  project_id: 'p',
  part_id: 'part',
  workflow_id: null,
  workflow_version: null,
  origin: 'workflow',
  title: id,
  status: 'succeeded',
  cancel_requested: false,
  node_counts: {},
  created_at: '2026-10-01T00:00:00Z',
  started_at: null,
  finished_at: null,
  updated_at: '2026-10-01T00:00:00Z',
  ...over,
});
const wf = (id: string, name: string, updated: string): Workflow =>
  ({ id, project_id: 'p', name, description: '', graph: { nodes: [], edges: [], groups: [] }, version: 1, template_key: null, part_id: 'part', created_at: updated, updated_at: updated, archived_at: null, validation: [] }) as Workflow;

describe('part flows', () => {
  const own = [wf('pw1', 'BRK drawing to quote', '2026-10-05T00:00:00Z'), wf('pw2', 'New flow', '2026-10-09T00:00:00Z')];
  const runs = [
    run('a', { workflow_id: 'pw1', created_at: '2026-10-06T00:00:00Z' }),
    run('b', { workflow_id: 'pw1', created_at: '2026-10-07T00:00:00Z', rerun_of: 'a' }),
    run('t', { workflow_id: 'tpl', created_at: '2026-10-02T00:00:00Z' }),
    run('w', { origin: 'workspace', title: 'AI modeling', created_at: '2026-10-03T00:00:00Z' }),
  ];
  const flows = partFlows(own, runs, new Map([['tpl', 'Design to quote']]));

  it('merges own workflows (also without runs) with template and workspace runs, newest activity first', () => {
    expect(flows.map((f) => f.key)).toEqual(['pw2', 'pw1', 'workspace', 'tpl']);
    expect(flows.find((f) => f.key === 'pw2')?.runs).toEqual([]);
  });

  it('marks what is editable and where template runs came from', () => {
    const by = Object.fromEntries(flows.map((f) => [f.key, f]));
    expect(by.pw1.workflow?.id).toBe('pw1');
    expect(by.tpl.workflow).toBeNull();
    expect(by.tpl.templateId).toBe('tpl');
    expect(by.tpl.name).toBe('Design to quote');
    expect(by.workspace.templateId).toBeNull();
  });

  it('numbers runs oldest first and picks flows', () => {
    const pw1 = flows.find((f) => f.key === 'pw1')!;
    expect(runNumber(pw1, 'b')).toBe(2);
    expect(runNumber(pw1, 'a')).toBe(1);
    expect(runNumber(pw1, 'zz')).toBe(0);
    expect(pickFlow(flows, 'tpl', null)?.key).toBe('tpl');
    expect(pickFlow(flows, null, 'a')?.key).toBe('pw1');
    expect(pickFlow(flows, 'nope', null)?.key).toBe('pw2');
    expect(pickFlow([], null, null)).toBeUndefined();
  });

  it('builds the legend in a fixed order', () => {
    expect(legend({ failed: 1, succeeded: 3, pending: 0 })).toEqual([
      { status: 'succeeded', n: 3 },
      { status: 'failed', n: 1 },
    ]);
  });
});

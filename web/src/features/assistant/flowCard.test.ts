import { describe, expect, it } from 'vitest';
import type { NodeRun, NodeStatus, RunOutputRef, WorkflowRun } from '../../api/types';
import { pendingItems, resultItems, runDigest, runOutcome } from './flowCard';

// drawing → reconstruct → design review → process → process review → quote (ids chosen so id order ≠ graph order)
const CHAIN: [string, string][] = [
  ['z_file', 'file_input'],
  ['y_model', 'drawing_reconstruct'],
  ['x_design', 'human_review'],
  ['w_plan', 'process_plan'],
  ['v_process', 'human_review'],
  ['u_quote', 'quote'],
];

function nr(node_id: string, node_type: string, status: NodeStatus, extra: Partial<NodeRun> = {}): NodeRun {
  return {
    id: `nr-${node_id}`,
    node_id,
    node_type,
    status,
    attempt: 1,
    max_attempts: 2,
    inputs: {},
    outputs: {},
    error: null,
    steps: [],
    started_at: null,
    finished_at: null,
    updated_at: null,
    ...extra,
  };
}

function run(statuses: NodeStatus[], extra: Partial<WorkflowRun> = {}, nodes?: NodeRun[]): WorkflowRun {
  return {
    id: 'r1',
    status: 'running',
    cancel_requested: false,
    graph: {
      nodes: CHAIN.map(([id, type]) => ({ id, type, position: { x: 0, y: 0 }, params: {}, label: id === 'x_design' ? 'Design review' : null })),
      edges: CHAIN.slice(1).map(([id], i) => ({ id: `e${i}`, source: CHAIN[i][0], source_port: 'out', target: id, target_port: 'in' })),
      groups: [],
    },
    nodes: nodes ?? CHAIN.map(([id, type], i) => nr(id, type, statuses[i])).filter((n) => statuses[CHAIN.findIndex(([id]) => id === n.node_id)] !== 'pending'),
    outputs: [],
    started_at: '2026-10-12T10:00:00Z',
    finished_at: null,
    ...extra,
  } as WorkflowRun;
}

const ids = (d: ReturnType<typeof runDigest>) => d.segments.map((s) => `${s.nodeId}:${s.status}`);

describe('runDigest', () => {
  it('names what finished, what waits and what comes next, in graph order', () => {
    const d = runDigest(run(['succeeded', 'succeeded', 'waiting_human', 'pending', 'pending', 'pending']));
    expect(ids(d)).toEqual(['y_model:succeeded', 'x_design:waiting_human', 'w_plan:pending']);
    expect(d.more).toBe(2);
    expect(d.total).toBe(5); // the input node is never a step
    expect(d.segments[1].label).toBe('Design review');
  });

  it('keeps every step that needs attention, then the neighbours of the first', () => {
    const d = runDigest(run(['succeeded', 'succeeded', 'waiting_human', 'succeeded', 'failed', 'pending']));
    expect(ids(d)).toEqual(['y_model:succeeded', 'x_design:waiting_human', 'v_process:failed']);
    expect(d.more).toBe(2);
  });

  it('starts at the first step of a queued run and says when everything is done', () => {
    expect(ids(runDigest(run(['pending', 'pending', 'pending', 'pending', 'pending', 'pending'])))).toEqual(['y_model:pending', 'x_design:pending', 'w_plan:pending']);
    const done = runDigest(run(['succeeded', 'succeeded', 'succeeded', 'succeeded', 'skipped', 'succeeded'], { status: 'succeeded' }));
    expect(done).toEqual({ segments: [], more: 0, allDone: true, total: 5 });
  });
});

describe('pendingItems', () => {
  it('sorts waiting steps by review kind and failed steps in graph order', () => {
    const nodes = [
      nr('z_file', 'file_input', 'succeeded'),
      nr('y_model', 'drawing_reconstruct', 'waiting_human', { attempt: 2, review: { kind: 'clarification', can_decide: true, clarification: { questions: ['Hole depth?'], answers: [] } } }),
      nr('x_design', 'human_review', 'waiting_human', { review: { kind: 'approval', can_decide: false, stage: 'design', instructions: 'Check holes', target_hash: 'h1' } as NodeRun['review'] }),
      nr('w_plan', 'process_plan', 'failed', { error: { code: 'timeout', message: 'too slow', details: {} } }),
      nr('v_process', 'drawing_intake', 'waiting_human', { review: { kind: 'manual_entry', can_decide: true, manual_entry: { reason: 'manual_entry', prefill: {}, file_version_id: 'f1' } } }),
      nr('u_quote', 'quote', 'pending'),
    ];
    const items = pendingItems(run([], {}, nodes));
    expect(items.map((p) => `${p.nodeId}:${p.kind}`)).toEqual(['y_model:clarification', 'x_design:approval', 'w_plan:failed', 'v_process:manual_entry']);
    const [clar, approval, failed, manual] = items;
    expect(clar).toMatchObject({ questions: ['Hole depth?'], canDecide: true, key: 'y_model:2:waiting_human' });
    expect(approval).toMatchObject({ canDecide: false, stage: 'design', instructions: 'Check holes', targetHash: 'h1' });
    expect(failed).toMatchObject({ code: 'timeout', message: 'too slow', canRetry: true });
    expect(manual).toMatchObject({ reason: 'manual_entry' });
  });

  it('offers no retry once the run was cancelled', () => {
    const nodes = [nr('y_model', 'drawing_reconstruct', 'failed')];
    expect(pendingItems(run([], { status: 'cancelled' }, nodes))[0]).toMatchObject({ kind: 'failed', canRetry: false });
  });
});

describe('resultItems / runOutcome', () => {
  const out = (node_id: string, attempt: number, version: RunOutputRef['version'], output_key = 'model'): RunOutputRef => ({ node_id, output_key, attempt, asset_version_id: `av-${node_id}-${attempt}`, version });
  const model = (no: number) => ({ id: `av${no}`, asset_id: 'a-model', asset_type: 'part_model', asset_name: 'BRK-1', label: `R${no}`, version_no: no, availability: 'ready', labels: ['simulated'], target: { kind: 'model_revision', id: `rev${no}`, part_id: 'p1' } });

  it('keeps the latest attempt per step, in graph order, with version, availability and check', () => {
    const nodes = [
      nr('y_model', 'drawing_reconstruct', 'succeeded', { attempt: 2, outputs: { requirements_check: { status: 'passed' } } }),
      nr('u_quote', 'quote', 'succeeded'),
    ];
    const r = run([], {
      outputs: [
        out('u_quote', 1, { id: 'q', asset_id: 'a-q', asset_type: 'quote', asset_name: 'Quote', version_no: 4, availability: 'ready' }, 'quote'),
        out('y_model', 1, model(1)),
        out('y_model', 2, model(2)),
        out('w_plan', 1, null, 'plan'),
      ],
    }, nodes);
    const items = resultItems(r);
    expect(items.map((i) => `${i.nodeId}:${i.attempt}`)).toEqual(['y_model:2', 'w_plan:1', 'u_quote:1']);
    expect(items[0]).toMatchObject({ versionLabel: 'R2', availability: 'ready', labels: ['simulated'], check: 'passed', target: { kind: 'model_revision', id: 'rev2', partId: 'p1' } });
    expect(items[1]).toMatchObject({ missing: true, versionLabel: null, check: null });
    expect(items[2]).toMatchObject({ versionLabel: 'v4', assetType: 'quote', check: null });
  });

  it('summarises only a finished run', () => {
    expect(runOutcome(run(['succeeded', 'running', 'pending', 'pending', 'pending', 'pending']))).toBeNull();
    const done = run(['succeeded', 'succeeded', 'succeeded', 'failed', 'cancelled', 'cancelled'], { status: 'failed', finished_at: '2026-10-12T10:02:00Z', outputs: [out('y_model', 1, model(1))] });
    expect(runOutcome(done)).toEqual({ status: 'failed', total: 5, done: 2, failed: 1, results: 1, durationMs: 120000 });
  });
});

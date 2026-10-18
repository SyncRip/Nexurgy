import { describe, expect, it } from 'vitest';
import type { Drawing, RunSummary } from '../../api/types';
import { filterCounts, flowProgress, latestRunByPart, latestRunByTask, matchesFilter, partFlowSummaries, partRunPath, partStatusBreakdown, pickPreviewDrawing, runFlowName, runStateOf, viewCrop, viewPaths } from './model';

function run(over: Partial<RunSummary>): RunSummary {
  return {
    id: over.id ?? 'r',
    project_id: 'p',
    part_id: null,
    workflow_id: null,
    workflow_version: null,
    origin: 'workflow',
    title: 'Design to quote',
    status: 'succeeded',
    cancel_requested: false,
    node_counts: {},
    created_at: '2026-10-09T10:00:00Z',
    started_at: null,
    finished_at: null,
    updated_at: '2026-10-09T10:00:00Z',
    ...over,
  };
}

function drawing(over: Partial<Drawing>): Drawing {
  return {
    id: over.id ?? 'd',
    part_id: 'part',
    model_revision_id: 'rev2',
    rev_no: 2,
    kind: 'generated',
    file_version_id: null,
    svg_sha: 'sha',
    dxf_sha: null,
    config: {},
    views: [],
    node_run_id: null,
    created_at: '2026-10-09T10:00:00Z',
    ...over,
  };
}

describe('latest run per part / task', () => {
  it('keeps the newest run of each part and skips runs without a part', () => {
    const runs = [
      run({ id: 'a1', part_id: 'A', created_at: '2026-10-01T00:00:00Z' }),
      run({ id: 'a2', part_id: 'A', created_at: '2026-10-03T00:00:00Z' }),
      run({ id: 'b1', part_id: 'B', created_at: '2026-10-02T00:00:00Z' }),
      run({ id: 'x', part_id: null, created_at: '2026-10-05T00:00:00Z' }),
    ];
    const m = latestRunByPart(runs);
    expect([...m.keys()].sort()).toEqual(['A', 'B']);
    expect(m.get('A')?.id).toBe('a2');
    expect(m.get('B')?.id).toBe('b1');
  });
  it('breaks equal timestamps by id, independent of input order', () => {
    const same = '2026-10-01T00:00:00Z';
    const one = latestRunByPart([run({ id: 'r1', part_id: 'A', created_at: same }), run({ id: 'r2', part_id: 'A', created_at: same })]);
    const two = latestRunByPart([run({ id: 'r2', part_id: 'A', created_at: same }), run({ id: 'r1', part_id: 'A', created_at: same })]);
    expect(one.get('A')?.id).toBe(two.get('A')?.id);
  });
  it('groups by task the same way', () => {
    const m = latestRunByTask([run({ id: 'old', task_id: 'T', created_at: '2026-10-01T00:00:00Z' }), run({ id: 'new', task_id: 'T', created_at: '2026-10-02T00:00:00Z' }), run({ id: 'none', task_id: null })]);
    expect(m.size).toBe(1);
    expect(m.get('T')?.id).toBe('new');
  });
});

describe('run state', () => {
  it('maps run statuses', () => {
    expect(runStateOf(run({ status: 'queued' }))).toBe('queued');
    expect(runStateOf(run({ status: 'running' }))).toBe('running');
    expect(runStateOf(run({ status: 'waiting' }))).toBe('waiting');
    expect(runStateOf(run({ status: 'failed' }))).toBe('failed');
    expect(runStateOf(run({ status: 'cancelled' }))).toBe('cancelled');
    expect(runStateOf(run({ status: 'succeeded' }))).toBe('done');
    // S6: a partial run (a required step failed) is failed — never "running" or "done"
    expect(runStateOf(run({ status: 'partial' }))).toBe('failed');
  });
  it('says "unknown", not "no run", when the fetched window was full', () => {
    expect(runStateOf(undefined)).toBe('none');
    expect(runStateOf(undefined, true)).toBe('unknown');
  });
});

describe('flow progress', () => {
  it('counts only succeeded nodes as done (S6: failed / skipped never), finished segments first', () => {
    const p = flowProgress({ pending: 1, succeeded: 2, waiting_human: 1, skipped: 1, running: 1 });
    expect(p).toEqual({ done: 2, total: 6, segments: ['ok', 'ok', 'skip', 'run', 'wait', 'pend'] });
    expect(flowProgress({ succeeded: 2, failed: 1, skipped: 2 }).done).toBe(2);
  });
  it('matches the seeded run (7 done of 9, one waiting)', () => {
    const p = flowProgress({ pending: 1, succeeded: 7, waiting_human: 1 });
    expect([p.done, p.total]).toEqual([7, 9]);
  });
  it('handles empty and odd counts', () => {
    expect(flowProgress(undefined)).toEqual({ done: 0, total: 0, segments: [] });
    expect(flowProgress({ succeeded: -2, failed: 1.7 })).toEqual({ done: 0, total: 1, segments: ['err'] });
    // A status this client does not know still counts as an unfinished node.
    expect(flowProgress({ succeeded: 1, paused: 2 } as never)).toEqual({ done: 1, total: 3, segments: ['ok', 'pend', 'pend'] });
  });
});

describe('list filters', () => {
  it('puts what needs a person under "attention"', () => {
    expect(matchesFilter('waiting', 'attention')).toBe(true);
    expect(matchesFilter('failed', 'attention')).toBe(true);
    expect(matchesFilter('cancelled', 'attention')).toBe(true);
    expect(matchesFilter('none', 'attention')).toBe(true);
    expect(matchesFilter('running', 'attention')).toBe(false);
    expect(matchesFilter('done', 'attention')).toBe(false);
  });
  it('counts every part under "all", unknown ones nowhere else', () => {
    expect(filterCounts(['done', 'running', 'queued', 'waiting', 'none', 'unknown'])).toEqual({ all: 6, attention: 2, running: 2, done: 1 });
  });
});

describe('part summaries', () => {
  it('derives state and progress from the latest run of each part', () => {
    const runs = [run({ id: 'r', part_id: 'A', status: 'waiting', node_counts: { succeeded: 4, waiting_human: 1, pending: 3 } })];
    const s = partFlowSummaries([{ id: 'A' }, { id: 'B' }], runs, false);
    expect(s.get('A')).toMatchObject({ state: 'waiting', progress: { done: 4, total: 8 } });
    expect(s.get('A')?.run?.id).toBe('r');
    expect(s.get('B')).toEqual({ run: null, state: 'none', progress: null });
    expect(partFlowSummaries([{ id: 'B' }], runs, true).get('B')?.state).toBe('unknown');
  });
  it('names the workflow by the run title, else its template', () => {
    expect(runFlowName(run({ title: 'Design to quote' }))).toBe('Design to quote');
    expect(runFlowName(run({ title: ' ', template_version: { id: 'v', version_no: 1, status: 'published', workflow_id: 'w', name: 'Bracket review' } }))).toBe('Bracket review');
    expect(runFlowName(run({ title: '' }))).toBe('');
  });
  it('links to the part canvas at one run', () => {
    expect(partRunPath('/parts/abc', 'r 1')).toBe('/parts/abc?run=r%201');
  });
});

describe('thumbnail drawing', () => {
  it('uses the newest drawing of the latest revision only', () => {
    const list = [
      drawing({ id: 'old-rev', model_revision_id: 'rev1', created_at: '2026-10-09T12:00:00Z' }),
      drawing({ id: 'a', svg_sha: 'sha-a', created_at: '2026-10-08T00:00:00Z' }),
      drawing({ id: 'b', svg_sha: 'sha-b', created_at: '2026-10-09T00:00:00Z' }),
      drawing({ id: 'no-svg', svg_sha: null, created_at: '2026-10-10T00:00:00Z' }),
    ];
    expect(pickPreviewDrawing(list, 'rev2')).toMatchObject({ sha: 'sha-b', drawingId: 'b', revNo: 2 });
    // No drawing of the current revision → no picture of older geometry.
    expect(pickPreviewDrawing([drawing({ model_revision_id: 'rev1' })], 'rev2')).toBeNull();
    expect(pickPreviewDrawing(list, null)).toBeNull();
  });
  it('keeps the visible outline paths that start inside the crop, and only valid path data', () => {
    const svg = [
      '<svg viewBox="0 0 420 297"><g id="views">',
      '<path class="hid" d="M230 50 L240 60"/>',
      '<path class="vis" d="M226.886 84.387 L226.886 76.222"/>',
      '<path d="M230.5 60 L283.454 43.562" class="vis"/>',
      '<path class="vis" d="M59.193 191.438 L59.193 181.438"/>',
      '<path class="vis" d="M230 50 L240 60&quot;/><script>"/>',
      '<path class="vis" data-d="M1 1" d="M240,70 A5,5 0 0 1 250,70 Z"/>',
      '</g><g id="dims"><path class="dim" d="M230 50 L300 50"/></g></svg>',
    ].join('\n');
    expect(viewPaths(svg, [218, 37, 158, 104])).toEqual(['M226.886 84.387 L226.886 76.222', 'M230.5 60 L283.454 43.562', 'M240,70 A5,5 0 0 1 250,70 Z']);
    expect(viewPaths(svg, [218, 37, 158, 104], 1)).toHaveLength(1);
    expect(viewPaths('<svg/>', [0, 0, 10, 10])).toEqual([]);
  });
  it('crops to the iso view with a margin', () => {
    const views: Drawing['views'] = [
      { name: 'front', scale: 1, bbox: [59, 181, 179, 191] },
      { name: 'iso', scale: 1, bbox: [200, 40, 300, 140] },
    ];
    expect(viewCrop(views)).toEqual([194, 34, 112, 112]);
    expect(viewCrop([{ name: 'front', scale: 1, bbox: [0, 0, 50, 10] }])).toEqual([-3, -3, 56, 16]);
    expect(viewCrop([{ name: 'iso', scale: 1, bbox: [5, 5, 5, 9] }])).toBeNull();
    expect(viewCrop([])).toBeNull();
    expect(viewCrop(undefined)).toBeNull();
  });
});

describe('part status breakdown', () => {
  it('counts every status in a fixed order and keeps empty ones', () => {
    const parts = [
      { status: 'draft' as const },
      { status: 'released' as const },
      { status: 'released' as const },
      { status: 'in_design' as const },
    ];
    const { total, slices } = partStatusBreakdown(parts);
    expect(total).toBe(4);
    // PART_STATUSES order is preserved; in_review / archived stay at zero.
    expect(slices.map((s) => s.status)).toEqual(['draft', 'in_design', 'in_review', 'released', 'archived']);
    expect(slices.map((s) => s.count)).toEqual([1, 1, 0, 2, 0]);
    expect(slices.find((s) => s.status === 'released')?.fraction).toBeCloseTo(0.5);
  });
  it('returns zero fractions and total for no parts', () => {
    const { total, slices } = partStatusBreakdown([]);
    expect(total).toBe(0);
    expect(slices).toHaveLength(5);
    expect(slices.every((s) => s.count === 0 && s.fraction === 0)).toBe(true);
  });
});

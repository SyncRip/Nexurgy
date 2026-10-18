import { describe, expect, it } from 'vitest';
import type { HoleRecord } from '../../api/types';
import type { EdgePick, FacePick } from '../../three/viewer';
import {
  EXCERPT_MAX,
  cadSelectionRef,
  clampExcerpt,
  criterionRef,
  distanceToAxis,
  fileVersionRef,
  isDrawingFileKind,
  isExcerptSourceKind,
  matchHoleCandidates,
  partRef,
  partRevisionRef,
  quoteFieldRef,
  quoteRef,
  runOutputRef,
  runRef,
  taskRef,
  textExcerptRef,
  assetVersionRef,
} from './refs';

const P = 'proj-1';

function hole(over: Partial<HoleRecord> & Pick<HoleRecord, 'op_id' | 'diameter'>): HoleRecord {
  return {
    depth: null,
    through: true,
    axis: [0, 0, -1],
    entry: [10, 10, 20],
    count: 1,
    counterbore: null,
    countersink: null,
    positions: [[10, 10, 20]],
    ...over,
  };
}

const face = (over: Partial<FacePick> = {}): FacePick => ({
  kind: 'face',
  index: 7,
  id: 12,
  surface: 'cylinder',
  normal: [1, 0, 0],
  planar: false,
  center: [10, 10, 10],
  area: 376.99111,
  radius: 3,
  point: [13, 10, 12.5],
  ...over,
});

describe('reference builders', () => {
  it('build plain object references with the project', () => {
    expect(partRef(P, 'part-1')).toEqual({ kind: 'part', object_id: 'part-1', project_id: P });
    expect(partRevisionRef(P, 'rev-3')).toEqual({ kind: 'part_revision', object_id: 'rev-3', project_id: P });
    expect(quoteRef(P, 'q-1')).toEqual({ kind: 'quote', object_id: 'q-1', project_id: P });
    expect(assetVersionRef(P, 'av-1')).toEqual({ kind: 'asset_version', object_id: 'av-1', project_id: P });
    expect(runRef(P, 'run-1')).toEqual({ kind: 'run', object_id: 'run-1', project_id: P });
    expect(taskRef(P, 'task-1')).toEqual({ kind: 'task', object_id: 'task-1', project_id: P });
    expect(criterionRef(P, 'c-1')).toEqual({ kind: 'criterion', object_id: 'c-1', project_id: P });
    expect(fileVersionRef(P, 'fv-1')).toEqual({ kind: 'file_version', object_id: 'fv-1', project_id: P });
  });

  it('quote_field sends only tier and key (the server reads the value)', () => {
    const r = quoteFieldRef(P, 'q-1', 50, 'material');
    expect(r).toEqual({ kind: 'quote_field', object_id: 'q-1', project_id: P, field: { tier_qty: 50, key: 'material' } });
    expect(r.field).not.toHaveProperty('value');
    expect(quoteFieldRef(P, 'q-1', 1, 'lead_days').field).toEqual({ tier_qty: 1, key: 'lead_days' });
  });

  it('run_output points at the exact asset version and its run / node / output key', () => {
    expect(runOutputRef(P, 'run-9', { node_id: 'model', output_key: 'model', asset_version_id: 'av-4' })).toEqual({
      kind: 'run_output',
      object_id: 'av-4',
      project_id: P,
      output: { run_id: 'run-9', node_id: 'model', output_key: 'model' },
    });
  });

  it('drawing files are pdf / image only', () => {
    expect(isDrawingFileKind('drawing_pdf')).toBe(true);
    expect(isDrawingFileKind('drawing_image')).toBe(true);
    expect(isDrawingFileKind('drawing_dxf')).toBe(false);
    expect(isDrawingFileKind('model_step')).toBe(false);
  });
});

describe('text excerpts', () => {
  it('trims and refuses empty text', () => {
    expect(textExcerptRef(P, 'quote', 'q-1', '   \n ')).toBeNull();
    expect(textExcerptRef(P, 'task', 't-1', '  材料费偏高  ')).toEqual({
      kind: 'text_excerpt',
      object_id: 't-1',
      project_id: P,
      excerpt: { source_kind: 'task', text: '材料费偏高' },
    });
  });

  it('caps at the server limit in code points', () => {
    const long = '孔'.repeat(EXCERPT_MAX + 50);
    const c = clampExcerpt(long)!;
    expect(c.truncated).toBe(true);
    expect(Array.from(c.text)).toHaveLength(EXCERPT_MAX);
    const emoji = '🔩'.repeat(EXCERPT_MAX); // 2 UTF-16 units each
    const e = clampExcerpt(emoji)!;
    expect(e.truncated).toBe(false);
    expect(e.text).toBe(emoji);
    expect(clampExcerpt('ok')).toEqual({ text: 'ok', truncated: false });
  });

  it('knows the excerpt source kinds', () => {
    expect(isExcerptSourceKind('criterion')).toBe(true);
    expect(isExcerptSourceKind('run')).toBe(false);
    expect(isExcerptSourceKind(null)).toBe(false);
  });
});

describe('cad_selection', () => {
  const rev = { id: 'rev-3', build_key: 'bk-abc', mesh_sha: 'sha-xyz' };

  it('pins a face to the revision build and mesh', () => {
    const r = cadSelectionRef(P, rev, face({ point: [13.000049, 10, 12.5], normal: [1, -0, 0] }));
    expect(r.kind).toBe('cad_selection');
    expect(r.object_id).toBe('rev-3');
    expect(r.selection).toMatchObject({
      entity: 'face',
      id: 12,
      build_key: 'bk-abc',
      mesh_sha: 'sha-xyz',
      units: 'mm',
      frame: 'world_mm_z_up',
      point: [13, 10, 12.5],
      normal: [1, 0, 0],
      surface: 'cylinder',
      radius: 3,
      area: 376.9911,
    });
    expect(r.selection).not.toHaveProperty('candidates');
    expect(r.selection).not.toHaveProperty('resolved_op_id');
    expect(Object.is(r.selection!.normal![1], -0)).toBe(false);
  });

  it('pins an edge with its kind and length', () => {
    const e: EdgePick = { kind: 'edge', index: 3, id: 5, edgeKind: 'circle', length: 18.84956, start: [0, 0, 0], end: [0, 0, 0], point: [1, 2, 3] };
    const r = cadSelectionRef(P, rev, e);
    expect(r.selection).toMatchObject({ entity: 'edge', id: 5, edge_kind: 'circle', length: 18.8496, point: [1, 2, 3], build_key: 'bk-abc', mesh_sha: 'sha-xyz' });
    expect(r.selection).not.toHaveProperty('surface');
  });

  it('carries hole candidates and the chosen op', () => {
    const candidates = [{ op_id: 'F-03', kind: 'hole', label: 'F-03 Ø6', diameter: 6, reason: 'r' }];
    const r = cadSelectionRef(P, rev, face(), { candidates, resolvedOpId: 'F-03' });
    expect(r.selection!.candidates).toEqual(candidates);
    expect(r.selection!.resolved_op_id).toBe('F-03');
  });
});

describe('matchHoleCandidates', () => {
  it('distance to an axis line', () => {
    expect(distanceToAxis([3, 4, 9], [0, 0, 0], [0, 0, 1])).toBeCloseTo(5);
  });

  it('matches a cylinder of the hole radius on the hole axis', () => {
    const c = matchHoleCandidates(face(), [hole({ op_id: 'F-03', diameter: 6 })]);
    expect(c).toHaveLength(1);
    expect(c[0]).toMatchObject({ op_id: 'F-03', kind: 'hole', label: 'F-03 Ø6', diameter: 6 });
    expect(c[0].reason).toContain('Ø6');
  });

  it('accepts 0.05 mm or 1 %, whichever is larger', () => {
    const h = hole({ op_id: 'F-03', diameter: 6 });
    expect(matchHoleCandidates(face({ radius: 3.049 }), [h])).toHaveLength(1);
    expect(matchHoleCandidates(face({ radius: 3.06 }), [h])).toHaveLength(0);
    const big = hole({ op_id: 'F-04', diameter: 20, positions: [[10, 10, 20]] }); // r 10 → 1 % = 0.1 mm
    expect(matchHoleCandidates(face({ radius: 10.09, point: [20.09, 10, 12] }), [big])).toHaveLength(1);
    expect(matchHoleCandidates(face({ radius: 10.11, point: [20.11, 10, 12] }), [big])).toHaveLength(0);
  });

  it('needs the pick within radius + 0.2 mm of an instance axis', () => {
    const h = hole({ op_id: 'F-03', diameter: 6 });
    expect(matchHoleCandidates(face({ point: [13.19, 10, 5] }), [h])).toHaveLength(1);
    expect(matchHoleCandidates(face({ point: [13.25, 10, 5] }), [h])).toHaveLength(0);
  });

  it('finds the nearest instance of a pattern', () => {
    const h = hole({ op_id: 'F-05', diameter: 6, count: 4, positions: [[0, 0, 20], [50, 0, 20], [0, 50, 20], [50, 50, 20]] });
    const c = matchHoleCandidates(face({ point: [53, 50, 10] }), [h], { describe: (m) => `${m.instance}/${m.count}` });
    expect(c).toHaveLength(1);
    expect(c[0].label).toBe('F-05 Ø6 ×4');
    expect(c[0].reason).toBe('4/4');
  });

  it('returns several candidates nearest first when holes overlap', () => {
    const a = hole({ op_id: 'F-03', diameter: 6, positions: [[10, 10, 20]] });
    const b = hole({ op_id: 'F-07', diameter: 6.04, positions: [[10.1, 10, 20]] });
    const c = matchHoleCandidates(face({ point: [13.1, 10, 12] }), [a, b]);
    expect(c.map((x) => x.op_id)).toEqual(['F-07', 'F-03']);
  });

  it('matches a counterbore cylinder', () => {
    const h = hole({ op_id: 'F-08', diameter: 6, counterbore: { diameter: 11, depth: 6 } });
    const c = matchHoleCandidates(face({ radius: 5.5, point: [15.5, 10, 18] }), [h], { describe: (m) => m.feature });
    expect(c).toHaveLength(1);
    expect(c[0]).toMatchObject({ op_id: 'F-08', label: 'F-08 ⌴Ø11', diameter: 6, reason: 'counterbore' });
  });

  it('ignores non-cylinders, missing radius and axis mismatch', () => {
    const h = hole({ op_id: 'F-03', diameter: 6 });
    expect(matchHoleCandidates(face({ surface: 'plane' }), [h])).toEqual([]);
    expect(matchHoleCandidates(face({ radius: undefined }), [h])).toEqual([]);
    expect(matchHoleCandidates(face(), [])).toEqual([]);
    // Same radius but the picked cylinder runs along X: not this hole.
    expect(matchHoleCandidates(face(), [h], { faceAxis: { direction: [1, 0, 0] } })).toEqual([]);
    expect(matchHoleCandidates(face(), [h], { faceAxis: { direction: [0, 0, 1] } })).toHaveLength(1);
  });

  it('falls back to the entry point when positions are missing', () => {
    const h = hole({ op_id: 'F-03', diameter: 6, positions: [] });
    expect(matchHoleCandidates(face(), [h])).toHaveLength(1);
  });
});

describe('matchHoleCandidates limits', () => {
  it('returns at most 20 candidates (server limit)', () => {
    const holes = Array.from({ length: 25 }, (_, i) => hole({ op_id: `F-${String(i + 10)}`, diameter: 6 }));
    expect(matchHoleCandidates(face(), holes)).toHaveLength(20);
  });
});

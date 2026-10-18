import { describe, expect, it } from 'vitest';
import type { DimensionEntity, DraftContent, DraftEntity } from '../../api/drafting';
import { applyLocal, blockFromSelection, newIds } from './commands';
import { dimensionGeometry, displayedText, distanceTo, fmtNum, hatchAt, inWindow, measure, numbersIn, pick, readingConsistent, transformEntity } from './geometry';
import { SNAP_KINDS, entitySnaps, findSnap, intersect, primitives } from './snaps';

const line = (id: string, p1: [number, number], p2: [number, number]): DraftEntity => ({ id, type: 'line', layer: '0', origin: 'person', p1, p2 });
const circle = (id: string, c: [number, number], r: number): DraftEntity => ({ id, type: 'circle', layer: '0', origin: 'person', center: c, radius: r });
const dim = (d: Partial<DimensionEntity>): DimensionEntity => ({ id: 'D', type: 'dimension', layer: 'DIM', origin: 'person', angle: 0, dimtype: 'linear', ...d }) as DimensionEntity;

const content = (entities: DraftEntity[]): DraftContent => ({
  sheets: [],
  layers: [{ name: '0', color: 7, linetype: 'CONTINUOUS', lineweight: -3, visible: true, locked: false }],
  entities,
  blocks: [],
  underlays: [],
  provenance: {},
  conflicts: [],
  assumptions: [],
  open_questions: [],
});

describe('assumptions', () => {
  it('a pending confirm_assumption marks the AI value confirmed in the preview only', () => {
    const base = content([]);
    base.assumptions = [{ id: 'A-01', subject: 'W.value', field: 'value', adopted: 100, unit: 'mm', basis: '', alternatives: [102], affected: [], sources: [], author: 'ai_turbo', at: '' }];
    const next = applyLocal(base, [{ op: 'confirm_assumption', assumption_id: 'A-01' }]);
    expect(next.assumptions[0].confirmation).toMatchObject({ author: 'person', decision: 'confirmed', value: 100 });
    expect(base.assumptions[0].confirmation).toBeUndefined();
  });
});

describe('dimensions', () => {
  it('measures like the server and keeps the override apart from the measurement', () => {
    expect(measure(dim({ p1: [0, 0], p2: [100, 20], location: [50, -10] })).value).toBe(100);
    expect(measure(dim({ dimtype: 'linear', angle: 90, p1: [0, 0], p2: [100, 20], location: [-10, 10] })).value).toBe(20);
    expect(measure(dim({ dimtype: 'aligned', p1: [0, 0], p2: [30, 40], location: [0, 0] })).value).toBe(50);
    expect(measure(dim({ dimtype: 'angular', center: [0, 0], p1: [10, 0], p2: [0, 10], location: [5, 5] }))).toEqual({ value: 90, unit: 'deg' });
    expect(displayedText(dim({ dimtype: 'diameter', center: [0, 0], radius: 4, text_override: '4×Ø<>' }))).toBe('4×Ø8');
    expect(displayedText(dim({ dimtype: 'radius', center: [0, 0], radius: 2.5 }))).toBe('R2.5');
    expect(displayedText(dim({ p1: [0, 0], p2: [100, 0], location: [0, -5], text_override: '102' }))).toBe('102');
    expect(readingConsistent('102', 100)).toBe(false);
    expect(readingConsistent('4×Ø8', 8)).toBe(true);
    expect(readingConsistent('SEE NOTE', 8)).toBeNull();
    expect(numbersIn('Ø10±0.1')).toEqual([[10, 0]]);
    expect(fmtNum(12.5)).toBe('12.5');
    expect(fmtNum(116.619)).toBe('116.62');
  });

  it('draws a linear dimension line at its location with arrows at both ends', () => {
    const g = dimensionGeometry(dim({ p1: [0, 0], p2: [100, 0], location: [50, -12] }));
    expect(g.lines.at(-1)).toEqual([
      [0, -12],
      [100, -12],
    ]);
    expect(g.arrows).toHaveLength(2);
    expect(g.text.value).toBe('100');
  });
});

describe('picking and transforms', () => {
  it('picks the nearest entity and box-selects whole entities', () => {
    const ents = [line('L', [0, 0], [10, 0]), circle('C', [20, 0], 5)];
    expect(pick(ents, [5, 0.2], 0.5)?.id).toBe('L');
    expect(pick(ents, [25, 0.1], 0.5)?.id).toBe('C');
    expect(pick(ents, [50, 50], 0.5)).toBeNull();
    expect(inWindow(ents, [-1, -1], [11, 1])).toEqual(['L']);
    expect(distanceTo(circle('C', [0, 0], 5), [8, 0])).toBe(3);
  });

  it('rotates and moves rigidly (no scaling)', () => {
    const moved = transformEntity(line('L', [0, 0], [10, 0]), 5, 0, 90, [0, 0]);
    expect(moved.type === 'line' && moved.p1).toEqual([5, 0]);
    expect(moved.type === 'line' && moved.p2).toEqual([5, 10]);
    const d = transformEntity(dim({ dimtype: 'diameter', center: [0, 0], radius: 4, angle: 45 }), 0, 0, 90, [0, 0]);
    expect(d.angle).toBe(135);
  });
});

describe('hatch boundaries', () => {
  it('uses the smallest closed outline around the click and keeps holes inside it as islands', () => {
    const plate: DraftEntity = { id: 'P', type: 'polyline', layer: '0', origin: 'person', closed: true, points: [[0, 0], [100, 0], [100, 60], [0, 60]] };
    const ents = [plate, circle('H1', [10, 10], 4), circle('H2', [90, 50], 4), circle('OUT', [200, 0], 4)];
    const h = hatchAt(ents, [50, 30]);
    expect(h?.refs).toEqual(['P', 'H1', 'H2']);
    expect(h?.loops[0].points).toHaveLength(4);
    expect(hatchAt(ents, [10, 10])?.refs).toEqual(['H1']); // clicking inside a hole hatches the hole
    expect(hatchAt(ents, [500, 500])).toBeNull();
  });
});

describe('snaps', () => {
  const all = new Set(SNAP_KINDS);
  it('finds endpoint, midpoint, centre and intersection snaps', () => {
    const ents = [line('A', [0, 0], [10, 10]), line('B', [0, 10], [10, 0]), circle('C', [30, 0], 4)];
    expect(findSnap(ents, [0.1, 0.1], 1, all, distanceTo)).toMatchObject({ kind: 'endpoint', point: [0, 0] });
    expect(findSnap(ents, [5.2, 4.8], 1, all, distanceTo)).toMatchObject({ kind: 'intersection', ids: ['A', 'B'] });
    expect(findSnap(ents, [30.2, 0.1], 1, all, distanceTo)).toMatchObject({ kind: 'center', point: [30, 0] });
    expect(findSnap(ents, [2.6, 2.4], 1, new Set(['midpoint'] as const), distanceTo)).toBeNull();
    expect(findSnap([line('M', [0, 0], [10, 0])], [5.1, 0.2], 1, all, distanceTo)).toMatchObject({ kind: 'midpoint', point: [5, 0] });
    expect(entitySnaps(circle('C', [0, 0], 2)).filter((s) => s.kind === 'quadrant')).toHaveLength(4);
  });

  it('intersects lines with circles and circles with circles', () => {
    const [seg] = primitives(line('L', [-10, 0], [10, 0]));
    const [circ] = primitives(circle('C', [0, 0], 5));
    const pts = intersect(seg, circ).map((p) => p.map((v) => Math.round(v * 1e6) / 1e6));
    expect(pts).toEqual([
      [-5, 0],
      [5, 0],
    ]);
    expect(intersect(circ, primitives(circle('D', [8, 0], 5))[0])).toHaveLength(2);
  });
});

describe('local commands', () => {
  it('assigns ids up front, moves, copies and keeps provenance of overrides', () => {
    const base = content([line('E-0001', [0, 0], [10, 0])]);
    expect(newIds(base, 2)).toEqual(['E-0002', 'E-0003']);
    const next = applyLocal(base, [
      { op: 'add_entities', entities: [{ type: 'dimension', dimtype: 'linear', p1: [0, 0], p2: [10, 0], location: [5, -5], angle: 0, layer: 'DIM' } as Partial<DraftEntity>] },
      { op: 'transform', ids: ['E-0001'], dx: 0, dy: 5, copy: true },
      { op: 'edit_dimension', id: 'E-0002', text_override: '12' },
    ]);
    expect(next.entities.map((e) => e.id)).toEqual(['E-0001', 'E-0002', 'E-0003']);
    expect(next.entities[2].type === 'line' && next.entities[2].p1).toEqual([0, 5]);
    expect(next.provenance['E-0002']).toMatchObject({ measured: 10, displayed: '12', consistent: false, flags: ['reading_mismatch'], override: { author: 'person' } });
    expect(base.entities).toHaveLength(1); // pure
  });

  it('builds a block from the selection relative to its base point', () => {
    const cmd = blockFromSelection('BOLT', [10, 10], [circle('X', [10, 10], 3), line('Y', [7, 10], [13, 10])]);
    expect(cmd.op).toBe('define_block');
    if (cmd.op === 'define_block') {
      expect(cmd.block.entities.map((e) => e.id)).toEqual(['B1', 'B2']);
      expect(cmd.block.entities[0].type === 'circle' && cmd.block.entities[0].center).toEqual([0, 0]);
    }
  });
});

import { describe, expect, it } from 'vitest';
import type { ModelOp } from '../../api/types';
import { defaultGeometryOp, templateParamIssue, translatePlane, validateLoop, validatePath } from './geometry';
import { defaultOp, translateBody, validateOps, type BodyOp } from './ops';

const ctx = { first: false, mode: 'add' as const, top: 10, bbox: null, lastBodyId: 'F-01', lastTargetId: 'F-01' };

function issues(loop: Parameters<typeof validateLoop>[0], closed = true) {
  const out: string[] = [];
  (closed ? validateLoop : validatePath)(loop, 'profile', (_f, key) => out.push(key));
  return out;
}

describe('W3 geometry helpers', () => {
  it('flags an arc whose ends are not on one circle, accepts the GEN-03 bracket section', () => {
    expect(issues({ start: [0, 0], segments: [{ kind: 'line', to: [10, 0] }, { kind: 'arc', to: [10, 9], center: [10, 5] }] })).toContain('geo.v.arcRadius');
    const gen03 = {
      start: [60, 0] as [number, number],
      segments: [
        { kind: 'line' as const, to: [6, 0] as [number, number] },
        { kind: 'arc' as const, to: [0, 6] as [number, number], center: [6, 6] as [number, number], ccw: false },
        { kind: 'line' as const, to: [0, 44] as [number, number] },
        { kind: 'line' as const, to: [4, 44] as [number, number] },
        { kind: 'line' as const, to: [4, 6] as [number, number] },
        { kind: 'arc' as const, to: [6, 4] as [number, number], center: [6, 6] as [number, number], ccw: true },
        { kind: 'line' as const, to: [60, 4] as [number, number] },
      ],
    };
    expect(issues(gen03)).toEqual([]);
  });

  it('requires a tangent-continuous sweep path', () => {
    expect(issues({ start: [0, 0], segments: [{ kind: 'line', to: [10, 0] }, { kind: 'line', to: [10, 10] }] }, false)).toContain('geo.v.tangent');
    expect(issues({ start: [0, 0], segments: [{ kind: 'line', to: [30, 0] }, { kind: 'arc', to: [40, 10], center: [30, 10], ccw: true }] }, false)).toEqual([]);
  });

  it('gives valid defaults for every new op', () => {
    const ops: ModelOp[] = [defaultOp('box', 'F-01', { hasBase: false, bodies: 0 })];
    for (const type of ['revolve', 'sweep', 'bent_plate', 'cut', 'shell', 'pattern'] as const) ops.push(defaultGeometryOp(type, `F-0${ops.length + 1}`, ctx));
    expect(validateOps(ops, false)).toEqual({});
  });

  it('flags a pattern without an earlier target and a datum ref to a later op', () => {
    const ops: ModelOp[] = [
      defaultOp('box', 'F-01', { hasBase: false, bodies: 0 }),
      { id: 'F-02', type: 'pattern', target: 'F-09', layout: { kind: 'linear', direction: [1, 0, 0], spacing: 10, count: 2 } },
      { id: 'F-03', type: 'cut', profile: { kind: 'circle', radius: 2 }, plane: { ref: 'F-04.top' }, depth: 2 },
    ];
    const out = validateOps(ops, false);
    expect(out['F-02'].map((i) => i.key)).toContain('geo.v.patternTarget');
    expect(out['F-03'].map((i) => i.key)).toContain('geo.v.datumRef');
  });

  it('moves workplane bodies by moving their plane', () => {
    const bent = defaultGeometryOp('bent_plate', 'F-02', ctx) as BodyOp;
    const moved = translateBody(bent, [1, 2, 3]) as typeof bent & { plane: { origin: number[] } };
    expect(moved.plane.origin).toEqual([41, -13, 13]);
    expect(translatePlane('XZ', [0, 5, 0])).toEqual({ origin: [0, 5, 0], normal: [0, -1, 0], x_dir: [1, 0, 0] });
    expect(translatePlane({ ref: 'F-01.top' }, [1, 0, 0])).toBeNull();
  });

  it('checks template parameters by type and range', () => {
    const spec = { name: 'length', type: 'float' as const, default: 30, unit: 'mm', min: 4, max: 400, label: { en: '', zh: '' } };
    expect(templateParamIssue(spec, 3)?.key).toBe('geo.tpl.v.range');
    expect(templateParamIssue(spec, 30)).toBeNull();
    expect(templateParamIssue({ ...spec, type: 'choice', choices: ['M8'] }, 'M7')?.key).toBe('geo.tpl.v.choice');
    expect(templateParamIssue({ ...spec, type: 'int' }, 2.5)?.key).toBe('geo.tpl.v.integer');
  });
});

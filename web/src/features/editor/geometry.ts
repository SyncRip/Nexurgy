// Pure helpers for the W3 geometry-library ops (mirror of server/app/cad/sketch.py and the new ops in ops.py):
// planes, sketches, defaults and early client-side validation. The server stays the authority (preview / save
// re-validate and the kernel builds). Units mm and degrees, world +Z up.

import type { BentPlateOp, CutOp, DatumRef, PatternOp, PlaneSpec, RevolveOp, Segment, ShellOp, SketchLoop, SketchProfile, SweepOp, TemplateParamSpec, Workplane } from '../../api/geometry';
import type { ModelOp, ModelOpType, Vec2, Vec3 } from '../../api/types';
import type { MessageKey } from '../../i18n/en';

type Push = (field: string, key: MessageKey, vars?: Record<string, string | number>) => void;
type Check = (field: string, v: unknown) => void;

const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);
const r3 = (v: number) => Math.round(v * 1000) / 1000 + 0;

// ─── planes ─────────────────────────────────────────────────────────────────

export const isNamedPlane = (p: PlaneSpec | undefined | null): p is 'XY' | 'XZ' | 'YZ' => p === 'XY' || p === 'XZ' || p === 'YZ';
export const isDatumRef = (p: PlaneSpec | undefined | null): p is DatumRef => !!p && typeof p === 'object' && 'ref' in p;
export const isWorkplane = (p: PlaneSpec | undefined | null): p is Workplane => !!p && typeof p === 'object' && !('ref' in p);

/** build123d named planes: XY (u X, v Y, n +Z), XZ (u X, v Z, n −Y), YZ (u Y, v Z, n +X). */
export const NAMED_FRAMES: Record<'XY' | 'XZ' | 'YZ', { x: Vec3; y: Vec3; z: Vec3 }> = {
  XY: { x: [1, 0, 0], y: [0, 1, 0], z: [0, 0, 1] },
  XZ: { x: [1, 0, 0], y: [0, 0, 1], z: [0, -1, 0] },
  YZ: { x: [0, 1, 0], y: [0, 0, 1], z: [1, 0, 0] },
};

export function planeToWorkplane(name: 'XY' | 'XZ' | 'YZ', origin: Vec3 = [0, 0, 0]): Workplane {
  const f = NAMED_FRAMES[name];
  return { origin, normal: f.z, x_dir: f.x };
}

/** The plane moved by a world vector (a datum plane cannot move by itself: null). */
export function translatePlane(p: PlaneSpec, d: Vec3): PlaneSpec | null {
  if (isDatumRef(p)) return null;
  const wp = isNamedPlane(p) ? planeToWorkplane(p) : p;
  const o = wp.origin ?? [0, 0, 0];
  return { ...wp, origin: [r3(o[0] + d[0]), r3(o[1] + d[1]), r3(o[2] + d[2])] };
}

export function planeText(p: PlaneSpec | undefined | null): string {
  if (!p || isNamedPlane(p)) return p ?? 'XY';
  if (isDatumRef(p)) return `${p.ref}${p.offset ? ` ${p.offset > 0 ? '+' : ''}${p.offset}` : ''}${p.flip ? ' ⇅' : ''}`;
  const n = p.normal ?? [0, 0, 1];
  return `⊥(${n.map((c) => r3(c)).join(', ')})`;
}

// ─── sketches ───────────────────────────────────────────────────────────────

const dist = (a: readonly number[], b: readonly number[]) => Math.hypot(a[0] - b[0], a[1] - b[1]);

export function sketchSummary(p: SketchProfile): { segments: number; arcs: number; holes: number } {
  return { segments: p.segments.length, arcs: p.segments.filter((s) => s.kind === 'arc').length, holes: p.holes?.length ?? 0 };
}

/** Per-segment problems of a loop / path (zero length, arc ends off one circle); `closed` adds the auto-closing check. */
export function validateLoop(loop: SketchLoop, field: string, push: Push, closed = true) {
  if (!loop.segments.length) {
    push(field, 'geo.v.segments');
    return;
  }
  if (!loop.start.every(finite)) push(`${field}.start`, 'ws.v.number');
  let cur: Vec2 = loop.start;
  loop.segments.forEach((s, i) => {
    const f = `${field}.segments.${i}`;
    if (!s.to.every(finite) || (s.kind === 'arc' && !s.center.every(finite))) {
      push(f, 'ws.v.number');
      return;
    }
    if (s.kind === 'line' && dist(cur, s.to) < 1e-6) push(f, 'geo.v.zeroSegment', { n: i + 1 });
    if (s.kind === 'arc') {
      const r0 = dist(cur, s.center);
      const r1 = dist(s.to, s.center);
      if (r0 < 1e-3) push(f, 'geo.v.zeroSegment', { n: i + 1 });
      else if (Math.abs(r0 - r1) > Math.max(1e-3, 1e-4 * r0)) push(f, 'geo.v.arcRadius', { n: i + 1, r0: r3(r0), r1: r3(r1) });
      else if (dist(cur, s.to) < 1e-6 && (!closed || loop.segments.length !== 1)) push(f, 'geo.v.fullCircle', { n: i + 1 });
    }
    cur = s.to;
  });
}

export function validateSketch(p: SketchProfile, field: string, push: Push) {
  validateLoop(p, field, push);
  (p.holes ?? []).forEach((h, i) => validateLoop(h, `${field}.holes.${i}`, push));
}

/** Unit tangent at the start (t = 0) / end (t = 1) of a segment starting at `from`. */
function tangent(from: Vec2, s: Segment, t: 0 | 1): Vec2 {
  if (s.kind === 'line') {
    const l = dist(from, s.to) || 1;
    return [(s.to[0] - from[0]) / l, (s.to[1] - from[1]) / l];
  }
  const p = t === 0 ? from : s.to;
  const rx = p[0] - s.center[0];
  const ry = p[1] - s.center[1];
  const r = Math.hypot(rx, ry) || 1;
  const sgn = s.ccw === false ? -1 : 1;
  return [(-ry / r) * sgn, (rx / r) * sgn];
}

export function validatePath(path: SketchLoop, field: string, push: Push) {
  validateLoop(path, field, push, false);
  let cur = path.start;
  for (let i = 0; i + 1 < path.segments.length; i++) {
    const a = tangent(cur, path.segments[i], 1);
    const next = path.segments[i].to;
    const b = tangent(next, path.segments[i + 1], 0);
    if (a[0] * b[0] + a[1] * b[1] < Math.cos((0.05 * Math.PI) / 180)) push(`${field}.segments.${i + 1}`, 'geo.v.tangent', { n: i + 1 });
    cur = next;
  }
}

export const lineSeg = (to: Vec2): Segment => ({ kind: 'line', to });

/** An arc from `from` to `to` through the given bulge side: centre on the perpendicular bisector at radius r. */
export function arcSeg(from: Vec2, to: Vec2, radius: number, ccw: boolean): Segment {
  const mx = (from[0] + to[0]) / 2;
  const my = (from[1] + to[1]) / 2;
  const half = dist(from, to) / 2;
  const h = Math.sqrt(Math.max(0, radius * radius - half * half));
  const ux = (to[0] - from[0]) / (2 * half || 1);
  const uy = (to[1] - from[1]) / (2 * half || 1);
  const side = ccw ? 1 : -1; // centre to the left of travel for a counter-clockwise (short) arc
  return { kind: 'arc', to, center: [r3(mx - uy * h * side), r3(my + ux * h * side)], ccw };
}

// ─── defaults ───────────────────────────────────────────────────────────────

export interface GeometryDefaultContext {
  first: boolean;
  mode: 'new' | 'add';
  top: number;
  bbox: { min: number[]; max: number[] } | null;
  lastBodyId: string | null;
  lastTargetId: string | null;
}

export function defaultGeometryOp(type: ModelOpType, id: string, ctx: GeometryDefaultContext): ModelOp {
  const z = ctx.first ? 0 : ctx.top;
  switch (type) {
    case 'revolve':
      return {
        id, type, mode: ctx.mode, at: [0, 0, z], axis: [0, 0, 1], angle: 360, rotate: [0, 0, 0],
        profile: { kind: 'sketch', start: [0, 6], segments: [lineSeg([20, 6]), lineSeg([20, 12]), lineSeg([0, 12])], holes: [] },
      } satisfies RevolveOp;
    case 'sweep':
      return {
        id, type, mode: ctx.mode, rotate: [0, 0, 0], profile: { kind: 'circle', radius: 3 },
        plane: ctx.first ? 'XY' : planeToWorkplane('XY', [0, 0, r3(z + 3)]),
        path: { start: [0, 0], segments: [lineSeg([30, 0]), { kind: 'arc', to: [40, 10], center: [30, 10], ccw: true }, lineSeg([40, 40])] },
      } satisfies SweepOp;
    case 'bent_plate':
      return {
        id, type, mode: ctx.mode, rotate: [0, 0, 0], thickness: 3, width: 30, legs: [40, 30], bends: [90], inner_radius: 2,
        leg_length: 'outside', plane: { origin: [40, -15, z], normal: [0, 1, 0], x_dir: [-1, 0, 0] },
      } satisfies BentPlateOp;
    case 'cut':
      return { id, type, profile: { kind: 'rect', width: 20, length: 10, corner_radius: 0 }, plane: planeToWorkplane('XY', [0, 0, ctx.top]), depth: 5 } satisfies CutOp;
    case 'shell':
      return { id, type, thickness: 2, open_faces: [[0, 0, 1]] } satisfies ShellOp;
    case 'pattern':
      return { id, type, target: ctx.lastTargetId ?? '', layout: { kind: 'linear', direction: [1, 0, 0], spacing: 20, count: 3 } } satisfies PatternOp;
    default:
      throw new Error(`no default for ${type}`);
  }
}

// ─── validation ─────────────────────────────────────────────────────────────

const PATTERN_TARGETS = new Set(['box', 'cylinder', 'extrude', 'revolve', 'sweep', 'bent_plate', 'hole', 'cut']);

function validatePlane(p: PlaneSpec | undefined, earlier: Set<string>, push: Push, num: Check) {
  if (!p || isNamedPlane(p)) return;
  if (isDatumRef(p)) {
    const op = p.ref.split('.')[0];
    if (p.ref !== 'origin' && (!/^F-\d{2,}\.[A-Za-z0-9_.[\]]+$/.test(p.ref) || !earlier.has(op))) push('plane', 'geo.v.datumRef', { ref: p.ref });
    num('plane.offset', p.offset ?? 0);
    return;
  }
  const n = p.normal ?? [0, 0, 1];
  if (!n.every(finite) || Math.hypot(...n) < 1e-9) push('plane.normal', 'ws.v.zeroVector');
  (p.origin ?? [0, 0, 0]).forEach((c, i) => num(`plane.origin.${i}`, c));
}

/** Early checks of the geometry-library ops (the profile of sweep / cut is checked by validateProfile). */
export function validateGeometryOp(op: ModelOp, ops: ModelOp[], push: Push, dim: Check, num: Check) {
  const idx = ops.findIndex((o) => o.id === op.id);
  const earlierOps = idx >= 0 ? ops.slice(0, idx) : ops;
  const earlier = new Set(earlierOps.map((o) => o.id));
  switch (op.type) {
    case 'revolve': {
      validateSketch(op.profile, 'profile', push);
      const vs = [op.profile.start[1], ...op.profile.segments.map((s) => s.to[1])];
      if (vs.some((v) => finite(v) && v < -1e-9)) push('profile', 'geo.v.radial');
      const a = op.angle ?? 360;
      if (!finite(a) || a <= 0 || a > 360) push('angle', 'geo.v.angle');
      const ax = op.axis ?? [0, 0, 1];
      if (!ax.every(finite) || Math.hypot(...ax) < 1e-9) push('axis', 'ws.v.zeroVector');
      break;
    }
    case 'sweep':
      validatePath(op.path, 'path', push);
      validatePlane(op.plane, earlier, push, num);
      break;
    case 'bent_plate': {
      dim('thickness', op.thickness);
      dim('width', op.width);
      op.legs.forEach((l, i) => dim(`legs.${i}`, l));
      if (op.legs.length < 2 || op.bends.length !== op.legs.length - 1) push('bends', 'geo.v.bends');
      op.bends.forEach((b, i) => {
        if (!finite(b) || Math.abs(b) < 1 || Math.abs(b) > 175) push(`bends.${i}`, 'geo.v.bendAngle');
      });
      const r = op.inner_radius ?? 0;
      if (!finite(r) || r < 0) push('inner_radius', 'ws.v.number');
      validatePlane(op.plane, earlier, push, num);
      break;
    }
    case 'cut':
      if (op.depth !== null && op.depth !== undefined) dim('depth', op.depth);
      num('offset', op.offset ?? 0);
      validatePlane(op.plane, earlier, push, num);
      break;
    case 'shell':
      dim('thickness', op.thickness);
      (op.open_faces ?? []).forEach((d, i) => {
        if (!d.every(finite) || Math.hypot(...d) < 1e-9) push(`open_faces.${i}`, 'ws.v.zeroVector');
      });
      break;
    case 'coil': {
      dim('mean_diameter', op.mean_diameter);
      if (!finite(op.turns) || op.turns < 0.05 || op.turns > 100) push('turns', 'ws.v.number');
      const given = [op.pitch, op.length].filter((v) => v !== null && v !== undefined);
      if (given.length !== 1) push('pitch', 'geo.v.coilPitchOrLength');
      given.forEach((v) => dim(op.pitch !== null && op.pitch !== undefined ? 'pitch' : 'length', v));
      const ax = op.axis ?? [0, 0, 1];
      if (!ax.every(finite) || Math.hypot(...ax) < 1e-9) push('axis', 'ws.v.zeroVector');
      break;
    }
    case 'pattern': {
      const target = earlierOps.find((o) => o.id === op.target);
      if (!target || !PATTERN_TARGETS.has(target.type)) push('target', 'geo.v.patternTarget');
      const l = op.layout;
      if (!Number.isInteger(l.count) || l.count < 2 || l.count > 360) push('layout.count', 'ws.v.countRange', { min: 2, max: 360 });
      if (l.kind === 'linear') {
        if (!finite(l.spacing) || l.spacing === 0) push('layout.spacing', 'ws.v.number');
        if (!l.direction.every(finite) || Math.hypot(...l.direction) < 1e-9) push('layout.direction', 'ws.v.zeroVector');
      } else {
        const ax = l.axis ?? [0, 0, 1];
        if (!ax.every(finite) || Math.hypot(...ax) < 1e-9) push('layout.axis', 'ws.v.zeroVector');
      }
      break;
    }
    default:
      break;
  }
}

// ─── templates ──────────────────────────────────────────────────────────────

/** Client-side check of one template parameter (the server re-checks, including cross-parameter rules). */
export function templateParamIssue(spec: TemplateParamSpec, value: unknown): { key: MessageKey; vars?: Record<string, string | number> } | null {
  if (spec.type === 'choice') return spec.choices?.includes(String(value)) ? null : { key: 'geo.tpl.v.choice' };
  if (spec.type === 'text') return null;
  if (!finite(value)) return { key: 'ws.v.number' };
  if (spec.type === 'int' && !Number.isInteger(value)) return { key: 'geo.tpl.v.integer' };
  if ((spec.min !== undefined && value < spec.min) || (spec.max !== undefined && value > spec.max))
    return { key: 'geo.tpl.v.range', vars: { min: spec.min ?? '−∞', max: spec.max ?? '∞' } };
  return null;
}

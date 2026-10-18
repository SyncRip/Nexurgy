// Pure helpers for the modeling op list (mirror of server/app/cad/ops.py).
//
// The server is the authority: these checks only give early, per-field
// feedback while typing. Everything is re-validated by POST /cad/preview and
// POST /parts/{id}/revisions. Units: mm and degrees, world +Z up.

import type {
  BodyMode,
  BoxOp,
  ChamferOp,
  CylinderOp,
  EdgeGroup,
  EdgeSelector,
  ExtrudeOp,
  FilletOp,
  HoleOp,
  ModelOp,
  ModelOpType,
  Profile,
  TransformOp,
  Vec2,
  Vec3,
} from '../../api/types';
import type { BentPlateOp, CoilOp, RevolveOp, SweepOp } from '../../api/geometry';
import type { MessageKey } from '../../i18n/en';
import { defaultGeometryOp, isNamedPlane, translatePlane, validateGeometryOp, validateSketch } from './geometry';

export const MIN_DIM = 0.01;
export const MAX_DIM = 5000;
export const MAX_OPS = 200;

export const OP_TYPES: readonly ModelOpType[] = ['box', 'cylinder', 'extrude', 'hole', 'fillet', 'chamfer', 'transform', 'revolve', 'sweep',
  'bent_plate', 'cut', 'shell', 'pattern'];
export const BODY_TYPES: readonly ModelOpType[] = ['box', 'cylinder', 'extrude', 'revolve', 'sweep', 'bent_plate', 'coil'];
export const PROFILE_KINDS: readonly Profile['kind'][] = ['rect', 'circle', 'slot', 'polygon', 'ring', 'sketch'];
export const EDGE_GROUPS: readonly EdgeGroup[] = ['all', 'vertical', 'top', 'bottom', 'top_outer', 'bottom_outer'];
export const BODY_MODES: readonly BodyMode[] = ['new', 'add', 'subtract', 'intersect'];

export type BodyOp = BoxOp | CylinderOp | ExtrudeOp | RevolveOp | SweepOp | BentPlateOp | CoilOp;

export function isBodyOp(op: ModelOp): op is BodyOp {
  return (BODY_TYPES as readonly string[]).includes(op.type);
}

const OP_ID_RE = /^F-(\d{2,})$/;

export function opNumber(id: string | null | undefined): number | null {
  const m = id ? OP_ID_RE.exec(id) : null;
  return m ? Number(m[1]) : null;
}

export function opIdFromNumber(n: number): string {
  return `F-${String(n).padStart(2, '0')}`;
}

/** First free op number: the revision's next_op_id (part-wide counter), never below the ids in the list. */
export function nextOpNumber(nextOpId: string | null | undefined, ops: ModelOp[]): number {
  let n = opNumber(nextOpId) ?? 1;
  for (const op of ops) {
    const k = opNumber(op.id);
    if (k !== null && k >= n) n = k + 1;
  }
  return Math.max(1, n);
}

export const round = (v: number, digits = 3): number => {
  const f = 10 ** digits;
  return Math.round(v * f) / f + 0;
};
export const roundVec3 = (v: readonly number[], digits = 3): Vec3 => [round(v[0], digits), round(v[1], digits), round(v[2], digits)];

// ─── defaults ──────────────────────────────────────────────────────────────

export interface DefaultContext {
  /** Model bounding box (mm) when one exists. */
  bbox?: { min: number[]; max: number[] } | null;
  hasBase: boolean;
  /** Active (unsuppressed) body ops already in the list. */
  bodies: number;
  /** Id of the last body op, for transform targets. */
  lastBodyId?: string | null;
  /** Id of the last body / hole / cut op, for pattern targets. */
  lastTargetId?: string | null;
}

/** A new op of `type` with sensible starting values. */
export function defaultOp(type: ModelOpType, id: string, ctx: DefaultContext): ModelOp {
  const first = !ctx.hasBase && ctx.bodies === 0;
  const top = ctx.bbox ? round(ctx.bbox.max[2]) : 0;
  const mode: BodyMode = first ? 'new' : 'add';
  switch (type) {
    case 'box':
      return first
        ? { id, type, mode, size: [80, 50, 10], at: [0, 0, 0], rotate: [0, 0, 0] }
        : { id, type, mode, size: [20, 20, 10], at: [0, 0, top], rotate: [0, 0, 0] };
    case 'cylinder':
      return { id, type, mode, radius: first ? 25 : 8, height: first ? 20 : 10, at: [0, 0, first ? 0 : top], axis: 'z', rotate: [0, 0, 0] };
    case 'extrude':
      return {
        id,
        type,
        mode,
        profile: { kind: 'rect', width: first ? 60 : 20, length: first ? 40 : 12, corner_radius: 0 },
        plane: 'XY',
        offset: first ? 0 : top,
        at: [0, 0],
        height: 10,
        rotate_profile: 0,
        rotate: [0, 0, 0],
      };
    case 'hole':
      return { id, type, at: [0, 0, top], direction: [0, 0, -1], diameter: 6, depth: null, counterbore: null, countersink: null, pattern: null };
    case 'fillet':
      return { id, type, radius: 1, edges: { group: 'vertical', near: null, max_count: null } };
    case 'chamfer':
      return { id, type, distance: 0.5, edges: { group: 'top_outer', near: null, max_count: null } };
    case 'transform':
      return { id, type, target: ctx.lastBodyId ?? '', translate: [0, 0, 0], rotate: [0, 0, 0] };
    default:
      return defaultGeometryOp(type, id, { first, mode, top, bbox: ctx.bbox ?? null, lastBodyId: ctx.lastBodyId ?? null, lastTargetId: ctx.lastTargetId ?? null });
  }
}

export function defaultProfile(kind: Profile['kind'], prev?: Profile): Profile {
  const size = prev ? profileSpan(prev) : 40;
  switch (kind) {
    case 'rect':
      return { kind, width: size, length: round(size * 0.6), corner_radius: 0 };
    case 'circle':
      return { kind, radius: round(size / 2) };
    case 'slot':
      return { kind, length: size, width: round(size / 3) };
    case 'ring':
      return { kind, outer_radius: round(size / 2), inner_radius: round(size / 4) };
    case 'polygon': {
      const h = round(size / 2);
      return { kind, points: [[-h, -h], [h, -h], [h, h], [-h, h]] };
    }
    case 'sketch': {
      const h = round(size / 2);
      const r = round(size / 5);
      // a rectangle with one rounded corner: lines and an arc, ready to edit
      return { kind, start: [-h, -h], segments: [{ kind: 'line', to: [h - r, -h] }, { kind: 'arc', to: [h, -h + r], center: [h - r, -h + r], ccw: true },
        { kind: 'line', to: [h, h] }, { kind: 'line', to: [-h, h] }], holes: [] };
    }
  }
}

function profileSpan(p: Profile): number {
  switch (p.kind) {
    case 'rect':
      return p.width;
    case 'circle':
      return p.radius * 2;
    case 'slot':
      return p.length;
    case 'ring':
      return p.outer_radius * 2;
    case 'polygon': {
      const us = p.points.map((q) => q[0]);
      return Math.max(1, Math.max(...us) - Math.min(...us));
    }
    case 'sketch': {
      const us = [p.start[0], ...p.segments.map((s) => s.to[0])];
      return Math.max(1, Math.max(...us) - Math.min(...us));
    }
  }
}

// ─── mode normalisation (server rule: only the first body may be "new") ────

/**
 * Keep body modes consistent with ModelSpec._check_sequence: without a base
 * the first active body must be "new" and no later body may be; with a base
 * none may be. Only swaps add <-> new (they mean the same for a first body);
 * a first body that subtracts or intersects is left alone and reported by
 * validateOps. Returns the same array when nothing changes.
 */
export function normalizeModes(ops: ModelOp[], hasBase: boolean): ModelOp[] {
  let active = 0;
  let changed = false;
  const out = ops.map((op) => {
    if (!isBodyOp(op)) return op;
    const mode = op.mode ?? 'add';
    let next = mode;
    if (hasBase) {
      if (mode === 'new') next = 'add';
    } else if (active === 0 && !op.suppressed) {
      if (mode === 'add') next = 'new';
    } else if (mode === 'new' && active > 0) {
      next = 'add';
    }
    if (!op.suppressed) active++;
    if (next !== mode || op.mode === undefined) {
      changed = true;
      return { ...op, mode: next } as ModelOp;
    }
    return op;
  });
  return changed ? out : ops;
}

// ─── client-side validation ────────────────────────────────────────────────

export interface FieldIssue {
  /** Dotted field path (e.g. "size.0", "profile.width"); empty for the whole op. */
  field: string;
  key: MessageKey;
  vars?: Record<string, string | number>;
}

export type IssueMap = Record<string, FieldIssue[]>;

const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);

export function validateOps(ops: ModelOp[], hasBase: boolean): IssueMap {
  const out: IssueMap = {};
  const seen = new Set<string>();
  const bodyIds = new Set<string>();
  let active = 0;
  for (const op of ops) {
    const issues: FieldIssue[] = [];
    const push = (field: string, key: MessageKey, vars?: Record<string, string | number>) => issues.push({ field, key, vars });
    const dim = (field: string, v: unknown) => {
      if (!finite(v)) push(field, 'ws.v.number');
      else if (v < MIN_DIM || v > MAX_DIM) push(field, 'ws.v.range', { min: MIN_DIM, max: MAX_DIM });
    };
    const num = (field: string, v: unknown) => {
      if (!finite(v)) push(field, 'ws.v.number');
    };
    const vec = (field: string, v: readonly unknown[] | undefined, n: number) => {
      if (!v) return;
      for (let i = 0; i < n; i++) num(`${field}.${i}`, v[i]);
    };
    if (seen.has(op.id)) push('', 'ws.v.duplicateId');
    seen.add(op.id);

    if (isBodyOp(op)) {
      const mode = op.mode ?? 'add';
      if (!op.suppressed && active === 0 && !hasBase && mode !== 'new') push('mode', 'ws.v.firstBodyMode');
      vec('rotate', op.rotate, 3);
      if (op.type === 'box') {
        op.size.forEach((v, i) => dim(`size.${i}`, v));
        vec('at', op.at, 3);
      } else if (op.type === 'cylinder') {
        dim('radius', op.radius);
        dim('height', op.height);
        vec('at', op.at, 3);
      } else if (op.type === 'extrude') {
        dim('height', op.height);
        num('offset', op.offset ?? 0);
        num('rotate_profile', op.rotate_profile ?? 0);
        vec('at', op.at, 2);
        validateProfile(op.profile, push, dim);
      } else {
        validateGeometryOp(op, ops, push, dim, num);
        if (op.type === 'sweep') validateProfile(op.profile, push, dim);
      }
      if (!op.suppressed) active++;
      bodyIds.add(op.id);
    } else if (op.type === 'hole') {
      vec('at', op.at, 3);
      dim('diameter', op.diameter);
      if (op.depth !== null && op.depth !== undefined) dim('depth', op.depth);
      const d = op.direction ?? [0, 0, -1];
      vec('direction', d, 3);
      if (d.every(finite) && d[0] * d[0] + d[1] * d[1] + d[2] * d[2] < 1e-12) push('direction', 'ws.v.zeroVector');
      if (op.counterbore && op.countersink) push('', 'ws.v.cbAndCs');
      if (op.counterbore) {
        dim('counterbore.diameter', op.counterbore.diameter);
        dim('counterbore.depth', op.counterbore.depth);
        if (finite(op.counterbore.diameter) && finite(op.diameter) && op.counterbore.diameter <= op.diameter) push('counterbore.diameter', 'ws.v.cbDiameter');
      }
      if (op.countersink) {
        dim('countersink.diameter', op.countersink.diameter);
        const a = op.countersink.angle ?? 90;
        if (!finite(a)) push('countersink.angle', 'ws.v.number');
        else if (a <= 0 || a >= 180) push('countersink.angle', 'ws.v.angle');
        if (finite(op.countersink.diameter) && finite(op.diameter) && op.countersink.diameter <= op.diameter) push('countersink.diameter', 'ws.v.csDiameter');
      }
      const p = op.pattern;
      if (p?.kind === 'linear') {
        p.count.forEach((c, i) => {
          if (!Number.isInteger(c) || c < 1) push(`pattern.count.${i}`, 'ws.v.countMin', { min: 1 });
        });
        p.pitch.forEach((v, i) => num(`pattern.pitch.${i}`, v));
      } else if (p?.kind === 'circular') {
        if (!Number.isInteger(p.count) || p.count < 2 || p.count > 360) push('pattern.count', 'ws.v.countRange', { min: 2, max: 360 });
        num('pattern.radius', p.radius);
      }
    } else if (op.type === 'fillet' || op.type === 'chamfer') {
      if (op.type === 'fillet') dim('radius', op.radius);
      else dim('distance', op.distance);
      const mc = op.edges.max_count;
      if (mc !== null && mc !== undefined && (!Number.isInteger(mc) || mc < 1 || mc > 500)) push('edges.max_count', 'ws.v.countRange', { min: 1, max: 500 });
      if (op.edges.near) vec('edges.near', op.edges.near, 3);
      if (active === 0 && !hasBase && !op.suppressed) push('', 'ws.v.needsBody');
    } else if (op.type === 'transform') {
      vec('translate', op.translate, 3);
      vec('rotate', op.rotate, 3);
      if (!op.target || !bodyIds.has(op.target)) push('target', 'ws.v.transformTarget');
    } else {
      validateGeometryOp(op, ops, push, dim, num);
      if (op.type === 'cut') validateProfile(op.profile, push, dim);
      if (active === 0 && !hasBase && !op.suppressed) push('', 'ws.v.needsBody');
    }
    if (op.type === 'hole' && active === 0 && !hasBase && !op.suppressed) push('', 'ws.v.needsBody');
    if (issues.length) out[op.id] = issues;
  }
  return out;
}

export function validateProfile(p: Profile, push: (f: string, k: MessageKey, v?: Record<string, string | number>) => void, dim: (f: string, v: unknown) => void) {
  switch (p.kind) {
    case 'sketch':
      validateSketch(p, 'profile', push);
      break;
    case 'rect': {
      dim('profile.width', p.width);
      dim('profile.length', p.length);
      const cr = p.corner_radius ?? 0;
      if (!finite(cr)) push('profile.corner_radius', 'ws.v.number');
      else if (cr < 0 || (finite(p.width) && finite(p.length) && cr * 2 >= Math.min(p.width, p.length))) push('profile.corner_radius', 'ws.v.cornerRadius');
      break;
    }
    case 'circle':
      dim('profile.radius', p.radius);
      break;
    case 'slot':
      dim('profile.length', p.length);
      dim('profile.width', p.width);
      if (finite(p.width) && finite(p.length) && p.width >= p.length) push('profile.width', 'ws.v.slotWidth');
      break;
    case 'ring':
      dim('profile.outer_radius', p.outer_radius);
      dim('profile.inner_radius', p.inner_radius);
      if (finite(p.inner_radius) && finite(p.outer_radius) && p.inner_radius >= p.outer_radius) push('profile.inner_radius', 'ws.v.ringInner');
      break;
    case 'polygon':
      if (p.points.length < 3) push('profile.points', 'ws.v.polygonPoints');
      else if (p.points.some((q) => !finite(q[0]) || !finite(q[1]))) push('profile.points', 'ws.v.number');
      else if (polygonSelfIntersects(p.points)) push('profile.points', 'ws.v.polygonSelf');
      break;
  }
}

function segmentsCross(a: Vec2, b: Vec2, c: Vec2, d: Vec2): boolean {
  const o = (p: Vec2, q: Vec2, r: Vec2) => Math.sign((q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0]));
  const o1 = o(a, b, c);
  const o2 = o(a, b, d);
  const o3 = o(c, d, a);
  const o4 = o(c, d, b);
  return o1 !== o2 && o3 !== o4 && o1 !== 0 && o2 !== 0 && o3 !== 0 && o4 !== 0;
}

export function polygonSelfIntersects(pts: Vec2[]): boolean {
  const n = pts.length;
  for (let i = 0; i < n; i++) {
    const a = pts[i];
    const b = pts[(i + 1) % n];
    for (let j = i + 2; j < n; j++) {
      if (i === 0 && j === n - 1) continue; // adjacent through the closing edge
      if (segmentsCross(a, b, pts[j], pts[(j + 1) % n])) return true;
    }
  }
  return false;
}

/** Map a pydantic loc like "ops.2.box.size" to the op id at that index. */
export function opIdFromLoc(loc: string, ops: ModelOp[]): string | null {
  const m = /^ops\.(\d+)/.exec(loc);
  if (!m) return null;
  return ops[Number(m[1])]?.id ?? null;
}

// ─── placement helpers ─────────────────────────────────────────────────────

/**
 * Move a body op by a world-space delta by editing its own placement (the
 * same thing a transform op would do). Extrude profiles live on a plane:
 * XY u=X v=Y normal +Z; XZ u=X v=Z normal -Y; YZ u=Y v=Z normal +X.
 */
export function translateBody(op: BodyOp, d: Vec3): BodyOp {
  if (op.type === 'extrude' && isNamedPlane(op.plane ?? 'XY')) {
    const [u, v] = op.at ?? [0, 0];
    const off = op.offset ?? 0;
    const plane = op.plane ?? 'XY';
    const [du, dv, dn] = plane === 'XY' ? [d[0], d[1], d[2]] : plane === 'XZ' ? [d[0], d[2], -d[1]] : [d[1], d[2], d[0]];
    return { ...op, at: [round(u + du), round(v + dv)], offset: round(off + dn) };
  }
  if (op.type === 'extrude' || op.type === 'sweep' || op.type === 'bent_plate') {
    const moved = translatePlane(op.plane ?? (op.type === 'bent_plate' ? 'XZ' : 'XY'), d);
    return moved ? ({ ...op, plane: moved } as BodyOp) : op; // a datum plane follows its datum: use a transform op
  }
  const at = op.at ?? [0, 0, 0];
  return { ...op, at: [round(at[0] + d[0]), round(at[1] + d[1]), round(at[2] + d[2])] } as BodyOp;
}

export function rotateBody(op: BodyOp, r: Vec3): BodyOp {
  const cur = op.rotate ?? [0, 0, 0];
  const wrap = (a: number) => {
    let x = ((a % 360) + 360) % 360;
    if (x > 180) x -= 360;
    return round(x);
  };
  return { ...op, rotate: [wrap(cur[0] + r[0]), wrap(cur[1] + r[1]), wrap(cur[2] + r[2])] } as BodyOp;
}

// ─── hole frame (port of cad/ops.py hole_frame / hole_positions) ───────────

function unit(v: readonly number[]): Vec3 {
  const n = Math.hypot(v[0], v[1], v[2]);
  if (n < 1e-12) return [0, 0, -1];
  return [v[0] / n, v[1] / n, v[2] / n];
}

/** In-plane axes (u, v, d) for a hole drilled along `direction`. */
export function holeFrame(direction: readonly number[]): [Vec3, Vec3, Vec3] {
  const d = unit(direction);
  const [ax, ay, az] = d.map(Math.abs);
  const refs: [Vec3, Vec3] = az >= ax && az >= ay ? [[1, 0, 0], [0, 1, 0]] : ax >= ay ? [[0, 1, 0], [0, 0, 1]] : [[1, 0, 0], [0, 0, 1]];
  const reject = (v: Vec3, ...basis: Vec3[]): Vec3 => {
    const out = [...v];
    for (const b of basis) {
      const dot = out[0] * b[0] + out[1] * b[1] + out[2] * b[2];
      for (let i = 0; i < 3; i++) out[i] -= dot * b[i];
    }
    return unit(out);
  };
  const u = reject(refs[0], d);
  const v = reject(refs[1], d, u);
  return [u, v, d];
}

/** Entry point of every hole instance (linear: `at` is the FIRST instance; circular: `at` is the centre). */
export function holePositions(op: HoleOp): Vec3[] {
  const at = op.at;
  if (!op.pattern) return [[at[0], at[1], at[2]]];
  const [u, v] = holeFrame(op.direction ?? [0, 0, -1]);
  const out: Vec3[] = [];
  if (op.pattern.kind === 'linear') {
    const [nx, ny] = op.pattern.count;
    const [px, py] = op.pattern.pitch;
    for (let j = 0; j < Math.min(ny, 200); j++)
      for (let i = 0; i < Math.min(nx, 200); i++) out.push([0, 1, 2].map((k) => at[k] + i * px * u[k] + j * py * v[k]) as Vec3);
  } else {
    const n = Math.min(op.pattern.count, 360);
    const r = op.pattern.radius;
    for (let k = 0; k < n; k++) {
      const t = (2 * Math.PI * k) / n;
      out.push([0, 1, 2].map((m) => at[m] + r * (Math.cos(t) * u[m] + Math.sin(t) * v[m])) as Vec3);
    }
  }
  return out;
}

/** "+X", "-Y", or a rounded vector, for describing pattern axes. */
export function axisName(v: readonly number[]): string {
  const names = ['X', 'Y', 'Z'];
  for (let i = 0; i < 3; i++) {
    if (Math.abs(Math.abs(v[i]) - 1) < 1e-6) return `${v[i] > 0 ? '+' : '−'}${names[i]}`;
  }
  return `(${v.map((x) => round(x, 3)).join(', ')})`;
}

// ─── edge selector preview (approximate; the kernel decides) ───────────────

export interface MeshEdgeLike {
  kind: string;
  points: number[][];
  seam?: boolean;
}

function pointSegmentDistance(p: readonly number[], a: readonly number[], b: readonly number[]): number {
  const ab = [b[0] - a[0], b[1] - a[1], b[2] - a[2]];
  const ap = [p[0] - a[0], p[1] - a[1], p[2] - a[2]];
  const len2 = ab[0] ** 2 + ab[1] ** 2 + ab[2] ** 2;
  const t = len2 > 0 ? Math.max(0, Math.min(1, (ap[0] * ab[0] + ap[1] * ab[1] + ap[2] * ab[2]) / len2)) : 0;
  return Math.hypot(ap[0] - t * ab[0], ap[1] - t * ab[1], ap[2] - t * ab[2]);
}

export function distanceToPolyline(p: readonly number[], pts: number[][]): number {
  if (pts.length === 1) return Math.hypot(p[0] - pts[0][0], p[1] - pts[0][1], p[2] - pts[0][2]);
  let best = Infinity;
  for (let i = 0; i + 1 < pts.length; i++) best = Math.min(best, pointSegmentDistance(p, pts[i], pts[i + 1]));
  return best;
}

/**
 * Indices of mesh edges an EdgeSelector would roughly pick, for highlighting.
 * `*_outer` is approximated by the whole top / bottom level (rims included).
 */
export function approxEdgeSelection(edges: MeshEdgeLike[], sel: EdgeSelector, sizeHint = 100): number[] {
  const tol = Math.max(1e-4, sizeHint * 1e-6);
  const usable = edges.map((e, i) => ({ e, i })).filter(({ e }) => !e.seam && e.kind !== 'degenerate' && e.points.length >= 2);
  const group = sel.group ?? 'all';
  let picked: { e: MeshEdgeLike; i: number }[];
  if (group === 'all') picked = usable;
  else if (group === 'vertical') {
    picked = usable.filter(({ e }) => {
      if (e.kind !== 'line') return false;
      const a = e.points[0];
      const b = e.points[e.points.length - 1];
      const len = Math.hypot(b[0] - a[0], b[1] - a[1], b[2] - a[2]);
      return len > 0 && Math.abs(b[2] - a[2]) / len > 1 - 1e-6;
    });
  } else {
    const flat = usable.filter(({ e }) => e.points.every((p) => Math.abs(p[2] - e.points[0][2]) <= tol));
    if (!flat.length) return [];
    const levels = flat.map(({ e }) => e.points[0][2]);
    const level = group.startsWith('top') ? Math.max(...levels) : Math.min(...levels);
    picked = flat.filter(({ e }) => Math.abs(e.points[0][2] - level) <= tol * 10);
  }
  if (sel.near) {
    const near = sel.near;
    const n = sel.max_count ?? 1;
    picked = [...picked].sort((a, b) => distanceToPolyline(near, a.e.points) - distanceToPolyline(near, b.e.points)).slice(0, n);
  }
  return picked.map(({ i }) => i);
}

// ─── misc ──────────────────────────────────────────────────────────────────

export function opsEqual(a: ModelOp[], b: ModelOp[]): boolean {
  return canonical(a) === canonical(b);
}

function sortKeys(v: unknown): unknown {
  if (Array.isArray(v)) return v.map(sortKeys);
  if (v && typeof v === 'object') {
    const o = v as Record<string, unknown>;
    return Object.keys(o)
      .sort()
      .reduce<Record<string, unknown>>((acc, k) => {
        if (o[k] !== undefined) acc[k] = sortKeys(o[k]);
        return acc;
      }, {});
  }
  return v;
}

export function canonical(v: unknown): string {
  return JSON.stringify(sortKeys(v));
}

/** Type guards used by the forms. */
export const isHole = (op: ModelOp): op is HoleOp => op.type === 'hole';
export const isEdgeOp = (op: ModelOp): op is FilletOp | ChamferOp => op.type === 'fillet' || op.type === 'chamfer';
export const isTransform = (op: ModelOp): op is TransformOp => op.type === 'transform';

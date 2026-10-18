// Object snaps of the drafting editor: endpoint, midpoint, centre, quadrant and intersection. Pure functions,
// unit-tested in snaps.test.ts.

import type { DraftEntity, Pt } from '../../api/drafting';
import { add, angleOf, arcSweep, bulgeArc, dist, mid, onArc, polySegments, scale, sub, unitAt } from './geometry';

export type SnapKind = 'endpoint' | 'midpoint' | 'center' | 'quadrant' | 'intersection';
export const SNAP_KINDS: SnapKind[] = ['endpoint', 'midpoint', 'center', 'quadrant', 'intersection'];

export interface Snap {
  kind: SnapKind;
  point: Pt;
  ids: string[];
}

/** Lines and arcs an entity is made of (for intersections). */
export type Prim = { kind: 'seg'; a: Pt; b: Pt } | { kind: 'arc'; c: Pt; r: number; start: number; end: number; full: boolean };

export function primitives(e: DraftEntity): Prim[] {
  switch (e.type) {
    case 'line':
      return [{ kind: 'seg', a: e.p1, b: e.p2 }];
    case 'polyline':
      return polySegments(e.points, e.bulges, e.closed).map(([a, b, bulge]) => {
        if (bulge && dist(a, b) > 1e-9) {
          const arc = bulgeArc(a, b, bulge);
          return { kind: 'arc', c: arc.c, r: arc.r, start: arc.start, end: arc.end, full: false } as Prim;
        }
        return { kind: 'seg', a, b } as Prim;
      });
    case 'circle':
      return [{ kind: 'arc', c: e.center, r: e.radius, start: 0, end: 360, full: true }];
    case 'arc':
      return [{ kind: 'arc', c: e.center, r: e.radius, start: e.start_angle, end: e.end_angle, full: false }];
    default:
      return [];
  }
}

/** Endpoint / midpoint / centre / quadrant snaps of one entity. */
export function entitySnaps(e: DraftEntity): Snap[] {
  const out: Snap[] = [];
  const ids = [e.id];
  switch (e.type) {
    case 'line':
      out.push({ kind: 'endpoint', point: e.p1, ids }, { kind: 'endpoint', point: e.p2, ids }, { kind: 'midpoint', point: mid(e.p1, e.p2), ids });
      break;
    case 'polyline':
      for (const p of e.points) out.push({ kind: 'endpoint', point: p, ids });
      for (const [a, b, bulge] of polySegments(e.points, e.bulges, e.closed)) if (!bulge) out.push({ kind: 'midpoint', point: mid(a, b), ids });
      break;
    case 'leader':
      for (const p of e.points) out.push({ kind: 'endpoint', point: p, ids });
      break;
    case 'circle':
      out.push({ kind: 'center', point: e.center, ids });
      for (const a of [0, 90, 180, 270]) out.push({ kind: 'quadrant', point: add(e.center, scale(unitAt(a), e.radius)), ids });
      break;
    case 'arc': {
      out.push({ kind: 'center', point: e.center, ids });
      out.push({ kind: 'endpoint', point: add(e.center, scale(unitAt(e.start_angle), e.radius)), ids });
      out.push({ kind: 'endpoint', point: add(e.center, scale(unitAt(e.end_angle), e.radius)), ids });
      const m = e.start_angle + arcSweep(e.start_angle, e.end_angle) / 2;
      out.push({ kind: 'midpoint', point: add(e.center, scale(unitAt(m), e.radius)), ids });
      break;
    }
    case 'text':
    case 'mtext':
    case 'insert':
      out.push({ kind: 'endpoint', point: e.insert, ids });
      break;
    default:
      break;
  }
  return out;
}

const EPS = 1e-9;

function onSeg(p: Pt, a: Pt, b: Pt): boolean {
  const ab = sub(b, a);
  const l2 = ab[0] * ab[0] + ab[1] * ab[1];
  if (l2 < EPS) return dist(p, a) < 1e-7;
  const t = ((p[0] - a[0]) * ab[0] + (p[1] - a[1]) * ab[1]) / l2;
  return t >= -1e-9 && t <= 1 + 1e-9;
}

function onArcPrim(p: Pt, arc: Extract<Prim, { kind: 'arc' }>): boolean {
  return arc.full || onArc(angleOf(sub(p, arc.c)), arc.start, arc.end);
}

function segSeg(a1: Pt, a2: Pt, b1: Pt, b2: Pt): Pt[] {
  const d1 = sub(a2, a1);
  const d2 = sub(b2, b1);
  const den = d1[0] * d2[1] - d1[1] * d2[0];
  if (Math.abs(den) < EPS) return [];
  const t = ((b1[0] - a1[0]) * d2[1] - (b1[1] - a1[1]) * d2[0]) / den;
  const u = ((b1[0] - a1[0]) * d1[1] - (b1[1] - a1[1]) * d1[0]) / den;
  if (t < -1e-9 || t > 1 + 1e-9 || u < -1e-9 || u > 1 + 1e-9) return [];
  return [[a1[0] + t * d1[0], a1[1] + t * d1[1]]];
}

function lineCircle(a: Pt, b: Pt, c: Pt, r: number): Pt[] {
  const d = sub(b, a);
  const f = sub(a, c);
  const A = d[0] * d[0] + d[1] * d[1];
  if (A < EPS) return [];
  const B = 2 * (f[0] * d[0] + f[1] * d[1]);
  const C = f[0] * f[0] + f[1] * f[1] - r * r;
  const disc = B * B - 4 * A * C;
  if (disc < -1e-9) return [];
  const s = Math.sqrt(Math.max(disc, 0));
  const ts = disc < 1e-12 ? [-B / (2 * A)] : [(-B - s) / (2 * A), (-B + s) / (2 * A)];
  return ts.map((t) => [a[0] + t * d[0], a[1] + t * d[1]] as Pt);
}

function circleCircle(c1: Pt, r1: number, c2: Pt, r2: number): Pt[] {
  const d = dist(c1, c2);
  if (d < EPS || d > r1 + r2 + 1e-9 || d < Math.abs(r1 - r2) - 1e-9) return [];
  const a = (r1 * r1 - r2 * r2 + d * d) / (2 * d);
  const h = Math.sqrt(Math.max(r1 * r1 - a * a, 0));
  const u: Pt = [(c2[0] - c1[0]) / d, (c2[1] - c1[1]) / d];
  const m: Pt = [c1[0] + a * u[0], c1[1] + a * u[1]];
  if (h < 1e-9) return [m];
  return [
    [m[0] - h * u[1], m[1] + h * u[0]],
    [m[0] + h * u[1], m[1] - h * u[0]],
  ];
}

/** Intersection points of two primitives (within both). */
export function intersect(p: Prim, q: Prim): Pt[] {
  if (p.kind === 'seg' && q.kind === 'seg') return segSeg(p.a, p.b, q.a, q.b);
  if (p.kind === 'seg' && q.kind === 'arc') return lineCircle(p.a, p.b, q.c, q.r).filter((x) => onSeg(x, p.a, p.b) && onArcPrim(x, q));
  if (p.kind === 'arc' && q.kind === 'seg') return intersect(q, p);
  if (p.kind === 'arc' && q.kind === 'arc') return circleCircle(p.c, p.r, q.c, q.r).filter((x) => onArcPrim(x, p) && onArcPrim(x, q));
  return [];
}

/** Intersections between entities near `p` (only entities within `reach` are tested, so it stays cheap). */
export function intersectionSnaps(entities: DraftEntity[], p: Pt, reach: number, distanceTo: (e: DraftEntity, p: Pt) => number): Snap[] {
  const near = entities.filter((e) => distanceTo(e, p) <= reach).slice(0, 60);
  const out: Snap[] = [];
  for (let i = 0; i < near.length; i++) {
    const pi = primitives(near[i]);
    for (let j = i + 1; j < near.length; j++) {
      const pj = primitives(near[j]);
      for (const a of pi) for (const b of pj) for (const x of intersect(a, b)) out.push({ kind: 'intersection', point: x, ids: [near[i].id, near[j].id] });
    }
  }
  return out;
}

const PRIORITY: Record<SnapKind, number> = { endpoint: 0, center: 1, intersection: 2, midpoint: 3, quadrant: 4 };

/** The snap closest to `p` within `tol` (ties: endpoint > centre > intersection > midpoint > quadrant). */
export function findSnap(
  entities: DraftEntity[],
  p: Pt,
  tol: number,
  kinds: ReadonlySet<SnapKind>,
  distanceTo: (e: DraftEntity, p: Pt) => number,
): Snap | null {
  let best: Snap | null = null;
  let bestScore = Infinity;
  const consider = (s: Snap) => {
    if (!kinds.has(s.kind)) return;
    const d = dist(s.point, p);
    if (d > tol) return;
    const score = d + PRIORITY[s.kind] * tol * 0.05;
    if (score < bestScore) {
      best = s;
      bestScore = score;
    }
  };
  for (const e of entities) {
    if (distanceTo(e, p) > tol * 4 && e.type !== 'circle' && e.type !== 'arc') continue;
    for (const s of entitySnaps(e)) consider(s);
  }
  if (kinds.has('intersection')) for (const s of intersectionSnaps(entities, p, tol * 2, distanceTo)) consider(s);
  return best;
}

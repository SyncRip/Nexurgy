// Pure 2D geometry of the drafting editor (mirror of server/app/cad/drafting.py where both compute the same
// thing: measured dimension values, displayed text, rigid transforms). Model space is millimetres, y up.

import type { DimensionEntity, DraftEntity, HatchLoop, Pt } from '../../api/drafting';

export const TOL = 1e-6;

export const add = (a: Pt, b: Pt): Pt => [a[0] + b[0], a[1] + b[1]];
export const sub = (a: Pt, b: Pt): Pt => [a[0] - b[0], a[1] - b[1]];
export const scale = (a: Pt, k: number): Pt => [a[0] * k, a[1] * k];
export const len = (v: Pt): number => Math.hypot(v[0], v[1]);
export const dist = (a: Pt, b: Pt): number => Math.hypot(a[0] - b[0], a[1] - b[1]);
export const mid = (a: Pt, b: Pt): Pt => [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
/** Unit vector at an angle; exact on multiples of 90° (no 6e-17 noise in vertical dimensions). */
export function unitAt(deg: number): Pt {
  const q = ((deg % 360) + 360) % 360;
  if (q === 0) return [1, 0];
  if (q === 90) return [0, 1];
  if (q === 180) return [-1, 0];
  if (q === 270) return [0, -1];
  return [Math.cos((deg * Math.PI) / 180), Math.sin((deg * Math.PI) / 180)];
}
export const angleOf = (v: Pt): number => (Math.atan2(v[1], v[0]) * 180) / Math.PI;
export const normAngle = (a: number): number => ((a % 360) + 360) % 360;

/** Round float noise so stored coordinates stay readable (server: _clean). */
export function clean(v: number): number {
  const r = Math.round(v * 1e9) / 1e9;
  return r === 0 ? 0 : r;
}
export const cleanPt = (p: Pt): Pt => [clean(p[0]), clean(p[1])];

export function rotate(p: Pt, c: Pt, deg: number): Pt {
  if (!deg) return p;
  const r = (deg * Math.PI) / 180;
  const cs = Math.cos(r);
  const sn = Math.sin(r);
  const x = p[0] - c[0];
  const y = p[1] - c[1];
  return [c[0] + x * cs - y * sn, c[1] + x * sn + y * cs];
}

export function arcSweep(start: number, end: number): number {
  const s = normAngle(end - start);
  return s < 1e-12 && start !== end ? 360 : s;
}

export function onArc(angle: number, start: number, end: number): boolean {
  return normAngle(angle - start) <= arcSweep(start, end) + 1e-9;
}

export function segDist(p: Pt, a: Pt, b: Pt): number {
  const ab = sub(b, a);
  const l2 = ab[0] * ab[0] + ab[1] * ab[1];
  if (l2 < 1e-18) return dist(p, a);
  const t = Math.max(0, Math.min(1, ((p[0] - a[0]) * ab[0] + (p[1] - a[1]) * ab[1]) / l2));
  return dist(p, [a[0] + t * ab[0], a[1] + t * ab[1]]);
}

/** Center, radius and angles of a bulged polyline segment (CCW for bulge > 0). */
export function bulgeArc(a: Pt, b: Pt, bulge: number): { c: Pt; r: number; start: number; end: number } {
  const chord = dist(a, b);
  const theta = 4 * Math.atan(bulge);
  const r = chord / (2 * Math.sin(Math.abs(theta) / 2));
  const m = mid(a, b);
  const d = Math.sqrt(Math.max(r * r - (chord / 2) ** 2, 0));
  const ux = (b[0] - a[0]) / chord;
  const uy = (b[1] - a[1]) / chord;
  let sign = bulge > 0 ? 1 : -1;
  if (Math.abs(theta) > Math.PI) sign = -sign;
  const c: Pt = [m[0] - uy * d * sign, m[1] + ux * d * sign];
  let start = angleOf(sub(a, c));
  let end = angleOf(sub(b, c));
  if (bulge < 0) [start, end] = [end, start];
  return { c, r, start, end };
}

export function polySegments(points: Pt[], bulges: number[] | null | undefined, closed: boolean): [Pt, Pt, number][] {
  const out: [Pt, Pt, number][] = [];
  const n = points.length;
  const last = closed ? n : n - 1;
  for (let i = 0; i < last; i++) out.push([points[i], points[(i + 1) % n], bulges ? bulges[i] ?? 0 : 0]);
  return out;
}

// ─── dimensions ────────────────────────────────────────────────────────────

export function measure(d: DimensionEntity): { value: number; unit: 'mm' | 'deg' } {
  switch (d.dimtype) {
    case 'linear': {
      const u = unitAt(d.angle ?? 0);
      const v = sub(d.p2!, d.p1!);
      return { value: Math.abs(v[0] * u[0] + v[1] * u[1]), unit: 'mm' };
    }
    case 'aligned':
      return { value: dist(d.p1!, d.p2!), unit: 'mm' };
    case 'diameter':
      return { value: 2 * (d.radius ?? 0), unit: 'mm' };
    case 'radius':
      return { value: d.radius ?? 0, unit: 'mm' };
    case 'angular': {
      const a1 = Math.atan2(d.p1![1] - d.center![1], d.p1![0] - d.center![0]);
      const a2 = Math.atan2(d.p2![1] - d.center![1], d.p2![0] - d.center![0]);
      const ang = Math.abs(((a2 - a1) * 180) / Math.PI) % 360;
      return { value: ang > 180 ? 360 - ang : ang, unit: 'deg' };
    }
  }
}

/** Number as the server formats it: at most `decimals` places, trailing zeros removed. */
export function fmtNum(v: number, decimals = 2): string {
  const s = (Math.round(v * 10 ** decimals) / 10 ** decimals).toFixed(decimals).replace(/0+$/, '').replace(/\.$/, '');
  return s === '-0' || s === '' ? '0' : s;
}

const DEFAULT_TEXT: Record<string, string> = { diameter: 'Ø<>', radius: 'R<>', angular: '<>°' };

export function displayedText(d: DimensionEntity): string {
  const { value, unit } = measure(d);
  const shown = fmtNum(value * (unit === 'mm' ? d.dimlfac ?? 1 : 1));
  const template = d.text_override ? d.text_override : DEFAULT_TEXT[d.dimtype] ?? '<>';
  return template.split('<>').join(shown);
}

/** Numbers of a displayed text with their decimals, tolerance parts skipped (server: numbers_in). */
export function numbersIn(text: string): [number, number][] {
  const out: [number, number][] = [];
  const re = /\d+(?:\.\d+)?|\.\d+/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text))) {
    const start = m.index;
    const prev = start > 0 ? text[start - 1] : '';
    if (prev && '+±'.includes(prev)) continue;
    if (prev && '-−'.includes(prev) && (start < 2 || ' \t(/'.includes(text[start - 2]))) continue;
    const g = m[0];
    out.push([parseFloat(g), g.includes('.') ? g.split('.')[1].length : 0]);
  }
  return out;
}

export function readingConsistent(text: string, value: number): boolean | null {
  const nums = numbersIn(text);
  if (!nums.length) return null;
  return nums.some(([n, dec]) => Math.abs(n - value) <= 0.5 * 10 ** -dec + 1e-9);
}

/** Drawable parts of a dimension: extension / dimension lines, arcs, arrows and the text anchor. */
export interface DimGeometry {
  lines: [Pt, Pt][];
  arcs: { c: Pt; r: number; start: number; end: number }[];
  arrows: { tip: Pt; dir: Pt }[];
  text: { at: Pt; rotation: number; value: string };
}

export function dimensionGeometry(d: DimensionEntity): DimGeometry {
  const value = displayedText(d);
  const lines: [Pt, Pt][] = [];
  const arcs: DimGeometry['arcs'] = [];
  const arrows: DimGeometry['arrows'] = [];
  if (d.dimtype === 'linear' || d.dimtype === 'aligned') {
    const p1 = d.p1!;
    const p2 = d.p2!;
    const loc = d.location ?? mid(p1, p2);
    const u = d.dimtype === 'linear' ? unitAt(d.angle ?? 0) : (() => {
      const v = sub(p2, p1);
      const l = len(v) || 1;
      return [v[0] / l, v[1] / l] as Pt;
    })();
    const n: Pt = [-u[1], u[0]];
    const off = (p: Pt) => (loc[0] - p[0]) * n[0] + (loc[1] - p[1]) * n[1];
    const a = add(p1, scale(n, off(p1)));
    const b = add(p2, scale(n, off(p2)));
    const ext = (p: Pt, q: Pt) => {
      const v = sub(q, p);
      const l = len(v);
      if (l > 1e-9) lines.push([add(p, scale(v, Math.min(1, 1 / l))), add(q, scale(v, 1.5 / l))]);
    };
    ext(p1, a);
    ext(p2, b);
    lines.push([a, b]);
    const ab = sub(b, a);
    const lab = len(ab) || 1;
    arrows.push({ tip: a, dir: [-ab[0] / lab, -ab[1] / lab] }, { tip: b, dir: [ab[0] / lab, ab[1] / lab] });
    let rot = angleOf(u);
    if (rot > 90.0001 || rot <= -90) rot += 180;
    return { lines, arcs, arrows, text: { at: add(mid(a, b), scale(n, 1.2)), rotation: normAngle(rot), value } };
  }
  if (d.dimtype === 'diameter' || d.dimtype === 'radius') {
    const c = d.center!;
    const r = d.radius ?? 0;
    const u = unitAt(d.angle ?? 45);
    const far = add(c, scale(u, r));
    const near = d.dimtype === 'diameter' ? sub(c, scale(u, r)) : c;
    lines.push([near, far]);
    arrows.push({ tip: far, dir: u });
    if (d.dimtype === 'diameter') arrows.push({ tip: near, dir: [-u[0], -u[1]] });
    let rot = d.angle ?? 45;
    rot = normAngle(rot);
    if (rot > 90 && rot <= 270) rot += 180;
    return { lines, arcs, arrows, text: { at: add(mid(near, far), scale([-u[1], u[0]], 1.2)), rotation: normAngle(rot), value } };
  }
  const c = d.center!;
  const loc = d.location ?? d.p2!;
  const r = Math.max(dist(loc, c), 1e-3);
  const a1 = angleOf(sub(d.p1!, c));
  const a2 = angleOf(sub(d.p2!, c));
  let start = a1;
  let end = a2;
  if (arcSweep(start, end) > 180) [start, end] = [end, start];
  arcs.push({ c, r, start, end });
  for (const [p, a] of [[d.p1!, a1], [d.p2!, a2]] as [Pt, number][]) {
    const onArcPt = add(c, scale(unitAt(a), r));
    if (Math.abs(dist(p, c) - r) > 1e-6) lines.push([p, onArcPt]);
  }
  const s = add(c, scale(unitAt(start), r));
  const e = add(c, scale(unitAt(end), r));
  arrows.push({ tip: s, dir: unitAt(start - 90) }, { tip: e, dir: unitAt(end + 90) });
  const m = start + arcSweep(start, end) / 2;
  return { lines, arcs, arrows, text: { at: add(c, scale(unitAt(m), r + 2)), rotation: 0, value } };
}

// ─── bounding boxes, hit testing ───────────────────────────────────────────

export function loopPoints(lp: HatchLoop): Pt[] {
  if (lp.points) return lp.points;
  if (lp.center && lp.radius != null) {
    const r = lp.radius;
    return [
      [lp.center[0] - r, lp.center[1] - r],
      [lp.center[0] + r, lp.center[1] + r],
    ];
  }
  return [];
}

export function entityPoints(e: DraftEntity): Pt[] {
  switch (e.type) {
    case 'line':
      return [e.p1, e.p2];
    case 'polyline':
    case 'leader':
      return e.points;
    case 'circle':
    case 'arc':
      return [
        [e.center[0] - e.radius, e.center[1] - e.radius],
        [e.center[0] + e.radius, e.center[1] + e.radius],
      ];
    case 'text':
      return [e.insert, [e.insert[0] + e.text.length * e.height * 0.7, e.insert[1] + e.height]];
    case 'mtext': {
      const lines = e.text.split('\n');
      const w = Math.max(...lines.map((l) => l.length)) * e.height * 0.7;
      return [e.insert, [e.insert[0] + w, e.insert[1] - lines.length * e.height * 1.67]];
    }
    case 'insert':
      return [e.insert];
    case 'dimension': {
      const pts = [e.p1, e.p2, e.center, e.location].filter(Boolean) as Pt[];
      if (e.center && e.radius) pts.push([e.center[0] - e.radius, e.center[1] - e.radius], [e.center[0] + e.radius, e.center[1] + e.radius]);
      return pts;
    }
    case 'hatch':
      return e.loops.flatMap(loopPoints);
  }
}

export type BBox = [number, number, number, number];

export function bboxOf(points: Pt[]): BBox | null {
  if (!points.length) return null;
  let [x0, y0, x1, y1] = [Infinity, Infinity, -Infinity, -Infinity];
  for (const [x, y] of points) {
    x0 = Math.min(x0, x);
    y0 = Math.min(y0, y);
    x1 = Math.max(x1, x);
    y1 = Math.max(y1, y);
  }
  return [x0, y0, x1, y1];
}

function pointInPolygon(p: Pt, poly: Pt[]): boolean {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, yi] = poly[i];
    const [xj, yj] = poly[j];
    if (yi > p[1] !== yj > p[1] && p[0] < ((xj - xi) * (p[1] - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

/** Distance from p to an entity's drawn shape (model units); used for picking. */
export function distanceTo(e: DraftEntity, p: Pt): number {
  switch (e.type) {
    case 'line':
      return segDist(p, e.p1, e.p2);
    case 'circle':
      return Math.abs(dist(p, e.center) - e.radius);
    case 'arc': {
      const a = angleOf(sub(p, e.center));
      if (onArc(a, e.start_angle, e.end_angle)) return Math.abs(dist(p, e.center) - e.radius);
      const ends = [e.start_angle, e.end_angle].map((x) => add(e.center, scale(unitAt(x), e.radius)));
      return Math.min(...ends.map((q) => dist(p, q)));
    }
    case 'polyline':
    case 'leader': {
      const closed = e.type === 'polyline' ? e.closed : false;
      const bulges = e.type === 'polyline' ? e.bulges : null;
      let best = Infinity;
      for (const [a, b, bulge] of polySegments(e.points, bulges, closed)) {
        if (bulge && dist(a, b) > TOL) {
          const arc = bulgeArc(a, b, bulge);
          const ang = angleOf(sub(p, arc.c));
          best = Math.min(best, onArc(ang, arc.start, arc.end) ? Math.abs(dist(p, arc.c) - arc.r) : Math.min(dist(p, a), dist(p, b)));
        } else best = Math.min(best, segDist(p, a, b));
      }
      return best;
    }
    case 'text':
    case 'mtext': {
      const bb = bboxOf(entityPoints(e))!;
      return p[0] >= bb[0] && p[0] <= bb[2] && p[1] >= bb[1] && p[1] <= bb[3] ? 0 : Infinity;
    }
    case 'insert':
      return dist(p, e.insert);
    case 'dimension': {
      const g = dimensionGeometry(e);
      let best = dist(p, g.text.at);
      for (const [a, b] of g.lines) best = Math.min(best, segDist(p, a, b));
      for (const arc of g.arcs) best = Math.min(best, Math.abs(dist(p, arc.c) - arc.r));
      return best;
    }
    case 'hatch': {
      for (const lp of e.loops) {
        if (lp.points && lp.points.length >= 3 && pointInPolygon(p, lp.points)) return 0;
        if (lp.center && lp.radius != null && dist(p, lp.center) <= lp.radius) return 0;
      }
      return Infinity;
    }
  }
}

/** Topmost entity within `tol` of p (hatches only when nothing else is close). */
export function pick(entities: DraftEntity[], p: Pt, tol: number, isVisible: (e: DraftEntity) => boolean = () => true): DraftEntity | null {
  let best: DraftEntity | null = null;
  let bestD = Infinity;
  for (let i = entities.length - 1; i >= 0; i--) {
    const e = entities[i];
    if (!isVisible(e) || e.type === 'hatch') continue;
    const d = distanceTo(e, p);
    if (d <= tol && d < bestD - 1e-12) {
      best = e;
      bestD = d;
    }
  }
  if (best) return best;
  for (let i = entities.length - 1; i >= 0; i--) {
    const e = entities[i];
    if (e.type === 'hatch' && isVisible(e) && distanceTo(e, p) <= tol) return e;
  }
  return null;
}

/** Entities whose characteristic points all lie inside the window (left-to-right box selection). */
export function inWindow(entities: DraftEntity[], a: Pt, b: Pt): string[] {
  const [x0, x1] = [Math.min(a[0], b[0]), Math.max(a[0], b[0])];
  const [y0, y1] = [Math.min(a[1], b[1]), Math.max(a[1], b[1])];
  return entities
    .filter((e) => {
      const pts = entityPoints(e);
      return pts.length > 0 && pts.every(([x, y]) => x >= x0 && x <= x1 && y >= y0 && y <= y1);
    })
    .map((e) => e.id);
}

// ─── rigid transforms (move / rotate / copy) ───────────────────────────────

export function transformEntity<E extends DraftEntity>(e: E, dx: number, dy: number, rot: number, center: Pt): E {
  const tp = (p: Pt | null | undefined): Pt | null | undefined => {
    if (!p) return p;
    const q = rotate(p, center, rot);
    return cleanPt([q[0] + dx, q[1] + dy]);
  };
  const angle = (a: number) => clean(normAngle(a + rot));
  const x = { ...e } as DraftEntity;
  switch (x.type) {
    case 'line':
      x.p1 = tp(x.p1)!;
      x.p2 = tp(x.p2)!;
      break;
    case 'polyline':
    case 'leader':
      x.points = x.points.map((p) => tp(p)!);
      break;
    case 'circle':
      x.center = tp(x.center)!;
      break;
    case 'arc':
      x.center = tp(x.center)!;
      if (rot) {
        x.start_angle = angle(x.start_angle);
        x.end_angle = angle(x.end_angle);
      }
      break;
    case 'text':
    case 'mtext':
    case 'insert':
      x.insert = tp(x.insert)!;
      if (rot) x.rotation = angle(x.rotation);
      break;
    case 'dimension':
      x.p1 = tp(x.p1) ?? x.p1;
      x.p2 = tp(x.p2) ?? x.p2;
      x.center = tp(x.center) ?? x.center;
      x.location = tp(x.location) ?? x.location;
      if (rot && ['linear', 'diameter', 'radius'].includes(x.dimtype)) x.angle = angle(x.angle ?? 0);
      break;
    case 'hatch':
      x.loops = x.loops.map((lp) => (lp.points ? { ...lp, points: lp.points.map((p) => tp(p)!) } : { ...lp, center: tp(lp.center) ?? lp.center }));
      break;
  }
  return x as E;
}

export function selectionCenter(entities: DraftEntity[]): Pt {
  const bb = bboxOf(entities.flatMap(entityPoints));
  return bb ? [(bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2] : [0, 0];
}

// ─── hatch boundaries ──────────────────────────────────────────────────────

function polygonOf(e: DraftEntity): Pt[] | null {
  return e.type === 'polyline' && e.closed && e.points.length >= 3 ? e.points : null;
}

function polyArea(pts: Pt[]): number {
  let a = 0;
  for (let i = 0; i < pts.length; i++) {
    const [x1, y1] = pts[i];
    const [x2, y2] = pts[(i + 1) % pts.length];
    a += x1 * y2 - x2 * y1;
  }
  return Math.abs(a) / 2;
}

/** Hatch loops for a click at `p`: the smallest closed polyline / circle around it is the outer loop, and every
 * closed polyline / circle inside that boundary (holes) becomes an island, so it stays unhatched. */
export function hatchAt(entities: DraftEntity[], p: Pt): { loops: HatchLoop[]; refs: string[] } | null {
  let outer: DraftEntity | null = null;
  let outerArea = Infinity;
  for (const e of entities) {
    const poly = polygonOf(e);
    if (e.type === 'circle' && dist(p, e.center) <= e.radius) {
      const a = Math.PI * e.radius * e.radius;
      if (a < outerArea) [outer, outerArea] = [e, a];
    } else if (poly && pointInPolygon(p, poly)) {
      const a = polyArea(poly);
      if (a < outerArea) [outer, outerArea] = [e, a];
    }
  }
  if (!outer) return null;
  const o: DraftEntity = outer;
  const toLoop = (e: DraftEntity): HatchLoop =>
    e.type === 'circle' ? { center: e.center, radius: e.radius } : e.type === 'polyline' ? { points: e.points, bulges: e.bulges ?? null } : {};
  const outerPoly = polygonOf(o);
  const inside = (q: Pt) => (o.type === 'circle' ? dist(q, o.center) < o.radius : outerPoly ? pointInPolygon(q, outerPoly) : false);
  const islands = entities.filter((e) => {
    if (e.id === o.id) return false;
    if (e.type === 'circle') {
      // an island does not contain the clicked point and lies inside the boundary
      return dist(p, e.center) > e.radius && inside(e.center) && [0, 90, 180, 270].every((a) => inside(add(e.center, scale(unitAt(a), e.radius))));
    }
    const poly = polygonOf(e);
    return !!poly && !pointInPolygon(p, poly) && poly.every(inside);
  });
  return { loops: [toLoop(o), ...islands.map(toLoop)], refs: [o.id, ...islands.map((e) => e.id)] };
}

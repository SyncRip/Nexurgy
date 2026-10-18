// Page adapters → version-pinned AI references (.trellis/tasks/archive/2026-10/10-08-ai-workspace/design.md §3.1, §7.4).
//
// One builder per business object. Builders only say *which* object (and which
// part of it) the user picked; the server validates the object, fills the
// title / version label / values and computes the key. Nothing here reads page
// state on its own: every reference is built from an explicit user action.
// Pure functions (no React, no DOM) so they are unit-tested in refs.test.ts.

import type { AiCadSelection, AiExcerpt, AiOpCandidate, AiRefInput, QuoteFieldKey, Vec3 } from '../../api/assistant';
import type { FileKind, HoleRecord, ModelRevision, UUID } from '../../api/types';
import type { EdgePick, FacePick } from '../../three/viewer';

/** What useAssistant().addContext takes: a reference input plus the project it belongs to. */
export type AiRefWithProject = AiRefInput & { project_id: UUID };

// ─── simple objects ────────────────────────────────────────────────────────

export const partRef = (projectId: UUID, partId: UUID): AiRefWithProject => ({ kind: 'part', object_id: partId, project_id: projectId });

/** A saved model revision (never an unsaved editor state). */
export const partRevisionRef = (projectId: UUID, revisionId: UUID): AiRefWithProject => ({ kind: 'part_revision', object_id: revisionId, project_id: projectId });

export const quoteRef = (projectId: UUID, quoteId: UUID): AiRefWithProject => ({ kind: 'quote', object_id: quoteId, project_id: projectId });

/** Totals a quote tier offers besides its cost lines (design §3.1 quote_field). */
export const QUOTE_TOTAL_KEYS = ['unit_cost_fen', 'unit_inc_tax_fen', 'order_inc_tax_fen', 'lead_days'] as const satisfies readonly QuoteFieldKey[];
export type QuoteTotalKey = (typeof QUOTE_TOTAL_KEYS)[number];

/**
 * One field of one quantity tier: a cost line key (material, machining …) or a
 * total. Only the pointer is sent; the server reads value, currency, formula
 * and label from the stored quote (client numbers are never trusted).
 */
export const quoteFieldRef = (projectId: UUID, quoteId: UUID, tierQty: number, key: QuoteFieldKey): AiRefWithProject => ({
  kind: 'quote_field',
  object_id: quoteId,
  project_id: projectId,
  field: { tier_qty: tierQty, key },
});

export const assetVersionRef = (projectId: UUID, assetVersionId: UUID): AiRefWithProject => ({ kind: 'asset_version', object_id: assetVersionId, project_id: projectId });

/** One output row of one run: the exact asset version plus where it came from (not "the latest run"). */
export const runOutputRef = (projectId: UUID, runId: UUID, out: { node_id: string; output_key: string; asset_version_id: UUID }): AiRefWithProject => ({
  kind: 'run_output',
  object_id: out.asset_version_id,
  project_id: projectId,
  output: { run_id: runId, node_id: out.node_id, output_key: out.output_key },
});

export const runRef = (projectId: UUID, runId: UUID): AiRefWithProject => ({ kind: 'run', object_id: runId, project_id: projectId });

export const taskRef = (projectId: UUID, taskId: UUID): AiRefWithProject => ({ kind: 'task', object_id: taskId, project_id: projectId });

export const criterionRef = (projectId: UUID, criterionId: UUID): AiRefWithProject => ({ kind: 'criterion', object_id: criterionId, project_id: projectId });

export const fileVersionRef = (projectId: UUID, fileVersionId: UUID): AiRefWithProject => ({ kind: 'file_version', object_id: fileVersionId, project_id: projectId });

/** Customer drawing files the drawing intake operation accepts. */
export function isDrawingFileKind(kind: FileKind | string): boolean {
  return kind === 'drawing_pdf' || kind === 'drawing_image';
}

// ─── text excerpts ─────────────────────────────────────────────────────────

/** Server limit for excerpt text (characters). */
export const EXCERPT_MAX = 2000;

/** Trim and cap selected text; null when nothing is left. Counted in code points so emoji / CJK are never split. */
export function clampExcerpt(raw: string): { text: string; truncated: boolean } | null {
  const trimmed = raw.trim();
  if (!trimmed) return null;
  const chars = Array.from(trimmed);
  if (chars.length <= EXCERPT_MAX) return { text: trimmed, truncated: false };
  return { text: chars.slice(0, EXCERPT_MAX).join('').trimEnd(), truncated: true };
}

/** Selected text of a source object, explicitly only a text reference. Null when the text is empty. */
export function textExcerptRef(projectId: UUID, sourceKind: AiExcerpt['source_kind'], sourceId: UUID, text: string): AiRefWithProject | null {
  const c = clampExcerpt(text);
  if (!c) return null;
  return { kind: 'text_excerpt', object_id: sourceId, project_id: projectId, excerpt: { source_kind: sourceKind, text: c.text } };
}

export const EXCERPT_SOURCE_KINDS: readonly AiExcerpt['source_kind'][] = ['quote', 'task', 'asset_version', 'part_revision', 'criterion'];

export function isExcerptSourceKind(v: string | null | undefined): v is AiExcerpt['source_kind'] {
  return !!v && (EXCERPT_SOURCE_KINDS as readonly string[]).includes(v);
}

// ─── CAD selection ─────────────────────────────────────────────────────────

const round = (v: number, d = 4) => {
  const f = 10 ** d;
  const r = Math.round(v * f) / f;
  return Object.is(r, -0) ? 0 : r;
};
const vec = (v: readonly number[] | null | undefined): Vec3 | null => (v && v.length >= 3 && v.slice(0, 3).every(Number.isFinite) ? [round(v[0]), round(v[1]), round(v[2])] : null);
const num = (v: number | null | undefined): number | null => (typeof v === 'number' && Number.isFinite(v) ? round(v) : null);

/**
 * A face / edge picked on the viewport of a *saved* revision. Face / edge ids
 * belong to that build only, so the revision's build_key and mesh_sha go with
 * them (the server rejects a mismatch). Units mm, world frame +Z up.
 */
export function cadSelectionRef(
  projectId: UUID,
  revision: Pick<ModelRevision, 'id' | 'build_key' | 'mesh_sha'>,
  pick: FacePick | EdgePick,
  opts: { candidates?: AiOpCandidate[]; resolvedOpId?: string | null } = {},
): AiRefWithProject {
  const selection: AiCadSelection = {
    entity: pick.kind,
    id: pick.id,
    build_key: revision.build_key,
    mesh_sha: revision.mesh_sha,
    units: 'mm',
    frame: 'world_mm_z_up',
    point: vec(pick.point),
  };
  if (pick.kind === 'face') {
    selection.surface = pick.surface;
    selection.normal = vec(pick.normal);
    selection.center = vec(pick.center);
    selection.area = num(pick.area);
    if (pick.radius !== undefined) selection.radius = num(pick.radius);
  } else {
    selection.edge_kind = pick.edgeKind;
    selection.length = num(pick.length);
  }
  if (opts.candidates?.length) selection.candidates = opts.candidates;
  if (opts.resolvedOpId) selection.resolved_op_id = opts.resolvedOpId;
  return { kind: 'cad_selection', object_id: revision.id, project_id: projectId, selection };
}

/** Short human scope of a pick: "Face #12" style parts for labels (ids only, no language). */
export function pickToken(pick: Pick<FacePick, 'kind' | 'id'> | Pick<EdgePick, 'kind' | 'id'>): string {
  return pick.kind === 'face' ? `#${pick.id}` : `E${pick.id}`;
}

// ─── hole candidates (design §7.4, U23) ────────────────────────────────────

/** Radius tolerance: 0.05 mm or 1 % of the hole radius, whichever is larger. */
export const HOLE_RADIUS_TOL_MM = 0.05;
export const HOLE_RADIUS_TOL_REL = 0.01;
/** The pick may lie up to radius + 0.2 mm from the hole axis. */
export const HOLE_AXIS_SLACK_MM = 0.2;
/** The server accepts at most 20 candidates per selection. */
export const MAX_HOLE_CANDIDATES = 20;
/** When the picked face's own axis is known it must be parallel to the hole axis (≈ 5°). */
const PARALLEL_COS = 0.996;

export interface HoleMatch {
  opId: string;
  /** Which cylinder of the hole op matched: the drilled bore or its counterbore. */
  feature: 'bore' | 'counterbore';
  /** Diameter of the matched cylinder (mm). */
  diameter: number;
  /** Radius of the picked face (mm). */
  faceRadius: number;
  /** 1-based instance of the hole pattern whose axis is nearest to the pick. */
  instance: number;
  count: number;
  radiusDelta: number;
  axisDistance: number;
}

export interface HolePick {
  surface: string;
  radius?: number | null;
  point: readonly number[];
}

const sub = (a: readonly number[], b: readonly number[]) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const dot = (a: readonly number[], b: readonly number[]) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const cross = (a: readonly number[], b: readonly number[]) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const len = (a: readonly number[]) => Math.hypot(a[0], a[1], a[2]);
const fmt3 = (v: number) => String(round(v, 3));

/** Distance from point p to the infinite line through o with direction d (unit). */
export function distanceToAxis(p: readonly number[], o: readonly number[], d: readonly number[]): number {
  return len(cross(sub(p, o), d));
}

/** Default (English) reason; pages pass a translated one. */
export function describeHoleMatch(m: HoleMatch): string {
  const what = m.feature === 'counterbore' ? `counterbore Ø${fmt3(m.diameter)}` : `Ø${fmt3(m.diameter)}`;
  const inst = m.count > 1 ? `instance ${m.instance} of ${m.count}` : 'the hole';
  return `cylinder r=${fmt3(m.faceRadius)} mm matches ${what} (Δ${fmt3(m.radiusDelta)} mm); pick ${fmt3(m.axisDistance)} mm from the axis of ${inst}`;
}

export function holeCandidateLabel(h: Pick<HoleRecord, 'op_id' | 'diameter' | 'count'>, feature: HoleMatch['feature'] = 'bore', diameter = h.diameter): string {
  return `${h.op_id} ${feature === 'counterbore' ? '⌴' : ''}Ø${fmt3(diameter)}${h.count > 1 ? ` ×${h.count}` : ''}`;
}

/**
 * Hole records a picked face may belong to: a cylindrical face whose radius is
 * ≈ the hole (or counterbore) diameter / 2 and whose pick point lies near the
 * axis through one of the hole's instance positions. Returns one candidate per
 * hole op, nearest first; [] when nothing matches (the pick is then only
 * geometry — no promise that the original feature can be changed).
 */
export function matchHoleCandidates(
  pick: HolePick,
  holes: readonly HoleRecord[],
  opts: { faceAxis?: { direction: readonly number[] } | null; describe?: (m: HoleMatch) => string } = {},
): AiOpCandidate[] {
  if (pick.surface !== 'cylinder' || typeof pick.radius !== 'number' || !(pick.radius > 0)) return [];
  if (!pick.point || pick.point.length < 3 || !pick.point.slice(0, 3).every(Number.isFinite)) return [];
  const faceRadius = pick.radius;
  const fd = opts.faceAxis?.direction;
  const faceDir = fd && fd.length >= 3 && len(fd) > 0 ? fd.map((c) => c / len(fd)) : null;
  const describe = opts.describe ?? describeHoleMatch;
  const matches: HoleMatch[] = [];

  for (const h of holes) {
    if (!h.axis || h.axis.length < 3 || !(len(h.axis) > 0)) continue;
    const axis = h.axis.map((c) => c / len(h.axis));
    if (faceDir && Math.abs(dot(faceDir, axis)) < PARALLEL_COS) continue;
    const positions = h.positions?.length ? h.positions : Array.isArray(h.entry) && h.entry.length >= 3 ? [h.entry] : [];
    if (!positions.length) continue;
    const cylinders: { feature: HoleMatch['feature']; diameter: number }[] = [{ feature: 'bore', diameter: h.diameter }];
    const cb = h.counterbore?.diameter;
    if (typeof cb === 'number' && cb > h.diameter) cylinders.push({ feature: 'counterbore', diameter: cb });

    let best: HoleMatch | null = null;
    for (const c of cylinders) {
      if (!(c.diameter > 0)) continue;
      const r = c.diameter / 2;
      const radiusDelta = Math.abs(faceRadius - r);
      if (radiusDelta > Math.max(HOLE_RADIUS_TOL_MM, HOLE_RADIUS_TOL_REL * r)) continue;
      for (let i = 0; i < positions.length; i++) {
        const pos = positions[i];
        if (!pos || pos.length < 3) continue;
        const d = distanceToAxis(pick.point, pos, axis);
        if (d > r + HOLE_AXIS_SLACK_MM) continue;
        if (!best || d < best.axisDistance - 1e-9 || (Math.abs(d - best.axisDistance) <= 1e-9 && radiusDelta < best.radiusDelta)) {
          best = { opId: h.op_id, feature: c.feature, diameter: c.diameter, faceRadius, instance: i + 1, count: positions.length, radiusDelta, axisDistance: d };
        }
      }
    }
    if (best) matches.push(best);
  }

  matches.sort((a, b) => a.axisDistance - b.axisDistance || a.radiusDelta - b.radiusDelta || a.opId.localeCompare(b.opId));
  return matches.slice(0, MAX_HOLE_CANDIDATES).map((m) => {
    const h = holes.find((x) => x.op_id === m.opId)!;
    return {
      op_id: m.opId,
      kind: 'hole',
      label: holeCandidateLabel({ op_id: h.op_id, diameter: h.diameter, count: m.count }, m.feature, m.diameter),
      diameter: round(h.diameter, 4),
      reason: describe(m),
    };
  });
}

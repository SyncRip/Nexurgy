// W3 geometry library: op / sketch / workplane types (mirror of server/app/cad/sketch.py and ops.py), the part
// template catalog and the feature-check report. Units mm and degrees, world +Z up.

import { request } from './client';
import type { BodyMode, ModelRevision, Profile, UUID, Vec2, Vec3 } from './types';

// ─── planes and sketches ─────────────────────────────────────────────────────

/** An arbitrary plane: origin, normal and the in-plane x direction (projected; default by the dominant-axis rule). */
export interface Workplane {
  origin?: Vec3;
  normal?: Vec3;
  x_dir?: Vec3 | null;
}

/** A plane from a named datum of an earlier op ("F-01.top"), shifted along its normal. */
export interface DatumRef {
  ref: string;
  offset?: number;
  flip?: boolean;
}

export type PlaneName = 'XY' | 'XZ' | 'YZ';
export type PlaneSpec = PlaneName | Workplane | DatumRef;

export type Segment = { kind: 'line'; to: Vec2 } | { kind: 'arc'; to: Vec2; center: Vec2; ccw?: boolean };

export interface SketchLoop {
  start: Vec2;
  segments: Segment[];
}

/** Closed outline of lines and arcs (auto-closed with a line), optional inner loops. */
export interface SketchProfile extends SketchLoop {
  kind: 'sketch';
  holes?: SketchLoop[];
}

export type PathSpec = SketchLoop;

// ─── ops ───────────────────────────────────────────────────────────────────

interface OpBase {
  id: string;
  label?: string | null;
  suppressed?: boolean;
}

interface BodyBase extends OpBase {
  mode?: BodyMode;
  rotate?: Vec3;
}

export interface CutOp extends OpBase {
  type: 'cut';
  profile: Profile;
  plane?: PlaneSpec;
  offset?: number;
  at?: Vec2;
  rotate_profile?: number;
  /** null = through all material ahead (against the plane normal). */
  depth?: number | null;
}

export interface RevolveOp extends BodyBase {
  type: 'revolve';
  /** Sketch in (u axial, v radius ≥ 0). */
  profile: SketchProfile;
  at?: Vec3;
  axis?: Vec3;
  ref_dir?: Vec3 | null;
  angle?: number;
}

export interface SweepOp extends BodyBase {
  type: 'sweep';
  profile: Profile;
  path: PathSpec;
  plane?: PlaneSpec;
}

export interface BentPlateOp extends BodyBase {
  type: 'bent_plate';
  thickness: number;
  width: number;
  legs: number[];
  bends: number[];
  inner_radius?: number;
  leg_length?: 'outside' | 'flat';
  plane?: PlaneSpec;
  offset?: number;
  at?: Vec2;
}

export interface ShellOp extends OpBase {
  type: 'shell';
  thickness: number;
  open_faces?: Vec3[];
}

export interface LinearLayout {
  kind: 'linear';
  direction: Vec3;
  spacing: number;
  count: number;
  direction2?: Vec3 | null;
  spacing2?: number;
  count2?: number;
}

export interface CircularLayout {
  kind: 'circular';
  axis_origin?: Vec3;
  axis?: Vec3;
  count: number;
  step_angle?: number | null;
}

export interface PatternOp extends OpBase {
  type: 'pattern';
  target: string;
  layout: LinearLayout | CircularLayout;
}

/** A round or rectangular wire swept along a helix (server/app/cad/coil.py): springs, split lock washers. Made by the
 * spring templates or the AI; the editor shows it read-only. */
export interface CoilOp extends BodyBase {
  type: 'coil';
  section: { kind: 'circle'; radius: number } | { kind: 'rect'; width: number; length: number; corner_radius?: number };
  mean_diameter: number;
  turns: number;
  pitch?: number | null;
  length?: number | null;
  handedness?: 'right' | 'left';
  start_angle?: number;
  end_form?: 'open' | 'closed' | 'closed_ground';
  at?: Vec3;
  axis?: Vec3;
}

export type GeometryOp = CutOp | RevolveOp | SweepOp | BentPlateOp | ShellOp | PatternOp | CoilOp;

// ─── datums, feature checks, templates ────────────────────────────────────

export interface Datum {
  name: string;
  kind: 'frame' | 'axis' | 'plane' | 'point';
  origin: number[];
  direction: number[];
  x_dir: number[];
}

export type FeatureCheckStatus = 'passed' | 'failed' | 'unknown' | 'skipped_turbo';

export interface FeatureCheckItem {
  op_id: string;
  kind: string;
  status: 'passed' | 'failed' | 'not_evaluated';
  expected: Record<string, unknown>;
  measured: Record<string, unknown>;
  tolerance: Record<string, number>;
  probes: number;
  message: string;
  failures: string[];
}

/** checks.features of a revision / feature_check of a preview (server/app/cad/feature_checks.py). */
export interface FeatureCheckReport {
  schema: string;
  checker_version: string;
  execution_policy: 'standard' | 'turbo';
  status: FeatureCheckStatus;
  build_key: string | null;
  checks: { check: string; status: FeatureCheckStatus; items?: FeatureCheckItem[] }[];
}

export type Representation = 'exact' | 'nominal' | 'simplified';
export type Procurement = 'standard' | 'buy' | 'make';

export interface TemplateParamSpec {
  name: string;
  type: 'float' | 'int' | 'choice' | 'text';
  default: number | string;
  unit: string | null;
  min?: number;
  max?: number;
  choices?: string[];
  label: { en: string; zh: string };
}

export interface TemplateCatalogEntry {
  id: string;
  version: number;
  title: { en: string; zh: string };
  category: string;
  representation: Representation;
  procurement: Procurement;
  standard: string | null;
  params: TemplateParamSpec[];
  ports: string[];
}

/** checks.template of a revision / meta of an instantiation (server/app/cad/templates.py). */
export interface TemplateMeta {
  template_id: string;
  template_version: number;
  title: { en: string; zh: string };
  category: string;
  params: Record<string, number | string>;
  representation: Representation;
  procurement: Procurement;
  material: string | null;
  material_source: 'parameter' | 'unspecified';
  standard: string | null;
  spec_label: string;
  notes: string[];
  ports: Datum[];
  op_ids: string[];
  modified_ops?: string[];
  removed_ops?: string[];
}

export type TemplateParams = Record<string, number | string>;

export interface TemplateRevisionCreate {
  template_id: string;
  params: TemplateParams;
  parent_id?: UUID | null;
  mode?: 'replace' | 'append';
  note?: string;
}

type Opt = { signal?: AbortSignal };

/** GET /cad/templates — the part template catalog. */
export const listTemplates = (o: Opt = {}) => request<{ templates: TemplateCatalogEntry[] }>('/cad/templates', { signal: o.signal });

/** POST /cad/templates/{id}/instantiate — ops + meta, nothing saved; 422 template_param_invalid with details.problems. */
export const instantiateTemplate = (templateId: string, params: TemplateParams, o: Opt = {}) =>
  request<{ ops: unknown[]; meta: TemplateMeta }>(`/cad/templates/${encodeURIComponent(templateId)}/instantiate`, {
    method: 'POST',
    body: { params },
    signal: o.signal,
  });

/** POST /parts/{id}/revisions/template — new revision from a template (replace the design or append). */
export const createTemplateRevision = (partId: UUID, body: TemplateRevisionCreate, o: Opt = {}) =>
  request<ModelRevision>(`/parts/${encodeURIComponent(partId)}/revisions/template`, { method: 'POST', body, signal: o.signal });

/** GET /revisions/{id}/datums — semantic datums (origin, <op>.<side>, <op>.axis[i], port.<name>). */
export const getRevisionDatums = (revisionId: UUID, o: Opt = {}) =>
  request<{ datums: Datum[] }>(`/revisions/${encodeURIComponent(revisionId)}/datums`, { signal: o.signal });

/** Typed views of the W3 parts of a revision's checks. */
export function revisionTemplate(rev: Pick<ModelRevision, 'checks'> | null | undefined): TemplateMeta | null {
  const tpl = rev?.checks ? (rev.checks as Record<string, unknown>).template : null;
  return tpl && typeof tpl === 'object' ? (tpl as TemplateMeta) : null;
}

export function revisionFeatureCheck(rev: Pick<ModelRevision, 'checks'> | null | undefined): FeatureCheckReport | null {
  const rep = rev?.checks ? (rev.checks as Record<string, unknown>).features : null;
  return rep && typeof rep === 'object' ? (rep as FeatureCheckReport) : null;
}

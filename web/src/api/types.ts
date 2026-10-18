// TypeScript mirrors of the PartRoute API.
//
// Implemented sections mirror the server code they name (pydantic schemas in
// server/app/api/schemas.py, and the dict builders revision_out, plan_out /
// _plan_view, quote_out / _quote_view, part_status, intake_out, _wf_out,
// _run_summary / run_out, node_catalog, template_list, drawing_out,
// config_out). When one of those builders changes, change the type here.

import type { Assumption,ExecutionPolicy,Verification } from './engineering';
import type { Datum,FeatureCheckReport,GeometryOp,PlaneSpec,SketchProfile,Workplane } from './geometry';
import type { PolicySource,SourceVersion } from './turbo';

export type UUID = string;
/** ISO 8601 timestamp with timezone, as serialised by pydantic. */
export type ISODateTime = string;
/** Hex sha256 of a stored blob. */
export type Sha256 = string;

// ─── enums (implemented: server/app/models.py) ────────────────────────────

export const ROLES = ['owner', 'designer', 'process', 'quoter', 'viewer'] as const;
export type Role = (typeof ROLES)[number];

export const PART_STATUSES = ['draft', 'in_design', 'in_review', 'released', 'archived'] as const;
export type PartStatus = (typeof PART_STATUSES)[number];

export const FILE_KINDS = ['model_step', 'model_mesh', 'drawing_pdf', 'drawing_image', 'drawing_dxf', 'document', 'other'] as const;
export type FileKind = (typeof FILE_KINDS)[number];

export type ProjectStatus = 'active' | 'archived';

export const REVISION_SOURCES = ['manual', 'ai', 'import', 'copy'] as const;
export type RevisionSource = (typeof REVISION_SOURCES)[number];
export type RevisionStatus = 'ready' | 'failed';

export const APPROVAL_STAGES = ['design', 'process', 'quote'] as const;
export type ApprovalStage = (typeof APPROVAL_STAGES)[number];
export type ApprovalSubjectType = 'model_revision' | 'drawing' | 'process_plan' | 'quote' | 'intake';
export type ApprovalDecision = 'approved' | 'rejected';

export const RUN_STATUSES = ['queued', 'running', 'waiting', 'succeeded', 'partial', 'failed', 'cancelled'] as const;
export type RunStatus = (typeof RUN_STATUSES)[number];
export const NODE_STATUSES = ['pending', 'ready', 'running', 'waiting_human', 'succeeded', 'failed', 'cancelled', 'skipped'] as const;
export type NodeStatus = (typeof NODE_STATUSES)[number];
export type RunOrigin = 'workflow' | 'workspace';

export const PROVIDERS = ['gemini', 'openai', 'openai_compatible'] as const;
export type Provider = (typeof PROVIDERS)[number];

// ─── system (implemented) ─────────────────────────────────────────────────

export interface Healthz {
  ok: boolean;
}

export type KernelCapability = { ok: true; version: string } | { ok: false; error: string };

export interface ProviderCapability {
  /** Enabled model configs for this provider. */
  configs: number;
  /** How many of them have a usable key (environment reference set, or a stored key the server can decrypt; not tested). */
  with_key: number;
  /** Enabled, key usable and no other issue (same resolver as the model list; still not a connection test). */
  ready?: number;
}

export type FeatureName = 'collaboration' | 'tasks' | 'stages' | 'process_modules' | 'template_library' | 'turbo_choice' | 'model_choice';
export type FeatureFlags = Record<FeatureName, boolean>;

export interface Capabilities {
  /** Required on current servers; absent only on servers predating the feature contract. */
  features?: FeatureFlags;
  database: { ok: boolean };
  storage: { backend: 'local' | 'gcs' | string };
  kernel: KernelCapability;
  ai: {
    providers: Record<string, ProviderCapability>;
    any_ready: boolean;
    /** Whether direct API keys can be saved / used on this server (a credential master key is configured). */
    direct_keys?: { available: boolean };
  };
  worker: { inline: boolean; threads: number };
  /** S2: every entry's input limit and the context budgets (server/app/ai/limits.py); absent on older servers. */
  limits?: SystemLimits;
}

/** S2: names of the input limits (characters) the server defines once; inputs read their maxLength from here. */
export type InputLimitKey =
  | 'chat'
  | 'material'
  | 'description'
  | 'long_note'
  | 'objective'
  | 'role_prompt'
  | 'intake_hints'
  | 'drawing_text'
  | 'drafting_text'
  | 'drafting_mtext'
  | 'team_display_name'
  | 'team_profile'
  | 'team_tone'
  | 'team_personality'
  | 'team_skill_name'
  | 'team_skill_description'
  | 'team_skill_instructions'
  | 'team_total';

export interface SystemLimits {
  inputs: Record<InputLimitKey, number>;
  /** Context budgets in characters (model context block, capped references, material digest / notes, spec chunks, planner). */
  context: Record<string, number>;
}

// ─── auth (implemented) ───────────────────────────────────────────────────

export interface User {
  id: UUID;
  email: string;
  name: string;
  is_admin: boolean;
}

export interface LoginRequest {
  email: string;
  password: string;
}

export interface LoginResponse {
  token: string;
  user: User;
}

// ─── projects & members (implemented) ─────────────────────────────────────

export interface Project {
  id: UUID;
  code: string;
  name: string;
  description: string;
  status: ProjectStatus;
  created_at: ISODateTime;
  updated_at: ISODateTime;
  archived_at: ISODateTime | null;
  my_role: Role | null;
  /** Non-archived parts. Not returned by PATCH / archive responses (null). */
  part_count: number | null;
  /** S5 "default Turbo" switch: the policy of a request that names none and continues from no version (absent = standard). */
  default_execution_policy?: ExecutionPolicy;
  version?: number;
}

export interface ProjectCreate {
  /** 2–40 chars, ^[A-Za-z0-9][A-Za-z0-9_.-]*$ */
  code: string;
  name: string;
  description?: string;
}

export interface ProjectPatch {
  name?: string;
  description?: string;
  /** S5: the project's "default Turbo" switch (project managers only). */
  default_execution_policy?: ExecutionPolicy;
  expected_version?: number;
}

export interface ProjectListQuery {
  include_archived?: boolean;
  q?: string;
}

/** Extra grants on top of the role (project_members.capabilities). */
export const MEMBER_CAPABILITIES = ['template_manager'] as const;
export type MemberCapability = (typeof MEMBER_CAPABILITIES)[number];

export interface Member {
  user: User;
  role: Role;
  /** Extra grants; absent on servers that predate capabilities. */
  capabilities?: MemberCapability[];
}

export interface MemberUpsert {
  email: string;
  role: Role;
  /** Omit to keep the current capabilities. */
  capabilities?: MemberCapability[];
}

// ─── parts (implemented) ──────────────────────────────────────────────────

/** Compact summary of the newest model revision, embedded in PartOut. */
export interface LatestRevisionRef {
  id: UUID;
  rev_no: number;
  source: RevisionSource;
  status: RevisionStatus;
  mesh_only: boolean;
  created_at: ISODateTime;
}

export interface Part {
  id: UUID;
  project_id: UUID;
  number: string;
  name: string;
  material: string;
  status: PartStatus;
  owner_id: UUID | null;
  tags: string[];
  folder: string;
  description: string;
  source_part_id: UUID | null;
  created_at: ISODateTime;
  updated_at: ISODateTime;
  archived_at: ISODateTime | null;
  latest_revision: LatestRevisionRef | null;
}

export interface PartCreate {
  number: string;
  name: string;
  material?: string;
  tags?: string[];
  folder?: string;
  description?: string;
  owner_id?: UUID | null;
}

export interface PartPatch {
  name?: string;
  material?: string;
  status?: PartStatus;
  tags?: string[];
  folder?: string;
  description?: string;
  owner_id?: UUID | null;
}

export interface PartListQuery {
  q?: string;
  status?: PartStatus | '';
  tag?: string;
  folder?: string;
  include_archived?: boolean;
}

export interface PartCopyRequest {
  target_project_id: UUID;
  /** Defaults to the source number (or "<number>-COPY" inside the same project). */
  number?: string;
}

// ─── files & blobs (implemented) ──────────────────────────────────────────

export interface FileVersion {
  id: UUID;
  file_id: UUID;
  version: number;
  blob_sha256: Sha256;
  original_name: string;
  note: string;
  created_at: ISODateTime;
  size: number | null;
  content_type: string | null;
}

/** A project file (named ProjectFile to avoid clashing with the DOM File type). */
export interface ProjectFile {
  id: UUID;
  project_id: UUID;
  part_id: UUID | null;
  kind: FileKind;
  name: string;
  source_file_id: UUID | null;
  created_at: ISODateTime;
  archived_at: ISODateTime | null;
  /** Newest first. */
  versions: FileVersion[];
}

export interface FileListQuery {
  part_id?: UUID;
  kind?: FileKind | '';
  include_archived?: boolean;
}

export interface FileUpload {
  file: Blob & { name?: string };
  kind?: FileKind;
  part_id?: UUID | null;
  note?: string;
}

export interface FileVersionUpload {
  file: Blob & { name?: string };
  note?: string;
}

/** PATCH /files/{id} takes query parameters, not a JSON body. */
export interface FilePatch {
  name?: string;
  part_id?: UUID;
  archived?: boolean;
}

// ─── modeling operations (implemented: server/app/cad/ops.py) ─────────────

export type Vec2 = [number, number];
export type Vec3 = [number, number, number];
export type BodyMode = 'new' | 'add' | 'subtract' | 'intersect';

interface OpBase {
  /** F-01, F-02 … monotonically increasing within a part, never reused (F-00 = imported base). */
  id: string;
  label?: string | null;
  suppressed?: boolean;
}

interface BodyOpBase extends OpBase {
  mode?: BodyMode;
  rotate?: Vec3;
}

export interface BoxOp extends BodyOpBase {
  type: 'box';
  size: Vec3;
  /** Centre of the bottom face. */
  at?: Vec3;
}

export interface CylinderOp extends BodyOpBase {
  type: 'cylinder';
  radius: number;
  height: number;
  /** Centre of the base circle. */
  at?: Vec3;
  axis?: 'x' | 'y' | 'z';
}

export type Profile =
  | { kind: 'rect'; width: number; length: number; corner_radius?: number }
  | { kind: 'circle'; radius: number }
  | { kind: 'slot'; length: number; width: number }
  | { kind: 'polygon'; points: Vec2[] }
  | { kind: 'ring'; outer_radius: number; inner_radius: number }
  | SketchProfile;

export interface ExtrudeOp extends BodyOpBase {
  type: 'extrude';
  profile: Profile;
  /** Named plane, arbitrary workplane or a datum of an earlier op (W3). */
  plane?: PlaneSpec;
  offset?: number;
  at?: Vec2;
  height: number;
  rotate_profile?: number;
}

export type HolePattern = { kind: 'linear'; count: [number, number]; pitch: Vec2 } | { kind: 'circular'; count: number; radius: number };

export interface HoleOp extends OpBase {
  type: 'hole';
  at: Vec3;
  direction?: Vec3;
  diameter: number;
  /** null = through. */
  depth?: number | null;
  counterbore?: { diameter: number; depth: number } | null;
  countersink?: { diameter: number; angle?: number } | null;
  pattern?: HolePattern | null;
  /** W3: local workplane; `at` / `direction` are then plane coordinates. */
  plane?: Workplane | null;
  /** W3: start the tool outside an inclined entry face (no lip in the mouth). */
  flush_entry?: boolean | null;
}

export type EdgeGroup = 'all' | 'vertical' | 'top' | 'bottom' | 'top_outer' | 'bottom_outer';

export interface EdgeSelector {
  group?: EdgeGroup;
  near?: Vec3 | null;
  max_count?: number | null;
}

export interface FilletOp extends OpBase {
  type: 'fillet';
  radius: number;
  edges: EdgeSelector;
}

export interface ChamferOp extends OpBase {
  type: 'chamfer';
  distance: number;
  edges: EdgeSelector;
}

export interface TransformOp extends OpBase {
  type: 'transform';
  target: string;
  translate?: Vec3;
  rotate?: Vec3;
}

export type ModelOp = BoxOp | CylinderOp | ExtrudeOp | HoleOp | FilletOp | ChamferOp | TransformOp | GeometryOp;
export type ModelOpType = ModelOp['type'];

export interface StepBase {
  kind: 'step';
  blob_sha256: Sha256;
}

// ─── kernel results (implemented: server/app/cad/results.py) ──────────────

export interface BuildIssue {
  op_id: string | null;
  code: string;
  message: string;
}

export interface BBox {
  min: number[];
  max: number[];
  size: number[];
}

export interface BuildMetrics {
  valid: boolean;
  solids: number;
  faces: number;
  edges: number;
  volume_mm3: number;
  area_mm2: number;
  bbox: BBox;
  center_of_mass: number[];
}

/** Metrics stored for a mesh-reference import (no B-rep). */
export interface MeshMetrics {
  valid: false;
  volume_mm3: number | null;
  area_mm2: number | null;
  bbox: BBox | null;
  triangles: number;
  watertight: boolean;
  units_assumed: string;
}

export interface FeatureRecord {
  op_id: string;
  type: string;
  /** "skipped" = after the first failing op (not replayed). */
  status: 'ok' | 'error' | 'suppressed' | 'skipped';
  summary: string;
  detail: Record<string, unknown>;
}

export interface HoleRecord {
  op_id: string;
  diameter: number;
  depth: number | null;
  through: boolean;
  axis: number[];
  entry: number[];
  count: number;
  counterbore: Record<string, number> | null;
  countersink: Record<string, number> | null;
  positions: number[][];
}

/** Heuristic bore found on an imported STEP base — must be confirmed before use. */
export interface DetectedHole {
  source: string;
  confidence: 'heuristic';
  diameter: number;
  length: number;
  through: boolean | null;
  axis: number[];
  count: number;
  positions: number[][];
}

/** BuildResult.summary() — JSON view of one kernel build. */
export interface BuildSummary {
  ok: boolean;
  build_key: string;
  kernel_version: string;
  errors: BuildIssue[];
  warnings: BuildIssue[];
  /** On failure: the last good state before the failing op, or null. */
  metrics: BuildMetrics | null;
  features: FeatureRecord[];
  holes: HoleRecord[];
  detected_holes: DetectedHole[];
  artifact_sizes: { step: number; glb: number; mesh_json: number };
  /** W3 (preview only): independent feature check of the draft, null when not built / no W3 feature. */
  feature_check?: FeatureCheckReport | null;
  /** W3 (preview only): semantic datums of the draft. */
  datums?: Datum[];
}

/** Editor selection mesh (mm, +Z up), design.md §2. */
export interface MeshJson {
  faces: { id: string | number; surface: string; normal?: number[]; center: number[]; area: number; positions: number[]; indices: number[] }[];
  edges: { id: string | number; kind: string; points: number[][] }[];
  bbox: BBox | Record<string, unknown>;
}

// ─── model revisions (implemented: services/revisions.py revision_out) ────

export interface ModelRevision {
  id: UUID;
  part_id: UUID;
  rev_no: number;
  /** Display label, "R<rev_no>". */
  label: string;
  parent_id: UUID | null;
  source: RevisionSource;
  status: RevisionStatus;
  base: StepBase | null;
  ops: ModelOp[];
  build_key: string | null;
  kernel_version: string | null;
  metrics: BuildMetrics | MeshMetrics | Record<string, never>;
  features: FeatureRecord[];
  holes: HoleRecord[];
  detected_holes: DetectedHole[];
  warnings: BuildIssue[];
  step_sha: Sha256 | null;
  glb_sha: Sha256 | null;
  mesh_sha: Sha256 | null;
  /** STL/OBJ/GLB reference: preview and bbox only, no STEP, not editable. */
  mesh_only: boolean;
  editable: boolean;
  material: string;
  request_text: string | null;
  ai_summary: Record<string, unknown> | null;
  /** The accepted requirement list (CAD AI revisions); null for manual edits, imports and older revisions. */
  requirements?: Requirement[] | null;
  /**
   * Server reports bound to this revision's STEP: `conformance` (AI revisions), `step_audit` (STEP imports),
   * `profile` + `profile_check` + `source` (DXF / DWG PROFILE imports), `source` + `evidence` (reconstruction).
   */
  checks?: RevisionChecks | null;
  /** Compact verdict of the requirement check; null when the revision has no requirement contract. */
  check_status?: CheckStatus | null;
  /** W0 verification block (policy, check / acceptance status, unverified, assumptions); later checks included. */
  verification?: Verification | null;
  note: string;
  source_revision_id: UUID | null;
  source_file_version_id: UUID | null;
  node_run_id: UUID | null;
  created_by: UUID;
  created_at: ISODateTime | null;
  /** Next free op id for this part ("F-07"); ids are never reused. */
  next_op_id: string | null;
}

export interface ModelSpec {
  base?: StepBase | null;
  ops: ModelOp[];
}

/** POST /cad/preview body. */
export interface CadPreviewRequest extends ModelSpec {
  part_id: UUID;
}

/** POST /cad/preview response (not persisted). */
export interface CadPreviewResponse {
  result: BuildSummary;
  mesh: MeshJson | null;
}

/** POST /parts/{id}/revisions body (manual save from the editor). */
export interface RevisionCreate extends ModelSpec {
  parent_id?: UUID | null;
  note?: string;
}

/** Strict PROFILE import of a DXF / DWG (api/revisions.py DxfProfileIn): thickness and scale have no defaults. */
export interface DxfProfile {
  layer?: string;
  thickness_mm: number;
  /** part_mm = drawing_units × scale (a 1:2 drawing needs 2). */
  scale: number;
  base_z?: number;
  /** Declares the unit of a unitless file (only mm); omitted = the file's header decides. */
  units?: 'mm' | null;
}

/** POST /parts/{id}/revisions/import body. */
export interface RevisionImport {
  file_version_id: UUID;
  /** DXF / DWG only; without it the server answers 422 drawing_needs_reconstruct. */
  dxf_profile?: DxfProfile | null;
}

// ─── CAD AI contracts (server/app/cad/requirements.py, conformance.py, step_source.py, profile_import.py) ──

export type RequirementKind = 'envelope' | 'hole' | 'pocket' | 'band' | 'note';
export type EvidenceSource = 'request' | 'clarification' | 'drawing_dimension' | 'drawing_text' | 'source_geometry';
export type NoteCategory = 'material' | 'tolerance' | 'roughness' | 'thread' | 'treatment' | 'general';

export interface RequirementEvidence {
  source: EvidenceSource;
  /** request / clarification: a literal quote; drawing_*: the evidence entity id; source_geometry: a feature id. */
  ref: string;
  value?: string | null;
}

export type RequirementContour =
  | { kind: 'rect'; center: Vec2; size: Vec2; corner_radius?: number }
  | { kind: 'circle'; center: Vec2; diameter: number }
  | { kind: 'polygon'; points: Vec2[] };

interface RequirementBase {
  /** "R1", "R2", … */
  id: string;
  label: string;
  evidence?: RequirementEvidence[];
  /** Fields computed from cited numbers. */
  derived?: string[];
  derivation?: string | null;
  /** Fields no source states (a critical one means the user is asked first). */
  assumed?: string[];
}

export interface EnvelopeRequirement extends RequirementBase {
  kind: 'envelope';
  size: [number | null, number | null, number | null];
  min?: Vec3 | null;
}

export interface HoleRequirement extends RequirementBase {
  kind: 'hole';
  diameter: number;
  direction?: Vec3;
  entries: Vec3[];
  through: boolean;
  depth?: number | null;
  counterbore?: { diameter: number; depth: number } | null;
  countersink?: { diameter: number; angle?: number } | null;
  thread?: string | null;
}

export interface PocketRequirement extends RequirementBase {
  kind: 'pocket';
  contour: RequirementContour;
  entry_z: number;
  through: boolean;
  depth?: number | null;
}

export interface BandRequirement extends RequirementBase {
  kind: 'band';
  z0: number;
  z1: number;
  outer: RequirementContour;
  voids?: RequirementContour[];
}

export interface NoteRequirement extends RequirementBase {
  kind: 'note';
  category: NoteCategory;
  text: string;
}

export type Requirement = EnvelopeRequirement | HoleRequirement | PocketRequirement | BandRequirement | NoteRequirement;

/** Geometry verdict of the independent checker (never the model's own report). */
/** skipped_turbo (W7): a Turbo revision whose requirement check was skipped — never a verdict. */
export type CheckStatus = 'passed' | 'failed' | 'not_evaluated' | 'skipped_turbo';

export interface CheckItem {
  requirement_id: string | null;
  kind: string;
  instance?: number | null;
  status: CheckStatus | string;
  expected?: Record<string, unknown>;
  measured?: Record<string, unknown>;
  tolerance?: Record<string, number>;
  message: string;
  failures?: string[];
}

export interface CoverageEntry {
  requirement_id: string;
  category: string;
  text: string;
  /** Why it is uncovered / unknown. */
  reason?: string;
  /** Where it is shown (covered). */
  where?: string;
}

export interface NotesCoverage {
  covered: CoverageEntry[];
  uncovered: CoverageEntry[];
  unknown: CoverageEntry[];
}

export interface UnrequestedFeature {
  kind: 'hole' | string;
  diameter: number;
  axis: number[];
  start: number[];
  end: number[];
  length: number;
  message: string;
}

/** cad/conformance.py CheckReport (schema partroute.check/1). */
export interface CheckReport {
  schema: string;
  validator_version: string;
  step_sha256: string;
  requirements_sha256: string;
  mode: 'create' | 'modify' | 'reconstruct' | string;
  tolerance?: Record<string, number>;
  status: CheckStatus;
  solid?: { solids: number; valid: boolean | null; volume_mm3?: number | null; bbox_min?: number[] | null; bbox_max?: number[] | null };
  items: CheckItem[];
  unrequested: UnrequestedFeature[];
  notes: NotesCoverage;
  error?: string;
}

export type StepAuditCode = 'accepted' | 'step_assembly' | 'step_multibody' | 'step_no_solid' | 'step_invalid' | 'step_unreadable';

/** cad/step_source.py StepAudit (full on an import, abbreviated in a refusal's details.audit). */
export interface StepAudit {
  audit_schema?: string;
  sha256: string;
  size_bytes: number;
  code: StepAuditCode;
  message: string;
  step_schema?: string | null;
  application_protocol?: string | null;
  originating_system?: string | null;
  length_unit?: string | null;
  product_names?: string[];
  products?: number | null;
  assemblies?: number | null;
  parts?: number | null;
  instances?: number | null;
  solids?: number;
  shells?: number;
  free_shells?: number;
  free_faces?: number;
  valid_solids?: number;
  volume_mm3?: number | null;
  bbox?: BBox | null;
  /** Issue objects (full audit) or codes (refusal brief). */
  warnings?: (BuildIssue | string)[];
}

export interface ProfileProblem {
  code: string;
  message: string;
  ids?: string[];
}

/** cad/profile_import.py ProfileReport. */
export interface ProfileReport {
  schema_version?: string;
  importer_version?: string;
  status: 'accepted' | 'refused';
  refusal?: ProfileProblem | null;
  problems?: ProfileProblem[];
  source_sha256?: string;
  layer: string;
  units?: { insunits?: number | null; declared?: string | null; used?: 'mm' | null; source?: 'header' | 'declared' | null };
  scale?: number | null;
  thickness_mm?: number | null;
  base_z?: number | null;
  used?: { id: string; type: string; role?: string | null }[];
  rejected?: { id: string; type: string; reason?: string | null }[];
  ignored?: { id: string; type: string; reason?: string | null }[];
  expected?: { area_mm2: number; volume_mm3: number; bbox_min: number[]; bbox_max: number[]; inner_count: number } | null;
  warnings?: BuildIssue[];
}

/** services/drawing_input.py provenance (revision.checks.source of a drawing import / reconstruction). */
export interface DrawingSource {
  schema?: string;
  format: 'dxf' | 'dwg';
  name: string;
  file_version_id: UUID | null;
  drawing_id: UUID | null;
  source_sha256: string;
  source_bytes?: number;
  dxf_sha256: string;
  dxf_role: 'original' | 'derived';
  /** DWG read provenance (source version, version_status provisional / untested, converter, losses, warnings). */
  dwg?: {
    source?: { version?: string | null; sha256?: string };
    version_status?: 'provisional' | 'untested' | string;
    converter?: { name?: string; version?: string | null };
    losses?: unknown[];
    warnings?: unknown[];
  } | null;
}

/** model_revisions.checks (server reports, each optional). */
export interface RevisionChecks {
  conformance?: CheckReport;
  step_audit?: StepAudit;
  profile?: ProfileReport;
  profile_check?: { schema?: string; status: 'passed' | 'failed'; items: { name: string; expected: unknown; measured: unknown; tolerance: number; status: 'passed' | 'failed' }[] };
  source?: DrawingSource;
  evidence?: { frame_id?: string; frame?: { number?: string | null; name?: string | null; scale?: string | null } | null; scale_classification?: { conflicts?: unknown[] } } | null;
  [k: string]: unknown;
}

/**
 * services/ai_modeling.py check_summary: the public summary of a requirement list and its check report
 * (node outputs `requirements_check`, assistant `result.check`, failure details.check).
 */
export interface CheckSummary {
  status: CheckStatus | string;
  requirements?: { total: number; geometric: number; by_kind: Record<string, number> };
  /** Why nothing was evaluated (no report). */
  reason?: string;
  /** Item counts by status. */
  items?: Record<string, number>;
  unrequested?: number;
  problems?: string[];
  /** "R3: text" lines. */
  notes?: { covered: string[]; uncovered: string[]; unknown: string[] };
  step_sha256?: string;
  requirements_sha256?: string;
  validator_version?: string;
  mode?: string;
  error?: string;
}

/** services/ai_reconstruct.py source_brief (node outputs `reconstruct`, assistant `result.source`). */
export interface ReconstructSource {
  format: 'dxf' | 'dwg';
  name: string;
  file_version_id: UUID | null;
  drawing_id: UUID | null;
  source_sha256: string;
  dxf_sha256: string;
  dxf_role: 'original' | 'derived';
  dwg?: { version?: string | null; version_status?: string | null; converter?: { name?: string; version?: string | null } | null; losses?: number; derived_dxf_blob?: string | null };
  frame_id?: string | null;
  frame?: { number?: string | null; name?: string | null; scale?: string | null; paper?: string | null; orientation?: string | null } | null;
  /** Conflicting dimensions logged by the scale classification. */
  conflicts?: number;
}

export interface FieldChange {
  path: string;
  before: unknown;
  after: unknown;
}

export interface OpChange {
  op_id: string;
  change: 'added' | 'removed' | 'modified';
  type: string;
  label: string | null;
  fields: FieldChange[];
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
}

export interface MetricDelta {
  before: number | null;
  after: number | null;
  delta: number | null;
  pct: number | null;
}

/** GET /revisions/{id}/diff?against= (defaults to the parent revision). */
export interface RevisionDiff {
  from: UUID | null;
  to: UUID;
  ops: OpChange[];
  /** volume / area {before, after, delta, pct}, bbox_size per axis, holes summary; {} when not comparable. */
  metrics: { volume_mm3?: MetricDelta; area_mm2?: MetricDelta; bbox_size?: unknown; holes?: unknown; [k: string]: unknown };
}

// ─── manufacturing (implemented: domain/schemas.py, planning.py, costing.py) ─

export interface HoleFeature {
  diameter: number;
  depth: number | null;
  through: boolean;
  count: number;
  counterbore: boolean;
  countersink: boolean;
}

export interface ToleranceNote {
  text: string;
  critical: boolean;
}

/** The only geometry input to planning and costing (design.md §3). */
export interface FeatureSummary {
  source: 'model' | 'drawing_intake';
  estimated: boolean;
  material: string;
  bbox_mm: Vec3;
  volume_mm3: number | null;
  area_mm2: number | null;
  holes: HoleFeature[];
  fillets: number;
  chamfers: number;
  setup_directions: number;
  unsupported: string[];
  tolerances: ToleranceNote[];
}

/** Citation of one constant used by a formula. */
export interface SourceRef {
  id: string;
  version: string;
  value: string;
  unit: string;
  source: string;
}

/** Bilingual server-side message (rules, gaps, staleness reasons). */
export interface Bilingual {
  message_en: string;
  message_zh: string;
}

export interface PlanGap extends Bilingual {
  code: string;
  severity: 'blocking' | 'warning';
  origin: 'summary' | 'rules';
  rule_id: string | null;
  subject: string | null;
}

export interface PlanAssumption extends Bilingual {
  code: string;
  rule_id: string | null;
}

export type SurfaceTreatment = 'anodize' | 'black_oxide' | 'passivate';

export interface PlanParams {
  allowance_mm?: number;
  stock_increment_mm?: number;
  through_hole_depth_rule?: 'min_bbox' | 'max_bbox';
  surface_treatment?: SurfaceTreatment | null;
  block_on_unsupported?: boolean;
}

export interface PlanOperation {
  op_id: string;
  name_key: string;
  label_zh: string;
  label_en: string;
  machine_id: string | null;
  subcontract_id: string | null;
  setup_min: number;
  cycle_min: number;
  lead_days: number;
  formula: string;
  sources: SourceRef[];
  inputs: Record<string, number | string | boolean | null>;
  estimated: boolean;
}

/** domain/planning.py ProcessPlan (stored as process_plans.plan). */
export interface PlanBody {
  rules_version: string;
  inputs_hash: string;
  table_versions: Record<string, string>;
  params: PlanParams;
  source: 'model' | 'drawing_intake';
  material_input: string;
  material_id: string | null;
  material_name_zh: string | null;
  material_name_en: string | null;
  estimated: boolean;
  estimated_fields: string[];
  measures: Record<string, number | boolean | number[]> | null;
  blank: { dims_mm: Vec3; allowance_mm: number; increment_mm: number; material_id: string | null; mass_kg: number | null; estimated: boolean; formula: string; sources: SourceRef[] } | null;
  operations: PlanOperation[];
  programming_min: number;
  programming_formula: string;
  programming_sources: SourceRef[];
  total_cycle_min: number;
  total_setup_min: number;
  gaps: PlanGap[];
  assumptions: PlanAssumption[];
  priceable: boolean;
}

export interface StaleReason extends Bilingual {
  field: string;
  before?: unknown;
  after?: unknown;
  manufacturing: boolean;
}

export interface Staleness {
  stale: boolean;
  manufacturing: boolean;
  reasons: StaleReason[];
  /** Where today's inputs come from: {model_revision_id, rev_no} or {intake_id}. */
  current: { model_revision_id?: UUID; rev_no?: number; intake_id?: UUID };
}

/** Latest decision on a subject for one stage; `valid` = approved and still current. */
export interface ApprovalState {
  decision: ApprovalDecision;
  valid: boolean;
  comment: string;
  role: Role;
  /** null when an independent review agent decided (full-auto collaboration). */
  user_id: UUID | null;
  actor_kind?: 'human' | 'agent';
  agent_id?: UUID | null;
  at: ISODateTime;
}

/** _plan_view: plan_out + staleness + approval. */
export interface ProcessPlan {
  id: UUID;
  part_id: UUID;
  model_revision_id: UUID | null;
  intake_id: UUID | null;
  inputs_hash: string;
  summary: FeatureSummary;
  params: PlanParams;
  plan: PlanBody;
  rules_version: string;
  node_run_id: UUID | null;
  created_by: UUID;
  created_at: ISODateTime | null;
  staleness: Staleness;
  approval: ApprovalState | null;
}

/** POST /parts/{id}/process-plans — exactly one of model_revision_id / intake_id. */
export interface ProcessPlanCreate {
  model_revision_id?: UUID | null;
  intake_id?: UUID | null;
  params?: PlanParams;
}

export interface QuoteParams {
  quantities: number[];
  margin_pct?: number;
  tax_pct?: number;
  currency?: 'CNY';
  /** Defaults to the server's business date. */
  quote_date?: string;
  validity_days?: number;
}

export interface CostLine {
  key: 'material' | 'machining' | 'setup' | 'subcontract' | 'loss';
  label_zh: string;
  label_en: string;
  /** Money is integer fen. */
  fen: number;
  formula: string;
  sources: SourceRef[];
  estimated: boolean;
}

export interface QuoteTier {
  qty: number;
  lines: CostLine[];
  unit_cost_fen: number;
  profit_fen: number;
  unit_ex_tax_fen: number;
  tax_fen: number;
  unit_inc_tax_fen: number;
  order_ex_tax_fen: number;
  order_tax_fen: number;
  order_inc_tax_fen: number;
  lead_days: number;
  lead_formula: string;
}

export interface QuoteResult {
  priceable: boolean;
  refusal_reasons: PlanGap[];
  rules_version: string;
  inputs_hash: string;
  plan_inputs_hash: string;
  table_versions: Record<string, string>;
  currency: 'CNY';
  quote_date: string;
  valid_until: string;
  material_price_valid_until: string;
  estimated: boolean;
  estimated_fields: string[];
  op_costs: { op_id: string; label_zh: string; label_en: string; machine_id: string; rate_fen_per_h: number; cycle_min: number; cycle_fen: number; setup_min: number; setup_fen: number; formula: string; estimated: boolean }[];
  pricing_basis?: 'synthetic_demo_rates';
  commercial_status?: 'draft';
  programming_fen: number;
  batch_fixed_fen: number;
  tiers: QuoteTier[];
  alerts: ({ code: string; level: 'info' | 'warning' | 'blocking' } & Bilingual)[];
}

/** _quote_view: quote_out + staleness + approval. */
export interface Quote {
  id: UUID;
  part_id: UUID;
  process_plan_id: UUID;
  inputs_hash: string;
  params: QuoteParams;
  result: QuoteResult;
  currency: string;
  node_run_id: UUID | null;
  created_by: UUID;
  created_at: ISODateTime | null;
  staleness: Staleness;
  approval: ApprovalState | null;
}

export interface QuoteCreate {
  params: QuoteParams;
}

/** Drawing-only path: customer drawing information organised for planning (always an estimate). */
export interface Intake {
  id: UUID;
  part_id: UUID;
  file_version_id: UUID | null;
  summary: FeatureSummary;
  extraction: Record<string, unknown> | null;
  method: 'ai' | 'manual';
  created_at: ISODateTime | null;
  approval: ApprovalState | null;
}

/** POST /parts/{id}/intakes (manual, no AI). */
export interface ManualIntakeCreate {
  file_version_id?: UUID | null;
  material?: string | null;
  bbox_mm: Vec3;
  volume_mm3?: number | null;
  holes?: HoleFeature[];
  fillets?: number;
  chamfers?: number;
  setup_directions?: number;
  tolerances?: ToleranceNote[];
  unsupported?: string[];
  notes?: string;
}

/** POST /approvals body. `expected_hash` guards against reviewing a changed subject. */
export interface ApprovalCreate {
  subject_type: ApprovalSubjectType;
  subject_id: UUID;
  stage: ApprovalStage;
  decision: ApprovalDecision;
  comment?: string;
  expected_hash?: string | null;
}

export interface ApprovalCreated {
  id: UUID;
  decision: ApprovalDecision;
  stage: ApprovalStage;
  subject_hash: string;
  created_at: ISODateTime;
}

/** GET /parts/{id}/approvals item. */
export interface Approval {
  id: UUID;
  subject_type: ApprovalSubjectType;
  subject_id: UUID;
  stage: ApprovalStage;
  decision: ApprovalDecision;
  comment: string;
  role: Role;
  /** null when an independent review agent decided (full-auto collaboration). */
  user_id: UUID | null;
  actor_kind?: 'human' | 'agent';
  agent_id?: UUID | null;
  subject_hash: string;
  created_at: ISODateTime;
  valid: boolean;
}

export type PartBlocker =
  | 'no_design'
  | 'design_not_confirmed'
  | 'no_plan'
  | 'plan_stale'
  | 'plan_not_confirmed'
  | 'no_quote'
  | 'quote_stale'
  | 'quote_not_confirmed'
  | 'quote_not_from_latest_plan';

/** GET /parts/{id}/status (services/status.py part_status). */
export interface PartPipelineStatus {
  part_id: UUID;
  design: { kind: 'model_revision'; id: UUID; rev_no: number; mesh_only: boolean; approval: ApprovalState | null } | { kind: 'intake'; id: UUID; approval: ApprovalState | null } | null;
  drawing: { id: UUID; model_revision_id: UUID | null; stale: boolean } | null;
  plan: (Staleness & { id: UUID; approval: ApprovalState | null; priceable: boolean; estimated: boolean }) | null;
  quote: (Staleness & { id: UUID; process_plan_id: UUID; approval: ApprovalState | null; estimated: boolean }) | null;
  /** True only when every confirmation holds on current data. */
  formal: boolean;
  blockers: PartBlocker[];
}

// ─── drawings (implemented: api/drawings.py, services/drawings.py drawing_out) ─

/** cad/results.py DrawingConfig. Everything optional; the server fills defaults. */
export interface DrawingConfig {
  views?: ('front' | 'top' | 'right' | 'iso')[];
  /** "auto" or a model-to-paper factor (2 = 2:1, 0.5 = 1:2). */
  scale?: 'auto' | number;
  projection?: 'third' | 'first';
  hidden_lines?: boolean;
  dimensions?: boolean;
  title?: Partial<Record<'part_number' | 'part_name' | 'revision' | 'material' | 'author' | 'date' | 'project', string>>;
  units?: 'mm';
  /** Section page: auto (when blind features need it), none, or an explicit cutting plane (model mm). */
  section?: DrawingSection;
}

/** cad/drawing.py PackageConfig.section. */
export type DrawingSection = 'auto' | 'none' | { plane: 'XZ' | 'YZ'; offset: number };

/** A placed view; bbox in sheet mm [x_min, y_min, x_max, y_max], origin top-left. */
export interface DrawingView {
  name: 'front' | 'top' | 'right' | 'iso';
  scale: number;
  bbox: number[];
}

export type DrawingFileFormat = 'svg' | 'dxf' | 'dwg';

/** One file of one package page (manifest entry; hash-verified on export). */
export interface DrawingPackageFile {
  format: DrawingFileFormat | string;
  sha256: Sha256;
  bytes: number;
  /** DXF $ACADVER / DWG target, e.g. "AC1015" (= R2000). */
  version?: string;
  /** Path inside package.zip, e.g. "pages/p1_overview.dwg". */
  path: string;
}

export interface DrawingPage {
  index: number;
  role: 'overview' | 'section' | string;
  title: string;
  files: DrawingPackageFile[];
  scale?: number;
  views?: { name: string; kind: string; label: string; scale: number; bbox: number[] }[];
  dimensions?: unknown[];
  callouts?: string[];
  hatches?: number;
  section?: { letter: string; plane: 'XZ' | 'YZ'; offset: number; features?: string[] } | null;
}

/** unverified (W7 Turbo): real DWG files written without the semantic readback. */
export type DwgCheckStatus = 'passed' | 'failed' | 'unavailable' | 'unverified';

/** drawing_out.checks (services/drawings.py _checks_out). */
export interface DrawingChecks {
  annotation: { status: 'passed' | 'failed' | 'skipped_turbo' | null; validator_version?: string | null; failed: number; warnings: number; report_sha256?: Sha256 | null };
  /** passed: verified R2000 per page; unavailable: runtime missing (reason); failed: conversion / readback failed (code, message, page). */
  dwg: { status?: DwgCheckStatus | null; reason?: string; code?: string; message?: string; page?: number; target?: string; verified?: boolean; pages?: number; report_sha256?: Sha256 };
}

export interface DrawingPackageManifest {
  schema: string;
  source?: { model_revision_id?: UUID | null; rev_no?: number; step_sha256?: string | null; build_key?: string | null; requirements_sha256?: string | null };
  pages?: { index: number; role: string; title?: string; files: DrawingPackageFile[] }[];
  checks?: Record<string, unknown> | null;
  coverage?: NotesCoverage | null;
  [k: string]: unknown;
}

export interface Drawing {
  id: UUID;
  part_id: UUID;
  model_revision_id: UUID | null;
  rev_no: number | null;
  kind: 'generated' | 'uploaded';
  file_version_id: UUID | null;
  /** Page-1 alias. Fetch with api/blobs.ts (authenticated). */
  svg_sha: Sha256 | null;
  dxf_sha: Sha256 | null;
  config: DrawingConfig;
  views: DrawingView[];
  /** Package pages; [] for drawings from before the package (use the page-1 aliases). */
  pages?: DrawingPage[];
  page_count?: number;
  manifest?: DrawingPackageManifest | null;
  manifest_sha256?: Sha256 | null;
  checks?: DrawingChecks | null;
  dwg_status?: DwgCheckStatus | null;
  /** Manufacturing requirements covered / not shown / unknown on the drawing. */
  coverage?: NotesCoverage | null;
  /** W0 verification block (+ per-check statuses, upstream_unverified). */
  verification?: Verification | null;
  node_run_id: UUID | null;
  created_at: ISODateTime | null;
}

/** services/drawings.py drawing_brief (+ page-1 aliases): workflow `drawing` output and assistant `result.drawings`. */
export interface DrawingBrief {
  type?: 'drawing';
  drawing_id: UUID;
  pages: number;
  manifest_sha256: Sha256 | null;
  annotation_status: 'passed' | 'failed' | 'skipped_turbo' | null;
  dwg_status: DwgCheckStatus | null;
  verification?: Verification | null;
  svg_sha?: Sha256 | null;
  dxf_sha?: Sha256 | null;
}

/** GET /runs/{id}/ai-calls row (api/ai.py ai_call_out): metadata only. */
export interface AiCallRecord {
  id: UUID;
  node_run_id: UUID | null;
  node_id: string | null;
  agent_execution_id: UUID | null;
  purpose: string | null;
  provider: string | null;
  model_id: string | null;
  status: string;
  error_code: string | null;
  /** J: logical work unit (e.g. definition:D-12), attempt from 1, failure class of a failed request. */
  unit?: string | null;
  attempt?: number | null;
  failure_class?: string | null;
  latency_ms: number | null;
  tokens_in: number | null;
  tokens_out: number | null;
  provider_request_id: string | null;
  simulated: boolean;
  created_at: ISODateTime | null;
}

// ─── AI modeling (implemented: api/ai.py) ─────────────────────────────────

// ─── workflows (implemented: api/workflows.py, workflow/*.py) ─────────────

/** Workflow catalog port kinds, including engineering and manufacturing batch results. */
export type PortType = 'text' | 'file' | 'model' | 'drawing' | 'spec' | 'plan' | 'quote' | 'engineering_spec' | 'assembly' | 'drawing_package' | 'delivery' | 'manufacturing_plan' | 'manufacturing_quote' | 'any';

export interface WorkflowNode {
  id: string;
  type: string;
  position: { x: number; y: number };
  params: Record<string, unknown>;
  label?: string | null;
}

export interface WorkflowEdge {
  id: string;
  source: string;
  source_port: string;
  target: string;
  target_port: string;
}

export interface WorkflowGroup {
  id: string;
  label: string;
  node_ids: string[];
  color?: string | null;
}

export interface WorkflowGraph {
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
  groups: WorkflowGroup[];
}

/** Graph validation error (workflow/graph.py _err). */
export interface GraphError {
  code: string;
  message: string;
  node_id?: string;
  edge_id?: string;
  param?: string;
  loc?: string;
  [k: string]: string | undefined;
}

export interface NodePort {
  name: string;
  type: PortType;
  required: boolean;
  multiple: boolean;
  /** W8: the node still runs when this input's upstream failed or was skipped (e.g. a delivery lists what is missing). */
  after_failure?: boolean;
}

/** GET /workflow-node-types item (node_catalog). */
export interface WorkflowNodeType {
  /** False for frozen nodes; their schema remains available to render history. */
  available?: boolean;
  type: string;
  category: string;
  inputs: NodePort[];
  outputs: NodePort[];
  /** JSON schema of the node parameters. */
  params_schema: Record<string, unknown>;
  max_attempts: number;
  uses_ai: boolean;
  doc: string;
}

/** Text the server sends in both languages (templates). Plain strings are tolerated. */
export type LocalizedText = string | { en: string; zh: string };

/** GET /workflow-templates item (workflow/templates.py template_list: name / description are {en, zh}). */
export interface WorkflowTemplate {
  key: 'design_to_quote' | 'drawing_to_quote' | 'design_change' | (string & {});
  name: LocalizedText;
  description: LocalizedText;
  graph: WorkflowGraph;
}

// ─── templates: draft policy, published versions (P3, api/workflows.py) ───

export type TemplateMode = 'standard' | 'exploratory';

/** Input quality rules of a template (closed structure, server defaults below). */
export interface TemplateInputRules {
  allow_estimated: boolean;
  allow_simulated: boolean;
  allow_mesh_reference: boolean;
  allow_failed_source: boolean;
  /** W0 §2.2: Turbo output / AI values nobody confirmed are no standard input unless allowed. */
  allow_unverified: boolean;
  require_approved_stage: ApprovalStage | null;
}

export interface TemplatePolicy {
  mode: TemplateMode;
  input_rules: TemplateInputRules;
}

export const DEFAULT_TEMPLATE_POLICY: TemplatePolicy = {
  mode: 'standard',
  input_rules: {
    allow_estimated: false,
    allow_simulated: true,
    allow_mesh_reference: false,
    allow_failed_source: false,
    allow_unverified: false,
    require_approved_stage: null,
  },
};

export interface UserRef {
  id: UUID;
  name: string;
}

export const TEMPLATE_VERSION_STATUSES = ['published', 'deprecated'] as const;
export type TemplateVersionStatus = (typeof TEMPLATE_VERSION_STATUSES)[number];

/** One run-time input of a published version (input_schema.inputs[]). */
export interface TemplateInputSpec {
  node_id: string;
  node_type: string;
  port_type: PortType;
  /** part_model for model_input, document for file_input, null for text. */
  asset_type: string | null;
  required: boolean;
  default_params: Record<string, unknown>;
  require_approved_stage: ApprovalStage | null;
}

export interface TemplateInputSchema {
  inputs: TemplateInputSpec[];
  /** node id → parameter names a run may override (human_review `stage` never). */
  overridable: Record<string, string[]>;
}

export interface TemplateOutputSpec {
  node_id: string;
  port: string;
  asset_type: string;
  /** export writes a new document asset; other producers append a version to the part's asset. */
  target: 'append_to_part_asset' | 'new_asset' | (string & {});
}

export interface TemplateOutputSchema {
  outputs: TemplateOutputSpec[];
}

/** version_out (services/templates.py; immutable published version). */
export interface ImportSource { v: number; project_id: UUID; workflow_id: UUID; template_version_id: UUID; version_no: number; hash: string; name: string; imported_at: ISODateTime; imported_by: UUID; reference_map: { kind: string; source_id: UUID; target_id: UUID }[] }

export interface TemplateVersion {
  import_source?: ImportSource | null;
  can_open_import_source?: boolean;
  id: UUID;
  workflow_id: UUID;
  project_id: UUID;
  version_no: number;
  name: string;
  description: string;
  status: TemplateVersionStatus;
  hash: string;
  notes: string;
  published_by: UserRef | null;
  published_at: ISODateTime | null;
  deprecated_at: ISODateTime | null;
  deprecated_by: UserRef | null;
  deprecation_reason: string | null;
  draft_version: number;
  source_public_version: { id: UUID; public_template_id: UUID; version_no: number; name: string | null } | null;
  run_count: number;
}

/** GET /template-versions/{vid} and the publish response (version_detail). */
export interface TemplateVersionDetail extends TemplateVersion {
  graph: WorkflowGraph;
  input_schema: TemplateInputSchema;
  output_schema: TemplateOutputSchema;
  policy: TemplatePolicy;
}

/** _wf_out.library: the public template this project template was imported from. */
export interface WorkflowLibraryLink {
  public_template_id: UUID;
  name: string;
  adopted_version_no: number | null;
  latest_version_no: number | null;
  upgrade_available: boolean;
}

/** _wf_out (the workflow row is the template draft). */
export interface Workflow {
  kind?: 'tool' | 'collaboration';
  import_source?: ImportSource | null;
  can_open_import_source?: boolean;
  id: UUID;
  project_id: UUID;
  name: string;
  description: string;
  graph: WorkflowGraph;
  /** Draft edit counter. */
  version: number;
  template_key: string | null;
  /** Part workflow (R1.2): the part's own working copy; null = a project template. */
  part_id?: UUID | null;
  created_at: ISODateTime;
  updated_at: ISODateTime;
  archived_at: ISODateTime | null;
  /** Empty = runnable. Drafts may be saved with errors. */
  validation: GraphError[];
  // P3 fields (optional so older servers still render).
  /** What blocks publishing: validation without input values, plus approval-gate errors (gate_missing). */
  publish_errors?: GraphError[];
  source_public_template_id?: UUID | null;
  policy?: TemplatePolicy;
  latest_published?: TemplateVersion | null;
  published_count?: number;
  /** The draft differs from the latest published version. */
  draft_dirty?: boolean;
  /** May create / edit / publish / deprecate (owner or template_manager). */
  can_manage?: boolean;
  library?: WorkflowLibraryLink | null;
}

export interface WorkflowCreate {
  name: string;
  description?: string;
  graph?: WorkflowGraph;
  template_key?: string;
  /** Create the part's own workflow instead of a project template (needs the `workflow` role only). */
  part_id?: UUID;
}

export interface WorkflowUpdate {
  name?: string;
  description?: string;
  graph?: WorkflowGraph;
  policy?: TemplatePolicy;
  /** Optimistic concurrency: 409 version_conflict when someone saved in between. */
  expected_version?: number;
}

export interface WorkflowValidation {
  errors: GraphError[];
  publish_errors?: GraphError[];
}

/** POST /workflows/{id}/publish body. */
export interface PublishRequest {
  /** Draft version the user reviewed; 409 version_conflict when it moved on. */
  expected_version?: number;
  notes: string;
}

/** POST /workflows/{id}/runs body. */
export interface RunCreate {
  part_id?: UUID | null;
  title?: string;
  /** Per-node parameter overrides for this run only, e.g. {request: {text: "…"}}. */
  params?: Record<string, Record<string, unknown>>;
  /** Pinned published version; omitted = latest published. */
  template_version_id?: UUID | null;
  /** Draft test run (exploration only, template managers). */
  draft?: boolean;
  task_id?: UUID | null;
  purpose?: string;
  /** W7 / S5: the request's explicit choice; omitted = inherited from the one spec version the graph pins, else the
   * project default, else standard (the run records `policy_source`). */
  execution_policy?: ExecutionPolicy;
}

/** POST /runs/{id}/rerun body. */
export interface RerunRequest {
  template_version_id?: UUID | null;
  draft?: boolean;
  params?: Record<string, Record<string, unknown>>;
  title?: string;
  purpose?: string;
  /** S5: omitted = inherited from the original run (recorded as policy_source inherited). */
  execution_policy?: ExecutionPolicy;
}

export interface RunListQuery {
  part_id?: UUID;
  /** One status or a comma-separated list. */
  status?: string;
  task_id?: UUID;
  /** Only runs without a task. */
  unlinked?: boolean;
  workflow_id?: UUID;
  template_version_id?: UUID;
  created_by?: UUID;
  origin?: RunOrigin;
  since?: ISODateTime;
  limit?: number;
  offset?: number;
}

/** run_out.template_version / list rows. */
export interface RunTemplateVersionRef {
  id: UUID;
  version_no: number;
  status: TemplateVersionStatus;
  workflow_id: UUID;
  name: string;
}

export interface RunTaskRef {
  id: UUID;
  key: string;
  title: string;
  status: string;
}

/** 422 input_quality details.issues[]. */
export interface InputQualityIssue {
  node_id: string;
  version_label?: string;
  code: string;
  message: string;
}

/** _run_summary (list items; also the head of run details). */
export interface RunSummary {
  id: UUID;
  project_id: UUID;
  part_id: UUID | null;
  workflow_id: UUID | null;
  workflow_version: number | null;
  origin: RunOrigin;
  title: string;
  status: RunStatus;
  /** Frozen at start (W7); older servers omit it (= standard). */
  execution_policy?: ExecutionPolicy;
  /** S5: where the frozen policy came from (null for runs from before S5) and the version it continued from. */
  policy_source?: PolicySource | null;
  policy_source_version?: SourceVersion | null;
  cancel_requested: boolean;
  /** Generation reliability J: the run's frozen budget (absent on older servers / runs). */
  budget?: { max_calls?: number | null; max_seconds?: number | null } | null;
  /** Node count per node status. */
  node_counts: Partial<Record<NodeStatus, number>>;
  created_at: ISODateTime;
  started_at: ISODateTime | null;
  finished_at: ISODateTime | null;
  updated_at: ISODateTime;
  // Task context and template pinning (optional: older servers omit them).
  task_id?: UUID | null;
  task_baseline_snapshot_id?: UUID | null;
  rerun_of?: UUID | null;
  purpose?: string;
  created_by?: UUID;
  template_version_id?: UUID | null;
  template_version?: RunTemplateVersionRef | null;
  from_draft?: boolean;
  task?: RunTaskRef | null;
}

export interface NodeStep {
  kind: string;
  ok: boolean;
  /** Set by handler steps (engine NodeContext.step) and decisions. AI agent steps use started_at instead. */
  at?: ISODateTime;
  /** AI agent steps (ai/trace.py AgentStep). */
  started_at?: ISODateTime;
  duration_ms?: number;
  attempt?: number;
  detail?: unknown;
}

export interface NodeRun {
  id: UUID;
  node_id: string;
  node_type: string;
  status: NodeStatus;
  attempt: number;
  max_attempts: number;
  inputs: Record<string, unknown>;
  outputs: Record<string, unknown>;
  error: { code: string | null; message: string | null; details: Record<string, unknown> } | null;
  steps: NodeStep[];
  /** Generation reliability N: the business checks the node's attempts really ran / skipped (null: no record). */
  checks_ledger?: { invoked: Record<string, number>; skipped: Record<string, number> } | null;
  started_at: ISODateTime | null;
  finished_at: ISODateTime | null;
  updated_at: ISODateTime | null;
  /** Present while status is waiting_human (api/workflows.py run_out). */
  review?: NodeReview;
}

/** A model_3d node asking questions before it can build. */
export interface NodeClarification {
  questions: string[];
  answers: { question: string; answer: string }[];
  /** Critical gaps the server gate found (missing / assumed fields, sheet, units, conflicting dimensions). */
  gaps?: ClarificationGap[];
}

/** One gap behind a clarification question (services/ai_modeling.py, ai_reconstruct.py). */
export interface ClarificationGap {
  requirement_id: string | null;
  kind: string;
  label: string;
  field: string;
  reason: 'missing' | 'assumed' | 'conflict' | 'ambiguous' | 'not_found' | string;
  question?: string;
  /** sheet: number of frames in the drawing. */
  frames?: number;
  /** conflict: the dimension entity, its view and both values. */
  evidence_id?: string;
  view?: string | null;
  displayed?: string | number | null;
  measurement?: number | null;
  dimlfac?: number | null;
  native_value?: number | null;
  view_ratio?: number | null;
  expected_by_view?: number | null;
}

/** A drawing_intake node handing over to a person (POST /parts/{id}/intakes, then decide with intake_id). */
export interface NodeManualEntry {
  /** "manual_entry", a provider error code, or "incomplete:<fields>". */
  reason: string;
  /** IntakeResult from the AI when it extracted something (ai/intake_agent.py), else {}. */
  prefill: Record<string, unknown>;
  file_version_id: UUID;
}

/**
 * What a waiting step needs:
 * - approval: human_review — approve / reject the subject (records an approval).
 * - clarification: model_3d asked questions — answer them all, or reject.
 * - manual_entry: drawing_intake — create an intake for the run's part, then approve with intake_id.
 */
export interface NodeReview {
  kind: 'approval' | 'clarification' | 'manual_entry';
  can_decide: boolean;
  stage?: ApprovalStage;
  instructions?: string;
  subject?: Record<string, unknown> | null;
  clarification?: NodeClarification | null;
  manual_entry?: NodeManualEntry | null;
  /** approval: the pending approval request and the hash of what it approves (sent back as expected_hash). */
  request_id?: string | null;
  target_hash?: string | null;
}

/** Frozen input version of a run (task_svc.bindings_out); port_key is the input node id. */
export interface RunInputBinding {
  port_key: string;
  asset_version_id: UUID;
  content_hash: string;
  snapshot: {
    asset_type?: string;
    availability?: string;
    publish_state?: string;
    labels?: string[];
    version_no?: number;
    engineering?: { stage: string; approved: boolean } | null;
    quality?: { labels?: string[]; approved?: boolean | null; availability?: string };
    [k: string]: unknown;
  };
  /** Catalogue view of the version (null when it no longer exists). */
  version?: RunAssetVersion | null;
}

/**
 * Catalogue version as embedded in run responses (services/tasks.version_brief →
 * assets.version_out, or {id, missing: true}). Kept structural so this file does
 * not depend on api/pm.ts.
 */
export interface RunAssetVersion {
  id: UUID;
  missing?: boolean;
  asset_id?: UUID;
  asset_type?: string;
  asset_name?: string;
  label?: string;
  version_no?: number;
  content_hash?: string;
  availability?: string;
  labels?: string[];
  [k: string]: unknown;
}

/** run_out.outputs[] (run_outputs rows). */
export interface RunOutputRef {
  node_id: string;
  output_key: string;
  attempt: number;
  asset_version_id: UUID;
  version: RunAssetVersion | null;
}

/** run_out: summary + the frozen graph snapshot + node runs. Also the SSE "run" event payload. */
export interface WorkflowRun extends RunSummary {
  graph: WorkflowGraph;
  nodes: NodeRun[];
  error: Record<string, unknown> | null;
  input_bindings?: RunInputBinding[];
  outputs?: RunOutputRef[];
  rerun_of_run?: { id: UUID; title: string } | null;
  reruns?: { id: UUID; title: string; status: RunStatus }[];
  /** Turbo: every assumption the run rests on (planner + nodes); [] for standard runs. */
  assumptions?: (Assumption & { owner?: { type: string; id: UUID }; node_id?: string })[];
  /** Generation reliability J: logical work units apart from real model requests (retries included). */
  calls?: RunCallSummary;
  /** Generation reliability J: the run's frozen budget and what it used. */
  budget?: RunBudget;
}

/** api/workflows.py run_calls (app/ai/retry.py summarize). */
export interface RunCallSummary {
  units: number;
  requests: number;
  retries: number;
  failed: number;
  /** failure class (connection, rate_limited, overloaded, stream_broken, auth, quota, unsupported, budget …) → failed requests */
  by_class: Record<string, number>;
}

/** api/workflows.py run_budget_out: null caps = no run-wide cap (each step keeps node_max_seconds). */
export interface RunBudget {
  max_calls: number | null;
  max_seconds: number | null;
  node_max_seconds: number;
  source: 'default' | 'request' | 'raised' | 'legacy';
  /** seconds of the time budget's clock (active time only); while it runs, the time before ``running_since`` (add the time since). */
  used: { requests: number; seconds: number; running_since?: string | null };
  changes?: { at: string; by: string | null; from: Record<string, number | null>; to: Record<string, number | null> }[];
}

// ─── public template library (api/template_library.py) ────────────────────

export interface PublicTemplateVersion {
  id: UUID;
  public_template_id?: UUID;
  version_no: number;
  hash: string;
  notes: string;
  status: TemplateVersionStatus;
  published_at: ISODateTime | null;
  published_by?: UserRef | null;
  deprecated_at?: ISODateTime | null;
  source_template_version_id: UUID | null;
  input_schema?: TemplateInputSchema;
  output_schema?: TemplateOutputSchema;
  policy?: TemplatePolicy;
  graph?: WorkflowGraph;
}

export interface PublicTemplate {
  id: UUID;
  name: string;
  description: string;
  created_by: UserRef | null;
  source_project: { id: UUID; code: string } | null;
  latest_version: PublicTemplateVersion | null;
  version_count: number;
  /** Creator or admin: may add / deprecate versions. */
  can_manage: boolean;
  created_at?: ISODateTime | null;
}

export interface PublicTemplateDetail extends PublicTemplate {
  versions: PublicTemplateVersion[];
}

export interface ShareTemplateRequest {
  template_version_id: UUID;
  /** Defaults to the version's name / description. */
  name?: string;
  description?: string;
}

export interface ImportTemplateRequest {
  public_template_version_id: UUID;
  name?: string;
}

export interface UpgradeTemplateRequest {
  public_template_version_id: UUID;
  expected_version?: number;
}

export interface NodeDecision {
  decision: ApprovalDecision;
  comment?: string;
  expected_hash?: string | null;
  /** Clarification: one answer per question (approved only). */
  answers?: string[];
  /** Manual entry: the intake created for the run's part (approved only). */
  intake_id?: UUID | null;
}

/** One parsed Server-Sent Event. GET /runs/{id}/events sends `event: run` with a full WorkflowRun. */
export interface RunEvent {
  event: string;
  id?: string;
  data: unknown;
}

// ─── model configs (implemented: api/ai.py, services/ai.py config_out) ───

export interface ModelCapabilities {
  text?: boolean;
  vision?: boolean;
  tools?: boolean;
  structured?: boolean;
}

export type ProviderErrorCode =
  | 'missing_key'
  | 'auth_failed'
  | 'model_not_found'
  | 'permission_denied'
  | 'quota_exceeded'
  | 'rate_limited'
  | 'invalid_request'
  | 'unsupported_capability'
  | 'network'
  | 'timeout'
  | 'bad_response'
  | 'unknown'
  // Credential resolution (W1): raised before any provider request.
  | 'master_key_missing'
  | 'decrypt_failed'
  | 'credential_changed'
  | 'config_disabled'
  | 'credential_cleared'
  | 'credential_revoked';

/** Stable AI failure codes (server/app/ai/diagnostics.py CODES). */
export const AI_DIAGNOSTIC_CODES = [
  'missing_key',
  'master_key_missing',
  'decrypt_failed',
  'credential_changed',
  'config_disabled',
  'credential_cleared',
  'credential_revoked',
  'auth_failed',
  'permission_denied',
  'protocol_mismatch',
  'model_not_found',
  'rate_limited',
  'quota_exhausted',
  'network_error',
  'timeout',
  'context_overflow',
  'output_truncated',
  'invalid_plan',
  'cad_unsupported',
  'conversion_failed',
  'budget_exceeded',
  'simulation_forbidden',
  'unknown',
] as const;
export type AiDiagnosticCode = (typeof AI_DIAGNOSTIC_CODES)[number];
export type AiDiagnosticStage = 'resolve' | 'context' | 'model_call' | 'validation' | 'tool' | 'run';

/** What failed, where, with which exact configuration, and how to recover (W1). Never contains a key. */
export interface AiDiagnostic {
  code: AiDiagnosticCode | string;
  stage: AiDiagnosticStage | string;
  purpose: string | null;
  config_id: UUID | null;
  config_version: number | null;
  call_ids: UUID[];
  retryable: boolean;
  /** Action codes, e.g. replace_key, test_config, retry_new_snapshot. */
  recovery: string[];
  /** The raw provider / application code the diagnostic was derived from. */
  provider_code?: string | null;
}

/** Stored result of POST /model-configs/{id}/test (ai/providers/base.py ConnectionTest + `at`). */
export interface ConnectionTest {
  ok: boolean;
  provider?: string;
  model_id?: string;
  latency_ms?: number | null;
  error_code?: ProviderErrorCode | null;
  error_message?: string | null;
  /** Set instead of error_message when the provider could not be built. */
  error?: string | null;
  retryable?: boolean | null;
  detected?: Record<string, boolean | null>;
  details?: Record<string, unknown>;
  simulated?: boolean;
  at?: ISODateTime;
  /** The config version the probe was made for (W1). */
  config_version?: number | null;
  /** Made for another config / credential version, or the server's env key changed since: no longer evidence. */
  stale?: boolean;
  diagnostic?: AiDiagnostic | null;
}

export type CredentialSource = 'env' | 'direct';
/** Current credential of a config: active, cleared (key removed), revoked, or none (never set). */
export type ConfigCredentialStatus = 'active' | 'cleared' | 'revoked' | 'none';

export interface CredentialVersion {
  version: number;
  source: CredentialSource;
  /** replaced: superseded but still serving operations frozen with it; cleared / revoked: no longer usable. */
  status: 'active' | 'replaced' | 'cleared' | 'revoked';
  created_at: ISODateTime | null;
  replaced_at: ISODateTime | null;
  revoked_at: ISODateTime | null;
  cleared_at: ISODateTime | null;
}

/** Latest real (not simulated) model call of a config for one purpose — shown apart from readiness and the probe. */
export interface ModelLastUse {
  call_id: UUID;
  status: 'ok' | 'error' | string;
  error_code: string | null;
  at: ISODateTime | null;
}

export const MODEL_PURPOSES = ['assistant', 'modeling', 'intake', 'planning'] as const;
export type ModelPurpose = (typeof MODEL_PURPOSES)[number];
export interface ModelProbe {
  id: UUID | null;
  status: 'not_tested' | 'queued' | 'running' | 'passed' | 'failed' | 'interrupted' | 'simulated';
  tested_config_version: number | null;
  applied_config_version: number | null;
  started_at: ISODateTime | null;
  finished_at: ISODateTime | null;
  expires_at: ISODateTime | null;
  stale: boolean;
  simulated: boolean;
  detected: Record<'text' | 'structured' | 'vision' | 'tools', boolean | null>;
  checks: Record<string, { status: 'passed' | 'unsupported' | 'failed' | 'unknown'; code: string | null }>;
  error: { code: string; message: string; retryable: boolean } | null;
}

export interface ModelConfig {
  id: UUID;
  label: string;
  provider: Provider;
  model_id: string;
  base_url: string | null;
  /** "env:NAME" for an environment reference (a name, not a key); null for a direct key. */
  secret_ref: string | null;
  credential_source?: CredentialSource;
  credential_status?: ConfigCredentialStatus;
  credential_version?: number | null;
  /** Bumped by every connection-relevant change (model, base URL, capabilities, credential). */
  config_version?: number;
  credential_versions?: CredentialVersion[];
  capabilities: ModelCapabilities;
  is_default: boolean;
  default_for?: ModelPurpose[];
  probe?: ModelProbe;
  ready_for?: Record<ModelPurpose, boolean>;
  enabled: boolean;
  /** Whether the key is usable here: the referenced variable is set, or the stored key's master key is configured (not a connection test). */
  key_present: boolean;
  /** Enabled and key present (no issues). Still not proof that the provider answers. */
  ready: boolean;
  issues: string[];
  issue_codes?: string[];
  last_test: ConnectionTest | null;
  /** purpose → latest real call. */
  last_use?: Record<string, ModelLastUse>;
}

/** Write-only credential input: exactly one field. The key is never returned by the server. */
export type CredentialInput = { env_name: string } | { api_key: string } | { keep: true } | { clear: true };

export interface ModelConfigCreate {
  label: string;
  provider: Provider;
  model_id: string;
  base_url?: string | null;
  /** Environment reference, ^env:[A-Z][A-Z0-9_]+$ (or use credential.env_name). */
  secret_ref?: string;
  credential?: CredentialInput;
  capabilities?: ModelCapabilities;
  is_default?: boolean;
  default_for?: ModelPurpose[];
  enabled?: boolean;
}

export type ModelConfigPatch = Partial<Omit<ModelConfigCreate, 'provider'>> & { expected_config_version?: number };

/** One model an endpoint offers to a key (POST /model-configs/models); `id` goes into model_id. */
export interface ListedModel {
  id: string;
  name?: string | null;
}

/** What to list with: the dialog's provider and base URL, and a key — a new one, or the saved key of `config_id`. */
export interface ModelListRequest {
  provider: Provider;
  base_url?: string | null;
  config_id?: UUID;
  credential?: CredentialInput;
}

/** A listing call only (nothing generated or saved); provider failures are `ok: false` with a stable code. */
export interface ModelListResult {
  ok: boolean;
  models: ListedModel[];
  error_code: ProviderErrorCode | null;
  error_message: string | null;
  retryable: boolean | null;
}

export interface RekeyResult {
  current_key_version: string | null;
  rekeyed: number;
  already_current: number;
  unreadable: { config_id: UUID; version: number; code: string }[];
}

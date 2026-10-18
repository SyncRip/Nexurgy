// v0.4 note: retained only because three/geometry.ts, three/viewer.ts and three/hero.ts
// (the landing hero and the upcoming viewer task) import it. Not used by v0.4 pages.

// Shared domain types. Every derived result (planning run, quote snapshot)
// stores the ids and an inputsHash of what it was computed from, so the UI can
// decide "stale" by recomputing the hash instead of trusting a stored flag.

export type Role = 'design' | 'process' | 'quote' | 'viewer';

/** Geometry maturity. Never collapse these into one "CAD file" notion. */
export type GeometryLevel = 'L1' | 'L2' | 'L3';

export type MaterialId = 'AL6061-T651' | 'AL7075-T651' | 'S45C';

export interface BracketParams {
  length: number;
  baseDepth: number;
  baseThk: number;
  wallHeight: number;
  wallThk: number;
  boreDia: number;
  centerDist: number;
  boreHeight: number;
  pocketLen: number;
  pocketWidth: number;
  pocketDepth: number;
  pocketR: number;
  mountHoleDia: number;
  cboreDia: number;
  cboreDepth: number;
  ribThk: number;
}

export type ParamKey = keyof BracketParams;

/**
 * Per-instance feature edits on top of the template (see domain/engineering.ts).
 * Template instances (H-01…H-04, B-01/B-02, T-01…T-08, P-01) keep their ids when
 * parameters change; through holes added by an operation get H-05, H-06 … and an
 * id is never reused (deleted ones stay in `added` and are listed in `deleted`).
 */
export interface FeatureEdits {
  /** Hole-diameter overrides of template instances (only H-xx counterbored holes). */
  dia: Record<string, number>;
  /** Through holes created by `add_through_hole`, part coordinates on the base top (mm). */
  added: { id: string; x: number; z: number; dia: number }[];
  /** Ids of operation-created features that were deleted. Template features cannot be deleted. */
  deleted: string[];
}

/** Provenance kept on a saved revision: the engineering operations it was made from. */
export interface OpSummary {
  opId: string;
  summary: string;
  source: 'human' | 'request';
  by: Role;
  at: string;
}

export interface ModelRevision {
  id: string;
  partId: string;
  rev: string;
  parentId?: string;
  params: BracketParams;
  material: MaterialId;
  /** Feature-instance edits (seed revisions: none). */
  features: FeatureEdits;
  /** Operation log summary written when the revision was saved from a working copy. */
  ops?: OpSummary[];
  /** How the revision was produced. 'nl_agent' only when the Agent service really ran. */
  source: 'template' | 'nl_agent' | 'step_import' | 'seed';
  geometryLevel: GeometryLevel;
  /** For L2/L3: where the kernel output came from. Seeds are labelled as such. */
  kernelNote?: string;
  status: 'draft' | 'confirmed' | 'superseded';
  note: string;
  createdAt: string;
  createdBy: Role;
  confirmedAt?: string;
  confirmedBy?: Role;
}

export type TolSpec =
  | { kind: 'none' }
  | { kind: 'sym'; value: number }
  | { kind: 'dev'; upper: number; lower: number }
  | { kind: 'fit'; code: string };

export type AnnotationKind =
  | 'dim-linear'
  | 'dim-diameter'
  | 'dim-radius'
  | 'finish'
  | 'gdt'
  | 'note';

/** A drawing annotation. Dimension values stay associative to model params unless overridden. */
export interface Annotation {
  id: string; // A1, A2 ... (bubble numbers)
  kind: AnnotationKind;
  label: string;
  featureRef: FeatureRef;
  paramKey?: ParamKey;
  /** Multiplier/count prefix, e.g. "4×" */
  prefix?: string;
  tol: TolSpec;
  /** Manually typed dimension text. A classic source of drawing/model conflicts. */
  override?: string;
  /** For finish/gdt/note annotations */
  text?: string;
  /** Anchor placement key understood by the drawing generator */
  place: string;
  /** Feature instance this annotation belongs to (derived per-instance callouts, e.g. A15-H-01). */
  featureId?: string;
}

export type FeatureRef =
  | 'outline'
  | 'base'
  | 'wall'
  | 'pocket'
  | 'bores'
  | 'cbores'
  | 'holes'
  | 'taps'
  | 'ribs'
  | 'material'
  | 'surface'
  | 'general';

export type UserEntity =
  | { id: string; type: 'line'; layer: string; x1: number; y1: number; x2: number; y2: number }
  | { id: string; type: 'circle'; layer: string; cx: number; cy: number; r: number }
  | { id: string; type: 'rect'; layer: string; x: number; y: number; w: number; h: number }
  | { id: string; type: 'text'; layer: string; x: number; y: number; text: string; h: number }
  | {
      id: string;
      type: 'dim';
      layer: string;
      x1: number;
      y1: number;
      x2: number;
      y2: number;
      offset: number;
      tol: TolSpec;
      override?: string;
    }
  /** Counter-clockwise from a0 to a1; degrees in DXF convention (y up), same as the drawing's arc prims. */
  | { id: string; type: 'arc'; layer: string; cx: number; cy: number; r: number; a0: number; a1: number }
  /** Open polyline through pts (sheet mm, y down); exported as LINE segments. */
  | { id: string; type: 'polyline'; layer: string; pts: [number, number][] };

export interface DrawingNotes {
  material: string;
  surface: string;
  generalTol: string;
  extra: string[];
}

export interface DrawingRevision {
  id: string;
  partId: string;
  rev: string;
  modelRevId: string;
  annotations: Annotation[];
  userEntities: UserEntity[];
  notes: DrawingNotes;
  status: 'draft' | 'confirmed' | 'superseded';
  createdAt: string;
  createdBy: Role;
  confirmedAt?: string;
  confirmedBy?: Role;
  /** Result of the last real CAD-MCP execution. Absent = never executed on a workstation. */
  cadExecution?: { at: string; workstation: string; ok: boolean; log: string[] };
}

/**
 * Design version (DV-A, DV-B …): a model revision and a drawing revision made
 * from that model, confirmed together by the design engineer. It is the only way
 * the baseline moves; planning and quoting work on exactly one design version.
 */
export interface DesignVersion {
  id: string;
  rev: string;
  modelRevId: string;
  drawingRevId: string;
  status: 'confirmed' | 'superseded';
  confirmedAt: string;
  confirmedBy: Role;
  note: string;
}

export interface Order {
  id: string;
  customer: string;
  partId: string;
  qtyTiers: number[];
  dueDays: number;
  /** Material as typed into the inquiry form by sales (unverified). */
  materialOnInquiry: string;
  inquiryDate: string;
}

export type IssueKind = 'conflict' | 'missing' | 'low_confidence' | 'baseline';

export interface ResolutionOption {
  id: string;
  label: string;
  /** Resolving with this option closes the issue; 'pending' keeps it open (e.g. waiting for customer). */
  closes: boolean;
  followUp?: string;
}

export interface PlanningIssue {
  id: string;
  kind: IssueKind;
  title: string;
  detail: string;
  refs: { annotations: string[]; features: FeatureRef[]; ops: string[] };
  evidence: string[];
  options: ResolutionOption[];
}

export interface Resolution {
  issueId: string;
  optionId: string;
  note: string;
  by: Role;
  at: string;
}

export interface AuditEvent {
  id: string;
  at: string;
  actor: Role | 'system';
  action: string;
  target: string;
  detail?: string;
}

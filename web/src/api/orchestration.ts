// W8 orchestration of the general mechanical engineering chain: the port values of the chain nodes
// (server/app/services/orchestration.py spec_brief / AssemblyBuild.brief / drawing_brief / delivery_brief) and the
// delivery manifest API (server/app/api/deliveries.py).

import { request } from './client';
import type { ExecutionPolicy, Verification } from './engineering';

type UUID = string;
type Opt = { signal?: AbortSignal };
const enc = encodeURIComponent;

export interface SpecQuestionBrief {
  id: string;
  subject: string | null;
  question: string | null;
  affects?: string[] | null;
}

export interface AssumptionBrief {
  id?: string;
  subject: string;
  field?: string | null;
  adopted?: unknown;
  unit?: string | null;
  author?: string | null;
  basis?: string | null;
  owner?: { type: string; id: string } | null;
}

/** Generation supplement §2.3 (server policy.decision_record): one AI / rule assumption read as a decision. */
export interface AiDecision {
  id?: string | null;
  subject: string;
  field: string;
  trigger: 'missing' | 'ambiguous' | 'conflict' | null;
  adopted: unknown;
  unit?: string | null;
  basis: string;
  affected: string[];
  sources: string[];
  alternatives: unknown[];
  author: 'ai_turbo' | 'rule' | string;
  state: 'adopted' | 'confirmed' | 'corrected' | 'replaced' | string;
  problems: ('no_value' | 'no_basis' | 'generic_basis' | 'no_subject' | string)[];
  owner?: { type: string; id: string; rev_no?: number; version_no?: number } | null;
  /** Generation reliability §2.6: where it was actually used (a delivery's decisions; absent elsewhere). */
  consumed_by?: DecisionUse[];
}

/** server policy.decisions_summary */
export interface AiDecisionSummary {
  count: number;
  by_trigger: Record<string, number>;
  needs_confirmation: number;
  with_problems: number;
  items: AiDecision[];
}

export type CoverageStage = 'extraction' | 'structure' | 'modeling' | 'placement' | 'assembly';

/** One missing object (server services/spec_coverage.missing_item). */
export interface CoverageMissing {
  stage: CoverageStage | string;
  key: string;
  name: string;
  kind: string;
  code: string;
  reason: string;
  quantity: number;
  instances: string[];
  recovery: string | null;
  [k: string]: unknown;
}

/** Target vs actual of an assembly build (server services/spec_coverage.coverage); the target never shrinks. */
export interface AssemblyCoverage {
  status: 'complete' | 'partial' | 'none';
  target: { basis: 'spec' | string; definitions: number; instances: number; subassemblies: number; subassembly_instances: number; recognized_definitions: number };
  built: { definitions: number };
  placed: { definitions: number; instances: number };
  /** Given a display position by a recorded decision only: never counted as installed. */
  arranged: { definitions: number; instances: number };
  /** Installed where a recorded Turbo AI decision placed them (a subset of `placed`); absent when there is none. */
  decided?: { definitions: number; instances: number };
  missing: CoverageMissing[];
  missing_counts: Partial<Record<CoverageStage, number>>;
  // Generation reliability §2.6 (package L): `placed` counts an instance only when its whole parent chain is placed.
  /** Positioned only locally, without a resolved position up to the root assembly: never installed (absent on older blocks). */
  local_only?: { definitions: number; instances: number };
  /** Sub-assembly instances (spec keys) placed up to the root, of all under the root; the kept ones without a position. */
  subassembly_placement?: { placed: number; total: number; unplaced: string[] };
  /** Generation reliability round 2 (P): installed at an AI-estimated position (own or of a sub-assembly instance above;
   * a subset of `placed`, shown apart). Absent on older blocks. */
  estimated?: { definitions: number; instances: number };
  /** Generation reliability K (design §2.5): the spec's candidates by identity class (not-parts are excluded, never
   * missing) and the lineage's frozen original target next to the current one. Absent on blocks from before. */
  identity?: CoverageIdentityCounts;
  /** Retained catalog variants outside this product, each supported by a recorded scope decision. */
  catalog_alternatives?: { key: string; name: string; reason?: string; sources?: string[]; decision?: string; selected_variant?: string }[];
  baseline?: CoverageBaseline;
  /** Generation reliability A: the unbound mentions (name, number, reason, quote) — traceable, never definitions. */
  unbound?: { name: string; number?: string; reason?: string; quote?: string; source?: string; chunk?: number }[];
}

export interface CoverageIdentityCounts {
  parts: number;
  subassemblies: number;
  aliases: number;
  excluded: number;
  catalog_alternatives?: number;
  unresolved: number;
  /** Names the sources mention that bound to no skeleton component (listed in `unbound`, never a definition). */
  unbound?: number;
}

export interface CoverageBaseline {
  spec_version_id: UUID | null;
  label: string;
  definitions: number;
  instances: number;
  subassemblies: number;
}

/** Generation reliability §2.6: where an AI decision was actually used (server assemblies.decision_consumers). */
export type DecisionUseStage = 'spec' | 'modeling' | 'placement' | 'drawing' | 'export';

export interface DecisionUse {
  stage: DecisionUseStage | string;
  /** Like an owner ({type, id, rev_no, label}), or a delivered file ({type: 'file', path}). */
  object: { type: string; id?: string; rev_no?: number; label?: string; path?: string };
  /** The spec key(s) it reached there (definition, instances). */
  ref?: string | string[] | null;
}

/** `engineering_spec` port value. */
export interface SpecValue {
  type: 'engineering_spec';
  spec_id: UUID;
  spec_version_id: UUID;
  code: string | null;
  title: string;
  version_no: number;
  status: 'extracting' | 'ready' | 'needs_answers' | 'failed' | string;
  execution_policy: ExecutionPolicy;
  simulated: boolean;
  counts: Record<string, number>;
  path: string;
  open_questions: SpecQuestionBrief[];
  assumptions: AssumptionBrief[];
  readiness?: { blocked_definitions: string[]; buildable_definitions: string[]; gate: string };
  error?: { stage?: string; code?: string; message?: string; recovery?: string } | null;
  /** Generation supplement A-02: what the spec step really read, per source (absent on values from before). */
  sources?: SpecSourceBrief[];
  /** A pasted text whose lines are also in an attached text file: read once, from the file. */
  input_dedupe?: SpecInputDedupe | null;
  /** Derived by a server rule (Turbo: definitions that hung nowhere hung under the root, one rule decision each; or,
   * rule ``turbo_alias_resolution`` (supplement H), the AI answers for them: aliases merged, members hung, the rest left). */
  derived?: {
    from_version_id?: UUID | null;
    rule?: string;
    definitions?: string[];
    merged?: { alias: string; alias_name?: string; into: string; into_name?: string }[];
    hung?: { key: string; name?: string; under: string }[];
    not_a_part?: { key: string; name?: string }[];
    unknown?: { key: string; name?: string }[];
  } | null;
  /** Generation supplement I (A-04): a new version of the same spec — revised with new material (reason ``revise``) or
   * read again after a source could not be read (``replace_source``). */
  revision?: SpecRevision | null;
}

export interface SpecRevision {
  reason: 'revise' | 'replace_source' | string;
  of_version_id: UUID;
  of_version_no: number;
  added_files: { source: string; file_version_id: UUID; label: string }[];
  dropped_files: { file_version_id: UUID; label: string | null }[];
  note_source: string | null;
  note_chars: number;
  answers_kept: number;
}

export type SpecSourceReadStatus = 'read' | 'partial' | 'unread' | 'failed';

/** One source of a spec version (server services/attachments.source_entry). */
export interface SpecSourceBrief {
  id: string;
  kind: 'text' | 'file_version' | 'drawing' | string;
  file_version_id: UUID | null;
  name: string;
  label: string;
  version: number | null;
  read_status: SpecSourceReadStatus | string;
  ranges: string[];
  error: { code?: string; message?: string } | null;
  answer_of?: string;
  /** Chunks not sent. */
  unread?: number;
  chars_read?: number;
  /** Generation reliability K (design §2.4): a drawing's source coverage next to its chunk progress. */
  coverage?: SourceCoverage;
}

/** Where every model-space entity of a drawing went: frames, outside frames (read / excluded with reason / unexplained),
 * quarantined (listed apart). The source is ``read`` only when nothing is unexplained and every chunk was sent. */
export interface SourceCoverage {
  frames: number;
  chunks?: number;
  chunks_sent?: number;
  entities: {
    total: number;
    in_frames: number;
    quarantined: number;
    quarantined_reasons?: Record<string, number>;
    outside: { total?: number; read: number; excluded: Record<string, number>; unexplained: number };
  };
  outside_groups_total?: number;
}

export interface SpecInputDedupe {
  rule: 'pasted_lines_in_attachment' | string;
  lines_removed: number;
  chars_removed: number;
  text_chars_kept: number;
  text_dropped: boolean;
  files: { file_version_id: UUID; name: string; version: number }[];
}

export type DefinitionStatus = 'built' | 'blocked' | 'needs_clarification' | 'failed' | 'not_started' | string;

export interface DefinitionOutcome {
  key: string;
  name: string;
  kind: string;
  status: DefinitionStatus;
  quantity?: number;
  source?: 'template' | 'ai_modeling' | string;
  part_id?: UUID;
  part_number?: string;
  revision_id?: UUID;
  rev_no?: number;
  questions?: { id?: string; subject?: string | null; question?: string | null; source?: string }[];
  /** S3 failure info: {stage, code, message, retryable, recovery}. */
  error?: { code?: string; message?: string; stage?: string; retryable?: boolean; recovery?: string } | null;
  assumptions?: AssumptionBrief[];
  notes?: string[];
  simulated?: boolean;
  /** S3: built by an earlier attempt (or the run it continues) and still valid; kept without a model call. */
  reused?: boolean;
}

/** The counts of `policy.decisions_summary` (port values carry them without the items). */
export type AiDecisionCounts = Omit<AiDecisionSummary, 'items'>;

/** `assembly` port value: the revision when it was built (also a partial one), and every definition's outcome. */
export interface AssemblyValue {
  type: 'assembly';
  spec_version_id: UUID;
  execution_policy: ExecutionPolicy;
  /** True only with a revision that holds the whole target (coverage complete). */
  complete: boolean;
  definitions: DefinitionOutcome[];
  parts: { key: string; part_id: UUID; revision_id: UUID; rev_no: number; number: string | null; name: string }[];
  unplaced_definitions: string[];
  simulated: boolean;
  assembly_id?: UUID;
  assembly_revision_id?: UUID;
  rev_no?: number;
  label?: string;
  number?: string | null;
  name?: string | null;
  stats?: Record<string, number>;
  check_status?: string;
  verification?: Verification;
  failure?: { code: string; message: string; details?: Record<string, unknown>; stage?: string; retryable?: boolean; recovery?: string } | null;
  /** S3: definitions built / kept from an earlier attempt / total. */
  progress?: { built: number; reused: number; total: number; scope?: string | null };
  /** Generation supplement B: target vs actual (also when no assembly could be made: status none). */
  coverage?: AssemblyCoverage | null;
  /** How many AI / rule decisions the revision and its built parts rest on. */
  decisions?: AiDecisionCounts | null;
}

/** Generation supplement E: the package of one sub-assembly revision made by a layered drawing step (server
 * orchestration.make_level_drawings). One that could not be made has no package_id, completeness `failed` and its error. */
export interface SubassemblyPackage {
  assembly_id: UUID;
  assembly_revision_id: UUID;
  number: string | null;
  name: string | null;
  rev_no: number | null;
  /** 1: directly under the root; 2: one level deeper … */
  depth: number;
  /** Instances of this revision in the whole tree (repeated instances share one package). */
  instances: number;
  package_id: UUID | null;
  completeness: 'complete' | 'partial' | 'failed' | string;
  pages?: number;
  files?: number;
  error?: { code: string; message: string };
}

/** `drawing_package` port value (W5 package). */
export interface DrawingPackageValue {
  type: 'drawing_package';
  /** Null only when a layered drawing step could not make the root package but kept sub-assembly packages (see error). */
  package_id: UUID | null;
  completeness: string;
  manifest_sha256: string | null;
  pages: number;
  files: number;
  /** Drawn from a partial assembly revision (generation supplement B-03). */
  coverage?: AssemblyCoverage | null;
  /** Generation supplement E (layered drawing step): one entry per distinct sub-assembly revision. */
  subassembly_packages?: SubassemblyPackage[];
  error?: { code: string; message: string };
}

/** Generation supplement E: the delivery's per-level summary (server orchestration._deliver_levels). */
export type DeliveryLevel = 'assembly' | 'subassembly' | 'part';
/** Generation reliability M: protocol_unmet = no STEP in the required protocol (a labelled alternative may exist). */
export type LevelStatus = 'generated' | 'missing' | 'failed' | 'protocol_unmet';

export interface LevelFile {
  status: LevelStatus | string;
  /** STEP: its path in the delivery zip. */
  path?: string | null;
  /** DWG: the path of every sheet's DWG in the zip. */
  paths?: string[];
  sha256?: string;
  package_id?: UUID;
  code?: string;
  message?: string | null;
  /** STEP (generation reliability M): the application protocol read from the file header (AP203 / AP214 …). */
  protocol?: string | null;
  required_protocol?: string;
  /** The stored file was in this protocol and re-exported by the kernel. */
  reexported_from?: string | null;
  /** protocol_unmet: the file kept in another protocol, labelled (never counted as the required one). */
  alternative?: { path: string; protocol: string | null; sha256?: string } | null;
}

/** Generation reliability M: what one part revision's build made and did not make (server cad/build_record.py). */
export interface BuildRecord {
  representation: 'exact' | 'nominal' | 'simplified' | 'placeholder' | string;
  created: { op_id: string | null; type: string | null; summary: string | null; text?: string | null; font_used?: string | null }[];
  not_created: { requirement: string; reason: string; [k: string]: unknown }[];
  usable: boolean;
  complete: boolean;
}

export interface LevelObject {
  key: string | null;
  number: string | null;
  name: string | null;
  /** A1 / R2 … (null for a definition that was never built). */
  rev: string | null;
  rev_no: number | null;
  step: LevelFile;
  dwg: LevelFile;
  depth?: number;
  instances?: number;
  procurement?: string | null;
  representation?: string | null;
  quantity?: number | null;
  in_assembly?: boolean;
  /** Listed from the assembly coverage (not built / left out): no files; the stage says where it stopped. */
  coverage?: boolean;
  stage?: string;
  /** Generation reliability N: the object's state (features/orchestration/status.ts), apart from its check status. */
  state?: string;
  /** Part level (generation reliability M): the part revision's build record. */
  build_record?: BuildRecord;
}

export interface LevelCounts {
  objects: number;
  step: Record<LevelStatus, number>;
  dwg: Record<LevelStatus, number>;
}

export interface DeliveryLevels {
  assembly: LevelObject[];
  subassembly: LevelObject[];
  part: LevelObject[];
  counts: Record<DeliveryLevel, LevelCounts>;
  /** Generation reliability M: the protocol every STEP of the delivery must be in (default AP203). */
  step_protocol_required?: string;
}

export interface MissingItem {
  item: string;
  code: string;
  message: string;
  [k: string]: unknown;
}

/** S6: the check status each delivered source recorded (server orchestration._delivery_source_checks). */
export interface DeliverySourceCheck {
  type: 'assembly_revision' | 'model_revision' | 'drawing_package' | string;
  id: UUID;
  label: string;
  check_status: string;
}

/** The delivery's verification block: a failed source makes the delivery's check_status failed (never only unverified). */
export type DeliveryVerification = Verification & { source_checks?: DeliverySourceCheck[] };

/** Generation reliability N: which business checks the delivery's policy closed and how often one of them really ran,
 * from the run's check ledger (server services/verification.checks_report). */
export interface ChecksReport {
  policy: ExecutionPolicy;
  /** The checks the policy closes (Turbo: the W0 business checks; standard: none). */
  closed: string[];
  /** How often a closed check really ran: 0 under Turbo. */
  invoked_closed: number;
  invoked?: Record<string, number>;
  skipped?: Record<string, number>;
  /** run: counted over the delivering run; delivery: a delivery made outside a run (only that call). */
  basis?: 'run' | 'delivery' | string;
  run_id?: UUID;
  nodes_recorded?: number;
  nodes_unrecorded?: string[];
  tool_calls_recorded?: number;
  tool_calls_unrecorded?: number;
}

/** `delivery` port value. */
export interface DeliveryValue {
  type: 'delivery';
  delivery_id: UUID;
  number: string;
  completeness: 'complete' | 'partial' | string;
  execution_policy: ExecutionPolicy;
  check_status: string;
  simulated: boolean;
  files: number;
  missing: MissingItem[];
  manifest_sha256: string;
  zip_sha256: string;
  verification: DeliveryVerification | Record<string, never>;
  assembly_revision_id: UUID | null;
  /** Generation supplement B: what the delivered assembly holds of its target; the AI decisions it rests on. */
  coverage?: AssemblyCoverage | null;
  decisions?: AiDecisionCounts | null;
  /** Generation supplement E: a layered delivery's objects per level with their STEP / DWG status. */
  levels?: DeliveryLevels | null;
  /** Generation reliability N: the delivery's state and its check report (closed checks really run: 0 under Turbo). */
  state?: string | null;
  checks?: ChecksReport | null;
}

export interface DeliveryFile {
  path: string;
  sha256: string;
  bytes: number;
  kind: string;
  item: string;
  source: Record<string, unknown>;
}

export interface DeliveryManifestBody {
  schema: string;
  units: string;
  execution_policy: ExecutionPolicy;
  completeness: string;
  simulated: boolean;
  sources: Record<string, Record<string, unknown>>;
  required: { item: string; from_spec: boolean }[];
  files: DeliveryFile[];
  missing: MissingItem[];
  representation: Record<string, string>;
  assumptions: AssumptionBrief[];
  verification: Verification;
  upstream: { node_id: string; status: string; error?: { code?: string; message?: string } | null }[];
  coverage?: AssemblyCoverage | null;
  decisions?: AiDecisionSummary | null;
  /** report/coverage.json, report/ai_decisions.json, report/ai_decisions.md in the zip. */
  reports?: { path: string; sha256: string; bytes: number }[];
  levels?: DeliveryLevels;
}

export interface Delivery extends DeliveryValue {
  id: UUID;
  project_id: UUID;
  manifest: DeliveryManifestBody;
  spec_version_id: UUID | null;
  drawing_package_id: UUID | null;
  asset_version_id: UUID | null;
  labels: string[];
  stale: { code: string; latest_revision_id: UUID; latest_rev_no: number } | null;
  created_at: string | null;
}

export type ChainValue = SpecValue | AssemblyValue | DrawingPackageValue | DeliveryValue;
export const CHAIN_TYPES = ['engineering_spec', 'assembly', 'drawing_package', 'delivery'] as const;

export function isChainValue(v: unknown): v is ChainValue {
  return typeof v === 'object' && v !== null && (CHAIN_TYPES as readonly string[]).includes((v as { type?: unknown }).type as string);
}

export const getDelivery = (id: UUID, o: Opt = {}) => request<Delivery>(`/delivery-manifests/${enc(id)}`, { signal: o.signal });
export const listDeliveries = (projectId: UUID, o: Opt & { assemblyRevisionId?: UUID } = {}) =>
  request<DeliveryValue[]>(`/projects/${enc(projectId)}/delivery-manifests`, { query: { assembly_revision_id: o.assemblyRevisionId }, signal: o.signal });

/** Authenticated download of a delivery zip (every delivered file + manifest.json). */
export async function downloadDelivery(id: UUID): Promise<{ blob: Blob; filename: string | null }> {
  const res = await request<Response>(`/delivery-manifests/${enc(id)}/download`, { as: 'response' });
  const header = res.headers.get('Content-Disposition');
  const m = header ? /filename\*=UTF-8''([^;]+)/i.exec(header) : null;
  return { blob: await res.blob(), filename: m ? decodeURIComponent(m[1]) : null };
}

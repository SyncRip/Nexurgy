import { operationChoices } from './capabilities';
// Engineering spec API (W2; server/app/api/engineering_specs.py, docs/development/mechanical-engineering/w0-contract.zh-CN.md §5).
// A spec is a project-scoped lineage of immutable versions. Creating, answering and re-extracting return a new
// version in status "extracting"; the server runs the extraction in the background — poll the version.
// Read state comes from what the server really sent to the model (chunks), never from the model's own claim.

import { request } from './client';
import type { Assumption,ExecutionPolicy,FactStatus,ProcurementKind,RepresentationLevel,Verification } from './engineering';
import type { ISODateTime,UUID } from './types';

type Opt = { signal?: AbortSignal };
const enc = encodeURIComponent;

export const SPEC_VERSION_STATUSES = ['extracting', 'ready', 'needs_answers', 'failed'] as const;
export type SpecVersionStatus = (typeof SPEC_VERSION_STATUSES)[number];
export type SpecOrigin = 'create' | 'answer' | 'reextract' | 'continue' | 'rule';
export const READ_STATUSES = ['read', 'partial', 'unread', 'failed'] as const;
export type ReadStatus = (typeof READ_STATUSES)[number];
export type ChunkStatus = 'pending' | 'sent' | 'failed' | 'unread';
export const FAILURE_STAGES = ['context_build', 'provider_select', 'provider_call', 'parse', 'validate', 'over_budget', 'interrupted'] as const;
export type FailureStage = (typeof FAILURE_STAGES)[number];
export type Readiness = 'ready' | 'blocked' | 'partial';

/** `{stage, code, message, detail, call_ids, retryable, recovery, recovery_message}`. `recovery` is a code (S3:
 * continue_extract | re_extract | split_sources); `recovery_message` the server's sentence. */
export interface SpecError {
  stage: FailureStage | string;
  code: string;
  message: string;
  detail?: unknown;
  call_ids?: string[];
  retryable?: boolean;
  recovery?: string;
  recovery_message?: string;
}

/** One source of a version with its read state (computed from the chunks the server sent). */
export interface SpecSource {
  id: string;
  kind: 'text' | 'file_version' | 'drawing';
  label: string;
  name?: string;
  file_version_id: UUID | null;
  file_id?: UUID | null;
  version?: number | null;
  sha256: string;
  bytes?: number | null;
  content_type?: string | null;
  answer_of?: string | null;
  read_status: ReadStatus;
  ranges: string[];
  unread?: { chunk: number; locator: string; status: ChunkStatus; reason: string | null }[];
  not_chunked?: string[];
  notes?: string[];
  chunks?: number;
  /** Text sources: characters of the whole text and characters read (overlapping chunks counted once). */
  chars?: number;
  chars_read?: number;
  error: { code: string; message: string } | null;
}

export interface SpecChunk {
  seq: number;
  source_id: string;
  source_chunk: number;
  kind: 'text' | 'drawing' | 'attachment';
  locator: string;
  start: number | null;
  end: number | null;
  chars: number;
  status: ChunkStatus;
  reason: string | null;
  ai_call_id: UUID | null;
  inherited_from_version_id: UUID | null;
  sent_at: ISODateTime | null;
  text_sha256: string | null;
  /** S4: leading characters repeated from the previous chunk of the same source. */
  overlap?: number;
  /** S3: the chunk's findings are stored (a continuation reuses it instead of sending it again). */
  findings_stored?: boolean;
  call_id?: UUID | null;
}

export interface SpecEvidence {
  source: string;
  chunk: number | null;
  locator: string;
  quote?: string | null;
  entity?: string | null;
  region?: number[] | null;
  detail?: string;
  /** The quote / drawing entity was found by the server in the chunk it sent. */
  verified: boolean;
  /** Attachment region (no scale): located, but its content cannot be checked by the server. */
  located?: boolean;
}

export interface SpecDefinition {
  key: string;
  kind: 'part' | 'assembly' | 'standard_part';
  name: string;
  number: string | null;
  material: string | null;
  procurement: ProcurementKind;
  representation: RepresentationLevel;
  template: { id: string; params: Record<string, unknown> } | null;
  fact_ids: string[];
  notes: string;
}

export interface SpecInstance {
  key: string;
  definition: string;
  parent: string | null;
  name: string;
  placement: { translate: number[]; rotate: number[] } | null;
  fact_ids: string[];
}

export interface SpecRelation {
  key: string;
  kind: string;
  a: { instance: string; datum: string };
  b: { instance: string; datum: string };
  params: Record<string, unknown>;
  fact_ids: string[];
}

export interface SpecFact {
  id: string;
  subject: string;
  kind: string;
  value: unknown;
  unit: string | null;
  status: FactStatus;
  evidence: SpecEvidence[];
  derivation: string | null;
}

export interface SpecAnswer {
  answer: string;
  by: UUID;
  by_name?: string;
  at: ISODateTime;
  source: string;
}

export interface SpecQuestion {
  id: string;
  subject: string;
  question: string;
  affects: string[];
  candidates: unknown[];
  origin?: 'model' | 'server' | 'converted_assumption';
  answer: SpecAnswer | null;
}

export interface SpecConflict {
  id: string;
  subject: string;
  values: { value: unknown; unit?: string | null; source: string; chunk?: number | null; quote?: string | null; entity?: string | null; verified?: boolean }[];
  resolution: Record<string, unknown> | null;
}

export interface SpecContent {
  units: 'mm';
  root: string | null;
  sources: Pick<SpecSource, 'id' | 'kind' | 'file_version_id' | 'label' | 'sha256' | 'read_status' | 'ranges' | 'error' | 'answer_of'>[];
  definitions: SpecDefinition[];
  instances: SpecInstance[];
  relations: SpecRelation[];
  facts: SpecFact[];
  manufacturing_notes: { id: string; subject: string; text: string; fact_ids: string[]; evidence?: SpecEvidence[] }[];
  deliverables: { kind: string; required: boolean }[];
  questions: SpecQuestion[];
  conflicts: SpecConflict[];
  assumptions: Assumption[];
  summary?: string;
}

export interface ReadinessEntry {
  state: Readiness;
  questions: string[];
  below?: string[];
}

export interface SpecReadiness {
  gate: 'applied' | 'skipped_turbo';
  definitions: Record<string, ReadinessEntry>;
  instances: Record<string, ReadinessEntry>;
  relations: Record<string, ReadinessEntry>;
  buildable_definitions: string[];
  blocked_definitions: string[];
}

export interface SpecCounts {
  definitions: number;
  instances: number;
  relations: number;
  facts: number;
  questions_open: number;
  conflicts: number;
  assumptions: number;
  sources: number;
  sources_not_read: number;
}

export interface SpecVersionBrief {
  id: UUID;
  spec_id: UUID;
  version_no: number;
  origin: SpecOrigin;
  status: SpecVersionStatus;
  execution_policy: ExecutionPolicy;
  simulated: boolean;
  parent_version_id: UUID | null;
  counts: SpecCounts;
  error: Pick<SpecError, 'stage' | 'code' | 'message' | 'retryable' | 'recovery'> | null;
  created_by: UUID;
  created_at: ISODateTime | null;
  finished_at: ISODateTime | null;
}

/** An AI decision the server did not keep (generation supplement D-03): ``stated_value`` (the source states the value
 * without a conflict), ``answered`` (a person's answer replaced it) or ``not_open`` (nothing to decide). */
export interface DroppedDecision {
  subject: string;
  adopted: unknown;
  unit?: string | null;
  reason: 'stated_value' | 'answered' | 'not_open' | string;
  assumption?: string;
  stated?: unknown;
  stated_unit?: string | null;
  by?: string;
}

export interface SpecVersion extends Omit<SpecVersionBrief, 'error'> {
  error: SpecError | null;
  /** S3: the version that continued this failed one. */
  continued_by?: { id: UUID; version_no: number } | null;
  content: SpecContent | Record<string, never>;
  sources: SpecSource[];
  chunks: SpecChunk[];
  readiness: SpecReadiness | null;
  quantities: Record<string, { total: number; per_parent: Record<string, number>; instances: string[] }> | null;
  checks: { check: string; status: string; open_questions?: string[] }[];
  validation: {
    problems?: string[];
    repairs?: number;
    demoted?: { fact: string; subject: string; problems: string[] }[];
    converted?: unknown[];
    /** Generation supplement D-03: AI decisions dropped because the value is stated (or a person answered it). */
    dropped_assumptions?: DroppedDecision[];
  };
  planning: {
    /** Server merge record (S7); generation supplement D: decisions reused from the re-read version / dropped. */
    merge?: { resolve_calls?: number; reused_decisions?: number; dropped_decisions?: DroppedDecision[] };
    provider?: string;
    model_id?: string;
    model_config_id?: string | null;
    simulated?: boolean;
    prompt_version?: string;
    ai_call_ids?: string[];
    chunks_total?: number;
    chunks_sent?: number;
    /** S3 continuation: chunks reused from the version it continues (not sent again) and that version. */
    chunks_reused?: number;
    continued_from?: UUID;
    repairs?: number;
  };
  answers: { question_id: string; answer: string; by: UUID; by_name?: string; at: ISODateTime; source: string }[];
  verification: Verification;
  model_config_id: UUID | null;
  spec?: { id: UUID; code: string; title: string };
}

export interface EngineeringSpec {
  id: UUID;
  project_id: UUID;
  number: number;
  code: string;
  title: string;
  created_by: { id: UUID; name: string };
  created_at: ISODateTime | null;
  updated_at: ISODateTime | null;
  versions_count: number;
  latest_version: SpecVersionBrief | null;
  versions?: SpecVersionBrief[];
}

export interface SpecCreate {
  title?: string;
  /** Pasted text: no chat-size cap; the server chunks it (≈12k chars, ≤ 40 chunks per version). */
  text?: string;
  file_version_ids?: UUID[];
  /** Chosen explicitly every time (standard by default); never inherited from a previous choice. */
  execution_policy?: ExecutionPolicy;
  model_config_id?: UUID | null;
}

export const createSpec = (projectId: UUID, body: SpecCreate) =>
  request<EngineeringSpec & { version: SpecVersion }>(`/projects/${enc(projectId)}/engineering-specs`, { method: 'POST', body: operationChoices(body) });

export const listSpecs = (projectId: UUID, q: { limit?: number; offset?: number } = {}, o: Opt = {}) =>
  request<{ items: EngineeringSpec[]; total: number }>(`/projects/${enc(projectId)}/engineering-specs`, { query: q, signal: o.signal });

export const getSpec = (specId: UUID, o: Opt = {}) => request<EngineeringSpec>(`/engineering-specs/${enc(specId)}`, { signal: o.signal });

export const getSpecVersion = (versionId: UUID, o: Opt = {}) =>
  request<SpecVersion>(`/engineering-spec-versions/${enc(versionId)}`, { signal: o.signal });

export const getSpecChunk = (versionId: UUID, seq: number, o: Opt = {}) =>
  request<SpecChunk & { text: string }>(`/engineering-spec-versions/${enc(versionId)}/chunks/${seq}`, { signal: o.signal });

export const answerSpecQuestions = (versionId: UUID, answers: { question_id: string; answer: string }[]) =>
  request<SpecVersion>(`/engineering-spec-versions/${enc(versionId)}/answers`, { method: 'POST', body: { answers } });

export const reextractSpec = (versionId: UUID, body: { execution_policy?: ExecutionPolicy; model_config_id?: UUID | null } = {}) =>
  request<SpecVersion>(`/engineering-spec-versions/${enc(versionId)}/reextract`, { method: 'POST', body: operationChoices(body) });

/** S3: continue a failed extraction (recovery continue_extract): a new version reusing the chunks already read. */
export const continueSpec = (versionId: UUID) =>
  request<SpecVersion>(`/engineering-spec-versions/${enc(versionId)}/continue`, { method: 'POST' });

/** In-app path of a spec (optionally an exact version). */
export const specPath = (specId: UUID, versionId?: UUID) => (versionId ? `/specs/${specId}?version=${versionId}` : `/specs/${specId}`);

/** A finished version (content usable): ready or waiting for answers. */
export const isUsable = (s: SpecVersionStatus) => s === 'ready' || s === 'needs_answers';

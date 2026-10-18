// W7 later standard checks on one exact artifact (AC-T08): server app/api/revisions.py and app/api/drawings.py
// (`/revisions/{id}/checks`, `/drawings/{id}/checks`), app/services/verification.py. A new check is an append-only
// record bound to the artifact's bytes; the record made at creation (e.g. skipped_turbo) is never rewritten.

// An artifact is verified only when every skipped check re-ran and passed AND a person confirmed every AI / rule
// assumption (`POST /revisions/{id}/assumptions/{aid}/confirm`, `POST /drawings/{id}/assumptions/{aid}/confirm`):
// an append-only `assumption.confirmation` record. A different value is a correction: recorded, the artifact stays
// not verified, the change goes through a new revision.

import { request } from './client';
import type { AssumptionRef, CheckStatus, ExecutionPolicy, Verification } from './engineering';
import type { ISODateTime, Sha256, UUID } from './types';

type Opt = { signal?: AbortSignal };
const enc = encodeURIComponent;

export type RevisionCheckName = 'model.conformance' | 'geometry.features';
export type DrawingCheckName = 'drawing.annotation' | 'drawing.dwg_semantic';

/** verification.check_out */
export interface ArtifactCheckRecord {
  id: UUID;
  subject_type: 'model_revision' | 'drawing' | 'assembly_revision' | 'engineering_spec_version';
  subject_id: UUID;
  /** The exact bytes checked: the revision's step_sha / the drawing's manifest_sha256. */
  subject_sha: Sha256 | null;
  check: RevisionCheckName | DrawingCheckName | string;
  execution_policy: ExecutionPolicy;
  status: CheckStatus;
  report: Record<string, unknown>;
  requested_by: UUID | null;
  created_at: ISODateTime | null;
}

/** A person's decision on one assumption, as listed in the verification block. */
export interface AssumptionDecision extends AssumptionRef {
  decision: 'confirmed' | 'corrected';
  value: unknown;
  by: { id: UUID; name: string } | null;
  at: ISODateTime | null;
  record_id: UUID;
}

/** Verification plus the status per check name and, for a drawing, whether its source revision is unverified. */
export interface ArtifactVerification extends Verification {
  checks?: Record<string, CheckStatus>;
  /** Skipped checks no later run can repeat (model.grounding / model.gate), satisfied by the person's confirmations. */
  satisfied_checks?: Record<string, { by: string; assumptions: string[]; rechecked: string[] }>;
  corrected_assumptions?: AssumptionDecision[];
  confirmed_assumptions?: AssumptionDecision[];
  upstream_unverified?: {
    type: string;
    /** The source revision (drawings); an assembly revision lists its unverified inputs under `items` instead (S5). */
    id?: UUID;
    rev_no?: number;
    pending_checks?: string[];
    unconfirmed_assumptions?: string[];
    corrected_assumptions?: string[];
    count?: number;
    items?: { type: string; id: UUID; rev_no?: number; part_number?: string | null; pending_checks: string[];
      unconfirmed_assumptions: string[]; corrected_assumptions: string[] }[];
  } | null;
}

export interface ArtifactChecks {
  verification: ArtifactVerification;
  records: ArtifactCheckRecord[];
  confirmations?: ArtifactCheckRecord[];
}

export interface ArtifactCheckResult {
  record: ArtifactCheckRecord;
  records?: ArtifactCheckRecord[];
  verification: ArtifactVerification;
}

export const listRevisionChecks = (revisionId: UUID, o: Opt = {}) =>
  request<ArtifactChecks>(`/revisions/${enc(revisionId)}/checks`, { signal: o.signal });

/** Standard check on the exact revision (design permission): the requirement check on its stored STEP, or the W3
 * feature checks on a rebuild of its ops (one record per feature kind). */
export const runRevisionCheck = (revisionId: UUID, check: RevisionCheckName = 'model.conformance', o: Opt = {}) =>
  request<ArtifactCheckResult>(`/revisions/${enc(revisionId)}/checks`, { method: 'POST', body: { check }, signal: o.signal });

/** Confirm assumptions: confirm the adopted value of one AI / rule assumption (design permission). Passing another `value` records
 * a correction, which keeps the artifact not verified (the change itself is a new revision). */
export const confirmRevisionAssumption = (revisionId: UUID, assumptionId: string, body: { value?: unknown; note?: string } = {}, o: Opt = {}) =>
  request<ArtifactCheckResult>(`/revisions/${enc(revisionId)}/assumptions/${enc(assumptionId)}/confirm`, { method: 'POST', body, signal: o.signal });

/** Same from a drawing: its own assumption, or one of its source revision (decided on that revision). */
export const confirmDrawingAssumption = (drawingId: UUID, assumptionId: string, body: { value?: unknown; note?: string } = {}, o: Opt = {}) =>
  request<ArtifactCheckResult>(`/drawings/${enc(drawingId)}/assumptions/${enc(assumptionId)}/confirm`, { method: 'POST', body, signal: o.signal });

export const listDrawingChecks = (drawingId: UUID, o: Opt = {}) => request<ArtifactChecks>(`/drawings/${enc(drawingId)}/checks`, { signal: o.signal });

// ─── S5: where a request's policy comes from, and one "confirm these assumptions" entry ───────────────────────────
// Server: app/api/execution_policy.py, app/services/execution_policy.py, verification.upstream_assumptions.
// A request's policy is explicit > inherited from the version it continues from > project default > standard, frozen
// before the run and recorded as `policy_source`. A standard step that takes over a Turbo product needs a person to
// confirm every AI / rule assumption upstream; they are listed in one place and confirmed in one go.

/** Where a frozen policy came from (design §3.4). */
export type PolicySource = 'explicit' | 'inherited' | 'project_default' | 'default';

/** The exact version a follow-up request continues from. */
export interface SourceVersion {
  type: 'engineering_spec_version' | 'assembly_revision' | 'model_revision' | 'run' | string;
  id: UUID;
  label: string;
  execution_policy: ExecutionPolicy;
}

export type PolicySourceType = 'engineering_spec_version' | 'assembly_revision' | 'model_revision';

/** GET /projects/{id}/execution-policy: what a request would be frozen with. */
export interface PolicyResolution {
  execution_policy: ExecutionPolicy;
  policy_source: PolicySource;
  source_version: SourceVersion | null;
  project_default: ExecutionPolicy;
  /** For a source version: its upstream assumptions by state (a standard takeover needs `unconfirmed` = 0). */
  upstream: { unconfirmed: number; corrected: number; confirmed: number; pending: number; blocked: boolean } | null;
}

export type UpstreamState = 'unconfirmed' | 'confirmed' | 'corrected';

/** One AI / rule assumption upstream of a product, with the version that carries (and decides) it. */
export interface UpstreamAssumption extends AssumptionRef {
  key: string;
  state: UpstreamState;
  owner: { type: string; id: UUID; label: string; rev_no?: number; version_no?: number };
  basis: string;
  alternatives: unknown[];
  /** Generation supplement D-02 (server policy.decision_record): why, what it affects, where it stands, what is wrong. */
  trigger?: 'missing' | 'ambiguous' | 'conflict' | null;
  affected?: string[];
  sources?: string[];
  problems?: string[];
  decision?: 'confirmed' | 'corrected';
  value?: unknown;
  by?: { id: UUID; name: string } | null;
  at?: ISODateTime | null;
}

export interface UpstreamAssumptions {
  source: SourceVersion;
  items: UpstreamAssumption[];
  counts: Record<UpstreamState, number>;
  pending: number;
  /** A corrected value blocks a standard takeover (an engineering change: a new version). */
  blocked: boolean;
  /** POST only: the keys this call confirmed. */
  confirmed_now?: string[];
}

export const getPolicyResolution = (
  projectId: UUID,
  /** ``part``: the part's latest revision (a modeling change of a part continues from it); ``run``: the run a rerun repeats. */
  q: { source_type?: PolicySourceType | 'part' | 'run'; source_id?: UUID; explicit?: ExecutionPolicy } = {},
  o: Opt = {},
) => request<PolicyResolution>(`/projects/${enc(projectId)}/execution-policy`, { query: q, signal: o.signal });

export const listUpstreamAssumptions = (projectId: UUID, source: { source_type: PolicySourceType; source_id: UUID }, o: Opt = {}) =>
  request<UpstreamAssumptions>(`/projects/${enc(projectId)}/upstream-assumptions`, { query: source, signal: o.signal });

/** Confirm these assumptions: confirm every unconfirmed assumption upstream (or the given keys) as this person (design permission). */
export const confirmUpstreamAssumptions = (
  projectId: UUID,
  body: { source_type: PolicySourceType; source_id: UUID; keys?: string[]; note?: string },
  o: Opt = {},
) => request<UpstreamAssumptions>(`/projects/${enc(projectId)}/upstream-assumptions/confirm`, { method: 'POST', body, signal: o.signal });

/** GET /assembly-revisions/{id}/assumptions: live verification, decisions on it and its upstream list. */
export interface AssemblyAssumptions {
  verification: ArtifactVerification;
  confirmations: ArtifactCheckRecord[];
  upstream: UpstreamAssumptions;
}

export const listAssemblyAssumptions = (revisionId: UUID, o: Opt = {}) =>
  request<AssemblyAssumptions>(`/assembly-revisions/${enc(revisionId)}/assumptions`, { signal: o.signal });

/** Standard annotation check or DWG semantic readback on the package's stored files (design permission). */
export const runDrawingCheck = (drawingId: UUID, check: DrawingCheckName = 'drawing.annotation', o: Opt = {}) =>
  request<ArtifactCheckResult>(`/drawings/${enc(drawingId)}/checks`, { method: 'POST', body: { check }, signal: o.signal });

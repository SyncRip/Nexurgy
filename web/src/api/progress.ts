// Run progress (server/app/services/run_progress.py): the execution process of a run — its stages, their work
// units, the model calls behind them and the decisions taken — for the conversation card and the run page.
// Metadata only (no prompt or reply text). Also sent as `event: progress` on GET /runs/{id}/events.

import { request } from './client';

type UUID = string;
type Opt = { signal?: AbortSignal };
const enc = encodeURIComponent;

export interface ProgressCalls {
  count: number;
  failed: number;
  /** Calls whose purpose is a repair (e.g. modeling_repair). */
  repairs: number;
  /** Real usage only; a missing provider count keeps that dimension unknown. */
  tokens_in: number | null;
  tokens_out: number | null;
  latency_ms: number;
  started_at: string | null;
  purposes: string[];
  last_error: { code: string | null; failure_class: string | null; diagnostic_code?: string | null } | null;
  details: ProgressCall[];
}

export interface ProgressCall {
  id: UUID;
  purpose: string | null;
  attempt: number | null;
  status: string;
  simulated: boolean;
  started_at: string | null;
  latency_ms: number | null;
  tokens_in: number | null;
  tokens_out: number | null;
  request_id: string | null;
  error: { code: string | null; failure_class: string | null; diagnostic_code?: string | null } | null;
}

export interface ProgressAssumption {
  kind: 'assumption';
  id?: string | null;
  subject?: string | null;
  field?: string | null;
  text?: string | null;
  basis?: string | null;
  adopted?: unknown;
  unit?: string | null;
  author?: string | null;
  trigger?: string | null;
  affected?: string[] | null;
  sources?: string[] | null;
  alternatives?: unknown[] | null;
  state?: string | null;
  problems?: string[] | null;
  confirmation?: { decision?: string | null } | null;
  replaced_by?: unknown;
  owner?: { type: string; id: string; label?: string; rev_no?: number; version_no?: number } | string | null;
  at?: string | null;
}

export type ProgressDecision =
  | ProgressAssumption
  | { kind: 'check_skipped'; checks: string[] }
  | { kind: 'repair'; count: number };

export interface ProgressError {
  code?: string;
  message?: string;
  stage?: string;
  recovery?: string;
  failure_class?: string;
  diagnostic_code?: string | null;
}

/** built / failed / blocked / needs_clarification / not_started (assembly build steps), pending (not built yet while
 * the stage runs), done (a unit known only from its model calls). */
export type ProgressUnitStatus = 'built' | 'failed' | 'blocked' | 'needs_clarification' | 'not_started' | 'pending' | 'done';

export interface ProgressUnit {
  /** "definition:D-02", "spec_chunk:1", "skeleton:1", "spec_resolve", … */
  key: string;
  kind: string;
  title: string | null;
  quantity: number | null;
  status: ProgressUnitStatus | string;
  attempts: number | null;
  source?: string | null;
  revision: { id: UUID; part_id: UUID; label: string; status: string; size: number[] | null; operations: { op: string; count: number }[] } | null;
  calls: ProgressCalls | null;
  decisions: ProgressDecision[];
  error: ProgressError | null;
}

export interface ProgressStage {
  node_id: string;
  node_type: string;
  label: string | null;
  status: string;
  attempt: number;
  started_at: string | null;
  finished_at: string | null;
  units: ProgressUnit[];
  units_truncated: boolean;
  calls: ProgressCalls;
  decisions: ProgressDecision[];
  error: ProgressError | null;
}

/** Immutable committed outputs; no live/latest lookup is needed to preview a drawing. */
export type ProgressDrawing =
  | { kind: 'drawing'; id: UUID; node_id: string; model_revision_id: UUID | null; manifest_sha256: string | null }
  | { kind: 'assembly_drawing'; id: UUID; node_id: string; assembly_revision_id: UUID; manifest_sha256: string; completeness: string };

export interface RunProgress {
  run_id: UUID;
  status: string;
  policy: string;
  simulated: boolean;
  started_at: string | null;
  finished_at: string | null;
  totals: {
    calls: number;
    failed_calls: number;
    tokens_in: number | null;
    tokens_out: number | null;
    assumptions: number;
    units: number;
    units_built: number;
  };
  truncated: boolean;
  stages: ProgressStage[];
  /** Optional for older progress responses. Metadata arrives only after the saved package commits. */
  drawings?: ProgressDrawing[];
}

/** GET /runs/{id}/progress — same access as the run detail. */
export const getRunProgress = (runId: UUID, o: Opt = {}) => request<RunProgress>(`/runs/${enc(runId)}/progress`, { signal: o.signal });

// Process resources API (design §4 C08 / C10; server/app/api/resources.py).
// A workflow resource is one *use relationship* of an exact asset version in a
// run / node / slot (input, candidate, formal, failed, superseded, evidence).
// The same version has one row per use; files are never copied. Types mirror
// the server dicts; the UI never derives acceptance from `kind` or `publish_state`.

import type { AssetAvailability, AssetType, PublishState } from './assets';
import { request } from './client';
import type { ISODateTime, UUID } from './types';

type Opt = { signal?: AbortSignal };
const enc = encodeURIComponent;

export const RESOURCE_KINDS = ['input', 'candidate', 'formal', 'failed', 'superseded', 'evidence'] as const;
export type ResourceKind = (typeof RESOURCE_KINDS)[number];

export const ACCEPTANCE_STATUSES = ['not_applicable', 'unsubmitted', 'in_review', 'approved', 'rejected', 'invalidated'] as const;
export type AcceptanceStatus = (typeof ACCEPTANCE_STATUSES)[number];

export const EXECUTION_MODES = ['full_auto', 'semi_auto'] as const;
export type ExecutionMode = (typeof EXECUTION_MODES)[number];

export const ACTOR_KINDS = ['human', 'agent'] as const;
export type ActorKind = (typeof ACTOR_KINDS)[number];

export const AVAILABILITIES = ['building', 'ready', 'failed'] as const;
export const RESOURCE_TASK_STATUSES = ['todo', 'in_progress', 'in_review', 'done', 'cancelled'] as const;

/** `actors.out` (design §3.1): a human member or an agent profile, never both. */
export interface ActorOut {
  kind: ActorKind;
  id: UUID | null;
  user: { id: UUID; name: string } | null;
  agent: { id: UUID; name: string; purpose: string; version: number; enabled: boolean } | null;
}

/**
 * Read-only acceptance projection of one resource row (C10). `approved` is only
 * valid together with `current=true`; a superseded approval comes back as
 * `invalidated` with the original decision ids kept.
 */
export interface TaskAcceptance {
  status: AcceptanceStatus;
  current: boolean;
  submission_id: UUID | null;
  request_id: UUID | null;
  decision_id: UUID | null;
  actor_id: UUID | null;
  actor_kind: ActorKind | null;
  actor_user_id: UUID | null;
  actor_agent_id: UUID | null;
  decided_at: ISODateTime | null;
}

/** `version_brief` + the catalogue fields C10 adds to it. `target` is only present on servers that include it. */
export interface VersionBrief {
  id: UUID;
  asset_id: UUID;
  version_no: number;
  label: string;
  content_hash?: string | null;
  asset_type: AssetType;
  asset_name: string;
  availability: AssetAvailability;
  publish_state: PublishState;
  labels: string[];
  /** Optional on brief projections; documents may carry summary.blob_sha256 for a direct download. */
  target?: { kind?: string; id?: UUID; part_id?: UUID | null; summary?: Record<string, unknown> } | null;
}

export interface WorkflowResourceItem {
  id: UUID;
  run_id: UUID;
  node_task_id: UUID | null;
  agent_execution_id: UUID | null;
  submission_id: UUID | null;
  attempt: number | null;
  asset_version_id: UUID;
  slot_key: string | null;
  kind: ResourceKind;
  metadata: Record<string, unknown>;
  created_at: ISODateTime | null;
  asset: VersionBrief;
  project_id: UUID;
  run_title: string | null;
  node_id: string | null;
  task_id: UUID | null;
  task_number: number | null;
  task_title: string | null;
  task_status: string | null;
  /** null for resources of a tool run (not a collaboration instance). */
  execution_mode: ExecutionMode | null;
  task_acceptance: TaskAcceptance;
}

export interface ResourceFacets {
  /** All six kinds, 0 when nothing matches; ignores the kind filter, keeps the others. */
  by_kind: Record<ResourceKind, number>;
}

export interface ResourcePage {
  items: WorkflowResourceItem[];
  total: number;
  limit: number;
  offset: number;
  facets: ResourceFacets;
}

export const RESOURCE_PAGE_DEFAULT = 50;
export const RESOURCE_PAGE_MAX = 100;

export interface ProjectResourceQuery {
  q?: string;
  run_id?: UUID | '';
  task_id?: UUID | '';
  node_id?: string;
  asset_version_id?: UUID | '';
  kind?: ResourceKind | '';
  asset_type?: AssetType | '';
  availability?: AssetAvailability | '';
  publish_state?: PublishState | '';
  task_status?: string;
  limit?: number;
  offset?: number;
}

export interface RunResourceQuery {
  node_id?: string;
  kind?: ResourceKind | '';
  asset_type?: AssetType | '';
  limit?: number;
  offset?: number;
}

/** Query string without empty values; limit / offset only when given. */
function cleanQuery(q: Record<string, string | number | undefined | null>): Record<string, string | number | undefined> {
  const out: Record<string, string | number | undefined> = {};
  for (const [k, v] of Object.entries(q)) if (v !== undefined && v !== null && v !== '') out[k] = v;
  return out;
}

/** GET /projects/{id}/workflow-resources — server-side filters, paging and facets (C10). */
export const listProjectResources = (projectId: UUID, q: ProjectResourceQuery = {}, o: Opt = {}) =>
  request<ResourcePage>(`/projects/${enc(projectId)}/workflow-resources`, {
    query: cleanQuery({
      q: q.q?.trim().slice(0, 200),
      run_id: q.run_id,
      task_id: q.task_id,
      node_id: q.node_id,
      asset_version_id: q.asset_version_id,
      kind: q.kind,
      asset_type: q.asset_type,
      availability: q.availability,
      publish_state: q.publish_state,
      task_status: q.task_status,
      limit: q.limit,
      offset: q.offset,
    }),
    signal: o.signal,
  });

/** GET /runs/{id}/resources — resources of one instance (C08). */
export const listRunResources = (runId: UUID, q: RunResourceQuery = {}, o: Opt = {}) =>
  request<ResourcePage>(`/runs/${enc(runId)}/resources`, {
    query: cleanQuery({ node_id: q.node_id, kind: q.kind, asset_type: q.asset_type, limit: q.limit, offset: q.offset }),
    signal: o.signal,
  });

/** Six-kind facet row with every kind present (older servers may omit some). */
export function facetsByKind(f: ResourceFacets | undefined | null): Record<ResourceKind, number> {
  const out = {} as Record<ResourceKind, number>;
  for (const k of RESOURCE_KINDS) out[k] = Number(f?.by_kind?.[k] ?? 0) || 0;
  return out;
}

/**
 * The acceptance a resource shows for *this* task. `invalidated` comes from the server (a superseded
 * formal output); a formal / superseded row whose approval is not current also reads as invalidated.
 * Other kinds (candidate, evidence, input, failed) only describe their submission and are never shown
 * as invalidated. Without a kind (older callers) a non-current approval stays on the safe side.
 */
export function effectiveAcceptance(a: TaskAcceptance | null | undefined, kind?: ResourceKind | string | null): AcceptanceStatus {
  if (!a) return 'not_applicable';
  if (a.status === 'invalidated') return 'invalidated';
  if (a.status === 'approved' && !a.current) {
    if (kind == null || kind === 'formal' || kind === 'superseded') return 'invalidated';
    return 'approved';
  }
  return a.status;
}

export const isResourceKind = (v: string | null | undefined): v is ResourceKind => !!v && (RESOURCE_KINDS as readonly string[]).includes(v);
export const isExecutionMode = (v: string | null | undefined): v is ExecutionMode => !!v && (EXECUTION_MODES as readonly string[]).includes(v);

// Asset catalogue API (server/app/api/assets.py, services/assets.py).
// Types mirror `asset_out`, `version_out` and the usages view exactly; the
// catalogue only indexes the specialised engineering rows, it never replaces them.

import { request } from './client';
import type { ApprovalRequest } from './approvals';
import type { KnowledgeItemRef, KnowledgeUsage } from './knowledge';
import type { WorkflowResourceItem } from './resources';
import type { ApprovalDecision, ApprovalStage, ISODateTime, UUID } from './types';

type Opt = { signal?: AbortSignal };
const enc = encodeURIComponent;

/** Every type the server registers (services/assets.KIND_FIELD); `engineering_spec` is in the server enum but never registered. */
export const ASSET_TYPES = [
  'part_model',
  'drawing',
  'process_plan',
  'quote',
  'document',
  'intake',
  'process_module',
  'assembly_model',
  'assembly_drawing',
  'drawing_document',
  'delivery_manifest',
] as const;
export type AssetType = (typeof ASSET_TYPES)[number];

/** Source labels the server keeps on versions (plus read-time `from_failed_run`). */
export const ASSET_LABELS = ['simulated', 'estimated', 'imported', 'mesh_reference', 'from_failed_run'] as const;
export type KnownAssetLabel = (typeof ASSET_LABELS)[number];

export type AssetAvailability = 'building' | 'ready' | 'failed';
export const PUBLISH_STATES = ['draft', 'published', 'deprecated'] as const;
export type PublishState = (typeof PUBLISH_STATES)[number];

/** Specialised-row kind in `target.kind` (approval subject names; documents are file versions). */
export type AssetTargetKind =
  | 'model_revision'
  | 'drawing'
  | 'process_plan'
  | 'quote'
  | 'file_version'
  | 'intake'
  | 'process_module'
  | 'assembly_revision'
  | 'assembly_drawing'
  | 'drawing_document_version'
  | 'delivery_manifest';

/** One dependency edge, seen from the version being viewed. */
export interface AssetEdge {
  version_id: UUID;
  /** derived_from / priced_from / imported_from / uploaded_as / extracted_from … */
  relation: string;
  asset_id?: UUID;
  asset_type?: AssetType;
  asset_name?: string | null;
  version_no?: number;
  label?: string;
  content_hash?: string;
  target_id?: UUID;
}

export interface EngineeringDecisionRef {
  decision: ApprovalDecision;
  comment: string;
  role: string;
  /** null when an independent review agent decided (full-auto collaboration). */
  user_id: UUID | null;
  actor_kind?: 'human' | 'agent';
  agent_id?: UUID | null;
  subject_hash: string;
  at: ISODateTime | null;
  /** Process modules: the closed review request the decision belongs to. */
  request_id?: UUID;
}

/** Engineering review state of the exact specialised row (null for documents). */
export interface EngineeringState {
  stage: ApprovalStage;
  /** Latest decision is an approval AND the content is unchanged since. */
  approved: boolean;
  decision: EngineeringDecisionRef | null;
  /** Process modules: an open review request on this version. */
  pending_request_id?: UUID | null;
}

/** Per-type summary of the specialised row (services/assets._summary). */
export interface PartModelSummary {
  rev_no: number;
  source: string;
  status: string;
  mesh_only: boolean;
  note: string | null;
  build_key: string | null;
  parent_id: UUID | null;
}
export interface DrawingSummary {
  kind: string;
  model_revision_id: UUID | null;
  rev_no: number | null;
  svg_sha: string | null;
  dxf_sha: string | null;
  views: number;
}
export interface ProcessPlanSummary {
  model_revision_id: UUID | null;
  intake_id: UUID | null;
  source: string | null;
  estimated: boolean;
  priceable: boolean;
  total_cycle_min: number | null;
  inputs_hash: string;
}
export interface QuoteSummary {
  process_plan_id: UUID;
  currency: string;
  estimated: boolean;
  first_tier: { qty: number | null; unit_inc_tax_fen: number | null };
  inputs_hash: string;
}
export interface DocumentSummary {
  file_id: UUID;
  version: number;
  original_name: string;
  kind: string | null;
  blob_sha256: string;
  note: string | null;
}
export interface IntakeSummary {
  method: string;
  file_version_id: UUID | null;
  estimated: boolean;
  material: string | null;
  bbox_mm: unknown;
}

export interface AssemblyModelSummary {
  assembly_id: UUID;
  rev_no: number;
  /** manual / import / ai / copy */
  source: string;
  status: string;
  execution_policy: string;
  check_status: string;
  parent_id: UUID | null;
}
export interface AssemblyDrawingSummary {
  assembly_id: UUID;
  assembly_revision_id: UUID;
  number: number;
  /** 1 = generated, +1 per layout overlay a person saved. */
  version: number;
  parent_id: UUID | null;
  execution_policy: string;
  completeness: string;
  check_status: string;
  pages: number;
}
export interface DrawingDocumentSummary {
  document_id: UUID;
  version_no: number;
  /** empty / text / file / image / edit / restore / calibrate */
  origin: string;
  execution_policy: string;
  check_status: string;
  entities: number;
}
export interface DeliveryManifestSummary {
  number: number;
  completeness: string;
  execution_policy: string;
  check_status: string;
  assembly_revision_id: UUID | null;
  files: number;
  missing: number;
}

export interface AssetTarget {
  kind: AssetTargetKind;
  id: UUID;
  part_id: UUID | null;
  /** Shape depends on the asset type; read through the helpers in features/assets. */
  summary: Record<string, unknown>;
}

export interface AssetVersion {
  id: UUID;
  asset_id: UUID;
  asset_type: AssetType;
  asset_name: string;
  project_id: UUID;
  version_no: number;
  /** Human label: R3 / D2 / P1 / Q1 / v2 / I1. */
  label: string;
  content_hash: string;
  availability: AssetAvailability;
  publish_state: PublishState;
  labels: string[];
  parent_version_id: UUID | null;
  created_at: ISODateTime | null;
  created_by: UUID | null;
  node_run_id: UUID | null;
  run_id: UUID | null;
  target: AssetTarget;
  engineering: EngineeringState | null;
  /** Outgoing edges (what this version rests on). */
  dependencies?: AssetEdge[];
  /** Incoming edges (what rests on this version). */
  dependents?: AssetEdge[];
}

export interface Asset {
  id: UUID;
  project_id: UUID;
  type: AssetType;
  name: string;
  part_id: UUID | null;
  /** The asset's part as registered (documents: the File's part may have been relinked since — read the File). */
  part?: { id: UUID; number: string; name: string } | null;
  file_id: UUID | null;
  natural_key: string;
  source_asset_id: UUID | null;
  owner_id: UUID | null;
  latest_version: AssetVersion | null;
  version_count: number;
  created_at: ISODateTime | null;
  archived_at: ISODateTime | null;
}

export interface AssetListQuery {
  type?: AssetType | '';
  q?: string;
  label?: string;
  part_id?: UUID | '';
  include_archived?: boolean;
}

interface TaskRef {
  task_id: UUID;
  task_number: number;
  task_title: string;
  task_status: string;
}

interface RunRef {
  run_id: UUID;
  run_title: string | null;
  run_status: string;
  task_id: UUID | null;
  created_at: ISODateTime | null;
}

export interface AssetUsages {
  version_id: UUID;
  baselines: (TaskRef & { snapshot_id: UUID; snapshot_seq: number; slot_key: string; current: boolean; adopted_at: ISODateTime | null })[];
  deliverables: (TaskRef & { id: UUID; criterion_ids: UUID[]; note: string; selected_by: UUID; selected_at: ISODateTime | null })[];
  submissions: (TaskRef & {
    submission_id: UUID;
    seq: number;
    status: string;
    item_id: UUID;
    criterion_id: UUID | null;
    content_hash: string;
    submitted_at: ISODateTime | null;
  })[];
  run_inputs: (RunRef & { port_key: string; content_hash: string | null })[];
  run_outputs: (RunRef & { node_run_id: UUID; attempt: number; output_key: string })[];
  approval_requests: {
    id: UUID;
    kind: string;
    stage: string | null;
    status: string;
    title: string;
    run_id: UUID | null;
    task_id: UUID | null;
    created_at: ISODateTime | null;
  }[];
  // ─── C11 (design §4): collaboration relations, each with its own paging. Absent on older servers.
  /** Process-resource rows of this exact version (C10 item shape). */
  workflow_resources?: WorkflowResourceItem[];
  /** Knowledge items pinned to this exact version. */
  knowledge_items?: KnowledgeItemRef[];
  /** Reference / adoption records that used this exact version. */
  knowledge_usages?: KnowledgeUsage[];
  pagination?: Partial<Record<UsageGroup, UsagePagination>>;
}

export const USAGE_RELATIONS = ['all', 'workflow_resource', 'knowledge_item', 'knowledge_usage'] as const;
export type UsageRelation = (typeof USAGE_RELATIONS)[number];
export type UsageGroup = 'workflow_resources' | 'knowledge_items' | 'knowledge_usages';
/** Array name → relation parameter that pages it. */
export const USAGE_GROUP_RELATION: Record<UsageGroup, Exclude<UsageRelation, 'all'>> = {
  workflow_resources: 'workflow_resource',
  knowledge_items: 'knowledge_item',
  knowledge_usages: 'knowledge_usage',
};

export interface UsagePagination {
  total: number;
  limit: number;
  offset: number;
}

export interface UsagesQuery {
  /** Default all: the three new arrays share one paging window; a single relation pages only that array. */
  relation?: UsageRelation;
  /** 1–100, default 50. */
  limit?: number;
  offset?: number;
}

/** GET /projects/{id}/assets — label filters on the latest version. */
export const listProjectAssets = (projectId: UUID, q: AssetListQuery = {}, o: Opt = {}) =>
  request<Asset[]>(`/projects/${enc(projectId)}/assets`, {
    query: { type: q.type || undefined, q: q.q || undefined, label: q.label || undefined, part_id: q.part_id || undefined, include_archived: q.include_archived || undefined },
    signal: o.signal,
  });

export const getAsset = (assetId: UUID, o: Opt = {}) => request<Asset>(`/assets/${enc(assetId)}`, { signal: o.signal });

/** GET /assets/{id}/versions — newest first, with edges. */
export const listAssetVersions = (assetId: UUID, o: Opt = {}) => request<AssetVersion[]>(`/assets/${enc(assetId)}/versions`, { signal: o.signal });

export const getAssetVersion = (versionId: UUID, o: Opt = {}) => request<AssetVersion>(`/asset-versions/${enc(versionId)}`, { signal: o.signal });

/** GET /asset-versions/{id}/usages?relation=&limit=&offset= — the six legacy arrays plus the paged C11 relations. */
export const getAssetVersionUsages = (versionId: UUID, q: UsagesQuery = {}, o: Opt = {}) =>
  request<AssetUsages>(`/asset-versions/${enc(versionId)}/usages`, {
    query: { relation: q.relation && q.relation !== 'all' ? q.relation : undefined, limit: q.limit, offset: q.offset || undefined },
    signal: o.signal,
  });

/**
 * POST /asset-versions/{id}/publish — design role. 409 asset_not_ready (not a
 * ready version) / approval_required (engineering types need a valid approval
 * of this exact version, details.stage).
 */
export const publishAssetVersion = (versionId: UUID, state: Exclude<PublishState, 'draft'>, o: Opt = {}) =>
  request<AssetVersion>(`/asset-versions/${enc(versionId)}/publish`, { method: 'POST', body: { state }, signal: o.signal });

// ─── process modules (contract §3.5) ───────────────────────────────────────
// A reusable, documented process module: explicit applicability, parameters,
// steps and validation scope. Nothing in it is executed. Each version is an
// immutable row; versions of one module share one asset (natural key module:<root>).

export interface ModuleParameter {
  name: string;
  unit: string;
  default: number | string | null;
  min: number | null;
  max: number | null;
}

export interface ModuleStep {
  name: string;
  machine: string;
  description: string;
  minutes?: number | null;
  /** Operation id of the source plan the step was copied from (informative). */
  source_op_id?: string | null;
}

export interface ModuleApplicability {
  materials: string[];
  /** max = three positive numbers (mm); min optional. */
  size_range_mm: { max: number[]; min?: number[] | null };
  equipment: string[];
  prerequisites: string;
  limitations: string;
}

export interface ModuleValidation {
  scope: string;
  evidence: string;
  validated_plan_ids: UUID[];
}

export interface ProcessModuleInput {
  name: string;
  summary: string;
  applicability: ModuleApplicability;
  parameters: ModuleParameter[];
  steps: ModuleStep[];
  validation: ModuleValidation;
  /** The concrete process plan the module was distilled from (same project); it is never changed. */
  source_plan_id?: UUID | null;
  /** Indices of the source plan's operations the steps were copied from. */
  source_steps?: number[];
}

export interface ProcessModuleContent extends ProcessModuleInput {
  id: UUID;
  project_id: UUID;
  created_by: UUID | null;
  created_at: ISODateTime | null;
}

/** GET /process-module-versions/{vid}, normalised: module content + its asset version. */
export interface ProcessModuleVersion {
  module: ProcessModuleContent;
  version: AssetVersion | null;
}

type RawModuleVersion = Partial<ProcessModuleContent> & {
  module?: ProcessModuleContent;
  content?: ProcessModuleContent;
  version?: AssetVersion;
  version_out?: AssetVersion;
  asset_version?: AssetVersion;
};

/** Accepts {module, version} as well as the module fields with the version alongside. */
export function normalizeModuleVersion(raw: RawModuleVersion): ProcessModuleVersion {
  const version = raw.version ?? raw.version_out ?? raw.asset_version ?? null;
  const module = (raw.module ?? raw.content ?? raw) as ProcessModuleContent;
  return { module, version };
}

/** The asset version a module create / new-version call registered (tolerates {version} or a bare version_out). */
export function createdModuleVersion(raw: RawModuleVersion | AssetVersion): AssetVersion | null {
  const r = raw as RawModuleVersion & Partial<AssetVersion>;
  if (r.version || r.version_out || r.asset_version) return (r.version ?? r.version_out ?? r.asset_version) as AssetVersion;
  return r.asset_id && r.id ? (r as AssetVersion) : null;
}

/** POST /projects/{id}/process-modules — process role, active project. 422 module_incomplete (details.missing). */
export const createProcessModule = (projectId: UUID, body: ProcessModuleInput, o: Opt = {}) =>
  request<RawModuleVersion | AssetVersion>(`/projects/${enc(projectId)}/process-modules`, { method: 'POST', body, signal: o.signal });

/** POST /assets/{id}/module-versions — a new immutable version whose parent is the latest one. */
export const createModuleVersion = (assetId: UUID, body: ProcessModuleInput, o: Opt = {}) =>
  request<RawModuleVersion | AssetVersion>(`/assets/${enc(assetId)}/module-versions`, { method: 'POST', body, signal: o.signal });

export const getProcessModuleVersion = async (versionId: UUID, o: Opt = {}): Promise<ProcessModuleVersion> =>
  normalizeModuleVersion(await request<RawModuleVersion>(`/process-module-versions/${enc(versionId)}`, { signal: o.signal }));

/**
 * POST /asset-versions/{id}/request-review — process modules only: opens an
 * engineering (process) request on this exact version. 409 when one is already pending.
 */
export const requestAssetVersionReview = (versionId: UUID, body: { assignee_id?: UUID | null } = {}, o: Opt = {}) =>
  request<ApprovalRequest>(`/asset-versions/${enc(versionId)}/request-review`, { method: 'POST', body, signal: o.signal });

// Knowledge library API (design §4 C09; server/app/api/knowledge.py).
// A collection archives one completed collaboration run; each immutable
// manifest version lists items that point at exact asset versions. Using an
// item only records a relationship (reference) or proposes a baseline
// (adoption) — the target task is always accepted anew, nothing is inherited.

import { request } from './client';
import type { ActorOut, ExecutionMode, TaskAcceptance, VersionBrief } from './resources';
import type { ISODateTime, UUID } from './types';

type Opt = { signal?: AbortSignal };
const enc = encodeURIComponent;

export const INDEX_STATUSES = ['pending', 'indexing', 'ready', 'partial', 'failed'] as const;
export type IndexStatus = (typeof INDEX_STATUSES)[number];
export const ITEM_INDEX_STATUSES = ['pending', 'ready', 'failed', 'not_applicable'] as const;
export type ItemIndexStatus = (typeof ITEM_INDEX_STATUSES)[number];
export const ITEM_KINDS = ['formal', 'history', 'candidate', 'failed', 'evidence'] as const;
export type ItemKind = (typeof ITEM_KINDS)[number];
export const KNOWLEDGE_USAGES = ['reference', 'baseline'] as const;
export type KnowledgeUsageKind = (typeof KNOWLEDGE_USAGES)[number];
export const USAGE_CONFIRMATIONS = ['none', 'pending', 'confirmed', 'rejected'] as const;
export type UsageConfirmation = (typeof USAGE_CONFIRMATIONS)[number];
export const COLLECTION_STATUSES = ['active', 'deprecated'] as const;
export type CollectionStatus = (typeof COLLECTION_STATUSES)[number];

export interface ItemCounts {
  formal: number;
  history: number;
  candidate: number;
  failed: number;
  evidence: number;
}

/** One immutable manifest version of a collection. */
export interface KnowledgeVersionRef {
  id: UUID;
  version_no: number;
  manifest_hash: string;
  archived_at: ISODateTime | null;
  counts: ItemCounts;
}

/** `collection_out`. */
export interface KnowledgeCollection {
  id: UUID;
  project_id: UUID;
  run_id: UUID;
  run_title: string | null;
  title: string;
  /** Free-form applicability description written at archive time. */
  scope: Record<string, unknown>;
  status: CollectionStatus;
  current_version: KnowledgeVersionRef | null;
  index_status: IndexStatus;
  index_error: string | null;
  versions_count: number;
  usage_counts: { reference: number; baseline: number };
  created_at: ISODateTime | null;
  /** Row version for PATCH expected_version (assumed beyond §4: the table has `version`, collection_out does not list it). */
  row_version?: number;
}

/** `item_out`: one resource of the archived run, pinned to an exact asset version. */
export interface KnowledgeItem {
  id: UUID;
  collection_version_id: UUID;
  resource_id: UUID;
  asset_version_id: UUID;
  version: VersionBrief;
  content_hash: string;
  run_id: UUID;
  node_task_id: UUID | null;
  node_id: string | null;
  task_id: UUID | null;
  task_key: string | null;
  slot_key: string | null;
  kind: ItemKind;
  /** Default-adoptable: current formal output of a valid node. Reference stays allowed when false. */
  reusable: boolean;
  index_status: ItemIndexStatus;
  index_error: string | null;
  acceptance: TaskAcceptance;
}

/** `usage_out` (fields shared with the C11 knowledge_usages rows). */
export interface KnowledgeUsage {
  id: UUID;
  item_id: UUID;
  collection_version_id: UUID;
  asset_version_id?: UUID;
  content_hash?: string;
  usage: KnowledgeUsageKind;
  target_project_id: UUID;
  target_run_id: UUID | null;
  target_node_task_id: UUID | null;
  target_task_id: UUID | null;
  slot_key: string | null;
  baseline_snapshot_id: UUID | null;
  used_by: ActorOut;
  reason: string;
  confirmation: UsageConfirmation;
  confirmed_by_user_id?: UUID | null;
  confirmed_at?: ISODateTime | null;
  created_at: ISODateTime | null;
  /** Assumed beyond §4: whether the current user is the confirming reviewer. Absent → role-based fallback. */
  can_confirm?: boolean;
  /** Assumed beyond §4: brief of the target task when the server denormalises it. */
  target_task?: { id: UUID; number: number; key?: string; title: string } | null;
}

/** C11 `knowledge_items` row of an asset version's usages. */
export interface KnowledgeItemRef {
  item_id: UUID;
  collection_id: UUID;
  collection_version_id: UUID;
  resource_id: UUID;
  asset_version_id: UUID;
  run_id: UUID;
  task_id: UUID | null;
  reusable: boolean;
  /** Assumed beyond §4 (shown when present). */
  collection_title?: string | null;
  kind?: ItemKind;
}

export interface KnowledgeCollectionDetail extends KnowledgeCollection {
  version: KnowledgeVersionRef & { manifest: Record<string, unknown> };
  items: KnowledgeItem[];
  usages: KnowledgeUsage[];
  versions: KnowledgeVersionRef[];
}

export interface KnowledgeListQuery {
  q?: string;
  reusable?: boolean;
  status?: CollectionStatus | '';
  limit?: number;
  offset?: number;
}

export interface KnowledgeList {
  items: KnowledgeCollection[];
  total: number;
}

/** GET /projects/{id}/knowledge — collections, newest first. */
export const listKnowledge = (projectId: UUID, q: KnowledgeListQuery = {}, o: Opt = {}) =>
  request<KnowledgeList>(`/projects/${enc(projectId)}/knowledge`, {
    query: {
      q: q.q?.trim() || undefined,
      reusable: q.reusable ? 'true' : undefined,
      status: q.status || undefined,
      limit: q.limit,
      offset: q.offset,
    },
    signal: o.signal,
  });

/** GET /knowledge/{id}?version_id= — one manifest version (default: current) with items, usages and the version list. */
export const getKnowledgeCollection = (collectionId: UUID, q: { version_id?: UUID | '' } = {}, o: Opt = {}) =>
  request<KnowledgeCollectionDetail>(`/knowledge/${enc(collectionId)}`, { query: { version_id: q.version_id || undefined }, signal: o.signal });

export interface KnowledgeCollectionPatch {
  expected_version: number;
  title?: string;
  scope?: Record<string, unknown>;
  status?: CollectionStatus;
}

/** PATCH /knowledge/{id} — manage; 409 version_conflict. */
export const patchKnowledgeCollection = (collectionId: UUID, body: KnowledgeCollectionPatch, o: Opt = {}) =>
  request<KnowledgeCollection>(`/knowledge/${enc(collectionId)}`, { method: 'PATCH', body, signal: o.signal });

/** POST /knowledge/{id}/reindex — queues indexing; manifest, hashes and acceptance stay unchanged. */
export const reindexKnowledgeCollection = (collectionId: UUID, o: Opt = {}) =>
  request<KnowledgeCollection>(`/knowledge/${enc(collectionId)}/reindex`, { method: 'POST', body: {}, signal: o.signal });

/** POST /runs/{id}/knowledge/archive — idempotent manual archive retry; 409 run_not_completed. */
export const archiveRunKnowledge = (runId: UUID, o: Opt = {}) =>
  request<KnowledgeCollection>(`/runs/${enc(runId)}/knowledge/archive`, { method: 'POST', body: {}, signal: o.signal });

export type UseTarget = { task_id: UUID } | { run_id: UUID; node_id: string };

export interface UseItemIn {
  usage: KnowledgeUsageKind;
  target: UseTarget;
  /** Required for baseline adoption. */
  slot_key?: string;
  reason: string;
}

/**
 * POST /knowledge/items/{id}/use — reference records a relationship only;
 * baseline on a semi_auto target waits for the node's human reviewer
 * (confirmation=pending). 409 item_not_reusable (adoption of a non-reusable item).
 */
export const useKnowledgeItem = (itemId: UUID, body: UseItemIn, o: Opt = {}) =>
  request<KnowledgeUsage>(`/knowledge/items/${enc(itemId)}/use`, { method: 'POST', body, signal: o.signal });

/** POST /knowledge-usages/{id}/confirm — the target node's human reviewer confirms or rejects a pending adoption. */
export const confirmKnowledgeUsage = (usageId: UUID, body: { decision: 'confirmed' | 'rejected'; comment: string }, o: Opt = {}) =>
  request<KnowledgeUsage>(`/knowledge-usages/${enc(usageId)}/confirm`, { method: 'POST', body, signal: o.signal });

// ─── target task context ────────────────────────────────────────────────────

/** `task_detail.collaboration` (design §4 "Integration with existing interfaces"): the node a task belongs to, if any. */
export interface TaskCollaborationRef {
  run_id: UUID;
  node_id: string;
  execution_mode: ExecutionMode;
  phase: string;
}

/** Reads the optional collaboration block of a task detail without widening api/pm.ts. */
export function collaborationOf(task: unknown): TaskCollaborationRef | null {
  const c = (task as { collaboration?: unknown } | null | undefined)?.collaboration;
  if (!c || typeof c !== 'object') return null;
  const r = c as Partial<TaskCollaborationRef>;
  if (typeof r.run_id !== 'string' || typeof r.node_id !== 'string') return null;
  if (r.execution_mode !== 'full_auto' && r.execution_mode !== 'semi_auto') return null;
  return { run_id: r.run_id, node_id: r.node_id, execution_mode: r.execution_mode, phase: typeof r.phase === 'string' ? r.phase : '' };
}

/** Human-readable applicability of a collection: scope.description / scope.text / scope.summary, else the string values joined. */
export function scopeText(scope: Record<string, unknown> | null | undefined): string {
  if (!scope || typeof scope !== 'object') return '';
  for (const k of ['description', 'text', 'summary', 'label']) {
    const v = scope[k];
    if (typeof v === 'string' && v.trim()) return v.trim();
  }
  const parts: string[] = [];
  for (const [k, v] of Object.entries(scope)) {
    if (k.endsWith('_id')) continue; // ids are not a description
    if (typeof v === 'string' && v.trim()) parts.push(v.trim());
    else if (Array.isArray(v)) parts.push(...v.filter((x): x is string => typeof x === 'string'));
  }
  return parts.join(' · ');
}

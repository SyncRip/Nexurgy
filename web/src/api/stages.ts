// Project stages (milestones), stage reviews, readiness and project creation
// with a stage list — contract .trellis/tasks/archive/2026-10/10-05-templates-stages/design.md
// §3.1–§3.3. Types mirror server/app/api/{milestone_reviews,projects,overview}.py
// and services/stages.py; when one of those builders changes, change it here.
//
// Milestone / lifecycle types that the overview also returns live in api/pm.ts.

import { request } from './client';
import type { RequestDecision } from './approvals';
import type { AssetVersion, Milestone } from './pm';
import type { ISODateTime, Project, ProjectCreate, UUID } from './types';

type Opt = { signal?: AbortSignal };
const enc = encodeURIComponent;

export const MILESTONE_STATUSES = ['open', 'in_review', 'accepted'] as const;
export type MilestoneStatus = (typeof MILESTONE_STATUSES)[number];
export const REVIEW_STATUSES = ['pending', 'approved', 'rejected', 'withdrawn', 'superseded'] as const;
export type ReviewStatus = (typeof REVIEW_STATUSES)[number];

// ─── readiness ─────────────────────────────────────────────────────────────

/** One reason a stage review cannot start (or be approved) now. Extra fields depend on the code. */
export interface StageBlocker {
  code: string;
  /** Server text (English); the UI shows a translation when it knows the code. */
  message: string;
  task_id?: UUID;
  key?: string;
  title?: string;
  asset_type?: string;
  asset_types?: string[];
  count?: number;
  n?: number;
  [k: string]: unknown;
}

/** One task of the stage as the readiness check sees it. */
export interface ReadinessTask {
  task_id?: UUID;
  id?: UUID;
  key?: string;
  number?: number;
  title: string;
  status: string;
  required?: boolean;
  submission_id?: UUID | null;
  submission_seq?: number | null;
  seq?: number | null;
  snapshot_hash?: string | null;
  /** Re-check of the accepted submission now (null = none accepted). */
  checks_ok?: boolean | null;
  problems?: StageBlocker[];
}

/** An asset version the server suggests as an extra stage deliverable. */
export interface SuggestedItem {
  asset_version_id?: UUID;
  id?: UUID;
  asset_id?: UUID;
  asset_type?: string;
  asset_name?: string | null;
  label?: string;
  labels?: string[];
  reason?: string;
  version?: AssetVersion;
}

export interface Readiness {
  milestone_id?: UUID;
  ready: boolean;
  blockers: StageBlocker[];
  tasks: ReadinessTask[];
  suggested_items: SuggestedItem[];
  /** Guard groups (stage_state / required_tasks / open_impacts / required_asset_types / not_empty). */
  checks?: ReviewCheck[];
}

/** Readiness now; `extraIds` are evaluated as extra stage deliverables (repeated query parameter). */
export const getMilestoneReadiness = (milestoneId: UUID, o: Opt & { extraIds?: UUID[] } = {}) => {
  const qs = (o.extraIds ?? []).map((id) => `extra_asset_version_ids=${enc(id)}`).join('&');
  return request<Readiness>(`/milestones/${enc(milestoneId)}/readiness${qs ? `?${qs}` : ''}`, { signal: o.signal });
};

/** Blocker codes an extra stage deliverable can resolve (the rest need work on tasks or the project). */
export const EXTRA_FIXABLE_BLOCKERS: ReadonlySet<string> = new Set(['missing_asset_type', 'stage_empty']);

// ─── milestone editing ─────────────────────────────────────────────────────

export interface MilestonePatch {
  name?: string;
  description?: string;
  /** Not allowed on an accepted stage (reopen first). */
  requirements?: { required_asset_types: string[] };
  expected_version: number;
}

export const patchMilestone = (milestoneId: UUID, body: MilestonePatch, o: Opt = {}) =>
  request<Milestone>(`/milestones/${enc(milestoneId)}`, { method: 'PATCH', body, signal: o.signal });

/** Only a stage without tasks and without reviews can be deleted. */
export const deleteMilestone = (milestoneId: UUID, o: Opt = {}) => request<void>(`/milestones/${enc(milestoneId)}`, { method: 'DELETE', signal: o.signal });

// ─── stage reviews ─────────────────────────────────────────────────────────

/** Frozen review content (services/stages snapshot). */
export interface ReviewSnapshotTask {
  task_id: UUID;
  key: string;
  title: string;
  required?: boolean;
  submission_id: UUID;
  seq: number;
  snapshot_hash: string;
}

export interface ReviewSnapshotExtra {
  asset_version_id: UUID;
  label: string;
  content_hash: string;
  asset_id?: UUID;
  asset_type?: string;
  asset_name?: string | null;
  labels?: string[];
}

/** One guard result frozen with the review; shape kept open on purpose. */
export interface ReviewCheck {
  code?: string;
  ok?: boolean;
  message?: string;
  [k: string]: unknown;
}

export interface ReviewSnapshot {
  schema?: number;
  milestone: { id: UUID; name: string; seq: number };
  requirements?: { required_asset_types?: string[] };
  tasks: ReviewSnapshotTask[];
  extras: ReviewSnapshotExtra[];
  checks: ReviewCheck[];
  note?: string;
}

/** Short version info (services/stages._version_info); `missing` when the version is gone. */
export interface VersionInfo {
  asset_version_id: UUID;
  missing?: boolean;
  asset_id?: UUID;
  asset_type?: string | null;
  asset_name?: string | null;
  label?: string;
  content_hash?: string;
  availability?: string;
  publish_state?: string;
}

export interface MilestoneReviewItem {
  id: UUID;
  kind: 'task_submission' | 'extra_asset';
  task_id: UUID | null;
  submission_id: UUID | null;
  asset_version_id: UUID | null;
  content_hash: string;
  version?: VersionInfo | null;
}

/** milestone_review_out. `snapshot` / `items` / `decisions` on detail calls. */
export interface MilestoneReview {
  id: UUID;
  project_id: UUID;
  milestone_id: UUID;
  seq: number;
  status: ReviewStatus;
  snapshot_hash: string;
  note: string;
  reviewer_id: UUID | null;
  reviewer?: { id: UUID; name: string } | null;
  submitted_by: UUID;
  submitted_by_name?: string | null;
  submitted_at: ISODateTime | null;
  decided_at: ISODateTime | null;
  /** The milestone_review approval request. */
  request_id?: UUID | null;
  request_status?: string | null;
  /** Detail calls: the caller may withdraw (pending, submitter or owner). */
  can_withdraw?: boolean;
  milestone?: { id: UUID; name: string; seq: number; status?: string } | null;
  snapshot?: ReviewSnapshot;
  items?: MilestoneReviewItem[];
  decisions?: RequestDecision[];
}

export interface ReviewCreate {
  reviewer_id: UUID;
  note: string;
  extra_asset_version_ids: UUID[];
  /** The milestone's version. */
  expected_version: number;
}

export const listMilestoneReviews = (milestoneId: UUID, o: Opt = {}) =>
  request<MilestoneReview[]>(`/milestones/${enc(milestoneId)}/reviews`, { signal: o.signal });

export const getMilestoneReview = (reviewId: UUID, o: Opt = {}) => request<MilestoneReview>(`/milestone-reviews/${enc(reviewId)}`, { signal: o.signal });

/** Manage action. 409 with details.blockers when the stage is not ready. */
export const createMilestoneReview = (milestoneId: UUID, body: ReviewCreate, o: Opt = {}) =>
  request<MilestoneReview>(`/milestones/${enc(milestoneId)}/reviews`, { method: 'POST', body, signal: o.signal });

/** Submitter or owner; closes the request and returns the stage to open. */
export const withdrawMilestoneReview = (reviewId: UUID, reason: string, o: Opt = {}) =>
  request<MilestoneReview>(`/milestone-reviews/${enc(reviewId)}/withdraw`, { method: 'POST', body: { reason }, signal: o.signal });

// ─── project creation with a stage list ────────────────────────────────────

export interface ProjectCreateWithStages extends ProjectCreate {
  /** Custom ordered stage names. */
  milestones?: string[];
  /** Server default list (requirements / design review / process assessment / quote delivery). */
  stage_template?: 'default';
  /** Language of the default stage names (zh / en). */
  lang?: string;
}

/** POST /projects with a stage list; `lang` (the UI language) names the default stages. */
export const createProjectWithStages = (body: ProjectCreateWithStages, o: Opt = {}) =>
  request<Project>('/projects', { method: 'POST', body, signal: o.signal });

// ─── helpers ───────────────────────────────────────────────────────────────

/** Blockers may arrive as a list of {code, message} or (older shape) as a dict of counts. */
export function normalizeBlockers(raw: unknown): StageBlocker[] {
  if (Array.isArray(raw)) {
    return raw
      .filter((b): b is Record<string, unknown> => !!b && typeof b === 'object')
      .map((b) => ({ ...b, code: String(b.code ?? 'unknown'), message: String(b.message ?? b.code ?? '') }) as StageBlocker);
  }
  if (raw && typeof raw === 'object') {
    const out: StageBlocker[] = [];
    for (const [code, v] of Object.entries(raw as Record<string, unknown>)) {
      if (v === 0 || v === false || v === null || v === undefined || (Array.isArray(v) && !v.length)) continue;
      if (typeof v === 'number') out.push({ code, message: `${code}: ${v}`, count: v });
      else if (Array.isArray(v)) out.push({ code, message: `${code}: ${v.join(', ')}`, names: v.map(String), count: v.length });
      else out.push({ code, message: String(v) });
    }
    return out;
  }
  return [];
}

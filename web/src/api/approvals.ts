// Approval centre API (server/app/api/approval_requests.py,
// services/approval_requests.py, services/submissions.py). One inbox for
// engineering reviews (opened by human_review run steps, or directly on a
// process module version), task acceptance (opened by a submission) and stage
// reviews (opened by a milestone review). Decisions are append-only and idempotent.

import type { ActorOut,ExecutionMode } from './agents';
import type { AssetVersion,ProcessModuleContent } from './assets';
import { request } from './client';
import type { CriterionDecision,CriterionResult,NodeTaskPhase } from './collaboration';
import type { MilestoneReview,Readiness } from './stages';
import type { ApprovalDecision,ApprovalStage,ApprovalSubjectType,ISODateTime,StaleReason,UUID } from './types';

type Opt = { signal?: AbortSignal };
const enc = encodeURIComponent;

export const REQUEST_KINDS = ['engineering', 'task_acceptance', 'milestone_review'] as const;
export type RequestKind = (typeof REQUEST_KINDS)[number];
export const REQUEST_STATUSES = ['pending', 'approved', 'rejected', 'withdrawn', 'superseded'] as const;
export type RequestStatus = (typeof REQUEST_STATUSES)[number];
export const REQUEST_SCOPES = ['mine', 'created', 'all'] as const;
export type RequestScope = (typeof REQUEST_SCOPES)[number];

export interface RequestTaskRef {
  id: UUID;
  number: number;
  /** "T-12" */
  key: string;
  title: string;
  status: string;
}

/** `request_out` */
export interface ApprovalRequest {
  id: UUID;
  project_id: UUID;
  kind: RequestKind;
  /** design / process / quote for engineering requests; null for task acceptance and stage reviews. */
  stage: ApprovalStage | null;
  status: RequestStatus;
  title: string;
  /** NULL = any member whose role may decide the stage (engineering). */
  assignee_id: UUID | null;
  assignee: { id: UUID; name: string } | null;
  task: RequestTaskRef | null;
  run_id: UUID | null;
  node_run_id: UUID | null;
  node_id: string | null;
  node_status: string | null;
  asset_version_id: UUID | null;
  submission_id: UUID | null;
  /** Stage review requests (kind milestone_review). Absent on older servers. */
  milestone_review_id?: UUID | null;
  target_hash: string;
  policy_version: string | null;
  version: number;
  created_by: UUID;
  created_by_name: string | null;
  created_at: ISODateTime | null;
  due_at: ISODateTime | null;
  closed_at: ISODateTime | null;
  /** Server-side check: pending, role / assignee / self-review / project state allow it. */
  can_decide: boolean;
  /** Server-side reassignment permission, including feature and frozen-reviewer rules. */
  can_reassign?: boolean;
  /** Collaboration node this acceptance belongs to (design §4 "Integration with existing interfaces"); null / absent for ordinary requests. */
  node_task?: RequestNodeTask | null;
  /** human = decided over HTTP by the assigned person; agent = the independent reviewing agent decides (full_auto). */
  decision_mode?: 'human' | 'agent';
}

/** request_out.node_task */
export interface RequestNodeTask {
  run_id: UUID;
  node_id: string;
  execution_mode: ExecutionMode;
  phase: NodeTaskPhase;
}

export function isCollaborationAcceptance(r: Pick<ApprovalRequest, 'kind' | 'node_task'>): r is ApprovalRequest & { node_task: RequestNodeTask } {
  return r.kind === 'task_acceptance' && !!r.node_task;
}

export interface RequestDecision {
  id: UUID;
  decision: ApprovalDecision;
  comment: string;
  actor_id: UUID | null;
  actor_name: string | null;
  role: string;
  target_hash: string;
  created_at: ISODateTime | null;
  /** The deciding actor (person or agent); absent on older servers. */
  actor?: ActorOut | null;
  /** Per-criterion verdicts of a collaboration acceptance; [] otherwise. */
  criterion_decisions?: CriterionDecision[];
}

/** C05 CriterionDecisionIn: reason 1–4000 chars, at most 100 evidence versions, at most 50 items per decision. */
export interface CriterionDecisionIn {
  criterion_id: UUID;
  result: CriterionResult;
  reason: string;
  evidence_asset_version_ids: UUID[];
}

export interface DecisionIn {
  decision: ApprovalDecision;
  comment: string;
  expected_hash: string;
  /** Required for collaboration acceptances (every required criterion); omitted for other requests. */
  criterion_decisions?: CriterionDecisionIn[];
}

// ─── checks (services/submissions.run_checks) ───────────────────────────────

export interface CheckProblem {
  code: string;
  message: string;
  asset_version_id?: UUID;
  label?: string;
  stage?: string;
  slot?: string;
  /** Consistency issues carry the traced sources and the expected design versions. */
  source_labels?: string[];
  expected_labels?: string[];
  blocking?: boolean;
}

export interface CriterionCheck {
  criterion_id: UUID;
  seq: number | null;
  description: string;
  required: boolean;
  manual_required: boolean;
  evidence: UUID[];
  problems: CheckProblem[];
  ok: boolean;
  blocking: boolean;
}

export interface ItemCheck {
  asset_version_id: UUID;
  label: string;
  asset_type: string | null;
  labels: string[];
  problems: CheckProblem[];
  warnings: CheckProblem[];
  ok: boolean;
}

export interface CheckReport {
  ok: boolean;
  criteria: CriterionCheck[];
  items: ItemCheck[];
  policy_version: string;
  snapshot_tampered?: boolean;
}

// ─── frozen submission snapshot ─────────────────────────────────────────────

export interface RuleSpec {
  asset_types?: string[];
  require_approval_stage?: ApprovalStage | null;
  allow_estimated?: boolean;
  allow_simulated?: boolean;
  allow_unverified?: boolean;
  consistent_with_slot?: string | null;
  min_count?: number;
}

export interface FrozenCriterion {
  id: UUID;
  task_id: UUID;
  seq: number;
  description: string;
  required: boolean;
  allowed_types: string[];
  rule_spec: RuleSpec;
  manual_required: boolean;
  version: number;
}

export interface FrozenItem {
  asset_version_id: UUID;
  asset_id: UUID;
  asset_type: string;
  label: string;
  version_no: number;
  content_hash: string;
  availability: string;
  labels: string[];
  criterion_ids: UUID[];
  note: string;
  /** Upstream chain (drawing → model, quote → plan → model) as version ids. */
  sources: UUID[];
  engineering: { stage: ApprovalStage; approved: boolean } | null;
}

export interface SubmissionSnapshot {
  schema: number;
  policy_version: string;
  task: { id: UUID; number: number; title: string; objective: string; version: number };
  baseline: { id: UUID; seq: number; hash: string; items: Record<string, UUID> } | null;
  criteria: FrozenCriterion[];
  items: FrozenItem[];
  checks: CheckReport;
  note: string;
  submitted_by: UUID;
}

export interface Submission {
  id: UUID;
  task_id: UUID;
  seq: number;
  baseline_snapshot_id: UUID | null;
  snapshot_hash: string;
  note: string;
  status: 'pending' | 'approved' | 'rejected' | 'withdrawn';
  submitted_by: UUID;
  submitted_at: ISODateTime | null;
  decided_at: ISODateTime | null;
  request_id: UUID | null;
  checks_ok: boolean;
  snapshot?: SubmissionSnapshot;
  items?: { id: UUID; asset_version_id: UUID; criterion_id: UUID | null; content_hash: string; check_result: unknown }[];
}

/** `request_detail.target` — engineering or task acceptance fields. */
export interface RequestTarget {
  part?: { id: UUID; number: string; name: string };
  source_path?: string;
  // engineering
  asset_version?: AssetVersion | { id: UUID } | null;
  subject?: { type: string; id: UUID; current_hash: string; changed: boolean; approved_now: boolean } | { error: string };
  node?: { node_id: string; status: string; instructions: string; run_title: string | null; run_status: string };
  /** Direct engineering request on a process module version: the module content. */
  module?: ProcessModuleContent | null;
  // task acceptance
  submission?: Submission;
  /** Checks re-run now on the frozen inputs (pending requests only; null when the re-check failed). */
  current_checks?: CheckReport | null;
  // stage review (kind milestone_review)
  milestone_review?: MilestoneReview & { readiness?: Readiness | null };
  /** Current readiness of the stage (pending requests). */
  readiness?: Readiness | null;
}

export interface ApprovalSubject {
  subject_type: ApprovalSubjectType;
  subject_id: UUID;
  stage: ApprovalStage;
  current_hash: string;
  can_request: boolean;
  stale_reasons: StaleReason[];
  source_path: string;
}
type DirectSubject = { subject_type: ApprovalSubjectType; subject_id: UUID; stage: ApprovalStage };
export const getApprovalSubject = (partId: UUID, query: DirectSubject, o: Opt = {}) =>
  request<ApprovalSubject>(`/parts/${enc(partId)}/approval-subject`, { query, signal: o.signal });
export const listPartApprovalRequests = (partId: UUID, query: DirectSubject, o: Opt = {}) =>
  request<ApprovalRequest[]>(`/parts/${enc(partId)}/approval-requests`, { query: { ...query, status: 'pending' }, signal: o.signal });
export const createPartApprovalRequest = (partId: UUID, body: DirectSubject & { expected_hash: string; assignee_id?: UUID | null }, o: Opt = {}) =>
  request<ApprovalRequestDetail & { reused: boolean }>(`/parts/${enc(partId)}/approval-requests`, { method: 'POST', body, signal: o.signal });

/** Engineering request decided directly on an asset version (process module review), not from a run step. */
export function isDirectEngineering(r: Pick<ApprovalRequest, 'kind' | 'run_id' | 'node_run_id'>): boolean {
  return r.kind === 'engineering' && !r.run_id && !r.node_run_id;
}

export interface ApprovalRequestDetail extends ApprovalRequest {
  decisions: RequestDecision[];
  target: RequestTarget;
}

export interface RequestListQuery {
  scope?: RequestScope;
  project_id?: UUID;
  /** Comma-separated statuses or "all". The server defaults to pending. */
  status?: RequestStatus | 'all' | string;
  kind?: RequestKind | '';
  task_id?: UUID;
  limit?: number;
}

/** Details of the 409 subject_stale error (engineering decisions). */
export type StaleReasonsDetails = { reasons?: StaleReason[] };

export const listApprovalRequests = (q: RequestListQuery = {}, o: Opt = {}) =>
  request<ApprovalRequest[]>('/approval-requests', {
    query: { scope: q.scope, project_id: q.project_id, status: q.status, kind: q.kind || undefined, task_id: q.task_id, limit: q.limit },
    signal: o.signal,
  });

export const getApprovalRequest = (requestId: UUID, o: Opt = {}) =>
  request<ApprovalRequestDetail>(`/approval-requests/${enc(requestId)}`, { signal: o.signal });

/**
 * POST /approval-requests/{id}/decisions — `expected_hash` is the target hash
 * the reviewer saw. Repeating the decision that closed the request returns it
 * unchanged; another decision is 409 request_closed. Collaboration acceptances
 * (C05) also send `criterion_decisions`: 422 criteria_incomplete /
 * criteria_not_passed / evidence_unknown, 409 mode_forbids_human_decision.
 */
export const decideApprovalRequest = (requestId: UUID, body: DecisionIn, o: Opt = {}) =>
  request<ApprovalRequestDetail>(`/approval-requests/${enc(requestId)}/decisions`, { method: 'POST', body, signal: o.signal });

/** POST /approval-requests/{id}/reassign — project owner only. */
export const reassignApprovalRequest = (requestId: UUID, assigneeId: UUID, o: Opt = {}) =>
  request<ApprovalRequestDetail>(`/approval-requests/${enc(requestId)}/reassign`, { method: 'POST', body: { assignee_id: assigneeId }, signal: o.signal });

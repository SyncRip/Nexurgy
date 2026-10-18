import { runChoices } from './capabilities';
// Project-management layer API (docs/archive/project-task-asset-design.zh-CN.md, contract
// .trellis/tasks/archive/2026-10/10-05-project-task-asset/design.md §4). Types mirror the server
// dict builders they name: server/app/api/{projects,overview,tasks,workflows}.py,
// services/{tasks,submissions,approval_requests}.py and services/assets.py
// (`version_out`, `asset_out`). When one of those builders changes, change it here.
//
// Asset catalogue reads used by the tasks UI (version pickers) live here too, so
// this file does not depend on api/assets.ts.

import { request } from './client';
import type { ISODateTime, Project, RunSummary, UUID } from './types';

type Opt = { signal?: AbortSignal };
const enc = encodeURIComponent;

// ─── enums (server/app/models.py) ──────────────────────────────────────────

export const LIFECYCLES = ['draft', 'active', 'on_hold', 'completed', 'cancelled'] as const;
export type Lifecycle = (typeof LIFECYCLES)[number];
export const LIFECYCLE_ACTIONS = ['start', 'pause', 'resume', 'complete', 'cancel', 'reopen'] as const;
export type LifecycleAction = (typeof LIFECYCLE_ACTIONS)[number];

export const TASK_STATUSES = ['todo', 'in_progress', 'in_review', 'done', 'cancelled'] as const;
export type TaskStatus = (typeof TASK_STATUSES)[number];
export const TASK_PRIORITIES = ['low', 'normal', 'high', 'urgent'] as const;
export type TaskPriority = (typeof TASK_PRIORITIES)[number];
export type TaskAction = 'start' | 'cancel' | 'reopen';

export const ASSET_TYPES = ['part_model', 'drawing', 'process_plan', 'quote', 'document', 'intake'] as const;
export type AssetType = (typeof ASSET_TYPES)[number];
export const APPROVAL_STAGES_PM = ['design', 'process', 'quote'] as const;
export type PmApprovalStage = (typeof APPROVAL_STAGES_PM)[number];

export type SubmissionStatus = 'pending' | 'approved' | 'rejected' | 'withdrawn';
export type RequestStatus = 'pending' | 'approved' | 'rejected' | 'withdrawn' | 'superseded';
export type RequestKind = 'engineering' | 'task_acceptance' | 'milestone_review';
export type ImpactStatus = 'open' | 'resolved' | 'dismissed';
export type Availability = 'building' | 'ready' | 'failed';
export type PublishState = 'draft' | 'published' | 'deprecated';

// ─── projects: lifecycle, objective, milestones ────────────────────────────

/** ProjectFullOut (api/projects.py). */
export interface ProjectFull extends Project {
  lifecycle: Lifecycle;
  objective: string;
  owner_id: UUID | null;
  hold_reason: string | null;
  version: number;
  /** P4: the current stage. */
  current_milestone_id?: UUID | null;
}

export interface MilestoneTaskCounts {
  by_status: Partial<Record<TaskStatus, number>>;
  done: number;
  /** Tasks in scope (cancelled excluded). */
  total: number;
  /** Required (stage-review) tasks in scope / done (P4). */
  required_total?: number;
  required_done?: number;
}

/** Stage state (P4): open → in_review (pending stage review) → accepted. */
export type MilestoneStatusPm = 'open' | 'in_review' | 'accepted';

/** Closed stage deliverable rule (P4). */
export interface MilestoneRequirements {
  required_asset_types?: string[];
}

/** milestone_out (api/overview.py). The P4 fields are optional so older servers still render. */
export interface Milestone {
  id: UUID;
  project_id: UUID;
  seq: number;
  name: string;
  description: string;
  created_at: ISODateTime | null;
  tasks: MilestoneTaskCounts;
  status?: MilestoneStatusPm;
  requirements?: MilestoneRequirements;
  accepted_review_id?: UUID | null;
  accepted_at?: ISODateTime | null;
  /** Optimistic-lock counter for PATCH and review creation. */
  version?: number;
  is_current?: boolean;
  pending_review_id?: UUID | null;
  /** Overview only (current stage): readiness summary. */
  readiness?: { ready: boolean; blocker_codes: string[] } | null;
}

export interface LifecycleRequest {
  action: LifecycleAction;
  reason?: string;
  expected_version: number;
  /** cancel only: also cancel the project's active runs. */
  cancel_runs?: boolean;
  /** reopen only (required when the project has stages): the stage to resume from. */
  milestone_id?: UUID | null;
}

export interface ProjectPlanPatch {
  objective?: string;
  owner_id?: UUID | null;
  expected_version?: number;
}

/** Run row of the overview (task_svc.run_brief + task / part / updated_at). */
export interface OverviewRun {
  id: UUID;
  title: string;
  status: RunSummary['status'];
  purpose: string;
  workflow_id: UUID | null;
  created_by: UUID;
  created_at: ISODateTime | null;
  task_id: UUID | null;
  part_id: UUID | null;
  updated_at: ISODateTime | null;
}

export interface Overview {
  project: {
    id: UUID;
    code: string;
    name: string;
    description: string;
    lifecycle: Lifecycle;
    status: 'active' | 'archived';
    archived_at: ISODateTime | null;
    objective: string;
    hold_reason: string | null;
    version: number;
    owner: { id: UUID; name: string; email: string } | null;
    current_milestone: Milestone | null;
    my_role: Project['my_role'];
  };
  /** Counts only (no weighted percentage); required_total excludes cancelled tasks. */
  tasks: { by_status: Record<TaskStatus, number>; required_done: number; required_total: number; in_scope?: number };
  my_tasks: Task[];
  pending_approvals: ApprovalRequestBrief[];
  active_runs: OverviewRun[];
  /** Failed in the last 7 days. */
  failed_runs_recent: OverviewRun[];
  open_impacts: ImpactNotice[];
  milestones: Milestone[];
  /** P4: current stage with its readiness summary (also under project.current_milestone). */
  current_milestone?: Milestone | null;
  /** P4: whether "complete" would pass now and why not (same blockers as the 409). */
  completion?: { ready: boolean; blockers: unknown } | null;
  /** P4: pending stage reviews (briefs) or their count. */
  pending_reviews?: OverviewReviewBrief[] | number;
}

/** Pending stage review shown on the overview. */
export interface OverviewReviewBrief {
  id: UUID;
  milestone_id: UUID;
  seq?: number;
  status?: string;
  reviewer_id?: UUID | null;
  reviewer?: { id: UUID; name: string } | null;
  request_id?: UUID | null;
  submitted_at?: ISODateTime | null;
}

export const getProjectFull = (projectId: UUID, o: Opt = {}) => request<ProjectFull>(`/projects/${enc(projectId)}`, { signal: o.signal });

export const getOverview = (projectId: UUID, o: Opt = {}) => request<Overview>(`/projects/${enc(projectId)}/overview`, { signal: o.signal });

export const projectLifecycle = (projectId: UUID, body: LifecycleRequest, o: Opt = {}) =>
  request<ProjectFull>(`/projects/${enc(projectId)}/lifecycle`, { method: 'POST', body, signal: o.signal });

export const patchProjectPlan = (projectId: UUID, body: ProjectPlanPatch, o: Opt = {}) =>
  request<ProjectFull>(`/projects/${enc(projectId)}`, { method: 'PATCH', body, signal: o.signal });

export const listMilestones = (projectId: UUID, o: Opt = {}) => request<Milestone[]>(`/projects/${enc(projectId)}/milestones`, { signal: o.signal });

export const createMilestone = (projectId: UUID, body: { name: string; description?: string; requirements?: { required_asset_types: string[] } }, o: Opt = {}) =>
  request<Milestone>(`/projects/${enc(projectId)}/milestones`, { method: 'POST', body, signal: o.signal });

// ─── tasks ─────────────────────────────────────────────────────────────────

/** task_out (services/tasks.py). */
export interface Task {
  id: UUID;
  project_id: UUID;
  number: number;
  /** "T-<number>" */
  key: string;
  title: string;
  objective: string;
  owner_id: UUID | null;
  reviewer_id: UUID | null;
  milestone_id: UUID | null;
  part_id: UUID | null;
  priority: TaskPriority;
  /** Required for its stage's review (P4); optional tasks never block a stage. Absent on older servers. */
  required?: boolean;
  status: TaskStatus;
  /** Set = the task is flagged as blocked (separate from status). */
  blocked_reason: string | null;
  start_date: ISODateTime | null;
  due_date: ISODateTime | null;
  current_baseline_id: UUID | null;
  version: number;
  created_by: UUID;
  created_at: ISODateTime | null;
  updated_at: ISODateTime | null;
}

export interface TaskBrief {
  id: UUID;
  number: number;
  key: string;
  title: string;
  status: TaskStatus;
}

/** RuleSpec (services/tasks.py): a closed, explainable rule set. */
export interface RuleSpec {
  asset_types: AssetType[];
  require_approval_stage: PmApprovalStage | null;
  allow_estimated: boolean;
  allow_simulated: boolean;
  /** W0 §2.2: Turbo output / AI values nobody confirmed are refused unless allowed. */
  allow_unverified: boolean;
  consistent_with_slot: string | null;
  min_count: number;
}

export const DEFAULT_RULE_SPEC: RuleSpec = {
  asset_types: [],
  require_approval_stage: null,
  allow_estimated: false,
  allow_simulated: false,
  allow_unverified: false,
  consistent_with_slot: null,
  min_count: 1,
};

/** criterion_out. */
export interface Criterion {
  id: UUID;
  task_id: UUID;
  seq: number;
  description: string;
  required: boolean;
  allowed_types: AssetType[];
  /** May be {} for rows created before the rule editor existed. */
  rule_spec: Partial<RuleSpec>;
  manual_required: boolean;
  version: number;
  created_at: ISODateTime | null;
}

export interface CriterionInput {
  description: string;
  required: boolean;
  allowed_types: AssetType[];
  rule_spec: RuleSpec;
  manual_required: boolean;
}

export interface BaselineItem {
  slot: string;
  asset_version_id: UUID;
  version: AssetVersion | MissingVersion;
}

/** baseline_out. */
export interface BaselineSnapshot {
  id: UUID;
  task_id: UUID;
  seq: number;
  hash: string;
  reason: string;
  /** null when an agent adopted it (collaboration knowledge reuse). */
  adopted_by: UUID | null;
  adopted_by_kind?: 'human' | 'agent';
  adopted_by_agent_id?: UUID | null;
  adopted_at: ISODateTime | null;
  current: boolean;
  items: BaselineItem[];
}

/** deliverable_out. */
export interface Deliverable {
  id: UUID;
  task_id: UUID;
  asset_version_id: UUID;
  criterion_ids: UUID[];
  note: string;
  selected_by: UUID;
  selected_at: ISODateTime | null;
  version: AssetVersion | MissingVersion;
}

/** notice_out. */
export interface ImpactNotice {
  id: UUID;
  project_id: UUID;
  task_id: UUID | null;
  submission_id: UUID | null;
  from_snapshot_id: UUID | null;
  to_snapshot_id: UUID | null;
  /** Stage-level impact (P4): stage_reopened / task_reopened_after_stage. */
  milestone_id?: UUID | null;
  milestone_review_id?: UUID | null;
  /** baseline_changed_after_done | deliverable_inconsistent | deliverable_not_in_baseline | stage_reopened | task_reopened_after_stage | approval_revoked | … */
  reason: string;
  /** warning | info */
  severity: string;
  details: Record<string, unknown>;
  status: ImpactStatus;
  resolved_by: UUID | null;
  resolution_note: string;
  created_at: ISODateTime | null;
  resolved_at: ISODateTime | null;
}

/** task_detail (services/tasks.py). */
export interface TaskDetail extends Task {
  criteria: Criterion[];
  baseline: BaselineSnapshot | null;
  dependencies: { predecessors: TaskBrief[]; successors: TaskBrief[] };
  collaborators: { id: UUID; name: string; email: string }[];
  deliverables: Deliverable[];
  latest_submission: Submission | null;
  active_run_count: number;
  pending_requests: ApprovalRequestBrief[];
  open_impacts: ImpactNotice[];
  permissions: { edit: boolean; participate: boolean };
  /** Only on transition responses. */
  /** `impacts` (reopen): stage impact notices it opened (task_reopened_after_stage). */
  transition?: { action: TaskAction; cancelled_runs?: UUID[]; active_runs?: RunBrief[]; impacts?: ImpactNotice[] };
}

export interface TaskListQuery {
  /** Comma-separated statuses. */
  status?: string;
  owner_id?: UUID;
  q?: string;
  milestone_id?: UUID;
}

export interface TaskCreate {
  title: string;
  objective?: string;
  owner_id?: UUID | null;
  reviewer_id?: UUID | null;
  milestone_id?: UUID | null;
  part_id?: UUID | null;
  priority?: TaskPriority;
  /** Required for the stage review (P4); default true on the server. */
  required?: boolean;
  blocked_reason?: string | null;
  start_date?: string | null;
  due_date?: string | null;
  collaborator_ids?: UUID[];
}

/** POST /projects/{id}/tasks/draft — an AI draft that is never stored; the user confirms it in the dialog. */
export interface TaskDraft {
  title: string;
  objective: string;
  criteria: { description: string; required: boolean; allowed_types: AssetType[]; rule_spec: Partial<RuleSpec> }[];
  suggested_workflow_id: UUID | null;
  /** true = produced by the simulated AI provider (deterministic placeholder). */
  simulated: boolean;
}

export type TaskPatch = Partial<Omit<TaskCreate, 'collaborator_ids'>> & { expected_version: number };

export interface TransitionRequest {
  action: TaskAction;
  reason?: string;
  expected_version: number;
  /** cancel only: true = cancel the active runs, false = let them finish. */
  cancel_runs?: boolean | null;
}

/** task_svc.run_brief (409 active_runs details, transition responses). */
export interface RunBrief {
  id: UUID;
  title: string;
  status: RunSummary['status'];
  purpose: string;
  workflow_id: UUID | null;
  created_by: UUID;
  created_at: ISODateTime | null;
}

/** _run_summary (api/workflows.py) including the task fields. */
export interface TaskRunSummary extends RunSummary {
  task_id: UUID | null;
  task_baseline_snapshot_id: UUID | null;
  rerun_of: UUID | null;
  purpose: string;
}

export interface TaskRunCreate {
  workflow_id: UUID;
  /** Pinned published version; omitted = the template's latest published version (drafts are not allowed here). */
  template_version_id?: UUID | null;
  part_id?: UUID | null;
  params?: Record<string, Record<string, unknown>>;
  purpose?: string;
  title?: string;
  /** W7: explicit per run; omitted = standard. */
  execution_policy?: 'standard' | 'turbo';
}

export const listTasks = (projectId: UUID, q: TaskListQuery = {}, o: Opt = {}) =>
  request<Task[]>(`/projects/${enc(projectId)}/tasks`, { query: { ...q }, signal: o.signal });

export const createTask = (projectId: UUID, body: TaskCreate, o: Opt = {}) =>
  request<TaskDetail>(`/projects/${enc(projectId)}/tasks`, { method: 'POST', body, signal: o.signal });

/** Manage action; nothing is stored. 503 ai_unavailable when no model is configured. */
export const draftTask = (projectId: UUID, text: string, o: Opt = {}) =>
  request<TaskDraft>(`/projects/${enc(projectId)}/tasks/draft`, { method: 'POST', body: { text }, signal: o.signal });

export const getTask = (taskId: UUID, o: Opt = {}) => request<TaskDetail>(`/tasks/${enc(taskId)}`, { signal: o.signal });

export const patchTask = (taskId: UUID, body: TaskPatch, o: Opt = {}) =>
  request<TaskDetail>(`/tasks/${enc(taskId)}`, { method: 'PATCH', body, signal: o.signal });

export const transitionTask = (taskId: UUID, body: TransitionRequest, o: Opt = {}) =>
  request<TaskDetail>(`/tasks/${enc(taskId)}/transitions`, { method: 'POST', body, signal: o.signal });

export const setTaskDependencies = (taskId: UUID, predecessorIds: UUID[], o: Opt = {}) =>
  request<TaskDetail>(`/tasks/${enc(taskId)}/dependencies`, { method: 'PUT', body: { predecessor_ids: predecessorIds }, signal: o.signal });

export const setTaskCollaborators = (taskId: UUID, userIds: UUID[], o: Opt = {}) =>
  request<TaskDetail>(`/tasks/${enc(taskId)}/collaborators`, { method: 'PUT', body: { user_ids: userIds }, signal: o.signal });

// criteria
export const listCriteria = (taskId: UUID, o: Opt = {}) => request<Criterion[]>(`/tasks/${enc(taskId)}/criteria`, { signal: o.signal });

export const addCriterion = (taskId: UUID, body: CriterionInput, o: Opt = {}) =>
  request<Criterion>(`/tasks/${enc(taskId)}/criteria`, { method: 'POST', body, signal: o.signal });

export const patchCriterion = (criterionId: UUID, body: Partial<CriterionInput> & { expected_version?: number }, o: Opt = {}) =>
  request<Criterion>(`/criteria/${enc(criterionId)}`, { method: 'PATCH', body, signal: o.signal });

export const deleteCriterion = (criterionId: UUID, o: Opt = {}) => request<void>(`/criteria/${enc(criterionId)}`, { method: 'DELETE', signal: o.signal });

// baselines
export interface BaselineAdopt {
  /** slot → asset version id (null removes the slot); merged into the current baseline unless replace. */
  items: Record<string, UUID | null>;
  reason?: string;
  replace?: boolean;
  expected_version?: number;
}

export interface BaselineAdopted {
  baseline: BaselineSnapshot;
  /** false = identical to the current baseline, nothing new was recorded. */
  created: boolean;
  impacts: ImpactNotice[];
  task: Task;
}

export const listBaselines = (taskId: UUID, o: Opt = {}) => request<BaselineSnapshot[]>(`/tasks/${enc(taskId)}/baselines`, { signal: o.signal });

export const adoptBaseline = (taskId: UUID, body: BaselineAdopt, o: Opt = {}) =>
  request<BaselineAdopted>(`/tasks/${enc(taskId)}/baselines`, { method: 'POST', body, signal: o.signal });

// runs
export const listTaskRuns = (taskId: UUID, o: Opt = {}) => request<TaskRunSummary[]>(`/tasks/${enc(taskId)}/runs`, { signal: o.signal });

/** Server audit trail of a task (status transitions with reasons, edits, criteria, baselines, dependencies). */
export interface TaskActivityEntry {
  id: UUID;
  action: string;
  data: { from?: string; to?: string; reason?: string; [k: string]: unknown };
  user_id: UUID | null;
  user_name: string | null;
  at: string;
}
export const listTaskActivity = (taskId: UUID, o: Opt = {}) => request<TaskActivityEntry[]>(`/tasks/${enc(taskId)}/activity`, { signal: o.signal });

export const startTaskRun = (taskId: UUID, body: TaskRunCreate, o: Opt = {}) =>
  request<TaskRunSummary>(`/tasks/${enc(taskId)}/runs`, { method: 'POST', body: runChoices(body), signal: o.signal });

// deliverables & checks
/** GET /tasks/{id}/deliverables — the selected deliverables (drafts until submitted). */
export const listTaskDeliverables = (taskId: UUID, o: Opt = {}) => request<Deliverable[]>(`/tasks/${enc(taskId)}/deliverables`, { signal: o.signal });

export const addDeliverable = (taskId: UUID, body: { asset_version_id: UUID; criterion_ids: UUID[]; note: string }, o: Opt = {}) =>
  request<Deliverable>(`/tasks/${enc(taskId)}/deliverables`, { method: 'POST', body, signal: o.signal });

export const deleteDeliverable = (deliverableId: UUID, o: Opt = {}) =>
  request<void>(`/deliverables/${enc(deliverableId)}`, { method: 'DELETE', signal: o.signal });

/** One problem of a check (services/submissions.run_checks). */
export interface CheckProblem {
  code: string;
  message: string;
  asset_version_id?: UUID;
  label?: string;
  stage?: string;
  slot?: string;
  asset_type?: string;
  sources?: UUID[];
  source_labels?: string[];
  expected?: UUID[];
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
  asset_type: AssetType | null;
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
  /** Only on approval-time re-checks. */
  snapshot_tampered?: boolean;
}

export const getTaskChecks = (taskId: UUID, o: Opt = {}) => request<CheckReport>(`/tasks/${enc(taskId)}/checks`, { signal: o.signal });

// submissions
export interface FrozenItem {
  asset_version_id: UUID;
  asset_id: UUID;
  asset_type: AssetType | null;
  label: string;
  version_no: number;
  content_hash: string;
  availability: Availability;
  labels: string[];
  criterion_ids: UUID[];
  note: string;
  sources: UUID[];
  engineering: { stage: string; approved: boolean } | null;
}

export interface SubmissionSnapshot {
  schema: number;
  policy_version: string;
  task: { id: UUID; number: number; title: string; objective: string; version: number };
  baseline: { id: UUID; seq: number; hash: string; items: Record<string, UUID> } | null;
  criteria: Criterion[];
  items: FrozenItem[];
  checks: CheckReport;
  note: string;
  submitted_by: UUID;
}

/** submission_out. `snapshot` / `items` only on list and detail calls. */
export interface Submission {
  id: UUID;
  task_id: UUID;
  seq: number;
  baseline_snapshot_id: UUID | null;
  snapshot_hash: string;
  note: string;
  status: SubmissionStatus;
  submitted_by: UUID;
  submitted_at: ISODateTime | null;
  decided_at: ISODateTime | null;
  /** The task_acceptance approval request. */
  request_id: UUID | null;
  checks_ok: boolean;
  snapshot?: SubmissionSnapshot;
  items?: { id: UUID; asset_version_id: UUID; criterion_id: UUID | null; content_hash: string; check_result: unknown }[];
}

export const listSubmissions = (taskId: UUID, o: Opt = {}) => request<Submission[]>(`/tasks/${enc(taskId)}/submissions`, { signal: o.signal });

export const createSubmission = (taskId: UUID, body: { note: string; expected_version: number }, o: Opt = {}) =>
  request<Submission & { task: Task }>(`/tasks/${enc(taskId)}/submissions`, { method: 'POST', body, signal: o.signal });

export const withdrawSubmission = (submissionId: UUID, o: Opt = {}) =>
  request<Submission & { task: Task }>(`/submissions/${enc(submissionId)}/withdraw`, { method: 'POST', signal: o.signal });

// impact notices
export const listImpactNotices = (projectId: UUID, q: { status?: ImpactStatus | 'all'; task_id?: UUID } = {}, o: Opt = {}) =>
  request<ImpactNotice[]>(`/projects/${enc(projectId)}/impact-notices`, { query: { ...q }, signal: o.signal });

export const resolveImpactNotice = (noticeId: UUID, body: { note: string; status?: 'resolved' | 'dismissed' }, o: Opt = {}) =>
  request<ImpactNotice>(`/impact-notices/${enc(noticeId)}/resolve`, { method: 'POST', body, signal: o.signal });

// ─── approval requests (read-only views the tasks UI needs) ─────────────────

/** request_out (services/approval_requests.py). */
export interface ApprovalRequestBrief {
  id: UUID;
  project_id: UUID;
  kind: RequestKind;
  stage: PmApprovalStage | null;
  status: RequestStatus;
  title: string;
  assignee_id: UUID | null;
  assignee: { id: UUID; name: string } | null;
  task: TaskBrief | null;
  run_id: UUID | null;
  node_run_id: UUID | null;
  node_id: string | null;
  node_status: string | null;
  asset_version_id: UUID | null;
  submission_id: UUID | null;
  target_hash: string;
  policy_version: string | null;
  version: number;
  created_by: UUID;
  created_by_name: string | null;
  created_at: ISODateTime | null;
  due_at: ISODateTime | null;
  closed_at: ISODateTime | null;
  can_decide: boolean;
}

export interface ApprovalDecisionRecord {
  id: UUID;
  decision: 'approved' | 'rejected';
  comment: string;
  actor_id: UUID;
  actor_name: string | null;
  role: string;
  target_hash: string;
  created_at: ISODateTime | null;
}

export interface ApprovalRequestWithDecisions extends ApprovalRequestBrief {
  decisions: ApprovalDecisionRecord[];
}

export const getApprovalRequestDecisions = (requestId: UUID, o: Opt = {}) =>
  request<ApprovalRequestWithDecisions>(`/approval-requests/${enc(requestId)}`, { signal: o.signal });

// ─── asset catalogue reads (version pickers) ───────────────────────────────

export interface AssetEdgeRef {
  version_id: UUID;
  relation: string;
  asset_id?: UUID;
  asset_type?: AssetType;
  asset_name?: string | null;
  version_no?: number;
  label?: string;
  content_hash?: string;
  target_id?: string;
}

/** version_out (services/assets.py). */
export interface AssetVersion {
  id: UUID;
  asset_id: UUID;
  asset_type: AssetType;
  asset_name: string;
  project_id: UUID;
  version_no: number;
  /** "R3" | "D2" | "P1" | "Q1" | "v2" | "I1" */
  label: string;
  content_hash: string;
  availability: Availability;
  publish_state: PublishState;
  /** simulated / estimated / mesh_reference / imported / from_failed_run … */
  labels: string[];
  parent_version_id: UUID | null;
  created_at: ISODateTime | null;
  created_by: UUID | null;
  node_run_id: UUID | null;
  run_id: UUID | null;
  target: { kind: string; id: string; part_id: UUID | null; summary: Record<string, unknown> };
  engineering: { stage: string; approved: boolean; decision: Record<string, unknown> | null } | null;
  dependencies?: AssetEdgeRef[];
  dependents?: AssetEdgeRef[];
  missing?: undefined;
}

/** Placeholder the server returns when a referenced version no longer exists. */
export interface MissingVersion {
  id: UUID;
  missing: true;
}

export function isMissingVersion(v: AssetVersion | MissingVersion | null | undefined): v is MissingVersion {
  return !!v && (v as MissingVersion).missing === true;
}

/** asset_out. */
export interface AssetSummary {
  id: UUID;
  project_id: UUID;
  type: AssetType;
  name: string;
  part_id: UUID | null;
  file_id: UUID | null;
  latest_version: AssetVersion | null;
  version_count: number;
  created_at: ISODateTime | null;
  archived_at: ISODateTime | null;
}

export const listProjectAssets = (projectId: UUID, q: { type?: AssetType; q?: string; part_id?: UUID } = {}, o: Opt = {}) =>
  request<AssetSummary[]>(`/projects/${enc(projectId)}/assets`, { query: { ...q }, signal: o.signal });

export const listAssetVersionsOf = (assetId: UUID, o: Opt = {}) => request<AssetVersion[]>(`/assets/${enc(assetId)}/versions`, { signal: o.signal });

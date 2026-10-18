// Agent collaboration API (.trellis/tasks/archive/2026-10/10-08-agent-collaboration/design.md §4 C01–C06).
// Types mirror the server dict builders they name: draft_out, collaboration_out,
// node_task_out, collaboration_summary (api/collaboration.py, services/collaboration.py,
// services/agent_runtime.py). When one of those changes, change it here.
//
// The backend is built in parallel: every page treats a 404 / 405 from these
// routes as an ordinary error state.

import type { ActorKind, ActorOut, ActorRef, AgentBrief, AgentLimits, ExecutionMode } from './agents';
import { request } from './client';
import type { AssetType, AssetVersion, CheckReport, MissingVersion, RunBrief, RuleSpec, Submission, Task } from './pm';
import type { Assumption, ExecutionPolicy, Verification } from './engineering';
import type { AiDiagnostic, GraphError, ISODateTime, RunStatus, RunSummary, UUID, Workflow, WorkflowGraph } from './types';

type Opt = { signal?: AbortSignal };
const enc = encodeURIComponent;

// ─── enums ─────────────────────────────────────────────────────────────────

export const PLANNING_SOURCES = ['ai', 'manual'] as const;
export type PlanningSource = (typeof PLANNING_SOURCES)[number];

/** completed_unverified (W7 Turbo): real outputs released downstream without review or acceptance — never "accepted". */
export const NODE_TASK_PHASES = [
  'blocked',
  'ready',
  'executing',
  'submitted',
  'in_review',
  'rework',
  'accepted',
  'failed',
  'cancelled',
  'completed_unverified',
] as const;
export type NodeTaskPhase = (typeof NODE_TASK_PHASES)[number];

export const NODE_ACTIONS = ['start', 'add_deliverable', 'check', 'submit', 'withdraw', 'reopen', 'cancel', 'adopt_baseline'] as const;
export type NodeAction = (typeof NODE_ACTIONS)[number];

export const RUN_ACTIONS = ['pause', 'resume', 'cancel', 'fork_semi_auto'] as const;
export type RunAction = (typeof RUN_ACTIONS)[number];

export const RESOURCE_KINDS = ['input', 'candidate', 'formal', 'failed', 'superseded', 'evidence'] as const;
export type ResourceKind = (typeof RESOURCE_KINDS)[number];

export type ArchiveStatus = 'none' | 'pending' | 'archived' | 'failed';
export type ExecutionRole = 'execute' | 'pre_review' | 'review' | 'planner';
export type ExecutionStatus = 'running' | 'delivered' | 'blocked' | 'failed' | 'superseded' | 'cancelled';
export type CriterionResult = 'pass' | 'fail' | 'unknown';

/** C01 demand length limit. */
export const DEMAND_MAX = 30000;
/** Idempotency keys are 8–120 characters. */
export const IDEMPOTENCY_KEY_MAX = 120;

// ─── business_task params (workflow/nodes.py BusinessTaskParams) ───────────

export interface CriterionParam {
  description: string;
  required: boolean;
  allowed_types: string[];
  rule_spec: Partial<RuleSpec>;
  manual_required: boolean;
}

export interface SlotParam {
  slot_key: string;
  asset_type: AssetType | string;
  required: boolean;
}

export interface InputSlotParam extends SlotParam {
  source_node_id: string | null;
  source_slot_key: string | null;
  fixed_asset_version_id: UUID | null;
}

export interface RecipeParam {
  instructions: string;
  tools: string[];
}

export interface BusinessTaskParams {
  title: string;
  objective: string;
  executor: ActorRef;
  /** Required by standard runs; a Turbo draft has no acceptance subject (null). */
  reviewer: ActorRef | null;
  pre_reviewer_agent_id: UUID | null;
  criteria: CriterionParam[];
  output_slots: SlotParam[];
  input_slots: InputSlotParam[];
  recipe: RecipeParam;
}

/** Read a graph node's params as business-task params (missing fields get safe defaults). */
export function businessTaskParams(params: Record<string, unknown> | undefined): BusinessTaskParams {
  const p = (params ?? {}) as Partial<BusinessTaskParams>;
  return {
    title: typeof p.title === 'string' ? p.title : '',
    objective: typeof p.objective === 'string' ? p.objective : '',
    executor: p.executor ?? { kind: 'agent' },
    reviewer: p.reviewer ?? { kind: 'agent' },
    pre_reviewer_agent_id: p.pre_reviewer_agent_id ?? null,
    criteria: Array.isArray(p.criteria) ? p.criteria : [],
    output_slots: Array.isArray(p.output_slots) ? p.output_slots : [],
    input_slots: Array.isArray(p.input_slots) ? p.input_slots : [],
    recipe: p.recipe ?? { instructions: '', tools: [] },
  };
}

// ─── drafts (C01 / C02) ────────────────────────────────────────────────────

export interface FixedInputIn {
  slot_key: string;
  asset_version_id: UUID;
}

/** Stored fixed input (collaboration_drafts.fixed_inputs). */
export interface FixedInput extends FixedInputIn {
  content_hash?: string;
  label?: string;
}

export interface MissingInformation {
  field: string;
  question: string;
  node_id: string | null;
}

/** graph._err shape: {code, message, node_id?, param?} (A03). */
export type ValidationError = GraphError;

export interface PlanningRecord {
  /** planned / failed / unavailable */
  status?: string;
  provider?: string | null;
  model_id?: string | null;
  simulated?: boolean;
  ai_call_ids?: UUID[];
  summary?: string;
  assumptions?: string[];
  at?: ISODateTime;
  error?: { code?: string; message?: string } | null;
  /** W1: code, stage, purpose, exact configuration, call ids and recovery of a failed / unavailable planning. */
  diagnostic?: AiDiagnostic | null;
}

/**
 * CollaborationDraft row. The default actors may be serialised as actor_out
 * objects or as the raw prefix columns; `draftActor` reads both.
 */
export interface CollaborationDraft {
  id: UUID;
  workflow_id: UUID;
  project_id: UUID;
  demand: string;
  execution_mode: ExecutionMode;
  /** Frozen before planning (W7); rows from before W7 read as standard. */
  execution_policy?: ExecutionPolicy;
  /** Values a Turbo planner adopted instead of asking (never confirmed facts). */
  assumptions?: Assumption[];
  planning_source: PlanningSource;
  planner_agent_id: UUID | null;
  executor?: ActorOut | ActorRef | null;
  reviewer?: ActorOut | ActorRef | null;
  executor_kind?: ActorKind;
  executor_user_id?: UUID | null;
  executor_agent_id?: UUID | null;
  reviewer_kind?: ActorKind;
  reviewer_user_id?: UUID | null;
  reviewer_agent_id?: UUID | null;
  pre_reviewer_agent_id: UUID | null;
  part_id: UUID | null;
  fixed_inputs: FixedInput[];
  knowledge_item_ids: UUID[];
  missing_information: MissingInformation[];
  validation_errors: ValidationError[];
  planning: PlanningRecord;
  summary: string;
  ready_to_start: boolean;
  version: number;
  created_by: UUID;
  created_at: ISODateTime | null;
  updated_at: ISODateTime | null;
}

/** The draft's default executor / reviewer as an ActorRef, whichever serialisation the server uses. */
export function draftActor(d: CollaborationDraft, which: 'executor' | 'reviewer'): ActorRef | null {
  const obj = d[which];
  if (obj && typeof obj === 'object') {
    if ('id' in obj && typeof obj.id === 'string') return obj.kind === 'human' ? { kind: 'human', user_id: obj.id } : { kind: 'agent', agent_profile_id: obj.id };
    const ref = obj as ActorRef;
    if (ref.user_id || ref.agent_profile_id) return ref;
  }
  const kind = d[`${which}_kind`];
  const userId = d[`${which}_user_id`];
  const agentId = d[`${which}_agent_id`];
  if (kind === 'agent' && agentId) return { kind: 'agent', agent_profile_id: agentId };
  if (userId) return { kind: 'human', user_id: userId };
  if (agentId) return { kind: 'agent', agent_profile_id: agentId };
  return null;
}

export interface DraftWorkflowRef {
  id: UUID;
  name: string;
  kind: 'collaboration' | 'tool' | string;
  version: number;
}

/** draft_out (C01 / C02). */
/** Latest instance started from the draft. */
export interface DraftRunBrief {
  id: UUID;
  title: string;
  status: RunStatus;
  execution_mode?: ExecutionMode;
  execution_policy?: ExecutionPolicy;
  created_at?: ISODateTime | null;
}

export interface DraftOut {
  workflow: DraftWorkflowRef;
  draft: CollaborationDraft;
  graph: WorkflowGraph;
  validation_errors: ValidationError[];
  missing_information: MissingInformation[];
  ready_to_start: boolean;
  planning: PlanningRecord;
  /** The instance started from this draft (e.g. full_auto + auto_start + ready in the same request), else null. */
  run: DraftRunBrief | Pick<RunBrief, 'id' | 'title' | 'status'> | null;
}

export interface PlanIn {
  demand: string;
  execution_mode: ExecutionMode;
  /** Explicit per request (W7); standard is the default. Part of the idempotency fingerprint. */
  execution_policy: ExecutionPolicy;
  planning_source: PlanningSource;
  planner_agent_id: UUID | null;
  executor: ActorRef;
  /** Required in standard; null in Turbo (no acceptance). */
  reviewer: ActorRef | null;
  pre_reviewer_agent_id: UUID | null;
  part_id: UUID | null;
  fixed_inputs: FixedInputIn[];
  knowledge_item_ids: UUID[];
  graph: WorkflowGraph | null;
  auto_start: boolean;
  idempotency_key: string;
  /** Re-plan an existing draft instead of creating one. */
  workflow_id: UUID | null;
}

export interface DraftPatch {
  expected_version: number;
  demand?: string;
  execution_mode?: ExecutionMode;
  executor?: ActorRef;
  reviewer?: ActorRef;
  pre_reviewer_agent_id?: UUID | null;
  part_id?: UUID | null;
  fixed_inputs?: FixedInputIn[];
  knowledge_item_ids?: UUID[];
  /** Replaces the list once the person has supplied the facts. */
  missing_information?: MissingInformation[];
  /** Only the given nodes change (A54). */
  nodes?: Record<string, Partial<BusinessTaskParams>>;
  graph?: WorkflowGraph;
}

export interface StartIn {
  execution_mode: ExecutionMode;
  /** Must equal the draft's frozen policy (422 execution_policy_mismatch). */
  execution_policy?: ExecutionPolicy;
  expected_version: number;
  idempotency_key: string;
  fixed_inputs?: FixedInputIn[];
  title?: string;
}

// ─── instances (C03) ───────────────────────────────────────────────────────

export interface CollaborationMeta {
  execution_mode: ExecutionMode;
  execution_policy?: ExecutionPolicy;
  /** Frozen with the run (Turbo planner assumptions). */
  assumptions?: Assumption[];
  paused_at: ISODateTime | null;
  pause_reason: string | null;
  control_version: number;
  contract_hash: string;
  forked_from_run_id: UUID | null;
  archive_status: ArchiveStatus;
  knowledge_collection_id: UUID | null;
  completed_at: ISODateTime | null;
}

/** Server-side counts; never recomputed in the browser. */
export interface Progress {
  total: number;
  executed: number;
  accepted: number;
  blocked: number;
  /** Turbo runs only: completed without review or acceptance (never part of accepted). */
  unverified?: number;
}

export interface ResourcesSummary {
  by_kind: Partial<Record<ResourceKind, number>>;
  total: number;
}

export interface KnowledgeRef {
  collection_id: UUID;
  status: string;
  index_status: string;
}

export interface ActivityEntry {
  at: ISODateTime;
  kind: string;
  actor: ActorOut | null;
  node_id: string | null;
  message: string;
  ref: unknown;
}

export type VersionBrief = AssetVersion | MissingVersion;

export interface BoundInput {
  slot_key: string;
  version: VersionBrief;
  content_hash: string;
  from_node_id: string | null;
}

export interface FormalOutput {
  slot_key: string;
  version: VersionBrief;
  content_hash?: string;
}

/** One per-criterion verdict (ReviewReply.criteria / criterion_decisions). */
/** What a tool_agent review read for one criterion (execution layer P4): one evidence tool call of the attempt. */
export interface EvidenceRead {
  call_index: number;
  tool: 'list_submission' | 'read_document' | 'read_checks' | 'compare_versions' | string;
  asset_version_id: UUID;
  base_asset_version_id?: UUID;
  label?: string;
  page?: number;
  pages?: number;
  sections?: string[];
  items?: string[];
}

export interface CriterionDecision {
  criterion_id: UUID;
  result: CriterionResult;
  reason: string;
  evidence_asset_version_ids: UUID[];
  /** P4: the reads behind the decision (tool_agent reviews only). */
  evidence?: EvidenceRead[];
}

/** agent_executions.result of a pre-review: the ReviewReply. */
export interface ReviewResult {
  criteria?: CriterionDecision[];
  summary?: string;
  [k: string]: unknown;
}

/** The executing / reviewing agent of a record: actor_out (current server) or a bare agent brief. */
export type AgentRef = ActorOut | AgentBrief;

export interface PreReview {
  execution_id: UUID;
  result: ReviewResult | null;
  at: ISODateTime | null;
  agent: AgentRef | null;
}

export interface FinalDecision {
  decision_id: UUID;
  request_id?: UUID;
  submission_id?: UUID;
  decision: 'approved' | 'rejected';
  comment?: string;
  actor: ActorOut | null;
  at: ISODateTime | null;
  criterion_decisions: CriterionDecision[];
}

export interface ExecutionUsage {
  tokens_in?: number;
  tokens_out?: number;
  calls?: number;
  cost_fen?: number | null;
  cost_unknown?: boolean;
}

export interface AgentExecutionBrief {
  id: UUID;
  role: ExecutionRole;
  attempt_no: number;
  rework_no: number;
  status: ExecutionStatus;
  agent: AgentRef | null;
  profile_version?: number;
  started_at: ISODateTime | null;
  finished_at: ISODateTime | null;
  duration_ms?: number | null;
  usage: ExecutionUsage;
  error: { code?: string | null; message?: string | null } | null;
  submission_id?: UUID | null;
  decision_id?: UUID | null;
  /** Only on GET /runs/{id}/node-tasks/{node_id}. */
  steps?: unknown[];
  result?: unknown;
}

/** Cumulative, never reset (design §3.9). */
export interface NodeBudget {
  steps: number;
  duration_ms: number;
  attempts: number;
  reworks: number;
  cost_fen: number | null;
  cost_unknown: boolean;
}

export interface BlockedReason {
  code: string;
  message: string;
  [k: string]: unknown;
}

/** node_task_out. `version` is the node_task optimistic lock C04 expects. */
export interface NodeTaskOut {
  id: UUID;
  run_id?: UUID;
  node_id: string;
  node_run_id?: UUID;
  task_id?: UUID;
  title: string;
  objective: string;
  phase: NodeTaskPhase;
  blocked_reason: BlockedReason | null;
  /** A person's answers to the questions an agent was blocked on (blocked_reason.questions). */
  clarifications?: { question: string; answer: string; at?: string | null }[];
  task: Task | null;
  executor: ActorOut;
  /** null in Turbo runs (no acceptance subject). */
  reviewer: ActorOut | null;
  pre_reviewer: ActorOut | null;
  input_slots: InputSlotParam[];
  output_slots: SlotParam[];
  bound_inputs: BoundInput[];
  candidates: VersionBrief[];
  current_submission: Submission | null;
  accepted_submission_id: UUID | null;
  formal_outputs: FormalOutput[];
  pre_review: PreReview | null;
  final_decision: FinalDecision | null;
  /** Turbo: the unverified outputs' verification (phase completed_unverified), else null. */
  verification?: Verification | null;
  /** Turbo: a person's node bound to an agent, or why it could not be automated. */
  turbo_binding?: TurboBinding | null;
  executions: AgentExecutionBrief[];
  budget: Partial<NodeBudget>;
  limits: Partial<AgentLimits>;
  /** Actions the current user may take now (design C04); the only source of action buttons. */
  actions: NodeAction[];
  version: number;
}

export interface TurboBinding {
  status: 'bound' | 'not_automatable';
  reason: string;
  tools: string[];
  from?: ActorRef;
  to?: ActorRef;
}

/** collaboration_out (C03). */
export interface CollaborationOut {
  run: RunSummary;
  collaboration: CollaborationMeta;
  progress: Progress;
  node_tasks: NodeTaskOut[];
  resources: ResourcesSummary;
  knowledge: KnowledgeRef | null;
  activity: ActivityEntry[];
  actions: RunAction[];
}

/**
 * collaboration_summary (list rows). The exact shape is C's; the list only
 * needs the run id / title / status, the mode and the progress, so
 * `summaryRow` reads either a nested {run, collaboration, progress} or a flat row.
 */
export interface CollaborationSummary {
  run?: Partial<RunSummary> | null;
  collaboration?: Partial<CollaborationMeta> | null;
  progress?: Partial<Progress> | null;
  run_id?: UUID;
  id?: UUID;
  title?: string;
  status?: RunStatus;
  execution_mode?: ExecutionMode;
  paused_at?: ISODateTime | null;
  created_at?: ISODateTime | null;
  completed_at?: ISODateTime | null;
  archive_status?: ArchiveStatus;
}

export interface CollaborationRow {
  runId: UUID;
  title: string;
  status: RunStatus | null;
  mode: ExecutionMode | null;
  /** W7: standard unless the row says turbo. */
  policy: ExecutionPolicy;
  paused: boolean;
  progress: Progress;
  createdAt: ISODateTime | null;
  completedAt: ISODateTime | null;
  archiveStatus: ArchiveStatus | null;
}

export function summaryRow(s: CollaborationSummary): CollaborationRow | null {
  const runId = s.run?.id ?? s.run_id ?? s.id;
  if (!runId) return null;
  const p = s.progress ?? {};
  return {
    runId,
    title: s.run?.title ?? s.title ?? '',
    status: s.run?.status ?? s.status ?? null,
    mode: s.collaboration?.execution_mode ?? s.execution_mode ?? null,
    policy: (s.collaboration?.execution_policy ?? s.run?.execution_policy) === 'turbo' ? 'turbo' : 'standard',
    paused: !!(s.collaboration?.paused_at ?? s.paused_at),
    progress: { total: p.total ?? 0, executed: p.executed ?? 0, accepted: p.accepted ?? 0, blocked: p.blocked ?? 0 },
    createdAt: s.run?.created_at ?? s.created_at ?? null,
    completedAt: s.collaboration?.completed_at ?? s.completed_at ?? null,
    archiveStatus: s.collaboration?.archive_status ?? s.archive_status ?? null,
  };
}

// ─── node actions (C04) ────────────────────────────────────────────────────

export type NodeTaskActionIn =
  | { action: 'start'; expected_version: number }
  | { action: 'add_deliverable'; expected_version: number; asset_version_id: UUID; criterion_ids: UUID[]; note: string }
  | { action: 'check'; expected_version: number }
  | { action: 'submit'; expected_version: number; note: string }
  | { action: 'withdraw'; expected_version: number; reason: string }
  | { action: 'reopen'; expected_version: number; reason: string }
  | { action: 'cancel'; expected_version: number; reason: string }
  | { action: 'adopt_baseline'; expected_version: number; items: Record<string, UUID | null>; reason: string };

/** C04 returns node_task_out; `check` may also carry the preview report. */
export interface NodeTaskActionOut extends NodeTaskOut {
  checks?: CheckReport | null;
}

// ─── requests ──────────────────────────────────────────────────────────────

/** C01: plan (and, for full_auto with auto_start, start) a demand. 503 ai_unavailable keeps the demand. */
export const planCollaboration = (projectId: UUID, body: PlanIn, o: Opt = {}) =>
  request<DraftOut>(`/projects/${enc(projectId)}/collaboration/plan`, { method: 'POST', body, signal: o.signal });

export const getCollaborationDraft = (workflowId: UUID, o: Opt = {}) =>
  request<DraftOut>(`/workflows/${enc(workflowId)}/collaboration-draft`, { signal: o.signal });

export const patchCollaborationDraft = (workflowId: UUID, body: DraftPatch, o: Opt = {}) =>
  request<DraftOut>(`/workflows/${enc(workflowId)}/collaboration-draft`, { method: 'PATCH', body, signal: o.signal });

/** C02: start. Same key + same content returns the existing instance; 422 draft_not_ready; 409 version_conflict / idempotency_mismatch. */
export const startCollaborationRun = (workflowId: UUID, body: StartIn, o: Opt = {}) =>
  request<CollaborationOut>(`/workflows/${enc(workflowId)}/collaboration-runs`, { method: 'POST', body, signal: o.signal });

export const listCollaborationRuns = (projectId: UUID, q: { status?: string; mode?: ExecutionMode | ''; limit?: number; offset?: number } = {}, o: Opt = {}) =>
  request<CollaborationSummary[]>(`/projects/${enc(projectId)}/collaboration-runs`, {
    query: { status: q.status || undefined, mode: q.mode || undefined, limit: q.limit, offset: q.offset },
    signal: o.signal,
  });

export const getCollaboration = (runId: UUID, o: Opt = {}) => request<CollaborationOut>(`/runs/${enc(runId)}/collaboration`, { signal: o.signal });

export const listNodeTasks = (runId: UUID, o: Opt = {}) => request<NodeTaskOut[]>(`/runs/${enc(runId)}/node-tasks`, { signal: o.signal });

export const getNodeTask = (runId: UUID, nodeId: string, o: Opt = {}) =>
  request<NodeTaskOut>(`/runs/${enc(runId)}/node-tasks/${enc(nodeId)}`, { signal: o.signal });

/** C04. 403 actor_not_allowed / 409 mode_forbids / 409 version_conflict. */
export const nodeTaskAction = (runId: UUID, nodeId: string, body: NodeTaskActionIn, o: Opt = {}) =>
  request<NodeTaskActionOut>(`/runs/${enc(runId)}/nodes/${enc(nodeId)}/task-action`, { method: 'POST', body, signal: o.signal });

/** Answer the questions an agent was blocked on; the node is queued again with the answers. 409 node_not_waiting_for_answers. */
export const answerNodeQuestions = (
  runId: UUID,
  nodeId: string,
  body: { answers: { question: string; answer: string }[]; expected_version?: number },
  o: Opt = {},
) => request<NodeTaskOut>(`/runs/${enc(runId)}/nodes/${enc(nodeId)}/answers`, { method: 'POST', body, signal: o.signal });

// C06
export const pauseCollaboration = (runId: UUID, body: { expected_version: number; reason: string }, o: Opt = {}) =>
  request<CollaborationOut>(`/runs/${enc(runId)}/collaboration/pause`, { method: 'POST', body, signal: o.signal });

export const resumeCollaboration = (runId: UUID, body: { expected_version: number }, o: Opt = {}) =>
  request<CollaborationOut>(`/runs/${enc(runId)}/collaboration/resume`, { method: 'POST', body, signal: o.signal });

export const forkSemiAuto = (runId: UUID, body: { expected_version: number; reviewer_user_id: UUID; idempotency_key: string; reason: string }, o: Opt = {}) =>
  request<CollaborationOut>(`/runs/${enc(runId)}/collaboration/fork-semi-auto`, { method: 'POST', body, signal: o.signal });

/** Collaboration drafts of a project (workflows of kind=collaboration; the template list hides them). */
export const listCollaborationDrafts = (projectId: UUID, o: Opt = {}) =>
  request<Workflow[]>(`/projects/${enc(projectId)}/workflows`, { query: { kind: 'collaboration' }, signal: o.signal });

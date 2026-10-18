import { operationChoices } from './capabilities';
// Unified AI workspace API (.trellis/tasks/archive/2026-10/10-08-ai-workspace/design.md §3–§6).
// Types mirror the server builders in services/assistant.py; when one of those
// changes, change it here. Conversations are private to their owner.

import { request } from './client';
import type { DraftCommand } from './drafting';
import type { Assumption } from './engineering';
import type { ChainValue, SpecValue } from './orchestration';
import type { TaskDraft } from './pm';
import type { PolicySource,SourceVersion } from './turbo';
import type {
AiDiagnostic,
CheckStatus,
CheckSummary,
ClarificationGap,
DrawingBrief,
ISODateTime,
ReconstructSource,
RunStatus,
UUID,
Workflow,
WorkflowEdge,
WorkflowGraph,
WorkflowRun,
} from './types';

type Opt = { signal?: AbortSignal };
const enc = encodeURIComponent;

// ─── enums ─────────────────────────────────────────────────────────────────

export const AI_OPERATION_KINDS = [
  'task_draft',
  'model_create',
  'model_modify',
  'drawing_intake',
  'ask',
  'drawing_generate',
  'drawing_reconstruct',
  'workflow_edit',
  // W8: description / files / spec version → spec → parts → assembly → drawings → delivery (a project-level run), and a
  // planned change of an exact assembly revision applied only on confirmation.
  'assembly_create',
  'assembly_modify',
  // AI work panel: a new part workflow planned from a template (applied on request; .trellis/tasks/archive/2026-10/10-12-ai-work-panel).
  'workflow_create',
  'spec_extract',
  'drafting_create',
  'drafting_modify',
] as const;
export type AiOperationKind = (typeof AI_OPERATION_KINDS)[number];
/** An explicit kind, or `auto` for the main agent (read-only answer when its runtime is disabled). */
export type AiRequestedOperation = AiOperationKind | 'auto';

export const AI_OPERATION_STATUSES = ['pending', 'running', 'waiting_input', 'succeeded', 'failed', 'cancelled'] as const;
export type AiOperationStatus = (typeof AI_OPERATION_STATUSES)[number];
export const AI_TERMINAL: ReadonlySet<AiOperationStatus> = new Set(['succeeded', 'failed', 'cancelled']);

export type AiMessageRole = 'user' | 'assistant';
/** card: a run or workflow the conversation follows (AI work panel; payload AiCardPayload). */
export type AiMessageKind = 'request' | 'operation' | 'answer' | 'choice' | 'notice' | 'card';

export const AI_REF_KINDS = [
  'part',
  'part_revision',
  'cad_selection',
  'quote',
  'quote_field',
  'asset_version',
  'run_output',
  'run',
  'task',
  'criterion',
  'file_version',
  'text_excerpt',
  'workflow',
  // W2: an exact engineering spec version (its digest, readiness and evidence are read into the context).
  'engineering_spec_version',
  // W8: an exact assembly revision (instances, mates, BOM); stale once a newer revision exists.
  'assembly_revision',
  'drawing_document_version',
] as const;
export type AiRefKind = (typeof AI_REF_KINDS)[number];

export type QuoteFieldKey = 'material' | 'machining' | 'setup' | 'subcontract' | 'loss' | 'unit_cost_fen' | 'unit_inc_tax_fen' | 'order_inc_tax_fen' | 'lead_days';

export type Vec3 = [number, number, number];

/** A hole (or other op) a picked face may belong to (design §7.4 matchHoleCandidates). */
export interface AiOpCandidate {
  op_id: string;
  kind: string;
  label: string;
  diameter?: number | null;
  reason: string;
}

/** CAD pick. Face / edge ids belong to this build only (build_key + mesh_sha of the revision). */
export interface AiCadSelection {
  entity: 'face' | 'edge' | 'op';
  id: string | number;
  build_key: string | null;
  mesh_sha: string | null;
  units?: 'mm';
  frame?: 'world_mm_z_up';
  point?: Vec3 | null;
  normal?: Vec3 | null;
  center?: Vec3 | null;
  area?: number | null;
  radius?: number | null;
  length?: number | null;
  surface?: string | null;
  edge_kind?: string | null;
  candidates?: AiOpCandidate[];
  resolved_op_id?: string | null;
}

export interface AiQuoteField {
  tier_qty: number;
  key: QuoteFieldKey;
  /** Filled by the server from the stored quote (the client value is ignored). */
  value?: number | null;
  currency?: string;
  label?: string;
  label_en?: string;
  label_zh?: string;
  /** "fen" (money, integer) or "days". */
  unit?: 'fen' | 'days' | string;
  /** The quote line is an estimate (e.g. drawing-only). */
  estimated?: boolean;
  formula?: string;
}

export interface AiExcerpt {
  source_kind: 'quote' | 'task' | 'asset_version' | 'part_revision' | 'criterion';
  text: string;
}

/** AI work panel: one node of a workflow (its pinned version) or of a run (its graph snapshot). */
export interface AiRefNodeIn {
  node_id: string;
}

export interface AiRefNode {
  node_id: string;
  type: string;
  label: string | null;
  /** Run nodes only: the node run's status when the reference was made. */
  status?: string | null;
}

export interface AiRunOutputPointer {
  run_id: UUID;
  node_id: string;
  output_key: string;
}

/** What a page sends to add a reference (design §3.1). The server validates and fills the rest. */
export interface AiRefInput {
  kind: AiRefKind;
  object_id: UUID;
  selection?: AiCadSelection;
  field?: Pick<AiQuoteField, 'tier_qty' | 'key'>;
  excerpt?: AiExcerpt;
  output?: AiRunOutputPointer;
  /** kind workflow / run only: a node of it (key `workflow:<id>:v<n>:node:<node>` / `run:<id>:node:<node>`). */
  node?: AiRefNodeIn;
  version?: number;
  instance?: { path: string[] };
  entities?: { ids: string[]; sheet_id?: string };
}

/** Normalised, version-pinned reference (design §3.2): what a composer and a request snapshot hold. */
export interface AiRef {
  key: string;
  kind: AiRefKind;
  project_id: UUID;
  object_id: UUID;
  part_id: UUID | null;
  task_id: UUID | null;
  version_label: string | null;
  title: string;
  summary: string;
  selection: AiCadSelection | null;
  field: AiQuoteField | null;
  excerpt: AiExcerpt | null;
  output: AiRunOutputPointer | null;
  source_path: string;
  snapshot_at: ISODateTime;
  /** Extra facts the server adds per kind (all optional). */
  mesh_only?: boolean;
  asset_type?: string | null;
  availability?: string | null;
  publish_state?: string | null;
  file_kind?: string | null;
  /** workflow: pinned version (the key ends with `:v{version}`; status `stale` once the workflow moved on). */
  workflow_id?: UUID | null;
  version?: number | null;
  /** workflow: a part workflow (AI can plan changes), a project template or a collaboration draft (read only here). */
  workflow_scope?: 'part' | 'template' | 'collaboration' | null;
  /** S2: a material saved from an over-long request (read complete / as a digest / in chunks, never cut silently). */
  material?: boolean;
  chars?: number;
  /** workflow / run: the node the reference points at (null / absent = the whole object). */
  node?: AiRefNode | null;
  /** run: the run was started from a part workflow of the project (a change asked on it plans that workflow). */
  part_workflow?: boolean;
  instance?: { path: string[] };
  entities?: { ids: string[]; sheet_id?: string };
  assembly_id?: UUID;
  document_id?: UUID;
}

/** Computed when read, never stored in a snapshot (design §3.3). */
export interface AiRefStatus {
  state: 'ok' | 'stale' | 'unavailable';
  latest_label?: string | null;
  reason?: 'deleted' | 'forbidden' | 'archived' | string | null;
}

export interface AiComposer {
  target_key: string | null;
  references: AiRef[];
  ref_status: Record<string, AiRefStatus>;
}

export interface AiConstraint {
  id: string;
  text: string;
  source_seq: number | null;
  created_at: ISODateTime;
}

/**
 * AI-native main agent (.trellis/tasks/archive/2026-10/10-15-ai-native-orchestrator/design.md decision 6): how much the main agent does
 * before asking. auto — nothing asks; confirm_task (server default) — only creating a task asks; step — every write
 * step asks. Approvals, acceptance and deletion are never tools of the main agent.
 */
export const AI_AUTONOMY = ['auto', 'confirm_task', 'step'] as const;
export type AiAutonomy = (typeof AI_AUTONOMY)[number];

export interface AiConversation {
  id: UUID;
  title: string;
  project: { id: UUID; code: string; name: string };
  task: { id: UUID; title: string; status: string } | null;
  archived: boolean;
  archived_at: ISODateTime | null;
  constraints: AiConstraint[];
  summary_status: { through_seq: number; built_at: ISODateTime } | null;
  forked_from: { conversation_id: UUID; operation_id: UUID | null } | null;
  /** Absent on servers without the main agent. */
  autonomy?: AiAutonomy;
  version: number;
  last_activity_at: ISODateTime;
  created_at: ISODateTime;
}

export interface AiConversationSummary {
  id: UUID;
  title: string;
  project: { id: UUID; code: string; name: string };
  task: { id: UUID; title: string } | null;
  archived: boolean;
  last_activity_at: ISODateTime;
  message_count: number;
  active_operations: number;
  last_status: AiOperationStatus | null;
}

export interface AiMessage {
  id: UUID;
  seq: number;
  role: AiMessageRole;
  kind: AiMessageKind;
  text: string;
  target: AiRef | null;
  references: AiRef[];
  operation_id: UUID | null;
  /**
   * request: {client_request_id, requested_operation, operation, task_id, model_config_id, retry_of?};
   * operation: {operation}; choice: {choices, reason: "no_target", request_seq};
   * answer: {cited_keys, suggested_operation, follow_ups, simulated} (+ P4 tool-agent answers: runtime "tool_agent",
   * consulted [{call_index, tool, ok, kind?, id?, title?, page?, pages?, count?, error_code?}]);
   * notice: {code: "task_changed", task_id, previous_task_id} | {event: "sub_agent_finished", operation_id, …} (a
   * sub-agent the main agent started ended and woke it; answer payload.runtime "orchestrator" = the main agent's reply);
   * card: AiCardPayload (a run / workflow the conversation follows).
   */
  payload: Record<string, unknown>;
  created_at: ISODateTime;
  ref_status: Record<string, AiRefStatus>;
}

export interface AiRequestSnapshot {
  text: string;
  operation: AiOperationKind;
  requested_operation: AiRequestedOperation;
  target: AiRef | null;
  references: AiRef[];
  constraints: { id: string; text: string }[];
  task_id: UUID | null;
  model_config_id: UUID | null;
  client_request_id: UUID;
  /** W7: frozen with the request (absent on requests from before W7 = standard). */
  execution_policy?: 'standard' | 'turbo';
  /** S5: where it came from and the version the request continues from. */
  policy_source?: PolicySource | null;
  policy_source_version?: SourceVersion | null;
  /** S2: materials of the request (text saved from an over-long request, or one still referenced from an earlier one). */
  materials?: AiMaterial[];
  /** Generation supplement A-01: files the server bound because the request text pointed at them (also in references). */
  auto_references?: AiAutoReference[];
  /** Generation supplement I (A-04 / IN-07): the explicit choice next to a referenced spec version (absent = none needed). */
  revise?: boolean;
  spec_choice?: AiSpecChoice;
  /** Generation supplement I (IN-08): the inputs the request continued with after a source could not be read (append-only;
   * the request above is the original snapshot). */
  input_revisions?: AiInputRevision[];
  /** AI-native: "orchestrator" = a turn of the main agent (an `ask` operation; see orchestratorResult). */
  runtime?: 'orchestrator' | string;
  /** Main-agent turn: what started it (the person's request, a finished sub-agent, a confirmation). */
  trigger?: OrchestratorTrigger;
  /** Main-agent turn / sub-agent: the person's turn the chain started from; a sub-agent's spawning turn and step. */
  root_op_id?: UUID;
  parent_op_id?: UUID;
  spawned_by?: { operation_id: UUID; step: number };
}

// ─── AI-native main agent (.trellis/tasks/archive/2026-10/10-15-ai-native-orchestrator/design.md §2.3) ─────

export type OrchestratorTrigger =
  | { type: 'request' }
  | { type: 'sub_agent'; operation_id: UUID; status: AiOperationStatus; event_id?: UUID; generation?: number; outcome?: string }
  | { type: 'confirmed'; operation_id: UUID; step: number }
  | { type: 'design_choice'; operation_id: UUID; choice_id: UUID; question_set_id: string; answered_design_ref: DesignRef };

export type OrchestratorStepStatus = 'running' | 'ok' | 'failed' | 'waiting_confirm' | 'waiting_design' | 'declined' | 'superseded' | 'undone';

/** Objects a step created, started or read. */
export interface OrchestratorStepRefs {
  draft_id?: UUID | null;
  part_id?: UUID | null;
  task_id?: UUID | null;
  /** A sub-agent (child operation). */
  operation_id?: UUID | null;
  revision_id?: UUID | null;
  asset_id?: UUID | null;
  asset_version_id?: UUID | null;
  process_plan_id?: UUID | null;
  quote_id?: UUID | null;
}

/** One tool call of a turn, written as it happens (the 2 s polling shows the timeline live). */
export interface OrchestratorStep {
  index: number;
  tool: string;
  title: string;
  /** Clipped. */
  args: Record<string, unknown>;
  status: OrchestratorStepStatus;
  message: string;
  error?: { code: string; message: string } | null;
  refs: OrchestratorStepRefs;
  started_at: ISODateTime;
  finished_at: ISODateTime | null;
  /** The step can be undone now (POST …/steps/{index}/undo). */
  undo?: boolean;
}

export interface OrchestratorResult {
  kind: 'orchestrator';
  steps: OrchestratorStep[];
  trigger: OrchestratorTrigger;
  simulated: boolean;
  /** Exact immutable completion events supplied to this turn; absent in historical records. */
  completion_context?: UUID[];
  assumptions?: Assumption[];
}

/** A turn of the main agent: an `ask` operation run by the orchestrator runtime. */
export function isOrchestratorTurn(op: Pick<AiOperation, 'kind' | 'request'>): boolean {
  return op.kind === 'ask' && op.request?.runtime === 'orchestrator';
}

/** The step timeline of a main-agent turn (null for other operations or before its first step); kept out of AiResult. */
export function orchestratorResult(op: Pick<AiOperation, 'kind' | 'request' | 'result'>): OrchestratorResult | null {
  const r = op.result as { kind?: string } | null;
  return isOrchestratorTurn(op) && r?.kind === 'orchestrator' ? (r as unknown as OrchestratorResult) : null;
}

/** A task draft the main agent stored (private to the conversation until a task is created from it). */
export interface AiTaskDraft {
  id: UUID;
  conversation_id: UUID;
  /** The turn that drafted it, and its step. */
  operation_id: UUID | null;
  step_index: number | null;
  part_id: UUID | null;
  title: string;
  objective: string;
  /** The shape stages.draft_task returns. */
  criteria: TaskDraft['criteria'];
  suggested_workflow_id: UUID | null;
  /** open: editable; used: a task was created from it (task_id); discarded. */
  status: 'open' | 'used' | 'discarded';
  task_id: UUID | null;
  simulated: boolean;
  version: number;
  created_at: ISODateTime;
  updated_at: ISODateTime;
}

/** Objects the main agent created in this conversation (GET detail `work`). */
export interface AiWork {
  drafts: AiTaskDraft[];
  tasks: { id: UUID; number: string; title: string; status: string }[];
  parts: { id: UUID; number: string; name: string }[];
}

export interface AiTaskDraftPatch {
  expected_version: number;
  title?: string;
  objective?: string;
  criteria?: TaskDraft['criteria'];
}

/** A spec version referenced with new files / change wording: ``revise`` = a new version of that spec with the new
 * material (the text as a revision note); ``keep`` = the exact version, ``files`` left out. */
export interface AiSpecChoice {
  choice: 'revise' | 'keep';
  spec_version_id: UUID;
  spec_key: string | null;
  files: { key: string | null; file_version_id: UUID; label: string | null }[];
  text_changes: boolean;
}

/** One continue with replacement files (services/spec_inputs.py). */
export interface AiInputRevision {
  n: number;
  node_id: string;
  at: string;
  by: UUID;
  file_version_ids: UUID[];
  requested: UUID[];
  added: { file_version_id: UUID; name: string; version: number }[];
  dropped: UUID[];
  previous_file_version_ids: UUID[];
  base_spec_version_id: UUID | null;
  failure: { code: string | null; recovery: string | null };
}

/** Why the server bound a file to a request (design §2.5, in this order): named in the text, referenced earlier in
 * the conversation, the person's own upload in the last 24 hours. */
export type AiAutoReferenceReason = 'named_in_text' | 'referenced_before' | 'recent_upload';

/** Generation supplement A-01: a file bound to a request because its text pointed at an attachment. */
export interface AiAutoReference {
  key: string;
  file_version_id: UUID;
  name: string;
  version: number;
  reason: AiAutoReferenceReason | string;
}

/** POST /ai/conversations/{id}/attachments/resolve: what a send of this text would bind now (nothing is written). */
export interface AiAttachmentResolution {
  /** The text points at an attachment and the request holds no file version. */
  needed: boolean;
  status: 'not_needed' | 'unique' | 'ambiguous' | 'required';
  reason: AiAutoReferenceReason | null;
  /** References the composer can add (POST …/references with kind file_version). */
  candidates: (AiRef & { binding_reason?: AiAutoReferenceReason })[];
  recovery: 'choose_file' | 'attach_file' | null;
}

/** S2: a request text longer than the chat limit, saved as a project text file and referenced by the request. */
export interface AiMaterial {
  key: string;
  file_version_id: UUID;
  name: string;
  chars: number | null;
  /** The chat limit it was over. */
  limit: number;
  /** Saved from this request (false: still referenced from an earlier one). */
  new: boolean;
}

/** S2: how one material reached the model. */
export type AiMaterialMethod = 'full' | 'digest' | 'chunked' | 'spec_chunks' | 'not_included';

export interface AiMaterialPlacement {
  key: string;
  label: string;
  chars: number | null;
  method: AiMaterialMethod;
  tokens_est?: number;
  /** chunked: chunks read (one call each) and calls made; pending = still to be read. */
  chunks?: number;
  calls?: number;
  simulated?: boolean;
  pending?: boolean;
  /** not_included: why (unavailable / too_many_chunks / chunk_over_budget / notes_over_budget / not_read_by_operation …). */
  reason?: string;
  tried?: string[];
}

export interface AiLegacyQuestion {
  /** confirm: a main-agent turn waits for the person to confirm one write step (POST …/confirm). */
  kind: 'clarification' | 'manual_entry' | 'confirm';
  questions?: string[];
  /** clarification / manual_entry: the run node that asks. */
  node_id?: string;
  can_answer?: boolean;
  reason?: string;
  run_path?: string;
  /** manual_entry: the drawing file version being analysed. */
  file_version_id?: UUID | null;
  /** clarification: the critical gaps behind the questions (missing / assumed fields, sheet, units, conflicts). */
  gaps?: ClarificationGap[];
  /** confirm: the waiting step, its tool, what it does, its (clipped) arguments and, for create_task, the draft. */
  step?: number;
  tool?: string;
  title?: string;
  summary?: string;
  args?: Record<string, unknown>;
  draft?: AiTaskDraft | null;
}

/** Server-issued immutable design identity. Never substitute the current/latest version. */
export interface DesignRef {
  schema_version: 1;
  plan_id: UUID;
  version_id: UUID;
  version: number;
  sha256: string;
}

export type DesignValue = string | number | boolean;
export type DesignChoiceAction = 'customize' | 'delegate_remaining' | 'accept_recommended';
export type DesignAnswer =
  | { question_id: string; kind: 'option'; option_id: string }
  | { question_id: string; kind: 'value'; value: DesignValue };

export interface DesignQuestion {
  id: string;
  key: string;
  label: string;
  description: string;
  input_kind: 'string' | 'number' | 'boolean';
  unit: string | null;
  minimum: number | null;
  maximum: number | null;
  options: { id: string; label: string; value: DesignValue; reason?: string }[];
  recommended: { kind: 'option'; option_id: string } | { kind: 'value'; value: DesignValue } | null;
  allow_custom: boolean;
  required: boolean;
}

export interface DesignChoiceBody {
  client_request_id: UUID;
  design_ref: DesignRef;
  question_set_id: string;
  action: DesignChoiceAction;
  answers: DesignAnswer[];
  free_text?: string;
}

export interface DesignChoiceReceipt {
  client_request_id: UUID;
  question_set_id: string;
  answered_design_ref: DesignRef;
  /** Null until the continuation saves its validated new version. */
  design_ref: DesignRef | null;
  action: DesignChoiceAction;
  resume_operation_id: UUID;
}

export interface DesignChoiceQuestion {
  kind: 'design_choice';
  design_ref: DesignRef;
  question_set_id: string;
  title: string;
  description: string;
  questions: DesignQuestion[];
  can_answer: boolean;
  allowed_actions: DesignChoiceAction[];
  last_response: DesignChoiceReceipt | null;
}

export interface DesignPlanSummary {
  ref: DesignRef;
  title: string;
  summary: string;
  status: 'adopted' | 'awaiting_choice' | 'superseded';
  structure: { root: string; definitions: number; part_definitions: number; assembly_definitions: number; instances: number };
  decisions: { id: string; key: string; label: string; value: DesignValue; unit: string | null; reason: string; origin: 'ai_design' | 'user_input' | 'user_choice' }[];
  questions: DesignQuestion[];
  last_response: DesignChoiceReceipt | null;
}

export type AiQuestion = AiLegacyQuestion | DesignChoiceQuestion;

export type AiResultAction = 'view' | 'continue' | 'use_as_input' | 'add_deliverable' | 'open_run' | 'fork';

export interface AiResultItem {
  ref: AiRef;
  asset_version_id: UUID | null;
  asset_id: UUID | null;
  asset_type: string | null;
  label: string;
  base: AiRef | null;
  run_id: UUID | null;
  node_id: string | null;
  availability: string | null;
  publish_state: string | null;
  actions: AiResultAction[];
  output_key?: string | null;
  /** Quality / provenance labels of the asset version (simulated, estimated …). */
  labels?: string[];
  /** part_model items: the revision's requirement check (null = no requirement contract). */
  check_status?: CheckStatus | null;
}

/**
 * Result of a succeeded operation. Model / reconstruct results carry the modeling node's requirement check
 * (`check`, null when the node produced none); drawing operations the drawing node outputs (`drawings`);
 * reconstruction the drawing source summary (`source`).
 */
export type AiResult =
  | {
      kind: 'model' | 'intake' | 'asset' | 'reconstruct' | 'drawing' | 'assembly' | 'spec_extract' | 'drafting_create';
      items: AiResultItem[];
      check?: CheckSummary | null;
      drawings?: DrawingBrief[];
      source?: ReconstructSource | null;
      /** W8 assembly_create: every chain step with its recorded value, the assumptions and the open questions. */
      chain?: AiAssemblyChain;
      /** Exact saved specification, including readiness and unanswered questions. */
      spec?: SpecValue;
    }
  | { kind: 'task_draft'; draft: TaskDraft };

// ─── W8 assembly operations (services/assembly_ai.py) ─────────────────────

export interface AiAssemblyChainStep {
  node_id: 'spec' | 'assembly' | 'drawings' | 'delivery' | string;
  node_type: string;
  status: string;
  value: ChainValue | null;
  error: { code: string | null; message: string | null } | null;
}

export interface AiAssemblyChain {
  steps: AiAssemblyChainStep[];
  assumptions: Assumption[];
  questions: { source: string; id?: string; subject?: string | null; question?: string | null; definition?: string }[];
  execution_policy: 'standard' | 'turbo';
  run_status: RunStatus;
}

/** The plan of an assembly_modify operation: W4 edit ops on an exact base revision, previewed, applied on request. */
export interface AiAssemblyModifyResult {
  kind: 'assembly_modify';
  assembly_id: UUID;
  assembly_number: string;
  assembly_name: string;
  base_revision_id: UUID;
  base_rev_no: number;
  /** Server digest of the validated executable plan; absent on historical plans. */
  plan_hash?: string;
  summary: string;
  ops: ({ op: string } & Record<string, unknown>)[];
  questions: string[];
  impact: {
    affected_instances: string[];
    bom_changes: { def_key: string; name: string | null; from: number; to: number }[];
    notes: { code: string; message: string }[];
    stats: Record<string, number>;
    will_be_stale: { kind: string | null; stale: boolean }[];
  } | null;
  removals: { op: string; instance?: string | null; mate_id?: string | null; removed?: string[] }[];
  execution_policy: 'standard' | 'turbo';
  application: { state: 'applied'; revision_id: UUID; rev_no: number; check_status: string; at: ISODateTime } | null;
}

/** The plan of an assembly_modify operation (null for other kinds or before it succeeded); kept out of AiResult. */
export function assemblyModifyResult(op: Pick<AiOperation, 'kind' | 'result'>): AiAssemblyModifyResult | null {
  const r = op.result as { kind?: string } | null;
  return op.kind === 'assembly_modify' && r?.kind === 'assembly_modify' ? (r as unknown as AiAssemblyModifyResult) : null;
}

/** Apply an assembly change plan: a new revision against the plan's base. 409 revision_conflict (the assembly moved
 * on: ask again), removals_need_confirmation (pass confirmRemovals), already_applied, nothing_to_apply. */
export const applyAssemblyPlan = (id: UUID, body: AiApplyBody, o: Opt = {}) =>
  request<{ operation: AiOperation; revision: { id: UUID; rev_no: number; assembly_id: UUID } | null }>(`/ai/operations/${enc(id)}/apply`, {
    method: 'POST',
    body,
    signal: o.signal,
  });

// ─── workflow_edit (services/assistant.py _run_workflow_edit, workflow/edit.py) ─────

/** One change of a plan, applied in order (workflow/edit.py Change). */
export type AiWorkflowChange =
  | {
      op: 'add_node';
      node_id?: string;
      type: string;
      label?: string;
      params?: Record<string, unknown>;
      position?: { x: number; y: number };
      /** Placed after this node; connected from it when the plan adds no edge into the new node. */
      after?: string;
    }
  | { op: 'remove_node'; node_id: string }
  | { op: 'add_edge'; source: string; source_port: string; target: string; target_port: string }
  | { op: 'remove_edge'; edge_id?: string; source?: string; source_port?: string; target?: string; target_port?: string }
  /** Merged into the node's params (a null value resets one to its default). */
  | { op: 'set_params'; node_id: string; params: Record<string, unknown> }
  | { op: 'rename_node'; node_id: string; label: string };

/** What the plan changes, computed from the base graph and the planned graph (edges compared by connection). */
export interface AiWorkflowDiff {
  added_nodes: { id: string; type: string; label: string | null; params: Record<string, unknown>; position: { x: number; y: number } }[];
  removed_nodes: { id: string; type: string; label: string | null }[];
  /** Only the keys that differ; a key missing from `after` went back to its default. */
  changed_params: { node_id: string; type: string; keys: string[]; before: Record<string, unknown>; after: Record<string, unknown> }[];
  added_edges: WorkflowEdge[];
  removed_edges: WorkflowEdge[];
  renamed: { node_id: string; before: string | null; after: string | null }[];
  /** Nothing would change (apply answers 409 nothing_to_apply). */
  empty: boolean;
}

/**
 * Notes on a plan. Validation codes (missing_param: a file / revision / text the user fills in; others with
 * `preexisting: true`: the workflow already had them), removes_review (an approval step is removed),
 * workflow_moved_on (planned on a newer version than the reference), private_context (copies text from the
 * private conversation context; applying makes it visible to the project).
 */
export interface AiWorkflowEditWarning {
  code: string;
  message: string;
  node_id?: string;
  edge_id?: string;
  param?: string;
  port?: string;
  preexisting?: boolean;
  node_ids?: string[];
  pinned_version?: number;
  base_version?: number;
}

export interface AiWorkflowEditApplication {
  state: 'applied' | 'undone';
  by: UUID;
  at: ISODateTime;
  version_before: number;
  version_after: number;
  undone_by?: UUID;
  undone_at?: ISODateTime;
  /** The new version undo wrote (the base graph restored; history is never rewritten). */
  undo_version?: number;
}

/** A succeeded workflow_edit operation = a plan ready to apply (nothing written until applyAiOperation). */
export interface AiWorkflowEditResult {
  kind: 'workflow_edit';
  workflow_id: UUID;
  workflow_name: string;
  part_id: UUID;
  /** The workflow version the plan was made on; apply requires the workflow to still be at it. */
  base_version: number;
  plan_hash?: string;
  /** The version pinned by the reference (may be older than base_version, see warning workflow_moved_on). */
  pinned_version: number | null;
  summary: string;
  changes: AiWorkflowChange[];
  diff: AiWorkflowDiff;
  graph_before: WorkflowGraph;
  graph_after: WorkflowGraph;
  warnings: AiWorkflowEditWarning[];
  /** Repair calls used before the plan applied cleanly. */
  repairs: number;
  application: AiWorkflowEditApplication | null;
  /** The node the request was about (a workflow / run node target); null when the request named the whole workflow. */
  focus_node_id?: string | null;
  /** The target was a run: the plan changes the run's part workflow for the NEXT run (warning run_unchanged). */
  from_run?: { run_id: UUID; workflow_version: number | null } | null;
}

/** A succeeded workflow_create operation: a new part workflow planned from a built-in template (nothing written
 * until applyAiOperation, which creates the workflow, adds a workflow card and makes it the composer target). */
export interface AiWorkflowCreateResult {
  kind: 'workflow_create';
  part_id: UUID;
  part_number: string;
  name: string;
  template_key: string;
  plan_hash?: string;
  summary: string;
  /** Relative to the template graph. */
  changes: AiWorkflowChange[];
  diff: AiWorkflowDiff;
  graph_after: WorkflowGraph;
  warnings: AiWorkflowEditWarning[];
  repairs: number;
  application: { state: 'applied'; workflow_id: UUID; version: number; by: UUID; at: ISODateTime } | null;
}

export function workflowCreateResult(op: Pick<AiOperation, 'kind' | 'result'>): AiWorkflowCreateResult | null {
  const r = op.result as { kind?: string } | null;
  return op.kind === 'workflow_create' && r?.kind === 'workflow_create' ? (r as unknown as AiWorkflowCreateResult) : null;
}

/** POST /ai/operations/{id}/apply | undo. */
export interface AiWorkflowEditApplyResult {
  operation: AiOperation;
  workflow: Workflow;
}

/** Human application of the exact displayed plan. New plans bind a lowercase SHA-256 digest. */
export interface AiApplyBody {
  plan_hash?: string;
  expected_head?: UUID;
  expected_version?: number;
  confirm_removals?: boolean;
}

export interface AiDraftingEditPlan {
  schema_version: 1;
  document_id: UUID;
  base_version_id: UUID;
  base_version_no: number;
  state_sha256: string;
  selection: { ids: string[]; sheet_id?: string | null } | null;
  commands: Exclude<DraftCommand, { op: 'calibrate' | 'resolve_conflict' | 'confirm_assumption' }>[];
  affected_entity_ids: string[];
  removed_entity_ids: string[];
  summary: string;
  changes: string[];
  warnings: Record<string, unknown>[];
  questions: string[];
}

export interface AiDraftingModifyResult {
  kind: 'drafting_modify';
  document_id: UUID;
  base_version_id: UUID;
  plan_hash: string;
  plan: AiDraftingEditPlan;
  summary: string;
  changes: string[];
  warnings: Record<string, unknown>[];
  questions: string[];
  removals: string[];
  application: { state: 'applied' | 'undone'; by: UUID; at: ISODateTime; plan_hash: string; expected_head: UUID; version_id: UUID; removals_confirmed: boolean } | null;
  drawing_document_version_id?: UUID;
  version_no?: number;
  cad_result?: CadResult;
}

export function draftingModifyResult(op: Pick<AiOperation, 'kind' | 'result'>): AiDraftingModifyResult | null {
  const result = op.result as unknown as Partial<AiDraftingModifyResult> | null;
  return op.kind === 'drafting_modify' && result?.kind === 'drafting_modify' && result.plan?.schema_version === 1
    ? result as AiDraftingModifyResult : null;
}

export const applyDraftingPlan = (id: UUID, body: AiApplyBody, o: Opt = {}) =>
  request<{ operation: AiOperation }>(`/ai/operations/${enc(id)}/apply`, { method: 'POST', body, signal: o.signal });

/** Payload of a `card` message: the object the card shows (its live state is read from the object itself). */
export interface AiCardPayload {
  object: { kind: 'run' | 'workflow'; id: UUID };
  origin: 'run_started' | 'workflow_created';
  client_request_id?: UUID;
  /** run cards: the workflow the run was started from. */
  workflow_id?: UUID | null;
}

export function cardPayload(m: Pick<AiMessage, 'kind' | 'payload'>): AiCardPayload | null {
  if (m.kind !== 'card') return null;
  const o = (m.payload as { object?: { kind?: string; id?: string } } | null)?.object;
  return o && (o.kind === 'run' || o.kind === 'workflow') && typeof o.id === 'string' ? (m.payload as unknown as AiCardPayload) : null;
}

/**
 * The plan of a workflow_edit operation (null for other kinds or before it succeeded). Kept out of the AiResult
 * union so code narrowing AiResult by `kind` keeps compiling; read workflow_edit results through this.
 */
export function workflowEditResult(op: Pick<AiOperation, 'kind' | 'result'>): AiWorkflowEditResult | null {
  const r = op.result as { kind?: string } | null;
  return op.kind === 'workflow_edit' && r?.kind === 'workflow_edit' ? (r as unknown as AiWorkflowEditResult) : null;
}

export interface AiError {
  code: string;
  message: string;
  details?: Record<string, unknown>;
  /** S3 failure info of the failed step (design §3.1), when the step recorded it. */
  stage?: string;
  retryable?: boolean;
  recovery?: string;
  recovery_message?: string;
}

export interface AiContextItem {
  layer: 'required' | 'memory' | 'retrieved';
  source: string;
  label: string;
  tokens_est: number;
  /** Size of the item text in characters (the text itself is not stored). */
  chars?: number;
  included: boolean;
  ref_key: string | null;
  /** Why it is not included, or "clipped" when only part of its text was used. */
  reason: string | null;
  /** Only part of the item's text was used (reason "clipped"). */
  truncated?: boolean;
  /** W2: read evidence ("content" / "partial" with ranges) or a metadata-only reference ("metadata"). */
  read?: 'content' | 'partial' | 'metadata';
  ranges?: string[];
  /** S2 (source "material"): how the material was placed. */
  method?: AiMaterialMethod;
}

export interface AiContextBudget {
  input_limit: number;
  output_reserve: number;
  safety: number;
  available: number;
  required_est: number;
  used_est: number;
  limit_source: 'builtin_table' | 'capabilities' | string;
  /** Always true: these numbers are estimates, not provider counts. */
  estimated: true;
  /** S2: how each material of the request was placed. */
  materials?: AiMaterialPlacement[];
}

export interface AiContextUsage {
  tokens_in: number | null;
  tokens_out: number | null;
  /** True when at least one call did not report usage. */
  unknown: boolean;
  calls: number;
  /** Simulated provider: nothing was counted (tokens null, unknown true). */
  simulated?: boolean;
  /** Where the numbers come from; only the provider's own counts are shown as real usage. */
  source?: 'provider' | string;
}

/** details of error code `context_over_budget` (required content does not fit; nothing is truncated). */
export interface AiOverBudgetDetails {
  required_est: number;
  available: number;
  largest: { label: string; tokens_est: number; source?: string }[];
  suggestions: { code: string; message: string }[];
  /** Which limit was exceeded. */
  limit: 'input' | 'output' | 'context_chars' | 'material' | string;
  operation?: string;
  estimated: true;
  /** S2 (limit "material"): the materials that could not be included in any form. */
  not_included?: { key: string; label: string; chars: number | null; reason?: string; tried?: string[] }[];
}

export interface AiContextBuild {
  id: UUID;
  ok: boolean;
  /** "Label (model_id)", or "simulated" / "unavailable". */
  model_label: string;
  budget: AiContextBudget;
  items?: AiContextItem[];
  error?: AiError | null;
  summary_through_seq?: number | null;
  usage: AiContextUsage | null;
  created_at?: ISODateTime;
}

export interface CadResult {
  event_id: string;
  object_type: 'part_model' | 'part_drawing' | 'drawing_document' | 'assembly';
  object_id: UUID;
  base_version_id: UUID | null;
  version_id: UUID;
  version_label: string;
  asset_version_id: UUID | null;
  requirements: Record<string, unknown>[];
  changes: Record<string, unknown>[];
  checks: Record<string, unknown>;
  artifacts: { kind: string; sha256: string; filename?: string }[];
  undo: { eligible: boolean; reason?: string; expected_head: UUID; restored?: { version_id: UUID; version_label: string; asset_version_id: UUID | null } };
}
export const undoCadOperation = (id: UUID, expectedHead: UUID, o: Opt = {}) =>
  request<{ operation: AiOperation }>(`/ai/operations/${enc(id)}/undo`, { method: 'POST', body: { expected_head: expectedHead }, signal: o.signal });

/** Immutable outcome identity with a separately evolving delivery state. */
export interface AiCompletionEvent {
  event_id: UUID;
  generation: number;
  event_seq: number;
  phase: 'terminal' | 'applied' | 'undone';
  outcome: string;
  wake_state: 'pending' | 'assigned' | 'consumed' | 'suppressed' | 'legacy_consumed' | 'delivery_failed';
  notice_seq: number | null;
}

export interface AiOperation {
  id: UUID;
  conversation_id: UUID;
  message_id: UUID;
  kind: AiOperationKind;
  status: AiOperationStatus;
  /** Absent on historical API snapshots; their generation is 1. */
  generation?: number;
  completion_events?: AiCompletionEvent[];
  /** W7: the frozen policy of the request and its run (standard for older operations). */
  execution_policy?: 'standard' | 'turbo';
  /** S5: where that policy came from (null for requests from before S5). */
  policy_source?: PolicySource | null;
  request: AiRequestSnapshot;
  /* context_build in a conversation detail has no `items`; GET /ai/operations/{id} includes them. */
  task: { id: UUID; title: string; status?: string } | null;
  part: { id: UUID; name: string; number?: string | null } | null;
  /** ``budget``: the run's frozen caps (generation reliability J; no live counts). */
  run: { id: UUID; status: RunStatus; title: string; cancel_requested?: boolean; budget?: { max_calls?: number | null; max_seconds?: number | null } | null } | null;
  question: AiQuestion | null;
  /** workflow_edit operations carry an AiWorkflowEditResult instead: read it with workflowEditResult(op). */
  result: (AiResult & { cad_result?: CadResult; design_plan?: DesignPlanSummary }) | null;
  error: AiError | null;
  /** W1: code, stage, purpose, exact configuration, call ids and recovery of an AI failure (null for business refusals). */
  diagnostic?: AiDiagnostic | null;
  simulated: boolean;
  retry_of: UUID | null;
  /** The operation that retried this one (a failed / cancelled operation is retried at most once). */
  retried_by?: UUID | null;
  /** AI-native: a sub-agent — the main-agent turn that started it. */
  parent_operation_id?: UUID | null;
  created_at: ISODateTime;
  updated_at: ISODateTime;
  finished_at: ISODateTime | null;
  context_build: AiContextBuild | null;
}

export interface AiConversationDetail {
  conversation: AiConversation;
  messages: AiMessage[];
  operations: AiOperation[];
  composer: AiComposer;
  /** AI-native: drafts, tasks and parts the main agent created here (absent on servers without it). */
  work?: AiWork;
}

export interface AiSendResult {
  message: AiMessage;
  operation: AiOperation | null;
  assistant_message: AiMessage | null;
}

export interface AiCapabilities {
  operations: Record<
    AiOperationKind,
    {
      available: boolean;
      /** simulation_unsupported: drawing_reconstruct needs a configured model (the simulated one cannot read drawings). */
      reason: 'no_permission' | 'ai_unavailable' | 'project_inactive' | 'simulation_unsupported' | null;
      /** This kind would run on the labelled simulated provider. */
      simulated?: boolean;
    }
  >;
  /** True only when the simulated provider will really be used (per-operation / per-answer flags stay the source of truth on cards). */
  simulation: boolean;
  ready_configs: number;
  /** AI-native: `auto` requests go to the main agent (its autonomy can be set per conversation). */
  orchestrator?: boolean;
  /** AI-native: the autonomy a new conversation starts with (the person's last one, else confirm_task). */
  autonomy_default?: AiAutonomy;
}

export interface AiSummary {
  method: 'digest';
  goals: string[];
  constraints: string[];
  decisions: { text: string; operation_id: UUID | null; base: string | null; result: string | null }[];
  open_questions: string[];
  version_refs: string[];
  sources: number[];
  through_seq: number;
  built_at: ISODateTime;
}

export interface AiConversationContext {
  summary: AiSummary | null;
  summary_through_seq: number;
  constraints: AiConstraint[];
  last_build: (AiContextBuild & { operation_id: UUID }) | null;
}

// ─── requests ──────────────────────────────────────────────────────────────

export const getAiCapabilities = (projectId: UUID, o: Opt = {}) =>
  request<AiCapabilities>(`/ai/capabilities?project_id=${enc(projectId)}`, { signal: o.signal });

export const listAiConversations = (
  f: { project_id?: UUID; task_id?: UUID; archived?: 'false' | 'true' | 'all'; limit?: number } = {},
  o: Opt = {},
) => {
  const q = new URLSearchParams();
  if (f.project_id) q.set('project_id', f.project_id);
  if (f.task_id) q.set('task_id', f.task_id);
  if (f.archived) q.set('archived', f.archived);
  if (f.limit) q.set('limit', String(f.limit));
  const s = q.toString();
  return request<AiConversationSummary[]>(`/ai/conversations${s ? `?${s}` : ''}`, { signal: o.signal });
};

export const createAiConversation = (body: { project_id: UUID; task_id?: UUID | null; title?: string; autonomy?: AiAutonomy }, o: Opt = {}) =>
  request<AiConversationDetail>('/ai/conversations', { method: 'POST', body, signal: o.signal });

export const getAiConversation = (id: UUID, o: Opt & { afterSeq?: number } = {}) =>
  request<AiConversationDetail>(`/ai/conversations/${enc(id)}${o.afterSeq ? `?after_seq=${o.afterSeq}` : ''}`, { signal: o.signal });

export const patchAiConversation = (
  id: UUID,
  body: {
    title?: string;
    task_id?: UUID | null;
    archived?: boolean;
    constraints?: { id?: string; text: string; source_seq?: number | null }[];
    autonomy?: AiAutonomy;
    expected_version?: number;
  },
  o: Opt = {},
) => request<AiConversationDetail>(`/ai/conversations/${enc(id)}`, { method: 'PATCH', body, signal: o.signal });

export interface AiAddReferenceResult {
  composer: AiComposer;
  added: AiRef;
  notice: 'target_preselected' | 'target_kept' | 'target_set' | 'already_added' | null;
}

export const addAiReference = (id: UUID, body: { ref: AiRefInput; as_target?: boolean }, o: Opt = {}) =>
  request<AiAddReferenceResult>(`/ai/conversations/${enc(id)}/references`, { method: 'POST', body, signal: o.signal });

export const removeAiReference = (id: UUID, key: string, o: Opt = {}) =>
  request<AiComposer>(`/ai/conversations/${enc(id)}/references/${enc(key)}`, { method: 'DELETE', signal: o.signal });

export const setAiTarget = (id: UUID, key: string | null, o: Opt = {}) =>
  request<AiComposer>(`/ai/conversations/${enc(id)}/target`, { method: 'PUT', body: { key }, signal: o.signal });

export interface AiSendBody {
  client_request_id: UUID;
  text: string;
  operation: AiRequestedOperation;
  /** Omitted = the composer's target; null = no target. */
  target_key?: string | null;
  /** Omitted = every composer reference except the target. */
  reference_keys?: string[];
  model_config_id?: UUID | null;
  /** Omitted = the conversation's task. */
  task_id?: UUID | null;
  /** W7 / S5: the explicit choice; omitted = inherited from the version the request continues from (its target, or the
   * spec version an assembly_create references), else the project default, else standard. Same client_request_id +
   * another explicit policy = 409 idempotency_conflict. */
  execution_policy?: 'standard' | 'turbo';
  /** S2: consent that a text over the chat limit is saved as a project material (members can see it); without it the
   * server answers 422 material_consent_required. */
  as_material?: boolean;
  /**
   * Generation supplement A-01: false = the composer already resolved the attachment wording and the person decided the
   * references (the server adds nothing back); omitted = the server binds the file the text points at. 422
   * attachment_ambiguous (details.candidates) / attachment_required / spec_and_sources refuse a send before anything is
   * written.
   */
  auto_references?: boolean;
  /** Generation supplement I (A-04 / IN-07): the answer to spec_and_sources — true = a new version of the referenced spec
   * with the new material, false = build from the exact version (the new material left out, shown on the card). */
  revise?: boolean;
}

/** 409 agent_busy while a main-agent turn of the conversation is pending / running (one active turn per conversation). */
export const sendAiMessage = (id: UUID, body: AiSendBody, o: Opt = {}) =>
  request<AiSendResult>(`/ai/conversations/${enc(id)}/messages`, { method: 'POST', body: operationChoices(body), signal: o.signal });

/** Generation supplement A-01: which file a send of this text would bind now (read only; same access as the conversation). */
export const resolveAiAttachments = (id: UUID, body: { text: string; target_key?: string | null; reference_keys?: string[]; operation?: string }, o: Opt = {}) =>
  request<AiAttachmentResolution>(`/ai/conversations/${enc(id)}/attachments/resolve`, { method: 'POST', body, signal: o.signal });

/** Generation reliability K (design §2.3): the kind of object an operation works on. A task is never a model or a file. */
export type AiPreflightTargetType =
  | 'part' | 'model_revision' | 'step_file' | 'drawing_file' | 'assembly_revision' | 'spec_version' | 'task' | 'file' | 'workflow' | 'run' | 'none';

export interface AiPreflightTarget {
  type: AiPreflightTargetType;
  id: string | null;
  label: string;
  version: string | null;
  key?: string | null;
  /** e.g. ``latest_revision`` (a part's latest model is drawn), ``run`` (the part workflow of that run). */
  via?: string;
}

/** What stops a send before any model call, and the entry that fixes it. */
export interface AiPreflightMissing {
  code: string;
  message: string;
  entry: { action: string; kind?: string; object_id?: string; label?: string; candidates?: AiRef[]; [k: string]: unknown };
  expected: string[];
}

export interface AiPreflight {
  /** Null and classified/choices are accepted only for historical responses. */
  operation: AiOperationKind | null;
  requested_operation: AiRequestedOperation;
  operation_source: 'main_agent' | 'explicit' | 'fallback' | 'classified';
  runtime?: 'orchestrator';
  target: AiPreflightTarget;
  references: { key: string | null; kind: string; type: AiPreflightTargetType; label: string; version: string | null; auto?: boolean; reason?: string }[];
  missing: AiPreflightMissing[];
  policy: 'standard' | 'turbo';
  policy_source: string;
  will_call_model: boolean;
  /** Historical preflight payload only; never a live operation selector. */
  choices?: AiOperationKind[];
}

/** POST …/preflight: the send body without its client_request_id. Read only; no model call. */
export const preflightAiMessage = (id: UUID, body: Omit<AiSendBody, 'client_request_id'>, o: Opt = {}) =>
  request<AiPreflight>(`/ai/conversations/${enc(id)}/preflight`, { method: 'POST', body, signal: o.signal });

export const forkAiConversation = (id: UUID, body: { operation_id?: UUID; reference_keys?: string[]; title?: string }, o: Opt = {}) =>
  request<AiConversationDetail>(`/ai/conversations/${enc(id)}/fork`, { method: 'POST', body, signal: o.signal });

export const getAiConversationContext = (id: UUID, o: Opt = {}) =>
  request<AiConversationContext>(`/ai/conversations/${enc(id)}/context`, { signal: o.signal });

export const getAiOperation = (id: UUID, o: Opt = {}) => request<AiOperation>(`/ai/operations/${enc(id)}`, { signal: o.signal });

export const answerAiOperation = (id: UUID, answers: string[], o: Opt = {}) =>
  request<AiOperation>(`/ai/operations/${enc(id)}/answer`, { method: 'POST', body: { answers }, signal: o.signal });

/** Answer only the displayed design question/version. The request body is retained intact for replay. */
export const answerAiDesignChoice = (id: UUID, body: DesignChoiceBody, o: Opt = {}) =>
  request<AiOperation>(`/ai/operations/${enc(id)}/design-choice`, { method: 'POST', body, signal: o.signal });

/** Also stops a main-agent turn (before its next step; its active sub-agents are cancelled too). */
export const cancelAiOperation = (id: UUID, o: Opt = {}) =>
  request<AiOperation>(`/ai/operations/${enc(id)}/cancel`, { method: 'POST', body: {}, signal: o.signal });

/**
 * Answer a main-agent turn waiting on a confirmation (question kind `confirm`): accept runs the step as the person and
 * queues the next turn; decline marks the step declined (no model call). Returns the turn; 409 not_waiting otherwise
 * (replay-safe).
 */
export const confirmAiOperation = (id: UUID, accept: boolean, o: Opt = {}) =>
  request<AiOperation>(`/ai/operations/${enc(id)}/confirm`, { method: 'POST', body: { accept }, signal: o.signal });

/** Undo one step of a main-agent turn (only while its `undo` is true); returns the turn. */
export const undoAiStep = (id: UUID, index: number, o: Opt = {}) =>
  request<AiOperation>(`/ai/operations/${enc(id)}/steps/${index}/undo`, { method: 'POST', body: {}, signal: o.signal });

/** Edit an open task draft (conversation owner). 409 version_conflict (changed meanwhile) / draft_closed. */
export const patchTaskDraft = (id: UUID, body: AiTaskDraftPatch, o: Opt = {}) =>
  request<AiTaskDraft>(`/ai/task-drafts/${enc(id)}`, { method: 'PATCH', body, signal: o.signal });

export const discardTaskDraft = (id: UUID, o: Opt = {}) =>
  request<AiTaskDraft>(`/ai/task-drafts/${enc(id)}/discard`, { method: 'POST', body: {}, signal: o.signal });

/**
 * S3: continue a failed assembly_create from its failed chain step in its own run (409 not_continuable / already_retried).
 * Generation supplement I (IN-08): ``fileVersionIds`` — a spec step that could not read a source runs again in the same
 * run with these files (the unread ones dropped); the same files again while it runs return the request as it is.
 */
export const continueAiOperation = (id: UUID, o: Opt & { fileVersionIds?: UUID[] } = {}) =>
  request<AiOperation>(`/ai/operations/${enc(id)}/continue`, {
    method: 'POST',
    ...(o.fileVersionIds ? { body: { file_version_ids: o.fileVersionIds } } : {}),
    signal: o.signal,
  });

export const retryAiOperation = (id: UUID, clientRequestId: UUID, o: Opt = {}) =>
  request<AiSendResult>(`/ai/operations/${enc(id)}/retry`, { method: 'POST', body: { client_request_id: clientRequestId }, signal: o.signal });

/**
 * Apply a workflow_edit plan: the planned graph becomes the part workflow's next version (only the next run uses it).
 * The conversation's composer reference to the workflow is re-pinned to the new version (reload the conversation).
 * 409: workflow_changed (the workflow is no longer at the plan's base version or at `expectedVersion`; ask for a new
 * plan), already_applied, already_undone, nothing_to_apply, plan_not_ready, not_workflow_edit; 403 without the
 * workflow permission.
 */
export const applyAiOperation = (id: UUID, body: AiApplyBody = {}, o: Opt = {}) =>
  request<AiWorkflowEditApplyResult>(`/ai/operations/${enc(id)}/apply`, {
    method: 'POST',
    body,
    signal: o.signal,
  });

/**
 * AI work panel: start a run of a part workflow from the conversation. The run and a `card` message are written
 * together; the same client_request_id returns the same message and run. Same checks as POST /workflows/{id}/runs;
 * 409 workflow_edit_part_only for a workflow that is not a part workflow of the conversation's project.
 */
export const startConversationRun = (
  conversationId: UUID,
  body: { workflow_id: UUID; client_request_id: UUID; execution_policy?: 'standard' | 'turbo' },
  o: Opt = {},
) => request<{ message: AiMessage; run: WorkflowRun }>(`/ai/conversations/${enc(conversationId)}/workflow-runs`, { method: 'POST', body: operationChoices(body), signal: o.signal });

/** AI work panel: re-pin the composer's references to this workflow (and its nodes) to its current version. */
export const repinComposer = (conversationId: UUID, workflowId: UUID, o: Opt = {}) =>
  request<AiComposer>(`/ai/conversations/${enc(conversationId)}/composer/repin`, { method: 'POST', body: { workflow_id: workflowId }, signal: o.signal });

/** Undo an applied plan (the base graph as a new version). 409 workflow_changed once the workflow moved on, not_applied, already_undone. */
export const undoAiOperation = (id: UUID, o: Opt = {}) =>
  request<AiWorkflowEditApplyResult>(`/ai/operations/${enc(id)}/undo`, { method: 'POST', body: {}, signal: o.signal });

// Agent executions (agent execution layer P1). Types mirror the binding contract
// .trellis/tasks/10-11-agent-execution-layer/design.md §6.4 (event types and payloads) and §6.5 (endpoints).
// When the server builders change, change them here.

import type { AgentRuntime } from './agents';
import { ApiError, buildUrl, currentToken, isAbortError, parseErrorBody, request } from './client';
import type { CriterionResult, ExecutionRole } from './collaboration';
import { createSseParser } from './endpoints';
import type { ISODateTime, ModelConfig, UUID } from './types';

type Opt = { signal?: AbortSignal };
const enc = encodeURIComponent;

// ─── events (§6.4) ─────────────────────────────────────────────────────────

export const EXEC_EVENT_TYPES = [
  'system',
  'model_call',
  'message',
  'tool_call',
  'tool_result',
  'question',
  'deliver',
  'blocked',
  'verdict',
  'status',
  // P3 workspace agent (design §8)
  'command',
  'file_change',
  'confirmation',
] as const;
export type ExecEventType = (typeof EXEC_EVENT_TYPES)[number];

/** Every payload carries `simulated` (true for simulated runs). */
interface Sim {
  simulated?: boolean;
}

/** code: started / lease_lost / paused / stopped / budget_exceeded / timeout / retry / failed … */
export interface SystemPayload extends Sim {
  code: string;
  message: string;
  detail?: unknown;
}
export interface ModelCallPayload extends Sim {
  call_id: string;
  model_id: string;
  tokens_in: number | null;
  tokens_out: number | null;
  latency_ms: number | null;
  first_token_ms?: number | null;
  finish_reason?: string | null;
}
/** The model's explanation or plan, truncated to 4,000 characters; never the prompt. */
export interface MessagePayload extends Sim {
  text: string;
  call_id?: string | null;
}
export interface ToolCallPayload extends Sim {
  call_index: number;
  name: string;
  /** Truncated by the server (_safe_args). */
  args: Record<string, unknown>;
}
export interface ProducedVersion {
  asset_version_id: UUID;
  asset_type: string;
  label: string;
}
export interface ToolResultPayload extends Sim {
  call_index: number;
  name: string;
  ok: boolean;
  message: string;
  error?: { code?: string | null; message?: string | null } | string | null;
  produced: ProducedVersion[];
  duration_ms: number | null;
  ai_calls: number | null;
  /** Generation reliability N: how the call ran (policy and its source, inputs, target, budget, checks). */
  execution?: Record<string, unknown> | null;
}
export interface QuestionPayload extends Sim {
  questions: unknown[];
}
export interface DeliverPayload extends Sim {
  deliverables: { asset_version_id?: UUID; slot_key?: string; [k: string]: unknown }[];
  note: string;
}
export interface BlockedPayload extends Sim {
  reason: string;
  missing: string[];
}
export interface VerdictPayload extends Sim {
  criteria: { criterion_id: UUID; result: CriterionResult | string; reason: string }[];
  summary: string;
}
export interface StatusPayload extends Sim {
  status: string;
}
/** A workspace terminal command (P3). output_head: the first lines of its output, secrets removed. */
export interface CommandPayload extends Sim {
  command: string;
  exit_code: number | null;
  output_head: string;
  duration_ms: number | null;
  timeout?: boolean;
}
/** A file the workspace agent created or edited (P3); path relative to the working directory. */
export interface FileChangePayload extends Sim {
  path: string;
  command?: string;
  added: number;
  removed: number;
  diff_head: string;
}
export type ConfirmationState = 'pending' | 'approved' | 'rejected';
/** A workspace action waiting for a person, and later the decision (same confirmation_id). risk: low / medium / high / unknown. */
export interface ConfirmationPayload extends Sim {
  confirmation_id: string;
  state: ConfirmationState | string;
  action?: string | null;
  risk?: string | null;
  detail?: { tools?: string[]; actions?: string[] } | null;
  reason?: string | null;
  by?: { id: UUID; name: string } | null;
}

interface EventOf<T extends ExecEventType, P> {
  seq: number;
  type: T;
  at: ISODateTime | null;
  payload: P;
}

export type KnownExecutionEvent =
  | EventOf<'system', SystemPayload>
  | EventOf<'model_call', ModelCallPayload>
  | EventOf<'message', MessagePayload>
  | EventOf<'tool_call', ToolCallPayload>
  | EventOf<'tool_result', ToolResultPayload>
  | EventOf<'question', QuestionPayload>
  | EventOf<'deliver', DeliverPayload>
  | EventOf<'blocked', BlockedPayload>
  | EventOf<'verdict', VerdictPayload>
  | EventOf<'status', StatusPayload>
  | EventOf<'command', CommandPayload>
  | EventOf<'file_change', FileChangePayload>
  | EventOf<'confirmation', ConfirmationPayload>;

/** A wire event: one of the known types, or a later type shown generically. */
export interface ExecutionEvent {
  seq: number;
  type: string;
  at: ISODateTime | null;
  payload: Record<string, unknown>;
}

const KNOWN: ReadonlySet<string> = new Set(EXEC_EVENT_TYPES);
export function asKnown(e: ExecutionEvent): KnownExecutionEvent | null {
  return KNOWN.has(e.type) ? (e as unknown as KnownExecutionEvent) : null;
}

export interface ExecutionEventsPage {
  events: ExecutionEvent[];
  last_seq: number;
  finished: boolean;
}

// ─── execution (§6.5) ──────────────────────────────────────────────────────

export type ExecutionAction = 'stop' | 'pause' | 'resume';

export interface ExecutionLimits {
  max_steps: number;
  max_tool_calls?: number | null;
  max_duration_s: number;
  max_cost_fen?: number | null;
}

export interface ExecutionUsed {
  steps: number;
  tool_calls: number;
  tokens_in: number;
  tokens_out: number;
  cost_fen: number | null;
  /** Some call had no price: the cost is unknown, never 0. */
  cost_unknown: boolean;
  duration_ms: number;
}

export interface ExecutionBudget {
  limits: ExecutionLimits;
  used: ExecutionUsed;
}

export interface ExecutionOutput {
  asset_version_id: UUID;
  asset_type: string;
  label: string;
  state: string;
  /** The tool_result event that produced it. */
  event_seq: number | null;
  /** A workspace file registered after the session (P3): its path in the working directory. */
  path?: string | null;
}

export interface ExecutionAttempt {
  id: UUID;
  role: ExecutionRole | string;
  attempt_no: number;
  rework_no: number;
  status: string;
  started_at: ISODateTime | null;
}

export interface AgentExecution {
  id: UUID;
  project_id: UUID;
  run_id: UUID;
  node_id: string;
  node_task_id: UUID | null;
  agent: { id: UUID; name: string; version: number | null } | null;
  role: ExecutionRole | string;
  runtime: AgentRuntime | string;
  /** e.g. "pydantic-ai 2.55.0" */
  runtime_impl: string | null;
  runtime_ref: string | null;
  model: { config_id: UUID | null; provider: string | null; model_id: string | null } | null;
  status: string;
  attempt_no: number;
  rework_no: number;
  started_at: ISODateTime | null;
  finished_at: ISODateTime | null;
  lease_until: ISODateTime | null;
  budget: ExecutionBudget;
  inputs: unknown[];
  outputs: ExecutionOutput[];
  attempts: ExecutionAttempt[];
  verdict?: VerdictPayload | null;
  error?: { code?: string | null; message?: string | null } | null;
  simulated: boolean;
  allowed_actions: ExecutionAction[];
  last_seq: number;
  /** workspace_agent only (P3): the frozen workspace options (confirmation, network). */
  runtime_options?: { confirmation?: 'never' | 'risky' | string; network?: string; max_tool_calls?: number | null } | null;
}

export interface AgentExecutionListItem {
  id: UUID;
  project_id: UUID;
  run_id: UUID;
  node_id: string;
  node_title: string | null;
  agent: { id: UUID; name: string } | null;
  role: ExecutionRole | string;
  runtime: AgentRuntime | string;
  status: string;
  started_at: ISODateTime | null;
  finished_at: ISODateTime | null;
  used: Partial<ExecutionUsed>;
  simulated: boolean;
}

/** The list may come back as a bare array or as a page with a cursor. */
export type AgentExecutionList = AgentExecutionListItem[] | { items: AgentExecutionListItem[]; next_cursor?: string | null };

export function listItems(l: AgentExecutionList | undefined): AgentExecutionListItem[] {
  if (!l) return [];
  return Array.isArray(l) ? l : l.items ?? [];
}
export function listCursor(l: AgentExecutionList | undefined): string | null {
  return l && !Array.isArray(l) ? l.next_cursor ?? null : null;
}

export interface ExecutionListQuery {
  project_id?: UUID;
  status?: string;
  agent_id?: UUID;
  runtime?: string;
  role?: string;
  limit?: number;
  cursor?: string;
}

// ─── requests ──────────────────────────────────────────────────────────────

export const getAgentExecution = (id: UUID, o: Opt = {}) => request<AgentExecution>(`/agent-executions/${enc(id)}`, { signal: o.signal });

export const listExecutionEvents = (id: UUID, q: { after?: number; limit?: number } = {}, o: Opt = {}) =>
  request<ExecutionEventsPage>(`/agent-executions/${enc(id)}/events`, { query: { after: q.after, limit: q.limit }, signal: o.signal });

export const listAgentExecutions = (q: ExecutionListQuery = {}, o: Opt = {}) =>
  request<AgentExecutionList>('/agent-executions', { query: { ...q }, signal: o.signal });

/** Workbench agent questions and used-up budgets; only users who may control the run see them. */
export type AgentTodoKind = 'agent_question' | 'budget_exhausted';
export interface AgentTodo {
  kind: AgentTodoKind;
  project_id: UUID;
  run_id: UUID;
  run_title?: string | null;
  node_id: string;
  node_task_id: UUID;
  node_title?: string | null;
  task_id?: UUID | null;
  agent: { id: UUID | null; name: string | null };
  execution_id: UUID | null;
  /** The node's block code (agent_blocked, unknown_verdict, budget_exceeded). */
  code?: string | null;
  reason?: string | null;
  at?: ISODateTime | null;
  node_version?: number;
  questions?: string[];
  limit?: unknown;
  budget?: Record<string, unknown>;
}

export const listAgentTodos = (q: { project_id?: UUID; limit?: number } = {}, o: Opt = {}) =>
  request<{ items: AgentTodo[]; kinds: string[] }>('/agent-todos', { query: { ...q }, signal: o.signal });

/** Records the stop request; the node blocks with agent_stopped (continuable). */
export const stopAgentExecution = (id: UUID, o: Opt = {}) => request<AgentExecution>(`/agent-executions/${enc(id)}/stop`, { method: 'POST', body: {}, signal: o.signal });

/** Forwarded to the run's pause / resume; expected_version is the collaboration control_version, as before. */
export const pauseAgentExecution = (id: UUID, body: { expected_version: number; reason?: string }, o: Opt = {}) =>
  request<AgentExecution>(`/agent-executions/${enc(id)}/pause`, { method: 'POST', body, signal: o.signal });
export const resumeAgentExecution = (id: UUID, body: { expected_version: number }, o: Opt = {}) =>
  request<AgentExecution>(`/agent-executions/${enc(id)}/resume`, { method: 'POST', body, signal: o.signal });

// ─── SSE (§6.5): fetch reader, since EventSource cannot send Authorization ─────────────────────────────

/** One SSE frame → an event. `data` is the event itself ({seq, type, at, payload}) or just its payload. */
export function eventFromFrame(frame: { event: string; id?: string; data: unknown }): ExecutionEvent | null {
  const d = frame.data;
  if (d && typeof d === 'object' && 'payload' in d && 'type' in d && 'seq' in d) {
    const e = d as ExecutionEvent;
    return Number.isFinite(Number(e.seq)) ? { ...e, seq: Number(e.seq) } : null;
  }
  const seq = Number(frame.id);
  if (!Number.isFinite(seq) || !frame.event || frame.event === 'message') return null;
  const payload = d && typeof d === 'object' ? (d as Record<string, unknown>) : { value: d };
  const at = typeof payload.at === 'string' ? (payload.at as string) : null;
  return { seq, type: frame.event, at, payload };
}

export interface StreamHandlers {
  onEvent: (e: ExecutionEvent) => void;
  /** `event: end` — the execution finished and every event was sent. */
  onEnd: () => void;
}

/**
 * Read GET /agent-executions/{id}/stream?after=<seq> until the server closes it. Resolves when the stream closes
 * (after `end`, or a dropped connection — the caller reconnects from its last seq); rejects on HTTP errors.
 */
export async function streamExecutionEvents(id: UUID, after: number, h: StreamHandlers, o: Opt = {}): Promise<void> {
  const headers: Record<string, string> = { Accept: 'text/event-stream' };
  const token = currentToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  // Resume point as ?after= only: a Last-Event-ID request header is not in the API's CORS allow list.
  let res: Response;
  try {
    res = await fetch(buildUrl(`/agent-executions/${enc(id)}/stream`, { after }), { headers, signal: o.signal });
  } catch (e) {
    if (isAbortError(e)) return;
    throw new ApiError(0, 'network', e instanceof Error ? e.message : 'network request failed');
  }
  if (!res.ok) throw parseErrorBody(res.status, await res.text().catch(() => ''), res.statusText);
  if (!res.body) return;
  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  const parse = createSseParser((frame) => {
    if (frame.event === 'end') return h.onEnd();
    const e = eventFromFrame(frame);
    if (e) h.onEvent(e);
  });
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      parse(value);
    }
  } catch (e) {
    if (isAbortError(e)) return;
    throw new ApiError(0, 'network', e instanceof Error ? e.message : 'stream interrupted');
  }
}

// ─── model capability check (§6.5 POST /ai/model-configs/{id}/diagnose) ──────────────────────────────

/** tool_calling, parallel_tool_calls, no_strict, usage_fields, streaming (codes as the server names them). */
export interface DiagnoseCheck {
  code: string;
  /** pass / fail / skipped / error; older shapes send ok: boolean. */
  status?: string;
  ok?: boolean | null;
  message?: string | null;
  detail?: unknown;
}

export interface ModelDiagnosis {
  config_id?: UUID;
  config_version?: number | null;
  at?: ISODateTime | null;
  simulated?: boolean;
  checks: DiagnoseCheck[];
  capabilities?: Partial<Record<string, boolean>>;
}

/** The diagnose response: the diagnosis itself, or the configuration carrying it. */
export type DiagnoseResponse = ModelDiagnosis | (ModelConfig & { diagnosis?: ModelDiagnosis | null; last_diagnosis?: ModelDiagnosis | null; last_diagnose?: unknown });

/** The server stores the result on the config as ``last_diagnose`` with checks keyed by code ({code: {ok}}). */
type StoredDiagnosis = Omit<ModelDiagnosis, 'checks'> & { checks?: Record<string, { ok?: boolean | null; simulated?: boolean; detail?: unknown }> | DiagnoseCheck[] };

function normalized(d: StoredDiagnosis | null | undefined): ModelDiagnosis | null {
  if (!d) return null;
  const checks = Array.isArray(d.checks)
    ? d.checks
    : Object.entries(d.checks ?? {}).map(([code, c]) => ({ code, ok: c?.ok ?? null, detail: c?.detail }));
  return { ...d, checks };
}

export function diagnosisOf(r: DiagnoseResponse | null | undefined): ModelDiagnosis | null {
  if (!r) return null;
  if ('checks' in r && r.checks) return normalized(r as StoredDiagnosis);
  const c = r as { diagnosis?: StoredDiagnosis | null; last_diagnosis?: StoredDiagnosis | null; last_diagnose?: StoredDiagnosis | null };
  return normalized(c.last_diagnose ?? c.diagnosis ?? c.last_diagnosis);
}

/** Returns the configuration, carrying the result as ``last_diagnose`` (bound to its config_version). */
export const diagnoseModelConfig = (id: UUID, o: Opt = {}) =>
  request<DiagnoseResponse>(`/ai/model-configs/${enc(id)}/diagnose`, { method: 'POST', body: {}, signal: o.signal });

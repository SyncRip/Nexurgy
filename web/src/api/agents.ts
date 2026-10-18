// Agent profiles and actors (.trellis/tasks/archive/2026-10/10-08-agent-collaboration/design.md §3.1, §3.2, §4 C07).
// Types mirror the server builders they name: services/actors.py `out` (actor_out) and
// services/agent_profiles.py `profile_out`. When one of those changes, change it here.

import { request } from './client';
import type { ISODateTime, UUID } from './types';

type Opt = { signal?: AbortSignal };
const enc = encodeURIComponent;

// ─── enums (server/app/models.py) ──────────────────────────────────────────

export const AGENT_PURPOSES = ['execute', 'review', 'planner', 'general'] as const;
export type AgentPurpose = (typeof AGENT_PURPOSES)[number];
export const EXECUTION_MODES = ['full_auto', 'semi_auto'] as const;
export type ExecutionMode = (typeof EXECUTION_MODES)[number];
export const ACTOR_KINDS = ['human', 'agent'] as const;
export type ActorKind = (typeof ACTOR_KINDS)[number];

/** The server tools of the registry (server/app/ai/tool_registry.py TOOL_NAMES), in their order. The editor lists
 * the live contract from GET /agent-tools; this copy is the fallback and the profile checks' list of known tools. */
export const TOOL_NAMES = [
  'document',
  'export',
  'model_3d',
  'drawing_reconstruct',
  'drawing',
  'process_plan',
  'quote',
  'knowledge_reference',
  'knowledge_adopt',
  'engineering_review',
  'engineering_spec',
  'assembly_from_spec',
  'assembly_drawing',
  'delivery_package',
] as const;
export type ToolName = (typeof TOOL_NAMES)[number];
/** Tools only a review / general profile may hold. */
export const REVIEW_ONLY_TOOLS: ReadonlySet<string> = new Set(['engineering_review']);
/** Tools that need a part on the draft (422 part_required otherwise). */
export const PART_TOOLS: ReadonlySet<string> = new Set(['model_3d', 'drawing_reconstruct', 'drawing', 'process_plan', 'quote']);

// ─── tool contract (agent execution layer P2, GET /agent-tools) ────────────

/** write: changes data; read: read-only; review_only: review / general profiles only; end: deliver / report_blocked. */
export const TOOL_KINDS = ['write', 'read', 'review_only', 'end'] as const;
export type ToolKind = (typeof TOOL_KINDS)[number];

/** The subset of JSON Schema (pydantic's model_json_schema) the contract table reads. */
export interface JsonSchema {
  type?: string | string[];
  title?: string;
  description?: string;
  format?: string;
  enum?: unknown[];
  const?: unknown;
  default?: unknown;
  items?: JsonSchema;
  anyOf?: JsonSchema[];
  oneOf?: JsonSchema[];
  $ref?: string;
  properties?: Record<string, JsonSchema>;
  required?: string[];
  $defs?: Record<string, JsonSchema>;
}

/** One registry entry: exactly what the executors give the model (args_schema = the argument model's JSON Schema). */
export interface AgentToolContract {
  name: string;
  description: string;
  kind: ToolKind;
  needs_part: boolean;
  produces_assets: boolean;
  args_schema: JsonSchema;
}

/** What a purpose may do (services/actors.PURPOSE_ROLES). */
export type ActorRole = 'execute' | 'review' | 'pre_review' | 'planner';
const PURPOSE_ROLES: Record<ActorRole, readonly AgentPurpose[]> = {
  execute: ['execute', 'general'],
  review: ['review', 'general'],
  pre_review: ['review', 'general'],
  planner: ['planner', 'general'],
};

export function purposeAllows(purpose: AgentPurpose | string | null | undefined, role: ActorRole): boolean {
  return !!purpose && (PURPOSE_ROLES[role] as readonly string[]).includes(purpose);
}

// ─── limits & pricing (design §3.2) ────────────────────────────────────────

export interface AgentLimits {
  max_steps: number;
  max_duration_s: number;
  max_attempts: number;
  max_reworks: number;
  /** null = no cost ceiling. */
  max_cost_fen: number | null;
}

export const DEFAULT_LIMITS: AgentLimits = { max_steps: 8, max_duration_s: 300, max_attempts: 2, max_reworks: 3, max_cost_fen: null };

export type BoundedLimit = Exclude<keyof AgentLimits, 'max_cost_fen'>;
export const LIMIT_RANGES: Record<BoundedLimit, { min: number; max: number }> = {
  max_steps: { min: 1, max: 64 },
  max_duration_s: { min: 1, max: 86400 },
  max_attempts: { min: 1, max: 10 },
  max_reworks: { min: 0, max: 20 },
};

/** Price per 1k tokens in fen; null pricing = cost unknown (never shown as 0). */
export interface AgentPricing {
  in_fen_per_1k: number;
  out_fen_per_1k: number;
}

export interface KnowledgeScope {
  scope: 'project' | 'none';
}

// ─── runtime (agent execution layer P1, .trellis/tasks/10-11-agent-execution-layer/design.md §6.3) ─────

/** Stored runtimes, including the retired workspace executor for historical profiles. */
export const AGENT_RUNTIMES = ['native', 'tool_agent', 'workspace_agent'] as const;
export type AgentRuntime = (typeof AGENT_RUNTIMES)[number];
export type SelectableAgentRuntime = Exclude<AgentRuntime, 'workspace_agent'>;
/** Only supported runtimes may be selected for new profile versions. */
export const SELECTABLE_RUNTIMES: ReadonlySet<AgentRuntime> = new Set(['native', 'tool_agent']);
export function isSelectableRuntime(runtime: AgentRuntime): runtime is SelectableAgentRuntime {
  return SELECTABLE_RUNTIMES.has(runtime);
}

/** Closed per-runtime options (plan §4.7): tool_agent has parallel_tool_calls / max_tool_calls; workspace_agent has
 * confirmation (never / risky), network (always off) and max_tool_calls (PartRoute tool calls over MCP). */
export interface RuntimeOptions {
  parallel_tool_calls?: boolean;
  max_tool_calls?: number | null;
  /** Historical workspace options; never offered in the profile editor. */
  confirmation?: 'never' | 'risky';
  network?: 'off';
}

export const MAX_TOOL_CALLS_RANGE = { min: 1, max: 64 } as const;

/** Profile list stats (design §6.5): last 7 days; unknown cost is never 0. */
export interface AgentProfileStats {
  running: number;
  executions_7d: number;
  cost_fen_7d: number | null;
  cost_unknown: boolean;
}

// ─── profile_out ───────────────────────────────────────────────────────────

export interface AgentModelConfigRef {
  id: UUID;
  label: string;
  provider: string;
  model_id: string;
  /** Enabled and key present on the server (not a connection test). */
  ready: boolean;
}

export interface AgentProfileUsage {
  runs_active: number;
  executions_total: number;
}

export interface AgentProfile {
  id: UUID;
  project_id: UUID;
  name: string;
  purpose: AgentPurpose;
  /** The project role of this agent ("project role"). */
  description: string;
  role_prompt: string;
  /** The configured id (also set when the configuration no longer exists and model_config is null). */
  model_config_id?: UUID | null;
  /** null = the project's default model configuration at run time. */
  model_config: AgentModelConfigRef | null;
  allowed_tools: string[];
  execution_modes: ExecutionMode[];
  knowledge_scope: KnowledgeScope;
  limits: AgentLimits;
  pricing: AgentPricing | null;
  enabled: boolean;
  version: number;
  created_by: UUID;
  created_at: ISODateTime | null;
  updated_at: ISODateTime | null;
  disabled_at: ISODateTime | null;
  usage: AgentProfileUsage;
  /** P1 fields; absent on servers before the execution layer. */
  runtime?: AgentRuntime;
  runtime_options?: RuntimeOptions;
  /** GET /projects/{id}/agent-profiles only. */
  stats?: AgentProfileStats;
  /** The model this profile would use is enabled, keyed and capable for its runtime. */
  model_available?: boolean;
}

export interface AgentProfileIn {
  name: string;
  purpose: AgentPurpose;
  description: string;
  role_prompt: string;
  model_config_id: UUID | null;
  allowed_tools: string[];
  execution_modes: ExecutionMode[];
  knowledge_scope: KnowledgeScope;
  limits: AgentLimits;
  pricing: AgentPricing | null;
  runtime?: SelectableAgentRuntime;
  runtime_options?: Pick<RuntimeOptions, 'parallel_tool_calls' | 'max_tool_calls'>;
}

/** PATCH body: optimistic lock plus any subset of the fields; `enabled=false` disables. */
export type AgentProfilePatch = Partial<AgentProfileIn> & { expected_version: number; enabled?: boolean };

// ─── actors (services/actors.py) ───────────────────────────────────────────

/** ActorRef: exactly one of user_id (human) / agent_profile_id (agent). */
export interface ActorRef {
  kind: ActorKind;
  user_id?: UUID | null;
  agent_profile_id?: UUID | null;
}

export function actorRef(kind: ActorKind, id: UUID): ActorRef {
  return kind === 'human' ? { kind, user_id: id } : { kind, agent_profile_id: id };
}

export function actorRefId(ref: ActorRef | null | undefined): UUID | null {
  if (!ref) return null;
  return (ref.kind === 'human' ? ref.user_id : ref.agent_profile_id) ?? null;
}

export function sameActor(a: ActorRef | null | undefined, b: ActorRef | null | undefined): boolean {
  return !!a && !!b && a.kind === b.kind && actorRefId(a) === actorRefId(b);
}

export interface AgentBrief {
  id: UUID;
  name: string | null;
  purpose: AgentPurpose | null;
  version: number | null;
  enabled: boolean;
}

/** actor_out: {kind, id, user, agent}. */
export interface ActorOut {
  kind: ActorKind;
  id: UUID;
  user: { id: UUID; name: string | null } | null;
  agent: AgentBrief | null;
}

export function actorOutRef(a: ActorOut | null | undefined): ActorRef | null {
  return a ? actorRef(a.kind, a.id) : null;
}

// ─── requests (C07) ────────────────────────────────────────────────────────

export const listAgentProfiles = (projectId: UUID, q: { purpose?: AgentPurpose | ''; enabled?: boolean } = {}, o: Opt = {}) =>
  request<AgentProfile[]>(`/projects/${enc(projectId)}/agent-profiles`, { query: { purpose: q.purpose || undefined, enabled: q.enabled }, signal: o.signal });

export const createAgentProfile = (projectId: UUID, body: AgentProfileIn, o: Opt = {}) =>
  request<AgentProfile>(`/projects/${enc(projectId)}/agent-profiles`, { method: 'POST', body, signal: o.signal });

export const getAgentProfile = (profileId: UUID, o: Opt = {}) => request<AgentProfile>(`/agent-profiles/${enc(profileId)}`, { signal: o.signal });

/** 409 version_conflict when expected_version is stale; 422 model_config_unusable / field errors. */
export const patchAgentProfile = (profileId: UUID, body: AgentProfilePatch, o: Opt = {}) =>
  request<AgentProfile>(`/agent-profiles/${enc(profileId)}`, { method: 'PATCH', body, signal: o.signal });

/** The tool contract: every registered tool, end tools last (project members read; 404 for a non-member). */
export const listAgentTools = (projectId: UUID, o: Opt = {}) =>
  request<AgentToolContract[]>('/agent-tools', { query: { project_id: projectId }, signal: o.signal });

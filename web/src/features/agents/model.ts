// Pure helpers of the agent wall and detail page (R1.2 prototype #flows.agents,
// #agent.<id>): which workflows / templates use an agent, what is wrong with its
// configuration, and the body of a copy. No React; unit-tested in model.test.ts.
//
// Everything here reads real profile / workflow data. Unpersisted guardrail
// placeholders are not part of the profile contract.

import { LIMIT_RANGES, REVIEW_ONLY_TOOLS, TOOL_NAMES, isSelectableRuntime, purposeAllows, type AgentProfile, type AgentProfileIn, type BoundedLimit } from '../../api/agents';
import type { ModelConfig, Workflow, WorkflowGraph } from '../../api/types';

// ─── usage ─────────────────────────────────────────────────────────────────

/** Agent profile ids a graph's business_task nodes name as executor, reviewer or pre-reviewer. */
export function agentRefsOf(graph: Pick<WorkflowGraph, 'nodes'> | null | undefined): Set<string> {
  const out = new Set<string>();
  for (const n of graph?.nodes ?? []) {
    if (n.type !== 'business_task') continue;
    const p = (n.params ?? {}) as Record<string, unknown>;
    for (const role of ['executor', 'reviewer'] as const) {
      const a = p[role] as { kind?: string; agent_profile_id?: string | null } | undefined;
      if (a?.kind === 'agent' && a.agent_profile_id) out.add(a.agent_profile_id);
    }
    if (typeof p.pre_reviewer_agent_id === 'string' && p.pre_reviewer_agent_id) out.add(p.pre_reviewer_agent_id);
  }
  return out;
}

type UsageWorkflow = Pick<Workflow, 'id' | 'graph' | 'archived_at'>;

/** One workflow or template that uses an agent (detail page "Usage"). */
export interface AgentUse {
  id: string;
  name: string;
  kind: 'template' | 'collaboration';
}

/** agent id → the (non-archived, de-duplicated) workflows and templates whose graph uses it. */
export function agentUsage<W extends UsageWorkflow>(workflows: readonly W[]): Map<string, W[]> {
  const out = new Map<string, W[]>();
  const seen = new Set<string>();
  for (const w of workflows) {
    if (w.archived_at || seen.has(w.id)) continue;
    seen.add(w.id);
    for (const id of agentRefsOf(w.graph)) out.set(id, [...(out.get(id) ?? []), w]);
  }
  return out;
}

// ─── configuration issues ──────────────────────────────────────────────────

export const AGENT_ISSUES = [
  'model_unavailable',
  'model_missing',
  'default_model_unavailable',
  'no_default_model',
  'no_modes',
  'review_tool_on_executor',
  'unknown_tool',
  'limit_out_of_range',
  'knowledge_tools_without_scope',
] as const;
export type AgentIssue = (typeof AGENT_ISSUES)[number];

export type ModelState = { kind: 'own'; label: string; modelId: string; ready: boolean } | { kind: 'default'; modelId: string | null; ready: boolean | null } | { kind: 'missing' };

/**
 * The model a profile runs with: its own configuration, or the default one at run
 * time (null config). `configs` may be unknown (not loaded / no access): then the
 * default's readiness is null.
 */
export function modelState(p: Pick<AgentProfile, 'model_config' | 'model_config_id'>, configs?: readonly ModelConfig[] | null): ModelState {
  if (p.model_config) return { kind: 'own', label: p.model_config.label, modelId: p.model_config.model_id, ready: p.model_config.ready };
  if (p.model_config_id) return { kind: 'missing' };
  if (!configs) return { kind: 'default', modelId: null, ready: null };
  const d = configs.find((c) => c.is_default && c.enabled);
  return { kind: 'default', modelId: d?.model_id ?? null, ready: d ? d.ready : false };
}

const KNOWLEDGE_TOOLS = ['knowledge_reference', 'knowledge_adopt'];
const BOUNDED: BoundedLimit[] = ['max_steps', 'max_duration_s', 'max_attempts', 'max_reworks'];

/** What would stop this profile from running as configured (prototype agentErrors, real fields only). */
export function agentIssues(
  p: Pick<AgentProfile, 'model_config' | 'model_config_id' | 'purpose' | 'allowed_tools' | 'execution_modes' | 'limits' | 'knowledge_scope'>,
  configs?: readonly ModelConfig[] | null,
): AgentIssue[] {
  const out: AgentIssue[] = [];
  const m = modelState(p, configs);
  if (m.kind === 'own' && !m.ready) out.push('model_unavailable');
  if (m.kind === 'missing') out.push('model_missing');
  if (m.kind === 'default' && m.ready === false) out.push(m.modelId ? 'default_model_unavailable' : 'no_default_model');
  if (!p.execution_modes?.length) out.push('no_modes');
  const tools = p.allowed_tools ?? [];
  if (tools.some((x) => REVIEW_ONLY_TOOLS.has(x)) && !purposeAllows(p.purpose, 'review')) out.push('review_tool_on_executor');
  if (tools.some((x) => !(TOOL_NAMES as readonly string[]).includes(x))) out.push('unknown_tool');
  const lim = p.limits;
  if (lim && (BOUNDED.some((k) => !(lim[k] >= LIMIT_RANGES[k].min && lim[k] <= LIMIT_RANGES[k].max)) || (lim.max_cost_fen !== null && lim.max_cost_fen !== undefined && lim.max_cost_fen < 0)))
    out.push('limit_out_of_range');
  if (p.knowledge_scope?.scope === 'none' && tools.some((x) => KNOWLEDGE_TOOLS.includes(x))) out.push('knowledge_tools_without_scope');
  return out;
}

// ─── copies ────────────────────────────────────────────────────────────────

/** `name`, or `name (copy)`, `name (copy 2)` … when it is taken (case-insensitive, trimmed). */
export function uniqueName(name: string, taken: Iterable<string>, copyWord: string): string {
  const used = new Set([...taken].map((x) => x.trim().toLowerCase()));
  const base = name.trim();
  if (!used.has(base.toLowerCase())) return base;
  for (let i = 1; ; i++) {
    const next = i === 1 ? `${base} (${copyWord})` : `${base} (${copyWord} ${i})`;
    if (!used.has(next.toLowerCase())) return next.slice(0, 200);
  }
}

/** Create body of a copy: every configured field, a new name; usage, version and state are the copy's own. */
export function copyBody(p: AgentProfile, name: string): AgentProfileIn {
  const runtime = p.runtime ?? 'native';
  if (!isSelectableRuntime(runtime)) throw new Error('Retired agent runtimes cannot be copied.');
  return {
    name,
    purpose: p.purpose,
    description: p.description,
    role_prompt: p.role_prompt,
    model_config_id: p.model_config?.id ?? null,
    allowed_tools: [...p.allowed_tools],
    execution_modes: [...p.execution_modes],
    knowledge_scope: { scope: p.knowledge_scope?.scope === 'none' ? 'none' : 'project' },
    limits: { ...p.limits },
    pricing: p.pricing ? { ...p.pricing } : null,
    runtime,
    runtime_options: runtime === 'tool_agent' ? { parallel_tool_calls: p.runtime_options?.parallel_tool_calls, max_tool_calls: p.runtime_options?.max_tool_calls } : {},
  };
}

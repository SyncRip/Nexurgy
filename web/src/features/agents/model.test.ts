import { describe, expect, it } from 'vitest';
import { AGENT_RUNTIMES, DEFAULT_LIMITS, isSelectableRuntime, type AgentProfile } from '../../api/agents';
import type { ModelConfig, Workflow, WorkflowNode } from '../../api/types';
import { agentIssues, agentRefsOf, agentUsage, copyBody, modelState, uniqueName } from './model';
import { eligibleAgents } from './common';

const task = (id: string, executor: string | null, reviewer: string | null, pre: string | null = null): WorkflowNode => ({
  id,
  type: 'business_task',
  position: { x: 0, y: 0 },
  params: {
    executor: executor ? { kind: 'agent', agent_profile_id: executor } : { kind: 'human', user_id: 'u1' },
    reviewer: reviewer ? { kind: 'agent', agent_profile_id: reviewer } : { kind: 'human', user_id: 'u2' },
    pre_reviewer_agent_id: pre,
  },
});

const wf = (id: string, nodes: WorkflowNode[], extra: Partial<Workflow> = {}): Workflow => ({
  id,
  project_id: 'p',
  name: id,
  description: '',
  graph: { nodes, edges: [], groups: [] },
  version: 1,
  template_key: null,
  created_at: '',
  updated_at: '',
  archived_at: null,
  validation: [],
  ...extra,
});

const profile = (extra: Partial<AgentProfile> = {}): AgentProfile => ({
  id: 'a1',
  project_id: 'p',
  name: 'Doc agent',
  purpose: 'execute',
  description: '',
  role_prompt: 'Write documents.',
  model_config: null,
  allowed_tools: ['document'],
  execution_modes: ['semi_auto'],
  knowledge_scope: { scope: 'project' },
  limits: { ...DEFAULT_LIMITS },
  pricing: null,
  enabled: true,
  version: 3,
  created_by: 'u',
  created_at: null,
  updated_at: null,
  disabled_at: null,
  usage: { runs_active: 0, executions_total: 0 },
  ...extra,
});

const config = (extra: Partial<ModelConfig>): ModelConfig =>
  ({ id: 'm1', label: 'Custom model', provider: 'openai_compatible', model_id: 'custom-model', is_default: false, enabled: true, ready: true, issues: [], ...extra }) as ModelConfig;

describe('agent usage', () => {
  it('collects executor, reviewer and pre-reviewer agents of business tasks only', () => {
    const refs = agentRefsOf({ nodes: [task('n1', 'a1', null, 'a3'), task('n2', null, 'a2'), { id: 'x', type: 'quote', position: { x: 0, y: 0 }, params: { executor: { kind: 'agent', agent_profile_id: 'a9' } } }] });
    expect([...refs].sort()).toEqual(['a1', 'a2', 'a3']);
  });

  it('counts each workflow once and skips archived ones', () => {
    const usage = agentUsage([wf('w1', [task('n1', 'a1', 'a2'), task('n2', 'a1', 'a2')]), wf('w2', [task('n1', 'a1', null)]), wf('w3', [task('n1', 'a1', null)], { archived_at: '2026-10-01' }), wf('w1', [task('n1', 'a1', null)])]);
    expect(usage.get('a1')?.map((w) => w.id)).toEqual(['w1', 'w2']);
    expect(usage.get('a2')?.map((w) => w.id)).toEqual(['w1']);
    expect(usage.get('zz')).toBeUndefined();
  });
});

describe('agent issues', () => {
  it('flags an unavailable own model and a missing default', () => {
    expect(agentIssues(profile({ model_config: { id: 'm1', label: 'GPT', provider: 'openai', model_id: 'gpt', ready: false } }))).toEqual(['model_unavailable']);
    expect(agentIssues(profile({ model_config_id: 'gone' }))).toEqual(['model_missing']);
    expect(agentIssues(profile(), [config({ is_default: true, ready: false })])).toEqual(['default_model_unavailable']);
    expect(agentIssues(profile(), [config({ is_default: false })])).toEqual(['no_default_model']);
    expect(agentIssues(profile(), [config({ is_default: true })])).toEqual([]);
    // Unknown configs (not loaded): the default's readiness is not guessed.
    expect(agentIssues(profile())).toEqual([]);
    expect(modelState(profile(), null)).toEqual({ kind: 'default', modelId: null, ready: null });
  });

  it('flags configuration the server would refuse to run', () => {
    expect(agentIssues(profile({ execution_modes: [] }))).toEqual(['no_modes']);
    expect(agentIssues(profile({ allowed_tools: ['engineering_review'] }))).toEqual(['review_tool_on_executor']);
    expect(agentIssues(profile({ purpose: 'review', allowed_tools: ['engineering_review'] }))).toEqual([]);
    expect(agentIssues(profile({ allowed_tools: ['web_search'] }))).toEqual(['unknown_tool']);
    expect(agentIssues(profile({ limits: { ...DEFAULT_LIMITS, max_steps: 0 } }))).toEqual(['limit_out_of_range']);
    expect(agentIssues(profile({ knowledge_scope: { scope: 'none' }, allowed_tools: ['knowledge_reference'] }))).toEqual(['knowledge_tools_without_scope']);
  });
});

describe('agent copies', () => {
  it('keeps the name when free and numbers copies otherwise', () => {
    expect(uniqueName('Doc agent', ['Other'], 'copy')).toBe('Doc agent');
    expect(uniqueName('Doc agent', ['doc agent '], 'copy')).toBe('Doc agent (copy)');
    expect(uniqueName('Doc agent', ['Doc agent', 'Doc agent (copy)'], 'copy')).toBe('Doc agent (copy 2)');
  });

  it('copies the configuration, not the identity', () => {
    const p = profile({ model_config: { id: 'm1', label: 'GPT', provider: 'openai', model_id: 'gpt', ready: true }, pricing: { in_fen_per_1k: 2, out_fen_per_1k: 8 } });
    const body = copyBody(p, 'Doc agent (copy)');
    expect(body).toMatchObject({ name: 'Doc agent (copy)', model_config_id: 'm1', allowed_tools: ['document'], knowledge_scope: { scope: 'project' }, pricing: { in_fen_per_1k: 2, out_fen_per_1k: 8 } });
    expect(body).not.toHaveProperty('id');
    expect(body).not.toHaveProperty('version');
    expect(body.allowed_tools).not.toBe(p.allowed_tools);
  });

  it('preserves supported runtime options but refuses a retired runtime instead of silently migrating it', () => {
    const p = profile({ runtime: 'tool_agent', runtime_options: { parallel_tool_calls: true, max_tool_calls: 12 } });
    expect(copyBody(p, 'Copy')).toMatchObject({ runtime: 'tool_agent', runtime_options: { parallel_tool_calls: true, max_tool_calls: 12 } });
    expect(() => copyBody(profile({ runtime: 'workspace_agent' }), 'Retired copy')).toThrow('Retired');
  });
});

describe('runtime retirement', () => {
  it('retains the historical runtime vocabulary but excludes retired profiles from new actor choices', () => {
    expect(AGENT_RUNTIMES).toContain('workspace_agent');
    expect(AGENT_RUNTIMES.filter(isSelectableRuntime)).toEqual(['native', 'tool_agent']);
    const profiles = [profile(), profile({ id: 'a2', runtime: 'tool_agent' }), profile({ id: 'a3', runtime: 'workspace_agent' })];
    expect(eligibleAgents(profiles, 'execute', 'semi_auto').map((p) => p.id)).toEqual(['a1', 'a2']);
  });
});

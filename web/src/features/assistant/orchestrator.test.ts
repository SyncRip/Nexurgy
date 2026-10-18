import { describe, expect, it } from 'vitest';
import type { AiOperation, AiResultItem, AiWork, OrchestratorStep } from '../../api/assistant';
import { itemsOf, overviewEmpty, sessionOverview, stepViews, subAgentNotice } from './orchestrator';

const T0 = '2026-10-15T10:00:00Z';

function op(id: string, over: Partial<AiOperation> = {}): AiOperation {
  return {
    id,
    conversation_id: 'c1',
    message_id: 'm1',
    kind: 'ask',
    status: 'succeeded',
    request: { text: 'x', operation: 'ask', requested_operation: 'auto', target: null, references: [], constraints: [], task_id: null, model_config_id: null, client_request_id: `crid-${id}` },
    task: null,
    part: null,
    run: null,
    question: null,
    result: null,
    error: null,
    simulated: true,
    retry_of: null,
    created_at: T0,
    updated_at: T0,
    finished_at: null,
    context_build: null,
    ...over,
  };
}

const turn = (id: string, steps: Partial<OrchestratorStep>[], over: Partial<AiOperation> = {}) =>
  op(id, {
    request: { ...op(id).request, runtime: 'orchestrator', trigger: { type: 'request' } },
    result: { kind: 'orchestrator', trigger: { type: 'request' }, simulated: true, steps: steps.map((s) => ({ index: 0, tool: '', title: '', args: {}, status: 'ok', message: '', refs: {}, started_at: T0, finished_at: T0, ...s })) } as never,
    ...over,
  });

const item = (kind: 'part_revision' | 'asset_version', objectId: string, assetType: string, label: string): AiResultItem =>
  ({ ref: { key: `${kind}:${objectId}`, kind, object_id: objectId, title: 'Flange', version_label: label }, asset_type: assetType, label, run_id: 'run9', node_id: kind === 'part_revision' ? 'model' : 'drawing' }) as never;

const work: AiWork = {
  drafts: [{ id: 'd1', title: 'Flange with 4 holes', status: 'open', version: 1 } as never],
  tasks: [{ id: 'k1', number: 'T-7', title: 'Flange', status: 'in_progress' }],
  parts: [{ id: 'p1', number: 'P-001', name: 'Flange' }],
};

const model = op('m1', { kind: 'model_create', status: 'succeeded', parent_operation_id: 't1', run: { id: 'run1', status: 'succeeded', title: 'model' }, result: { kind: 'model', items: [item('part_revision', 'rev2', 'part_model', 'R2')] } });
const drawing = op('g1', { kind: 'drawing_generate', status: 'running', parent_operation_id: 't2', run: null, result: { kind: 'drawing', items: [item('asset_version', 'av1', 'drawing', 'v1')] } });

describe('step timeline', () => {
  it('keeps the workflow kind for coordination targets, including before the child snapshot arrives', () => {
    const parent = turn('parent', [{ tool: 'create_assembly', refs: { operation_id: 'assembly' } }]);
    const child = op('assembly', { kind: 'assembly_create', status: 'failed', parent_operation_id: 'parent',
      run: { id: 'assembly-run', status: 'partial', title: 'G203' } });
    expect(stepViews(parent, { operations: [] })[0].target).toMatchObject({
      operationKind: 'assembly_create', agent: 'manager', status: null, panel: null,
    });
    expect(stepViews(parent, { operations: [child] })[0].target).toMatchObject({
      operationKind: 'assembly_create', agent: 'manager', status: 'failed', panel: { kind: 'run', id: 'assembly-run' },
    });
    expect(sessionOverview({ operations: [parent, child] }).agents[0]).toMatchObject({
      agent: 'manager', op: { status: 'failed', run: { status: 'partial' } },
    });
    expect(subAgentNotice({ event: 'sub_agent_finished', operation_id: child.id, status: 'failed' }, [child]))
      .toMatchObject({ kind: 'assembly_create', agent: 'manager', status: 'failed' });
  });

  it('opens manufacturing results at their exact saved asset version', () => {
    const t = turn('t', [
      { tool: 'create_process_plan', refs: { part_id: 'p1', revision_id: 'r2', asset_id: 'plan-asset', asset_version_id: 'plan-v1' } },
      { index: 1, tool: 'create_quote', refs: { part_id: 'p1', asset_id: 'quote-asset', asset_version_id: 'quote-v2' } },
    ]);
    const views = stepViews(t, { operations: [t], work });
    expect(views.map((v) => v.target)).toMatchObject([
      { object: 'asset', id: 'plan-v1', href: '/assets/plan-asset?version=plan-v1', panel: null },
      { object: 'asset', id: 'quote-v2', href: '/assets/quote-asset?version=quote-v2', panel: null },
    ]);
  });

  it('points each step at its object through one table, in step order', () => {
    const t = turn('t1', [
      { index: 3, tool: 'create_model', refs: { operation_id: 'm1', part_id: 'p1' } },
      { index: 0, tool: 'draft_task', refs: { draft_id: 'd1' }, undo: true },
      { index: 1, tool: 'create_part', refs: { part_id: 'p1' } },
      // waiting for confirmation: no task yet, so the draft it would use
      { index: 2, tool: 'create_task', status: 'waiting_confirm', refs: { draft_id: 'd1' } },
      { index: 4, tool: 'generate_drawing', status: 'failed', refs: { operation_id: 'g1', revision_id: 'rev2' }, error: { code: 'forbidden', message: 'no design permission' } },
      { index: 5, tool: 'get_revision', refs: { revision_id: 'rev2' } },
      { index: 6, tool: 'future_tool', refs: { task_id: 'k1' } },
      { index: 7, tool: 'project_overview' },
    ]);
    const views = stepViews(t, { operations: [t, model, drawing], work });
    expect(views.map((v) => v.index)).toEqual([0, 1, 2, 3, 4, 5, 6, 7]);
    const [draftV, part, task, mdl, drw, rev, other, look] = views;
    expect(draftV).toMatchObject({ undo: true, target: { object: 'draft', name: 'Flange with 4 holes', panel: { kind: 'draft', id: 'd1' }, href: null } });
    expect(part.target).toMatchObject({ object: 'part', name: 'P-001 · Flange', panel: null, href: '/parts/p1' });
    expect(task.target?.object).toBe('draft');
    expect(mdl.target).toMatchObject({ object: 'operation', agent: 'modeling', status: 'succeeded', panel: { kind: 'run', id: 'run1' } });
    // a sub-agent without a run yet: no pane (the chip scrolls to its card)
    expect(drw).toMatchObject({ error: { code: 'forbidden' }, target: { object: 'operation', agent: 'drawing', status: 'running', panel: null } });
    expect(rev.target).toMatchObject({ object: 'revision', name: 'R2', panel: { kind: 'revision', id: 'rev2' } });
    expect(other.target).toMatchObject({ object: 'task', name: 'T-7 · Flange', panel: { kind: 'task', id: 'k1' } });
    expect(look.target).toBeNull();
    expect(stepViews(model, { operations: [] })).toEqual([]);
  });
});

describe('overview', () => {
  it('lists sub-agents with their outputs, the work, and 2D drawing drafts', () => {
    const t = turn('t1', []);
    const m = sessionOverview({ operations: [t, model, drawing], work });
    expect(m.agents.map((a) => [a.op.id, a.agent, a.panel])).toEqual([
      ['m1', 'modeling', { kind: 'run', id: 'run1' }],
      ['g1', 'drawing', null],
    ]);
    expect(m.agents[0].outputs).toEqual([{ key: 'part_revision:rev2', assetType: 'part_model', label: 'R2', panel: { kind: 'revision', id: 'rev2' }, nodeId: null }]);
    expect(m.agents[1].outputs[0]).toMatchObject({ panel: { kind: 'run', id: 'run9' }, nodeId: 'drawing' });
    expect(m.drawings).toEqual([{ key: 'asset_version:av1', title: 'Flange', label: 'v1', runId: 'run9', nodeId: 'drawing', simulated: true }]);
    expect(m.drafts.map((d) => d.id)).toEqual(['d1']);
    expect(overviewEmpty(m)).toBe(false);
    expect(overviewEmpty(sessionOverview({ operations: [t, op('plain', { kind: 'model_create' })] }))).toBe(true);
  });
});

describe('notices', () => {
  it('reads a finished sub-agent notice', () => {
    expect(subAgentNotice({ event: 'sub_agent_finished', operation_id: 'm1', status: 'failed' }, [model])).toEqual({ operationId: 'm1', agent: 'modeling', kind: 'model_create', status: 'failed' });
    expect(subAgentNotice({ event: 'sub_agent_finished', operation_id: 'gone', kind: 'drawing_generate' }, [])).toMatchObject({ agent: 'drawing', status: null });
    expect(subAgentNotice({ code: 'task_changed' }, [model])).toBeNull();
  });
});


describe('plan and completion outcomes', () => {
  const planned = (application: 'applied' | 'undone' | null = null): AiOperation => op('plan', {
    kind: 'workflow_edit', status: 'succeeded', generation: 2,
    result: { kind: 'workflow_edit', base_version: 3, plan_hash: 'a'.repeat(64), application: application ? { state: application } : null } as unknown as AiOperation['result'],
  });
  it('distinguishes plan readiness, applied and undone in the parent timeline', () => {
    const parent = turn('p', [{ tool: 'edit_workflow', refs: { operation_id: 'plan', part_id: 'part' } }]);
    for (const application of [null, 'applied', 'undone'] as const) {
      const target = stepViews(parent, { operations: [planned(application)] })[0].target;
      expect(target).toMatchObject({ object: 'operation', id: 'plan', outcome: application ?? 'plan_ready' });
    }
  });
  it('shows the immutable first notice while the same child runs a later generation', () => {
    const later = { ...planned(), status: 'running' as const };
    expect(subAgentNotice({ event: 'sub_agent_finished', operation_id: 'plan', kind: 'workflow_edit', generation: 1, outcome: 'failed', status: 'failed' }, [later])).toMatchObject({ status: 'failed', generation: 1 });
    expect(subAgentNotice({ event: 'sub_agent_finished', operation_id: 'plan', generation: 1, outcome: 'plan_ready', status: 'succeeded' }, [planned('applied')])).toMatchObject({ status: 'succeeded', outcome: 'plan_ready', generation: 1 });
    expect(subAgentNotice({ event: 'sub_agent_finished', operation_id: 'plan', status: 'succeeded' }, [planned('applied')])).not.toHaveProperty('outcome');
  });
});


it('does not turn historical raw service references into blank actionable result cards', () => {
  const historical = { result: { kind: 'drafting_create', items: [{ ref: { kind: 'drawing_document_version', object_id: 'v1' } }] } } as unknown as AiOperation;
  expect(itemsOf(historical)).toEqual([]);
  expect(itemsOf(model)[0].ref.key).toBe('part_revision:rev2');
});

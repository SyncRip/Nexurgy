import { describe, expect, it } from 'vitest';
import { orchestratorResult, type AiCompletionEvent, type AiMessage, type AiOperation, type AiResultItem, type CadResult, type OrchestratorStep } from '../../api/assistant';
import { designChoicePlan, designChoiceQuestion } from './designChoiceFixtures.test-support';
import { conversationEntries, currentJourney, journeyAnswerRole, operationHasFailure, type ConversationEntry, type GenerationJourney } from './journey';

const TIME = '2026-10-18T00:00:00Z';

function op(id: string, overrides: Partial<AiOperation> = {}): AiOperation {
  return {
    id, conversation_id: 'conversation', message_id: `request-${id}`, kind: 'ask', status: 'succeeded',
    request: { text: 'A short request', operation: 'ask', requested_operation: 'auto', target: null, references: [],
      constraints: [], task_id: null, model_config_id: null, client_request_id: `client-${id}` },
    task: null, part: null, run: null, question: null, result: null, error: null, simulated: true, retry_of: null,
    created_at: TIME, updated_at: TIME, finished_at: TIME, context_build: null,
    ...overrides,
  };
}

function message(id: string, seq: number, overrides: Partial<AiMessage> = {}): AiMessage {
  return { id, seq, role: 'assistant', kind: 'operation', text: '', target: null, references: [], operation_id: null,
    payload: {}, created_at: TIME, ref_status: {}, ...overrides };
}

function request(id: string, seq = 1): AiMessage {
  return message(`request-${id}`, seq, { role: 'user', kind: 'request', text: 'A short request', operation_id: id,
    payload: { client_request_id: `client-${id}` } });
}

function child(id: string, root: string, overrides: Partial<AiOperation> = {}): AiOperation {
  return op(id, { kind: 'model_create', message_id: `request-${root}`, parent_operation_id: root,
    request: { ...op(id).request, operation: 'model_create', root_op_id: root, parent_op_id: root }, ...overrides });
}

function turn(id: string, steps: Partial<OrchestratorStep>[] = [], overrides: Partial<AiOperation> = {}): AiOperation {
  return op(id, { request: { ...op(id).request, runtime: 'orchestrator', trigger: { type: 'request' }, root_op_id: id },
    result: { kind: 'orchestrator', trigger: { type: 'request' }, simulated: true,
      steps: steps.map((s, index) => ({ index, tool: 'plan_design', title: '', args: {}, status: 'ok', message: '', refs: {},
        started_at: TIME, finished_at: TIME, ...s })) } as unknown as AiOperation['result'], ...overrides });
}

function output(): AiResultItem {
  return { ref: { key: 'part_revision:revision', kind: 'part_revision', object_id: 'revision', title: 'Saved part',
    version_label: 'R1', project_id: 'project', part_id: 'part' } as AiResultItem['ref'],
    asset_version_id: null, asset_id: null, asset_type: 'part_model', label: 'R1', base: null,
    run_id: 'run', node_id: 'model', availability: 'available', publish_state: null, actions: ['view'] };
}

function cad(): CadResult {
  return { event_id: 'saved', object_type: 'assembly', object_id: 'assembly', base_version_id: null, version_id: 'revision',
    version_label: 'A1', asset_version_id: null, requirements: [], changes: [], checks: {}, artifacts: [],
    undo: { eligible: false, expected_head: 'revision' } };
}

function completion(overrides: Partial<AiCompletionEvent> = {}): AiCompletionEvent {
  return { event_id: 'event', event_seq: 1, generation: 1, phase: 'terminal', outcome: 'succeeded', wake_state: 'consumed', notice_seq: 4, ...overrides };
}

function journeys(entries: ConversationEntry[]): GenerationJourney[] {
  return entries.flatMap((e) => e.kind === 'journey' ? [e.journey] : []);
}

function project(operations: AiOperation[], messages: AiMessage[] = [request(operations[0].id)]): GenerationJourney {
  const result = journeys(conversationEntries({ operations, messages }));
  expect(result).toHaveLength(1);
  return result[0];
}

function allMessages(entries: ConversationEntry[]): AiMessage[] {
  return entries.flatMap((e) => e.kind === 'message' ? [e.message] : [e.journey.request, ...e.journey.messages]);
}

describe('conversation journey lineage', () => {
  it('keeps a completed root running while its explicitly owned child runs', () => {
    const root = turn('root');
    const modeling = child('model', root.id, { status: 'running' });
    expect(project([root, modeling])).toMatchObject({ id: root.id, status: 'running', focusOperationId: modeling.id });
  });

  it('groups request, child, notice and wake by identity in message order without mutating input', () => {
    const root = turn('root');
    const modeling = child('model', root.id, { result: { kind: 'model', items: [output()] } });
    const wake = turn('wake', [], { message_id: root.message_id,
      request: { ...op('wake').request, runtime: 'orchestrator', root_op_id: root.id,
        trigger: { type: 'sub_agent', operation_id: modeling.id, status: 'succeeded' } } });
    const messages = [request(root.id), message('root-op', 2, { operation_id: root.id }),
      message('model-op', 3, { operation_id: modeling.id }),
      message('notice', 4, { kind: 'notice', payload: { event: 'sub_agent_finished', operation_id: modeling.id } }),
      message('wake-op', 5, { operation_id: wake.id }), message('answer', 6, { kind: 'answer', operation_id: wake.id, text: 'Finished.' })];
    const detail = { operations: [wake, modeling, root], messages: [...messages].reverse() };
    const before = JSON.stringify(detail);
    const entries = conversationEntries(detail);
    const [journey] = journeys(entries);
    expect(entries).toHaveLength(1);
    expect(journey.operations.map((o) => o.id)).toEqual(['root', 'model', 'wake']);
    expect(journey.messages.map((m) => m.id)).toEqual(messages.slice(1).map((m) => m.id));
    expect(journey.latestAnswer).toBe(messages[5]);
    expect(journey).toMatchObject({ hasSavedResult: true, status: 'generated', focusOperationId: modeling.id });
    expect(JSON.stringify(detail)).toBe(before);
  });

  it('keeps interleaved human requests independent even when tools read another operation', () => {
    const a = turn('a', [{ tool: 'get_operation_status', refs: { operation_id: 'model-b' } }]);
    const b = turn('b');
    const aChild = child('model-a', a.id);
    const bChild = child('model-b', b.id, { status: 'running' });
    const messages = [request('a'), message('a-op', 2, { operation_id: a.id }), request('b', 3),
      message('b-op', 4, { operation_id: b.id }), message('b-child', 5, { operation_id: bChild.id }),
      message('a-child', 6, { operation_id: aChild.id }), message('a-answer', 7, { kind: 'answer', operation_id: a.id })];
    const entries = conversationEntries({ operations: [bChild, aChild, b, a], messages: [...messages].reverse() });
    expect(journeys(entries).map((j) => [j.id, j.operations.map((o) => o.id), j.status])).toEqual([
      ['a', ['a', 'model-a'], 'complete'], ['b', ['b', 'model-b'], 'running'],
    ]);
    expect(allMessages(entries).map((m) => m.id).sort()).toEqual(messages.map((m) => m.id).sort());
  });

  it('treats a human retry of a child as its own root despite inherited ancestry', () => {
    const root = turn('root');
    const failed = child('failed', root.id, { status: 'failed', retried_by: 'retry' });
    const retry = child('retry', root.id, { message_id: 'request-retry', retry_of: failed.id,
      result: { kind: 'model', items: [output()] } });
    const wake = turn('wake', [], { message_id: retry.message_id, request: { ...op('wake').request,
      runtime: 'orchestrator', root_op_id: root.id, trigger: { type: 'sub_agent', operation_id: retry.id, status: 'succeeded' } } });
    const entries = conversationEntries({ operations: [wake, retry, failed, root], messages: [request(root.id),
      message('old-child', 2, { operation_id: failed.id }), request(retry.id, 3),
      message('new-child', 4, { operation_id: retry.id }), message('new-wake', 5, { operation_id: wake.id })] });
    expect(journeys(entries).map((j) => [j.id, j.operations.map((o) => o.id), j.status])).toEqual([
      ['root', ['root', 'failed'], 'failed'], ['retry', ['retry', 'wake'], 'generated'],
    ]);
  });

  it('keeps root retries separate and never uses retry_of as ancestry', () => {
    const root = turn('root', [], { status: 'failed' });
    const retry = turn('retry', [], { retry_of: root.id, request: { ...root.request, client_request_id: 'client-retry' } });
    const orphanRetry = op('orphan-retry', { retry_of: root.id });
    const entries = conversationEntries({ operations: [root, retry, orphanRetry], messages: [request(root.id), request(retry.id, 3),
      message('orphan-op', 5, { operation_id: orphanRetry.id })] });
    expect(journeys(entries).map((j) => [j.id, j.status])).toEqual([['root', 'failed'], ['retry', 'complete']]);
    expect(entries[2]).toMatchObject({ kind: 'message', message: { id: 'orphan-op' } });
  });

  it('follows explicit parent, spawned-by, confirmation and choice triggers without root metadata', () => {
    const root = turn('root');
    const spawned = op('spawned', { request: { ...op('spawned').request, spawned_by: { operation_id: root.id, step: 1 } } });
    const confirmed = turn('confirmed', [], { request: { ...op('confirmed').request, runtime: 'orchestrator',
      trigger: { type: 'confirmed', operation_id: spawned.id, step: 2 } } });
    const choice = turn('choice', [], { request: { ...op('choice').request, runtime: 'orchestrator', trigger: { type: 'design_choice',
      operation_id: confirmed.id, choice_id: 'choice-id', question_set_id: 'questions', answered_design_ref: designChoiceQuestion.design_ref } } });
    expect(project([root, choice, confirmed, spawned]).operations.map((o) => o.id).sort()).toEqual(['choice', 'confirmed', 'root', 'spawned']);
  });

  it('retains a bound human design response inside the same journey', () => {
    const root = turn('root');
    const response = message('response', 3, { role: 'user', kind: 'request', operation_id: root.id,
      payload: { event: 'design_choice' }, text: 'Use the recommended values.' });
    const result = project([root], [response, request(root.id)]);
    expect(result.request.id).toBe('request-root');
    expect(result.messages).toEqual([response]);
  });

  it('uses only an unambiguous saved request identity as its legacy fallback', () => {
    const root = op('root');
    const m = request(root.id);
    m.operation_id = null;
    expect(project([root], [m]).root).toBe(root);
    const ambiguous = op('another', { message_id: m.id });
    m.payload = {};
    expect(conversationEntries({ messages: [m], operations: [root, ambiguous] })).toEqual([{ kind: 'message', message: m }]);
  });

  it('does not assign orphan, cyclic, unknown notice/card or unbound answer messages by proximity', () => {
    const root = turn('root');
    const a = op('cycle-a', { request: { ...op('cycle-a').request, parent_op_id: 'cycle-b' } });
    const b = op('cycle-b', { request: { ...op('cycle-b').request, parent_op_id: 'cycle-a' } });
    const orphan = op('orphan', { request: { ...op('orphan').request, root_op_id: 'missing' } });
    const messages = [request(root.id), message('cycle-a-op', 2, { operation_id: a.id }), message('cycle-b-op', 3, { operation_id: b.id }),
      message('orphan-op', 4, { operation_id: orphan.id }), message('unknown-notice', 5, { kind: 'notice', operation_id: root.id,
        payload: { event: 'future_notice', operation_id: root.id } }), message('unknown-card', 6, { kind: 'card', operation_id: root.id }),
      message('answer', 7, { kind: 'answer', text: 'Same request, all done.' })];
    const entries = conversationEntries({ operations: [b, root, orphan, a], messages: [...messages].reverse() });
    expect(journeys(entries)[0].operations).toEqual([root]);
    expect(entries).toHaveLength(messages.length);
    expect(allMessages(entries).map((m) => m.id)).toEqual(messages.map((m) => m.id));
  });

  it('uses a known root when a parent is absent and preserves messages with no loaded operation', () => {
    const root = turn('root');
    const c = child('child', root.id, { parent_operation_id: 'gone', request: { ...op('child').request, root_op_id: root.id, parent_op_id: 'gone' } });
    const missing = request('unloaded', 9);
    const entries = conversationEntries({ operations: [c, root], messages: [request(root.id), missing] });
    expect(journeys(entries)[0].operations.map((o) => o.id).sort()).toEqual(['child', 'root']);
    expect(entries[1]).toEqual({ kind: 'message', message: missing });
  });

  it('does not transfer cyclic ancestry between two explicit human roots', () => {
    const a = op('cycle-a', { request: { ...op('cycle-a').request, parent_op_id: 'cycle-b', root_op_id: 'a' } });
    const b = op('cycle-b', { request: { ...op('cycle-b').request, parent_op_id: 'cycle-a', root_op_id: 'b' } });
    const messages = [request('a'), request('b', 2), message('cycle-a-op', 3, { operation_id: a.id }), message('cycle-b-op', 4, { operation_id: b.id })];
    for (const operations of [[op('a'), op('b'), a, b], [b, a, op('b'), op('a')]]) {
      const entries = conversationEntries({ operations, messages });
      expect(journeys(entries).map((j) => j.operations.map((o) => o.id))).toEqual([['a'], ['b']]);
      expect(allMessages(entries).map((m) => m.id)).toEqual(messages.map((m) => m.id));
    }
  });

  it('keeps conflicting direct parent identities unowned instead of choosing array order', () => {
    const conflicting = op('conflicting', { parent_operation_id: 'a', request: { ...op('conflicting').request, parent_op_id: 'b' } });
    const entries = conversationEntries({ operations: [op('a'), conflicting, op('b')],
      messages: [request('a'), request('b', 2), message('conflicting-op', 3, { operation_id: conflicting.id })] });
    expect(journeys(entries).map((j) => j.operations.map((o) => o.id))).toEqual([['a'], ['b']]);
    expect(entries[2].kind).toBe('message');
  });
});

describe('truthful journey status', () => {
  it.each(['pending', 'running'] as const)('reports %s children as running after the answer ended', (status) => {
    expect(project([turn('root'), child('child', 'root', { status })]).status).toBe('running');
  });

  it('prioritizes a real decision over concurrent progress and retains its exact operation', () => {
    const wait = child('decision', 'root', { status: 'waiting_input', question: designChoiceQuestion });
    expect(project([turn('root'), wait, child('building', 'root', { status: 'running' })])).toMatchObject({
      status: 'waiting', focusOperationId: wait.id,
    });
  });

  it('does not revive an answered design question retained in terminal history', () => {
    const root = turn('root', [{ status: 'ok' }], { question: { ...designChoiceQuestion, can_answer: false } });
    expect(project([root]).status).toBe('complete');
  });

  it.each(['queued', 'running', 'waiting'] as const)('uses the live run state %s even with a terminal operation copy', (status) => {
    const c = child('child', 'root', { run: { id: 'run', title: 'Work', status } });
    expect(project([turn('root'), c])).toMatchObject({ status: status === 'waiting' ? 'waiting' : 'running', focusOperationId: c.id });
  });

  it.each(['pending', 'assigned'] as const)('waits for the relevant %s completion delivery', (wake_state) => {
    const c = child('child', 'root', { result: { kind: 'model', items: [output()] }, completion_events: [completion({ wake_state })] });
    expect(project([turn('root'), c])).toMatchObject({ status: 'running', hasSavedResult: true, focusOperationId: c.id });
  });

  it('ignores stale generations and superseded completion delivery states', () => {
    const c = child('child', 'root', { generation: 2, completion_events: [completion({ wake_state: 'pending' }),
      completion({ generation: 2, event_seq: 2, wake_state: 'assigned' }),
      completion({ generation: 2, event_seq: 3, phase: 'applied', wake_state: 'consumed' })] });
    expect(project([turn('root'), c]).status).toBe('complete');
  });

  it('keeps failed completion delivery visible despite saved output', () => {
    const c = child('child', 'root', { result: { kind: 'model', items: [output()] },
      completion_events: [completion({ wake_state: 'delivery_failed' })] });
    expect(project([turn('root'), c])).toMatchObject({ status: 'partial', hasSavedResult: true, focusOperationId: c.id });
  });

  it('does not let a successful answer hide a failed planning tool', () => {
    const root = turn('root', [{ status: 'failed', error: { code: 'bad_plan', message: 'Invalid plan.' } }]);
    const answer = message('answer', 2, { kind: 'answer', operation_id: root.id, text: 'Everything succeeded and was repaired.' });
    expect(project([root], [request(root.id), answer])).toMatchObject({ status: 'failed', hasSavedResult: false, latestAnswer: answer });
  });

  it('exposes unresolved operation errors independently of a running journey summary', () => {
    const root = turn('root', [{ status: 'failed' }]);
    expect(project([root, child('child', root.id, { status: 'running' })]).status).toBe('running');
    expect(operationHasFailure(root)).toBe(true);
    expect(operationHasFailure(turn('recovered', [{ tool: 'project_overview', status: 'failed' }, { tool: 'project_overview', status: 'ok' }]))).toBe(false);
  });

  it('recognizes a later successful same-tool call in the same turn while retaining the failed record', () => {
    const root = turn('root', [{ index: 4, tool: 'project_overview', status: 'ok' }, { index: 1, tool: 'project_overview', status: 'failed' }]);
    expect(project([root]).status).toBe('complete');
    expect(root.result).toMatchObject({ steps: [{ index: 4, status: 'ok' }, { index: 1, status: 'failed' }] });
    expect(project([turn('root', [{ status: 'ok' }, { status: 'failed' }])]).status).toBe('failed');
  });

  it('recovers a design planning failure only after the same turn saves the matching design version', () => {
    const refs: OrchestratorStep['refs'] & { design_version_id: string } = { design_version_id: designChoicePlan.ref.version_id };
    const root = turn('root', [{ status: 'failed', args: { description: 'Original intent' } },
      { status: 'ok', args: { description: 'Clarified intent' }, refs }]);
    root.result = { ...root.result!, design_plan: { ...designChoicePlan, status: 'adopted' } };
    expect(project([root]).status).toBe('complete');
    expect(operationHasFailure(root)).toBe(false);
    expect(root.result).toMatchObject({ steps: [{ status: 'failed' }, { status: 'ok' }] });
    expect(project([root, child('model', root.id, { result: { kind: 'model', items: [output()] } })]).status).toBe('generated');
  });

  it.each(['missing_ref', 'mismatched_ref', 'missing_plan'] as const)('keeps a design failure when saved recovery evidence is %s', (condition) => {
    const refs: OrchestratorStep['refs'] & { design_version_id?: string } = condition === 'missing_ref' ? {}
      : { design_version_id: condition === 'mismatched_ref' ? 'another-version' : designChoicePlan.ref.version_id };
    const root = turn('root', [{ status: 'failed', args: { description: 'Original intent' } },
      { status: 'ok', args: { description: 'Clarified intent' }, refs }]);
    if (condition !== 'missing_plan') root.result = { ...root.result!, design_plan: { ...designChoicePlan, status: 'adopted' } };
    expect(project([root]).status).toBe('failed');
    // Identical descriptions are not evidence that the authoritative design was saved either.
    const unchangedArgs = turn('same-args', [{ status: 'failed' }, { status: 'ok', refs }]);
    if (condition !== 'missing_plan') unchangedArgs.result = { ...unchangedArgs.result!, design_plan: { ...designChoicePlan, status: 'adopted' } };
    expect(operationHasFailure(unchangedArgs)).toBe(true);
  });

  it('does not use an earlier saved plan or a different turn to recover a later design failure', () => {
    const refs: OrchestratorStep['refs'] & { design_version_id: string } = { design_version_id: designChoicePlan.ref.version_id };
    const root = turn('root', [{ status: 'ok', refs }, { status: 'failed' }]);
    root.result = { ...root.result!, design_plan: { ...designChoicePlan, status: 'adopted' } };
    const wake = turn('wake', [{ status: 'ok', refs }], { request: { ...op('wake').request, runtime: 'orchestrator', parent_op_id: root.id } });
    wake.result = { ...wake.result!, design_plan: { ...designChoicePlan, status: 'adopted' } };
    expect(project([root, wake]).status).toBe('failed');
  });

  it('does not equate another successful tool or a read-only lookup with recovery', () => {
    const root = turn('root', [{ tool: 'plan_design', status: 'failed' }, { tool: 'get_operation_status', status: 'ok' }]);
    expect(project([root]).status).toBe('failed');
  });

  it('requires the same structured call scope for recovery, ignoring argument key order', () => {
    const otherObject = turn('root', [{ tool: 'modify_model', args: { part_id: 'a' }, status: 'failed' },
      { tool: 'modify_model', args: { part_id: 'b' }, status: 'ok' }]);
    expect(project([otherObject]).status).toBe('failed');
    const sameObject = turn('root', [{ tool: 'modify_model', args: { part_id: 'a', edit: { width: 2, depth: 3 } }, status: 'failed' },
      { tool: 'modify_model', args: { edit: { depth: 3, width: 2 }, part_id: 'a' }, status: 'ok' }]);
    expect(project([sameObject]).status).toBe('complete');
  });

  it('does not erase a failed child because another child or root succeeded', () => {
    expect(project([turn('root'), child('bad', 'root', { status: 'failed' }), child('good', 'root')]).status).toBe('failed');
  });

  it('accepts an explicitly linked successful retry inside the same journey, not an arbitrary replacement', () => {
    const failed = child('failed', 'root', { status: 'failed', retried_by: 'retry' });
    const retry = child('retry', 'root', { retry_of: failed.id, result: { kind: 'model', items: [output()] } });
    expect(project([turn('root'), failed, retry])).toMatchObject({ status: 'generated', hasSavedResult: true });
    expect(project([turn('root'), failed, { ...retry, retry_of: null, id: 'other' }]).status).toBe('partial');
  });

  it('does not recover cyclic retry metadata or a retry with its own failed tools', () => {
    const failed = child('failed', 'root', { status: 'failed', retry_of: 'retry', retried_by: 'retry' });
    const retry = child('retry', 'root', { status: 'failed', retry_of: failed.id, retried_by: failed.id });
    expect(project([turn('root'), failed, retry]).status).toBe('failed');
    const failedTurn = turn('retry', [{ status: 'failed' }], { retry_of: failed.id,
      request: { ...op('retry').request, runtime: 'orchestrator', parent_op_id: 'root' } });
    expect(project([turn('root'), failed, failedTurn]).status).toBe('failed');
  });

  it.each(['failed', 'partial'] as const)('reports partial saved results when the underlying run is %s', (status) => {
    const c = child('child', 'root', { run: { id: 'run', title: 'Work', status }, result: { kind: 'model', items: [output()] } });
    expect(project([turn('root'), c])).toMatchObject({ status: 'partial', hasSavedResult: true, focusOperationId: c.id });
  });

  it('does not report a partial run as a complete answer even if no normalized result is loaded', () => {
    expect(project([turn('root'), child('child', 'root', { run: { id: 'run', title: 'Work', status: 'partial' } })]).status).toBe('partial');
  });

  it('retains an explicitly partial run even if a later wake also failed', () => {
    const root = turn('root');
    const c = child('child', root.id, { run: { id: 'run', title: 'Work', status: 'partial' } });
    const wake = turn('wake', [], { status: 'failed', request: { ...op('wake').request, runtime: 'orchestrator',
      root_op_id: root.id, trigger: { type: 'sub_agent', operation_id: c.id, status: 'failed' } } });
    expect(project([root, c, wake], [request(root.id), message('child-op', 2, { operation_id: c.id }),
      message('wake-op', 3, { operation_id: wake.id })]).status).toBe('partial');
  });

  it('keeps failed result checks distinct from generated unverified results', () => {
    const c = child('child', 'root', { result: { kind: 'model', items: [output()], check: { status: 'failed' } } });
    expect(project([turn('root'), c]).status).toBe('partial');
    c.result = { kind: 'model', items: [output()], check: { status: 'skipped_turbo' } };
    expect(project([turn('root'), c]).status).toBe('generated');
  });

  it('reports cancellation honestly and preserves any partial saved products', () => {
    const cancelled = child('child', 'root', { status: 'cancelled' });
    expect(project([turn('root'), cancelled]).status).toBe('cancelled');
    cancelled.result = { kind: 'model', items: [output()] };
    expect(project([turn('root'), cancelled])).toMatchObject({ status: 'partial', hasSavedResult: true });
  });

  it('keeps an unapplied plan distinct from a saved result and still exposes failure in siblings', () => {
    const plan = child('plan', 'root', { kind: 'workflow_create', result: { kind: 'workflow_create', application: null } as unknown as AiOperation['result'] });
    expect(project([turn('root'), plan])).toMatchObject({ status: 'plan_ready', hasSavedResult: false, focusOperationId: plan.id });
    expect(project([turn('root'), plan, child('bad', 'root', { status: 'failed' })]).status).toBe('failed');
  });

  it('distinguishes applied and undone plans without inventing generated CAD', () => {
    const applied = child('plan', 'root', { kind: 'workflow_create', result: { kind: 'workflow_create', application: { state: 'applied' } } as unknown as AiOperation['result'] });
    expect(project([turn('root'), applied])).toMatchObject({ status: 'generated', hasSavedResult: true });
    applied.result = { kind: 'workflow_create', application: { state: 'undone' } } as unknown as AiOperation['result'];
    expect(project([turn('root'), applied])).toMatchObject({ status: 'complete', hasSavedResult: false });
  });

  it('uses exact saved CAD identity even when legacy normalized items are absent', () => {
    const c = child('child', 'root', { result: { kind: 'assembly', items: [], cad_result: cad() } });
    expect(project([turn('root'), c])).toMatchObject({ status: 'generated', hasSavedResult: true, focusOperationId: c.id });
    c.result = { kind: 'model', items: [{ ref: { kind: 'part_revision', object_id: 'revision' } }] } as unknown as AiOperation['result'];
    expect(project([turn('root'), c])).toMatchObject({ status: 'complete', hasSavedResult: false });
  });
});

describe('current journey selection', () => {
  it('selects the newest waiting/active task instead of a newer finished task', () => {
    const operations = [op('waiting', { status: 'waiting_input' }), op('active', { status: 'running' }), op('done')];
    const entries = conversationEntries({ operations, messages: [request('waiting', 1), request('active', 3), request('done', 5)] });
    expect(currentJourney(entries)?.id).toBe('active');
    expect(currentJourney(entries.slice(0, 1))?.id).toBe('waiting');
  });

  it('otherwise selects the latest task and tolerates unknown-only or empty conversations', () => {
    const entries = conversationEntries({ operations: [op('a'), op('b')], messages: [request('a'), request('b', 3), message('notice', 4, { kind: 'notice' })] });
    expect(currentJourney(entries)?.id).toBe('b');
    expect(currentJourney([])).toBeNull();
    expect(currentJourney([{ kind: 'message', message: message('unknown', 1) }])).toBeNull();
  });
});

describe('manager delivery answer ownership', () => {
  function wake(id = 'wake', context: string[] = ['event']): AiOperation {
    const result = turn(id, [], { request: { ...op(id).request, runtime: 'orchestrator', root_op_id: 'root',
      trigger: { type: 'sub_agent', operation_id: 'child', status: 'succeeded' } } });
    orchestratorResult(result)!.completion_context = context;
    return result;
  }
  function model(id = 'child', event = completion()): AiOperation {
    return child(id, 'root', { generation: event.generation, result: { kind: 'model', items: [output()] }, completion_events: [event] });
  }
  function answer(id = 'reply', seq = 7, owner = 'wake'): AiMessage {
    return message(id, seq, { kind: 'answer', operation_id: owner, text: 'Finished the whole project.' });
  }

  it.each(['running', 'waiting_input'] as const)('keeps a final-sounding launch answer as progress while a child is %s', (status) => {
    const j = project([turn('root'), { ...model(), status }], [request('root'), answer('startup', 4, 'root')]);
    expect(journeyAnswerRole(j)).toBe('progress');
  });

  it('requires the actual current answer owner to consume a current event, including fast terminal children', () => {
    const startup = answer('startup', 4, 'root');
    const pending = project([turn('root'), model('child', completion({ wake_state: 'pending' }))], [request('root'), startup]);
    expect(journeyAnswerRole(pending)).toBe('progress');
    const delivered = project([turn('root'), model(), wake()], [request('root'), startup, answer()]);
    expect(journeyAnswerRole(delivered)).toBe('delivery');
    // A consumed event or another manager's receipt cannot turn the original answer into a final summary.
    expect(journeyAnswerRole(project(delivered.operations, [request('root'), startup]))).toBe('response');
    expect(journeyAnswerRole(project(delivered.operations, [request('root'), answer('root-later', 8, 'root')]))).toBe('response');
  });

  it('keeps legacy/missing receipts, wrong owners and cross-conversation records neutral', () => {
    for (const manager of [turn('wake', [], { request: wake().request }), wake('wake', []), wake('wake', ['different-event']),
      { ...wake(), conversation_id: 'other-conversation' }, { ...wake(), request: { ...wake().request, runtime: 'tool_agent' } }]) {
      expect(journeyAnswerRole(project([turn('root'), model(), manager], [request('root'), answer()]))).toBe('response');
    }
    expect(journeyAnswerRole(project([turn('root'), model(), wake()], [request('root'), answer('missing', 7, 'root')]))).toBe('response');
    const malformed = wake();
    Object.assign(orchestratorResult(malformed)!, { completion_context: 'event' });
    expect(journeyAnswerRole(project([turn('root'), model(), malformed], [request('root'), answer()]))).toBe('response');
    const legacy = model();
    delete legacy.completion_events;
    expect(journeyAnswerRole(project([turn('root'), legacy, wake()], [request('root'), answer()]))).toBe('response');
    expect(journeyAnswerRole(project([turn('root'), { ...model(), conversation_id: 'other-conversation' }, wake()],
      [request('root'), answer()]))).toBe('response');
  });

  it.each(['pending', 'assigned', 'delivery_failed', 'suppressed', 'legacy_consumed'] as const)(
    'never claims a delivery from %s event state', (wake_state) => {
      expect(journeyAnswerRole(project([turn('root'), model('child', completion({ wake_state })), wake()],
        [request('root'), answer()]))).not.toBe('delivery');
    },
  );

  it.each([null, 7, 8])('does not use a missing or future notice sequence %s as current-answer evidence', (notice_seq) => {
    expect(journeyAnswerRole(project([turn('root'), model('child', completion({ notice_seq })), wake()],
      [request('root'), answer()]))).toBe('response');
  });

  it('rejects stale generation or phase receipts even when all visible operations succeeded', () => {
    const old = completion();
    const current = completion({ event_id: 'generation-2', event_seq: 2, generation: 2 });
    const c = { ...model('child', current), completion_events: [old, current] };
    const messages = [request('root'), answer()];
    expect(journeyAnswerRole(project([turn('root'), c, wake()], messages))).toBe('response');
    expect(journeyAnswerRole(project([turn('root'), c, wake('wake', ['generation-2'])], messages))).toBe('delivery');
    const applied = completion({ ...current, event_id: 'applied', event_seq: 3, phase: 'applied', outcome: 'applied' });
    c.completion_events.push(applied);
    expect(journeyAnswerRole(project([turn('root'), c, wake('wake', ['generation-2'])], messages))).toBe('response');
    expect(journeyAnswerRole(project([turn('root'), c, wake('wake', ['applied'])], messages))).toBe('delivery');
  });

  it('requires every coalesced current child outcome and ignores repeated polling copies', () => {
    const children = [model(), model('second-child', completion({ event_id: 'second-event' }))];
    const messages = [request('root'), answer()];
    expect(journeyAnswerRole(project([turn('root'), ...children, wake()], messages))).toBe('response');
    const operations = [turn('root'), ...children, wake('wake', ['event', 'second-event'])];
    const before = JSON.stringify({ operations, messages });
    expect(journeyAnswerRole(project(operations, messages))).toBe('delivery');
    expect(journeyAnswerRole(project(JSON.parse(JSON.stringify(operations)), JSON.parse(JSON.stringify(messages))))).toBe('delivery');
    expect(JSON.stringify({ operations, messages })).toBe(before);
  });

  it('recognizes a late child only after its own answer, while retaining earlier exact receipts', () => {
    const first = model();
    const second = model('second-child', completion({ event_id: 'late-event', notice_seq: 8 }));
    const firstWake = wake();
    const secondWake = wake('late-wake', ['late-event']);
    const firstMessages = [request('root'), answer()];
    expect(journeyAnswerRole(project([turn('root'), first, firstWake], firstMessages))).toBe('delivery');
    expect(journeyAnswerRole(project([turn('root'), first, firstWake, second], firstMessages))).toBe('response');
    const operations = [turn('root'), first, firstWake, second, secondWake];
    const messages = [...firstMessages, answer('late-reply', 10, 'late-wake')];
    expect(journeyAnswerRole(project(operations, messages))).toBe('delivery');
    // An operation receipt without a saved answer, or an answer before its notice, is not earlier coverage.
    expect(journeyAnswerRole(project(operations, [request('root'), messages[2]]))).toBe('response');
    expect(journeyAnswerRole(project(operations, [request('root'), answer('early-reply', 4), messages[2]]))).toBe('response');
    expect(journeyAnswerRole(project(operations.map((op) => op.id === firstWake.id ? { ...op, status: 'failed' } : op), messages))).toBe('response');
  });

  it('retains failure, partial, cancellation and unapplied-plan outcomes instead of a delivery label', () => {
    const messages = [request('root'), answer()];
    const changes: Partial<AiOperation>[] = [
      { status: 'failed' }, { run: { id: 'run', title: 'Partial', status: 'partial' } }, { status: 'cancelled' },
      { kind: 'workflow_create', result: { kind: 'workflow_create', application: null } as unknown as AiOperation['result'] },
    ];
    for (const patch of changes) expect(journeyAnswerRole(project([turn('root'), { ...model(), ...patch }, wake()], messages))).toBe('response');
    expect(journeyAnswerRole(project([turn('root')], [request('root'), answer('ordinary', 4, 'root')]))).toBe('response');
  });
});

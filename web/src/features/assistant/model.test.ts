import { describe, expect, it } from 'vitest';
import type { AiCompletionEvent, AiComposer, AiConversationDetail, AiMessage, AiOperation, AiRef, AiTaskDraft } from '../../api/assistant';
import {
  applyDraft,
  applySendResult,
  clipConstraint,
  composerSendKeys,
  hasActiveOperations,
  historyRefs,
  mergeDetail,
  newRequestId,
  operationEvents,
  pollDelay,
  POLL_MAX_MS,
  POLL_MS,
  previousUserMessage,
  requestRecorded,
  retriedByMap,
  sameObject,
  sendBlock,
} from './model';

const T0 = '2026-10-08T10:00:00Z';

function ref(key: string, kind: AiRef['kind'] = 'part_revision', label = 'R3'): AiRef {
  return {
    key,
    kind,
    project_id: 'p1',
    object_id: key.split(':')[1] ?? key,
    part_id: 'part1',
    task_id: null,
    version_label: label,
    title: 'Bracket',
    summary: '',
    selection: null,
    field: null,
    excerpt: null,
    output: null,
    source_path: '/parts/part1/model',
    snapshot_at: T0,
  };
}

function op(id: string, status: AiOperation['status'], updated = T0): AiOperation {
  return {
    id,
    conversation_id: 'c1',
    message_id: `m-${id}`,
    kind: 'model_modify',
    status,
    request: {
      text: 'x',
      operation: 'model_modify',
      requested_operation: 'auto',
      target: null,
      references: [],
      constraints: [],
      task_id: null,
      model_config_id: null,
      client_request_id: `crid-${id}`,
    },
    task: null,
    part: null,
    run: null,
    question: null,
    result: null,
    error: null,
    simulated: false,
    retry_of: null,
    created_at: T0,
    updated_at: updated,
    finished_at: null,
    context_build: null,
  };
}

function msg(id: string, seq: number, role: AiMessage['role'] = 'user', extra: Partial<AiMessage> = {}): AiMessage {
  return { id, seq, role, kind: role === 'user' ? 'request' : 'operation', text: id, target: null, references: [], operation_id: null, payload: {}, created_at: T0, ref_status: {}, ...extra };
}

const composer: AiComposer = { target_key: 'part_revision:r3', references: [ref('part_revision:r3'), ref('quote:q1', 'quote', 'Q1')], ref_status: {} };

function detail(over: Partial<AiConversationDetail> = {}, version = 1): AiConversationDetail {
  return {
    conversation: {
      id: 'c1',
      title: 't',
      project: { id: 'p1', code: 'P', name: 'P' },
      task: null,
      archived: false,
      archived_at: null,
      constraints: [],
      summary_status: null,
      forked_from: null,
      version,
      last_activity_at: T0,
      created_at: T0,
    },
    messages: [],
    operations: [],
    composer,
    ...over,
  };
}

describe('polling stop condition', () => {
  it('polls only while an operation is unfinished', () => {
    expect(hasActiveOperations(null)).toBe(false);
    expect(hasActiveOperations(detail({ operations: [op('a', 'succeeded'), op('b', 'failed'), op('c', 'cancelled')] }))).toBe(false);
    expect(hasActiveOperations(detail({ operations: [op('a', 'succeeded'), op('b', 'running')] }))).toBe(true);
    expect(hasActiveOperations(detail({ operations: [op('a', 'waiting_input')] }))).toBe(true);
    expect(hasActiveOperations(detail({ operations: [op('a', 'pending')] }))).toBe(true);
  });
  it('backs off after failures and caps the delay', () => {
    expect(pollDelay(0)).toBe(POLL_MS);
    expect(pollDelay(1)).toBe(POLL_MS * 2);
    expect(pollDelay(20)).toBe(POLL_MAX_MS);
  });
});

describe('merging server responses', () => {
  it('appends incremental messages once and keeps them ordered', () => {
    const prev = detail({ messages: [msg('a', 1), msg('b', 2, 'assistant')] });
    const next = detail({ messages: [msg('b', 2, 'assistant'), msg('c', 3, 'assistant')] });
    const merged = mergeDetail(prev, next, { incremental: true });
    expect(merged.messages.map((m) => m.id)).toEqual(['a', 'b', 'c']);
  });
  it('does not roll an operation back to an older copy from a slow poll', () => {
    const prev = detail({ operations: [op('a', 'cancelled', '2026-10-08T10:00:05Z')] });
    const stale = detail({ operations: [op('a', 'running', '2026-10-08T10:00:01Z')] });
    expect(mergeDetail(prev, stale, { incremental: true }).operations[0].status).toBe('cancelled');
    expect(mergeDetail(prev, stale).operations[0].status).toBe('cancelled');
  });
  it('keeps the higher conversation version and a locally changed composer', () => {
    const prev = detail({ composer: { ...composer, target_key: null } }, 3);
    const merged = mergeDetail(prev, detail({}, 2), { incremental: true, keepComposer: true });
    expect(merged.conversation.version).toBe(3);
    expect(merged.composer.target_key).toBeNull();
  });
  it('replaces everything for another conversation', () => {
    const other = detail();
    other.conversation = { ...other.conversation, id: 'c2' };
    expect(mergeDetail(detail({ messages: [msg('a', 1)] }), other, { incremental: true }).messages).toEqual([]);
  });
  it('fills the operation message an ask send result skipped when refetching after the request', () => {
    // send result: user request #5 + answer #7; the operation message #6 only comes with GET ?after_seq=4
    const sent = applySendResult(detail({ messages: [msg('old', 4)] }), { message: msg('u', 5), operation: op('o', 'succeeded'), assistant_message: msg('ans', 7, 'assistant') });
    const refetched = mergeDetail(sent, detail({ messages: [msg('u', 5), msg('opm', 6, 'assistant'), msg('ans', 7, 'assistant')], operations: [op('o', 'succeeded')] }), { incremental: true });
    expect(refetched.messages.map((m) => m.seq)).toEqual([4, 5, 6, 7]);
  });
  it('keeps a saved draft over an older polled copy, and the work a response leaves out', () => {
    const draft = (version: number, title = 'Flange'): AiTaskDraft => ({ id: 'd1', conversation_id: 'c1', operation_id: 'o', step_index: 0, part_id: null, title, objective: '',
      criteria: [], suggested_workflow_id: null, status: 'open', task_id: null, simulated: true, version, created_at: T0, updated_at: T0 });
    const work = (v: number, title?: string) => ({ drafts: [draft(v, title)], tasks: [], parts: [] });
    const saved = applyDraft(detail({ work: work(1) }), draft(2, 'Flange, 4 holes'));
    expect(saved.work?.drafts.map((d) => [d.version, d.title])).toEqual([[2, 'Flange, 4 holes']]);
    expect(mergeDetail(saved, detail({ work: work(1) }), { incremental: true }).work?.drafts[0].version).toBe(2); // slow poll
    expect(mergeDetail(saved, detail({ work: work(3, 'By the agent') })).work?.drafts[0].title).toBe('By the agent');
    expect(mergeDetail(saved, detail(), { incremental: true }).work).toBe(saved.work);
  });
  it('adds a send result without duplicating it on the next poll', () => {
    const sent = applySendResult(detail(), { message: msg('u', 1), operation: op('o', 'pending'), assistant_message: msg('a', 2, 'assistant') });
    const polled = mergeDetail(sent, detail({ messages: [msg('a', 2, 'assistant')], operations: [op('o', 'running', '2026-10-08T10:00:03Z')] }), { incremental: true });
    expect(polled.messages.map((m) => m.id)).toEqual(['u', 'a']);
    expect(polled.operations).toHaveLength(1);
    expect(polled.operations[0].status).toBe('running');
  });
});

describe('operation events', () => {
  it('emits nothing on the first load and once per status change', () => {
    expect(operationEvents(null, [op('a', 'succeeded')])).toEqual([]);
    const ev = operationEvents([op('a', 'running'), op('b', 'running')], [op('a', 'succeeded'), op('b', 'running'), op('c', 'pending')]);
    expect(ev.map((e) => [e.operation.id, e.type])).toEqual([
      ['a', 'succeeded'],
      ['c', 'status'],
    ]);
    expect(operationEvents([op('a', 'running')], [op('a', 'failed')])[0].type).toBe('failed');
  });
});

describe('request snapshot keys', () => {
  it('sends exactly the shown target and references', () => {
    expect(composerSendKeys(composer)).toEqual({ target_key: 'part_revision:r3', reference_keys: ['quote:q1'] });
    expect(composerSendKeys({ target_key: 'gone', references: composer.references })).toEqual({ target_key: null, reference_keys: ['part_revision:r3', 'quote:q1'] });
  });
  it('finds the request a reply belongs to', () => {
    const list = [msg('u1', 1), msg('a1', 2, 'assistant'), msg('u2', 3), msg('a2', 4, 'assistant')];
    expect(previousUserMessage(list, 4)?.id).toBe('u2');
    expect(previousUserMessage(list, 2)?.id).toBe('u1');
    expect(previousUserMessage(list, 1)).toBeNull();
  });
  it('lists history references newest first, once', () => {
    const list = [msg('u1', 1, 'user', { target: ref('part_revision:r3') }), msg('u2', 3, 'user', { target: ref('part_revision:r4', 'part_revision', 'R4'), references: [ref('part_revision:r3')] })];
    expect(historyRefs(list).map((h) => [h.ref.key, h.asTarget])).toEqual([
      ['part_revision:r4', true],
      ['part_revision:r3', false],
    ]);
  });
});

describe('composer send guards', () => {
  it('blocks sending for archived conversations and unavailable targets', () => {
    const base = { archived: false, text: 'make the hole 10 mm', sending: false, hasProject: true };
    expect(sendBlock(base)).toBeNull();
    expect(sendBlock({ ...base, archived: true })).toBe('archived');
    expect(sendBlock({ ...base, text: '   ' })).toBe('empty');
    expect(sendBlock({ ...base, targetStatus: { state: 'unavailable', reason: 'deleted' } })).toBe('target_unavailable');
    expect(sendBlock({ ...base, targetStatus: { state: 'stale', latest_label: 'R5' } })).toBeNull();
    expect(sendBlock({ ...base, hasProject: false })).toBe('no_project');
  });
  it('matches page suggestions by kind, object and scope', () => {
    const face = { ...ref('cad_selection:r3:face:12:1_2_3', 'cad_selection'), object_id: 'r3', selection: { entity: 'face' as const, id: 12, build_key: 'b', mesh_sha: 'm' } };
    expect(sameObject({ kind: 'cad_selection', object_id: 'r3', selection: { entity: 'face', id: '12', build_key: 'b', mesh_sha: 'm' } }, face)).toBe(true);
    expect(sameObject({ kind: 'cad_selection', object_id: 'r3', selection: { entity: 'face', id: 13, build_key: 'b', mesh_sha: 'm' } }, face)).toBe(false);
    expect(sameObject({ kind: 'part_revision', object_id: 'r3' }, ref('part_revision:r3'))).toBe(true);
  });
  it('generates v4 request ids', () => {
    const a = newRequestId();
    expect(a).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    expect(newRequestId()).not.toBe(a);
  });
});

describe('retries', () => {
  it('maps a retried operation to the newer one (server field or the newer retry_of)', () => {
    const a = { ...op('a', 'failed'), retried_by: 'b' };
    const b = { ...op('b', 'failed'), retry_of: 'a' };
    const c = { ...op('c', 'cancelled'), retry_of: 'b' };
    const m = retriedByMap([a, b, c]);
    expect(m.get('a')).toBe('b');
    expect(m.get('b')).toBe('c');
    expect(m.has('c')).toBe(false);
    // before the old copy was refetched: only the newer operation knows
    expect(retriedByMap([op('x', 'failed'), { ...op('y', 'running'), retry_of: 'x' }]).get('x')).toBe('y');
  });
  it('recognises a request the server recorded although the response was lost', () => {
    const d = detail({ messages: [msg('u1', 1, 'user', { payload: { client_request_id: 'r-1' } })], operations: [op('o1', 'running')] });
    expect(requestRecorded('r-1', d)).toBe(true);
    expect(requestRecorded('crid-o1', d)).toBe(true);
    expect(requestRecorded('r-2', d)).toBe(false);
    expect(requestRecorded(null, d)).toBe(false);
    expect(requestRecorded('r-1', null)).toBe(false);
  });
});

describe('constraints from messages', () => {
  it('keeps short text as is', () => {
    expect(clipConstraint('  wall ≥ 3 mm  ')).toEqual({ text: 'wall ≥ 3 mm', clipped: false });
    expect(clipConstraint('x'.repeat(500))).toEqual({ text: 'x'.repeat(500), clipped: false });
  });
  it('shortens to at most 500 characters with an ellipsis and says so', () => {
    const r = clipConstraint('y'.repeat(501));
    expect(r.clipped).toBe(true);
    expect(r.text.length).toBe(500);
    expect(r.text.endsWith('…')).toBe(true);
    expect(clipConstraint('z'.repeat(2000)).text.length).toBeLessThanOrEqual(500);
  });
  it('never splits a surrogate pair', () => {
    const lone = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])/;
    const whole = clipConstraint(`${'a'.repeat(497)}😀😀😀`);
    expect(whole.text.length).toBe(500);
    expect(whole.text.endsWith('😀…')).toBe(true);
    const cut = clipConstraint(`${'a'.repeat(498)}😀😀😀`);
    expect(cut.clipped).toBe(true);
    expect(cut.text.length).toBeLessThanOrEqual(500);
    expect(cut.text).not.toMatch(lone);
    expect(cut.text.endsWith('a…')).toBe(true);
  });
});

describe('saved result event identity', () => {
  const saved = (eventId: string, eligible = true): AiOperation => ({ ...op('cad', 'succeeded'), result: { kind: 'model', items: [], cad_result: {
    event_id: eventId, object_type: 'part_model', object_id: 'part1', base_version_id: 'r1', version_id: 'r2', version_label: 'R2', asset_version_id: 'av2', requirements: [], changes: [], checks: {}, artifacts: [], undo: { eligible, expected_head: 'r2' },
  } } });
  it('emits one result event when undo changes a succeeded operation, without replaying hydration', () => {
    const previous = saved('saved:r2');
    const next = saved('restored:r3', false);
    expect(operationEvents(null, [next])).toEqual([]);
    expect(operationEvents([previous], [next]).map((event) => event.type)).toEqual(['result']);
    expect(operationEvents([next], [next])).toEqual([]);
  });
  it('does not refresh geometry when only current undo eligibility changes', () => {
    expect(operationEvents([saved('saved:r2')], [saved('saved:r2', false)])).toEqual([]);
  });
  it('observes apply and parent-step undo without a status transition', () => {
    const a: AiOperation = { ...op('plan', 'succeeded'), result: { kind: 'model', items: [], application: { state: 'applied', version_after: 4 } } as AiOperation['result'] };
    const b = { ...a, result: { kind: 'model', items: [], application: { state: 'undone', undo_version: 5 } } as AiOperation['result'] };
    expect(operationEvents([a], [b])[0].type).toBe('result');
  });
  it('recognizes a recorded no-op answer after the send response is lost', () => {
    expect(requestRecorded('send-1', { messages: [msg('request', 1, 'user', { payload: { client_request_id: 'send-1' } })], operations: [] })).toBe(true);
  });
});


describe('durable completion generations', () => {
  const completion = (seq: number, patch: Partial<AiCompletionEvent> = {}): AiCompletionEvent => ({
    event_id: `event-${seq}`, generation: 1, event_seq: seq, phase: 'terminal', outcome: 'plan_ready', wake_state: 'pending', notice_seq: null, ...patch,
  });
  it('keeps a continued generation over a late response even when its timestamp is later', () => {
    const current: AiOperation = { ...op('a', 'running'), generation: 2, completion_events: [completion(1, { outcome: 'failed' })] };
    const old: AiOperation = { ...op('a', 'failed', '2026-10-08T10:01:00Z'), generation: 1, completion_events: [completion(1, { outcome: 'failed' })] };
    for (const incremental of [true, false]) expect(mergeDetail(detail({ operations: [current] }), detail({ operations: [old] }), { incremental }).operations[0]).toBe(current);
  });
  it('keeps an applied event over a plan-ready poll with the same timestamp', () => {
    const ready: AiOperation = { ...op('a', 'succeeded'), generation: 1, completion_events: [completion(1)] };
    const applied: AiOperation = { ...ready, completion_events: [...ready.completion_events!, completion(2, { phase: 'applied', outcome: 'applied' })] };
    expect(mergeDetail(detail({ operations: [applied] }), detail({ operations: [ready] })).operations[0]).toBe(applied);
    const refreshed = mergeDetail(detail({ operations: [ready] }), detail({ operations: [applied] })).operations;
    expect(operationEvents([ready], refreshed).map((e) => e.type)).toEqual(['result']);
    expect(operationEvents(refreshed, refreshed)).toEqual([]);
    expect(operationEvents(null, refreshed)).toEqual([]);
  });
  it('observes repeated terminal status for a new generation but ignores delivery-only updates', () => {
    const first: AiOperation = { ...op('a', 'succeeded'), generation: 1, completion_events: [completion(1)] };
    const delivered: AiOperation = { ...first, completion_events: [completion(1, { wake_state: 'consumed', notice_seq: 4 })] };
    expect(operationEvents([first], [delivered])).toEqual([]);
    const continued: AiOperation = { ...delivered, generation: 2, completion_events: [completion(1), completion(2, { generation: 2, outcome: 'succeeded' })] };
    expect(operationEvents([delivered], [continued]).map((e) => e.type)).toEqual(['result']);
  });
  it('continues polling a terminal child until its parent wake is delivered or closed', () => {
    for (const wake_state of ['pending', 'assigned', 'consumed', 'suppressed', 'legacy_consumed', 'delivery_failed'] as const) {
      const operation: AiOperation = { ...op('a', 'succeeded'), completion_events: [completion(1, { wake_state })] };
      expect(hasActiveOperations(detail({ operations: [operation] }))).toBe(wake_state === 'pending' || wake_state === 'assigned');
    }
  });
});

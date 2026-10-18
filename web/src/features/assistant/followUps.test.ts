import { describe, expect, it, vi } from 'vitest';
import type { AiConversationDetail, AiMessage, AiOperation, AiRef } from '../../api/assistant';
import { draftKey, EMPTY_DRAFT, readDraft, writeDraft, type DraftStorage } from './drafts';
import { answerFollowUps, followUpBlock, followUpContext, followUpDraftKey, prepareFollowUp } from './followUps';
import { requestRecorded } from './model';

const ref = (key: string) => ({ key, kind: 'part_revision', object_id: key, title: key, version_label: 'R1' }) as AiRef;
const message = (patch: Partial<AiMessage> = {}): AiMessage => ({
  id: 'reply-a', role: 'assistant', kind: 'answer', seq: 3, text: 'Answer', operation_id: 'op-a',
  target: null, references: [], payload: {}, ref_status: {}, created_at: '2026-10-18T00:00:00Z', ...patch,
});
const operation = (patch: Partial<AiOperation> = {}): AiOperation => ({
  id: 'op-a', conversation_id: 'conversation-a', kind: 'ask', status: 'succeeded',
  request: { runtime: 'orchestrator', target: ref('part_revision:original'), references: [ref('file_version:source')], client_request_id: 'original-request' },
  ...patch,
}) as AiOperation;
const detail = (patch: Partial<AiConversationDetail> = {}): AiConversationDetail => ({
  conversation: { id: 'conversation-a', archived: false }, operations: [operation()], messages: [],
  composer: { target_key: 'part_revision:latest', references: [ref('part_revision:latest'), ref('file_version:unrelated')] },
  ...patch,
}) as AiConversationDetail;

function memory(): DraftStorage {
  const data = new Map<string, string>();
  return { getItem: (key) => data.get(key) ?? null, setItem: (key, value) => { data.set(key, value); }, removeItem: (key) => { data.delete(key); } };
}

describe('answer follow-up identity', () => {
  it('keeps complete, distinct option labels without malformed or duplicate actions', () => {
    expect(answerFollowUps(message({ payload: { follow_ups: ['  Build this design  ', '', null, 2, 'Build this design', 'Another design'] } })))
      .toEqual(['Build this design', 'Another design']);
    expect(answerFollowUps(message({ payload: { follow_ups: 'Not an option list' } }))).toEqual([]);
  });

  it('isolates action storage by user, conversation, exact answer and option without touching the composer key', () => {
    const base = draftKey('user-a', 'conversation-a');
    const first = followUpDraftKey(base, 'answer-a', 'Build it');
    expect(followUpDraftKey(base, 'answer-a', 'Build it')).toBe(first);
    expect(new Set([base, first,
      followUpDraftKey(draftKey('user-b', 'conversation-a'), 'answer-a', 'Build it'),
      followUpDraftKey(draftKey('user-a', 'conversation-b'), 'answer-a', 'Build it'),
      followUpDraftKey(base, 'answer-b', 'Build it'),
      followUpDraftKey(base, 'answer-a', 'Change it'),
    ]).size).toBe(6);
    expect(followUpDraftKey(base, 'a.b', 'c')).not.toBe(followUpDraftKey(base, 'a', 'b.c'));
  });
});

describe('follow-up request context', () => {
  it('binds an empty answer snapshot to its exact owning operation despite an interleaved user request', () => {
    const unrelated = operation({ id: 'op-b', request: { ...operation().request, target: ref('part_revision:other'), references: [] } });
    const conversation = detail({ operations: [unrelated, operation()], messages: [
      message({ id: 'request-a', seq: 1, role: 'user', kind: 'request', target: ref('part_revision:original') }),
      message({ id: 'request-b', seq: 2, role: 'user', kind: 'request', operation_id: 'op-b', target: ref('part_revision:other') }),
      message(),
    ] });
    const context = followUpContext(message(), conversation);
    const body = prepareFollowUp(context, 'Explain this part', EMPTY_DRAFT, () => 'next-request').attemptedBody;
    expect(body).toEqual({ client_request_id: 'next-request', text: 'Explain this part', operation: 'auto',
      target_key: 'part_revision:original', reference_keys: ['file_version:source'], auto_references: false });
  });

  it('refuses missing linked context rather than borrowing from the answer or an unmatched operation', () => {
    const answer = message({ target: ref('part_revision:saved'), references: [ref('file_version:saved')] });
    for (const operations of [[operation({ conversation_id: 'conversation-b' })], [operation({ id: 'other' })], []]) {
      const context = followUpContext(answer, detail({ operations }));
      expect(context).toBeNull();
      const gen = vi.fn(() => 'must-not-start');
      expect(() => prepareFollowUp(context, 'Continue', EMPTY_DRAFT, gen)).toThrow();
      expect(gen).not.toHaveBeenCalled();
    }
  });

  it('uses a historical answer snapshot only when no owning operation is linked', () => {
    const answer = message({ operation_id: null, target: ref('part_revision:saved'), references: [ref('file_version:saved')] });
    expect(followUpContext(answer, detail())).toEqual({ target: answer.target, references: answer.references });
    expect(followUpContext(message({ operation_id: null }), detail())).toEqual({ target: null, references: [] });
  });

  it('keeps deliberately empty saved context explicit rather than inheriting current composer references', () => {
    const owner = operation({ request: { ...operation().request, target: null, references: [] } });
    const context = followUpContext(message({ target: ref('part_revision:other') }), detail({ operations: [owner] }));
    expect(prepareFollowUp(context, 'Build something new', EMPTY_DRAFT, () => 'new-id').attemptedBody).toEqual({
      client_request_id: 'new-id', text: 'Build something new', operation: 'auto', target_key: null, reference_keys: [], auto_references: false,
    });
  });

  it('sends each saved reference once and fails if a saved reference has no key', () => {
    const context = { target: ref('part_revision:r1'), references: [ref('part_revision:r1'), ref('file_version:f1'), ref('file_version:f1')] };
    expect(prepareFollowUp(context, 'Continue', EMPTY_DRAFT, () => 'id').attemptedBody.reference_keys).toEqual(['file_version:f1']);
    const gen = vi.fn(() => 'must-not-start');
    expect(() => prepareFollowUp({ ...context, references: [ref('')] }, 'Continue', EMPTY_DRAFT, gen)).toThrow();
    expect(gen).not.toHaveBeenCalled();
  });
});

describe('persisted direct-option attempts', () => {
  it('preserves unrelated composer text, pending sources and suppression during success or retry', () => {
    const storage = memory();
    const base = draftKey('user', 'conversation-a');
    const composer = { ...EMPTY_DRAFT, text: 'My unfinished request', contextScope: 'drawing-r2', suppressed: ['removed'],
      pending: [{ input: { kind: 'part_revision' as const, object_id: 'r2', project_id: 'p1' }, label: 'My selected part', target: true }] };
    writeDraft(storage, base, composer);
    const before = readDraft(storage, base);
    const key = followUpDraftKey(base, 'reply-a', 'Generate this');
    const attempt = prepareFollowUp(followUpContext(message(), detail()), 'Generate this', readDraft(storage, key), () => 'action-id');
    writeDraft(storage, key, attempt);
    expect(readDraft(storage, base)).toEqual(before);
    expect(readDraft(storage, key).attemptedBody?.reference_keys).toEqual(['file_version:source']);
    expect(requestRecorded(attempt.requestId, { messages: [message({ role: 'user', payload: { client_request_id: 'action-id' } })], operations: [] })).toBe(true);
    // Acknowledgement does not erase this action's identity or the normal draft.
    expect(readDraft(storage, key).requestId).toBe('action-id');
    expect(readDraft(storage, base)).toEqual(before);
  });

  it('replays an identical persisted body after a lost response, reload and changed saved context', () => {
    const storage = memory();
    const key = followUpDraftKey(draftKey('user', 'conversation-a'), 'reply-a', 'Generate this');
    const gen = vi.fn(() => 'original-action-id');
    const first = prepareFollowUp(followUpContext(message(), detail()), 'Generate this', EMPTY_DRAFT, gen);
    writeDraft(storage, key, first);
    const restored = readDraft(storage, key);
    const retry = prepareFollowUp({ target: ref('part_revision:changed'), references: [] }, 'Later wording', restored, gen);
    expect(retry.attemptedBody).toEqual(first.attemptedBody);
    expect(retry.text).toBe('Generate this');
    const missingContext = followUpContext(message(), detail({ operations: [] }));
    expect(prepareFollowUp(missingContext, 'Later wording', restored, gen).attemptedBody).toEqual(first.attemptedBody);
    expect(gen).toHaveBeenCalledTimes(1);
    expect(requestRecorded(restored.requestId, { messages: [], operations: [operation({ request: { ...operation().request, client_request_id: 'original-action-id' } })] })).toBe(true);
  });

  it('blocks fresh options during an active main turn while allowing an unresolved idempotent retry', () => {
    const attempt = prepareFollowUp({ target: null, references: [] }, 'Continue', EMPTY_DRAFT, () => 'saved-id');
    for (const status of ['pending', 'running'] as const) {
      const active = detail({ operations: [operation({ status })] });
      expect(followUpBlock(active, EMPTY_DRAFT)).toBe('busy');
      expect(followUpBlock(active, attempt)).toBeNull();
    }
    expect(followUpBlock(detail({ operations: [operation({ status: 'waiting_input' })] }), EMPTY_DRAFT)).toBeNull();
    expect(followUpBlock(detail({ operations: [operation({ kind: 'model_create', status: 'running' })] }), EMPTY_DRAFT)).toBeNull();
    const archived = detail();
    archived.conversation.archived = true;
    expect(followUpBlock(archived, attempt)).toBe('archived');
  });
});

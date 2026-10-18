import { describe, expect, it } from 'vitest';
import {
  afterSent,
  draftAlreadySent,
  draftKey,
  editText,
  EMPTY_DRAFT,
  keptRequestIds,
  migrateDraft,
  parseDraft,
  prepareSend,
  readDraft,
  requestIdKey,
  retryScope,
  writeDraft,
  type DraftStorage,
} from './drafts';

function memory(): DraftStorage & { data: Map<string, string> } {
  const data = new Map<string, string>();
  return {
    data,
    getItem: (k) => data.get(k) ?? null,
    setItem: (k, v) => void data.set(k, v),
    removeItem: (k) => void data.delete(k),
  };
}

const broken: DraftStorage = {
  getItem: () => {
    throw new Error('denied');
  },
  setItem: () => {
    throw new Error('quota');
  },
  removeItem: () => {
    throw new Error('denied');
  },
};

let n = 0;
const gen = () => `id-${++n}`;

describe('draft storage', () => {
  it('keys drafts per user and conversation', () => {
    expect(draftKey('u1', 'c1')).toBe('partroute.assistant.draft.v1.u1.c1');
    expect(draftKey('u1', null, 'p1')).toBe('partroute.assistant.draft.v1.u1.new.p1');
  });
  it('round-trips and removes empty drafts', () => {
    const s = memory();
    const k = draftKey('u1', 'c1');
    const d = { ...EMPTY_DRAFT, text: 'hole to 10 mm', operation: 'auto' as const };
    expect(writeDraft(s, k, d)).toBe(true);
    expect(readDraft(s, k)).toEqual(d);
    expect(writeDraft(s, k, EMPTY_DRAFT)).toBe(true);
    expect(s.data.has(k)).toBe(false);
  });
  it('survives unavailable or throwing storage', () => {
    expect(readDraft(null, 'k')).toEqual(EMPTY_DRAFT);
    expect(writeDraft(null, 'k', { ...EMPTY_DRAFT, text: 'x' })).toBe(false);
    expect(readDraft(broken, 'k')).toEqual(EMPTY_DRAFT);
    expect(writeDraft(broken, 'k', { ...EMPTY_DRAFT, text: 'x' })).toBe(false);
  });
  it('rejects malformed stored drafts', () => {
    expect(parseDraft('{bad')).toEqual(EMPTY_DRAFT);
    expect(parseDraft('null')).toEqual(EMPTY_DRAFT);
    expect(parseDraft(JSON.stringify({ text: 'a', operation: 'delete_everything', requestId: 'x' }))).toEqual({ ...EMPTY_DRAFT, text: 'a' });
  });
});

describe('client_request_id reuse', () => {
  it('reuses the id when the same draft is re-sent after a failure', () => {
    const first = prepareSend({ ...EMPTY_DRAFT, text: 'move the hole 2 mm' }, gen);
    expect(first.requestId).toBeTruthy();
    const s = memory();
    writeDraft(s, 'k', first);
    // network failure: the draft (with its id) stays; a reload restores it
    const again = prepareSend(readDraft(s, 'k'), gen);
    expect(again.requestId).toBe(first.requestId);
  });
  it('uses a new id once the text changes, and the old one again if it is restored', () => {
    const sent = prepareSend({ ...EMPTY_DRAFT, text: 'a' }, gen);
    const edited = editText(sent, 'ab');
    expect(edited.requestId).toBeNull();
    expect(prepareSend(edited, gen).requestId).not.toBe(sent.requestId);
    expect(editText(sent, 'a')).toBe(sent);
  });
  it('clears the text and retired overrides after the server confirmed', () => {
    const d = afterSent(prepareSend({ ...EMPTY_DRAFT, text: 'a', operation: 'ask', modelConfigId: 'm1' }, gen));
    expect(d).toEqual(EMPTY_DRAFT);
  });
  it('recognises a draft whose request already reached the server', () => {
    const d = prepareSend({ ...EMPTY_DRAFT, text: 'a' }, gen);
    expect(draftAlreadySent(d, [{ request: { client_request_id: d.requestId! } as never }])).toBe(true);
    expect(draftAlreadySent(d, [])).toBe(false);
    expect(draftAlreadySent(EMPTY_DRAFT, [{ request: { client_request_id: 'x' } as never }])).toBe(false);
  });
});

describe('start page draft migration', () => {
  it('moves the start page draft (text, operation, request id) into the new conversation', () => {
    const s = memory();
    const from = draftKey('u1', null, 'p1');
    const to = draftKey('u1', 'c-new');
    const typed = prepareSend({ ...EMPTY_DRAFT, text: 'add a 6 mm hole', operation: 'modeling' }, gen);
    writeDraft(s, from, typed);
    const moved = migrateDraft(readDraft(s, from), readDraft(s, to));
    expect(moved).toEqual(typed);
    expect(moved?.requestId).toBe(typed.requestId);
  });
  it('never overwrites a draft the new conversation already has, and skips empty drafts', () => {
    const existing = { ...EMPTY_DRAFT, text: 'already here' };
    expect(migrateDraft({ ...EMPTY_DRAFT, text: 'typed' }, existing)).toBeNull();
    expect(migrateDraft(EMPTY_DRAFT, EMPTY_DRAFT)).toBeNull();
    // Retired operation choices do not create a draft.
    expect(migrateDraft({ ...EMPTY_DRAFT, operation: 'drawing_intake' }, EMPTY_DRAFT)).toBeNull();
  });
});

describe('kept retry request ids', () => {
  const uuid = (i: number) => `00000000-0000-4000-8000-${String(i).padStart(12, '0')}`;
  let k = 0;
  const g = () => uuid(++k);

  it('reuses one id per retried operation until the server confirmed it', () => {
    const ids = keptRequestIds(memory(), 'u1', g);
    const a = ids.take(retryScope('op1'));
    // network failure: the response was lost, the user clicks again
    expect(ids.take(retryScope('op1'))).toBe(a);
    // retrying another operation is a different request
    expect(ids.take(retryScope('op2'))).not.toBe(a);
    // confirmed: the next click is a new request
    ids.forget(retryScope('op1'));
    expect(ids.peek(retryScope('op1'))).toBeNull();
    expect(ids.take(retryScope('op1'))).not.toBe(a);
  });
  it('survives a remount or reload through storage', () => {
    const s = memory();
    const first = keptRequestIds(s, 'u1', g).take(retryScope('op1'));
    expect(s.data.get(requestIdKey('u1', retryScope('op1')))).toBe(first);
    const again = keptRequestIds(s, 'u1', g);
    expect(again.take(retryScope('op1'))).toBe(first);
    again.forget(retryScope('op1'));
    expect(s.data.has(requestIdKey('u1', retryScope('op1')))).toBe(false);
  });
  it('keeps ids in memory without (or with throwing) storage and ignores malformed stored values', () => {
    for (const st of [null, broken]) {
      const ids = keptRequestIds(st, 'u1', g);
      const a = ids.take(retryScope('op2'));
      expect(ids.take(retryScope('op2'))).toBe(a);
      ids.forget(retryScope('op2'));
      expect(ids.peek(retryScope('op2'))).toBeNull();
    }
    const s = memory();
    s.data.set(requestIdKey('u1', retryScope('op3')), '<script>');
    expect(keptRequestIds(s, 'u1', g).peek(retryScope('op3'))).toBeNull();
  });
  it('keeps ids per user', () => {
    const s = memory();
    const a = keptRequestIds(s, 'u1', g).take(retryScope('op4'));
    expect(keptRequestIds(s, 'u2', g).take(retryScope('op4'))).not.toBe(a);
  });
});


describe('current composer request contract', () => {
  it('migrates retired overrides out of old local storage', () => {
    const restored = parseDraft(JSON.stringify({ ...EMPTY_DRAFT, text: 'keep my text', operation: 'modeling', modelConfigId: 'm-old', policy: 'turbo', taskId: 't-old', autonomy: 'auto' }));
    expect(restored).toEqual({ ...EMPTY_DRAFT, text: 'keep my text' });
  });
  it('preserves an exact attempted body after a network failure, then rotates identity on context changes', () => {
    const first = prepareSend({ ...EMPTY_DRAFT, text: 'edit' }, gen, 'p1-r1-face3');
    const body = { client_request_id: first.requestId!, text: 'edit', operation: 'auto' as const, target_key: 'cad:r1:face3', reference_keys: ['file:v2'], auto_references: false };
    const restored = parseDraft(JSON.stringify({ ...first, attemptedBody: body }));
    expect(prepareSend(restored, gen, 'p1-r1-face3').attemptedBody).toEqual(body);
    expect(prepareSend(restored, gen, 'p1-r1-face3').requestId).toBe(first.requestId);
    const changed = prepareSend(restored, gen, 'p1-r2-face3');
    expect(changed.requestId).not.toBe(first.requestId);
    expect(changed.attemptedBody).toBeNull();
  });
  it('does not replay a persisted body containing hidden overrides or a different request id', () => {
    const first = prepareSend({ ...EMPTY_DRAFT, text: 'edit' }, gen, 'fingerprint');
    const body = { client_request_id: first.requestId, text: 'edit', operation: 'auto', target_key: null, reference_keys: [], auto_references: false };
    for (const change of [{ task_id: 'old-task' }, { execution_policy: 'turbo' }, { model_config_id: 'old-model' }, { client_request_id: 'other' }, { reference_keys: ['ok', 3] }]) {
      const restored = parseDraft(JSON.stringify({ ...first, attemptedBody: { ...body, ...change } }));
      expect(restored.attemptedBody).toBeUndefined();
      expect(restored.requestId).toBeNull();
      expect(prepareSend(restored, gen, 'fingerprint').requestId).not.toBe(first.requestId);
    }
  });
});

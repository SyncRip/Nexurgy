// Server state of the active conversation, shared by the dock, the embedded
// panel and the full workspace (design §7.2–§7.3). Owned by AssistantProvider
// so it survives layout switches and keeps polling while the dock is closed
// (pages such as the model tab listen for operation results).
//
// - One GET on switch; while an operation is unfinished, GET ?after_seq= every
//   2 s (back-off on errors, paused while the browser tab is hidden), aborted on
//   switch / unmount.
// - Every change goes through commit(), which emits operation events for
//   status changes of the same conversation.

import { useCallback,useEffect,useRef,useState } from 'react';
import {
addAiReference,
answerAiDesignChoice,
answerAiOperation,
cancelAiOperation,
confirmAiOperation,
discardTaskDraft,
getAiConversation,
patchAiConversation,
patchTaskDraft,
removeAiReference,
retryAiOperation,
sendAiMessage,
setAiTarget,
undoAiStep,
type AiAddReferenceResult,
type AiComposer,
type AiConversationDetail,
type AiOperation,
type AiRefInput,
type AiSendBody,
type AiSendResult,
type AiTaskDraft,
type AiTaskDraftPatch,
type DesignChoiceBody,
} from '../../api/assistant';
import { isAbortError,isApiError } from '../../api/client';
import type { UUID } from '../../api/types';
import { applyComposer,applyDraft,applyOperation,applySendResult,hasActiveOperations,maxSeq,mergeDetail,operationEvents,pollDelay,type OperationEvent } from './model';

export type ConversationPatch = Parameters<typeof patchAiConversation>[1];

export interface ConversationStore {
  detail: AiConversationDetail | null;
  /** True while the first GET of the selected conversation is in flight. */
  loading: boolean;
  error: unknown;
  reload(): void;
  /** Incremental GET (?after_seq=) merged into the shown conversation; errors are left to the next poll / reload. */
  refresh(conversationId: UUID, afterSeq?: number): Promise<void>;
  /** Show a conversation the caller already has (just created / forked) without waiting for a GET. */
  prime(detail: AiConversationDetail): void;
  send(conversationId: UUID, body: AiSendBody): Promise<AiSendResult>;
  addRef(input: AiRefInput, asTarget?: boolean): Promise<AiAddReferenceResult>;
  removeRef(key: string): Promise<void>;
  setTarget(key: string | null): Promise<void>;
  patch(body: ConversationPatch): Promise<AiConversationDetail>;
  answer(operationId: UUID, answers: string[]): Promise<AiOperation>;
  cancel(operationId: UUID): Promise<AiOperation>;
  retry(operationId: UUID, clientRequestId: UUID): Promise<AiSendResult>;
  /** Main agent: answer a confirmation (the next turn is queued on accept). */
  confirm(operationId: UUID, accept: boolean): Promise<AiOperation>;
  answerDesign(operationId: UUID, body: DesignChoiceBody): Promise<AiOperation>;
  /** Main agent: undo one step of a turn. */
  undoStep(operationId: UUID, index: number): Promise<AiOperation>;
  /** Main agent: edit / discard a task draft of the shown conversation. */
  saveDraft(draftId: UUID, body: AiTaskDraftPatch): Promise<AiTaskDraft>;
  discardDraft(draftId: UUID): Promise<AiTaskDraft>;
  /** A composer returned by a call made outside the store (addContext from a page). */
  applyComposer(conversationId: UUID, composer: AiComposer): void;
}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) return resolve();
    const timer = setTimeout(done, ms);
    function done() {
      clearTimeout(timer);
      signal.removeEventListener('abort', done);
      resolve();
    }
    signal.addEventListener('abort', done);
  });
}

/** Resolve when the browser tab is visible (polling pauses in background tabs). */
function whenVisible(signal: AbortSignal): Promise<void> {
  if (typeof document === 'undefined' || !document.hidden) return Promise.resolve();
  return new Promise((resolve) => {
    function done() {
      if (document.hidden && !signal.aborted) return;
      document.removeEventListener('visibilitychange', done);
      signal.removeEventListener('abort', done);
      resolve();
    }
    document.addEventListener('visibilitychange', done);
    signal.addEventListener('abort', done);
  });
}

export function useConversationStore(conversationId: UUID | null, onEvents: (events: OperationEvent[]) => void): ConversationStore {
  const [detail, setDetail] = useState<AiConversationDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [nonce, setNonce] = useState(0);
  const detailRef = useRef<AiConversationDetail | null>(null);
  /** Bumped on every local composer change; a GET that started earlier keeps the shown composer. */
  const composerStamp = useRef(0);
  const onEventsRef = useRef(onEvents);
  onEventsRef.current = onEvents;

  const commit = useCallback((next: AiConversationDetail | null) => {
    const prev = detailRef.current;
    detailRef.current = next;
    setDetail(next);
    if (prev && next && prev.conversation.id === next.conversation.id) {
      const events = operationEvents(prev.operations, next.operations);
      if (events.length) onEventsRef.current(events.map((event) => ({ ...event, projectId: next.conversation.project.id })));
    }
  }, []);

  /** Apply to the shown conversation only (a late response for another one is dropped). */
  const update = useCallback(
    (id: UUID, fn: (d: AiConversationDetail) => AiConversationDetail) => {
      const cur = detailRef.current;
      if (cur && cur.conversation.id === id) commit(fn(cur));
    },
    [commit],
  );

  // Load on switch (and on reload()).
  useEffect(() => {
    if (!conversationId) {
      commit(null);
      setError(null);
      setLoading(false);
      return;
    }
    const ctrl = new AbortController();
    const shown = detailRef.current?.conversation.id === conversationId;
    if (!shown) commit(null);
    setLoading(!shown);
    setError(null);
    const stamp = composerStamp.current;
    getAiConversation(conversationId, { signal: ctrl.signal })
      .then((d) => {
        if (ctrl.signal.aborted) return;
        commit(mergeDetail(detailRef.current, d, { keepComposer: composerStamp.current !== stamp }));
        setLoading(false);
      })
      .catch((e: unknown) => {
        if (ctrl.signal.aborted || isAbortError(e)) return;
        setError(e);
        setLoading(false);
      });
    return () => ctrl.abort();
  }, [conversationId, nonce, commit]);

  // Poll while an operation is unfinished.
  const active = hasActiveOperations(detail);
  const shownId = detail?.conversation.id ?? null;
  useEffect(() => {
    if (!active || !shownId) return;
    const ctrl = new AbortController();
    let failures = 0;
    void (async () => {
      while (!ctrl.signal.aborted) {
        await sleep(pollDelay(failures), ctrl.signal);
        await whenVisible(ctrl.signal);
        if (ctrl.signal.aborted) return;
        const cur = detailRef.current;
        if (!cur || cur.conversation.id !== shownId) return;
        const stamp = composerStamp.current;
        try {
          const res = await getAiConversation(shownId, { afterSeq: maxSeq(cur.messages), signal: ctrl.signal });
          if (ctrl.signal.aborted) return;
          const merged = mergeDetail(detailRef.current, res, { incremental: true, keepComposer: composerStamp.current !== stamp });
          commit(merged);
          failures = 0;
          if (!hasActiveOperations(merged)) return;
        } catch (e) {
          if (ctrl.signal.aborted || isAbortError(e)) return;
          if (isApiError(e) && (e.status === 404 || e.status === 403)) {
            setError(e);
            return;
          }
          failures += 1;
        }
      }
    })();
    return () => ctrl.abort();
  }, [active, shownId, commit]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);

  const refresh = useCallback(
    async (id: UUID, afterSeq?: number) => {
      const cur = detailRef.current;
      if (!cur || cur.conversation.id !== id) return;
      const stamp = composerStamp.current;
      try {
        const res = await getAiConversation(id, { afterSeq: afterSeq ?? maxSeq(cur.messages) });
        const now = detailRef.current;
        if (!now || now.conversation.id !== id) return;
        commit(mergeDetail(now, res, { incremental: true, keepComposer: composerStamp.current !== stamp }));
      } catch {
        /* the next poll or reload catches up */
      }
    },
    [commit],
  );

  const prime = useCallback(
    (d: AiConversationDetail) => {
      commit(d);
      setError(null);
      setLoading(false);
    },
    [commit],
  );

  const currentId = (): UUID => {
    const id = detailRef.current?.conversation.id;
    if (!id) throw new Error('no conversation selected');
    return id;
  };

  const setComposer = useCallback(
    (id: UUID, composer: AiComposer) => {
      composerStamp.current += 1;
      update(id, (d) => applyComposer(d, composer));
    },
    [update],
  );

  const send = useCallback(
    async (id: UUID, body: AiSendBody) => {
      const res = await sendAiMessage(id, body);
      update(id, (d) => applySendResult(d, res));
      // The result carries one assistant message (answer or operation); fetch everything after the request.
      void refresh(id, Math.max(0, res.message.seq - 1));
      return res;
    },
    [update, refresh],
  );

  const addRef = useCallback(
    async (input: AiRefInput, asTarget?: boolean) => {
      const id = currentId();
      const res = await addAiReference(id, { ref: input, as_target: asTarget });
      setComposer(id, res.composer);
      return res;
    },
    [setComposer],
  );

  const removeRef = useCallback(
    async (key: string) => {
      const id = currentId();
      setComposer(id, await removeAiReference(id, key));
    },
    [setComposer],
  );

  const setTarget = useCallback(
    async (key: string | null) => {
      const id = currentId();
      setComposer(id, await setAiTarget(id, key));
    },
    [setComposer],
  );

  const patch = useCallback(
    async (body: ConversationPatch) => {
      const id = currentId();
      const res = await patchAiConversation(id, body);
      update(id, (d) => mergeDetail(d, res, { incremental: true, keepComposer: true }));
      return res;
    },
    [update],
  );

  const answer = useCallback(
    async (operationId: UUID, answers: string[]) => {
      const op = await answerAiOperation(operationId, answers);
      update(op.conversation_id, (d) => applyOperation(d, op));
      void refresh(op.conversation_id);
      return op;
    },
    [update, refresh],
  );

  const cancel = useCallback(
    async (operationId: UUID) => {
      const op = await cancelAiOperation(operationId);
      update(op.conversation_id, (d) => applyOperation(d, op));
      void refresh(op.conversation_id);
      return op;
    },
    [update, refresh],
  );

  const retry = useCallback(
    async (operationId: UUID, clientRequestId: UUID) => {
      const id = currentId();
      const res = await retryAiOperation(operationId, clientRequestId);
      update(id, (d) => applySendResult(d, res));
      void refresh(id, Math.max(0, res.message.seq - 1));
      return res;
    },
    [update, refresh],
  );

  /** A call that returns an operation of the shown conversation: show it, then fetch what it added (next turn, notices). */
  const opCall = useCallback(
    async (call: Promise<AiOperation>) => {
      const op = await call;
      update(op.conversation_id, (d) => applyOperation(d, op));
      void refresh(op.conversation_id);
      return op;
    },
    [update, refresh],
  );
  const confirm = useCallback((operationId: UUID, accept: boolean) => opCall(confirmAiOperation(operationId, accept)), [opCall]);
  const answerDesign = useCallback((operationId: UUID, body: DesignChoiceBody) => opCall(answerAiDesignChoice(operationId, body)), [opCall]);
  const undoStep = useCallback((operationId: UUID, index: number) => opCall(undoAiStep(operationId, index)), [opCall]);

  const draftCall = useCallback(
    async (call: Promise<AiTaskDraft>) => {
      const d = await call;
      update(d.conversation_id, (x) => applyDraft(x, d));
      return d;
    },
    [update],
  );
  const saveDraft = useCallback((draftId: UUID, body: AiTaskDraftPatch) => draftCall(patchTaskDraft(draftId, body)), [draftCall]);
  const discardDraft = useCallback((draftId: UUID) => draftCall(discardTaskDraft(draftId)), [draftCall]);

  return { detail, loading, error, reload, refresh, prime, send, addRef, removeRef, setTarget, patch, answer, cancel, retry, confirm, answerDesign, undoStep, saveDraft, discardDraft,
    applyComposer: setComposer };
}

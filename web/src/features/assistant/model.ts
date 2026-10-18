// Pure helpers of the assistant's conversation state (no React, unit-tested in
// model.test.ts): merging server responses, the polling stop condition,
// operation events, request snapshots and composer compatibility types.
// Server data is the source of truth; these only combine what it returned.

import {
AI_TERMINAL,
type AiComposer,
type AiConversationDetail,
type AiMessage,
type AiOperation,
type AiRef,
type AiRefInput,
type AiRefStatus,
type AiSendResult,
type AiTaskDraft,
type AiWork,
} from '../../api/assistant';

/** Poll interval while the active conversation has unfinished operations (design §7.3). */
export const POLL_MS = 2000;
/** Upper bound of the back-off after failed polls. */
export const POLL_MAX_MS = 16000;

export interface OperationEvent {
  operation: AiOperation;
  type: 'succeeded' | 'failed' | 'status' | 'result';
  projectId?: string;
}

export function isTerminal(op: Pick<AiOperation, 'status'>): boolean {
  return AI_TERMINAL.has(op.status);
}

/** Keep following terminal children until their durable completion wake is delivered or closed. */
export function hasActiveOperations(detail: Pick<AiConversationDetail, 'operations'> | null | undefined): boolean {
  return !!detail && detail.operations.some((op) => !isTerminal(op) || op.completion_events?.some((event) => event.wake_state === 'pending' || event.wake_state === 'assigned'));
}

/** Delay before the next poll: 2 s, doubled after each consecutive failure, capped. */
export function pollDelay(failures: number): number {
  if (failures <= 0) return POLL_MS;
  return Math.min(POLL_MAX_MS, POLL_MS * 2 ** Math.min(failures, 6));
}

export function maxSeq(messages: readonly Pick<AiMessage, 'seq'>[]): number {
  return messages.reduce((m, x) => (x.seq > m ? x.seq : m), 0);
}

/** Union by id (incoming wins), ordered by seq. */
export function mergeMessages(prev: readonly AiMessage[], incoming: readonly AiMessage[]): AiMessage[] {
  if (!incoming.length) return prev as AiMessage[];
  const byId = new Map(prev.map((m) => [m.id, m]));
  for (const m of incoming) byId.set(m.id, m);
  return [...byId.values()].sort((a, b) => a.seq - b.seq);
}

function newer(a: AiOperation, b: AiOperation): AiOperation {
  // Domain generation and immutable completion order take precedence over timestamps:
  // a slow poll must not erase a continuation or an apply with unchanged status.
  const generationA = a.generation ?? 1;
  const generationB = b.generation ?? 1;
  if (generationA !== generationB) return generationB > generationA ? b : a;
  const sequence = (op: AiOperation) => Math.max(0, ...(op.completion_events ?? []).map((event) => event.event_seq));
  const sequenceA = sequence(a), sequenceB = sequence(b);
  if (sequenceA !== sequenceB) return sequenceB > sequenceA ? b : a;
  return Date.parse(b.updated_at) >= Date.parse(a.updated_at) || Number.isNaN(Date.parse(a.updated_at)) ? b : a;
}

/** Upsert by id keeping the newest copy; order by creation. */
export function upsertOperations(prev: readonly AiOperation[], incoming: readonly AiOperation[]): AiOperation[] {
  if (!incoming.length) return prev as AiOperation[];
  const byId = new Map(prev.map((o) => [o.id, o]));
  for (const o of incoming) {
    const p = byId.get(o.id);
    byId.set(o.id, p ? newer(p, o) : o);
  }
  return [...byId.values()].sort((a, b) => Date.parse(a.created_at) - Date.parse(b.created_at));
}

/**
 * Combine a GET / PATCH response with what is shown. Another conversation's
 * response replaces everything. Messages merge by id; operations keep the
 * newest copy; the conversation keeps the higher optimistic-lock version.
 * `keepComposer` = the composer was changed locally while the request was in
 * flight, so the response's composer is older than the shown one.
 */
export function mergeDetail(prev: AiConversationDetail | null, next: AiConversationDetail, opts: { incremental?: boolean; keepComposer?: boolean } = {}): AiConversationDetail {
  if (!prev || prev.conversation.id !== next.conversation.id) return next;
  const prevOps = new Map(prev.operations.map((o) => [o.id, o]));
  return {
    conversation: next.conversation.version >= prev.conversation.version ? next.conversation : prev.conversation,
    messages: opts.incremental ? mergeMessages(prev.messages, next.messages) : next.messages,
    operations: opts.incremental
      ? upsertOperations(prev.operations, next.operations)
      : next.operations.map((o) => {
          const p = prevOps.get(o.id);
          return p ? newer(p, o) : o;
        }),
    composer: opts.keepComposer ? prev.composer : next.composer,
    work: mergeWork(prev.work, next.work),
  };
}

/**
 * The main agent's work objects: the response's lists, but a draft keeps the higher version (a poll that started
 * before a local save must not roll the saved draft back). A response without `work` keeps what is shown.
 */
export function mergeWork(prev: AiWork | undefined, next: AiWork | undefined): AiWork | undefined {
  if (!next || !prev) return next ?? prev;
  const shown = new Map(prev.drafts.map((d) => [d.id, d]));
  return {
    ...next,
    drafts: next.drafts.map((d) => {
      const p = shown.get(d.id);
      return p && p.version > d.version ? p : d;
    }),
  };
}

/** A draft returned by a save / discard (upserted; an older copy never replaces a newer one). */
export function applyDraft(prev: AiConversationDetail, draft: AiTaskDraft): AiConversationDetail {
  if (draft.conversation_id !== prev.conversation.id) return prev;
  const work = prev.work ?? { drafts: [], tasks: [], parts: [] };
  const drafts = work.drafts.some((d) => d.id === draft.id) ? work.drafts.map((d) => (d.id === draft.id ? draft : d)) : [...work.drafts, draft];
  return { ...prev, work: mergeWork(work, { ...work, drafts }) };
}

/** Add what a send / retry returned (user message, operation, assistant message). */
export function applySendResult(prev: AiConversationDetail, res: AiSendResult): AiConversationDetail {
  const msgs = [res.message, res.assistant_message].filter((m): m is AiMessage => !!m);
  return {
    ...prev,
    messages: mergeMessages(prev.messages, msgs),
    operations: res.operation ? upsertOperations(prev.operations, [res.operation]) : prev.operations,
  };
}

export function applyOperation(prev: AiConversationDetail, op: AiOperation): AiConversationDetail {
  if (op.conversation_id !== prev.conversation.id) return prev;
  return { ...prev, operations: upsertOperations(prev.operations, [op]) };
}

export function applyComposer(prev: AiConversationDetail, composer: AiComposer): AiConversationDetail {
  return { ...prev, composer };
}

/**
 * Which operation retried which (old id → newer id): the server's `retried_by`,
 * or a shown operation whose `retry_of` points back (before the old copy was refetched).
 */
export function retriedByMap(operations: readonly Pick<AiOperation, 'id' | 'retry_of' | 'retried_by'>[]): Map<string, string> {
  const out = new Map<string, string>();
  for (const o of operations) if (o.retried_by) out.set(o.id, o.retried_by);
  for (const o of operations) if (o.retry_of && !out.has(o.retry_of)) out.set(o.retry_of, o.id);
  return out;
}

/** Did the server record a request with this client_request_id (seen after a lost response or a reload)? */
export function requestRecorded(id: string | null | undefined, d: Pick<AiConversationDetail, 'messages' | 'operations'> | null | undefined): boolean {
  if (!id || !d) return false;
  return d.messages.some((m) => m.role === 'user' && m.payload?.client_request_id === id) || d.operations.some((o) => o.request?.client_request_id === id);
}

/**
 * Events for operations whose status changed between two views of the same
 * conversation. The first load (prev null) emits nothing, so history never
 * looks like fresh results; a new operation (send / retry) emits once.
 */
export function resultIdentity(op: AiOperation): string {
  const result = op.result as unknown as { cad_result?: { event_id: string }; application?: unknown; steps?: unknown } | null;
  // Delivery metadata is mutable and does not identify a new domain result.
  const completions = [...(op.completion_events ?? [])].sort((a, b) => a.event_seq - b.event_seq)
    .map(({ event_id, generation, event_seq, phase, outcome }) => [event_id, generation, event_seq, phase, outcome]);
  return JSON.stringify([op.generation ?? 1, completions, result?.cad_result?.event_id ?? null, result?.application ?? null, result?.steps ?? null]);
}

export function operationEvents(prev: readonly AiOperation[] | null, next: readonly AiOperation[]): OperationEvent[] {
  if (!prev) return [];
  const before = new Map(prev.map((o) => [o.id, o]));
  const out: OperationEvent[] = [];
  for (const op of next) {
    const was = before.get(op.id);
    if (was?.status === op.status) {
      if (resultIdentity(was) !== resultIdentity(op)) out.push({ operation: op, type: 'result' });
      continue;
    }
    out.push({ operation: op, type: op.status === 'succeeded' ? 'succeeded' : op.status === 'failed' ? 'failed' : 'status' });
  }
  return out;
}

// ─── composer ─────────────────────────────────────────────────────────────

/** Legacy draft/prefill vocabulary. Fresh composer requests always send auto. */
export type ComposerOperation = 'auto' | 'task_draft' | 'modeling' | 'drawing_generate' | 'drawing_reconstruct'
  | 'drawing_intake' | 'assembly_create' | 'assembly_modify' | 'ask';

/** Explicit keys for a send: exactly the target and references the user sees. */
export function composerSendKeys(composer: Pick<AiComposer, 'target_key' | 'references'>): { target_key: string | null; reference_keys: string[] } {
  const target = composer.target_key && composer.references.some((r) => r.key === composer.target_key) ? composer.target_key : null;
  return { target_key: target, reference_keys: composer.references.filter((r) => r.key !== target).map((r) => r.key) };
}

/** Why the send button is disabled (null = can send). */
export type SendBlock = 'archived' | 'empty' | 'too_long' | 'sending' | 'target_unavailable' | 'no_project';

/**
 * S2: a text longer than the chat limit is not refused: the server saves it as a project material and the request
 * references it. Only a text over the material limit (both from GET /system/capabilities) cannot be sent.
 */
export function sendBlock(s: { archived: boolean; text: string; sending: boolean; targetStatus?: AiRefStatus | null; hasProject: boolean; maxChars?: number }): SendBlock | null {
  if (s.archived) return 'archived';
  if (!s.hasProject) return 'no_project';
  if (s.sending) return 'sending';
  if (!s.text.trim()) return 'empty';
  if (s.maxChars !== undefined && s.text.length > s.maxChars) return 'too_long';
  if (s.targetStatus?.state === 'unavailable') return 'target_unavailable';
  return null;
}

/** S2: will this text be saved as a project material when sent (longer than the chat limit)? */
export function becomesMaterial(text: string, chatLimit: number | undefined): boolean {
  return chatLimit !== undefined && text.trim().length > chatLimit;
}

/** Longest pinned constraint the API accepts (characters). */
export const MAX_CONSTRAINT_TEXT = 500;

/**
 * A message pinned as a constraint: at most `max` characters, shortened with an
 * ellipsis (never splitting a surrogate pair) and reported so the user is told.
 */
export function clipConstraint(text: string, max = MAX_CONSTRAINT_TEXT): { text: string; clipped: boolean } {
  const v = text.trim();
  if (v.length <= max) return { text: v, clipped: false };
  let end = max - 1;
  const last = v.charCodeAt(end - 1);
  if (last >= 0xd800 && last <= 0xdbff) end -= 1;
  return { text: `${v.slice(0, end).trimEnd()}…`, clipped: true };
}

/** The user request a choice / answer message replies to: the closest earlier user message. */
export function previousUserMessage(messages: readonly AiMessage[], seq: number): AiMessage | null {
  let found: AiMessage | null = null;
  for (const m of messages) {
    if (m.seq >= seq) break;
    if (m.role === 'user') found = m;
  }
  return found;
}

/** Every reference used in earlier requests, newest first, once per key. */
export function historyRefs(messages: readonly AiMessage[]): { ref: AiRef; status: AiRefStatus | undefined; seq: number; asTarget: boolean }[] {
  const seen = new Set<string>();
  const out: { ref: AiRef; status: AiRefStatus | undefined; seq: number; asTarget: boolean }[] = [];
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (m.role !== 'user') continue;
    const refs = [...(m.target ? [{ ref: m.target, asTarget: true }] : []), ...m.references.map((r) => ({ ref: r, asTarget: false }))];
    for (const { ref, asTarget } of refs) {
      if (seen.has(ref.key)) continue;
      seen.add(ref.key);
      out.push({ ref, status: m.ref_status?.[ref.key], seq: m.seq, asTarget });
    }
  }
  return out;
}

/** Does the composer already hold the object a page suggests (same kind, object and scope)? */
export function sameObject(input: AiRefInput, ref: AiRef): boolean {
  if (input.kind !== ref.kind || input.object_id !== ref.object_id) return false;
  switch (input.kind) {
    case 'cad_selection':
      return !!ref.selection && !!input.selection && ref.selection.entity === input.selection.entity && String(ref.selection.id) === String(input.selection.id);
    case 'quote_field':
      return !!ref.field && !!input.field && ref.field.tier_qty === input.field.tier_qty && ref.field.key === input.field.key;
    case 'run_output':
      return !!ref.output && !!input.output && ref.output.node_id === input.output.node_id && ref.output.output_key === input.output.output_key;
    case 'text_excerpt':
      return !!ref.excerpt && !!input.excerpt && ref.excerpt.text === input.excerpt.text;
    default:
      return true;
  }
}

/** RFC 4122 v4 id for client_request_id (crypto when available). */
export function newRequestId(): string {
  const c = (globalThis as { crypto?: Crypto }).crypto;
  if (c && typeof c.randomUUID === 'function') return c.randomUUID();
  const b = new Uint8Array(16);
  if (c && typeof c.getRandomValues === 'function') c.getRandomValues(b);
  else for (let i = 0; i < 16; i++) b[i] = Math.floor(Math.random() * 256);
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const h = [...b].map((x) => x.toString(16).padStart(2, '0')).join('');
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

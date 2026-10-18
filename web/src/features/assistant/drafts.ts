// Unsent composer drafts (design §7.3): per user + conversation in
// localStorage under `partroute.assistant.draft.v1.<userId>.<conversationId>`.
// A draft keeps the client_request_id it was sent with until the send is
// confirmed, so re-sending the same text after a network failure reuses the id
// and the server returns the existing request instead of starting a second
// one. Editing the text drops the id. Every storage access is guarded: without
// storage the draft lives in memory only (the panel says so).
//
// Actions that retry a recorded request keep their
// client_request_id the same way, under `…<userId>.req.<scope>`, until the
// server confirmed it, so a click after a lost response never duplicates it.

import type { AiOperation,AiSendBody } from '../../api/assistant';
import type { UUID } from '../../api/types';
import type { ComposerOperation } from './model';
import { parsePending,type PendingReference } from './pageContext';

export interface DraftStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

export interface ComposerDraft {
  text: string;
  /** client_request_id of the last send attempt of this exact text. */
  requestId: string | null;
  /** The text `requestId` was generated for. */
  requestText: string | null;
  operation: ComposerOperation;
  modelConfigId: UUID | null;
  /** Legacy input only. Dropped when a draft is restored or submitted. */
  policy?: 'standard' | 'turbo' | null;
  pending?: PendingReference[];
  contextScope?: string | null;
  suppressed?: string[];
  requestFingerprint?: string | null;
  attemptedBody?: AiSendBody | null;
}

export const EMPTY_DRAFT: ComposerDraft = { text: '', requestId: null, requestText: null, operation: 'auto', modelConfigId: null };

export const DRAFT_PREFIX = 'partroute.assistant.draft.v1.';

/** Drafts of a conversation that does not exist yet are kept per project. */
export function draftKey(userId: string, conversationId: string | null, projectId?: string | null): string {
  return `${DRAFT_PREFIX}${userId}.${conversationId ?? `new.${projectId ?? 'none'}`}`;
}

/** localStorage when it can be used (private mode / disabled storage → null). */
export function safeStorage(): DraftStorage | null {
  try {
    const s = globalThis.localStorage;
    if (!s) return null;
    const probe = `${DRAFT_PREFIX}probe`;
    s.setItem(probe, '1');
    s.removeItem(probe);
    return s;
  } catch {
    return null;
  }
}

function str(v: unknown): string | null {
  return typeof v === 'string' ? v : null;
}

function safeAttempt(value: unknown, requestId: string | null): AiSendBody | null {
  if (!value || typeof value !== 'object') return null;
  const body = value as AiSendBody;
  if (body.operation !== 'auto' || body.client_request_id !== requestId || typeof body.text !== 'string' || body.auto_references !== false || body.execution_policy || body.model_config_id || body.task_id || !Array.isArray(body.reference_keys) || !body.reference_keys.every((key) => typeof key === 'string') || (body.target_key !== null && typeof body.target_key !== 'string')) return null;
  return { client_request_id: body.client_request_id, text: body.text, operation: 'auto', target_key: body.target_key, reference_keys: body.reference_keys, auto_references: false,
    ...(typeof body.as_material === 'boolean' ? { as_material: body.as_material } : {}), ...(typeof body.revise === 'boolean' ? { revise: body.revise } : {}) };
}

/** Restore a draft; anything malformed becomes the empty draft. */
export function parseDraft(raw: string | null): ComposerDraft {
  if (!raw) return EMPTY_DRAFT;
  try {
    const v = JSON.parse(raw) as Record<string, unknown>;
    if (!v || typeof v !== 'object') return EMPTY_DRAFT;
    const text = str(v.text) ?? '';
    const requestId = str(v.requestId);
    const requestText = str(v.requestText);
    const attemptedBody = safeAttempt(v.attemptedBody, requestId);
    // A rejected body must not retain an identity that the server may already have
    // recorded with different overrides. The next send is a new request.
    const invalidAttempt = v.attemptedBody != null && (!attemptedBody || attemptedBody.text !== requestText?.trim());
    return {
      text,
      requestId: !invalidAttempt && requestId && requestText !== null ? requestId : null,
      requestText: !invalidAttempt && requestId && requestText !== null ? requestText : null,
      operation: 'auto',
      modelConfigId: null,
      ...(Array.isArray(v.pending) ? { pending: parsePending(v.pending) } : {}),
      ...(typeof v.contextScope === 'string' ? { contextScope: v.contextScope } : {}),
      ...(Array.isArray(v.suppressed) ? { suppressed: v.suppressed.filter((x): x is string => typeof x === 'string') } : {}),
      ...(!invalidAttempt && typeof v.requestFingerprint === 'string' ? { requestFingerprint: v.requestFingerprint } : {}),
      ...(!invalidAttempt && attemptedBody ? { attemptedBody } : {}),
    };
  } catch {
    return EMPTY_DRAFT;
  }
}

export function readDraft(storage: DraftStorage | null, key: string): ComposerDraft {
  if (!storage) return EMPTY_DRAFT;
  try {
    return parseDraft(storage.getItem(key));
  } catch {
    return EMPTY_DRAFT;
  }
}

export function isEmptyDraft(d: ComposerDraft): boolean {
  return !d.text && !d.requestId && !d.pending?.length && !d.suppressed?.length;
}

/** Persist (or remove an empty draft). False when storage is unavailable. */
export function writeDraft(storage: DraftStorage | null, key: string, d: ComposerDraft): boolean {
  if (!storage) return false;
  try {
    if (isEmptyDraft(d)) storage.removeItem(key);
    else storage.setItem(key, JSON.stringify(d));
    return true;
  } catch {
    return false;
  }
}

export function removeDraft(storage: DraftStorage | null, key: string): void {
  if (!storage) return;
  try {
    storage.removeItem(key);
  } catch {
    /* nothing to clean up */
  }
}

/** New text: a different text is a different request, so its old request id no longer applies. */
export function editText(d: ComposerDraft, text: string): ComposerDraft {
  if (text === d.text) return d;
  const keepId = d.requestId !== null && text === d.requestText;
  return { ...d, text, requestId: keepId ? d.requestId : null, requestText: keepId ? d.requestText : null, attemptedBody: keepId ? d.attemptedBody : null, requestFingerprint: keepId ? d.requestFingerprint : null };
}

/** Before sending: reuse the id of an earlier attempt of the same text, else generate one. */
export function prepareSend(d: ComposerDraft, gen: () => string, fingerprint?: string): ComposerDraft {
  if (d.requestId && d.requestText === d.text && (fingerprint === undefined || d.requestFingerprint === fingerprint)) return d;
  const { policy: _policy, ...rest } = d;
  return { ...rest, operation: 'auto', modelConfigId: null, requestId: gen(), requestText: d.text, ...(fingerprint !== undefined ? { requestFingerprint: fingerprint, attemptedBody: null } : {}) };
}

/** Clear the confirmed request and every retired request override. */
export function afterSent(d: ComposerDraft): ComposerDraft {
  const { policy: _policy, attemptedBody: _body, requestFingerprint: _fingerprint, ...rest } = d;
  return { ...rest, text: '', operation: 'auto', modelConfigId: null, requestId: null, requestText: null };
}

/** The draft's request already reached the server (seen after a reload): it must not be sent again. */
export function draftAlreadySent(d: ComposerDraft, operations: readonly Pick<AiOperation, 'request'>[]): boolean {
  return !!d.requestId && operations.some((op) => op.request?.client_request_id === d.requestId);
}

/**
 * The start page's draft (`new.<projectId>`) when a conversation was created
 * from it (e.g. by adding a page object): it moves to the new conversation
 * unless that conversation already has a draft. Null = nothing to move.
 */
export function migrateDraft(from: ComposerDraft, to: ComposerDraft): ComposerDraft | null {
  if (isEmptyDraft(from) || !isEmptyDraft(to)) return null;
  return from;
}

// ─── kept request ids of re-send actions ──────────────────────────────────

/** Retry of an operation. */
export function retryScope(operationId: string): string {
  return `retry.${operationId}`;
}

export function requestIdKey(userId: string, scope: string): string {
  return `${DRAFT_PREFIX}${userId}.req.${scope}`;
}

export interface KeptRequestIds {
  /** The id kept for this action, if an earlier attempt was not confirmed. */
  peek(scope: string): string | null;
  /** The id to send: the kept one, else a new one that is kept until forget(). */
  take(scope: string): string;
  /** The server confirmed the request (or it no longer applies). */
  forget(scope: string): void;
}

const ID_RE = /^[0-9a-zA-Z-]{8,64}$/;

/** Kept ids in memory and (guarded) storage, so they survive remounts and reloads. */
export function keptRequestIds(storage: DraftStorage | null, userId: string, gen: () => string): KeptRequestIds {
  const memory = new Map<string, string>();
  const peek = (scope: string): string | null => {
    const key = requestIdKey(userId, scope);
    const mem = memory.get(key);
    if (mem) return mem;
    if (!storage) return null;
    try {
      const v = storage.getItem(key);
      return v && ID_RE.test(v) ? v : null;
    } catch {
      return null;
    }
  };
  return {
    peek,
    take(scope) {
      const kept = peek(scope);
      if (kept) return kept;
      const id = gen();
      const key = requestIdKey(userId, scope);
      memory.set(key, id);
      if (storage) {
        try {
          storage.setItem(key, id);
        } catch {
          /* memory only */
        }
      }
      return id;
    },
    forget(scope) {
      const key = requestIdKey(userId, scope);
      memory.delete(key);
      removeDraft(storage, key);
    },
  };
}

import { isOrchestratorTurn, type AiConversationDetail, type AiMessage, type AiSendBody } from '../../api/assistant';
import { ApiError } from '../../api/client';
import { EMPTY_DRAFT, prepareSend, type ComposerDraft } from './drafts';

/** Duplicate labels are one action; answer identity keeps identical choices in different replies separate. */
export function answerFollowUps(message: Pick<AiMessage, 'payload'>): string[] {
  const values = message.payload.follow_ups;
  return Array.isArray(values)
    ? [...new Set(values.filter((value): value is string => typeof value === 'string').map((value) => value.trim()).filter(Boolean))]
    : [];
}

/** The caller supplies the existing user/conversation draft key, never the mutable composer draft itself. */
export function followUpDraftKey(conversationDraftKey: string, messageId: string, text: string): string {
  return `${conversationDraftKey}.followup.${encodeURIComponent(JSON.stringify([messageId, text]))}`;
}

/** Agent replies can omit snapshots; only their exact owning operation can supply the saved context. */
export function followUpContext(message: Pick<AiMessage, 'operation_id' | 'target' | 'references'>,
  detail: Pick<AiConversationDetail, 'conversation' | 'operations'>): Pick<AiMessage, 'target' | 'references'> | null {
  if (!message.operation_id) return { target: message.target, references: message.references };
  const owner = detail.operations.find((op) => op.id === message.operation_id
    && op.conversation_id === detail.conversation.id);
  return owner ? { target: owner.request.target, references: owner.request.references } : null;
}

export type FollowUpAttempt = ComposerDraft & { attemptedBody: AiSendBody };

/** A retry replays the persisted envelope even if later messages, context or settings have changed. */
export function prepareFollowUp(message: Pick<AiMessage, 'target' | 'references'> | null, text: string,
  saved: ComposerDraft, gen: () => string): FollowUpAttempt {
  if (saved.attemptedBody) return { ...saved, attemptedBody: saved.attemptedBody };
  if (!message) throw new ApiError(422, 'follow_up_context_missing', 'The saved option context is unavailable.');
  const target = message.target;
  if ((target && !target.key) || message.references.some((ref) => !ref.key)) {
    throw new ApiError(422, 'ref_not_in_composer', 'The saved option context has no reference key.');
  }
  const targetKey = target?.key ?? null;
  const referenceKeys = [...new Set(message.references.map((ref) => ref.key).filter((key) => key !== targetKey))];
  const draft = prepareSend({ ...EMPTY_DRAFT, text }, gen);
  return {
    ...draft,
    attemptedBody: {
      client_request_id: draft.requestId!, text, operation: 'auto', target_key: targetKey,
      reference_keys: referenceKeys, auto_references: false,
    },
  };
}

/** The server checks idempotency before its busy gate, so an uncertain attempt must remain retryable. */
export function followUpBlock(detail: Pick<AiConversationDetail, 'conversation' | 'operations'>,
  saved: Pick<ComposerDraft, 'attemptedBody'>): 'archived' | 'busy' | null {
  if (detail.conversation.archived) return 'archived';
  if (!saved.attemptedBody && detail.operations.some((op) => isOrchestratorTurn(op) && (op.status === 'pending' || op.status === 'running'))) return 'busy';
  return null;
}

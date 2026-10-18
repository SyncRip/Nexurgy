import { ArrowRight, Check, Clock3, Loader2, RotateCcw, TriangleAlert } from 'lucide-react';
import { useEffect, useId, useRef, useState } from 'react';
import type { AiConversationDetail, AiMessage } from '../../api/assistant';
import { ApiError } from '../../api/client';
import { useI18n } from '../../i18n/I18nProvider';
import { useAssistantSession } from './AssistantProvider';
import { AsError, RefChip } from './common';
import { answerFollowUps, followUpBlock, followUpContext, followUpDraftKey, prepareFollowUp } from './followUps';
import { conversationEntries } from './journey';
import { focusJourney } from './JourneyNavigator';
import { newRequestId, requestRecorded } from './model';

/** Explicit answer actions use their saved context and leave the person's composer draft untouched. */
export function FollowUpActions({ message, detail, historical = false }: {
  message: AiMessage; detail: AiConversationDetail; historical?: boolean;
}) {
  const { t } = useI18n();
  const { drafts, store, bumpList } = useAssistantSession();
  const [sendingKey, setSendingKey] = useState<string | null>(null);
  const [errors, setErrors] = useState<Record<string, unknown>>({});
  const [acknowledged, setAcknowledged] = useState<ReadonlySet<string>>(() => new Set());
  const sending = useRef(false);
  const mounted = useRef(true);
  const revealRequest = useRef<string | null>(null);
  const group = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const hintId = useId();
  const conversationId = detail.conversation.id;
  const draftKey = drafts.key(conversationId);
  const context = followUpContext(message, detail);
  const actions = answerFollowUps(message).map((text, index) => {
    const key = followUpDraftKey(draftKey, message.id, text);
    const draft = drafts.read(key);
    const submitted = requestRecorded(draft.requestId, detail) || !!draft.requestId && acknowledged.has(draft.requestId);
    return { text, key, number: index + 1, draft, submitted, block: followUpBlock(detail, draft), contextMissing: !context && !draft.attemptedBody };
  });

  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  // Only the explicit activation below requests navigation. Polls/restored attempts never send or jump on mount.
  useEffect(() => {
    if (!revealRequest.current || !group.current) return;
    const entry = conversationEntries(detail).find((item) => item.kind === 'journey'
      && item.journey.request.payload.client_request_id === revealRequest.current);
    if (entry?.kind === 'journey' && focusJourney(group.current.closest('.as-panel') ?? group.current, entry.journey)) {
      revealRequest.current = null;
    }
  }, [detail]);

  const submit = async (key: string, text: string) => {
    // Read again: a previous mount (dock/full handover) may already have attempted this action.
    const saved = drafts.read(key);
    if (sending.current || followUpBlock(detail, saved) || (!context && !saved.attemptedBody) || requestRecorded(saved.requestId, detail)
      || saved.requestId && acknowledged.has(saved.requestId)) return;
    sending.current = true;
    setSendingKey(key);
    setErrors((previous) => ({ ...previous, [key]: null }));
    try {
      const attempt = prepareFollowUp(context, text, saved, newRequestId);
      drafts.write(key, attempt); // Persist the exact body and identity before the first network operation.
      revealRequest.current = attempt.requestId;
      const result = await store.send(conversationId, attempt.attemptedBody);
      if (!requestRecorded(attempt.requestId, {
        messages: [result.message], operations: result.operation ? [result.operation] : [],
      })) throw new ApiError(0, 'follow_up_response_missing', 'The option submission has not been confirmed.');
      // Keep this one-shot identity after success so reloads continue to show Submitted.
      if (mounted.current) setAcknowledged((previous) => new Set([...previous, attempt.requestId!]));
      bumpList();
    } catch (error) {
      if (mounted.current) setErrors((previous) => ({ ...previous, [key]: error }));
      void store.refresh(conversationId);
    } finally {
      sending.current = false;
      if (mounted.current) setSendingKey(null);
    }
  };

  if (detail.conversation.archived || !actions.length) return null;
  const isUncertain = (action: typeof actions[number]) => !action.submitted && !!action.draft.attemptedBody;
  const renderGroup = (options: typeof actions, section: 'current' | 'retry' | 'history') => {
    if (!options.length) return null;
    const failed = options.find((action) => !action.submitted && errors[action.key]);
    const retry = options.some(isUncertain);
    const busy = options.some((action) => !action.submitted && action.block === 'busy');
    const unavailable = options.some((action) => !action.submitted && action.contextMissing);
    const status = options.some((action) => action.key === sendingKey) ? t('as.followups.sending') : [
      failed || retry ? t('as.followups.retryHint') : '',
      unavailable ? t('as.err.follow_up_context_missing') : '',
      busy ? t('as.followups.busyHint') : '',
      !failed && !retry && !busy && !unavailable && options.some((action) => action.submitted) ? t('as.followups.submittedHint') : '',
    ].filter(Boolean).join(' ');
    return <div className="as-followups-group" role="group" aria-labelledby={`${titleId}-${section}`} aria-describedby={`${hintId}-${section}`}>
      <div className="as-followups-heading">
        <h4 id={`${titleId}-${section}`}>{t(section === 'retry' ? 'as.followups.retry' : 'as.followups.title')}</h4>
        <span id={`${hintId}-${section}`} className="as-followups-hint">{t('as.followups.hint')}</span>
      </div>
      {context && (context.target || context.references.length > 0) && <div className="as-snap as-followups-context">
        <span className="as-snap-label">{t('as.followups.context')}</span>
        {context.target && <RefChip refItem={context.target} status={message.ref_status?.[context.target.key]} target compact />}
        {context.references.filter((ref) => ref.key !== context.target?.key).map((ref) => <RefChip key={ref.key} refItem={ref} status={message.ref_status?.[ref.key]} compact />)}
      </div>}
      <div className="as-followups-list">
        {options.map((action) => {
          const state = action.submitted ? 'submitted' : sendingKey === action.key ? 'sending'
            : action.draft.attemptedBody ? 'retry' : action.contextMissing ? 'unavailable' : action.block ? 'busy' : 'ready';
          const Icon = state === 'submitted' ? Check : state === 'sending' ? Loader2 : state === 'retry' ? RotateCcw
            : state === 'unavailable' ? TriangleAlert : state === 'busy' ? Clock3 : ArrowRight;
          return <button key={action.key} type="button" className="as-followup" data-followup-state={state}
            disabled={action.submitted || !!sendingKey || !!action.block || action.contextMissing} onClick={() => void submit(action.key, action.text)}>
            <span className="as-followup-number" aria-hidden>{action.number}</span>
            <span className="as-followup-copy">
              <span className="as-followup-text">{action.text}</span>
              <span className="as-followup-action"><Icon aria-hidden className={state === 'sending' ? 'spin-icon' : undefined} />{t(`as.followups.${state}`)}</span>
            </span>
          </button>;
        })}
      </div>
      <p className={`as-followups-status${status ? '' : ' sr-only'}`} role="status">{status}</p>
      <AsError error={failed ? errors[failed.key] : null} />
      {failed && <p className="as-ctx-note">{t('as.followups.correctHint')}</p>}
    </div>;
  };
  const previous = historical ? actions.filter((action) => !isUncertain(action)) : [];
  return <div className="as-followups" ref={group}>
    {renderGroup(historical ? actions.filter(isUncertain) : actions, historical ? 'retry' : 'current')}
    {previous.length > 0 && <details className="as-followups-history">
      <summary>{t('as.followups.history')}</summary>
      {renderGroup(previous, 'history')}
    </details>}
    {!drafts.persistent && <p className="as-ctx-warn">{t('as.followups.noStorage')}</p>}
  </div>;
}

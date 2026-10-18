import { Check, Loader2, MessageSquareText, RotateCcw, Sparkles } from 'lucide-react';
import { useEffect, useId, useRef, useState } from 'react';
import type { AiOperation, DesignChoiceAction, DesignChoiceQuestion, DesignChoiceReceipt, DesignQuestion } from '../../api/assistant';
import { ApiError, isApiError } from '../../api/client';
import { useI18n } from '../../i18n/I18nProvider';
import { useAssistantSession } from './AssistantProvider';
import { AsError } from './common';
import { collectDesignAnswers, DESIGN_TEXT_LIMIT, DESIGN_VALUE_LIMIT, designChoiceScope, designReceiptMatches, emptyDesignDraft, prepareDesignChoice, type DesignChoiceDraft, type DesignInput, type DesignInputError } from './designChoices';
import { designValueText } from './DesignPlanSummary';
import { newRequestId } from './model';

export function DesignChoiceCard({ op, question, disabled }: { op: AiOperation; question: DesignChoiceQuestion; disabled: boolean }) {
  const { t } = useI18n();
  const { store, designDrafts } = useAssistantSession();
  const scope = designChoiceScope(op.conversation_id, op.id, question);
  const [draft, setDraft] = useState(() => designDrafts.read(scope, question));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [acknowledged, setAcknowledged] = useState<DesignChoiceReceipt | null>(null);
  const sending = useRef(false);
  const mounted = useRef(true);
  const formId = useId();
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const receipt = op.result?.design_plan?.last_response ?? question.last_response;
  const seen = designReceiptMatches(draft.attemptedBody, receipt);
  useEffect(() => {
    if (!seen || !receipt) return;
    designDrafts.remove(scope);
    setDraft(emptyDesignDraft());
    setAcknowledged(receipt);
    setError(null);
  }, [seen, receipt, designDrafts, scope]);

  const active = op.status === 'waiting_input' && question.can_answer && !disabled && !acknowledged;
  const inputDisabled = !active || busy || !!draft.attemptedBody;
  const { answers, errors } = collectDesignAnswers(question.questions, draft.values);
  const hasErrors = Object.keys(errors).length > 0 || draft.freeText.length > DESIGN_TEXT_LIMIT;
  const hasEdits = answers.length > 0 || !!draft.freeText.trim();
  const save = (next: DesignChoiceDraft) => { designDrafts.write(scope, next); setDraft(next); setError(null); };
  const editValue = (id: string, value: DesignInput | null) => {
    if (inputDisabled) return;
    const values = { ...draft.values };
    if (value) values[id] = value;
    else delete values[id];
    save({ ...draft, values });
  };
  const submit = async (action: DesignChoiceAction) => {
    if (!active || sending.current || !question.allowed_actions.includes(action)) return;
    const body = prepareDesignChoice(question, draft, action, newRequestId);
    if (!body) return;
    const attempted = { ...draft, attemptedBody: body };
    // Persist before I/O. Unknown outcomes can only replay this exact body, including free text.
    designDrafts.write(scope, attempted);
    setDraft(attempted);
    sending.current = true;
    setBusy(true);
    setError(null);
    try {
      const result = await store.answerDesign(op.id, body);
      const received = result.result?.design_plan?.last_response ?? (result.question?.kind === 'design_choice' ? result.question.last_response : null);
      if (!designReceiptMatches(body, received)) throw new ApiError(0, 'design_response_missing', 'The design response is not yet confirmed.');
      designDrafts.remove(scope);
      if (mounted.current) { setDraft(emptyDesignDraft()); setAcknowledged(received); }
    } catch (e) {
      // Invalid inputs can be corrected. Unknown outcomes and identity conflicts retain the submitted identity.
      if (isApiError(e) && e.status === 422) {
        const refused = { ...attempted, attemptedBody: null };
        designDrafts.write(scope, refused);
        if (mounted.current) setDraft(refused);
      }
      if (mounted.current) setError(e);
      void store.refresh(op.conversation_id);
    } finally {
      sending.current = false;
      if (mounted.current) setBusy(false);
    }
  };
  return (
    <section className="as-design-choice" aria-labelledby={`${formId}-title`}>
      <div className="as-design-heading"><MessageSquareText aria-hidden /><strong id={`${formId}-title`}>{question.title || t('as.design.choice')}</strong></div>
      {question.description && <p className="small as-design-copy">{question.description}</p>}
      <p className="small faint">{t('as.design.choiceHint')}</p>
      {question.questions.map((q) => (
        <DesignField key={q.id} question={q} value={draft.values[q.id]} disabled={inputDisabled} error={errors[q.id]} onChange={(value) => editValue(q.id, value)} />
      ))}
      <label className="as-design-free">
        <span className="small strong">{t('as.design.freeText')}</span>
        <textarea className="input" rows={2} maxLength={DESIGN_TEXT_LIMIT} value={draft.freeText} disabled={inputDisabled} placeholder={t('as.design.freeTextHint')} onChange={(event) => save({ ...draft, freeText: event.target.value })} />
      </label>
      {acknowledged && <p className="small as-design-receipt" role="status"><Check aria-hidden />{t(`as.design.receipt.${acknowledged.action}`)}</p>}
      {!active && !acknowledged && <p className="small faint">{t('as.design.closed')}</p>}
      {draft.attemptedBody && !seen && <p className="small faint" role="status">{t('as.design.pendingReply')}</p>}
      {!designDrafts.persistent && <p className="small faint">{t('as.design.noStorage')}</p>}
      {active && (
        <div className="as-design-actions">
          {draft.attemptedBody ? (
            <button type="button" className="btn sm primary" disabled={busy || !question.allowed_actions.includes(draft.attemptedBody.action)} onClick={() => void submit(draft.attemptedBody!.action)}>
              {busy ? <Loader2 className="spin-icon" /> : <RotateCcw />} {t(busy ? 'as.design.sending' : 'as.design.retry')}
            </button>
          ) : (
            <>
              {question.allowed_actions.includes('accept_recommended') && <button type="button" className="btn sm primary" disabled={busy || hasErrors} onClick={() => void submit('accept_recommended')}><Sparkles />{t(hasEdits ? 'as.design.recommendWithEdits' : 'as.design.recommend')}</button>}
              {question.allowed_actions.includes('delegate_remaining') && <button type="button" className="btn sm" disabled={busy || hasErrors} onClick={() => void submit('delegate_remaining')}><Sparkles />{t(hasEdits ? 'as.design.delegateRemaining' : 'as.design.delegateAll')}</button>}
              {question.allowed_actions.includes('customize') && <button type="button" className="btn sm" disabled={busy || hasErrors || !hasEdits} onClick={() => void submit('customize')}><Check />{t('as.design.customize')}</button>}
            </>
          )}
        </div>
      )}
      <AsError error={error} />
    </section>
  );
}

function DesignField({ question: q, value, error, disabled, onChange }: { question: DesignQuestion; value?: DesignInput; error?: DesignInputError; disabled: boolean; onChange: (value: DesignInput | null) => void }) {
  const { t } = useI18n();
  const id = useId();
  const recommendation = q.recommended;
  const customText = value?.kind === 'value' ? value.text : '';
  const descriptionId = q.description ? `${id}-description` : undefined;
  const errorId = error ? `${id}-error` : undefined;
  const describedBy = [descriptionId, errorId].filter(Boolean).join(' ') || undefined;
  return (
    <fieldset className="as-design-field" disabled={disabled} aria-describedby={describedBy}>
      <legend>{q.label}{q.unit ? ` (${q.unit})` : ''}</legend>
      {q.description && <p id={descriptionId} className="small faint as-design-copy">{q.description}</p>}
      <div className="as-design-options">
        {q.options.map((option) => (
          <label key={option.id} className={`as-design-option ${value?.kind === 'option' && value.option_id === option.id ? 'selected' : ''}`}>
            <input type="radio" name={id} checked={value?.kind === 'option' && value.option_id === option.id} onChange={() => onChange({ kind: 'option', option_id: option.id })} />
            <span><span className="as-design-option-label">{option.label}{recommendation?.kind === 'option' && recommendation.option_id === option.id && <span className="tag">{t('as.design.recommended')}</span>}</span>{option.reason && <span className="small faint as-design-copy">{option.reason}</span>}</span>
          </label>
        ))}
      </div>
      {recommendation?.kind === 'value' && <p className="small faint">{t('as.design.recommendedValue', { value: `${designValueText(recommendation.value, t)}${q.unit ? ` ${q.unit}` : ''}` })}</p>}
      {q.allow_custom && (
        <label className="as-design-custom">
          <span className="small">{t('as.design.customValue')}</span>
          {q.input_kind === 'boolean' ? (
            <select className="input" value={customText} onChange={(e) => onChange(e.target.value ? { kind: 'value', text: e.target.value } : null)} aria-invalid={!!error} aria-describedby={describedBy}>
              <option value="">{t('as.design.unanswered')}</option><option value="true">{t('as.design.yes')}</option><option value="false">{t('as.design.no')}</option>
            </select>
          ) : (
            <input className="input" inputMode={q.input_kind === 'number' ? 'decimal' : undefined} value={customText} maxLength={DESIGN_VALUE_LIMIT} placeholder={t('as.design.unanswered')} onChange={(e) => onChange({ kind: 'value', text: e.target.value })} aria-invalid={!!error} aria-describedby={describedBy} />
          )}
        </label>
      )}
      {value && <button type="button" className="linkish small" onClick={() => onChange(null)}>{t('as.design.clear')}</button>}
      {error && <p id={errorId} className="small error-text" role="alert">{t(`as.design.invalid.${error}`, { min: q.minimum ?? '', max: q.maximum ?? '' })}</p>}
    </fieldset>
  );
}

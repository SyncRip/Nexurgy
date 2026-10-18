// Generation supplement D-03: a person corrects one AI decision of a Turbo spec version. The correction is an engineering
// change: it goes through the spec's answer flow (the decision id stands for the question), which makes a NEW version
// with the person's value; the corrected version keeps its decision as it was. Server: engineering_spec.decision_questions.

import { PencilLine, Send } from 'lucide-react';
import { useState } from 'react';
import { answerSpecQuestions, type SpecVersion } from '../../api/spec';
import { LimitedInput } from '../../components/ui/LimitedText';
import { ErrorNotice } from '../../components/ui/States';
import { useI18n } from '../../i18n/I18nProvider';

export function DecisionCorrect({ versionId, id, onDone }: { versionId: string; id: string; onDone?: (v: SpecVersion) => void }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  if (!open) {
    return (
      <button type="button" className="btn sm ghost" onClick={() => setOpen(true)}>
        <PencilLine aria-hidden /> {t('tb.decision.correct')}
      </button>
    );
  }
  const submit = async () => {
    if (!value.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const out = await answerSpecQuestions(versionId, [{ question_id: id, answer: value.trim() }]);
      setOpen(false);
      setValue('');
      onDone?.(out);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <form
      className="tb-decision-form"
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
    >
      <span className="tb-assumption-basis">{t('tb.decision.correctHint')}</span>
      <LimitedInput
        className="input"
        aria-label={t('tb.decision.correctLabel', { id })}
        placeholder={t('tb.decision.correctPlaceholder')}
        value={value}
        limit="description"
        onChange={(e) => setValue(e.target.value)}
      />
      <button type="submit" className="btn sm primary" disabled={busy || !value.trim()}>
        <Send aria-hidden /> {t('tb.decision.correctSubmit')}
      </button>
      <button type="button" className="btn sm ghost" disabled={busy} onClick={() => setOpen(false)}>
        {t('common.cancel')}
      </button>
      {error ? <ErrorNotice error={error} /> : null}
    </form>
  );
}

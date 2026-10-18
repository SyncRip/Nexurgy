import { MessageSquareReply } from 'lucide-react';
import { useState } from 'react';
import { answerNodeQuestions, type NodeTaskOut } from '../../api/collaboration';
import { ErrorNotice } from '../../components/ui/States';
import { useI18n } from '../../i18n/I18nProvider';

/** Questions an agent was blocked on (the CAD tools' clarification questions, else its missing facts) with one
 *  answer box each. Sending queues the node again; its next attempt receives the answers. */
export function AnswerQuestions({ runId, node, onAnswered }: { runId: string; node: NodeTaskOut; onAnswered: (n: NodeTaskOut) => void }) {
  const { t } = useI18n();
  const raw = node.blocked_reason?.questions;
  const questions = Array.isArray(raw) ? raw.map(String).filter(Boolean) : [];
  const fallback = questions.length ? questions : [String(node.blocked_reason?.message ?? '')];
  const [answers, setAnswers] = useState<string[]>(() => fallback.map(() => ''));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const filled = answers.some((a) => a.trim());

  async function send() {
    setBusy(true);
    setError(null);
    try {
      const out = await answerNodeQuestions(runId, node.node_id, {
        answers: fallback.map((q, i) => ({ question: q, answer: answers[i] ?? '' })).filter((a) => a.answer.trim()),
        expected_version: node.version,
      });
      onAnswered(out);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="cl-answers" aria-label={t('collab.answers.title')}>
      <h4 className="cl-answers-title">
        <MessageSquareReply aria-hidden size={16} /> {t('collab.answers.title')}
      </h4>
      <p className="faint">{t('collab.answers.hint')}</p>
      <ol className="cl-answers-list">
        {fallback.map((q, i) => (
          <li key={i}>
            <label>
              <span className="cl-answers-q">{q}</span>
              <textarea
                rows={2}
                value={answers[i] ?? ''}
                onChange={(e) => setAnswers((prev) => prev.map((a, j) => (j === i ? e.target.value : a)))}
                placeholder={t('collab.answers.placeholder')}
              />
            </label>
          </li>
        ))}
      </ol>
      {error ? <ErrorNotice error={error} /> : null}
      <button type="button" className="btn primary" disabled={!filled || busy} onClick={send}>
        {busy ? t('collab.answers.sending') : t('collab.answers.send')}
      </button>
    </section>
  );
}

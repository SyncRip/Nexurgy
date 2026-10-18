// What an AI revision recorded: the request, changes, assumptions and model.
// (The per-part AI run panel was replaced by the unified assistant, which the
// model page embeds in its "AI" side tab.)

import type { ModelRevision } from '../../../api/types';
import { useI18n } from '../../../i18n/I18nProvider';

/** changes / assumptions stored with an AI revision. */
export function AiSummary({ rev }: { rev: ModelRevision }) {
  const { t } = useI18n();
  const s = (rev.ai_summary ?? {}) as { changes?: string[]; assumptions?: string[]; model?: string; provider?: string };
  return (
    <div className="stack-sm">
      {rev.request_text && (
        <p className="small">
          <span className="faint">{t('ws.ai.request')}: </span>“{rev.request_text}”
        </p>
      )}
      {!!s.changes?.length && (
        <div>
          <div className="small strong">{t('ws.ai.changes')}</div>
          <ul className="ws-plain small">
            {s.changes.map((c, i) => (
              <li key={i}>{c}</li>
            ))}
          </ul>
        </div>
      )}
      {!!s.assumptions?.length && (
        <div>
          <div className="small strong">{t('ws.ai.assumptions')}</div>
          <ul className="ws-plain small">
            {s.assumptions.map((c, i) => (
              <li key={i}>{c}</li>
            ))}
          </ul>
        </div>
      )}
      {s.provider && (
        <p className="faint small mono">
          {s.provider}/{s.model}
        </p>
      )}
    </div>
  );
}

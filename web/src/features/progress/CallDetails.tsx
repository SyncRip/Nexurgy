import { CheckCircle2, XCircle } from 'lucide-react';
import type { ProgressCall } from '../../api/progress';
import { useI18n } from '../../i18n/I18nProvider';
import { formatDuration } from '../workflow/model';
import { failureLabel, purposeLabel } from './labels';

/** Only public call metadata; unsuccessful attempts remain in the unit's history after recovery. */
export function CallDetails({ calls }: { calls: ProgressCall[] }) {
  const { t, fmt } = useI18n();
  const num = (n: number | null) => n === null ? t('cad.run.unknown') : fmt.number(n);
  return (
    <ol className="prog-calls">
      {calls.map((call) => {
        const ok = call.status === 'ok' || call.status === 'succeeded';
        return (
          <li key={call.id} className="prog-call">
            <div className="prog-row">
              {ok ? <CheckCircle2 className="prog-ico ok" aria-hidden /> : <XCircle className="prog-ico err" aria-hidden />}
              <strong>{purposeLabel(t, call.purpose)}</strong>
              <span>{ok ? t('cad.run.ok') : t('cad.run.error')}</span>
              {call.attempt !== null && <span className="faint">{t('prog.unit.attempt', { n: call.attempt })}</span>}
              {call.simulated && <span className="prog-chip">{t('prog.sum.simulated')}</span>}
            </div>
            <dl className="prog-call-meta">
              <div><dt>{t('prog.call.started')}</dt><dd>{call.started_at ? fmt.dateTime(call.started_at) : t('cad.run.unknown')}</dd></div>
              <div><dt>{t('cad.run.colLatency')}</dt><dd>{call.latency_ms === null ? t('cad.run.unknown') : formatDuration(call.latency_ms)}</dd></div>
              <div><dt>{t('cad.run.colTokens')}</dt><dd>{call.simulated ? t('prog.call.noUsage') : `${num(call.tokens_in)} / ${num(call.tokens_out)}`}</dd></div>
              <div><dt>{t('cad.run.colRequest')}</dt><dd className="mono">{call.request_id ?? t('cad.run.unknown')}</dd></div>
            </dl>
            {call.error && <p className="prog-call-error">{[failureLabel(t, call.error), call.error.code].filter(Boolean).join(' · ')}</p>}
          </li>
        );
      })}
    </ol>
  );
}

import './aiFailure.css';
import { hasKey } from '../../i18n/core';
import type { MessageKey } from '../../i18n/en';
import { useI18n } from '../../i18n/I18nProvider';

interface FailureCause {
  stage: string;
  code: string;
  message: string;
  failureClass: string;
  definition: string;
  name: string;
  instances: string[];
  callIds: string[];
}

const record = (value: unknown): Record<string, unknown> | null =>
  value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null;
const str = (value: unknown, max = 300): string => typeof value === 'string' ? value.slice(0, max) : '';
const strings = (value: unknown, max: number): string[] => Array.isArray(value)
  ? value.filter((v): v is string => typeof v === 'string').slice(0, max).map((v) => v.slice(0, 100)) : [];

/** Read observed causes only. Old failures without evidence never acquire a guessed provider diagnosis. */
export function readFailureCauses(details: unknown): FailureCause[] {
  const values = record(details)?.causes;
  if (!Array.isArray(values)) return [];
  return values.slice(0, 12).flatMap((value) => {
    const c = record(value);
    if (!c || !str(c.code)) return [];
    return [{
      stage: str(c.stage, 60), code: str(c.code, 80), message: str(c.message),
      failureClass: str(c.failure_class, 60), definition: str(c.definition, 100), name: str(c.name, 100),
      instances: strings(c.instances, 20), callIds: strings(c.call_ids, 10),
    }];
  });
}

interface CauseGroup extends FailureCause {
  subjects: string[];
}

export function groupFailureCauses(causes: readonly FailureCause[]): CauseGroup[] {
  const groups = new Map<string, CauseGroup>();
  for (const cause of causes) {
    const key = JSON.stringify([cause.stage, cause.code, cause.failureClass, cause.message]);
    const group = groups.get(key) ?? { ...cause, subjects: [], callIds: [], instances: [] };
    const subject = cause.name || cause.definition;
    if (subject && !group.subjects.includes(subject)) group.subjects.push(subject);
    group.instances = [...new Set([...group.instances, ...cause.instances])];
    group.callIds = [...new Set([...group.callIds, ...cause.callIds])];
    groups.set(key, group);
  }
  return [...groups.values()];
}

const SERVICE_CLASSES = new Set(['connection', 'rate_limited', 'server_error', 'overloaded', 'stream_broken', 'no_response']);

export function AiFailureCauses({ details }: { details: unknown }) {
  const { t } = useI18n();
  const causes = readFailureCauses(details);
  if (!causes.length) return null;
  const groups = groupFailureCauses(causes);
  const tr = (key: string, fallback: string) => hasKey(key) ? t(key as MessageKey) : fallback;
  return (
    <section className="ai-failure-causes" aria-label={t('cov.failure.causes')}>
      <strong>{t('cov.failure.causes')}</strong>
      {causes.some((c) => SERVICE_CLASSES.has(c.failureClass)) && <p className="small">{t('cov.failure.serviceHint')}</p>}
      <ul>
        {groups.map((cause, index) => (
          <li key={index}>
            <div className="ai-diag-code">
              <strong>{tr(`cov.use.stage.${cause.stage}`, cause.stage || t('cov.stage.other'))}</strong>
              {cause.subjects.length > 0 && <span>{cause.subjects.join('、')}</span>}
              <span>{tr(`cad.fclass.${cause.failureClass}`, tr(`diag.code.${cause.code}`, cause.code))}</span>
            </div>
            {cause.message && <div className="small ai-cause-message">{cause.message}</div>}
            {cause.instances.length > 0 && <div className="small faint">{t('cov.failure.instances', { list: cause.instances.join(', ') })}</div>}
            {cause.callIds.length > 0 && (
              <details className="small">
                <summary>{t('diag.callsLabel')}</summary>
                <div className="mono ai-diag-ids">{cause.callIds.map((id) => <span key={id}>{id}</span>)}</div>
              </details>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

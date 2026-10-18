// What an AI failure was, where it happened, with which exact model configuration, which model calls to
// look up and what to do (W1 diagnostics, server/app/ai/diagnostics.py). Used by the collaboration draft's
// planning banner, AI workspace operation errors and the model settings' connection test. Shows the observed
// code only; it never guesses a root cause, and the payload never contains a key.

import './aiFailure.css';
import type { AiDiagnostic } from '../../api/types';
import { hasKey } from '../../i18n/core';
import type { MessageKey } from '../../i18n/en';
import { useI18n } from '../../i18n/I18nProvider';

/** The diagnostic inside an untyped record (planning record, error details), or null when absent / malformed. */
export function asDiagnostic(value: unknown): AiDiagnostic | null {
  if (!value || typeof value !== 'object') return null;
  const v = value as Record<string, unknown>;
  if (typeof v.code !== 'string' || !v.code) return null;
  return {
    code: v.code,
    stage: typeof v.stage === 'string' ? v.stage : 'model_call',
    purpose: typeof v.purpose === 'string' ? v.purpose : null,
    config_id: typeof v.config_id === 'string' ? v.config_id : null,
    config_version: typeof v.config_version === 'number' ? v.config_version : null,
    call_ids: Array.isArray(v.call_ids) ? v.call_ids.filter((x): x is string => typeof x === 'string') : [],
    retryable: v.retryable === true,
    recovery: Array.isArray(v.recovery) ? v.recovery.filter((x): x is string => typeof x === 'string') : [],
    provider_code: typeof v.provider_code === 'string' ? v.provider_code : null,
  };
}

export function AiFailureDetails({ diagnostic, showTitle = true }: { diagnostic: AiDiagnostic | null | undefined; showTitle?: boolean }) {
  const { t } = useI18n();
  if (!diagnostic) return null;
  const tr = (key: string, fallback: string) => (hasKey(key) ? t(key as MessageKey) : fallback);
  const d = diagnostic;
  return (
    <div className="ai-diag" role="group" aria-label={t('diag.title')}>
      {showTitle && <div className="ai-diag-title">{t('diag.title')}</div>}
      <div className="ai-diag-code">
        <span>{tr(`diag.code.${d.code}`, t('diag.code.unknown'))}</span> <code className="error-code">{d.code}</code>
        {d.provider_code && d.provider_code !== d.code && <code className="error-code faint">{d.provider_code}</code>}
      </div>
      <dl className="ai-diag-grid small">
        <dt>{t('diag.stageLabel')}</dt>
        <dd>{tr(`diag.stage.${d.stage}`, d.stage)}</dd>
        {d.purpose && (
          <>
            <dt>{t('diag.purposeLabel')}</dt>
            <dd>{tr(`cred.purpose.${d.purpose}`, d.purpose)}</dd>
          </>
        )}
        {d.config_id && (
          <>
            <dt>{t('diag.configLabel')}</dt>
            <dd className="mono" title={d.config_id}>
              {d.config_version != null ? t('diag.configValue', { id: d.config_id.slice(0, 8), version: d.config_version }) : d.config_id.slice(0, 8)}
            </dd>
          </>
        )}
        {d.call_ids.length > 0 && (
          <>
            <dt>{t('diag.callsLabel')}</dt>
            <dd className="mono ai-diag-ids">
              {d.call_ids.map((id) => (
                <span key={id}>{id}</span>
              ))}
            </dd>
          </>
        )}
      </dl>
      {d.recovery.length > 0 && (
        <div className="small">
          <span className="faint">{t('diag.recoveryLabel')}</span>
          <ul className="ai-diag-recovery">
            {d.recovery.map((r) => (
              <li key={r}>{tr(`diag.recovery.${r}`, r)}</li>
            ))}
          </ul>
        </div>
      )}
      {d.retryable && <p className="faint small">{t('diag.retryable')}</p>}
    </div>
  );
}

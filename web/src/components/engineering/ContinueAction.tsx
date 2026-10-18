// "Continue" of a failed multi-step AI task (S3): shown on failure states, driven by the failure's recovery code
// (recovery.ts). Enabled only for a retryable failure whose recovery continues from the checkpoint; otherwise disabled
// with the reason (what to do instead). The server re-checks everything (409 not_continuable …).

import { StepForward } from 'lucide-react';
import { useId, useState } from 'react';
import { ErrorNotice } from '../ui/States';
import { useI18n } from '../../i18n/I18nProvider';
import { continueState, recoveryText, type FailureInfo, type RecoveryCode } from './recovery';

export function ContinueAction({
  failure,
  onContinue,
  blocked,
  canWrite = true,
  showText = true,
  also,
}: {
  failure: FailureInfo | null | undefined;
  /** Calls the continue endpoint; the page refreshes / navigates on success. */
  onContinue: () => Promise<unknown>;
  /** A reason the page knows that it cannot be continued (translated), e.g. already continued. */
  blocked?: string | null;
  /** The person may start work in this project (viewers only read). */
  canWrite?: boolean;
  /** Also show the recovery text ("What to do"). */
  showText?: boolean;
  /** Recovery codes that continue on this page only (recovery.ts continueState). */
  also?: readonly RecoveryCode[];
}) {
  const { t } = useI18n();
  const id = useId();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const state = continueState(t, failure, blocked, also);
  if (!state.show) return null;
  const text = recoveryText(t, failure);
  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      await onContinue();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="stack-sm recovery-action">
      {showText && text ? (
        <span className="small">
          <span className="faint">{t('recovery.label')}: </span>
          {text}
        </span>
      ) : null}
      {canWrite ? (
        <div className="row wrap">
          <button
            type="button"
            className="btn sm primary"
            onClick={() => void run()}
            disabled={!state.enabled || busy}
            aria-describedby={state.reason ? `${id}-why` : undefined}
            title={state.reason ?? undefined}
          >
            <StepForward aria-hidden /> {busy ? t('recovery.continuing') : t('recovery.continue')}
          </button>
          {state.reason ? (
            <span id={`${id}-why`} className="faint small">
              {t('recovery.disabled', { reason: state.reason })}
            </span>
          ) : null}
        </div>
      ) : null}
      {error ? <ErrorNotice error={error} /> : null}
    </div>
  );
}

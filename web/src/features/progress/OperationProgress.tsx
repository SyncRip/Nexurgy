// The execution process of an operation's run inside its conversation card: polled while the operation is active,
// loaded once otherwise. Until the first view arrives the card's own note (`children`) stays in place.

import type { ReactNode } from 'react';
import { useI18n } from '../../i18n/I18nProvider';
import { ProgressTimeline } from './ProgressTimeline';
import type { useRunProgress } from './useRunProgress';

/** The operation owns one progress subscription shared by the visible gallery and these diagnostic details. */
export function OperationProgress({ progress, error, reload, expanded, setExpanded, children }: ReturnType<typeof useRunProgress> & { children?: ReactNode }) {
  const { t } = useI18n();
  return (
    <>
      {progress ? <ProgressTimeline progress={progress} variant="compact" expanded={expanded} onExpandedChange={setExpanded} /> : children}
      {!!error && (
        <p className="as-op-progress" role="status">
          {t('prog.loadError')} <button type="button" className="linkish" onClick={reload}>{t('common.retry')}</button>
        </p>
      )}
    </>
  );
}

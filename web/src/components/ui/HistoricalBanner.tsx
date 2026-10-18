// "This is a historical version" (S6, agent execution layer design §3.3): shown at the top of a page that opened an
// exact version which is not the current one — a link from an earlier result, run or asset opens what it refers to,
// never silently the newest. The page stays on the old version; the button opens the current one.

import { History } from 'lucide-react';
import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { useI18n } from '../../i18n/I18nProvider';

export function HistoricalBanner({
  current,
  onOpenCurrent,
  currentTo,
  children,
  className = '',
}: {
  /** Label of the current version (R5, D3, v2 …); omitted when not known. */
  current?: string | null;
  /** Opens the current version in place (a state change on the same page) … */
  onOpenCurrent?: () => void;
  /** … or a link to it. */
  currentTo?: string;
  /** Extra page-specific explanation under the title. */
  children?: ReactNode;
  className?: string;
}) {
  const { t } = useI18n();
  const label = current ? `${t('ver.history.open')} · ${current}` : t('ver.history.open');
  return (
    <div className={`callout warn hist-banner ${className}`} role="note" aria-label={t('ver.history.title')}>
      <History aria-hidden />
      <div className="grow hist-banner-text">
        <strong>{t('ver.history.title')}</strong>
        <span>{current ? t('ver.history.body', { current }) : t('ver.history.bodyNoCurrent')}</span>
        {children}
      </div>
      {currentTo ? (
        <Link className="btn sm" to={currentTo}>
          {label}
        </Link>
      ) : onOpenCurrent ? (
        <button type="button" className="btn sm" onClick={onOpenCurrent}>
          {label}
        </button>
      ) : null}
    </div>
  );
}

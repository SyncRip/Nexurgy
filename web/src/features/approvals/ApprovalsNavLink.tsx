// Global "Approvals" entry for the shell (rail + mobile drawer) with the count
// of pending requests the current user may decide (scope=mine). The count is
// refreshed periodically, when the tab becomes visible and after decisions.

import './approvals.css';
import { ClipboardCheck } from 'lucide-react';
import { useEffect, useState } from 'react';
import { NavLink } from 'react-router-dom';
import { isAbortError } from '../../api/client';
import { listApprovalRequests } from '../../api/approvals';
import { paths } from '../../app/nav';
import { useI18n } from '../../i18n/I18nProvider';
import { onApprovalsChanged } from './meta';

const LIMIT = 100;
const POLL_MS = 60_000;

/** Pending requests in "mine" scope; null until known (or when the server lacks the endpoint). */
export function usePendingApprovalCount(): number | null {
  const [count, setCount] = useState<number | null>(null);
  useEffect(() => {
    let ctrl: AbortController | null = null;
    const load = () => {
      if (document.visibilityState === 'hidden') return;
      ctrl?.abort();
      const c = new AbortController();
      ctrl = c;
      listApprovalRequests({ scope: 'mine', status: 'pending', limit: LIMIT }, { signal: c.signal })
        .then((rows) => {
          if (!c.signal.aborted) setCount(rows.length);
        })
        .catch((e: unknown) => {
          if (!isAbortError(e) && !c.signal.aborted) setCount(null);
        });
    };
    load();
    const h = window.setInterval(load, POLL_MS);
    const onVis = () => document.visibilityState === 'visible' && load();
    document.addEventListener('visibilitychange', onVis);
    const off = onApprovalsChanged(load);
    return () => {
      ctrl?.abort();
      window.clearInterval(h);
      document.removeEventListener('visibilitychange', onVis);
      off();
    };
  }, []);
  return count;
}

function Badge({ count }: { count: number | null }) {
  const { t } = useI18n();
  if (!count) return null;
  const text = count >= LIMIT ? `${LIMIT - 1}+` : String(count);
  return (
    <span className="ap-nav-badge" aria-label={t('ap.nav.pending', { count: text })} title={t('ap.nav.pending', { count: text })}>
      {text}
    </span>
  );
}

/** Rail link (same look as the other rail links) with the pending badge. */
export function ApprovalsRailLink() {
  const { t } = useI18n();
  const count = usePendingApprovalCount();
  return (
    <NavLink to={paths.approvals()} end className={({ isActive }) => `rail-link ap-rail-link ${isActive ? 'active' : ''}`} title={t('ap.nav.label')}>
      <ClipboardCheck />
      <span className="rail-label">{t('ap.nav.label')}</span>
      <Badge count={count} />
    </NavLink>
  );
}

/** Drawer entry for the mobile menu. */
export function ApprovalsDrawerLink({ onGo }: { onGo: (to: string) => void }) {
  const { t } = useI18n();
  const count = usePendingApprovalCount();
  return (
    <button type="button" className="drawer-link ap-drawer-link" onClick={() => onGo(paths.approvals())}>
      <ClipboardCheck /> {t('ap.nav.label')}
      <Badge count={count} />
    </button>
  );
}

/** The pending-for-me badge on its own (R1.2 rail entry). */
export function PendingApprovalsBadge() {
  const count = usePendingApprovalCount();
  return <Badge count={count} />;
}

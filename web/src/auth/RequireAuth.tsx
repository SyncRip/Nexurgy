import { RefreshCw, ServerOff } from 'lucide-react';
import type { ReactNode } from 'react';
import { Link, Navigate, useLocation } from 'react-router-dom';
import { API_ORIGIN } from '../api/client';
import { Logo } from '../components/Logo';
import { Spinner } from '../components/ui/States';
import { useI18n } from '../i18n/I18nProvider';
import { useAuth } from './AuthProvider';

/** Route guard: renders children only for a signed-in user. */
export function RequireAuth({ children }: { children: ReactNode }) {
  const { status, recheck, logout } = useAuth();
  const { t } = useI18n();
  const location = useLocation();

  if (status === 'loading') {
    return (
      <div className="fullpage">
        <Logo size={40} />
        <Spinner label={t('auth.checking')} />
        <p className="faint">{t('auth.checking')}</p>
      </div>
    );
  }
  if (status === 'unreachable') {
    return (
      <div className="fullpage">
        <span className="err-mark big" aria-hidden>
          <ServerOff />
        </span>
        <h1>{t('auth.unreachableTitle')}</h1>
        <p className="muted measure-text">{t('auth.unreachableBody', { url: API_ORIGIN })}</p>
        <div className="row">
          <button type="button" className="btn primary" onClick={recheck}>
            <RefreshCw /> {t('common.retry')}
          </button>
          <button type="button" className="btn" onClick={() => void logout()}>
            {t('user.signOut')}
          </button>
          <Link to="/" className="btn ghost">
            {t('auth.backToHome')}
          </Link>
        </div>
      </div>
    );
  }
  if (status === 'signed_out') {
    return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />;
  }
  return <>{children}</>;
}

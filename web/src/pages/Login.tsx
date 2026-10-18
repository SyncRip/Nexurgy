import { ArrowLeft, Info, LogIn } from 'lucide-react';
import { useState } from 'react';
import { Link, Navigate, useLocation, useNavigate } from 'react-router-dom';
import { API_ORIGIN, isApiError } from '../api/client';
import { paths } from '../app/nav';
import { useAuth } from '../auth/AuthProvider';
import { Logo } from '../components/Logo';
import { LanguageToggle, ThemeToggle } from '../components/shell/Prefs';
import { Field } from '../components/ui/Form';
import { ErrorNotice } from '../components/ui/States';
import { useI18n } from '../i18n/I18nProvider';
import { Trans } from '../i18n/Trans';

const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

export default function Login() {
  const { t } = useI18n();
  const { status, login, signOutReason } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const from = (location.state as { from?: string } | null)?.from;
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  if (status === 'signed_in') return <Navigate to={from && from !== '/login' ? from : paths.workbench()} replace />;

  const emailErr = !email.trim() ? t('auth.emailRequired') : !EMAIL_RE.test(email.trim()) ? t('auth.emailInvalid') : null;
  const passErr = !password ? t('auth.passwordRequired') : null;

  const submit = async () => {
    setTouched(true);
    if (emailErr || passErr) return;
    setBusy(true);
    setError(null);
    try {
      await login(email.trim(), password);
      navigate(from && from !== '/login' ? from : paths.workbench(), { replace: true });
    } catch (e) {
      setError(e);
      // Never keep a rejected password in the field.
      if (isApiError(e) && e.code === 'invalid_credentials') {
        setPassword('');
        setTouched(false);
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login">
      <div className="login-top">
        <Link to="/" className="btn ghost sm">
          <ArrowLeft /> {t('auth.backToHome')}
        </Link>
        <span className="grow" />
        <LanguageToggle />
        <ThemeToggle />
      </div>
      <div className="login-grid">
        <aside className="login-panel" aria-hidden>
          <div className="login-blueprint">
            <svg viewBox="0 0 320 220">
              <g fill="none" strokeWidth="1">
                <path d="M40 170 L160 200 L280 170 L280 90 L160 60 L40 90 Z" stroke="var(--line-2)" />
                <path d="M40 90 L160 120 L280 90 M160 120 L160 200" stroke="var(--line-2)" />
                <path d="M100 105 L160 120 L220 105" stroke="var(--measure)" strokeDasharray="4 4" />
                <ellipse cx="120" cy="96" rx="16" ry="8" stroke="var(--accent)" />
                <ellipse cx="200" cy="96" rx="16" ry="8" stroke="var(--accent)" />
                <path d="M40 186 L160 216 M40 180 L40 192 M160 210 L160 222" stroke="var(--measure)" />
              </g>
            </svg>
          </div>
          <h2>{t('auth.panelTitle')}</h2>
          <p>{t('auth.panelBody')}</p>
        </aside>
        <main className="login-card">
          <div className="login-brand">
            <Logo size={36} />
            <div>
              <h1>{t('auth.title')}</h1>
              <p className="muted">{t('auth.subtitle')}</p>
            </div>
          </div>
          {signOutReason === 'expired' && !error && (
            <div className="callout warn" role="status">
              <Info aria-hidden />
              <span>{t('auth.sessionExpired')}</span>
            </div>
          )}
          {signOutReason === 'user' && !error && (
            <div className="callout info" role="status">
              <Info aria-hidden />
              <span>{t('user.signedOut')}</span>
            </div>
          )}
          <form
            className="stack"
            noValidate
            onSubmit={(e) => {
              e.preventDefault();
              void submit();
            }}
          >
            <Field label={t('auth.email')} error={touched ? emailErr : null}>
              <input className="input lg" type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} autoFocus disabled={busy} />
            </Field>
            <Field label={t('auth.password')} error={touched ? passErr : null}>
              <input className="input lg" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} disabled={busy} />
            </Field>
            <ErrorNotice error={error} />
            <button type="submit" className="btn primary lg" disabled={busy}>
              <LogIn /> {busy ? t('auth.signingIn') : t('auth.signIn')}
            </button>
          </form>
          <div className="dev-note">
            <b>{t('auth.devNoteTitle')}</b>
            <span>
              <Trans k="auth.devNote" values={{ cmd: <code>uv run python -m app.seed</code> }} />
            </span>
          </div>
          <p className="faint mono small">{t('auth.apiTarget', { url: API_ORIGIN })}</p>
        </main>
      </div>
    </div>
  );
}

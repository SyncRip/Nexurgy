// Session state for the PartRoute API.
//
// Token storage trade-off: the bearer token lives in localStorage under
// `partroute.token`. That keeps the SPA stateless and lets the API stay a plain
// bearer-token service that can be hosted on a different origin (Firebase
// Hosting → Cloud Run) without cookie / CSRF machinery. The cost is that any
// script running on this origin (an XSS bug, a malicious dependency) can read
// the token. Mitigations: no third-party scripts besides fonts, React escaping
// everywhere, short server-side TTL (PARTROUTE_TOKEN_TTL_HOURS), revocation on
// sign-out (POST /auth/logout), and a global sign-out on any 401. If the threat
// model tightens, move to an httpOnly SameSite cookie issued by the API.

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { isApiError, onUnauthorized, setTokenProvider } from '../api/client';
import { getMe, login as apiLogin, logout as apiLogout } from '../api/endpoints';
import { setCapabilitiesIdentity } from '../api/capabilities';
import type { User } from '../api/types';

export const TOKEN_STORAGE_KEY = 'partroute.token';

export type AuthStatus = 'loading' | 'signed_in' | 'signed_out' | 'unreachable';
export type SignOutReason = 'user' | 'expired' | null;

interface AuthValue {
  status: AuthStatus;
  user: User | null;
  /** Why the last sign-out happened (shown on the login page). */
  signOutReason: SignOutReason;
  login: (email: string, password: string) => Promise<User>;
  logout: () => Promise<void>;
  /** Re-run the session check (after the API was unreachable). */
  recheck: () => void;
}

const AuthContext = createContext<AuthValue | null>(null);

function readToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_STORAGE_KEY);
  } catch {
    return null;
  }
}

function writeToken(token: string | null): void {
  try {
    if (token) localStorage.setItem(TOKEN_STORAGE_KEY, token);
    else localStorage.removeItem(TOKEN_STORAGE_KEY);
  } catch {
    /* storage unavailable: the session lasts for this page only */
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const tokenRef = useRef<string | null>(readToken());
  const [status, setStatus] = useState<AuthStatus>(tokenRef.current ? 'loading' : 'signed_out');
  const [user, setUser] = useState<User | null>(null);
  const [signOutReason, setSignOutReason] = useState<SignOutReason>(null);
  const [checkNonce, setCheckNonce] = useState(0);

  // The API client reads the token through this provider (set once, synchronously,
  // before any child effect can issue a request).
  setTokenProvider(() => tokenRef.current);

  const clearSession = useCallback((reason: SignOutReason) => {
    setCapabilitiesIdentity(null);
    tokenRef.current = null;
    writeToken(null);
    setUser(null);
    setSignOutReason(reason);
    setStatus('signed_out');
  }, []);

  // Validate a stored token on load (and on recheck).
  useEffect(() => {
    if (!tokenRef.current) return;
    const ctrl = new AbortController();
    setStatus('loading');
    getMe({ signal: ctrl.signal })
      .then((u) => {
        if (ctrl.signal.aborted) return;
        setCapabilitiesIdentity(u.id);
        setUser(u);
        setStatus('signed_in');
      })
      .catch((e: unknown) => {
        if (ctrl.signal.aborted) return;
        if (isApiError(e) && e.status === 401) clearSession('expired');
        else if (isApiError(e) && e.code === 'network') setStatus('unreachable');
        else setStatus('unreachable');
      });
    return () => ctrl.abort();
  }, [checkNonce, clearSession]);

  // Any 401 on an authenticated request ends the session.
  useEffect(() => onUnauthorized(() => clearSession('expired')), [clearSession]);

  // Sign-in / sign-out in another tab.
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key !== TOKEN_STORAGE_KEY) return;
      if (!e.newValue) {
        if (tokenRef.current) clearSession('user');
      } else if (e.newValue !== tokenRef.current) {
        setCapabilitiesIdentity(null);
        setUser(null);
        setStatus('loading');
        tokenRef.current = e.newValue;
        setCheckNonce((n) => n + 1);
      }
    };
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, [clearSession]);

  const login = useCallback(async (email: string, password: string) => {
    const res = await apiLogin({ email, password });
    tokenRef.current = res.token;
    writeToken(res.token);
    setCapabilitiesIdentity(null);
    setCapabilitiesIdentity(res.user.id);
    setUser(res.user);
    setSignOutReason(null);
    setStatus('signed_in');
    return res.user;
  }, []);

  const logout = useCallback(async () => {
    try {
      if (tokenRef.current) await apiLogout();
    } catch {
      /* revoke is best effort; the local session ends regardless */
    }
    clearSession('user');
  }, [clearSession]);

  const recheck = useCallback(() => setCheckNonce((n) => n + 1), []);

  const value = useMemo<AuthValue>(() => ({ status, user, signOutReason, login, logout, recheck }), [status, user, signOutReason, login, logout, recheck]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const v = useContext(AuthContext);
  if (!v) throw new Error('useAuth must be used inside <AuthProvider>');
  return v;
}

/** The signed-in user (only call below <RequireAuth>). */
export function useUser(): User {
  const { user } = useAuth();
  if (!user) throw new Error('useUser called without a signed-in user');
  return user;
}

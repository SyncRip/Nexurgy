import { Suspense } from 'react';
import { HashRouter, Route, Routes, useLocation } from 'react-router-dom';
import { AuthProvider } from '../auth/AuthProvider';
import { RequireAuth } from '../auth/RequireAuth';
import { AppShell } from '../components/shell/AppShell';
import { SkeletonRows } from '../components/ui/States';
import { ToastProvider } from '../components/ui/Toast';
import { I18nProvider } from '../i18n/I18nProvider';
import Home from '../pages/Home';
import Login from '../pages/Login';
import { ThemeProvider } from '../prefs/ThemeProvider';
import { FeatureGate } from '../features/capabilities/FeatureGate';
import { routeFeature } from './featureAccess';
import type { ReactNode } from 'react';
import { SHELL_ROUTES } from './routes';

function FeatureRoute({ children }: { children: ReactNode }) {
  const { pathname, search } = useLocation();
  const feature = routeFeature(pathname, search);
  return feature ? <FeatureGate feature={feature}>{children}</FeatureGate> : children;
}

function PageFallback() {
  return (
    <div className="page">
      <SkeletonRows />
    </div>
  );
}

/**
 * Provider order: preferences first (theme, language) so every screen —
 * including the login page and full-page states — is themed and translated.
 * Nothing in the tree is keyed by language: switching it keeps route state,
 * open dialogs and unsaved drafts.
 */
export function App() {
  return (
    <ThemeProvider>
      <I18nProvider>
        <ToastProvider>
          <AuthProvider>
            <HashRouter>
              <Routes>
                <Route path="/" element={<Home />} />
                <Route path="/login" element={<Login />} />
                <Route
                  element={
                    <RequireAuth>
                      <AppShell />
                    </RequireAuth>
                  }
                >
                  {SHELL_ROUTES.map((r) => (
                    <Route key={r.path} path={r.path} element={<Suspense fallback={<PageFallback />}><FeatureRoute>{r.element}</FeatureRoute></Suspense>} />
                  ))}
                </Route>
              </Routes>
            </HashRouter>
          </AuthProvider>
        </ToastProvider>
      </I18nProvider>
    </ThemeProvider>
  );
}

// Authenticated layout: top bar (with the project switcher), left rail of the
// R1.2 areas (bottom bar + drawer under 768px), main outlet, assistant dock.

import { ChevronRight,FolderKanban,House,LogOut,Menu as MenuIcon,PanelLeftClose,PanelLeftOpen,Settings,ShieldCheck } from 'lucide-react';
import { useState } from 'react';
import { Link,NavLink,Outlet,useLocation,useNavigate } from 'react-router-dom';
import { AREAS,areaOf,paths,type AreaMeta } from '../../app/nav';
import { useAuth,useUser } from '../../auth/AuthProvider';
import { useI18n } from '../../i18n/I18nProvider';
import { Wordmark } from '../Logo';
import { Dialog } from '../ui/Dialog';
// [FB approvals] global Approvals entry with the pending-for-me badge.
// [F notifications] bell with the unread count.
import { TodoBell,TodosProvider } from '../../features/todos/Todos';
import { Menu } from '../ui/Menu';
import { CurrentProjectProvider } from './CurrentProject';
import { LanguageToggle,ThemeSegmented,ThemeToggle } from './Prefs';
import { ProjectSwitcher } from './ProjectSwitcher';
import { ServiceStatus } from './ServiceStatus';
import { ShellProvider,useShell } from './ShellContext';
// Unified AI workspace: one assistant for every page (.trellis/tasks/archive/2026-10/10-08-ai-workspace/design.md §7).
import { AssistantProvider } from '../../features/assistant/AssistantProvider';
import {
AssistantBottomLink,
AssistantDock,
AssistantDrawerLink,
AssistantTopButton,
useAssistantHotkeys,
useDockVisible,
} from '../../features/assistant/ShellEntries';
import '../../features/assistant/assistant.css';

function initials(name: string, email: string): string {
  const src = name.trim() || email;
  const parts = src.split(/\s+/).filter(Boolean);
  const s = parts.length > 1 ? parts[0][0] + parts[1][0] : src.slice(0, 2);
  return s.toUpperCase();
}

function Breadcrumbs() {
  const { t } = useI18n();
  const { crumbs } = useShell();
  if (!crumbs.length) return null;
  return (
    <nav className="crumbs" aria-label={t('nav.breadcrumbs')}>
      <ol>
        {crumbs.map((c, i) => {
          const last = i === crumbs.length - 1;
          return (
            <li key={`${i}-${c.label}`} className={last ? 'current' : ''}>
              {i > 0 && <ChevronRight className="sep" aria-hidden />}
              {c.to && !last ? (
                <Link to={c.to} className={c.mono ? 'mono' : ''}>
                  {c.label}
                </Link>
              ) : (
                <span className={c.mono ? 'mono' : ''} aria-current={last ? 'page' : undefined}>
                  {c.label}
                </span>
              )}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}

function UserMenu() {
  const { t } = useI18n();
  const user = useUser();
  const { logout } = useAuth();
  const navigate = useNavigate();
  return (
    <Menu
      label={t('user.menu')}
      triggerClassName="avatar-btn"
      trigger={<span className="avatar">{initials(user.name, user.email)}</span>}
      header={
        <div className="user-head">
          <b>{user.name}</b>
          <span className="faint">{user.email}</span>
          {user.is_admin && (
            <span className="chip measure">
              <ShieldCheck aria-hidden /> {t('user.admin')}
            </span>
          )}
        </div>
      }
      items={[
        { id: 'settings', label: t('nav.settings'), icon: <Settings />, onSelect: () => navigate(paths.settings()) },
        {
          id: 'signout',
          label: t('user.signOut'),
          icon: <LogOut />,
          separator: true,
          onSelect: () => {
            void logout().then(() => navigate(paths.login(), { replace: true }));
          },
        },
      ]}
    />
  );
}

function TopBar({ onOpenDrawer }: { onOpenDrawer: () => void }) {
  const { t } = useI18n();
  const { projectSwitcherHidden } = useShell();
  return (
    <header className="topbar">
      <button type="button" className="btn ghost icon only-mobile" onClick={onOpenDrawer} aria-label={t('nav.openMenu')}>
        <MenuIcon />
      </button>
      <Link to={paths.home()} className="topbar-brand" aria-label={`${t('app.name')} · ${t('nav.backToHome')}`} title={t('nav.backToHome')}>
        <Wordmark size={28} />
      </Link>
      <span className={`topbar-div ${projectSwitcherHidden ? 'hide-mobile' : ''}`} aria-hidden />
      {/* R1.2: the current project every area page works on (hidden on a page with its own scope picker) */}
      {!projectSwitcherHidden && (
        <>
          <ProjectSwitcher />
          <span className="topbar-div hide-mobile" aria-hidden />
        </>
      )}
      <span className="hide-mobile topbar-crumbs">
        <Breadcrumbs />
      </span>
      <span className="grow" />
      <Link to={paths.home()} className="btn ghost sm topbar-home hide-mobile">
        <House aria-hidden />
        <span>{t('nav.backToHome')}</span>
      </Link>
      <ServiceStatus />
      {/* [AI workspace] dock toggle (desktop) / full page (phones) */}
      <AssistantTopButton />
      <span className="hide-mobile">
        <LanguageToggle />
      </span>
      <span className="hide-mobile">
        <ThemeToggle />
      </span>
      <TodoBell />
      <UserMenu />
    </header>
  );
}

/** Rail entry; active for the whole area (detail pages included). */
function AreaLink({ area, active, compact }: { area: AreaMeta; active: boolean; compact?: boolean }) {
  const { t } = useI18n();
  const label = t(area.labelKey);
  const short = compact && area.shortKey ? t(area.shortKey) : label;
  return (
    <Link to={area.to} className={`${compact ? 'bb-link' : 'rail-link'} ${active ? 'active' : ''}`} aria-current={active ? 'page' : undefined} title={label}>
      {area.icon}
      {compact ? <span>{short}</span> : <span className="rail-label">{label}</span>}
      {/* [FB approvals] pending-for-me badge */}
    </Link>
  );
}

function Rail() {
  const { t } = useI18n();
  const { railCollapsed, setRailCollapsed } = useShell();
  const current = areaOf(useLocation().pathname);
  return (
    <aside className="rail" aria-label={t('nav.main')}>
      <nav className="rail-nav">
        {AREAS.map((a) => (
          <AreaLink key={a.id} area={a} active={current === a.id} />
        ))}
      </nav>
      <span className="grow" />
      {<nav className="rail-nav" aria-label={t('nav.settings')}>
          <NavLink to={paths.settings()} className={({ isActive }) => `rail-link ${isActive ? 'active' : ''}`} title={t('nav.settings')}>
            <Settings />
            <span className="rail-label">{t('nav.settings')}</span>
          </NavLink>
        </nav>}
      <button type="button" className="rail-link rail-collapse" onClick={() => setRailCollapsed(!railCollapsed)} aria-label={t(railCollapsed ? 'layout.expandNav' : 'layout.collapseNav')} title={t(railCollapsed ? 'layout.expandNav' : 'layout.collapseNav')} aria-expanded={!railCollapsed}>
        {railCollapsed ? <PanelLeftOpen aria-hidden /> : <PanelLeftClose aria-hidden />}
        <span className="rail-label">{t('layout.collapseNav')}</span>
      </button>
    </aside>
  );
}

/** Phones: the four most used areas, the assistant and the menu. */
const BOTTOM_AREAS: AreaMeta['id'][] = ['home', 'parts', 'flows', 'resources'];

function BottomBar({ onOpenDrawer }: { onOpenDrawer: () => void }) {
  const { t } = useI18n();
  const current = areaOf(useLocation().pathname);
  return (
    <nav className="bottombar" aria-label={t('nav.main')}>
      {AREAS.filter((a) => BOTTOM_AREAS.includes(a.id)).map((a) => (
        <AreaLink key={a.id} area={a} active={current === a.id} compact />
      ))}
      {/* [AI workspace] the assistant is a full page on phones */}
      <AssistantBottomLink />
      <button type="button" className="bb-link" onClick={onOpenDrawer}>
        <MenuIcon />
        <span>{t('nav.menu')}</span>
      </button>
    </nav>
  );
}

function MobileDrawer({ onClose }: { onClose: () => void }) {
  const { t } = useI18n();
  const user = useUser();
  const { logout } = useAuth();
  const navigate = useNavigate();
  const go = (to: string) => {
    onClose();
    navigate(to);
  };
  return (
    <Dialog title={t('nav.menu')} onClose={onClose} size="sm">
      <div className="drawer-nav">
        <Link to={paths.home()} className="drawer-link drawer-home" onClick={onClose}>
          <House aria-hidden /> {t('nav.backToHome')}
        </Link>
        {AREAS.map((a) => (
          <button key={a.id} type="button" className="drawer-link" onClick={() => go(a.to)}>
            {a.icon} {t(a.labelKey)}
          </button>
        ))}
        {/* [AI workspace] */}
        <AssistantDrawerLink onGo={go} />
        <button type="button" className="drawer-link" onClick={() => go(paths.projects())}>
          <FolderKanban /> {t('pswitch.all')}
        </button>
        {<button type="button" className="drawer-link" onClick={() => go(paths.settings())}>
            <Settings /> {t('nav.settings')}
          </button>}
        <div className="drawer-section">{t('prefs.language')}</div>
        <LanguageToggle />
        <div className="drawer-section">{t('prefs.theme')}</div>
        <ThemeSegmented />
        <div className="drawer-section">{user.email}</div>
        <button
          type="button"
          className="drawer-link"
          onClick={() => {
            onClose();
            void logout().then(() => navigate(paths.login(), { replace: true }));
          }}
        >
          <LogOut /> {t('user.signOut')}
        </button>
      </div>
    </Dialog>
  );
}

function ShellLayout() {
  const { t } = useI18n();
  const { railCollapsed } = useShell();
  const [drawer, setDrawer] = useState(false);
  // [AI workspace] Ctrl/⌘+J, Esc; the dock is the right column of .app when open.
  useAssistantHotkeys();
  const dock = useDockVisible();
  return (
    <div className={`app ${dock ? 'has-dock' : ''} ${railCollapsed ? 'rail-collapsed' : ''}`}>
      {/* A button, not href="#main": the hash belongs to the router. */}
      <button type="button" className="skip-link" onClick={() => document.getElementById('main')?.focus()}>
        {t('common.skipToContent')}
      </button>
      <TopBar onOpenDrawer={() => setDrawer(true)} />
      <Rail />
      <main className="app-main" id="main" tabIndex={-1}>
        <Outlet />
      </main>
      <AssistantDock />
      <BottomBar onOpenDrawer={() => setDrawer(true)} />
      {drawer && <MobileDrawer onClose={() => setDrawer(false)} />}
    </div>
  );
}

export function AppShell() {
  return (
    <CurrentProjectProvider>
      <ShellProvider>
        <AssistantProvider>
          <TodosProvider><ShellLayout /></TodosProvider>
        </AssistantProvider>
      </ShellProvider>
    </CurrentProjectProvider>
  );
}

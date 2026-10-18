// Shell entries of the assistant (design §7.1): rail button (aria-pressed;
// toggles the dock on desktop, opens /assistant on phones), top-bar button,
// bottom-bar and drawer entries, Ctrl/⌘+J, Esc closes the dock (not while
// typing), and the dock itself as the right column of .app (an overlay below
// 1100 px; not rendered below 768 px, where the assistant is a page).

import { Sparkles } from 'lucide-react';
import { lazy,Suspense,useEffect,useRef,useState } from 'react';
import { NavLink } from 'react-router-dom';
import { paths } from '../../app/nav';
import { Spinner } from '../../components/ui/States';
import { isTypingTarget } from '../../hooks/keyboard';
import { useI18n } from '../../i18n/I18nProvider';
import { useAssistant,useAssistantSession } from './AssistantProvider';
import './assistant.css';

const AssistantPanel = lazy(() => import('./AssistantPanel').then((m) => ({ default: m.AssistantPanel })));

export function useMedia(query: string): boolean {
  const [matches, setMatches] = useState(() => typeof window !== 'undefined' && !!window.matchMedia?.(query).matches);
  useEffect(() => {
    const mq = typeof window !== 'undefined' ? window.matchMedia?.(query) : undefined;
    if (!mq) return;
    const on = () => setMatches(mq.matches);
    on();
    mq.addEventListener('change', on);
    return () => mq.removeEventListener('change', on);
  }, [query]);
  return matches;
}

function isMac(): boolean {
  return typeof navigator !== 'undefined' && /Mac|iPhone|iPad/i.test(navigator.platform || navigator.userAgent);
}

export function shortcutLabel(): string {
  return isMac() ? '⌘J' : 'Ctrl+J';
}

/** The dock is shown: open, no embedded host, not on /assistant, not a phone. */
export function useDockVisible(): boolean {
  const { isOpen, layout } = useAssistant();
  const phone = useMedia('(max-width: 767px)');
  return isOpen && layout === 'dock' && !phone;
}

function modalOpen(): boolean {
  return !!document.querySelector('[role="dialog"][aria-modal="true"], [role="alertdialog"][aria-modal="true"]');
}

/** Ctrl/⌘+J toggles; Esc closes the dock when focus is not in a field and no menu / popover / dialog is open. */
export function useAssistantHotkeys(): void {
  const { toggle, close, layout, isOpen } = useAssistant();
  const { requestFocus } = useAssistantSession();
  const dockVisible = useDockVisible();
  const state = useRef({ toggle, close, layout, isOpen, dockVisible, requestFocus });
  state.current = { toggle, close, layout, isOpen, dockVisible, requestFocus };
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const s = state.current;
      if ((e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === 'j') {
        if (modalOpen()) return;
        e.preventDefault();
        const opening = !(s.layout === 'dock' && s.isOpen);
        s.toggle();
        if (opening) s.requestFocus();
        return;
      }
      if (e.key === 'Escape' && s.dockVisible && !e.defaultPrevented) {
        if (isTypingTarget(e.target) || modalOpen()) return;
        if (document.querySelector('.popover, [role="menu"]')) return;
        s.close();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);
}

/** Right column of the shell. Focus returns to where it was when the dock closes with focus inside. */
export function AssistantDock() {
  const { t } = useI18n();
  const visible = useDockVisible();
  const opener = useRef<HTMLElement | null>(null);
  const wasVisible = useRef(visible);
  const seenVisible = useRef(false);
  // Remember the element that had focus when the dock opened (before the panel moves focus into itself).
  if (visible && !seenVisible.current && typeof document !== 'undefined') opener.current = document.activeElement as HTMLElement | null;
  seenVisible.current = visible;
  useEffect(() => {
    if (!visible && wasVisible.current && (document.activeElement === document.body || !document.activeElement)) {
      const back = opener.current && document.contains(opener.current) ? opener.current : document.getElementById('main');
      back?.focus();
    }
    wasVisible.current = visible;
  }, [visible]);
  if (!visible) return null;
  return (
    <aside className="as-dock" aria-label={t('as.title')}>
      <Suspense
        fallback={
          <div className="as-dock-loading">
            <Spinner label={t('common.loading')} />
          </div>
        }
      >
        <AssistantPanel variant="dock" />
      </Suspense>
    </aside>
  );
}

export function AssistantRailButton() {
  const { t } = useI18n();
  const { isOpen, layout, toggle } = useAssistant();
  const pressed = layout === 'full' || (layout === 'dock' && isOpen);
  return (
    <button type="button" className={`rail-link as-rail-btn ${pressed ? 'active' : ''}`} aria-pressed={pressed} onClick={toggle} title={`${t('as.title')} (${shortcutLabel()})`}>
      <Sparkles />
      <span className="rail-label">{t('as.title')}</span>
      <kbd className="as-kbd rail-label" aria-hidden>
        {shortcutLabel()}
      </kbd>
    </button>
  );
}

export function AssistantTopButton() {
  const { t } = useI18n();
  const { isOpen, layout, toggle } = useAssistant();
  const pressed = layout === 'full' || (layout === 'dock' && isOpen);
  return (
    <button type="button" className={`btn ghost icon as-top-btn ${pressed ? 'active' : ''}`} aria-pressed={pressed} onClick={toggle} aria-label={`${t('as.title')} (${shortcutLabel()})`} title={`${t('as.title')} (${shortcutLabel()})`}>
      <Sparkles />
    </button>
  );
}

export function AssistantBottomLink() {
  const { t } = useI18n();
  const assistant = useAssistant();
  return (
    <NavLink to={paths.assistant(assistant.conversationId ?? undefined)} onClick={(event) => { event.preventDefault(); assistant.open({ full: true }); }} className={({ isActive }) => `bb-link ${isActive ? 'active' : ''}`}>
      <Sparkles />
      <span>{t('as.short')}</span>
    </NavLink>
  );
}

export function AssistantDrawerLink({ onGo }: { onGo: (to: string) => void }) {
  const { t } = useI18n();
  const assistant = useAssistant();
  return (
    <button type="button" className="drawer-link" onClick={() => { assistant.open({ full: true }); onGo(paths.assistant(assistant.conversationId ?? undefined)); }}>
      <Sparkles /> {t('as.title')}
    </button>
  );
}

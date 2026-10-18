import { useEffect, useRef, useState } from 'react';
import { useLocation } from 'react-router-dom';
import { useI18n } from '../../i18n/I18nProvider';
import { focusIds } from './focus';

/** Exact todo links reveal a saved item; they never trigger its decision action. */
export function FocusNotice({ kind, version }: { kind: 'assumption' | 'questions'; version: string }) {
  const { search } = useLocation();
  const { t } = useI18n();
  const anchor = useRef<HTMLDivElement>(null);
  const [missing, setMissing] = useState(false);
  const value = new URLSearchParams(search).get(kind);
  useEffect(() => {
    if (!value) { setMissing(false); return; }
    const ids = new Set(focusIds(value, kind === 'questions'));
    const root = anchor.current?.parentElement;
    if (!root) return;
    const attribute = kind === 'questions' ? 'data-question-id' : 'data-assumption-id';
    let focused: HTMLElement | null = null;
    const scan = () => {
      const target = [...root.querySelectorAll<HTMLElement>(`[${attribute}]`)].find((el) => ids.has(el.getAttribute(attribute) ?? ''));
      setMissing(!target);
      if (!target || focused === target) return;
      focused = target;
      for (let element: HTMLElement | null = target.parentElement; element && element !== root; element = element.parentElement) if (element instanceof HTMLDetailsElement) element.open = true;
      target.scrollIntoView({ block: 'center' });
      target.focus({ preventScroll: true });
    };
    const frame = requestAnimationFrame(scan);
    const observer = new MutationObserver(scan);
    observer.observe(root, { childList: true, subtree: true });
    return () => { cancelAnimationFrame(frame); observer.disconnect(); };
  }, [value, version, kind]);
  return <div ref={anchor} hidden={!value || !missing}>{value && missing && <p className="callout slim" role="status">{t('core.focus.missing')}</p>}</div>;
}

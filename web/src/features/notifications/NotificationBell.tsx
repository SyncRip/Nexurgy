// Notification bell in the top bar (contract §3.6, §4): unread count polled
// every 30 s (paused while the tab is hidden, refreshed when it becomes
// visible), a dropdown with the latest notifications, mark one / all read,
// and links to the request, task or review each one is about.

import './notifications.css';
import { Bell, CheckCheck, ClipboardCheck, ListChecks, Milestone, Send, TriangleAlert, UserCog } from 'lucide-react';
import { useCallback, useEffect, useState, type ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import { isAbortError } from '../../api/client';
import { getUnreadNotificationCount, listNotifications, markNotificationsRead, type AppNotification } from '../../api/notifications';
import { Popover } from '../../components/ui/Popover';
import { ErrorNotice, SkeletonRows } from '../../components/ui/States';
import { useResource } from '../../hooks/useResource';
import { useI18n, type TFunction } from '../../i18n/I18nProvider';
import { onApprovalsChanged, requestKindText } from '../approvals/meta';
import { dyn } from '../stages/meta';

const POLL_MS = 30_000;
const LIST_LIMIT = 30;
const EVENT = 'partroute:notifications-changed';

function notifyNotificationsChanged() {
  try {
    window.dispatchEvent(new Event(EVENT));
  } catch {
    /* non-browser */
  }
}

/** Unread count; null until known (or when the server has no notifications endpoint). */
export function useUnreadNotificationCount(): number | null {
  const [count, setCount] = useState<number | null>(null);
  useEffect(() => {
    let ctrl: AbortController | null = null;
    const load = () => {
      if (document.visibilityState === 'hidden') return;
      ctrl?.abort();
      const c = new AbortController();
      ctrl = c;
      getUnreadNotificationCount({ signal: c.signal })
        .then((n) => {
          if (!c.signal.aborted) setCount(n);
        })
        .catch((e: unknown) => {
          if (!isAbortError(e) && !c.signal.aborted) setCount(null);
        });
    };
    load();
    const h = window.setInterval(load, POLL_MS);
    const onVis = () => document.visibilityState === 'visible' && load();
    document.addEventListener('visibilitychange', onVis);
    window.addEventListener(EVENT, load);
    const off = onApprovalsChanged(load);
    return () => {
      ctrl?.abort();
      window.clearInterval(h);
      document.removeEventListener('visibilitychange', onVis);
      window.removeEventListener(EVENT, load);
      off();
    };
  }, []);
  return count;
}

function KindIcon({ kind }: { kind: string }) {
  if (kind.startsWith('request.reassigned')) return <UserCog aria-hidden />;
  if (kind.startsWith('request.')) return <ClipboardCheck aria-hidden />;
  if (kind.startsWith('task.')) return <ListChecks aria-hidden />;
  if (kind.startsWith('review.')) return <Milestone aria-hidden />;
  if (kind.startsWith('impact.')) return <TriangleAlert aria-hidden />;
  return <Send aria-hidden />;
}

const str = (v: unknown): string => (typeof v === 'string' ? v : v === null || v === undefined ? '' : String(v));

/**
 * Localised title / body from the event kind and payload; the server's
 * English text is the fallback (and the tooltip).
 */
export function notificationText(t: TFunction, lang: string, n: AppNotification): { title: string; body: string } {
  const p = n.payload ?? {};
  const fallback = { title: n.title.replace(/^\[[^\]]*\]\s*/, ''), body: lang === 'en' ? n.body : '' };
  if (!n.payload) return fallback;
  const kind = str(p.request_kind);
  const label = str(p.title) || (kind ? requestKindText(t, kind, str(p.stage) || null, kind === 'engineering' && !p.task_id && !!p.asset_version_id) : '');
  switch (n.kind) {
    case 'request.opened':
      return { title: t('nt.t.requestOpened', { label }), body: '' };
    case 'request.decided':
      return { title: t('nt.t.requestDecided', { label, decision: dyn(t, `ap.status.${str(p.decision)}`, str(p.decision)) }), body: '' };
    case 'request.reassigned':
      return { title: t('nt.t.requestReassigned', { label }), body: '' };
    case 'task.status':
      return {
        title: t('nt.t.taskStatus', {
          task: `${str(p.key)} ${str(p.title)}`.trim(),
          from: dyn(t, `tk.status.${str(p.from)}`, str(p.from)),
          to: dyn(t, `tk.status.${str(p.to)}`, str(p.to)),
        }),
        body: str(p.reason),
      };
    case 'review.decided':
      return { title: t('nt.t.reviewDecided', { name: str(p.milestone_name), decision: dyn(t, `ap.status.${str(p.decision)}`, str(p.decision)) }), body: '' };
    case 'impact.opened':
      return { title: t('nt.t.impactOpened', { reason: dyn(t, `nt.reason.${str(p.reason)}`, str(p.reason)) }), body: n.body };
    default:
      return fallback;
  }
}

/** In-app paths only; anything else is not followed. */
const inAppLink = (link: string | null | undefined): string | null => (link && link.startsWith('/') && !link.startsWith('//') ? link : null);

function NotificationList({ onClose }: { onClose: () => void }) {
  const { t, fmt, lang } = useI18n();
  const navigate = useNavigate();
  const res = useResource((signal) => listNotifications({ limit: LIST_LIMIT }, { signal }), []);
  const [error, setError] = useState<unknown>(null);
  const [busyAll, setBusyAll] = useState(false);
  const rows = res.data ?? [];
  const unread = rows.filter((n) => !n.read_at).length;

  const markLocal = useCallback(
    (ids: string[] | 'all') => {
      const now = new Date().toISOString();
      res.setData((prev) => (prev ?? []).map((n) => (ids === 'all' || ids.includes(n.id) ? { ...n, read_at: n.read_at ?? now } : n)));
    },
    [res],
  );

  const open = async (n: AppNotification) => {
    const link = inAppLink(n.link);
    if (!n.read_at) {
      markLocal([n.id]);
      markNotificationsRead({ ids: [n.id] })
        .then(notifyNotificationsChanged)
        .catch((e: unknown) => setError(e));
    }
    if (link) {
      onClose();
      navigate(link);
    }
  };

  const markAll = async () => {
    setBusyAll(true);
    setError(null);
    try {
      await markNotificationsRead({ all: true });
      markLocal('all');
      notifyNotificationsChanged();
    } catch (e) {
      setError(e);
    } finally {
      setBusyAll(false);
    }
  };

  let body: ReactNode;
  if (res.error && !res.data) body = <ErrorNotice error={res.error} onRetry={res.reload} />;
  else if (!res.data) body = <SkeletonRows rows={3} cols={2} />;
  else if (!rows.length) body = <p className="faint small nt-empty">{t('nt.empty')}</p>;
  else
    body = (
      <ul className="nt-list">
        {rows.map((n) => {
          const link = inAppLink(n.link);
          const text = notificationText(t, lang, n);
          return (
            <li key={n.id} className={n.read_at ? 'read' : 'unread'}>
              <button type="button" className="nt-item" onClick={() => void open(n)} title={n.title} aria-label={`${n.read_at ? '' : `${t('nt.unread')}: `}${text.title}`}>
                <span className="nt-ico">
                  <KindIcon kind={n.kind} />
                </span>
                <span className="nt-text">
                  <span className="nt-title">{text.title}</span>
                  {text.body && <span className="nt-body">{text.body}</span>}
                  <span className="nt-meta">
                    {dyn(t, `nt.kind.${n.kind}`, n.kind)}
                    {n.project?.code ? ` · ${n.project.code}` : ''} · <time title={fmt.dateTime(n.created_at)}>{fmt.relative(n.created_at)}</time>
                    {!link && ` · ${t('nt.noLink')}`}
                  </span>
                </span>
                {!n.read_at && <span className="nt-dot" aria-hidden />}
              </button>
            </li>
          );
        })}
      </ul>
    );

  return (
    <div className="nt-panel">
      <div className="nt-head">
        <h2>{t('nt.title')}</h2>
        <span className="grow" />
        <button type="button" className="btn ghost sm" onClick={() => void markAll()} disabled={busyAll || !unread}>
          <CheckCheck /> {busyAll ? t('common.working') : t('nt.markAll')}
        </button>
      </div>
      {error ? <ErrorNotice error={error} /> : null}
      <div className="nt-scroll">{body}</div>
      <p className="nt-foot faint small">{t('nt.foot')}</p>
    </div>
  );
}

export function NotificationBell() {
  const { t } = useI18n();
  const count = useUnreadNotificationCount();
  const text = count && count > 99 ? '99+' : String(count ?? 0);
  const label = count ? t('nt.bellUnread', { count: text }) : t('nt.bell');
  return (
    <span className="nt-wrap">
      <Popover
        label={label}
        triggerClassName="btn ghost icon nt-bell"
        trigger={
          <>
            <Bell />
            {count ? (
              <span className="nt-badge" aria-hidden>
                {text}
              </span>
            ) : null}
          </>
        }
      >
        {(close) => <NotificationList onClose={close} />}
      </Popover>
    </span>
  );
}

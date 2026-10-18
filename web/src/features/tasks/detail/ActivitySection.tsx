// Activity: a timeline assembled from the records the API exposes (task
// creation, status transitions with reasons from the audit trail, baselines,
// runs, submissions and their closing, impact notices).

import { AlertTriangle, CirclePlus, Layers, PlayCircle, Send, CheckCircle2, OctagonX, Undo2, ArrowRightLeft } from 'lucide-react';
import { useMemo, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { listBaselines, listSubmissions, listTaskActivity, listTaskRuns } from '../../../api/pm';
import type { UUID } from '../../../api/types';
import { ErrorNotice, SkeletonRows } from '../../../components/ui/States';
import { useResource } from '../../../hooks/useResource';
import { useI18n } from '../../../i18n/I18nProvider';
import { RunStatusChip } from '../../workflow/icons';
import { PersonName } from '../common';
import { noticeText } from '../ImpactNotices';
import type { TaskSectionProps } from './shared';

interface Event {
  key: string;
  at: string;
  icon: ReactNode;
  tone: 'ok' | 'warn' | 'err' | 'measure' | 'pending';
  text: ReactNode;
  actor?: UUID | null;
}

export function ActivitySection({ task, members }: TaskSectionProps) {
  const { t, fmt } = useI18n();
  const baselines = useResource((signal) => listBaselines(task.id, { signal }), [task.id, task.version]);
  const subs = useResource((signal) => listSubmissions(task.id, { signal }), [task.id, task.version]);
  const runs = useResource((signal) => listTaskRuns(task.id, { signal }), [task.id, task.version]);
  const audit = useResource((signal) => listTaskActivity(task.id, { signal }), [task.id, task.version]);

  const events = useMemo(() => {
    const out: Event[] = [];
    if (task.created_at) out.push({ key: 'created', at: task.created_at, icon: <CirclePlus />, tone: 'pending', text: t('tk.act.created', { key: task.key }), actor: task.created_by });
    for (const b of baselines.data ?? []) {
      if (!b.adopted_at) continue;
      out.push({
        key: `b-${b.id}`,
        at: b.adopted_at,
        icon: <Layers />,
        tone: 'measure',
        text: (
          <>
            {t('tk.act.baseline', { n: b.seq, slots: b.items.length })}
            {b.reason && <span className="faint"> — {b.reason}</span>}
          </>
        ),
        actor: b.adopted_by,
      });
    }
    for (const r of runs.data ?? []) {
      out.push({
        key: `r-${r.id}`,
        at: r.created_at,
        icon: <PlayCircle />,
        tone: 'pending',
        text: (
          <>
            {t('tk.act.run')} <Link to={`/runs/${r.id}`}>{r.title || t('tk.run.untitled')}</Link> <RunStatusChip status={r.status} />
          </>
        ),
      });
    }
    for (const s of subs.data ?? []) {
      if (s.submitted_at) out.push({ key: `s-${s.id}`, at: s.submitted_at, icon: <Send />, tone: 'warn', text: t('tk.act.submitted', { n: s.seq }), actor: s.submitted_by });
      if (s.decided_at && s.status !== 'pending') {
        const icon = s.status === 'approved' ? <CheckCircle2 /> : s.status === 'rejected' ? <OctagonX /> : <Undo2 />;
        const tone = s.status === 'approved' ? 'ok' : s.status === 'rejected' ? 'err' : 'pending';
        out.push({ key: `sd-${s.id}`, at: s.decided_at, icon, tone, text: t(`tk.act.sub.${s.status}`, { n: s.seq }) });
      }
    }
    for (const a of audit.data ?? []) {
      // Only status transitions; other audit entries are already covered above.
      if (!['task.start', 'task.cancel', 'task.reopen'].includes(a.action)) continue;
      const kind = a.action.slice(5) as 'start' | 'cancel' | 'reopen';
      out.push({
        key: `a-${a.id}`,
        at: a.at,
        icon: <ArrowRightLeft />,
        tone: kind === 'cancel' ? 'err' : 'pending',
        text: (
          <>
            {t(`tk.act.tr.${kind}`)}
            {a.data.reason && <span className="faint"> — {a.data.reason}</span>}
          </>
        ),
        actor: a.user_id,
      });
    }
    for (const n of task.open_impacts) {
      if (n.created_at) out.push({ key: `i-${n.id}`, at: n.created_at, icon: <AlertTriangle />, tone: 'warn', text: noticeText(t, n) });
    }
    return out.sort((a, b) => (a.at < b.at ? 1 : a.at > b.at ? -1 : 0));
  }, [task, baselines.data, runs.data, subs.data, audit.data, t]);

  const err = baselines.error || subs.error || runs.error || audit.error;
  const loading = !baselines.data || !subs.data || !runs.data || !audit.data;

  return (
    <section className="panel tk-panel" aria-label={t('tk.act.title')}>
      <div className="panel-h">
        <h3>{t('tk.act.title')}</h3>
      </div>
      <div className="panel-b stack-sm">
        <p className="faint small">{t('tk.act.note')}</p>
        {err ? (
          <ErrorNotice
            error={err}
            onRetry={() => {
              baselines.reload();
              subs.reload();
              runs.reload();
              audit.reload();
            }}
          />
        ) : null}
        {loading && !err ? (
          <SkeletonRows rows={4} cols={3} />
        ) : (
          <ol className="tk-timeline">
            {events.map((e) => (
              <li key={e.key} className={`tone-${e.tone}`}>
                <span className="tk-tl-ico" aria-hidden>
                  {e.icon}
                </span>
                <div className="grow tk-xs">
                  <span className="small">{e.text}</span>
                  <span className="faint small">
                    <span title={fmt.dateTime(e.at)}>{fmt.dateTime(e.at)}</span>
                    {e.actor && (
                      <>
                        {' · '}
                        <PersonName id={e.actor} members={members} />
                      </>
                    )}
                  </span>
                </div>
              </li>
            ))}
          </ol>
        )}
      </div>
    </section>
  );
}

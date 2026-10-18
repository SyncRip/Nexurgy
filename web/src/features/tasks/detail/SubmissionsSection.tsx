import { useFeature } from '../../../api/capabilities';
// Submission history: every acceptance batch with its frozen items, checks and
// decision. A pending submission can be withdrawn (task back to in progress);
// the reviewer decides on the approval request page.

import { ExternalLink, Inbox, Undo2 } from 'lucide-react';
import { useState } from 'react';
import { Link } from 'react-router-dom';
import { getApprovalRequestDecisions, listSubmissions, withdrawSubmission, type Submission } from '../../../api/pm';
import { paths } from '../../../app/nav';
import { useAuth } from '../../../auth/AuthProvider';
import { ConfirmDialog } from '../../../components/ui/Dialog';
import { EmptyState, ErrorNotice, ErrorState, SkeletonRows } from '../../../components/ui/States';
import { useToast } from '../../../components/ui/Toast';
import { useResource } from '../../../hooks/useResource';
import { useI18n } from '../../../i18n/I18nProvider';
import { CheckReportView } from '../ChecksView';
import { assetTypeLabel, Count, EngineeringChip, PersonName, SubmissionStatusChip, TkError, VersionLabels, type Members } from '../common';
import type { TaskSectionProps } from './shared';

function Decisions({ requestId, members }: { requestId: string; members: Members }) {
  const { t, fmt } = useI18n();
  const res = useResource((signal) => getApprovalRequestDecisions(requestId, { signal }), [requestId]);
  if (res.error && !res.data) return <ErrorNotice error={res.error} onRetry={res.reload} />;
  if (!res.data) return <SkeletonRows rows={1} cols={3} />;
  const r = res.data;
  return (
    <div className="tk-xs">
      <span className="faint small">
        {t('tk.sub.assignee')} {r.assignee ? r.assignee.name : <PersonName id={r.assignee_id} members={members} />}
      </span>
      {r.decisions.length ? (
        <ul className="tk-rows">
          {r.decisions.map((d) => (
            <li key={d.id} className="tk-decision">
              <span className={`strong ${d.decision === 'approved' ? 'ok-text' : 'err-text'}`}>{t(`tk.decision.${d.decision}`)}</span>
              <span className="small">{d.actor_name ?? members.name(d.actor_id)}</span>
              <span className="faint small" title={fmt.dateTime(d.created_at)}>
                {fmt.relative(d.created_at)}
              </span>
              {d.comment && <span className="small tk-quote">{d.comment}</span>}
            </li>
          ))}
        </ul>
      ) : (
        <span className="faint small">{t('tk.sub.noDecision')}</span>
      )}
    </div>
  );
}

function SubmissionCard({ s, members, canWithdraw, onWithdraw }: { s: Submission; members: Members; canWithdraw: boolean; onWithdraw: () => void }) {
  const { t, fmt } = useI18n();
  const [open, setOpen] = useState(s.status === 'pending');
  const snap = s.snapshot;
  const crit = new Map((snap?.criteria ?? []).map((c) => [c.id, `#${c.seq} ${c.description}`]));
  return (
    <li className={`tk-sub st-${s.status}`}>
      <div className="tk-sub-head">
        <span className="mono strong">#{s.seq}</span>
        <SubmissionStatusChip status={s.status} />
        <span className="small">
          <PersonName id={s.submitted_by} members={members} />
        </span>
        <span className="faint small" title={fmt.dateTime(s.submitted_at)}>
          {fmt.relative(s.submitted_at)}
        </span>
        {s.decided_at && (
          <span className="faint small" title={fmt.dateTime(s.decided_at)}>
            · {t('tk.sub.closed', { time: fmt.relative(s.decided_at) })}
          </span>
        )}
        <span className="grow" />
        {s.request_id && (
          <Link to={paths.approvalRequest(s.request_id)} className="btn sm">
            <ExternalLink /> {t('tk.sub.openRequest')}
          </Link>
        )}
        {canWithdraw && s.status === 'pending' && (
          <button type="button" className="btn sm danger" onClick={onWithdraw}>
            <Undo2 /> {t('tk.sub.withdraw')}
          </button>
        )}
      </div>
      {s.note && <p className="small tk-quote">{s.note}</p>}
      <details open={open} onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}>
        <summary className="small">{t('tk.sub.details')}</summary>
        {open && (
          <div className="stack tk-details-body">
            <section className="tk-xs">
              <h4 className="tk-sublabel">{t('tk.sub.frozenItems', { n: snap?.items.length ?? 0 })}</h4>
              {snap?.baseline && <span className="faint small">{t('tk.sub.baselineAt', { n: snap.baseline.seq })}</span>}
              <ul className="tk-dl-list">
                {(snap?.items ?? []).map((it) => (
                  <li key={it.asset_version_id}>
                    <div className="grow tk-xs">
                      <span className="row">
                        <Link to={paths.asset(it.asset_id, it.asset_version_id)} className="mono strong">
                          {it.label}
                        </Link>
                        <span className="faint small">{assetTypeLabel(t, it.asset_type)}</span>
                        <VersionLabels labels={it.labels} />
                        {it.engineering && <EngineeringChip engineering={{ ...it.engineering, decision: null }} />}
                      </span>
                      {it.criterion_ids.length > 0 && (
                        <ul className="tk-inline-list small">
                          {it.criterion_ids.map((c) => (
                            <li key={c} className="tk-crit-tag">
                              {crit.get(c) ?? c.slice(0, 8)}
                            </li>
                          ))}
                        </ul>
                      )}
                      {it.note && <span className="small">{it.note}</span>}
                      <span className="mono faint small" title={it.content_hash}>
                        {t('tk.sub.hash', { h: it.content_hash.slice(0, 12) })}
                      </span>
                    </div>
                  </li>
                ))}
              </ul>
            </section>
            {snap?.checks && <CheckReportView report={snap.checks} title={t('tk.sub.checksAtSubmit')} />}
            <section className="tk-xs">
              <h4 className="tk-sublabel">{t('tk.sub.decision')}</h4>
              {s.request_id ? <Decisions requestId={s.request_id} members={members} /> : <span className="faint small">{t('tk.sub.noRequest')}</span>}
            </section>
            <p className="mono faint small" title={s.snapshot_hash}>
              {t('tk.sub.snapshotHash', { h: s.snapshot_hash.slice(0, 16) })}
            </p>
          </div>
        )}
      </details>
    </li>
  );
}

export function SubmissionsSection({ task, reloadTask, members }: TaskSectionProps) {
  const tasksEnabled = useFeature('tasks');
  const { t } = useI18n();
  const toast = useToast();
  const { user } = useAuth();
  const res = useResource((signal) => listSubmissions(task.id, { signal }), [task.id, task.version]);
  const [withdrawing, setWithdrawing] = useState<Submission | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  let body;
  if (res.error && !res.data) body = <ErrorState error={res.error} onRetry={res.reload} />;
  else if (!res.data) body = <SkeletonRows rows={3} cols={4} />;
  else if (!res.data.length) body = <EmptyState compact icon={<Inbox className="tk-empty-ico" aria-hidden />} title={t('tk.sub.empty')} body={t('tk.sub.emptyBody')} />;
  else
    body = (
      <ul className="tk-subs">
        {res.data.map((s) => (
          <SubmissionCard key={s.id} s={s} members={members} canWithdraw={tasksEnabled && (s.submitted_by === user?.id || task.permissions.edit)} onWithdraw={() => setWithdrawing(s)} />
        ))}
      </ul>
    );

  return (
    <section className="panel tk-panel" aria-label={t('tk.sub.title')}>
      <div className="panel-h">
        <h3>{t('tk.sub.title')}</h3>
        {res.data && <Count n={res.data.length} />}
      </div>
      <div className="panel-b stack-sm">
        {task.pending_requests.some((r) => r.kind === 'task_acceptance' && r.can_decide) && (
          <div className="callout info slim">
            <Inbox aria-hidden />
            <span>{t('tk.sub.youReview')}</span>
          </div>
        )}
        {body}
      </div>
      {withdrawing && (
        <ConfirmDialog
          title={t('tk.sub.withdrawTitle', { n: withdrawing.seq })}
          body={t('tk.sub.withdrawBody')}
          confirmLabel={t('tk.sub.withdraw')}
          danger
          busy={busy}
          error={<TkError error={error} />}
          onConfirm={async () => {
            setBusy(true);
            setError(null);
            try {
              await withdrawSubmission(withdrawing.id);
              setWithdrawing(null);
              toast(t('tk.sub.withdrawn'));
              reloadTask();
            } catch (e) {
              setError(e);
            } finally {
              setBusy(false);
            }
          }}
          onClose={() => {
            setWithdrawing(null);
            setError(null);
          }}
        />
      )}
    </section>
  );
}

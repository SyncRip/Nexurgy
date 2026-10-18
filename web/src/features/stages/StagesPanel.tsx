// Stages panel of the project overview (design §4.1, contract §3.1–§3.2):
// the ordered stages with their state, required-task counts and deliverable
// rule; for the current stage the readiness check (blockers, the tasks whose
// accepted submissions would be frozen) and "Start stage review"; per stage
// the review history. Owners add, edit and delete stages here.

import './stages.css';
import { Check, ExternalLink, History, Milestone as MilestoneIcon, MoreHorizontal, Pencil, Plus, Send, Trash2, Undo2 } from 'lucide-react';
import { useState } from 'react';
import { Link } from 'react-router-dom';
import { isApiError } from '../../api/client';
import { TASK_STATUSES, type Milestone, type Overview, type OverviewReviewBrief, type TaskStatus } from '../../api/pm';
import { deleteMilestone, EXTRA_FIXABLE_BLOCKERS, getMilestoneReadiness, type Readiness } from '../../api/stages';
import { paths } from '../../app/nav';
import type { ProjectCapabilities } from '../../auth/permissions';
import { useUser } from '../../auth/AuthProvider';
import { Chip } from '../../components/ui/Badges';
import { ConfirmDialog } from '../../components/ui/Dialog';
import { Menu } from '../../components/ui/Menu';
import { ErrorNotice, Skeleton } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { assetTypeLabel } from '../assets/meta';
import { notifyApprovalsChanged } from '../approvals/meta';
import { Count, TaskStatusChip, TkError, useMembers } from '../tasks/common';
import { MilestoneDialog } from './MilestoneDialog';
import { BlockerList, MilestoneStatusChip } from './meta';
import { ReviewHistory, WithdrawReviewDialog } from './ReviewHistory';
import { StageReviewDialog } from './StageReviewDialog';

const PLANNABLE = new Set(['draft', 'active', 'on_hold']);
const isTaskStatus = (s: string): s is TaskStatus => (TASK_STATUSES as readonly string[]).includes(s);

function ReadinessBox({ readiness, loading, error, onRetry }: { readiness: Readiness | undefined; loading: boolean; error: unknown; onRetry: () => void }) {
  const { t } = useI18n();
  if (error && !readiness) {
    // Older servers have no readiness endpoint: the review dialog still works, the server guards it.
    if (isApiError(error) && error.isNotFound) return null;
    return <ErrorNotice error={error} onRetry={onRetry} />;
  }
  if (!readiness)
    return (
      <div className="stack-sm" role="status" aria-label={t('common.loading')}>
        <Skeleton w="50%" />
        <Skeleton w="70%" />
      </div>
    );
  return (
    <div className={`stack-sm ${loading ? 'is-refreshing' : ''}`}>
      {readiness.ready ? (
        <p className="small ok-text row">
          <Check aria-hidden className="inline-ico" /> {t('stg.readiness.ready')}
        </p>
      ) : (
        <>
          <span className="small strong">{t('stg.readiness.blocked')}</span>
          <BlockerList blockers={readiness.blockers} />
        </>
      )}
      {readiness.tasks.length > 0 && (
        <ul className="stg-tasks" aria-label={t('stg.readiness.tasks')}>
          {readiness.tasks.map((x, i) => {
            const id = x.task_id ?? x.id;
            const seq = x.submission_seq ?? x.seq;
            return (
              <li key={id ?? i}>
                {id ? (
                  <Link to={paths.task(id)}>
                    <span className="mono faint">{x.key}</span> {x.title}
                  </Link>
                ) : (
                  <span>
                    <span className="mono faint">{x.key}</span> {x.title}
                  </span>
                )}
                {isTaskStatus(x.status) && <TaskStatusChip status={x.status} />}
                <span className="faint">{x.required === false ? t('tk.crit.optional') : t('tk.crit.required')}</span>
                {x.submission_id ? (
                  <span className="ok-text">{typeof seq === 'number' ? t('stg.readiness.acceptedSeq', { seq }) : t('stg.readiness.accepted')}</span>
                ) : (
                  x.required !== false && <span className="warn-text">{t('stg.readiness.noAccepted')}</span>
                )}
                {x.problems && x.problems.length > 0 && <BlockerList blockers={x.problems} tone="warn" />}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

export function StagesPanel({ ov, caps, onChanged }: { ov: Overview; caps: ProjectCapabilities; onChanged: () => void }) {
  const { t, fmt } = useI18n();
  const toast = useToast();
  const me = useUser();
  const members = useMembers(ov.project.id);
  const p = ov.project;
  const archived = p.status === 'archived' || !!p.archived_at;
  const canPlan = caps.manage && !archived && PLANNABLE.has(p.lifecycle);
  const stages = ov.milestones;
  const current = ov.current_milestone ?? p.current_milestone;
  const currentId = stages.find((m) => m.is_current)?.id ?? current?.id ?? null;
  const currentStage = stages.find((m) => m.id === currentId) ?? null;
  const needsReadiness = !!currentStage && (currentStage.status ?? 'open') === 'open';
  // Re-checked on every overview load (task / impact changes do not bump the project version).
  const readiness = useResource((signal) => getMilestoneReadiness(currentId!, { signal }), [currentId, ov], { enabled: needsReadiness });
  // Blockers an extra deliverable can fix do not stop the dialog from opening.
  const hardBlocked = !!readiness.data && readiness.data.blockers.some((b) => !EXTRA_FIXABLE_BLOCKERS.has(b.code));
  const briefs: OverviewReviewBrief[] = Array.isArray(ov.pending_reviews) ? ov.pending_reviews : [];

  const [editing, setEditing] = useState<Milestone | 'new' | null>(null);
  const [deleting, setDeleting] = useState<Milestone | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState<unknown>(null);
  const [reviewing, setReviewing] = useState<Milestone | null>(null);
  const [withdrawing, setWithdrawing] = useState<string | null>(null);
  const [historyOpen, setHistoryOpen] = useState<Set<string>>(new Set());
  const [historyKey, setHistoryKey] = useState(0);

  // The overview reload brings a new `ov`, which also re-checks readiness.
  const changed = () => {
    onChanged();
    setHistoryKey((k) => k + 1);
  };

  return (
    <section className="panel tk-panel" aria-label={t('stg.title')}>
      <div className="panel-h">
        <span className="tk-panel-ico" aria-hidden>
          <MilestoneIcon />
        </span>
        <h3>{t('stg.title')}</h3>
        <Count n={stages.length} />
        <span className="grow" />
        {canPlan && (
          <button type="button" className="btn sm" onClick={() => setEditing('new')}>
            <Plus /> {t('stg.ms.add')}
          </button>
        )}
      </div>
      <div className="panel-b stack-sm">
        {!stages.length ? (
          <p className="faint small tk-empty-line">{p.lifecycle === 'draft' ? t('stg.emptyDraft') : t('stg.empty')}</p>
        ) : (
          <ol className="stg-list">
            {stages.map((m) => {
              const isCurrent = m.id === currentId;
              const status = m.status ?? 'open';
              const types = m.requirements?.required_asset_types ?? [];
              const brief = briefs.find((b) => b.milestone_id === m.id);
              const pendingId = m.pending_review_id ?? brief?.id ?? null;
              const reqTotal = m.tasks.required_total;
              const canReview = isCurrent && status === 'open' && caps.manage && !archived && p.lifecycle === 'active';
              const menu = canPlan
                ? [
                    { id: 'edit', label: t('stg.ms.edit'), icon: <Pencil />, onSelect: () => setEditing(m) },
                    { id: 'delete', label: t('stg.ms.delete'), icon: <Trash2 />, danger: true, separator: true, onSelect: () => setDeleting(m) },
                  ]
                : [];
              return (
                <li key={m.id} className={`stg-item ${isCurrent ? 'current' : ''} ${status}`} aria-current={isCurrent ? 'step' : undefined}>
                  <span className="stg-seq mono" aria-hidden>
                    {status === 'accepted' ? <Check /> : m.seq}
                  </span>
                  <div className="stg-body">
                    <div className="stg-title">
                      <span className="sr-only">{t('stg.seq', { n: m.seq })}</span>
                      <span className="strong">{m.name}</span>
                      {isCurrent && <Chip tone="accent">{t('tk.ms.current')}</Chip>}
                      <MilestoneStatusChip status={status} />
                    </div>
                    {m.description && <span className="faint small">{m.description}</span>}
                    <div className="stg-facts">
                      {typeof reqTotal === 'number' ? (
                        <span className="mono" title={t('stg.requiredHint')}>
                          {t('stg.requiredCount', { done: m.tasks.required_done ?? 0, total: reqTotal })}
                        </span>
                      ) : null}
                      <span className="mono" title={t('tk.ms.countHint')}>
                        {t('tk.ms.count', { done: m.tasks.done, total: m.tasks.total })}
                      </span>
                      {types.length > 0 && <span>{t('stg.needsTypes', { types: types.map((x) => assetTypeLabel(t, x)).join(', ') })}</span>}
                      {m.accepted_at && <span>{t('stg.acceptedAt', { time: fmt.date(m.accepted_at) })}</span>}
                    </div>
                  </div>
                  {menu.length > 0 ? (
                    <span className="row-actions">
                      <Menu label={t('stg.ms.actions', { name: m.name })} trigger={<MoreHorizontal />} items={menu} />
                    </span>
                  ) : (
                    <span />
                  )}

                  {status === 'in_review' && (
                    <div className="stg-detail">
                      <div className="callout warn slim" role="note">
                        <Send aria-hidden />
                        <div className="row grow">
                          <span className="grow">
                            {t('stg.review.pendingLine', { reviewer: brief?.reviewer?.name ?? (brief?.reviewer_id ? members.name(brief.reviewer_id) : '—') })}
                          </span>
                          {brief?.request_id && (
                            <Link to={paths.approvalRequest(brief.request_id)} className="btn sm">
                              <ExternalLink /> {t('stg.review.openRequest')}
                            </Link>
                          )}
                          {pendingId && caps.manage && !archived && (
                            <button type="button" className="btn sm" onClick={() => setWithdrawing(pendingId)}>
                              <Undo2 /> {t('stg.review.withdraw')}
                            </button>
                          )}
                        </div>
                      </div>
                    </div>
                  )}

                  {isCurrent && status === 'open' && (
                    <div className="stg-detail stg-ready" aria-label={t('stg.readiness.title')}>
                      <div className="row">
                        <span className="tk-label grow">{t('stg.readiness.title')}</span>
                        {caps.manage && !archived && (
                          <button
                            type="button"
                            className="btn sm primary"
                            disabled={!canReview || hardBlocked}
                            title={p.lifecycle !== 'active' ? t('stg.review.needsActive') : undefined}
                            onClick={() => setReviewing(m)}
                          >
                            <Send /> {t('stg.review.start')}
                          </button>
                        )}
                      </div>
                      {p.lifecycle !== 'active' && caps.manage && <p className="faint small">{t('stg.review.needsActive')}</p>}
                      <ReadinessBox readiness={readiness.data} loading={readiness.loading} error={readiness.error} onRetry={readiness.reload} />
                    </div>
                  )}

                  <details
                    className="stg-detail stg-history"
                    open={historyOpen.has(m.id)}
                    onToggle={(e) => {
                      const open = e.currentTarget.open;
                      setHistoryOpen((s) => {
                        if (open === s.has(m.id)) return s;
                        const n = new Set(s);
                        if (open) n.add(m.id);
                        else n.delete(m.id);
                        return n;
                      });
                    }}
                  >
                    <summary>
                      <History aria-hidden className="inline-ico" /> {t('stg.review.history')}
                    </summary>
                    {historyOpen.has(m.id) && <ReviewHistory key={historyKey} milestoneId={m.id} members={members} meId={me.id} canManage={caps.manage && !archived} onChanged={changed} />}
                  </details>
                </li>
              );
            })}
          </ol>
        )}
      </div>

      {editing && (
        <MilestoneDialog
          projectId={p.id}
          milestone={editing === 'new' ? null : editing}
          onClose={() => setEditing(null)}
          onReload={() => {
            setEditing(null);
            changed();
          }}
          onSaved={(m, warning) => {
            setEditing(null);
            if (warning) toast(t('stg.ms.ruleNotSaved', { name: m.name }), { tone: 'err' });
            else toast(editing === 'new' ? t('tk.ms.added', { name: m.name }) : t('stg.ms.saved', { name: m.name }));
            changed();
          }}
        />
      )}
      {deleting && (
        <ConfirmDialog
          title={t('stg.ms.deleteTitle', { name: deleting.name })}
          body={t('stg.ms.deleteBody')}
          confirmLabel={t('stg.ms.delete')}
          danger
          busy={deleteBusy}
          error={<TkError error={deleteError} />}
          onConfirm={async () => {
            setDeleteBusy(true);
            setDeleteError(null);
            try {
              await deleteMilestone(deleting.id);
              toast(t('stg.ms.deleted', { name: deleting.name }));
              setDeleting(null);
              changed();
            } catch (e) {
              setDeleteError(e);
            } finally {
              setDeleteBusy(false);
            }
          }}
          onClose={() => {
            setDeleting(null);
            setDeleteError(null);
          }}
        />
      )}
      {reviewing && (
        <StageReviewDialog
          projectId={p.id}
          milestone={reviewing}
          readiness={readiness.data ?? null}
          onClose={() => setReviewing(null)}
          onReload={() => {
            setReviewing(null);
            changed();
          }}
          onCreated={() => {
            setReviewing(null);
            notifyApprovalsChanged();
            toast(t('stg.review.createdToast'));
            changed();
          }}
        />
      )}
      {withdrawing && (
        <WithdrawReviewDialog
          review={{ id: withdrawing }}
          onClose={() => setWithdrawing(null)}
          onDone={() => {
            setWithdrawing(null);
            changed();
          }}
        />
      )}
    </section>
  );
}

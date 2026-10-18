// Deliverables: the task owner picks exact asset versions as deliverable drafts
// and links them to criteria (design §6.3). A dry run of the submission checks
// explains every pass / fail / warning before submitting for acceptance; the
// server re-runs the same checks at submission and at approval.

import { ClipboardCheck, Lock, Package, Pencil, Plus, RefreshCw, Send, Trash2 } from 'lucide-react';
import { useState } from 'react';
import { isApiError } from '../../../api/client';
import { addDeliverable, createSubmission, deleteDeliverable, getTaskChecks, isMissingVersion, type AssetVersion, type CheckReport, type Deliverable, type TaskDetail } from '../../../api/pm';
import { ConfirmDialog, Dialog } from '../../../components/ui/Dialog';
import { Field } from '../../../components/ui/Form';
import { EmptyState, ErrorNotice, SkeletonRows } from '../../../components/ui/States';
import { useToast } from '../../../components/ui/Toast';
import { useResource } from '../../../hooks/useResource';
import { useI18n } from '../../../i18n/I18nProvider';
import { AssetVersionPicker } from '../AssetVersionPicker';
import { CheckReportView } from '../ChecksView';
import { Count, PersonName, TkError, VersionRef, type Members } from '../common';
import type { TaskSectionProps } from './shared';
import { LimitedTextarea } from '../../../components/ui/LimitedText';

function DeliverableDialog({ task, existing, onClose, onSaved }: { task: TaskDetail; existing: Deliverable | null; onClose: () => void; onSaved: () => void }) {
  const { t } = useI18n();
  const [version, setVersion] = useState<AssetVersion | null>(existing && !isMissingVersion(existing.version) ? existing.version : null);
  const [criteria, setCriteria] = useState<Set<string>>(() => new Set(existing?.criterion_ids ?? []));
  const [note, setNote] = useState(existing?.note ?? '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const already = !existing && version ? task.deliverables.find((d) => d.asset_version_id === version.id) : undefined;

  const submit = async () => {
    const vid = existing?.asset_version_id ?? version?.id;
    if (!vid) return;
    setBusy(true);
    setError(null);
    try {
      await addDeliverable(task.id, { asset_version_id: vid, criterion_ids: [...criteria], note: note.trim() });
      onSaved();
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  };

  return (
    <Dialog
      title={existing ? t('tk.dl.editTitle') : t('tk.dl.addTitle')}
      subtitle={t('tk.dl.addHint')}
      onClose={onClose}
      busy={busy}
      size="lg"
      onSubmit={() => void submit()}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={busy || (!existing && !version)}>
            {busy ? t('common.saving') : existing ? t('common.save') : t('tk.dl.add')}
          </button>
        </>
      }
    >
      <div className="stack">
        {existing ? (
          <div className="tk-xs">
            <span className="tk-label">{t('tk.dl.version')}</span>
            <VersionRef v={existing.version} showEngineering />
          </div>
        ) : (
          <AssetVersionPicker projectId={task.project_id} value={version} onChange={setVersion} />
        )}
        {already && <p className="faint small">{t('tk.dl.alreadyDraft')}</p>}
        <fieldset className="tk-fieldset stack-sm">
          <legend className="tk-label">{t('tk.dl.criteria')}</legend>
          {task.criteria.length ? (
            <div className="tk-check-col">
              {task.criteria.map((c) => (
                <label key={c.id} className="check tk-wrap">
                  <input
                    type="checkbox"
                    checked={criteria.has(c.id)}
                    onChange={(e) =>
                      setCriteria((s) => {
                        const n = new Set(s);
                        if (e.target.checked) n.add(c.id);
                        else n.delete(c.id);
                        return n;
                      })
                    }
                  />
                  <span className="mono faint">#{c.seq}</span> {c.description}
                </label>
              ))}
            </div>
          ) : (
            <p className="faint small">{t('tk.dl.noCriteria')}</p>
          )}
        </fieldset>
        <Field label={t('tk.dl.note')} optional hint={t('tk.dl.noteHint')}>
          <LimitedTextarea className="input" rows={2} limit="description" value={note} onChange={(e) => setNote(e.target.value)} />
        </Field>
        <TkError error={error} />
      </div>
    </Dialog>
  );
}

function SubmitDialog({ task, members, onClose, onDone, onReload }: { task: TaskDetail; members: Members; onClose: () => void; onDone: () => void; onReload: () => void }) {
  const { t } = useI18n();
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const failed: CheckReport | null = isApiError(error) && error.code === 'submission_checks_failed' ? (((error.details as { checks?: CheckReport }).checks ?? null) as CheckReport | null) : null;

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await createSubmission(task.id, { note: note.trim(), expected_version: task.version });
      onDone();
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  };

  return (
    <Dialog
      title={t('tk.sub.submitTitle', { key: task.key })}
      subtitle={t('tk.sub.submitHint')}
      onClose={onClose}
      busy={busy}
      size={failed ? 'lg' : 'md'}
      onSubmit={() => void submit()}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={busy || !task.reviewer_id}>
            <Send /> {busy ? t('common.working') : t('tk.sub.submit')}
          </button>
        </>
      }
    >
      <div className="stack">
        <dl className="tk-meta">
          <div>
            <dt>{t('tk.field.reviewer')}</dt>
            <dd>{task.reviewer_id ? <PersonName id={task.reviewer_id} members={members} /> : <span className="err-text">{t('tk.noReviewer')}</span>}</dd>
          </div>
          <div>
            <dt>{t('tk.sub.items')}</dt>
            <dd className="mono">{task.deliverables.length}</dd>
          </div>
          <div>
            <dt>{t('tk.sub.baseline')}</dt>
            <dd className="mono">{task.baseline ? `#${task.baseline.seq}` : t('tk.none')}</dd>
          </div>
        </dl>
        {!task.reviewer_id && <p className="small err-text">{t('tk.sub.needsReviewer')}</p>}
        <p className="faint small">{t('tk.sub.freezeNote')}</p>
        <Field label={t('tk.sub.note')} optional>
          <LimitedTextarea className="input" rows={3} limit="long_note" value={note} onChange={(e) => setNote(e.target.value)} />
        </Field>
        <TkError error={error} onReload={onReload} />
        {failed && <CheckReportView report={failed} title={t('tk.sub.failedChecks')} />}
      </div>
    </Dialog>
  );
}

export function DeliverablesSection({ task, reloadTask, members, projectActive }: TaskSectionProps) {
  const { t, fmt } = useI18n();
  const toast = useToast();
  const checks = useResource((signal) => getTaskChecks(task.id, { signal }), [task.id, task.version, task.deliverables.length]);
  const [dialog, setDialog] = useState<Deliverable | 'new' | null>(null);
  const [removing, setRemoving] = useState<Deliverable | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const editable = task.permissions.participate && projectActive && task.status === 'in_progress';
  const critText = new Map(task.criteria.map((c) => [c.id, `#${c.seq} ${c.description}`]));

  const statusHint =
    task.status === 'todo'
      ? t('tk.dl.startFirst')
      : task.status === 'in_review'
        ? t('tk.dl.frozenReview')
        : task.status === 'done'
          ? t('tk.dl.frozenDone')
          : task.status === 'cancelled'
            ? t('tk.dl.cancelled')
            : !projectActive
              ? t('tk.dl.projectInactive')
              : !task.permissions.participate
                ? t('tk.dl.notParticipant')
                : null;

  return (
    <div className="stack">
      <section className="panel tk-panel" aria-label={t('tk.dl.title')}>
        <div className="panel-h">
          <Package className="tk-panel-ico" aria-hidden />
          <h3>{t('tk.dl.title')}</h3>
          <Count n={task.deliverables.length} />
          <span className="grow" />
          {editable && (
            <button type="button" className="btn sm" onClick={() => setDialog('new')}>
              <Plus /> {t('tk.dl.add')}
            </button>
          )}
        </div>
        <div className="panel-b stack-sm">
          {statusHint && (
            <div className="callout slim pending">
              <Lock aria-hidden />
              <span>{statusHint}</span>
            </div>
          )}
          {!task.deliverables.length ? (
            <EmptyState compact title={t('tk.dl.empty')} body={t('tk.dl.emptyBody')} />
          ) : (
            <ul className="tk-dl-list">
              {task.deliverables.map((d) => (
                <li key={d.id}>
                  <div className="grow tk-xs">
                    <VersionRef v={d.version} showEngineering />
                    {d.criterion_ids.length ? (
                      <ul className="tk-inline-list small">
                        {d.criterion_ids.map((cid) => (
                          <li key={cid} className="tk-crit-tag">
                            {critText.get(cid) ?? t('tk.dl.removedCriterion')}
                          </li>
                        ))}
                      </ul>
                    ) : (
                      <span className="faint small">{t('tk.dl.noLinks')}</span>
                    )}
                    {d.note && <span className="small">{d.note}</span>}
                    <span className="faint small">
                      {t('tk.dl.selectedBy')} <PersonName id={d.selected_by} members={members} /> · <span title={fmt.dateTime(d.selected_at)}>{fmt.relative(d.selected_at)}</span>
                    </span>
                  </div>
                  {editable && (
                    <span className="row-actions">
                      <button type="button" className="btn ghost icon sm" onClick={() => setDialog(d)} aria-label={t('tk.dl.editLinks')}>
                        <Pencil />
                      </button>
                      <button type="button" className="btn ghost icon sm" onClick={() => setRemoving(d)} aria-label={t('tk.dl.remove')}>
                        <Trash2 />
                      </button>
                    </span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      </section>

      <section className="panel tk-panel" aria-label={t('tk.dl.checks')}>
        <div className="panel-h">
          <ClipboardCheck className="tk-panel-ico" aria-hidden />
          <h3>{t('tk.dl.checks')}</h3>
          <span className="grow" />
          <button type="button" className="btn sm" onClick={checks.reload} disabled={checks.loading}>
            <RefreshCw /> {t('tk.dl.recheck')}
          </button>
        </div>
        <div className="panel-b stack-sm">
          <p className="faint small">{t('tk.dl.checksHint')}</p>
          {checks.error && !checks.data ? (
            <ErrorNotice error={checks.error} onRetry={checks.reload} />
          ) : !checks.data ? (
            <SkeletonRows rows={3} cols={2} />
          ) : (
            <div className={checks.loading ? 'is-refreshing' : ''}>
              <CheckReportView report={checks.data} />
            </div>
          )}
        </div>
      </section>

      {task.status === 'in_progress' && task.permissions.participate && (
        <section className="panel tk-panel tk-submit-panel" aria-label={t('tk.sub.submit')}>
          <div className="panel-b row">
            <div className="grow tk-xs">
              <span className="strong">{t('tk.sub.readyTitle')}</span>
              <span className="faint small">
                {task.reviewer_id ? (
                  <>
                    {t('tk.sub.reviewerIs')} <PersonName id={task.reviewer_id} members={members} />
                  </>
                ) : (
                  <span className="err-text">{t('tk.sub.needsReviewer')}</span>
                )}
              </span>
            </div>
            <button type="button" className="btn primary" onClick={() => setSubmitting(true)} disabled={!projectActive || !task.deliverables.length || !task.reviewer_id}>
              <Send /> {t('tk.sub.submit')}
            </button>
          </div>
        </section>
      )}

      {dialog && (
        <DeliverableDialog
          task={task}
          existing={dialog === 'new' ? null : dialog}
          onClose={() => setDialog(null)}
          onSaved={() => {
            setDialog(null);
            toast(t('tk.dl.saved'));
            reloadTask();
            checks.reload();
          }}
        />
      )}
      {removing && (
        <ConfirmDialog
          title={t('tk.dl.removeTitle')}
          body={t('tk.dl.removeBody')}
          confirmLabel={t('common.remove')}
          danger
          busy={busy}
          error={<TkError error={error} />}
          onConfirm={async () => {
            setBusy(true);
            setError(null);
            try {
              await deleteDeliverable(removing.id);
              setRemoving(null);
              toast(t('tk.dl.removed'));
              reloadTask();
              checks.reload();
            } catch (e) {
              setError(e);
            } finally {
              setBusy(false);
            }
          }}
          onClose={() => {
            setRemoving(null);
            setError(null);
          }}
        />
      )}
      {submitting && (
        <SubmitDialog
          task={task}
          members={members}
          onClose={() => setSubmitting(false)}
          onReload={() => {
            setSubmitting(false);
            reloadTask();
          }}
          onDone={() => {
            setSubmitting(false);
            toast(t('tk.sub.submitted'));
            reloadTask();
          }}
        />
      )}
    </div>
  );
}

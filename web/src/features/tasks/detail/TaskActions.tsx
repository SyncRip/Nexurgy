// Task transitions (start / cancel / reopen). in_review and done are only
// reached through submissions and acceptance, never from here. Cancelling a
// task with active runs asks whether to cancel them or let them finish
// (409 active_runs from the server is handled the same way).

import { Ban, Play, RotateCcw } from 'lucide-react';
import { useState } from 'react';
import { Link } from 'react-router-dom';
import { isApiError } from '../../../api/client';
import { transitionTask, type RunBrief, type TaskDetail } from '../../../api/pm';
import { Dialog } from '../../../components/ui/Dialog';
import { Field } from '../../../components/ui/Form';
import { useToast } from '../../../components/ui/Toast';
import { useI18n } from '../../../i18n/I18nProvider';
import { RunStatusChip } from '../../workflow/icons';
import { ReasonDialog, TkError } from '../common';
import { LimitedTextarea } from '../../../components/ui/LimitedText';

function CancelTaskDialog({ task, onClose, onDone, onReload }: { task: TaskDetail; onClose: () => void; onDone: (t: TaskDetail) => void; onReload: () => void }) {
  const { t } = useI18n();
  const [reason, setReason] = useState('');
  const [runs, setRuns] = useState<RunBrief[] | null>(null);
  const [choice, setChoice] = useState<'' | 'cancel' | 'finish'>('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const needsChoice = task.active_run_count > 0 || (runs?.length ?? 0) > 0;

  const submit = async () => {
    if (needsChoice && !choice) return;
    setBusy(true);
    setError(null);
    try {
      const out = await transitionTask(task.id, {
        action: 'cancel',
        reason: reason.trim(),
        expected_version: task.version,
        cancel_runs: needsChoice ? choice === 'cancel' : null,
      });
      onDone(out);
    } catch (e) {
      if (isApiError(e) && e.code === 'active_runs') {
        setRuns(((e.details as { runs?: RunBrief[] }).runs ?? []) as RunBrief[]);
        setError(null);
      } else setError(e);
      setBusy(false);
    }
  };

  return (
    <Dialog
      title={t('tk.tr.cancel.title', { key: task.key })}
      onClose={onClose}
      busy={busy}
      onSubmit={() => void submit()}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn danger-solid" disabled={busy || (needsChoice && !choice)}>
            {busy ? t('common.working') : t('tk.tr.cancel')}
          </button>
        </>
      }
    >
      <div className="stack-sm">
        <p className="muted">{t('tk.tr.cancel.body')}</p>
        <Field label={t('tk.reason')} optional>
          <LimitedTextarea className="input" rows={2} limit="description" value={reason} onChange={(e) => setReason(e.target.value)} />
        </Field>
        {needsChoice && (
          <fieldset className="tk-fieldset stack-sm">
            <legend className="tk-label">{t('tk.tr.cancel.runsTitle', { n: runs?.length ?? task.active_run_count })}</legend>
            {runs && runs.length > 0 && (
              <ul className="tk-rows">
                {runs.map((r) => (
                  <li key={r.id}>
                    <Link to={`/runs/${r.id}`} className="tk-row-link">
                      <RunStatusChip status={r.status} />
                      <span className="grow ellip">{r.title || t('tk.run.untitled')}</span>
                    </Link>
                  </li>
                ))}
              </ul>
            )}
            <label className="check tk-wrap">
              <input type="radio" name="tk-cancel-runs" checked={choice === 'cancel'} onChange={() => setChoice('cancel')} />
              {t('tk.tr.cancel.cancelRuns')}
            </label>
            <label className="check tk-wrap">
              <input type="radio" name="tk-cancel-runs" checked={choice === 'finish'} onChange={() => setChoice('finish')} />
              {t('tk.tr.cancel.letFinish')}
            </label>
          </fieldset>
        )}
        <TkError error={error} onReload={onReload} />
      </div>
    </Dialog>
  );
}

export function TaskActions({ task, setTask, reloadTask, disabled }: { task: TaskDetail; setTask: (t: TaskDetail) => void; reloadTask: () => void; disabled: boolean }) {
  const { t } = useI18n();
  const toast = useToast();
  const [open, setOpen] = useState<'start' | 'cancel' | 'reopen' | null>(null);
  if (!task.permissions.edit || disabled) return null;
  const done = (out: TaskDetail, key: 'tk.tr.started' | 'tk.tr.cancelled' | 'tk.tr.reopened') => {
    setOpen(null);
    setTask(out);
    const n = out.transition?.cancelled_runs?.length ?? 0;
    const affected = out.transition?.impacts?.length ?? 0;
    if (n) toast(t('tk.tr.cancelledRuns', { n }));
    else if (affected) toast(t('tk.tr.reopenedImpacts', { key: task.key, n: affected }));
    else toast(t(key, { key: task.key }));
  };
  return (
    <>
      {task.status === 'todo' && (
        <button type="button" className="btn primary sm" onClick={() => setOpen('start')}>
          <Play /> {t('tk.tr.start')}
        </button>
      )}
      {task.status === 'done' && (
        <button type="button" className="btn sm" onClick={() => setOpen('reopen')}>
          <RotateCcw /> {t('tk.tr.reopen')}
        </button>
      )}
      {(task.status === 'todo' || task.status === 'in_progress') && (
        <button type="button" className="btn sm danger" onClick={() => setOpen('cancel')}>
          <Ban /> {t('tk.tr.cancel')}
        </button>
      )}
      {task.status === 'in_review' && <span className="faint small">{t('tk.tr.inReviewHint')}</span>}
      {open === 'start' && (
        <ReasonDialog
          title={t('tk.tr.start.title', { key: task.key })}
          body={t('tk.tr.start.body')}
          confirmLabel={t('tk.tr.start')}
          onConfirm={async () => done(await transitionTask(task.id, { action: 'start', expected_version: task.version }), 'tk.tr.started')}
          onClose={() => setOpen(null)}
          onReload={() => {
            setOpen(null);
            reloadTask();
          }}
        />
      )}
      {open === 'reopen' && (
        <ReasonDialog
          title={t('tk.tr.reopen.title', { key: task.key })}
          body={t('tk.tr.reopen.body')}
          reasonLabel={t('tk.reason')}
          reasonRequired
          confirmLabel={t('tk.tr.reopen')}
          onConfirm={async (reason) => done(await transitionTask(task.id, { action: 'reopen', reason, expected_version: task.version }), 'tk.tr.reopened')}
          onClose={() => setOpen(null)}
          onReload={() => {
            setOpen(null);
            reloadTask();
          }}
        />
      )}
      {open === 'cancel' && (
        <CancelTaskDialog
          task={task}
          onClose={() => setOpen(null)}
          onDone={(out) => done(out, 'tk.tr.cancelled')}
          onReload={() => {
            setOpen(null);
            reloadTask();
          }}
        />
      )}
    </>
  );
}


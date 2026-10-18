// Project completion and reopen (design §4.1, contract §3.3).
// - Complete: shows the completion guard before asking (all stages accepted,
//   no open tasks, active runs, pending requests / stage reviews or open impact
//   notices) and every blocker of a 409 project_not_completable.
// - Reopen: needs a reason and, when the project has stages, the stage to
//   resume from; accepted stages from there on go back to open.

import { CheckCircle2, CircleCheck, RotateCcw, TriangleAlert } from 'lucide-react';
import { useState } from 'react';
import { isApiError } from '../../api/client';
import { projectLifecycle, type Overview, type ProjectFull } from '../../api/pm';
import { normalizeBlockers } from '../../api/stages';
import { Dialog } from '../../components/ui/Dialog';
import { Field } from '../../components/ui/Form';
import { useI18n } from '../../i18n/I18nProvider';
import { TkError } from '../tasks/common';
import { BlockerList, dyn } from './meta';
import { LimitedTextarea } from '../../components/ui/LimitedText';

/** Error details of project_not_completable without the non-blocker fields. */
function completionBlockersOf(error: unknown): unknown {
  if (!isApiError(error) || error.code !== 'project_not_completable') return undefined;
  const d = error.details as Record<string, unknown>;
  if (d.blockers !== undefined) return d.blockers;
  const rest = Object.fromEntries(Object.entries(d).filter(([k]) => k !== 'lifecycle' && k !== 'current_version'));
  return rest;
}

export function CompleteProjectDialog({ ov, onClose, onDone, onReload }: { ov: Overview; onClose: () => void; onDone: (p: ProjectFull) => void; onReload: () => void }) {
  const { t } = useI18n();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const pre = ov.completion;
  const preBlockers = pre ? normalizeBlockers(pre.blockers) : [];
  const refused = completionBlockersOf(error);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      onDone(await projectLifecycle(ov.project.id, { action: 'complete', reason: '', expected_version: ov.project.version }));
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  };

  return (
    <Dialog
      title={t('tk.lc.complete.title')}
      onClose={onClose}
      busy={busy}
      onSubmit={() => void submit()}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={busy}>
            <CheckCircle2 /> {busy ? t('common.working') : t('tk.lc.complete')}
          </button>
        </>
      }
    >
      <div className="stack-sm">
        <p className="muted">{t('stg.complete.body')}</p>
        {pre && !refused && (pre.ready && !preBlockers.length ? (
          <div className="callout ok slim" role="note">
            <CircleCheck aria-hidden />
            <span>{t('stg.complete.ready')}</span>
          </div>
        ) : (
          <div className="callout warn" role="note">
            <TriangleAlert aria-hidden />
            <div className="stack-sm grow">
              <span>{t('stg.complete.notReady')}</span>
              <BlockerList blockers={pre.blockers} prefix="stg.completion" tone="warn" />
            </div>
          </div>
        ))}
        {refused !== undefined ? (
          <div className="callout err" role="alert">
            <TriangleAlert aria-hidden />
            <div className="stack-sm grow">
              <span>{t('stg.complete.refused')}</span>
              <BlockerList blockers={refused} prefix="stg.completion" />
              <code className="error-code">{t('common.errorCode', { code: 'project_not_completable' })}</code>
            </div>
          </div>
        ) : (
          <TkError error={error} onReload={onReload} />
        )}
      </div>
    </Dialog>
  );
}

export function ReopenProjectDialog({ ov, onClose, onDone, onReload }: { ov: Overview; onClose: () => void; onDone: (p: ProjectFull) => void; onReload: () => void }) {
  const { t } = useI18n();
  // A project can resume at any stage up to the first one not yet accepted (the server refuses later ones).
  const firstOpen = ov.milestones.find((m) => m.status !== 'accepted');
  const stages = firstOpen ? ov.milestones.filter((m) => m.seq <= firstOpen.seq) : ov.milestones;
  const current = ov.current_milestone ?? ov.project.current_milestone;
  const [stageId, setStageId] = useState<string>(current?.id ?? stages[stages.length - 1]?.id ?? '');
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [touched, setTouched] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const reasonErr = !reason.trim() ? t('validation.required', { field: t('tk.reason') }) : null;
  const stageErr = stages.length && !stageId ? t('stg.reopen.stageRequired') : null;
  const chosen = stages.find((m) => m.id === stageId);
  const reverted = chosen ? stages.filter((m) => m.seq >= chosen.seq && m.status === 'accepted') : [];

  const submit = async () => {
    setTouched(true);
    if (reasonErr || stageErr) return;
    setBusy(true);
    setError(null);
    try {
      onDone(
        await projectLifecycle(ov.project.id, {
          action: 'reopen',
          reason: reason.trim(),
          expected_version: ov.project.version,
          milestone_id: stages.length ? stageId : undefined,
        }),
      );
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  };

  return (
    <Dialog
      title={t('tk.lc.reopen.title')}
      onClose={onClose}
      busy={busy}
      onSubmit={() => void submit()}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={busy}>
            <RotateCcw /> {busy ? t('common.working') : t('tk.lc.reopen')}
          </button>
        </>
      }
    >
      <div className="stack-sm">
        <p className="muted">{t('tk.lc.reopen.body')}</p>
        {stages.length > 0 && (
          <Field label={t('stg.reopen.stage')} hint={t('stg.reopen.stageHint')} error={touched ? stageErr : null}>
            <select className="select" value={stageId} onChange={(e) => setStageId(e.target.value)}>
              {!stageId && <option value="">{t('stg.reopen.chooseStage')}</option>}
              {stages.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.seq}. {m.name} · {dyn(t, `stg.status.${m.status ?? 'open'}`, m.status ?? 'open')}
                </option>
              ))}
            </select>
          </Field>
        )}
        {reverted.length > 0 && (
          <div className="callout warn slim" role="note">
            <TriangleAlert aria-hidden />
            <span>{t('stg.reopen.reverts', { names: reverted.map((m) => m.name).join(', ') })}</span>
          </div>
        )}
        <Field label={t('tk.reason')} error={touched ? reasonErr : null}>
          <LimitedTextarea className="input" rows={3} limit="description" value={reason} onChange={(e) => setReason(e.target.value)} />
        </Field>
        <TkError error={error} onReload={onReload} />
      </div>
    </Dialog>
  );
}

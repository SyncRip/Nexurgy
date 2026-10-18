// Impact notices (design §6.5): a baseline change or a revoked approval is
// shown as a notice to handle, never as a silent status rollback. Resolving
// keeps a note.

import { AlertTriangle, CheckCheck, Info } from 'lucide-react';
import { useState } from 'react';
import { Link } from 'react-router-dom';
import { resolveImpactNotice, type CheckProblem, type ImpactNotice } from '../../api/pm';
import type { UUID } from '../../api/types';
import { paths } from '../../app/nav';
import { Chip } from '../../components/ui/Badges';
import { Dialog } from '../../components/ui/Dialog';
import { Field } from '../../components/ui/Form';
import { useToast } from '../../components/ui/Toast';
import { useI18n, type TFunction } from '../../i18n/I18nProvider';
import { problemText } from './ChecksView';
import { dynT, TkError } from './common';
import { LimitedTextarea } from '../../components/ui/LimitedText';

function slotName(t: TFunction, slot: string): string {
  return dynT(t, `tk.slot.${slot}`, slot);
}

/** One-line explanation of a notice from its reason code and details. */
export function noticeText(t: TFunction, n: ImpactNotice): string {
  const d = n.details ?? {};
  if (n.reason === 'baseline_changed_after_done') {
    const slots = Object.keys((d.changed_slots as Record<string, unknown> | undefined) ?? {});
    return t('tk.impact.baseline_changed_after_done', { slots: slots.map((s) => slotName(t, s)).join(', ') || '—' });
  }
  if (n.reason === 'deliverable_inconsistent' && typeof d.code === 'string') {
    return t('tk.impact.deliverable_inconsistent', { detail: problemText(t, d as unknown as CheckProblem) });
  }
  if (n.reason === 'deliverable_not_in_baseline') {
    return t('tk.impact.deliverable_not_in_baseline', { slot: slotName(t, String(d.slot ?? '')) });
  }
  return dynT(t, `tk.impact.${n.reason}`, n.reason);
}

function ResolveDialog({ notice, onClose, onDone }: { notice: ImpactNotice; onClose: () => void; onDone: (n: ImpactNotice) => void }) {
  const { t } = useI18n();
  const [note, setNote] = useState('');
  const [status, setStatus] = useState<'resolved' | 'dismissed'>('resolved');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      onDone(await resolveImpactNotice(notice.id, { note: note.trim(), status }));
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  };
  return (
    <Dialog
      title={t('tk.impact.resolveTitle')}
      subtitle={noticeText(t, notice)}
      onClose={onClose}
      busy={busy}
      size="sm"
      onSubmit={() => void submit()}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={busy}>
            {busy ? t('common.working') : status === 'resolved' ? t('tk.impact.markResolved') : t('tk.impact.dismiss')}
          </button>
        </>
      }
    >
      <div className="stack-sm">
        <fieldset className="tk-radio-row">
          <legend className="sr-only">{t('tk.impact.outcome')}</legend>
          <label className="check">
            <input type="radio" name="tk-impact-status" checked={status === 'resolved'} onChange={() => setStatus('resolved')} />
            {t('tk.impact.outcome.resolved')}
          </label>
          <label className="check">
            <input type="radio" name="tk-impact-status" checked={status === 'dismissed'} onChange={() => setStatus('dismissed')} />
            {t('tk.impact.outcome.dismissed')}
          </label>
        </fieldset>
        <Field label={t('tk.impact.note')} optional hint={t('tk.impact.noteHint')}>
          <LimitedTextarea className="input" rows={3} limit="description" value={note} onChange={(e) => setNote(e.target.value)} />
        </Field>
        <TkError error={error} />
      </div>
    </Dialog>
  );
}

export function ImpactNoticeList({
  notices,
  canResolve,
  showTaskLink,
  taskLabel,
  onChanged,
}: {
  notices: ImpactNotice[];
  canResolve: boolean;
  showTaskLink?: boolean;
  /** Task key / title for the link (when known). */
  taskLabel?: (taskId: UUID) => string | undefined;
  onChanged: () => void;
}) {
  const { t, fmt } = useI18n();
  const toast = useToast();
  const [resolving, setResolving] = useState<ImpactNotice | null>(null);
  return (
    <>
      <ul className="tk-notice-list">
        {notices.map((n) => (
          <li key={n.id} className={`tk-notice ${n.severity === 'info' ? 'info' : 'warn'}`}>
            {n.severity === 'info' ? <Info aria-hidden /> : <AlertTriangle aria-hidden />}
            <div className="grow tk-xs">
              <span>{noticeText(t, n)}</span>
              <span className="row faint small">
                <Chip tone={n.severity === 'info' ? 'measure' : 'warn'}>{dynT(t, `tk.severity.${n.severity}`, n.severity)}</Chip>
                <span title={fmt.dateTime(n.created_at)}>{fmt.relative(n.created_at)}</span>
                {showTaskLink && n.task_id && <Link to={paths.task(n.task_id)}>{taskLabel?.(n.task_id) ?? t('tk.impact.openTask')}</Link>}
                {n.status !== 'open' && n.resolution_note && <span>· {n.resolution_note}</span>}
              </span>
            </div>
            {canResolve && n.status === 'open' && (
              <button type="button" className="btn sm" onClick={() => setResolving(n)}>
                <CheckCheck /> {t('tk.impact.resolve')}
              </button>
            )}
          </li>
        ))}
      </ul>
      {resolving && (
        <ResolveDialog
          notice={resolving}
          onClose={() => setResolving(null)}
          onDone={() => {
            setResolving(null);
            toast(t('tk.impact.resolvedToast'));
            onChanged();
          }}
        />
      )}
    </>
  );
}

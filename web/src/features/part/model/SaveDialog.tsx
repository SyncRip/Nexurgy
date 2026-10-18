// "Save revision" dialog: a note, then POST /parts/{id}/revisions through the
// editor adapter. 422 invalid_ops / build_failed / op_id_reused are explained.

import { Save } from 'lucide-react';
import { useState } from 'react';
import { isApiError } from '../../../api/client';
import type { ModelOp, ModelRevision } from '../../../api/types';
import { Dialog } from '../../../components/ui/Dialog';
import { Field } from '../../../components/ui/Form';
import { ErrorNotice } from '../../../components/ui/States';
import { useI18n } from '../../../i18n/I18nProvider';
import { opIdFromLoc } from '../../editor/ops';

export function SaveErrorDetails({ error, ops }: { error: unknown; ops: ModelOp[] }) {
  const { t } = useI18n();
  if (!isApiError(error)) return null;
  const d = error.details;
  if (error.code === 'build_failed') {
    const errs = (d.errors as { op_id: string | null; code: string; message: string }[] | undefined) ?? [];
    return (
      <ul className="ws-errlist">
        {errs.map((e, i) => (
          <li key={i}>
            <span className="mono strong">{e.op_id ?? '—'}</span> <code className="error-code">{e.code}</code> {e.message}
          </li>
        ))}
      </ul>
    );
  }
  if (error.code === 'invalid_ops') {
    const errs = (d.errors as { loc: string; msg: string }[] | undefined) ?? [];
    return (
      <ul className="ws-errlist">
        {errs.map((e, i) => (
          <li key={i}>
            <span className="mono strong">{opIdFromLoc(e.loc, ops) ?? e.loc}</span> {e.msg}
          </li>
        ))}
      </ul>
    );
  }
  if (error.code === 'op_id_reused') {
    return <p className="small">{t('ws.save.reused', { id: String(d.op_id ?? '?'), next: String(d.next ?? '?') })}</p>;
  }
  return null;
}

export function SaveDialog({
  parentLabel,
  ops,
  onSave,
  onClose,
}: {
  parentLabel: string | null;
  ops: ModelOp[];
  onSave: (note: string) => Promise<ModelRevision>;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await onSave(note.trim());
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  };
  return (
    <Dialog
      title={t('ws.save.title')}
      subtitle={parentLabel ? t('ws.save.subParent', { rev: parentLabel }) : t('ws.save.subBlank')}
      size="sm"
      busy={busy}
      onClose={onClose}
      onSubmit={() => void submit()}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={busy}>
            <Save /> {busy ? t('ws.save.building') : t('ws.save.do')}
          </button>
        </>
      }
    >
      <div className="stack-sm">
        <Field label={t('ws.save.note')} optional hint={t('ws.save.noteHint')}>
          <textarea className="input" rows={3} maxLength={2000} value={note} onChange={(e) => setNote(e.target.value)} data-autofocus />
        </Field>
        <p className="faint small">{t('ws.save.explain', { n: ops.length })}</p>
        {error ? (
          <>
            <ErrorNotice error={error} />
            <SaveErrorDetails error={error} ops={ops} />
          </>
        ) : null}
      </div>
    </Dialog>
  );
}

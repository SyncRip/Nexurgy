// Edit the project plan: objective and project owner (owner only; PATCH with
// the overview's version, a 409 offers a reload). Used by the project strip
// and the stages tab.

import { useState } from 'react';
import { patchProjectPlan, type Overview } from '../../api/pm';
import { Dialog } from '../../components/ui/Dialog';
import { Field } from '../../components/ui/Form';
import { useI18n } from '../../i18n/I18nProvider';
import { TkError, useMembers } from './common';
import { LimitedTextarea } from '../../components/ui/LimitedText';

export function EditPlanDialog({ ov, onClose, onSaved, onReload }: { ov: Overview; onClose: () => void; onSaved: () => void; onReload: () => void }) {
  const { t } = useI18n();
  const members = useMembers(ov.project.id);
  const [objective, setObjective] = useState(ov.project.objective);
  const [ownerId, setOwnerId] = useState(ov.project.owner?.id ?? '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const body: { objective?: string; owner_id?: string; expected_version: number } = { expected_version: ov.project.version };
      if (objective.trim() !== ov.project.objective) body.objective = objective.trim();
      if (ownerId && ownerId !== (ov.project.owner?.id ?? '')) body.owner_id = ownerId;
      await patchProjectPlan(ov.project.id, body);
      onSaved();
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  };
  return (
    <Dialog
      title={t('tk.ov.editPlan')}
      onClose={onClose}
      busy={busy}
      onSubmit={() => void submit()}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={busy}>
            {busy ? t('common.saving') : t('common.save')}
          </button>
        </>
      }
    >
      <div className="form-grid one">
        <Field label={t('tk.ov.objective')} hint={t('tk.ov.objectiveHint')}>
          <LimitedTextarea className="input" rows={5} limit="objective" value={objective} onChange={(e) => setObjective(e.target.value)} />
        </Field>
        <Field label={t('tk.ov.owner')} hint={t('tk.ov.ownerHint')}>
          <select className="select" value={ownerId} onChange={(e) => setOwnerId(e.target.value)} disabled={!members.list}>
            {!ownerId && <option value="">{t('tk.unassigned')}</option>}
            {(members.list ?? []).map((m) => (
              <option key={m.id} value={m.id}>
                {m.name} · {t(`role.${m.role}`)}
              </option>
            ))}
          </select>
        </Field>
      </div>
      <TkError error={error ?? members.error} onReload={onReload} />
    </Dialog>
  );
}

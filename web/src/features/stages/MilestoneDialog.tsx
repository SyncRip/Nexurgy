// Create or edit a stage: name, description and the stage deliverable rule
// (required asset types). The rule cannot change on an accepted stage —
// reopen the project at that stage first.

import '../tasks/tasks.css';
import { Lock } from 'lucide-react';
import { useState } from 'react';
import { ASSET_TYPES } from '../../api/assets';
import { createMilestone, type Milestone } from '../../api/pm';
import { patchMilestone, type MilestonePatch } from '../../api/stages';
import type { UUID } from '../../api/types';
import { Dialog } from '../../components/ui/Dialog';
import { Field } from '../../components/ui/Form';
import { useI18n } from '../../i18n/I18nProvider';
import { AssetTypeIcon, assetTypeLabel } from '../assets/meta';
import { TkError } from '../tasks/common';
import { LimitedTextarea } from '../../components/ui/LimitedText';

/** Server cap: Requirements.required_asset_types max_length (services/stages.py). */
const MAX_REQUIRED_TYPES = 7;

export function RequiredTypeToggles({ value, onChange, disabled }: { value: string[]; onChange: (v: string[]) => void; disabled?: boolean }) {
  const { t } = useI18n();
  const full = value.length >= MAX_REQUIRED_TYPES;
  return (
    <div className="tk-type-toggles" role="group" aria-label={t('stg.ms.requiredTypes')}>
      {ASSET_TYPES.map((x) => {
        const on = value.includes(x);
        return (
          <button
            key={x}
            type="button"
            className={`tk-toggle ${on ? 'on' : ''}`}
            aria-pressed={on}
            disabled={disabled || (full && !on)}
            title={full && !on ? t('as.typeLimit', { n: MAX_REQUIRED_TYPES }) : undefined}
            onClick={() => onChange(on ? value.filter((v) => v !== x) : [...value, x])}
          >
            <AssetTypeIcon type={x} />
            {assetTypeLabel(t, x)}
          </button>
        );
      })}
    </div>
  );
}

const sameSet = (a: string[], b: string[]) => a.length === b.length && a.every((x) => b.includes(x));

export function MilestoneDialog({
  projectId,
  milestone,
  onClose,
  onSaved,
  onReload,
}: {
  projectId: UUID;
  /** null = create a new stage at the end of the list. */
  milestone: Milestone | null;
  onClose: () => void;
  /** warning = the stage was created but its rule could not be saved. */
  onSaved: (m: Milestone, warning?: unknown) => void;
  onReload: () => void;
}) {
  const { t } = useI18n();
  const accepted = milestone?.status === 'accepted';
  const initialTypes = milestone?.requirements?.required_asset_types ?? [];
  const [name, setName] = useState(milestone?.name ?? '');
  const [description, setDescription] = useState(milestone?.description ?? '');
  const [types, setTypes] = useState<string[]>(initialTypes);
  const [busy, setBusy] = useState(false);
  const [touched, setTouched] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const nameErr = !name.trim() ? t('validation.required', { field: t('tk.ms.name') }) : null;

  const submit = async () => {
    setTouched(true);
    if (nameErr) return;
    setBusy(true);
    setError(null);
    try {
      if (!milestone) {
        const m = await createMilestone(projectId, { name: name.trim(), description: description.trim(), ...(types.length ? { requirements: { required_asset_types: types } } : {}) });
        // Older servers ignore requirements on create: set them with a PATCH when they did not stick.
        const saved = m.requirements?.required_asset_types ?? [];
        if (types.length && !sameSet(types, saved)) {
          try {
            onSaved(await patchMilestone(m.id, { requirements: { required_asset_types: types }, expected_version: m.version ?? 1 }));
          } catch (e) {
            onSaved(m, e);
          }
          return;
        }
        onSaved(m);
        return;
      }
      const body: MilestonePatch = { expected_version: milestone.version ?? 1 };
      if (name.trim() !== milestone.name) body.name = name.trim();
      if (description.trim() !== milestone.description) body.description = description.trim();
      if (!accepted && !sameSet(types, initialTypes)) body.requirements = { required_asset_types: types };
      if (Object.keys(body).length === 1) {
        onClose();
        return;
      }
      onSaved(await patchMilestone(milestone.id, body));
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  };

  return (
    <Dialog
      title={milestone ? t('stg.ms.editTitle', { name: milestone.name }) : t('stg.ms.add')}
      subtitle={milestone ? undefined : t('stg.ms.addHint')}
      onClose={onClose}
      busy={busy}
      onSubmit={() => void submit()}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={busy}>
            {busy ? t('common.saving') : milestone ? t('common.save') : t('common.create')}
          </button>
        </>
      }
    >
      <div className="form-grid one">
        <Field label={t('tk.ms.name')} error={touched ? nameErr : null}>
          <input className="input" value={name} maxLength={200} onChange={(e) => setName(e.target.value)} data-autofocus />
        </Field>
        <Field label={t('tk.ms.description')} optional>
          <LimitedTextarea className="input" rows={3} limit="description" value={description} onChange={(e) => setDescription(e.target.value)} />
        </Field>
        <div className="field">
          <span>{t('stg.ms.requiredTypes')}</span>
          <RequiredTypeToggles value={types} onChange={setTypes} disabled={accepted || busy} />
          {accepted ? (
            <span className="field-hint row">
              <Lock aria-hidden className="inline-ico" /> {t('stg.ms.requirementsLocked')}
            </span>
          ) : (
            <span className="field-hint">{t('stg.ms.requiredTypesHint')}</span>
          )}
        </div>
      </div>
      <TkError error={error} onReload={onReload} />
    </Dialog>
  );
}

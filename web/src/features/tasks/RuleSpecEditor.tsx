// Acceptance criterion editing: the closed rule set (services/tasks.RuleSpec)
// and the criterion dialog. Rules are explainable conditions only — asset
// types, count, engineering approval, estimated / simulated policy and
// consistency with a baseline slot; anything else is a manual judgement.

import { useState } from 'react';
import {
  addCriterion,
  APPROVAL_STAGES_PM,
  ASSET_TYPES,
  DEFAULT_RULE_SPEC,
  patchCriterion,
  type AssetType,
  type Criterion,
  type CriterionInput,
  type PmApprovalStage,
  type RuleSpec,
} from '../../api/pm';
import type { UUID } from '../../api/types';
import { Dialog } from '../../components/ui/Dialog';
import { Field } from '../../components/ui/Form';
import { useI18n, type TFunction } from '../../i18n/I18nProvider';
import { AssetTypeIcon, assetTypeLabel, dynT, TkError } from './common';
import { LimitedTextarea } from '../../components/ui/LimitedText';

/** Named baseline slots offered by default (free names matching the server pattern are allowed too). */
export const SLOT_PRESETS: { slot: string; type: AssetType }[] = [
  { slot: 'target_model', type: 'part_model' },
  { slot: 'reference_drawing', type: 'drawing' },
  { slot: 'process_plan', type: 'process_plan' },
  { slot: 'quote', type: 'quote' },
  { slot: 'input_intake', type: 'intake' },
  { slot: 'reference_document', type: 'document' },
];
export const SLOT_RE = /^[a-z][a-z0-9_:.-]{0,79}$/;

export function normalizeSpec(spec: Partial<RuleSpec> | null | undefined, allowed: AssetType[] = []): RuleSpec {
  const s = { ...DEFAULT_RULE_SPEC, ...(spec ?? {}) };
  const types = Array.from(new Set([...(s.asset_types ?? []), ...allowed]));
  return { ...s, asset_types: types, min_count: typeof s.min_count === 'number' ? s.min_count : 1 };
}

/** Server cap: RuleSpec.asset_types max_length (services/tasks.py). */
const MAX_RULE_TYPES = 6;

export function AssetTypeToggles({ value, onChange, label }: { value: AssetType[]; onChange: (v: AssetType[]) => void; label: string }) {
  const { t } = useI18n();
  const full = value.length >= MAX_RULE_TYPES;
  return (
    <div className="tk-type-toggles" role="group" aria-label={label}>
      {ASSET_TYPES.map((x) => {
        const on = value.includes(x);
        return (
          <button
            key={x}
            type="button"
            className={`tk-toggle ${on ? 'on' : ''}`}
            aria-pressed={on}
            disabled={full && !on}
            title={full && !on ? t('as.typeLimit', { n: MAX_RULE_TYPES }) : undefined}
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

export function RuleSpecEditor({ value, onChange, slots }: { value: RuleSpec; onChange: (v: RuleSpec) => void; slots: string[] }) {
  const { t } = useI18n();
  const set = <K extends keyof RuleSpec>(k: K, v: RuleSpec[K]) => onChange({ ...value, [k]: v });
  const slotBad = !!value.consistent_with_slot && !SLOT_RE.test(value.consistent_with_slot);
  const slotOptions = Array.from(new Set([...SLOT_PRESETS.map((s) => s.slot), ...slots]));
  return (
    <div className="stack-sm tk-rule">
      <Field label={t('tk.rule.assetTypes')} hint={value.asset_types.length ? undefined : t('tk.rule.anyType')}>
        <AssetTypeToggles value={value.asset_types} onChange={(v) => set('asset_types', v)} label={t('tk.rule.assetTypes')} />
      </Field>
      <div className="form-grid">
        <Field label={t('tk.rule.minCount')} hint={t('tk.rule.minCountHint')}>
          <input
            className="input"
            type="number"
            min={0}
            max={50}
            step={1}
            value={value.min_count}
            onChange={(e) => {
              const n = Number.parseInt(e.target.value, 10);
              set('min_count', Number.isFinite(n) ? Math.min(50, Math.max(0, n)) : 0);
            }}
          />
        </Field>
        <Field label={t('tk.rule.approval')} hint={t('tk.rule.approvalHint')}>
          <select className="select" value={value.require_approval_stage ?? ''} onChange={(e) => set('require_approval_stage', (e.target.value || null) as PmApprovalStage | null)}>
            <option value="">{t('tk.rule.noApproval')}</option>
            {APPROVAL_STAGES_PM.map((s) => (
              <option key={s} value={s}>
                {dynT(t, `tk.stage.${s}`, s)}
              </option>
            ))}
          </select>
        </Field>
        <Field label={t('tk.rule.consistentSlot')} hint={t('tk.rule.consistentSlotHint')} error={slotBad ? t('tk.slotInvalid') : null}>
          <input
            className="input mono"
            list="tk-slot-options"
            value={value.consistent_with_slot ?? ''}
            placeholder={t('tk.rule.noSlot')}
            maxLength={80}
            onChange={(e) => set('consistent_with_slot', e.target.value.trim() || null)}
          />
        </Field>
        <datalist id="tk-slot-options">
          {slotOptions.map((s) => (
            <option key={s} value={s}>
              {dynT(t, `tk.slot.${s}`, s)}
            </option>
          ))}
        </datalist>
        <div className="stack-sm tk-rule-flags">
          <label className="check">
            <input type="checkbox" checked={value.allow_estimated} onChange={(e) => set('allow_estimated', e.target.checked)} />
            {t('tk.rule.allowEstimated')}
          </label>
          <label className="check">
            <input type="checkbox" checked={value.allow_simulated} onChange={(e) => set('allow_simulated', e.target.checked)} />
            {t('tk.rule.allowSimulated')}
          </label>
          <label className="check">
            <input type="checkbox" checked={value.allow_unverified} onChange={(e) => set('allow_unverified', e.target.checked)} />
            {t('tk.rule.allowUnverified')}
          </label>
        </div>
      </div>
    </div>
  );
}

/** Short, translated summary of a criterion's rules (list rows). */
export function ruleSummary(t: TFunction, c: Pick<Criterion, 'rule_spec' | 'allowed_types'>): string[] {
  const s = normalizeSpec(c.rule_spec, c.allowed_types);
  const out: string[] = [];
  out.push(s.asset_types.length ? s.asset_types.map((x) => assetTypeLabel(t, x)).join(' / ') : t('tk.rule.anyType'));
  if (s.min_count !== 1) out.push(t('tk.rule.atLeast', { n: s.min_count }));
  if (s.require_approval_stage) out.push(t('tk.rule.needsApproval', { stage: dynT(t, `tk.stage.${s.require_approval_stage}`, s.require_approval_stage) }));
  if (s.consistent_with_slot) out.push(t('tk.rule.matchesSlot', { slot: dynT(t, `tk.slot.${s.consistent_with_slot}`, s.consistent_with_slot) }));
  out.push(s.allow_estimated ? t('tk.rule.estimatedOk') : t('tk.rule.noEstimated'));
  out.push(s.allow_simulated ? t('tk.rule.simulatedOk') : t('tk.rule.noSimulated'));
  out.push(s.allow_unverified ? t('tk.rule.unverifiedOk') : t('tk.rule.noUnverified'));
  return out;
}

export function CriterionDialog({
  taskId,
  criterion,
  slots,
  onClose,
  onSaved,
}: {
  taskId: UUID;
  /** null = new criterion. */
  criterion: Criterion | null;
  slots: string[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const { t } = useI18n();
  const [description, setDescription] = useState(criterion?.description ?? '');
  const [required, setRequired] = useState(criterion?.required ?? true);
  const [manual, setManual] = useState(criterion?.manual_required ?? false);
  const [spec, setSpec] = useState<RuleSpec>(() => normalizeSpec(criterion?.rule_spec, criterion?.allowed_types));
  const [busy, setBusy] = useState(false);
  const [touched, setTouched] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const descErr = !description.trim() ? t('validation.required', { field: t('tk.crit.description') }) : null;
  const slotBad = !!spec.consistent_with_slot && !SLOT_RE.test(spec.consistent_with_slot);

  const submit = async () => {
    setTouched(true);
    if (descErr || slotBad) return;
    setBusy(true);
    setError(null);
    const body: CriterionInput = { description: description.trim(), required, manual_required: manual, allowed_types: spec.asset_types, rule_spec: spec };
    try {
      if (criterion) await patchCriterion(criterion.id, { ...body, expected_version: criterion.version });
      else await addCriterion(taskId, body);
      onSaved();
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  };

  return (
    <Dialog
      title={criterion ? t('tk.crit.edit') : t('tk.crit.add')}
      subtitle={t('tk.crit.dialogHint')}
      onClose={onClose}
      busy={busy}
      size="lg"
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
      <div className="stack">
        <Field label={t('tk.crit.description')} error={touched ? descErr : null}>
          <LimitedTextarea className="input" rows={2} limit="description" value={description} onChange={(e) => setDescription(e.target.value)} />
        </Field>
        <div className="row">
          <label className="check">
            <input type="checkbox" checked={required} onChange={(e) => setRequired(e.target.checked)} />
            {t('tk.crit.requiredLabel')}
          </label>
          <label className="check">
            <input type="checkbox" checked={manual} onChange={(e) => setManual(e.target.checked)} />
            {t('tk.crit.manualLabel')}
          </label>
        </div>
        <RuleSpecEditor value={spec} onChange={setSpec} slots={slots} />
        <TkError error={error} />
      </div>
    </Dialog>
  );
}

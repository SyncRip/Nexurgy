// "Save as process module" (design §6.1, §9.3; contract §3.5): distil a
// concrete process plan into a documented, reusable module. Steps are
// prefilled from the plan's operations; applicability (materials, size range,
// equipment) and the validation scope must be stated explicitly — a concrete
// plan is never just renamed into a general module. The source plan is not
// changed. Also used for a new version of an existing module.

import './modules.css';
import '../tasks/tasks.css';
import { ArrowDown, ArrowUp, Blocks, Info, Plus, Trash2, TriangleAlert, Wand2 } from 'lucide-react';
import { useMemo, useRef, useState } from 'react';
import { isApiError } from '../../api/client';
import {
  createModuleVersion,
  createProcessModule,
  createdModuleVersion,
  type AssetVersion,
  type ProcessModuleContent,
  type ProcessModuleInput,
} from '../../api/assets';
import type { ProcessPlan, UUID } from '../../api/types';
import { Dialog } from '../../components/ui/Dialog';
import { Field, TagInput } from '../../components/ui/Form';
import { ErrorNotice } from '../../components/ui/States';
import { useI18n, type TFunction } from '../../i18n/I18nProvider';
import { LabelCallouts } from '../assets/meta';
import { dyn } from '../stages/meta';
import { LimitedTextarea } from '../../components/ui/LimitedText';

interface StepDraft {
  key: number;
  name: string;
  machine: string;
  description: string;
  minutes: string;
  /** Index of the source plan operation this step was copied from. */
  source: number | null;
  /** Operation id of the source plan (kept across module versions). */
  opId: string | null;
}

interface ParamDraft {
  key: number;
  name: string;
  unit: string;
  def: string;
  min: string;
  max: string;
}

type Xyz = [string, string, string];

/** Text → number; null when empty or not a number (0 is a valid value). */
function parseNum(s: string): number | null {
  const v = s.trim().replace(',', '.');
  if (!v) return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

const numText = (n: number | null | undefined) => (typeof n === 'number' && Number.isFinite(n) ? String(Math.round(n * 1000) / 1000) : '');
const xyzOf = (v: number[] | null | undefined): Xyz => [numText(v?.[0]), numText(v?.[1]), numText(v?.[2])];

/** Field path from module_incomplete details.missing → readable name. */
export function missingText(t: TFunction, key: string): string {
  return dyn(t, `mod.missing.${key.replace(/\[\d+\]/g, '')}`, key);
}

export function ModuleDialog({
  projectId,
  source,
  base,
  onClose,
  onCreated,
}: {
  projectId: UUID;
  /** Distil from this plan (steps prefilled). */
  source?: { plan: ProcessPlan; label?: string; labels?: string[] } | null;
  /** New version of an existing module (form prefilled from its latest content). */
  base?: { assetId: UUID; module: ProcessModuleContent; labels?: string[] } | null;
  onClose: () => void;
  /** The registered version when the server returned it, else null (reload). */
  onCreated: (v: AssetVersion | null) => void;
}) {
  const { t, lang, fmt } = useI18n();
  const plan = source?.plan ?? null;
  const ops = plan?.plan.operations ?? [];
  const opName = (i: number) => {
    const op = ops[i];
    return op ? (lang === 'zh' ? op.label_zh || op.label_en : op.label_en || op.label_zh) || op.op_id : '';
  };
  const keyRef = useRef(0);
  const nextKey = () => ++keyRef.current;

  const [name, setName] = useState(base?.module.name ?? '');
  const [summary, setSummary] = useState(base?.module.summary ?? '');
  const [materials, setMaterials] = useState<string[]>(base?.module.applicability?.materials ?? []);
  const [sizeMax, setSizeMax] = useState<Xyz>(xyzOf(base?.module.applicability?.size_range_mm?.max));
  const [sizeMin, setSizeMin] = useState<Xyz>(xyzOf(base?.module.applicability?.size_range_mm?.min ?? null));
  const [equipment, setEquipment] = useState<string[]>(base?.module.applicability?.equipment ?? []);
  const [prerequisites, setPrerequisites] = useState(base?.module.applicability?.prerequisites ?? '');
  const [limitations, setLimitations] = useState(base?.module.applicability?.limitations ?? '');
  const [params, setParams] = useState<ParamDraft[]>(() =>
    (base?.module.parameters ?? []).map((p) => ({ key: nextKey(), name: p.name, unit: p.unit ?? '', def: p.default === null || p.default === undefined ? '' : String(p.default), min: numText(p.min), max: numText(p.max) })),
  );
  const [steps, setSteps] = useState<StepDraft[]>(() =>
    base
      ? (base.module.steps ?? []).map((s) => ({ key: nextKey(), name: s.name, machine: s.machine ?? '', description: s.description ?? '', minutes: numText(s.minutes), source: null, opId: s.source_op_id ?? null }))
      : ops.map((op, i) => ({
          key: nextKey(),
          name: opName(i),
          machine: op.machine_id ?? op.subcontract_id ?? '',
          description: t('mod.form.stepFromPlan', { op: op.op_id, setup: fmt.number(op.setup_min, { maximumFractionDigits: 1 }), cycle: fmt.number(op.cycle_min, { maximumFractionDigits: 2 }) }),
          minutes: numText(op.cycle_min),
          source: i,
          opId: op.op_id,
        })),
  );
  const [scope, setScope] = useState(base?.module.validation?.scope ?? '');
  const [evidence, setEvidence] = useState(base?.module.validation?.evidence ?? '');
  const baseValidated = base?.module.validation?.validated_plan_ids ?? [];
  const [validatedOnSource, setValidatedOnSource] = useState(false);
  const [busy, setBusy] = useState(false);
  const [touched, setTouched] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const planMaterial = plan ? (lang === 'zh' ? plan.plan.material_name_zh || plan.plan.material_name_en : plan.plan.material_name_en || plan.plan.material_name_zh) || plan.plan.material_input : '';
  const planBbox = plan?.summary?.bbox_mm ?? null;
  const stepMachines = useMemo(() => [...new Set(steps.map((s) => s.machine.trim()).filter(Boolean))], [steps]);

  // client-side mirror of module_incomplete (the server stays the guard)
  const maxNums = sizeMax.map(parseNum);
  const minFilled = sizeMin.some((x) => x.trim());
  const minNums = sizeMin.map(parseNum);
  const errs = {
    name: !name.trim() ? t('validation.required', { field: t('mod.form.name') }) : null,
    materials: !materials.length ? t('mod.form.materialsRequired') : null,
    sizeMax: maxNums.some((n) => n === null || n <= 0) ? t('mod.form.sizeMaxRequired') : null,
    sizeMin:
      minFilled && (minNums.some((n) => n === null || n < 0) || minNums.some((n, i) => n !== null && maxNums[i] !== null && n > (maxNums[i] as number)))
        ? t('mod.form.sizeMinInvalid')
        : null,
    equipment: !equipment.length ? t('mod.form.equipmentRequired') : null,
    steps: !steps.length ? t('mod.form.stepsRequired') : steps.some((s) => !s.name.trim()) ? t('mod.form.stepNameRequired') : steps.some((s) => s.minutes.trim() && ((parseNum(s.minutes) ?? -1) < 0)) ? t('mod.form.numberInvalid') : null,
    params: params.some((p) => !p.name.trim()) ? t('mod.form.paramNameRequired') : params.some((p) => [p.min, p.max].some((x) => x.trim() && parseNum(x) === null)) ? t('mod.form.numberInvalid') : null,
    scope: !scope.trim() ? t('mod.form.scopeRequired') : null,
  };
  const invalid = Object.values(errs).some(Boolean);
  const show = (k: keyof typeof errs) => (touched ? errs[k] : null);

  const body = (): ProcessModuleInput => {
    const validated = new Set(baseValidated);
    if (plan && validatedOnSource) validated.add(plan.id);
    return {
      name: name.trim(),
      summary: summary.trim(),
      applicability: {
        materials,
        size_range_mm: { max: maxNums as number[], min: minFilled ? (minNums as number[]) : null },
        equipment,
        prerequisites: prerequisites.trim(),
        limitations: limitations.trim(),
      },
      parameters: params.map((p) => {
        const d = parseNum(p.def);
        return { name: p.name.trim(), unit: p.unit.trim(), default: p.def.trim() ? (d ?? p.def.trim()) : null, min: parseNum(p.min), max: parseNum(p.max) };
      }),
      steps: steps.map((s) => ({ name: s.name.trim(), machine: s.machine.trim(), description: s.description.trim(), minutes: parseNum(s.minutes), source_op_id: s.opId })),
      validation: { scope: scope.trim(), evidence: evidence.trim(), validated_plan_ids: [...validated] },
      ...(plan
        ? { source_plan_id: plan.id, source_steps: steps.map((s) => s.source).filter((x): x is number => x !== null) }
        : base?.module.source_plan_id
          ? { source_plan_id: base.module.source_plan_id, source_steps: base.module.source_steps ?? [] }
          : {}),
    };
  };

  const submit = async () => {
    setTouched(true);
    if (invalid) return;
    setBusy(true);
    setError(null);
    try {
      const res = base ? await createModuleVersion(base.assetId, body()) : await createProcessModule(projectId, body());
      onCreated(createdModuleVersion(res));
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  };

  const updStep = (key: number, patch: Partial<StepDraft>) => setSteps((xs) => xs.map((s) => (s.key === key ? { ...s, ...patch } : s)));
  const updParam = (key: number, patch: Partial<ParamDraft>) => setParams((xs) => xs.map((p) => (p.key === key ? { ...p, ...patch } : p)));
  const moveStep = (i: number, d: -1 | 1) =>
    setSteps((xs) => {
      const j = i + d;
      if (j < 0 || j >= xs.length) return xs;
      const n = [...xs];
      [n[i], n[j]] = [n[j], n[i]];
      return n;
    });
  const newKey = nextKey;
  const incomplete = isApiError(error) && error.code === 'module_incomplete' ? (error.details as { missing?: string[]; invalid?: string[] }) : null;
  const missing = incomplete ? [...(incomplete.missing ?? []), ...(incomplete.invalid ?? []).map((k) => `invalid:${k}`)] : null;
  const labels = source?.labels ?? base?.labels ?? (plan?.plan.estimated ? ['estimated'] : []);

  return (
    <Dialog
      title={base ? t('mod.form.newVersionTitle', { name: base.module.name }) : source ? t('mod.form.fromPlanTitle', { label: source.label ?? '' }) : t('mod.form.newTitle')}
      subtitle={t('mod.form.subtitle')}
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
            <Blocks /> {busy ? t('common.working') : base ? t('mod.form.submitVersion') : t('mod.form.submit')}
          </button>
        </>
      }
    >
      <div className="stack">
        <div className="callout info slim" role="note">
          <Info aria-hidden />
          <span>{plan ? t('mod.form.sourceNote') : t('mod.form.docNote')}</span>
        </div>
        {labels.length > 0 && (
          <>
            <LabelCallouts labels={labels} />
            <p className="faint small">{t('mod.form.labelsInherit')}</p>
          </>
        )}

        <div className="form-grid">
          <Field label={t('mod.form.name')} error={show('name')} className="span-2">
            <input className="input" value={name} maxLength={200} onChange={(e) => setName(e.target.value)} data-autofocus />
          </Field>
          <Field label={t('mod.form.summary')} optional className="span-2">
            <LimitedTextarea className="input" rows={2} limit="description" value={summary} onChange={(e) => setSummary(e.target.value)} />
          </Field>
        </div>

        <fieldset className="mod-section">
          <legend>{t('mod.applicability')}</legend>
          <Field label={t('mod.materials')} hint={t('mod.form.tagHint')} error={show('materials')}>
            <TagInput value={materials} onChange={setMaterials} placeholder={t('mod.form.materialsPlaceholder')} />
          </Field>
          {planMaterial && !materials.includes(planMaterial) && (
            <div className="mod-quickfill">
              <button type="button" className="btn sm" onClick={() => setMaterials((m) => [...m, planMaterial])}>
                <Wand2 /> {t('mod.form.usePlanMaterial', { material: planMaterial })}
              </button>
            </div>
          )}
          <div className="field">
            <span>{t('mod.sizeMax')}</span>
            <div className="mod-xyz" role="group" aria-label={t('mod.sizeMax')}>
              {(['x', 'y', 'z'] as const).map((ax, i) => (
                <input
                  key={ax}
                  className="input mono"
                  inputMode="decimal"
                  value={sizeMax[i]}
                  placeholder={ax.toUpperCase()}
                  aria-label={t('mod.form.axis', { axis: ax.toUpperCase(), what: t('mod.sizeMax') })}
                  aria-invalid={show('sizeMax') ? true : undefined}
                  onChange={(e) => setSizeMax((v) => v.map((x, k) => (k === i ? e.target.value : x)) as Xyz)}
                />
              ))}
            </div>
            {show('sizeMax') ? <span className="field-error">{errs.sizeMax}</span> : <span className="field-hint">{t('mod.form.sizeHint')}</span>}
            {planBbox && (
              <div className="mod-quickfill">
                <button type="button" className="btn sm" onClick={() => setSizeMax(xyzOf(planBbox))}>
                  <Wand2 /> {t('mod.form.usePlanSize', { size: planBbox.map((x) => fmt.number(x, { maximumFractionDigits: 1 })).join(' × ') })}
                </button>
              </div>
            )}
          </div>
          <div className="field">
            <span>
              {t('mod.sizeMin')} <span className="opt">· {t('common.optional')}</span>
            </span>
            <div className="mod-xyz" role="group" aria-label={t('mod.sizeMin')}>
              {(['x', 'y', 'z'] as const).map((ax, i) => (
                <input
                  key={ax}
                  className="input mono"
                  inputMode="decimal"
                  value={sizeMin[i]}
                  placeholder={ax.toUpperCase()}
                  aria-label={t('mod.form.axis', { axis: ax.toUpperCase(), what: t('mod.sizeMin') })}
                  aria-invalid={show('sizeMin') ? true : undefined}
                  onChange={(e) => setSizeMin((v) => v.map((x, k) => (k === i ? e.target.value : x)) as Xyz)}
                />
              ))}
            </div>
            {show('sizeMin') && <span className="field-error">{errs.sizeMin}</span>}
          </div>
          <Field label={t('mod.equipment')} hint={t('mod.form.tagHint')} error={show('equipment')}>
            <TagInput value={equipment} onChange={setEquipment} placeholder={t('mod.form.equipmentPlaceholder')} />
          </Field>
          {stepMachines.some((m) => !equipment.includes(m)) && (
            <div className="mod-quickfill">
              <button type="button" className="btn sm" onClick={() => setEquipment((e) => [...new Set([...e, ...stepMachines])])}>
                <Wand2 /> {t('mod.form.useStepMachines', { list: stepMachines.join(', ') })}
              </button>
            </div>
          )}
          <Field label={t('mod.prerequisites')} optional>
            <LimitedTextarea className="input" rows={2} limit="description" value={prerequisites} onChange={(e) => setPrerequisites(e.target.value)} />
          </Field>
          <Field label={t('mod.limitations')} optional>
            <LimitedTextarea className="input" rows={2} limit="description" value={limitations} onChange={(e) => setLimitations(e.target.value)} />
          </Field>
        </fieldset>

        <fieldset className="mod-section">
          <legend>{t('mod.steps')}</legend>
          {plan && <p className="faint small">{t('mod.form.stepsFromPlan', { n: ops.length })}</p>}
          {steps.map((s, i) => (
            <div key={s.key} className="mod-row-draft">
              <div className="mod-row-head">
                <span className="mono faint">#{i + 1}</span>
                {s.opId ? <span className="small faint grow ellip">{t('mod.form.fromOp', { op: s.opId })}</span> : <span className="grow" />}
                <button type="button" className="btn ghost icon sm" onClick={() => moveStep(i, -1)} disabled={i === 0} aria-label={t('mod.form.stepUp', { n: i + 1 })}>
                  <ArrowUp />
                </button>
                <button type="button" className="btn ghost icon sm" onClick={() => moveStep(i, 1)} disabled={i === steps.length - 1} aria-label={t('mod.form.stepDown', { n: i + 1 })}>
                  <ArrowDown />
                </button>
                <button type="button" className="btn ghost icon sm" onClick={() => setSteps((xs) => xs.filter((x) => x.key !== s.key))} aria-label={t('mod.form.stepRemove', { n: i + 1 })}>
                  <Trash2 />
                </button>
              </div>
              <div className="mod-step-grid">
                <input className="input" value={s.name} maxLength={200} placeholder={t('mod.form.stepName')} aria-label={t('mod.form.stepNameN', { n: i + 1 })} onChange={(e) => updStep(s.key, { name: e.target.value })} />
                <input className="input mono" value={s.machine} maxLength={120} placeholder={t('mod.form.stepMachine')} aria-label={t('mod.form.stepMachineN', { n: i + 1 })} onChange={(e) => updStep(s.key, { machine: e.target.value })} />
                <input className="input mono" inputMode="decimal" value={s.minutes} placeholder={t('mod.form.stepMinutes')} aria-label={t('mod.form.stepMinutesN', { n: i + 1 })} onChange={(e) => updStep(s.key, { minutes: e.target.value })} />
              </div>
              <LimitedTextarea className="input" rows={2} limit="description" value={s.description} placeholder={t('mod.form.stepDescription')} aria-label={t('mod.form.stepDescriptionN', { n: i + 1 })} onChange={(e) => updStep(s.key, { description: e.target.value })} />
            </div>
          ))}
          {show('steps') && <span className="field-error">{errs.steps}</span>}
          <div>
            <button type="button" className="btn sm" onClick={() => setSteps((xs) => [...xs, { key: newKey(), name: '', machine: '', description: '', minutes: '', source: null, opId: null }])}>
              <Plus /> {t('mod.form.addStep')}
            </button>
          </div>
        </fieldset>

        <fieldset className="mod-section">
          <legend>{t('mod.parameters')}</legend>
          <p className="faint small">{t('mod.form.paramsHint')}</p>
          {params.map((p, i) => (
            <div key={p.key} className="mod-row-draft">
              <div className="mod-param-grid">
                <input className="input" value={p.name} maxLength={120} placeholder={t('mod.param.name')} aria-label={t('mod.form.paramNameN', { n: i + 1 })} onChange={(e) => updParam(p.key, { name: e.target.value })} />
                <input className="input mono" value={p.unit} maxLength={20} placeholder={t('mod.param.unit')} aria-label={t('mod.form.paramUnitN', { n: i + 1 })} onChange={(e) => updParam(p.key, { unit: e.target.value })} />
                <input className="input mono" value={p.def} maxLength={60} placeholder={t('mod.param.default')} aria-label={t('mod.form.paramDefaultN', { n: i + 1 })} onChange={(e) => updParam(p.key, { def: e.target.value })} />
                <input className="input mono" inputMode="decimal" value={p.min} placeholder={t('mod.param.min')} aria-label={t('mod.form.paramMinN', { n: i + 1 })} onChange={(e) => updParam(p.key, { min: e.target.value })} />
                <input className="input mono" inputMode="decimal" value={p.max} placeholder={t('mod.param.max')} aria-label={t('mod.form.paramMaxN', { n: i + 1 })} onChange={(e) => updParam(p.key, { max: e.target.value })} />
              </div>
              <div>
                <button type="button" className="btn ghost sm" onClick={() => setParams((xs) => xs.filter((x) => x.key !== p.key))}>
                  <Trash2 /> {t('mod.form.paramRemove', { n: i + 1 })}
                </button>
              </div>
            </div>
          ))}
          {show('params') && <span className="field-error">{errs.params}</span>}
          <div>
            <button type="button" className="btn sm" onClick={() => setParams((xs) => [...xs, { key: newKey(), name: '', unit: '', def: '', min: '', max: '' }])}>
              <Plus /> {t('mod.form.addParam')}
            </button>
          </div>
        </fieldset>

        <fieldset className="mod-section">
          <legend>{t('mod.validation')}</legend>
          <Field label={t('mod.validationScope')} hint={t('mod.form.scopeHint')} error={show('scope')}>
            <LimitedTextarea className="input" rows={3} limit="description" value={scope} onChange={(e) => setScope(e.target.value)} />
          </Field>
          <Field label={t('mod.evidence')} optional>
            <LimitedTextarea className="input" rows={2} limit="description" value={evidence} onChange={(e) => setEvidence(e.target.value)} />
          </Field>
          {plan && (
            <label className="check tk-wrap">
              <input type="checkbox" checked={validatedOnSource} onChange={(e) => setValidatedOnSource(e.target.checked)} />
              {t('mod.form.validatedOnSource', { label: source?.label ?? '' })}
            </label>
          )}
          {baseValidated.length > 0 && <p className="faint small">{t('mod.form.keepsValidated', { n: baseValidated.length })}</p>}
        </fieldset>

        {missing && (
          <div className="callout err" role="alert">
            <TriangleAlert aria-hidden />
            <div className="stack-sm grow">
              <span>{t('mod.err.incomplete')}</span>
              {missing.length > 0 && (
                <ul className="tk-plain-list small">
                  {missing.map((m) => (
                    <li key={m}>{m.startsWith('invalid:') ? t('mod.missing.invalid', { field: missingText(t, m.slice(8)) }) : missingText(t, m)}</li>
                  ))}
                </ul>
              )}
              <code className="error-code">{t('common.errorCode', { code: 'module_incomplete' })}</code>
            </div>
          </div>
        )}
        {!missing && <ErrorNotice error={error} overrides={{ forbidden: 'mod.err.forbidden', project_not_active: 'mod.err.projectNotActive' }} />}
        {touched && invalid && !error && <p className="field-error">{t('mod.form.fixErrors')}</p>}
      </div>
    </Dialog>
  );
}

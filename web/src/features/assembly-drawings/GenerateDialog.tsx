// Generate a drawing package for one exact assembly revision: views, section (auto / none / a plane), BOM view,
// optional key (interface / mounting) dimensions between two instances, technical notes and the execution policy
// (standard preselected; Turbo is always an explicit choice).

import { Plus,Trash2 } from 'lucide-react';
import { useState } from 'react';
import { getScene } from '../../api/assemblies';
import { generateAssemblyDrawing,VIEW_NAMES,type AssemblyDrawingPackage,type Axis,type ViewName } from '../../api/assemblyDrawings';
import { Dialog } from '../../components/ui/Dialog';
import { Field } from '../../components/ui/Form';
import { ErrorNotice } from '../../components/ui/States';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { buildConfig,DATUMS,DEFAULT_FORM,formProblem,type DatumChoice,type GenerateForm,type KeyDimRow } from './model';

export const GENERATE_ERRORS = {
  drawing_budget_exceeded: 'asmd.err.budget',
  revision_not_ready: 'asmd.err.notReady',
  invalid_drawing_config: 'asmd.err.config',
} as const;

const AXES: Axis[] = ['x', 'y', 'z'];

export function GenerateDialog({
  revision,
  isLatest,
  onClose,
  onCreated,
}: {
  revision: { id: string; label: string };
  /** S5: given, the package follows the revision's policy unless the person picks one (shown with its source). */
  projectId?: string;
  isLatest: boolean;
  onClose: () => void;
  onCreated: (pkg: AssemblyDrawingPackage) => void;
}) {
  const { t } = useI18n();
  const [form, setForm] = useState<GenerateForm>(DEFAULT_FORM);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const wantScene = form.keyDims.length > 0;
  const sceneRes = useResource((signal) => getScene(revision.id, { signal }), [revision.id], { enabled: wantScene });
  const occurrences = sceneRes.data?.occurrences ?? [];
  const problem = formProblem(form);
  const patch = (p: Partial<GenerateForm>) => setForm((f) => ({ ...f, ...p }));
  const setDim = (i: number, p: Partial<KeyDimRow>) => patch({ keyDims: form.keyDims.map((k, j) => (j === i ? { ...k, ...p } : k)) });
  const toggleView = (v: ViewName) => patch({ views: form.views.includes(v) ? form.views.filter((x) => x !== v) : VIEW_NAMES.filter((x) => x === v || form.views.includes(x)) });
  const submit = async () => {
    if (problem) return;
    setBusy(true);
    setError(null);
    try {
      onCreated(await generateAssemblyDrawing(revision.id, { config: buildConfig(form) }));
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  const instanceSelect = (value: string, onChange: (v: string) => void, label: string) => (
    <select className="select" value={value} onChange={(e) => onChange(e.target.value)} aria-label={label} disabled={!occurrences.length}>
      <option value="">{sceneRes.loading ? t('common.loading') : t('asmd.gen.pickInstance')}</option>
      {occurrences.map((o) => (
        <option key={o.path.join('/')} value={o.path.join('/')}>
          {o.names.join(' / ')}
        </option>
      ))}
    </select>
  );
  const datumSelect = (value: DatumChoice, onChange: (v: DatumChoice) => void, label: string) => (
    <select className="select" value={value} onChange={(e) => onChange(e.target.value as DatumChoice)} aria-label={label}>
      {DATUMS.map((d) => (
        <option key={d} value={d}>
          {t(`asmd.datum.${d === 'origin' ? 'origin' : d === 'bbox.min' ? 'min' : d === 'bbox.max' ? 'max' : 'center'}`)}
        </option>
      ))}
    </select>
  );
  return (
    <Dialog
      title={t('asmd.gen.title', { rev: revision.label })}
      subtitle={t('asmd.gen.subtitle')}
      onClose={onClose}
      busy={busy}
      size="lg"
      onSubmit={() => void submit()}
      footer={
        <>
          {problem && <span className="asmd-err small grow">{t(`asmd.gen.problem.${problem}`)}</span>}
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={busy || !!problem}>
            {busy ? t('asmd.gen.busy') : t('asmd.gen.go')}
          </button>
        </>
      }
    >
      {!isLatest && <p className="callout slim warn">{t('asmd.gen.oldRevision')}</p>}
      {error ? <ErrorNotice error={error} overrides={GENERATE_ERRORS} /> : null}
      <Field label={t('asmd.gen.views')} hint={t('asmd.gen.viewsHint')}>
        <div className="asmd-checks" role="group">
          {VIEW_NAMES.map((v) => (
            <label key={v} className="check">
              <input type="checkbox" checked={form.views.includes(v)} onChange={() => toggleView(v)} /> {t(`asmd.view.${v}`)}
            </label>
          ))}
        </div>
      </Field>
      <Field label={t('asmd.gen.section')} hint={t('asmd.gen.sectionHint')}>
        <div className="stack-sm">
          <div className="seg" role="group">
            {(['auto', 'none', 'plane'] as const).map((m) => (
              <button key={m} type="button" className="seg-btn" aria-pressed={form.section === m} onClick={() => patch({ section: m })}>
                {t(`asmd.gen.section.${m}`)}
              </button>
            ))}
          </div>
          {form.section === 'plane' && (
            <div className="row asmd-plane">
              <select className="select" value={form.plane} onChange={(e) => patch({ plane: e.target.value as 'XZ' | 'YZ' })} aria-label={t('asmd.gen.plane')}>
                <option value="XZ">{t('asmd.gen.plane.XZ')}</option>
                <option value="YZ">{t('asmd.gen.plane.YZ')}</option>
              </select>
              <label className="row">
                <span className="small">{t('asmd.gen.offset')}</span>
                <input className="input mono asmd-num" inputMode="decimal" value={form.offset} onChange={(e) => patch({ offset: e.target.value })} />
              </label>
            </div>
          )}
        </div>
      </Field>
      <Field label={t('asmd.gen.bom')}>
        <div className="seg" role="group">
          {(['flattened', 'hierarchical'] as const).map((b) => (
            <button key={b} type="button" className="seg-btn" aria-pressed={form.bomView === b} onClick={() => patch({ bomView: b })}>
              {t(`asmd.bom.view.${b}`)}
            </button>
          ))}
        </div>
      </Field>
      <Field label={t('asmd.gen.keyDims')} hint={t('asmd.gen.keyDimsHint')} optional>
        <div className="stack-sm">
          {form.keyDims.map((k, i) => (
            <fieldset key={i} className="asmd-keydim">
              <legend className="small">{t('asmd.gen.keyDimN', { n: i + 1 })}</legend>
              <div className="asmd-keydim-grid">
                <input className="input" value={k.label} maxLength={40} placeholder={t('asmd.gen.keyDimLabel')} aria-label={t('asmd.gen.keyDimLabel')} onChange={(e) => setDim(i, { label: e.target.value })} />
                <select className="select" value={k.axis} onChange={(e) => setDim(i, { axis: e.target.value as Axis })} aria-label={t('asmd.gen.axis')}>
                  {AXES.map((a) => (
                    <option key={a} value={a}>
                      {t('asmd.gen.axisN', { axis: a.toUpperCase() })}
                    </option>
                  ))}
                </select>
                {instanceSelect(k.a, (v) => setDim(i, { a: v }), t('asmd.gen.fromInstance'))}
                {datumSelect(k.aDatum, (v) => setDim(i, { aDatum: v }), t('asmd.gen.fromDatum'))}
                {instanceSelect(k.b, (v) => setDim(i, { b: v }), t('asmd.gen.toInstance'))}
                {datumSelect(k.bDatum, (v) => setDim(i, { bDatum: v }), t('asmd.gen.toDatum'))}
              </div>
              <button type="button" className="btn ghost sm" onClick={() => patch({ keyDims: form.keyDims.filter((_, j) => j !== i) })}>
                <Trash2 aria-hidden /> {t('common.remove')}
              </button>
            </fieldset>
          ))}
          <button
            type="button"
            className="btn sm asmd-add"
            disabled={form.keyDims.length >= 12}
            onClick={() => patch({ keyDims: [...form.keyDims, { label: '', axis: 'x', a: '', aDatum: 'origin', b: '', bDatum: 'origin' }] })}
          >
            <Plus aria-hidden /> {t('asmd.gen.addKeyDim')}
          </button>
        </div>
      </Field>
      <Field label={t('asmd.gen.notes')} hint={t('asmd.gen.notesHint')} optional>
        <textarea className="input" rows={3} value={form.notes} onChange={(e) => patch({ notes: e.target.value })} />
      </Field>

    </Dialog>
  );
}

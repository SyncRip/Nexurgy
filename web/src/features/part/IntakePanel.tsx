// Drawing-only intake: a person enters the manufacturing information from a
// customer drawing (no AI). Always an estimate; needs design approval before
// it can drive a confirmed plan. The source drawing can instead be handed to
// the AI assistant (drawing extraction keeps manual entry as the fallback).

import { Plus,ScrollText,Trash2 } from 'lucide-react';
import { useState } from 'react';
import { createManualIntake,listFiles } from '../../api/endpoints';
import type { HoleFeature,Intake,ManualIntakeCreate,Part,Project,ToleranceNote,Vec3 } from '../../api/types';
import type { ProjectCapabilities } from '../../auth/permissions';
import { Dialog } from '../../components/ui/Dialog';
import { TagInput } from '../../components/ui/Form';
import { EmptyState,ErrorNotice } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';

import { LimitedTextarea } from '../../components/ui/LimitedText';
import { isDrawingFileKind } from '../assistant/refs';
import { ApprovalActions,ApprovalChip,EstimatedBadge } from './common';
import { CheckField,NumField,NumInput,VecField } from './model/fields';

export const MATERIAL_SUGGESTIONS = ['AL6061-T651', 'AL7075-T651', 'S45C', 'SUS304', 'POM'];

export function IntakeList({
  intakes,
  latestId,
  caps,
  names,
  onChanged,
  onPlan,
}: {
  intakes: Intake[];
  latestId: string | null;
  caps: ProjectCapabilities;
  names: (id: string) => string;
  onChanged: () => void;
  onPlan?: (intake: Intake) => void;
}) {
  const { t, fmt } = useI18n();
  if (!intakes.length) return <EmptyState compact title={t('ws.intake.none')} body={t('ws.intake.noneBody')} icon={<ScrollText />} />;
  return (
    <ul className="ws-intakes">
      {intakes.map((it) => {
        const s = it.summary;
        const holes = s.holes.reduce((n, h) => n + h.count, 0);
        return (
          <li key={it.id} className="ws-intake">
            <div className="row">
              <span className="strong">{t(it.method === 'ai' ? 'ws.intake.methodAi' : 'ws.intake.methodManual')}</span>
              <EstimatedBadge />
              {it.id === latestId && <span className="chip measure">{t('ws.intake.latest')}</span>}
              <ApprovalChip approval={it.approval} names={names} />
              <span className="grow" />
              <span className="faint small">{fmt.dateTime(it.created_at)}</span>
            </div>
            <div className="small mono">
              {s.bbox_mm.join(' × ')} mm · {s.material} · {t('ws.intake.holesN', { n: holes })} · V≈{fmt.number(s.volume_mm3, { maximumFractionDigits: 0 })} mm³
            </div>
            {s.tolerances.length > 0 && <div className="small muted">{s.tolerances.map((x) => `${x.critical ? '★ ' : ''}${x.text}`).join(' · ')}</div>}
            {s.unsupported.length > 0 && <div className="small warn-text">{t('ws.intake.unsupported')}: {s.unsupported.join(', ')}</div>}
            <div className="row">
              {!it.approval?.valid && it.id === latestId && <ApprovalActions partId={it.part_id} compact subjectType="intake" subjectId={it.id} stage="design" can={caps.design} onDone={onChanged} />}
              {onPlan && caps.process && (
                <button type="button" className="btn sm" onClick={() => onPlan(it)}>
                  {t('ws.intake.planFrom')}
                </button>
              )}
            </div>
          </li>
        );
      })}
    </ul>
  );
}

interface HoleRow extends HoleFeature {
  key: number;
}

export function IntakeDialog({ part, project, onClose, onCreated }: { part: Part; project: Project; onClose: () => void; onCreated: (it: Intake) => void }) {
  const { t } = useI18n();
  const toast = useToast();
  const filesRes = useResource((signal) => listFiles(project.id, { part_id: part.id }, { signal }), [project.id, part.id]);
  const drawingFiles = (filesRes.data ?? []).filter((f) => f.kind.startsWith('drawing_') && f.versions.length);
  const [fileVersionId, setFileVersionId] = useState('');
  const [material, setMaterial] = useState(part.material || '');
  const [bbox, setBbox] = useState<number[]>([100, 60, 20]);
  const [volume, setVolume] = useState<number | null>(null);
  const [holes, setHoles] = useState<HoleRow[]>([]);
  const [fillets, setFillets] = useState(0);
  const [chamfers, setChamfers] = useState(0);
  const [setups, setSetups] = useState(1);
  const [tols, setTols] = useState<ToleranceNote[]>([]);
  const [unsupported, setUnsupported] = useState<string[]>([]);
  const [notes, setNotes] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const seq = useState({ n: 0 })[0];
  const chosenDrawing = drawingFiles.find((f) => f.versions[0].id === fileVersionId) ?? null;

  const bboxValid = bbox.every((x) => Number.isFinite(x) && x > 0);
  const submit = async () => {
    if (!bboxValid) return;
    setBusy(true);
    setError(null);
    const body: ManualIntakeCreate = {
      file_version_id: fileVersionId || null,
      material: material.trim() || null,
      bbox_mm: bbox as Vec3,
      volume_mm3: volume && volume > 0 ? volume : null,
      holes: holes.map(({ key: _k, ...h }) => ({ ...h, depth: h.through ? null : h.depth })),
      fillets,
      chamfers,
      setup_directions: setups,
      tolerances: tols.filter((x) => x.text.trim()),
      unsupported,
      notes,
    };
    try {
      const it = await createManualIntake(part.id, body);
      toast(t('ws.intake.created'));
      onCreated(it);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      title={t('ws.intake.new')}
      subtitle={t('ws.intake.newSub')}
      size="lg"
      busy={busy}
      onClose={onClose}
      onSubmit={() => void submit()}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={busy || !bboxValid}>
            {busy ? t('common.saving') : t('ws.intake.create')}
          </button>
        </>
      }
    >
      <div className="stack-sm">
        <div className="callout pending slim">
          <span>{t('ws.intake.estimateNote')}</span>
        </div>
        <div className="ws-row2">
          <div className="ws-f">
            <label htmlFor="intake-file">{t('ws.intake.file')}</label>
            <select id="intake-file" className="select sm" value={fileVersionId} onChange={(e) => setFileVersionId(e.target.value)}>
              <option value="">{t('ws.intake.noFile')}</option>
              {drawingFiles.map((f) => (
                <option key={f.id} value={f.versions[0].id}>
                  {f.name} · v{f.versions[0].version}
                </option>
              ))}
            </select>
            {chosenDrawing && isDrawingFileKind(chosenDrawing.kind) && (
              <span className="ws-f-hint">
                <></>
              </span>
            )}
          </div>
          <div className="ws-f">
            <label htmlFor="intake-mat">{t('ws.intake.material')}</label>
            <input id="intake-mat" className="input sm" list="ws-materials" value={material} onChange={(e) => setMaterial(e.target.value)} />
            <datalist id="ws-materials">
              {MATERIAL_SUGGESTIONS.map((m) => (
                <option key={m} value={m} />
              ))}
            </datalist>
          </div>
        </div>
        <VecField label={t('ws.intake.bbox')} unit="mm" value={bbox} onChange={setBbox} error={bboxValid ? undefined : t('ws.v.positive')} />
        <div className="ws-row3">
          <NumField label={t('ws.intake.volume')} unit="mm³" value={volume} onChange={setVolume} onClear={() => setVolume(null)} placeholder={t('ws.intake.volumeAuto')} hint={t('ws.intake.volumeHint')} />
          <NumField label={t('ws.intake.setups')} integer value={setups} onChange={(v) => setSetups(Math.min(6, Math.max(1, v)))} />
          <div className="ws-row2">
            <NumField label={t('ws.intake.fillets')} integer value={fillets} onChange={(v) => setFillets(Math.max(0, v))} />
            <NumField label={t('ws.intake.chamfers')} integer value={chamfers} onChange={(v) => setChamfers(Math.max(0, v))} />
          </div>
        </div>

        <div className="ws-f">
          <span className="ws-f-label">{t('ws.intake.holes')}</span>
          {holes.length > 0 && (
            <table className="t dense ws-mini">
              <thead>
                <tr>
                  <th>Ø mm</th>
                  <th>{t('ws.hole.through')}</th>
                  <th>{t('ws.hole.depth')}</th>
                  <th>{t('ws.inspect.count')}</th>
                  <th>{t('ws.hole.head.cb')}</th>
                  <th>{t('ws.hole.head.cs')}</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {holes.map((h) => {
                  const upd = (patch: Partial<HoleRow>) => setHoles((xs) => xs.map((x) => (x.key === h.key ? { ...x, ...patch } : x)));
                  return (
                    <tr key={h.key}>
                      <td>
                        <NumInput value={h.diameter} onChange={(v) => upd({ diameter: v })} aria-label="Ø" />
                      </td>
                      <td>
                        <input type="checkbox" checked={h.through} onChange={(e) => upd({ through: e.target.checked })} aria-label={t('ws.hole.through')} />
                      </td>
                      <td>{!h.through && <NumInput value={h.depth ?? null} onChange={(v) => upd({ depth: v })} aria-label={t('ws.hole.depth')} />}</td>
                      <td>
                        <NumInput integer value={h.count} onChange={(v) => upd({ count: Math.max(1, v) })} aria-label={t('ws.inspect.count')} />
                      </td>
                      <td>
                        <input type="checkbox" checked={h.counterbore} onChange={(e) => upd({ counterbore: e.target.checked, countersink: e.target.checked ? false : h.countersink })} aria-label={t('ws.hole.head.cb')} />
                      </td>
                      <td>
                        <input type="checkbox" checked={h.countersink} onChange={(e) => upd({ countersink: e.target.checked, counterbore: e.target.checked ? false : h.counterbore })} aria-label={t('ws.hole.head.cs')} />
                      </td>
                      <td className="act">
                        <button type="button" className="btn sm icon ghost" onClick={() => setHoles((xs) => xs.filter((x) => x.key !== h.key))} aria-label={t('common.remove')}>
                          <Trash2 />
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
          <div>
            <button type="button" className="btn sm" onClick={() => setHoles((xs) => [...xs, { key: ++seq.n, diameter: 6, depth: null, through: true, count: 1, counterbore: false, countersink: false }])}>
              <Plus /> {t('ws.intake.addHole')}
            </button>
          </div>
        </div>

        <div className="ws-f">
          <span className="ws-f-label">{t('ws.intake.tolerances')}</span>
          {tols.map((tol, i) => (
            <div className="row" key={i}>
              <input className="input sm grow" value={tol.text} placeholder="Ø10 H7 · Ra1.6 · ±0.02" onChange={(e) => setTols((xs) => xs.map((x, j) => (j === i ? { ...x, text: e.target.value } : x)))} aria-label={t('ws.intake.tolerance')} />
              <CheckField label={t('ws.intake.critical')} checked={tol.critical} onChange={(v) => setTols((xs) => xs.map((x, j) => (j === i ? { ...x, critical: v } : x)))} />
              <button type="button" className="btn sm icon ghost" onClick={() => setTols((xs) => xs.filter((_, j) => j !== i))} aria-label={t('common.remove')}>
                <Trash2 />
              </button>
            </div>
          ))}
          <div>
            <button type="button" className="btn sm" onClick={() => setTols((xs) => [...xs, { text: '', critical: false }])}>
              <Plus /> {t('ws.intake.addTolerance')}
            </button>
          </div>
        </div>
        <div className="ws-f">
          <span className="ws-f-label">{t('ws.intake.unsupportedLabel')}</span>
          <TagInput value={unsupported} onChange={setUnsupported} placeholder={t('ws.intake.unsupportedPh')} />
        </div>
        <div className="ws-f">
          <label htmlFor="intake-notes">{t('ws.intake.notes')}</label>
          <LimitedTextarea id="intake-notes" className="input" rows={2} value={notes} limit="description" onChange={(e) => setNotes(e.target.value)} />
        </div>
        <ErrorNotice error={error} />
      </div>
    </Dialog>
  );
}

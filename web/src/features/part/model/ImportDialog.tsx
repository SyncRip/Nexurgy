// Import a model or drawing file as a new revision (file-only path, no AI):
// pick a project file (this part's first) or upload one. STEP becomes an
// editable base (F-00) after the server's source audit (an assembly or several
// bodies are refused with the audit); STL / OBJ / GLB become reference-only
// mesh revisions; a DXF / DWG becomes a plate only through the strict PROFILE
// import (layer, thickness and scale are required — nothing is guessed). The
// alternative for a drawing is "Rebuild with AI": the assistant opens with
// that exact file version as the target and drawing_reconstruct preselected.

import { ArrowLeft, FileUp, Layers, Upload, Wand2 } from 'lucide-react';
import { useMemo, useRef, useState } from 'react';
import { isApiError } from '../../../api/client';
import { importRevision, listFiles, uploadFile } from '../../../api/endpoints';
import type { DxfProfile, ModelRevision, Part, Project, ProjectFile } from '../../../api/types';
import { KindBadge } from '../../../components/ui/Badges';
import { Dialog } from '../../../components/ui/Dialog';
import { EmptyState, ErrorNotice, SkeletonRows } from '../../../components/ui/States';
import { useToast } from '../../../components/ui/Toast';
import { useResource } from '../../../hooks/useResource';
import { useI18n } from '../../../i18n/I18nProvider';
import { useAssistant } from '../../assistant/AssistantProvider';
import { fileVersionRef, partRef } from '../../assistant/refs';
import { CadErrorDetails } from '../../cad/CadErrorDetails';
import { cadDrawingFormat, cadErrorFamily, profileFieldProblem } from '../../cad/status';
import '../../cad/cad.css';
import { NumField, SelectField } from './fields';

const MODEL_EXT = /\.(step|stp|stl|obj|glb|dxf|dwg)$/i;
const MESH_OR_STEP = /\.(step|stp|stl|obj|glb)$/i;

function isImportable(f: ProjectFile): boolean {
  return f.kind === 'model_step' || f.kind === 'model_mesh' || f.kind === 'drawing_dxf' || f.versions.some((v) => MODEL_EXT.test(v.original_name));
}

interface DrawingTarget {
  fileVersionId: string;
  name: string;
  format: 'dxf' | 'dwg';
  /** The file's part (null = project file; the part is added to the AI request as a reference). */
  filePartId: string | null;
}

export function ImportDialog({ part, project, onClose, onImported }: { part: Part; project: Project; onClose: () => void; onImported: (rev: ModelRevision) => void }) {
  const { t, fmt } = useI18n();
  const filesRes = useResource((signal) => listFiles(project.id, {}, { signal }), [project.id]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [versionOf, setVersionOf] = useState<Record<string, string>>({});
  const [drawing, setDrawing] = useState<DrawingTarget | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const files = useMemo(() => {
    const list = (filesRes.data ?? []).filter((f) => !f.archived_at && isImportable(f) && f.versions.length);
    return list.sort((a, b) => Number(b.part_id === part.id) - Number(a.part_id === part.id) || b.created_at.localeCompare(a.created_at));
  }, [filesRes.data, part.id]);

  const doImport = async (fileVersionId: string, key: string, profile?: DxfProfile) => {
    setBusy(key);
    setError(null);
    try {
      const rev = await importRevision(part.id, { file_version_id: fileVersionId, ...(profile ? { dxf_profile: profile } : {}) });
      onImported(rev);
    } catch (e) {
      setError(e);
      // The server recognised a drawing (by its bytes) that the name did not reveal: offer the PROFILE form / AI rebuild.
      if (!profile && isApiError(e) && e.code === 'drawing_needs_reconstruct') {
        const f = (filesRes.data ?? []).find((x) => x.versions.some((v) => v.id === fileVersionId));
        const v = f?.versions.find((x) => x.id === fileVersionId);
        const format = e.details.format === 'dwg' ? 'dwg' : 'dxf';
        setDrawing({ fileVersionId, name: v?.original_name ?? f?.name ?? fileVersionId.slice(0, 8), format, filePartId: f?.part_id ?? part.id });
      }
    } finally {
      setBusy(null);
    }
  };

  /** A DXF / DWG is never imported without its PROFILE parameters: open the form instead. */
  const start = (f: ProjectFile, versionId: string) => {
    const v = f.versions.find((x) => x.id === versionId) ?? f.versions[0];
    const format = cadDrawingFormat(v.original_name, f.kind);
    if (format) {
      setError(null);
      setDrawing({ fileVersionId: v.id, name: v.original_name, format, filePartId: f.part_id });
      return;
    }
    void doImport(v.id, f.id);
  };

  const upload = async (file: File) => {
    if (!MODEL_EXT.test(file.name)) {
      setError(new Error(t('cad.imp.badExt')));
      return;
    }
    setBusy('upload');
    setError(null);
    try {
      const pf = await uploadFile(project.id, { file, part_id: part.id, note: t('ws.import.uploadNote') });
      filesRes.reload();
      const v = pf.versions[0];
      if (!v) {
        setBusy(null);
        return;
      }
      const format = cadDrawingFormat(v.original_name, pf.kind);
      if (format && !MESH_OR_STEP.test(v.original_name)) {
        setBusy(null);
        setDrawing({ fileVersionId: v.id, name: v.original_name, format, filePartId: pf.part_id });
        return;
      }
      await doImport(v.id, 'upload');
    } catch (e) {
      setError(e);
      setBusy(null);
    }
  };

  if (drawing) {
    return (
      <Dialog title={t('cad.imp.profileTitle', { name: drawing.name })} subtitle={t('cad.imp.profileSub')} size="lg" onClose={onClose} busy={!!busy}>
        <ProfileForm
          part={part}
          project={project}
          target={drawing}
          busy={busy === 'profile'}
          error={error}
          onBack={() => {
            setDrawing(null);
            setError(null);
          }}
          onImport={(profile) => void doImport(drawing.fileVersionId, 'profile', profile)}
          onAiOpened={onClose}
        />
      </Dialog>
    );
  }

  return (
    <Dialog title={t('cad.imp.title')} subtitle={t('ws.import.sub')} size="lg" onClose={onClose} busy={!!busy}>
      <div className="stack">
        <div className="ws-import-kinds">
          <div className="callout info slim">
            <span>
              <strong>STEP</strong> — {t('ws.import.stepNote')}
            </span>
          </div>
          <div className="callout pending slim">
            <span>
              <strong>STL / OBJ / GLB</strong> — {t('ws.import.meshNote')}
            </span>
          </div>
          <div className="callout pending slim">
            <span>
              <strong>DXF / DWG</strong> — {t('cad.imp.dxfNote')}
            </span>
          </div>
        </div>
        <div className="row">
          <input
            ref={inputRef}
            type="file"
            accept=".step,.stp,.stl,.obj,.glb,.dxf,.dwg"
            hidden
            onChange={(e) => {
              const f = e.target.files?.[0];
              e.target.value = '';
              if (f) void upload(f);
            }}
          />
          <button type="button" className="btn" disabled={!!busy} onClick={() => inputRef.current?.click()}>
            <Upload /> {busy === 'upload' ? t('ws.import.uploading') : t('ws.import.upload')}
          </button>
          <span className="faint small">{t('ws.import.uploadHint')}</span>
        </div>
        <ErrorNotice error={error} />
        {isApiError(error) && cadErrorFamily(error.code) !== 'other' && <CadErrorDetails code={error.code} details={error.details} />}
        {filesRes.initial && filesRes.loading ? (
          <SkeletonRows rows={3} cols={4} />
        ) : filesRes.error ? (
          <ErrorNotice error={filesRes.error} onRetry={filesRes.reload} />
        ) : !files.length ? (
          <EmptyState compact title={t('ws.import.noFiles')} body={t('cad.imp.noFilesBody')} icon={<FileUp />} />
        ) : (
          <div className="table-wrap">
            <table className="t dense">
              <thead>
                <tr>
                  <th>{t('ws.import.file')}</th>
                  <th>{t('ws.import.version')}</th>
                  <th>{t('ws.import.scope')}</th>
                  <th className="act" />
                </tr>
              </thead>
              <tbody>
                {files.map((f) => {
                  const vid = versionOf[f.id] ?? f.versions[0].id;
                  const v = f.versions.find((x) => x.id === vid) ?? f.versions[0];
                  const isDrawing = !!cadDrawingFormat(v.original_name, f.kind);
                  return (
                    <tr key={f.id}>
                      <td>
                        <div className="cell-main">
                          <KindBadge kind={f.kind} withLabel={false} />
                          <span className="ellip">{f.name}</span>
                        </div>
                      </td>
                      <td>
                        <select className="select sm" value={vid} onChange={(e) => setVersionOf((m) => ({ ...m, [f.id]: e.target.value }))} aria-label={t('ws.import.version')}>
                          {f.versions.map((x) => (
                            <option key={x.id} value={x.id}>
                              v{x.version} · {x.original_name} · {fmt.date(x.created_at)}
                            </option>
                          ))}
                        </select>
                      </td>
                      <td className="small">{f.part_id === part.id ? t('ws.import.thisPart') : f.part_id ? t('ws.import.otherPart') : t('ws.import.project')}</td>
                      <td className="act">
                        <button type="button" className="btn sm primary" disabled={!!busy} onClick={() => start(f, vid)}>
                          {busy === f.id ? t('common.working') : isDrawing ? <Layers aria-hidden /> : null} {busy === f.id ? null : t('ws.import.do')}
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </Dialog>
  );
}

/** Strict PROFILE parameters (no defaults for thickness / scale) and the AI alternative. */
function ProfileForm({
  part,
  project,
  target,
  busy,
  error,
  onBack,
  onImport,
  onAiOpened,
}: {
  part: Part;
  project: Project;
  target: DrawingTarget;
  busy: boolean;
  error: unknown;
  onBack: () => void;
  onImport: (p: DxfProfile) => void;
  onAiOpened: () => void;
}) {
  const { t } = useI18n();
  const toast = useToast();
  const { addContext } = useAssistant();
  const [layer, setLayer] = useState('PROFILE');
  const [thickness, setThickness] = useState<number | null>(null);
  const [scale, setScale] = useState<number | null>(null);
  const [baseZ, setBaseZ] = useState<number | null>(0);
  const [units, setUnits] = useState<'' | 'mm'>('');
  const [touched, setTouched] = useState(false);
  const [aiBusy, setAiBusy] = useState(false);
  const thicknessProblem = profileFieldProblem(thickness, { positive: true });
  const scaleProblem = profileFieldProblem(scale, { positive: true });
  const baseProblem = profileFieldProblem(baseZ);
  const layerProblem = layer.trim() ? null : 'required';
  const valid = !thicknessProblem && !scaleProblem && !baseProblem && !layerProblem;
  const otherPart = !!target.filePartId && target.filePartId !== part.id;
  const problemText = (p: 'required' | 'positive' | null) => (touched && p ? t(p === 'required' ? 'cad.imp.required' : 'cad.imp.positive') : undefined);

  const submit = () => {
    setTouched(true);
    if (!valid || busy) return;
    onImport({ layer: layer.trim(), thickness_mm: thickness!, scale: scale!, base_z: baseZ ?? 0, units: units || null });
  };

  /** Assistant: the exact file version as the target, drawing_reconstruct preselected (a project file brings this part along). */
  const rebuildWithAi = async () => {
    if (aiBusy) return;
    setAiBusy(true);
    try {
      if (!target.filePartId) await addContext(partRef(project.id, part.id), { open: false });
      const added = await addContext(fileVersionRef(project.id, target.fileVersionId), { asTarget: true, operation: 'drawing_reconstruct' });
      if (added) {
        toast(t('cad.imp.aiAdded'), { tone: 'info' });
        onAiOpened();
      }
    } finally {
      setAiBusy(false);
    }
  };

  const needsReconstruct = isApiError(error) && error.code === 'drawing_needs_reconstruct';
  const aiButton = (
    <button type="button" className={`btn ${needsReconstruct ? 'primary' : ''}`} onClick={() => void rebuildWithAi()} disabled={aiBusy || busy || otherPart} title={otherPart ? t('ws.import.otherPart') : t('cad.imp.aiNote')}>
      <Wand2 aria-hidden /> {t('cad.imp.ai')}
    </button>
  );

  return (
    <form
      className="stack"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
      <div className="cad-profile-form">
        <div className={`ws-f ${problemText(layerProblem) ? 'has-error' : ''}`}>
          <label htmlFor="cad-imp-layer">{t('cad.imp.layer')}</label>
          <input id="cad-imp-layer" className="input sm mono" value={layer} maxLength={255} onChange={(e) => setLayer(e.target.value)} disabled={busy} />
          {problemText(layerProblem) && <span className="ws-f-err">{problemText(layerProblem)}</span>}
        </div>
        <SelectField<'' | 'mm'>
          label={t('cad.imp.units')}
          value={units}
          disabled={busy}
          options={[
            { value: '', label: t('cad.imp.unitsFile') },
            { value: 'mm', label: t('cad.imp.unitsMm') },
          ]}
          onChange={setUnits}
        />
        <NumField label={t('cad.imp.thickness')} unit="mm" value={thickness} onChange={setThickness} onClear={() => setThickness(null)} step={0.5} disabled={busy} error={problemText(thicknessProblem)} placeholder="—" />
        <NumField label={t('cad.imp.scale')} value={scale} onChange={setScale} onClear={() => setScale(null)} step={0.1} disabled={busy} error={problemText(scaleProblem)} hint={t('cad.imp.scaleHint')} placeholder="—" />
        <NumField label={t('cad.imp.baseZ')} unit="mm" value={baseZ} onChange={setBaseZ} onClear={() => setBaseZ(null)} step={1} disabled={busy} error={problemText(baseProblem)} />
      </div>
      <ErrorNotice error={error} />
      {isApiError(error) && cadErrorFamily(error.code) !== 'other' && (
        <CadErrorDetails code={error.code} details={error.details} action={<p className="small">{t('cad.imp.needsReconstruct')}</p>} />
      )}
      <div className="cad-actions">
        <button type="submit" className="btn primary" disabled={busy || (touched && !valid)}>
          <Layers aria-hidden /> {busy ? t('common.working') : t('cad.imp.do')}
        </button>
        {aiButton}
        <span className="grow" />
        <button type="button" className="btn ghost" onClick={onBack} disabled={busy}>
          <ArrowLeft aria-hidden /> {t('cad.imp.back')}
        </button>
      </div>
      <p className="faint small">{t('cad.imp.aiNote')}</p>
    </form>
  );
}

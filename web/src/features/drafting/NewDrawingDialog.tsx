// Manual drawing creation: a blank sheet, faithful DWG / DXF re-drawing or an image underlay.
// Text generation belongs to the assistant. The server freezes the project policy into version 1.

import { FilePlus2,FileText,Image as ImageIcon,Upload } from 'lucide-react';
import { useMemo,useState,type ReactNode } from 'react';
import { createDrawingDocument,type CreateMode,type DrawingDocument,type VersionFull } from '../../api/drafting';
import { listFiles,uploadFile } from '../../api/endpoints';
import type { Project,ProjectFile } from '../../api/types';
import { Dialog } from '../../components/ui/Dialog';
import { ErrorNotice } from '../../components/ui/States';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';

const IS_CAD = /\.(dxf|dwg)$/i;
const IS_IMAGE = /\.(png|jpe?g|webp|bmp|tiff?|gif)$/i;
type DialogCreateMode = Exclude<CreateMode, 'text'>;

export function NewDrawingDialog({ project, onClose, onCreated }: { project: Project; onClose: () => void; onCreated: (doc: DrawingDocument<VersionFull>) => void }) {
  const { t } = useI18n();
  const [mode, setMode] = useState<DialogCreateMode>('empty');
  const [title, setTitle] = useState('');
  const [number, setNumber] = useState('');
  const [size, setSize] = useState<'A4' | 'A3' | 'A2' | 'A1' | 'A0'>('A3');
  const [fileVersionId, setFileVersionId] = useState('');
  const [upload, setUpload] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const files = useResource((signal) => listFiles(project.id, {}, { signal }), [project.id], { enabled: mode === 'file' || mode === 'image' });
  const choices = useMemo(() => {
    const re = mode === 'image' ? IS_IMAGE : IS_CAD;
    return (files.data ?? []).filter((f: ProjectFile) => !f.archived_at && f.versions.length && re.test(f.versions[0].original_name || f.name));
  }, [files.data, mode]);

  const needsFile = mode === 'file' || mode === 'image';
  const ready = title.trim() && (!needsFile || fileVersionId || upload);

  const submit = async () => {
    if (!ready || busy) return;
    setBusy(true);
    setError(null);
    try {
      let fv = fileVersionId;
      if (needsFile && upload) {
        const f = await uploadFile(project.id, { file: upload });
        fv = f.versions[0].id;
      }
      const doc = await createDrawingDocument(project.id, {
        title: title.trim(),
        ...(number.trim() ? { number: number.trim() } : {}),
        mode,
        ...(needsFile ? { file_version_id: fv } : {}),
        ...(mode === 'empty' ? { sheet: { size } } : {}),
      });
      onCreated(doc);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  const modes: { id: DialogCreateMode; icon: ReactNode }[] = [
    { id: 'empty', icon: <FilePlus2 /> },
    { id: 'file', icon: <FileText /> },
    { id: 'image', icon: <ImageIcon /> },
  ];

  return (
    <Dialog
      title={t('dr.new.title')}
      subtitle={t('dr.new.subtitle')}
      size="lg"
      busy={busy}
      onClose={onClose}
      onSubmit={() => void submit()}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={!ready || busy}>
            {busy ? t('common.working') : t('dr.new.create')}
          </button>
        </>
      }
    >
      <div className="stack dr-new">
        <div className="seg dr-new-modes" role="group" aria-label={t('dr.new.mode')}>
          {modes.map((m) => (
            <button key={m.id} type="button" className="seg-btn" aria-pressed={mode === m.id} onClick={() => { setMode(m.id); setFileVersionId(''); setUpload(null); }}>
              {m.icon} {t(`dr.new.mode.${m.id}`)}
            </button>
          ))}
        </div>
        <p className="faint small">{t(`dr.new.mode.${mode}.hint`)}</p>
        <div className="form-grid">
          <label className="field">
            <span>{t('dr.new.docTitle')}</span>
            <input className="input" value={title} onChange={(e) => setTitle(e.target.value)} maxLength={200} data-autofocus />
          </label>
          <label className="field">
            <span>
              {t('dr.new.number')} <span className="opt">{t('dr.new.optional')}</span>
            </span>
            <input className="input mono" value={number} onChange={(e) => setNumber(e.target.value)} maxLength={80} placeholder="DD-0001" />
          </label>
        </div>
        {mode === 'empty' && (
          <label className="field">
            <span>{t('dr.sheet.size')}</span>
            <select className="input" value={size} onChange={(e) => setSize(e.target.value as typeof size)}>
              {['A4', 'A3', 'A2', 'A1', 'A0'].map((s) => (
                <option key={s}>{s}</option>
              ))}
            </select>
          </label>
        )}
        {needsFile && (
          <div className="stack-sm">
            <label className="field">
              <span>{t(mode === 'image' ? 'dr.new.pickImage' : 'dr.new.pickCad')}</span>
              <select className="input" value={fileVersionId} onChange={(e) => { setFileVersionId(e.target.value); setUpload(null); }} disabled={files.loading}>
                <option value="">{files.loading ? t('common.working') : choices.length ? t('dr.new.choose') : t('dr.new.noFiles')}</option>
                {choices.map((f) => (
                  <option key={f.id} value={f.versions[0].id}>
                    {f.name} · v{f.versions[0].version}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>
                <Upload aria-hidden /> {t('dr.new.upload')}
              </span>
              <input className="input file" type="file" accept={mode === 'image' ? 'image/*' : '.dxf,.dwg'} onChange={(e) => { setUpload(e.target.files?.[0] ?? null); setFileVersionId(''); }} />
            </label>
          </div>
        )}

        {error != null && <ErrorNotice error={error} />}
      </div>
    </Dialog>
  );
}

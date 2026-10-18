// The stored-file dialogs shared by the files panel and the resources detail: upload a new version
// (POST /files/{id}/versions) and rename / relink a part (PATCH /files/{id}). The caller closes the dialog in `onDone`;
// nothing changes on screen before the server answers.

import { Upload } from 'lucide-react';
import { useState, type ReactNode } from 'react';
import { addFileVersion, listParts, patchFile } from '../../api/endpoints';
import type { FilePatch, ProjectFile, UUID } from '../../api/types';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { Dialog } from '../ui/Dialog';
import { Field } from '../ui/Form';
import { ErrorNotice } from '../ui/States';

const NOTE_MAX = 2000;
const NAME_MAX = 300;

export function NewFileVersionDialog({
  file,
  intro,
  noteRequired = false,
  onClose,
  onDone,
}: {
  file: Pick<ProjectFile, 'id' | 'name'>;
  /** A line above the form (what the new version keeps). */
  intro?: ReactNode;
  /** The change note must be filled in. */
  noteRequired?: boolean;
  onClose: () => void;
  onDone: (f: ProjectFile) => void;
}) {
  const { t, fmt } = useI18n();
  const [picked, setPicked] = useState<File | null>(null);
  const [note, setNote] = useState('');
  const [tried, setTried] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const noteMissing = noteRequired && !note.trim();
  const submit = async () => {
    setTried(true);
    if (!picked || noteMissing) return;
    setBusy(true);
    setError(null);
    try {
      onDone(await addFileVersion(file.id, { file: picked, note: note.trim() }));
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog
      title={t('files.newVersion.title', { name: file.name })}
      onClose={onClose}
      busy={busy}
      onSubmit={() => void submit()}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={busy || !picked}>
            <Upload /> {busy ? t('files.upload.uploading') : t('files.upload.submit')}
          </button>
        </>
      }
    >
      {intro && (
        <div className="callout info" role="note">
          {intro}
        </div>
      )}
      <div className="form-grid one">
        <Field label={t('files.newVersion.file')}>
          <input className="input file" type="file" onChange={(e) => setPicked(e.target.files?.[0] ?? null)} disabled={busy} />
        </Field>
        {picked && (
          <p className="faint small mono">
            {picked.name} · {fmt.bytes(picked.size)}
          </p>
        )}
        <Field label={t('files.upload.note')} optional={!noteRequired} error={tried && noteMissing ? t('validation.required', { field: t('files.upload.note') }) : null}>
          <input className="input" value={note} maxLength={NOTE_MAX} onChange={(e) => setNote(e.target.value)} placeholder={t('files.upload.notePlaceholder')} disabled={busy} />
        </Field>
      </div>
      <ErrorNotice error={error} />
    </Dialog>
  );
}

export function FileMetaDialog({
  file,
  partsOf,
  onClose,
  onDone,
}: {
  file: Pick<ProjectFile, 'id' | 'name' | 'part_id'>;
  /** Also offer (re)linking a part of this project; without it the dialog only renames. */
  partsOf?: UUID;
  onClose: () => void;
  onDone: (f: ProjectFile) => void;
}) {
  const { t } = useI18n();
  const parts = useResource((signal) => listParts(partsOf!, {}, { signal }), [partsOf], { enabled: !!partsOf });
  const [name, setName] = useState(file.name);
  const [partId, setPartId] = useState(file.part_id ?? '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const nameErr = !name.trim() ? t('validation.required', { field: t('files.rename.field') }) : null;
  // Only what changed; the API links or relinks a part but cannot unlink one (no "none" once linked).
  const patch: FilePatch = {
    ...(name.trim() !== file.name ? { name: name.trim() } : {}),
    ...(partId && partId !== (file.part_id ?? '') ? { part_id: partId } : {}),
  };
  const changed = Object.keys(patch).length > 0;
  const submit = async () => {
    if (nameErr || !changed) return;
    setBusy(true);
    setError(null);
    try {
      onDone(await patchFile(file.id, patch));
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  const choices = (parts.data ?? []).filter((p) => !p.archived_at || p.id === file.part_id);
  return (
    <Dialog
      title={t(partsOf ? 'rd.meta.title' : 'files.rename.title')}
      onClose={onClose}
      busy={busy}
      size="sm"
      onSubmit={() => void submit()}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={busy || !!nameErr || !changed}>
            {busy ? t('common.saving') : t('common.save')}
          </button>
        </>
      }
    >
      <div className="form-grid one">
        <Field label={t('files.rename.field')} error={nameErr}>
          <input className="input mono" value={name} maxLength={NAME_MAX} onChange={(e) => setName(e.target.value)} onFocus={(e) => e.currentTarget.select()} disabled={busy} />
        </Field>
        {partsOf && (
          <Field label={t('rd.meta.part')} optional hint={file.part_id ? t('rd.meta.cannotUnlink') : undefined}>
            <select className="select" value={partId} onChange={(e) => setPartId(e.target.value)} disabled={busy || parts.initial}>
              {!file.part_id && <option value="">{t('rd.meta.noPart')}</option>}
              {choices.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.number} · {p.name}
                </option>
              ))}
            </select>
          </Field>
        )}
      </div>
      {parts.error ? <ErrorNotice error={parts.error} onRetry={parts.reload} /> : null}
      {partsOf && <p className="faint small">{t('rd.meta.note')}</p>}
      <ErrorNotice error={error} />
    </Dialog>
  );
}

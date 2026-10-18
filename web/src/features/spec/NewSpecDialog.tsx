// "New engineering specification": paste a long description (no chat-size cap) and / or pick project files, choose Standard / Turbo.
// The policy is chosen explicitly every time (the project default preselected, never silently replaced); the latest version of each picked file is
// pinned by id. The server answers with the new spec whose first version is extracting.

import { FileText } from 'lucide-react';
import { useState } from 'react';
import { listFiles } from '../../api/endpoints';
import type { ExecutionPolicy } from '../../api/engineering';
import { createSpec,type EngineeringSpec } from '../../api/spec';
import { Dialog } from '../../components/ui/Dialog';
import { Field } from '../../components/ui/Form';
import { ErrorNotice } from '../../components/ui/States';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';

/** Server budget (W0 contract §7): chunks of about 12,000 characters, at most 40 per version. */
export const CHUNK_CHARS = 12_000;
export const MAX_CHUNKS = 40;

export function NewSpecDialog({ projectId, onClose, onCreated }: { projectId: string; defaultPolicy?: ExecutionPolicy; onClose: () => void; onCreated: (spec: EngineeringSpec, versionId: string) => void }) {
  const { t, fmt } = useI18n();
  const files = useResource((signal) => listFiles(projectId, {}, { signal }), [projectId]);
  const [title, setTitle] = useState('');
  const [text, setText] = useState('');
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [tried, setTried] = useState(false);

  const chars = Array.from(text).length;
  const chunks = chars ? Math.max(1, Math.ceil(chars / CHUNK_CHARS)) : 0;
  const empty = !text.trim() && picked.size === 0;

  const toggle = (versionId: string) =>
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(versionId)) next.delete(versionId);
      else next.add(versionId);
      return next;
    });

  const submit = async () => {
    setTried(true);
    if (empty || busy) return;
    setBusy(true);
    setError(null);
    try {
      const out = await createSpec(projectId, { title: title.trim(), text, file_version_ids: [...picked] });
      onCreated(out, out.version.id);
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  };

  const list = (files.data ?? []).filter((f) => !f.archived_at && f.versions.length);
  return (
    <Dialog
      title={t('spec.new.title')}
      subtitle={t('spec.new.subtitle')}
      size="lg"
      busy={busy}
      onClose={onClose}
      onSubmit={() => void submit()}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={busy}>
            {t('spec.new.submit')}
          </button>
        </>
      }
    >
      <div className="stack">
        <Field label={t('spec.new.name')} optional>
          <input className="input" value={title} maxLength={200} placeholder={t('spec.new.namePlaceholder')} onChange={(e) => setTitle(e.target.value)} />
        </Field>
        <Field
          label={t('spec.new.text')}
          hint={chunks > MAX_CHUNKS ? t('spec.new.textOver', { chars: fmt.number(chars), chunks, max: MAX_CHUNKS }) : t('spec.new.textHint', { chars: fmt.number(chars), chunks, max: MAX_CHUNKS })}
          error={tried && empty ? t('spec.new.needSource') : undefined}
        >
          <textarea className="input spec-textarea" value={text} placeholder={t('spec.new.textPlaceholder')} onChange={(e) => setText(e.target.value)} />
        </Field>
        <div className="field">
          <span>
            {t('spec.new.files')} <span className="faint small">· {t('spec.new.filesSelected', { n: picked.size })}</span>
          </span>
          <div className="spec-files" role="group" aria-label={t('spec.new.files')}>
            {files.error ? <ErrorNotice error={files.error} onRetry={files.reload} /> : null}
            {!files.initial && !list.length ? <p className="faint small">{t('spec.new.filesEmpty')}</p> : null}
            {list.map((f) => {
              const v = f.versions[0];
              return (
                <label key={f.id} className="spec-file">
                  <input type="checkbox" checked={picked.has(v.id)} onChange={() => toggle(v.id)} />
                  <FileText aria-hidden className="inline-ico" />
                  <span className="name" title={v.original_name}>
                    {f.name}
                  </span>
                  <span className="mono small faint">v{v.version}</span>
                  <span className="small faint">{fmt.bytes(v.size)}</span>
                </label>
              );
            })}
          </div>
          <span className="field-hint">{t('spec.new.filesHint')}</span>
        </div>
        {null}
        {error ? <ErrorNotice error={error} /> : null}
      </div>
    </Dialog>
  );
}

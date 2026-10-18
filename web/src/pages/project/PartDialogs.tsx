// Create / edit part and copy-to-project dialogs (used by the parts list and the part workspace).

import { Info } from 'lucide-react';
import { useMemo, useState } from 'react';
import { isApiError } from '../../api/client';
import { copyPart, createPart, listProjects, updatePart } from '../../api/endpoints';
import type { Member, Part, Project } from '../../api/types';
import { copyTargets } from '../../auth/permissions';
import { Dialog } from '../../components/ui/Dialog';
import { Field, TagInput } from '../../components/ui/Form';
import { ErrorNotice, Skeleton } from '../../components/ui/States';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { DEFAULT_MATERIAL, KNOWN_MATERIALS, isKnownMaterial } from '../../lib/catalog';

const OTHER = '__other__';

export function PartDialog({
  mode,
  project,
  part,
  members,
  folders,
  meId,
  onClose,
  onSaved,
}: {
  mode: 'create' | 'edit';
  project: Project;
  part?: Part;
  members: Member[] | undefined;
  folders: string[];
  meId: string;
  onClose: () => void;
  onSaved: (p: Part) => void;
}) {
  const { t } = useI18n();
  const initialMaterial = part?.material ?? DEFAULT_MATERIAL;
  const [number, setNumber] = useState(part?.number ?? '');
  const [name, setName] = useState(part?.name ?? '');
  const [materialChoice, setMaterialChoice] = useState(isKnownMaterial(initialMaterial) ? initialMaterial : OTHER);
  const [customMaterial, setCustomMaterial] = useState(isKnownMaterial(initialMaterial) ? '' : initialMaterial);
  const [tags, setTags] = useState<string[]>(part?.tags ?? []);
  const [folder, setFolder] = useState(part?.folder ?? '');
  const [description, setDescription] = useState(part?.description ?? '');
  const [ownerId, setOwnerId] = useState(part?.owner_id ?? meId);
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const material = materialChoice === OTHER ? customMaterial.trim() : materialChoice;
  const taken = isApiError(error) && error.code === 'part_number_taken';
  const numberErr = !number.trim() ? t('validation.required', { field: t('parts.field.number') }) : number.length > 80 ? t('validation.maxLength', { field: t('parts.field.number'), max: 80 }) : taken ? t('error.part_number_taken') : null;
  const nameErr = !name.trim() ? t('validation.required', { field: t('parts.field.name') }) : null;
  const materialErr = !material ? t('validation.required', { field: t('parts.field.material') }) : material.length > 40 ? t('validation.maxLength', { field: t('parts.field.material'), max: 40 }) : null;

  const submit = async () => {
    setTouched(true);
    if ((numberErr && !taken) || nameErr || materialErr) return;
    setBusy(true);
    setError(null);
    try {
      const body = { name: name.trim(), material, tags, folder: folder.trim(), description: description.trim(), owner_id: ownerId || null };
      const saved = mode === 'create' ? await createPart(project.id, { number: number.trim(), ...body }) : await updatePart(part!.id, body);
      onSaved(saved);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  const folderListId = `folders-${project.id}`;
  return (
    <Dialog
      title={mode === 'create' ? t('parts.create.title') : t('part.edit.title', { number: part!.number })}
      subtitle={<span className="mono">{project.code}</span>}
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
            {busy ? (mode === 'create' ? t('common.creating') : t('common.saving')) : mode === 'create' ? t('common.create') : t('common.save')}
          </button>
        </>
      }
    >
      <div className="form-grid">
        {mode === 'create' ? (
          <Field label={t('parts.field.number')} hint={t('parts.field.numberHint')} error={touched || taken ? numberErr : null}>
            <input
              className="input mono"
              value={number}
              maxLength={80}
              spellCheck={false}
              onChange={(e) => {
                setNumber(e.target.value);
                if (taken) setError(null);
              }}
            />
          </Field>
        ) : (
          <Field label={t('parts.field.number')}>
            <input className="input mono" value={part!.number} disabled readOnly />
          </Field>
        )}
        <Field label={t('parts.field.name')} error={touched ? nameErr : null}>
          <input className="input" value={name} maxLength={200} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label={t('parts.field.material')} hint={t('parts.field.materialHint')} error={touched && materialChoice !== OTHER ? materialErr : null}>
          <select className="select mono" value={materialChoice} onChange={(e) => setMaterialChoice(e.target.value)}>
            {KNOWN_MATERIALS.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
            <option value={OTHER}>{t('parts.field.materialCustomOption')}</option>
          </select>
        </Field>
        {materialChoice === OTHER && (
          <Field label={t('parts.field.materialCustom')} error={touched ? materialErr : null}>
            <input className="input mono" value={customMaterial} maxLength={40} onChange={(e) => setCustomMaterial(e.target.value)} />
          </Field>
        )}
        <Field label={t('parts.field.owner')}>
          <select className="select" value={ownerId ?? ''} onChange={(e) => setOwnerId(e.target.value)} disabled={!members}>
            {(members ?? []).map((m) => (
              <option key={m.user.id} value={m.user.id}>
                {m.user.name} · {t(`role.${m.role}`)}
              </option>
            ))}
            {!members && <option value={ownerId ?? ''}>{t('common.loading')}</option>}
          </select>
        </Field>
        <Field label={t('parts.field.folder')} hint={t('parts.field.folderHint')} optional>
          <input className="input mono" value={folder} maxLength={200} list={folderListId} onChange={(e) => setFolder(e.target.value)} placeholder="fixtures/jaws" />
        </Field>
        <datalist id={folderListId}>
          {folders.map((f) => (
            <option key={f} value={f} />
          ))}
        </datalist>
        <Field label={t('parts.field.tags')} hint={t('parts.field.tagsHint')} optional>
          <TagInput value={tags} onChange={setTags} placeholder={t('parts.field.tagsPlaceholder')} />
        </Field>
        <Field label={t('parts.field.description')} optional className="span-2">
          <textarea className="input" rows={3} maxLength={5000} value={description} onChange={(e) => setDescription(e.target.value)} />
        </Field>
      </div>
      <ErrorNotice error={taken ? null : error} />
    </Dialog>
  );
}

export function CopyPartDialog({ part, project, onClose, onCopied }: { part: Part; project: Project; onClose: () => void; onCopied: (p: Part, target: Project) => void }) {
  const { t } = useI18n();
  const res = useResource((signal) => listProjects({}, { signal }), []);
  const targets = useMemo(() => copyTargets(res.data ?? []), [res.data]);
  const [targetId, setTargetId] = useState<string>('');
  const [number, setNumber] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const chosen = targets.find((p) => p.id === (targetId || targets.find((x) => x.id !== project.id)?.id || targets[0]?.id));

  const submit = async () => {
    if (!chosen) return;
    setBusy(true);
    setError(null);
    try {
      const copy = await copyPart(part.id, { target_project_id: chosen.id, number: number.trim() || undefined });
      onCopied(copy, chosen);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      title={t('parts.copy.title', { number: part.number })}
      onClose={onClose}
      busy={busy}
      onSubmit={() => void submit()}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={busy || !chosen}>
            {busy ? t('common.working') : t('parts.copy.submit')}
          </button>
        </>
      }
    >
      <div className="callout info">
        <Info aria-hidden />
        <span>{t('parts.copy.explain')}</span>
      </div>
      {res.initial && !res.error ? (
        <div className="stack-sm">
          <Skeleton h={30} />
          <Skeleton h={30} />
        </div>
      ) : res.error ? (
        <ErrorNotice error={res.error} onRetry={res.reload} />
      ) : !targets.length ? (
        <p className="muted">{t('parts.copy.noTargets')}</p>
      ) : (
        <div className="form-grid one">
          <Field label={t('parts.copy.target')}>
            <select className="select" value={chosen?.id ?? ''} onChange={(e) => setTargetId(e.target.value)}>
              {targets.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.id === project.id ? t('parts.copy.thisProject', { code: p.code }) : `${p.code} · ${p.name}`}
                </option>
              ))}
            </select>
          </Field>
          <Field label={t('parts.copy.number')} hint={t('parts.copy.numberHint', { number: part.number })} optional>
            <input className="input mono" value={number} maxLength={80} onChange={(e) => setNumber(e.target.value)} placeholder={chosen?.id === project.id ? `${part.number}-COPY` : part.number} />
          </Field>
        </div>
      )}
      <ErrorNotice error={error} />
    </Dialog>
  );
}

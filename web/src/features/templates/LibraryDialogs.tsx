// Public template library dialogs: share a published project version to the
// library (new public template or a new version of one you maintain), and import
// a public version into a project (contract §2.8). Library changes never change a
// project: projects adopt a version explicitly.

import { Download, Info, Share2 } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { addPublicTemplateVersion, getPublicTemplate, importPublicTemplate, listProjects, listTemplateLibrary, shareTemplateToLibrary } from '../../api/endpoints';
import type { PublicTemplateDetail, TemplateVersion, UUID, Workflow } from '../../api/types';
import { Dialog } from '../../components/ui/Dialog';
import { Field } from '../../components/ui/Form';
import { ErrorNotice } from '../../components/ui/States';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { TPL_ERRORS } from './common';
import { LimitedTextarea } from '../../components/ui/LimitedText';

export function ShareDialog({
  workflow,
  versions,
  defaultVersionId,
  onClose,
  onShared,
}: {
  workflow: Workflow;
  /** Project versions, newest first. */
  versions: TemplateVersion[];
  defaultVersionId?: UUID;
  onClose: () => void;
  onShared: (pt: PublicTemplateDetail) => void;
}) {
  const { t, fmt } = useI18n();
  const published = versions.filter((v) => v.status === 'published');
  const [versionId, setVersionId] = useState<UUID>(defaultVersionId && published.some((v) => v.id === defaultVersionId) ? defaultVersionId : published[0]?.id ?? '');
  const [mode, setMode] = useState<'new' | 'append'>('new');
  const [name, setName] = useState(workflow.name);
  const [description, setDescription] = useState(workflow.description);
  const [targetId, setTargetId] = useState<UUID>('');
  const [notes, setNotes] = useState('');
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const library = useResource((signal) => listTemplateLibrary({}, { signal }), []);
  const mine = (library.data ?? []).filter((p) => p.can_manage);

  const nameErr = mode === 'new' && !name.trim() ? t('tpl.share.nameRequired') : null;
  const targetErr = mode === 'append' && !targetId ? t('tpl.share.targetRequired') : null;
  const versionErr = !versionId ? t('tpl.share.versionRequired') : null;

  const submit = async () => {
    setTouched(true);
    if (nameErr || targetErr || versionErr) return;
    setBusy(true);
    setError(null);
    try {
      const pt =
        mode === 'new'
          ? await shareTemplateToLibrary({ template_version_id: versionId, name: name.trim(), description: description.trim() })
          : await addPublicTemplateVersion(targetId, { template_version_id: versionId, notes: notes.trim() });
      onShared(pt);
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  };

  return (
    <Dialog
      title={t('tpl.share.title')}
      subtitle={workflow.name}
      busy={busy}
      onClose={onClose}
      onSubmit={() => void submit()}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={busy || !published.length}>
            <Share2 /> {busy ? t('common.working') : t('tpl.share.submit')}
          </button>
        </>
      }
    >
      <div className="stack">
        <div className="callout info slim">
          <Info aria-hidden />
          <span>{t('tpl.share.explain')}</span>
        </div>
        {!published.length ? (
          <p className="faint">{t('tpl.share.noVersions')}</p>
        ) : (
          <>
            <Field label={t('tpl.share.version')} error={touched ? versionErr : null}>
              <select className="select" value={versionId} onChange={(e) => setVersionId(e.target.value)}>
                {published.map((v) => (
                  <option key={v.id} value={v.id}>
                    v{v.version_no} · {fmt.date(v.published_at)}
                    {v.notes ? ` · ${v.notes.slice(0, 50)}` : ''}
                  </option>
                ))}
              </select>
            </Field>
            <fieldset className="tpl-radio-row">
              <legend className="sr-only">{t('tpl.share.mode')}</legend>
              <label className="check">
                <input type="radio" name="tpl-share-mode" checked={mode === 'new'} onChange={() => setMode('new')} />
                <span>{t('tpl.share.modeNew')}</span>
              </label>
              <label className="check">
                <input type="radio" name="tpl-share-mode" checked={mode === 'append'} onChange={() => setMode('append')} disabled={!mine.length} />
                <span>{t('tpl.share.modeAppend')}</span>
              </label>
            </fieldset>
            {mode === 'new' ? (
              <div className="form-grid one">
                <Field label={t('tpl.share.name')} error={touched ? nameErr : null}>
                  <input className="input" value={name} maxLength={200} onChange={(e) => setName(e.target.value)} />
                </Field>
                <Field label={t('wf.field.description')} optional>
                  <LimitedTextarea className="input" rows={3} limit="description" value={description} onChange={(e) => setDescription(e.target.value)} />
                </Field>
              </div>
            ) : (
              <div className="form-grid one">
                <Field label={t('tpl.share.target')} error={touched ? targetErr : null} hint={t('tpl.share.targetHint')}>
                  <select className="select" value={targetId} onChange={(e) => setTargetId(e.target.value)}>
                    <option value="">{t('tpl.share.chooseTarget')}</option>
                    {mine.map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.name}
                        {p.latest_version ? ` · v${p.latest_version.version_no}` : ''}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field label={t('tpl.publish.notes')} optional>
                  <LimitedTextarea className="input" rows={2} limit="description" value={notes} onChange={(e) => setNotes(e.target.value)} />
                </Field>
              </div>
            )}
          </>
        )}
        {library.error ? <ErrorNotice error={library.error} onRetry={library.reload} /> : null}
        <ErrorNotice error={error} overrides={TPL_ERRORS} />
      </div>
    </Dialog>
  );
}

/**
 * Import a public template version into a project: a new project template whose
 * v1 is that public version. Either the project or the public template may be
 * fixed by the caller (library page vs project templates tab).
 */
export function ImportDialog({
  projectId: fixedProjectId,
  template: fixedTemplate,
  templateId: fixedTemplateId,
  defaultVersionId,
  onClose,
  onImported,
}: {
  projectId?: UUID;
  /** Fixed public template (already loaded) … */
  template?: PublicTemplateDetail;
  /** … or its id (loaded here). Without either, the dialog lists the library. */
  templateId?: UUID;
  defaultVersionId?: UUID;
  onClose: () => void;
  onImported: (w: Workflow) => void;
}) {
  const { t, fmt } = useI18n();
  const fixedId = fixedTemplate?.id ?? fixedTemplateId;
  const [projectId, setProjectId] = useState<UUID>(fixedProjectId ?? '');
  const [templateId, setTemplateId] = useState<UUID>(fixedId ?? '');
  const [versionId, setVersionId] = useState<UUID>(defaultVersionId ?? '');
  const [name, setName] = useState(fixedTemplate?.name ?? '');
  const [nameEdited, setNameEdited] = useState(false);
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const projects = useResource((signal) => listProjects({}, { signal }), [], { enabled: !fixedProjectId });
  const library = useResource((signal) => listTemplateLibrary({}, { signal }), [], { enabled: !fixedId });
  const detail = useResource((signal) => getPublicTemplate(templateId, { signal }), [templateId], { enabled: !fixedTemplate && !!templateId });
  const tpl = fixedTemplate ?? (detail.data?.id === templateId ? detail.data : undefined);
  useEffect(() => {
    if (tpl && !nameEdited && !name) setName(tpl.name);
  }, [tpl, nameEdited, name]);
  const usable = useMemo(() => (tpl?.versions ?? []).filter((v) => v.status !== 'deprecated').sort((a, b) => b.version_no - a.version_no), [tpl]);
  const version = usable.find((v) => v.id === versionId) ?? usable[0];
  // Viewers cannot hold templates; the server re-checks the template permission.
  const targets = (projects.data ?? []).filter((p) => p.status !== 'archived' && p.my_role && p.my_role !== 'viewer');

  const projectErr = !projectId ? t('tpl.import.projectRequired') : null;
  const versionErr = !version ? t('tpl.import.versionRequired') : null;
  const submit = async () => {
    setTouched(true);
    if (projectErr || versionErr || !version) return;
    setBusy(true);
    setError(null);
    try {
      onImported(await importPublicTemplate(projectId, { public_template_version_id: version.id, name: name.trim() || undefined }));
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  };

  return (
    <Dialog
      title={t('tpl.import.title')}
      subtitle={fixedId ? tpl?.name : undefined}
      busy={busy}
      onClose={onClose}
      onSubmit={() => void submit()}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={busy}>
            <Download /> {busy ? t('common.working') : t('tpl.import.submit')}
          </button>
        </>
      }
    >
      <div className="stack">
        <div className="callout info slim">
          <Info aria-hidden />
          <span>{t('tpl.import.explain')}</span>
        </div>
        <div className="form-grid one">
          {!fixedProjectId && (
            <Field label={t('tpl.import.project')} error={touched ? projectErr : null} hint={t('tpl.import.projectHint')}>
              <select className="select" value={projectId} onChange={(e) => setProjectId(e.target.value)} disabled={!projects.data}>
                <option value="">{projects.data ? (targets.length ? t('tpl.import.chooseProject') : t('tpl.import.noProjects')) : t('common.loading')}</option>
                {targets.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.code} · {p.name}
                  </option>
                ))}
              </select>
            </Field>
          )}
          {!fixedId && (
            <Field label={t('tpl.import.template')}>
              <select
                className="select"
                value={templateId}
                onChange={(e) => {
                  setTemplateId(e.target.value);
                  setVersionId('');
                  const next = library.data?.find((p) => p.id === e.target.value);
                  if (!nameEdited) setName(next?.name ?? '');
                }}
                disabled={!library.data}
              >
                <option value="">{library.data ? (library.data.length ? t('tpl.import.chooseTemplate') : t('tpl.lib.empty')) : t('common.loading')}</option>
                {(library.data ?? []).map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                    {p.source_project ? ` · ${p.source_project.code}` : ''}
                  </option>
                ))}
              </select>
            </Field>
          )}
          <Field label={t('tpl.import.version')} error={touched && tpl ? versionErr : null}>
            <select className="select" value={version?.id ?? ''} onChange={(e) => setVersionId(e.target.value)} disabled={!tpl}>
              {!tpl && <option value="">{t('tpl.start.chooseTemplateFirst')}</option>}
              {tpl && !usable.length && <option value="">{t('tpl.import.noVersions')}</option>}
              {usable.map((v) => (
                <option key={v.id} value={v.id}>
                  v{v.version_no} · {fmt.date(v.published_at)}
                  {v.notes ? ` · ${v.notes.slice(0, 50)}` : ''}
                </option>
              ))}
            </select>
          </Field>
          <Field label={t('tpl.import.name')} hint={t('tpl.import.nameHint')}>
            <input
              className="input"
              value={name}
              maxLength={200}
              onChange={(e) => {
                setName(e.target.value);
                setNameEdited(true);
              }}
            />
          </Field>
        </div>
        {projects.error ? <ErrorNotice error={projects.error} onRetry={projects.reload} /> : null}
        {library.error ? <ErrorNotice error={library.error} onRetry={library.reload} /> : null}
        {detail.error ? <ErrorNotice error={detail.error} onRetry={detail.reload} /> : null}
        <ErrorNotice error={error} overrides={TPL_ERRORS} />
      </div>
    </Dialog>
  );
}

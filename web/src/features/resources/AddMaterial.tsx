import { useFeature } from '../../api/capabilities';
// "Add material" (handoff §4.1): only the actions the user can run in at least one accessible active project — upload a
// file (design), new engineering spec (any workflow role), new 2D drawing (design), new process module (process).
// Every form names its save location explicitly: the scope's project by default in a project scope, no default across
// projects. Spec / drawing / module creation
// reuse their existing dialogs and still open their editors afterwards; an upload opens the new item's detail.

import '../drafting/drafting.css';
import '../spec/spec.css';
import './detail.css';
import { Blocks, ChevronDown, FilePlus2, Plus, ScrollText, Upload } from 'lucide-react';
import { useId, useMemo, useState, type ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import { listParts, uploadFile } from '../../api/endpoints';
import { specPath } from '../../api/spec';
import { FILE_KINDS, type FileKind, type Project, type UUID } from '../../api/types';
import { paths } from '../../app/nav';
import { Dialog } from '../../components/ui/Dialog';
import { Field } from '../../components/ui/Form';
import { Menu, type MenuItem } from '../../components/ui/Menu';
import { ErrorNotice } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { guessKind } from '../../lib/catalog';
import { NewDrawingDialog } from '../drafting/NewDrawingDialog';
import { ModuleDialog } from '../modules/ModuleDialog';
import { NewSpecDialog } from '../spec/NewSpecDialog';
import { ADD_ACTIONS, defaultSaveLocation, saveTargets, type AddAction } from './detailModel';
import type { ViewScope } from './types';

export interface AddMaterialMenuProps {
  scope: ViewScope;
  /** Accessible projects; the save location is one of them. */
  projects: readonly Project[];
  /** After a real save: the page switches to that project's scope and opens the item when its key is known. */
  onCreated: (r: { projectId: UUID; itemKey?: string }) => void;
}

const ACTION_ICON: Record<AddAction, ReactNode> = {
  upload: <Upload />,
  spec: <ScrollText />,
  drawing: <FilePlus2 />,
  module: <Blocks />,
};

const NOTE_MAX = 2000;

/** The explicit "save location" select lists projects where the action is allowed. */
function SaveLocationField({ scope, targets, value, onChange, error, disabled }: { scope: ViewScope; targets: readonly Project[]; value: string; onChange: (id: string) => void; error?: string | null; disabled?: boolean }) {
  const { t } = useI18n();
  const scopeProjectDenied = scope.kind === 'project' && !targets.some((p) => p.id === scope.projectId);
  const hint =
    scope.kind === 'all'
        ? t('rd.save.allHint')
        : scopeProjectDenied
          ? t('rd.save.scopeNoRole')
          : t('rd.save.projectHint');
  return (
    <Field label={t('rd.save.label')} hint={hint} error={error}>
      <select className="select" value={value} onChange={(e) => onChange(e.target.value)} disabled={disabled} required>
        {!value && (
          <option value="" disabled>
            {t('rd.save.choose')}
          </option>
        )}
        {targets.map((p) => (
          <option key={p.id} value={p.id}>
            {p.name} · {p.code}
          </option>
        ))}
      </select>
    </Field>
  );
}

// ─── upload ─────────────────────────────────────────────────────────────────

function UploadMaterialDialog({ scope, targets, onClose, onUploaded }: { scope: ViewScope; targets: readonly Project[]; onClose: () => void; onUploaded: (projectId: UUID, fileId: UUID, name: string) => void }) {
  const { t, fmt } = useI18n();
  const [projectId, setProjectId] = useState<string>(() => defaultSaveLocation(scope, targets));
  const [file, setFile] = useState<File | null>(null);
  const [kind, setKind] = useState<FileKind>('other');
  const [partId, setPartId] = useState('');
  const [note, setNote] = useState('');
  const [tried, setTried] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const parts = useResource((signal) => listParts(projectId, {}, { signal }), [projectId], { enabled: !!projectId });
  const partChoices = (parts.data ?? []).filter((p) => !p.archived_at);

  const submit = async () => {
    setTried(true);
    if (!projectId || !file) return;
    setBusy(true);
    setError(null);
    try {
      const f = await uploadFile(projectId, { file, kind, part_id: partId || null, note: note.trim() });
      onUploaded(projectId, f.id, f.name);
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  };

  return (
    <Dialog
      title={t('rd.upload.title')}
      onClose={onClose}
      busy={busy}
      onSubmit={() => void submit()}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={busy}>
            <Upload /> {busy ? t('files.upload.uploading') : t('rd.upload.submit')}
          </button>
        </>
      }
    >
      <div className="form-grid one">
        <SaveLocationField
          scope={scope}
          targets={targets}
          value={projectId}
          onChange={(id) => {
            setProjectId(id);
            setPartId('');
          }}
          error={tried && !projectId ? t('rd.save.required') : null}
          disabled={busy}
        />
        <Field label={t('rd.upload.file')} error={tried && !file ? t('rd.upload.needFile') : null}>
          <input
            className="input file"
            type="file"
            disabled={busy}
            onChange={(e) => {
              const picked = e.target.files?.[0] ?? null;
              setFile(picked);
              if (picked) setKind(guessKind(picked.name));
            }}
          />
        </Field>
        {file && (
          <p className="faint small mono">
            {file.name} · {fmt.bytes(file.size)}
          </p>
        )}
        <Field label={t('rd.upload.kind')}>
          <select className="select" value={kind} onChange={(e) => setKind(e.target.value as FileKind)} disabled={busy}>
            {FILE_KINDS.map((k) => (
              <option key={k} value={k}>
                {t(`fileKind.${k}`)}
              </option>
            ))}
          </select>
        </Field>
        <Field label={t('rd.upload.part')} optional hint={!projectId ? t('rd.upload.partNeedsProject') : undefined}>
          <select className="select" value={partId} onChange={(e) => setPartId(e.target.value)} disabled={busy || !projectId || parts.initial}>
            <option value="">{t('rd.upload.noPart')}</option>
            {partChoices.map((p) => (
              <option key={p.id} value={p.id}>
                {p.number} · {p.name}
              </option>
            ))}
          </select>
        </Field>
        {parts.error ? <ErrorNotice error={parts.error} onRetry={parts.reload} /> : null}
        <Field label={t('rd.upload.note')} optional>
          <input className="input" value={note} maxLength={NOTE_MAX} onChange={(e) => setNote(e.target.value)} placeholder={t('rd.upload.notePlaceholder')} disabled={busy} />
        </Field>
      </div>
      <p className="faint small">{t('rd.upload.newItem')}</p>
      <ErrorNotice error={error} />
    </Dialog>
  );
}

// ─── spec / drawing / module: save location, then the existing dialog ───────

function CreateFlow({ action, scope, targets, onClose, onCreated }: { action: Exclude<AddAction, 'upload'>; scope: ViewScope; targets: readonly Project[]; onClose: () => void; onCreated: AddMaterialMenuProps['onCreated'] }) {
  const { t } = useI18n();
  const toast = useToast();
  const navigate = useNavigate();
  const [projectId, setProjectId] = useState<string>(() => defaultSaveLocation(scope, targets));
  const [tried, setTried] = useState(false);
  const [stage, setStage] = useState<'location' | 'form'>('location');
  const project = targets.find((p) => p.id === projectId) ?? null;
  const title = t(`rd.add.${action}`);

  if (stage === 'form' && project) {
    if (action === 'spec')
      return (
        <NewSpecDialog
          projectId={project.id}
          defaultPolicy={project.default_execution_policy === 'turbo' ? 'turbo' : 'standard'}
          onClose={onClose}
          onCreated={(spec, versionId) => {
            toast(t('spec.new.created'));
            onCreated({ projectId: project.id, itemKey: `spec:${spec.id}` });
            onClose();
            navigate(specPath(spec.id, versionId));
          }}
        />
      );
    if (action === 'drawing')
      return (
        <NewDrawingDialog
          project={project}
          onClose={onClose}
          onCreated={(doc) => {
            onCreated({ projectId: project.id, itemKey: `drawing_document:${doc.id}` });
            onClose();
            navigate(`/drafting/${doc.id}`);
          }}
        />
      );
    return (
      <ModuleDialog
        projectId={project.id}
        onClose={onClose}
        onCreated={(v) => {
          toast(t('mod.created'));
          onCreated({ projectId: project.id, itemKey: v ? `asset:${v.asset_id}` : undefined });
          onClose();
          if (v) navigate(paths.asset(v.asset_id, v.id));
        }}
      />
    );
  }

  const locationError = tried && !project ? t('rd.save.required') : null;
  return (
    <Dialog
      title={title}
      subtitle={t('rd.create.subtitle')}
      size="sm"
      onClose={onClose}
      onSubmit={() => {
        setTried(true);
        if (project) setStage('form');
      }}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary">
            {t('rd.create.continue')}
          </button>
        </>
      }
    >
      <SaveLocationField scope={scope} targets={targets} value={projectId} onChange={setProjectId} error={locationError} />
    </Dialog>
  );
}

// ─── the menu ───────────────────────────────────────────────────────────────

export function AddMaterialMenu({ scope, projects, onCreated }: AddMaterialMenuProps) {
  const modulesEnabled = useFeature('process_modules');
  const { t } = useI18n();
  const toast = useToast();
  const [open, setOpen] = useState<AddAction | null>(null);
  const targets = useMemo(() => Object.fromEntries(ADD_ACTIONS.map((a) => [a, saveTargets(projects, a)])) as Record<AddAction, Project[]>, [projects]);
  const items: MenuItem[] = ADD_ACTIONS.filter((a) => (a !== 'module' || modulesEnabled) && targets[a].length > 0).map((a) => ({ id: a, label: t(`rd.add.${a}`), icon: ACTION_ICON[a], onSelect: () => setOpen(a) }));
  const label = t('rd.add.label');
  const whyId = useId();

  if (!items.length)
    return (
      <span className="rd-add-none">
        <button type="button" className="btn primary" disabled aria-describedby={whyId} title={t('rd.add.none')}>
          <Plus /> {label}
        </button>
        <span id={whyId} className="sr-only">
          {t('rd.add.none')}
        </span>
      </span>
    );

  return (
    <>
      <Menu
        label={label}
        triggerClassName="btn primary"
        trigger={
          <>
            <Plus aria-hidden /> {label} <ChevronDown aria-hidden />
          </>
        }
        items={items}
      />
      {open === 'upload' && (
        <UploadMaterialDialog
          scope={scope}
          targets={targets.upload}
          onClose={() => setOpen(null)}
          onUploaded={(projectId, fileId, name) => {
            setOpen(null);
            toast(t('rd.upload.done', { name }));
            onCreated({ projectId, itemKey: `file:${fileId}` });
          }}
        />
      )}
      {open && open !== 'upload' && (open !== 'module' || modulesEnabled) && <CreateFlow action={open} scope={scope} targets={targets[open]} onClose={() => setOpen(null)} onCreated={onCreated} />}
    </>
  );
}

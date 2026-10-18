import { featureEnabled,useCapabilities,useFeature } from '../../api/capabilities';
import { projectTabFeature } from '../../app/featureAccess';
// /project/:tab — the project (manager view, R1.2 §09, prototype #project.*):
// task board, stages, members and project approvals of the current project.
// The header is the prototype's: project name, code and the manager-view line,
// edit / archive actions, then the strip a manager scans first — objective,
// stage bar with blockers, task numbers (features/tasks/ProjectStrip).
// Personal to-dos live on the workbench, not here.

import { Archive,ArchiveRestore,Pencil } from 'lucide-react';
import { useState } from 'react';
import { Navigate,useParams } from 'react-router-dom';
import { archiveProject,updateProject } from '../../api/endpoints';
import type { Project } from '../../api/types';
import { PROJECT_TAB_META,isProjectTab,paths } from '../../app/nav';
import { useCurrentProject } from '../../components/shell/CurrentProject';
import { ArchivedChip,RoleBadge } from '../../components/ui/Badges';
import { ConfirmDialog,Dialog } from '../../components/ui/Dialog';
import { Field } from '../../components/ui/Form';
import { ErrorNotice,ErrorState,SkeletonRows } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { ProjectStrip } from '../../features/tasks/ProjectStrip';
import { useI18n } from '../../i18n/I18nProvider';
import { AreaFrame,type AreaContext } from '../areas/AreaFrame';
import { PROJECT_SECTION_COMPONENTS } from './registry';

function EditProjectDialog({ project, onClose, onSaved }: { project: Project; onClose: () => void; onSaved: (p: Project) => void }) {
  const { t } = useI18n();
  const [name, setName] = useState(project.name);
  const [description, setDescription] = useState(project.description);
  // S5 "default Turbo" switch (off unless a project manager turns it on).
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [touched, setTouched] = useState(false);
  const nameErr = !name.trim() ? t('validation.required', { field: t('projects.field.name') }) : null;
  const submit = async () => {
    setTouched(true);
    if (nameErr) return;
    setBusy(true);
    setError(null);
    try {
      onSaved(
        await updateProject(project.id, {
          name: name.trim(),
          description: description.trim(),
        }),
      );
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog
      title={t('project.edit.title')}
      subtitle={<span className="mono">{project.code}</span>}
      onClose={onClose}
      busy={busy}
      onSubmit={() => void submit()}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={busy}>
            {busy ? t('common.saving') : t('common.save')}
          </button>
        </>
      }
    >
      <div className="form-grid one">
        <Field label={t('projects.field.name')} error={touched ? nameErr : null}>
          <input className="input" value={name} maxLength={200} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label={t('projects.field.description')} optional>
          <textarea className="input" rows={4} maxLength={5000} value={description} onChange={(e) => setDescription(e.target.value)} />
        </Field>
        <></>
      </div>
      <ErrorNotice error={error} />
    </Dialog>
  );
}

/** Edit details / archive (owner) — the page actions next to the project name. */
function ProjectActions({ ctx }: { ctx: AreaContext }) {
  const { t } = useI18n();
  const toast = useToast();
  const { reloadProjects } = useCurrentProject();
  const { project, caps, reloadProject } = ctx;
  const [editing, setEditing] = useState(false);
  const [archiveOpen, setArchiveOpen] = useState(false);
  const [archiveBusy, setArchiveBusy] = useState(false);
  const [archiveError, setArchiveError] = useState<unknown>(null);
  const archived = project.status === 'archived';

  const changed = () => {
    reloadProject();
    reloadProjects();
  };

  const doArchive = async () => {
    setArchiveBusy(true);
    setArchiveError(null);
    try {
      const updated = await archiveProject(project.id, archived);
      changed();
      toast(t(archived ? 'projects.restoredToast' : 'projects.archivedToast', { code: updated.code }));
      setArchiveOpen(false);
    } catch (e) {
      setArchiveError(e);
    } finally {
      setArchiveBusy(false);
    }
  };

  return (
    <>
      <RoleBadge role={project.my_role} />
      {caps.manage && (
        <button type="button" className="btn sm" onClick={() => setEditing(true)}>
          <Pencil /> {t('project.edit')}
        </button>
      )}
      {caps.archive && (
        <button type="button" className={`btn sm ${archived ? '' : 'danger'}`} onClick={() => setArchiveOpen(true)}>
          {archived ? <ArchiveRestore /> : <Archive />} {archived ? t('common.restore') : t('common.archive')}
        </button>
      )}
      {editing && (
        <EditProjectDialog
          project={project}
          onClose={() => setEditing(false)}
          onSaved={() => {
            changed();
            setEditing(false);
            toast(t('project.savedToast'));
          }}
        />
      )}
      {archiveOpen && (
        <ConfirmDialog
          title={t(archived ? 'projects.restore.title' : 'projects.archive.title', { code: project.code })}
          body={t(archived ? 'projects.restore.body' : 'projects.archive.body')}
          confirmLabel={archived ? t('common.restore') : t('common.archive')}
          danger={!archived}
          busy={archiveBusy}
          error={<ErrorNotice error={archiveError} />}
          onConfirm={() => void doArchive()}
          onClose={() => {
            setArchiveOpen(false);
            setArchiveError(null);
          }}
        />
      )}
    </>
  );
}

/** Strip and the archived notice under the heading. */
function ProjectHead({ ctx }: { ctx: AreaContext }) {
  const stagesEnabled = useFeature('stages');
  const tasksEnabled = useFeature('tasks');
  const showStrip = stagesEnabled || tasksEnabled;
  const { t } = useI18n();
  const { reloadProjects } = useCurrentProject();
  const { project, caps, reloadProject } = ctx;
  return (
    <>
      {showStrip && <ProjectStrip
        project={project}
        caps={caps}
        onChanged={() => {
          reloadProject();
          reloadProjects();
        }}
      />}
      {project.status === 'archived' && (
        <div className="callout warn">
          <Archive aria-hidden />
          <span>
            {t('project.archivedBanner')} {caps.archive && t('project.archivedOwner')}
          </span>
        </div>
      )}
    </>
  );
}

export default function ProjectPage() {
  const capabilities = useCapabilities();
  const { t } = useI18n();
  const { tab } = useParams();
  if (!isProjectTab(tab) && capabilities.error) return <ErrorState error={capabilities.error} onRetry={capabilities.refresh} />;
  if (!isProjectTab(tab) && capabilities.status !== 'ready') return <SkeletonRows />;
  if (!isProjectTab(tab)) return <Navigate to={paths.projectTab(featureEnabled('tasks', capabilities) ? 'tasks' : 'members')} replace />;
  const meta = PROJECT_TAB_META.find((m) => m.id === tab)!;
  const Section = PROJECT_SECTION_COMPONENTS[tab];
  return (
    <AreaFrame
      title={t('nav.project')}
      crumbs={[{ label: t(meta.labelKey) }]}
      heading={({ project }) => (
        <span className="pa-title">
          {project.name}
          {project.status === 'archived' && <ArchivedChip />}
        </span>
      )}
      subtitle={({ project }) => (
        <span className="pa-sub" title={project.description || undefined}>
          <span className="mono">{project.code}</span> · {project.description || t('pa.head.managerView')}
        </span>
      )}
      actions={(ctx) => <ProjectActions ctx={ctx} />}
      tabs={{ label: t('project.tabsLabel'), items: PROJECT_TAB_META.filter((item) => { const feature = projectTabFeature(item.id); return !feature || featureEnabled(feature, capabilities); }), to: (id) => paths.projectTab(id) }}
      head={(ctx) => <ProjectHead ctx={ctx} />}
    >
      {(ctx) => <Section {...ctx} />}
    </AreaFrame>
  );
}

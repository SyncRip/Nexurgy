import { useState } from 'react';
import { getProject,updateProject } from '../../api/endpoints';
import { projectCapabilities } from '../../auth/permissions';
import { useCurrentProject } from '../../components/shell/CurrentProject';
import { EmptyState,ErrorNotice,SkeletonRows } from '../../components/ui/States';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { MembersTab } from '../../pages/project/MembersTab';
import { DefaultTurboSwitch } from '../turbo/ProjectPolicy';

export function ProjectSettings() {
  const { t } = useI18n();
  const current = useCurrentProject();
  const res = useResource((signal) => getProject(current.projectId!, { signal }), [current.projectId], { enabled: !!current.projectId });
  const project = res.data?.id === current.projectId ? res.data : undefined;
  const caps = projectCapabilities(project);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const change = async (turbo: boolean) => {
    if (!project || !caps.manage || project.version == null) return;
    setBusy(true); setError(null);
    try { res.setData(await updateProject(project.id, { default_execution_policy: turbo ? 'turbo' : 'standard', expected_version: project.version })); current.reloadProjects(); }
    catch (e) { setError(e); res.reload(); }
    finally { setBusy(false); }
  };
  if (!current.projectId) return <EmptyState title={t('core.settings.project')} body={t('area.noProject.body')} />;
  if (!project) return <><ErrorNotice error={res.error} onRetry={res.reload} />{!res.error && <SkeletonRows rows={3} />}</>;
  return <div className="stack"><section className="panel"><div className="panel-h"><h2>{project.code} · {project.name}</h2></div><div className="panel-b stack">
    <DefaultTurboSwitch checked={project.default_execution_policy === 'turbo'} onChange={(on) => void change(on)} disabled={!caps.manage || busy || project.version == null} />
    <p className="faint small">{t('core.settings.policyHint')}</p><ErrorNotice error={error} />
  </div></section><MembersTab project={project} caps={caps} reloadProject={res.reload} /></div>;
}

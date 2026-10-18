// Shared frame of the R1.2 area pages (/parts, /flows, /resources, /project):
// loads the current project fresh (role, part count), declares it to the
// shell, and renders the area's tab bar. The project section components keep
// their ProjectTabProps contract (pages/project/tabProps.ts).

import { FolderKanban } from 'lucide-react';
import { Suspense, type ReactNode } from 'react';
import { Link, NavLink } from 'react-router-dom';
import { isApiError } from '../../api/client';
import { getProject } from '../../api/endpoints';
import type { Project } from '../../api/types';
import { paths, type TabMeta } from '../../app/nav';
import { projectCapabilities, type ProjectCapabilities } from '../../auth/permissions';
import { useCurrentProject } from '../../components/shell/CurrentProject';
import { useCrumbs, useShellProject, type Crumb } from '../../components/shell/ShellContext';
import { EmptyState, ErrorState, SkeletonRows } from '../../components/ui/States';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';

export interface AreaContext {
  project: Project;
  caps: ProjectCapabilities;
  reloadProject: () => void;
}

export interface AreaTabs<T extends string> {
  label: string;
  items: TabMeta<T>[];
  to: (tab: T) => string;
  /** Count shown after a tab label (prototype `.tabs em`); omitted while unknown. */
  counts?: Partial<Record<T, number | string>>;
  /** The highlighted tab when the URL is one of its sections (default: the link that matches the URL). */
  activeId?: T;
}

export function AreaFrame<T extends string>({
  title,
  heading,
  subtitle,
  actions,
  crumbs,
  tabs,
  head,
  children,
}: {
  title: string;
  /** Page heading once the project is loaded (default: the title). */
  heading?: (ctx: AreaContext) => ReactNode;
  /** One line under the heading. */
  subtitle?: ReactNode | ((ctx: AreaContext) => ReactNode);
  /** Page actions on the right of the heading. */
  actions?: (ctx: AreaContext) => ReactNode;
  /** Crumbs after the area title. */
  crumbs?: Crumb[];
  tabs?: AreaTabs<T>;
  /** Extra header content under the title (e.g. the project strip). */
  head?: (ctx: AreaContext) => ReactNode;
  children: (ctx: AreaContext) => ReactNode;
}) {
  const { t } = useI18n();
  const current = useCurrentProject();
  const projectId = current.projectId;
  const res = useResource((signal) => getProject(projectId!, { signal }), [projectId], { enabled: !!projectId });
  // Stale data of the previous project must not render under the new one.
  const project = res.data && res.data.id === projectId ? res.data : undefined;

  useShellProject(project);
  useCrumbs([{ label: title }, ...(crumbs ?? [])]);

  if (!projectId) {
    if (current.projectsError) {
      return (
        <div className="page">
          <ErrorState error={current.projectsError} onRetry={current.reloadProjects} />
        </div>
      );
    }
    if (current.projects && !current.projects.length) {
      return (
        <div className="page">
          <EmptyState
            icon={<FolderKanban className="big-ico" aria-hidden />}
            title={t('area.noProject.title')}
            body={t('area.noProject.body')}
            action={
              <Link to={paths.projects()} className="btn primary">
                {t('pswitch.all')}
              </Link>
            }
          />
        </div>
      );
    }
    return (
      <div className="page">
        <SkeletonRows />
      </div>
    );
  }

  if (res.error && !project) {
    const missing = isApiError(res.error) && (res.error.status === 404 || res.error.status === 403);
    return (
      <div className="page">
        {missing ? (
          <EmptyState
            title={t('project.notFound.title')}
            body={t('project.notFound.body')}
            action={
              <Link to={paths.projects()} className="btn">
                {t('pswitch.all')}
              </Link>
            }
          />
        ) : (
          <ErrorState error={res.error} onRetry={res.reload} />
        )}
      </div>
    );
  }

  const ctx: AreaContext | null = project ? { project, caps: projectCapabilities(project), reloadProject: res.reload } : null;

  return (
    <div className="page area-page">
      <header className="area-head">
        {/* Always the same row shape (56px floor) so the title and the toolbar
            below it sit at the same spot across areas, with or without a subtitle. */}
        <div className="area-head-row">
          <div className="area-head-text">
            <h1 className="area-title">{(ctx && heading?.(ctx)) || title}</h1>
            {subtitle && <p className="area-sub">{typeof subtitle === 'function' ? ctx && subtitle(ctx) : subtitle}</p>}
          </div>
          {ctx && actions && <div className="area-actions">{actions(ctx)}</div>}
        </div>
        {ctx && head?.(ctx)}
      </header>
      {tabs && (
        <nav className="tabs" aria-label={tabs.label}>
          {tabs.items.map((s) => {
            const n = tabs.counts?.[s.id];
            const body = (
              <>
                {s.icon}
                <span>{t(s.labelKey)}</span>
                {n !== undefined && <em className="tab-count">{n}</em>}
              </>
            );
            // With activeId the URL may be a section of a tab, so the highlight is explicit.
            if (tabs.activeId) {
              const on = tabs.activeId === s.id;
              return (
                <Link key={s.id} to={tabs.to(s.id)} className={`tab ${on ? 'active' : ''}`} aria-current={on ? 'page' : undefined}>
                  {body}
                </Link>
              );
            }
            return (
              <NavLink key={s.id} to={tabs.to(s.id)} className={({ isActive }) => `tab ${isActive ? 'active' : ''}`}>
                {body}
              </NavLink>
            );
          })}
        </nav>
      )}
      <div className="section-body">{ctx ? <Suspense fallback={<SkeletonRows />}>{children(ctx)}</Suspense> : <SkeletonRows />}</div>
    </div>
  );
}

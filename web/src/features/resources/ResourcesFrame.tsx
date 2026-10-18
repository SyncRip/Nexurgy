// Material page frame: title, a single scope selector and a unified material list. The historical experience page provides a title and back entry; usage records retain their dedicated view.
// Replaces AreaFrame for this page only: the scope may be one project or all accessible projects,
// so it never assumes a single current project, and the top-bar project switcher is hidden (by
// ResourcesPage) so the page has one scope entry.
//
// Scope rules (scope.ts): the URL names it (?project=<uuid> | ?scope=all); otherwise the remembered scope,
// else the current project, and the URL is rewritten to name it. A project the server refuses (403 / 404), an unusable
// URL value or a remembered project that is gone falls back to an available scope with a visible reason. A project
// scope makes that project current (as other area pages do); all projects never touches the current project.

import { ArrowLeft,FolderKanban,TriangleAlert,X } from 'lucide-react';
import { useCallback,useEffect,useMemo,useState,type ReactNode } from 'react';
import { Link,useLocation,useNavigate } from 'react-router-dom';
import { useFeature } from '../../api/capabilities';
import { isApiError } from '../../api/client';
import { getProject } from '../../api/endpoints';
import type { Project } from '../../api/types';
import { paths,type ResourceTab } from '../../app/nav';
import { useAuth } from '../../auth/AuthProvider';
import { useCurrentProject } from '../../components/shell/CurrentProject';
import { useCrumbs,useShell,useShellProject } from '../../components/shell/ShellContext';
import { EmptyState,ErrorState,SkeletonRows } from '../../components/ui/States';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import '../assets/assets.css';
import { FeatureNotice } from '../capabilities/FeatureGate';
import ExperienceView from './ExperienceView';
import MaterialsView from './MaterialsView';
import { ScopePanel,scopeName } from './ScopePanel';
import UsageRecordsView from './UsageRecordsView';
import './resources.css';
import {
parseScopeValue,
readSavedScope,
readUrlScope,
resolveScope,
scopeSearch,
scopeStorage,
scopeUrl,
scopeValue,
withScope,
withScopeProject,
writeSavedScope,
type ScopeFallbackReason,
} from './scope';
import type { ViewScope } from './types';
import { useCatalog } from './useCatalog';

const VIEW_LABEL = { files: 'rp.view.files', knowledge: 'rp.view.knowledge', process: 'rp.view.process' } as const satisfies Record<ResourceTab, string>;

/** Result of opening the scope's project: the row, or null when the server refused it (403 / 404). */
interface ProjectProbe {
  id: string;
  project: Project | null;
}

export default function ResourcesFrame({ view }: { view: ResourceTab }) {
  const { t } = useI18n();
  const navigate = useNavigate();
  const location = useLocation();
  const { user } = useAuth();
  const userId = user?.id ?? 'anonymous';
  const current = useCurrentProject();
  const { setProject: setShellProject } = useShell();
  const storage = useMemo(() => scopeStorage(), []);
  const [saved, setSaved] = useState<ViewScope | null>(() => readSavedScope(storage, userId));
  const [unavailable, setUnavailable] = useState<ReadonlySet<string>>(() => new Set());
  const [notice, setNotice] = useState<{ reason: ScopeFallbackReason; scope: string } | null>(null);

  const search = useMemo(() => new URLSearchParams(location.search), [location.search]);
  const url = useMemo(() => readUrlScope(search), [search]);
  const resolution = resolveScope({ url, saved, current: current.projectId, projects: current.projects, unavailable });
  const scope = resolution.scope;
  const scopeKey = scope ? scopeValue(scope) : null;

  // The scope's project, fresh (role / status), the same way AreaFrame loads it; a refusal means "not accessible".
  const scopeProjectId = scope?.kind === 'project' ? scope.projectId : null;
  const probe = useResource<ProjectProbe>(
    async (signal) => {
      const id = scopeProjectId!;
      try {
        return { id, project: await getProject(id, { signal }) };
      } catch (e) {
        if (isApiError(e) && (e.status === 403 || e.status === 404)) return { id, project: null };
        throw e;
      }
    },
    [scopeProjectId],
    { enabled: !!scopeProjectId },
  );
  // Stale data of the previous scope must not render under the new one.
  const probed = probe.data && probe.data.id === scopeProjectId ? probe.data : null;
  const scopeProject = probed?.project ?? null;
  useEffect(() => {
    if (probed && !probed.project) setUnavailable((s) => (s.has(probed.id) ? s : new Set(s).add(probed.id)));
  }, [probed]);

  // A project scope makes the project current; the cross-project scopes leave the current project alone and clear
  // the shell's page project (there is no single project on the page).
  useShellProject(scopeProject ?? undefined);
  const scopeKind = scope?.kind;
  useEffect(() => {
    if (scopeKind && scopeKind !== 'project') setShellProject(null);
  }, [scopeKind, setShellProject]);

  // Name the scope in the URL (refresh, back and return links keep it). A fallback from the URL drops that URL's
  // other keys (they belonged to the refused scope); naming a remembered / current scope keeps them.
  const rewrite = scope && !resolution.fromUrl ? (resolution.fallback && resolution.fallback !== 'saved_project' ? scopeSearch(scope) : withScope(search, scope)) : null;
  const rewriteTo = rewrite ? `${location.pathname}?${rewrite.toString()}` : null;
  const fallback = resolution.fallback;
  useEffect(() => {
    if (!rewriteTo || !scopeKey) return;
    if (fallback) setNotice({ reason: fallback, scope: scopeKey });
    navigate(rewriteTo, { replace: true });
  }, [rewriteTo, fallback, scopeKey, navigate]);

  // Remember the scope once the URL names it and (for a project) the server confirmed it.
  const confirmed = !!scopeKey && resolution.fromUrl && (scopeKind !== 'project' || !!scopeProject);
  useEffect(() => {
    if (!confirmed || !scopeKey) return;
    const s = parseScopeValue(scopeKey);
    if (!s) return;
    writeSavedScope(storage, userId, s);
    setSaved((prev) => (prev && scopeValue(prev) === scopeKey ? prev : s));
  }, [confirmed, scopeKey, storage, userId]);

  const changeScope = useCallback(
    (next: ViewScope, extra?: Record<string, string>) => {
      setNotice(null);
      navigate(scopeUrl(view, next, extra));
    },
    [navigate, view],
  );

  useCrumbs(view === 'files' ? [{ label: t('area.resources') }] : [{ label: t('area.resources'), to: scope ? scopeUrl('files', scope) : undefined }, { label: t(VIEW_LABEL[view]) }]);

  const projects = useMemo(() => withScopeProject(current.projects, scopeProject), [current.projects, scopeProject]);
  const noProjects = !!current.projects && current.projects.length === 0 && scopeKind !== 'project';

  let content: ReactNode;
  if (current.projectsError && !current.projects) content = <ErrorState error={current.projectsError} onRetry={current.reloadProjects} />;
  else if (noProjects)
    content = (
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
    );
  else if (!scope) content = <SkeletonRows />;
  else if (scopeProjectId && !scopeProject)
    content = probe.error && !probe.loading && !probed ? <ErrorState error={probe.error} onRetry={probe.reload} /> : <SkeletonRows />;
  else content = <ScopeBody key={scopeKey} scope={scope} view={view} projects={projects} scopeProject={scopeProject} onScope={changeScope} reloadProject={probe.reload} />;

  return (
    <div className="page rp-page">
      <header className="rp-head page-halo">
        <h1 className="rp-title">{t('area.resources')}</h1>
        <p className="rp-sub">{t('rp.subtitle')}</p>
      </header>
      {notice && (
        <div className="callout warn rp-notice" role="status">
          <TriangleAlert aria-hidden />
          <div className="grow">{t(`rp.notice.${notice.reason}`, { scope: scopeName(t, parseScopeValue(notice.scope) ?? { kind: 'all' }, projects ?? []) })}</div>
          <button type="button" className="btn ghost icon sm" aria-label={t('rp.notice.dismiss')} onClick={() => setNotice(null)}>
            <X />
          </button>
        </div>
      )}
      {content}
    </div>
  );
}

/** Everything that depends on one resolved scope (remounted per scope, so no state of the old scope leaks in). */
function ScopeBody({
  scope,
  view,
  projects,
  scopeProject,
  onScope,
  reloadProject,
}: {
  scope: ViewScope;
  view: ResourceTab;
  /** Accessible projects (+ the scope's project); null while the list loads. */
  projects: readonly Project[] | null;
  scopeProject: Project | null;
  onScope: (scope: ViewScope, extra?: Record<string, string>) => void;
  reloadProject: () => void;
}) {
  const collaborationEnabled = useFeature('collaboration');
  const { t } = useI18n();
  const navigate = useNavigate();
  const catalog = useCatalog(scope, projects);
  const list = projects ?? [];

  return (
    <>
      <ScopePanel scope={scope} view={view} projects={list} onChange={(s) => onScope(s)} onTaskResults={collaborationEnabled ? () => navigate(scopeUrl('process', scope)) : undefined} />
      {view !== 'files' && <FeatureNotice feature="collaboration" />}
      <section className={`rp-panel ${view === 'files' ? 'rp-panel-single' : ''}`}>
        {view === 'process' ? (
          <UsageRecordsView scope={scope} project={scopeProject} reloadProject={reloadProject} />
        ) : (
          <>
            {view === 'knowledge' && <header className="rp-history-head"><Link className="btn ghost sm" to={scopeUrl('files', scope)}><ArrowLeft aria-hidden />{t('rp.backMaterials')}</Link><h2>{t('rp.view.knowledge')}</h2></header>}
            <div className="rp-tabpanel">
              {view === 'files' ? (
                <MaterialsView scope={scope} project={scopeProject} projects={list} catalog={catalog} onScope={onScope} />
              ) : (
                <ExperienceView scope={scope} projects={projects} project={scopeProject} />
              )}
            </div>
          </>
        )}
      </section>
    </>
  );
}

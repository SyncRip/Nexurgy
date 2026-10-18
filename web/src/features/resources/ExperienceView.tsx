// The "Experience" view of resources & knowledge (handoff §4, §10; redesign §4): archived workflow collections for the
// page's view scope. Project scope = the existing KnowledgeTab. All projects = one list merged from every accessible
// project (each row names its source project; per-project failures and truncation are stated, late answers for an
// older query never land).
// URL keys: q, reusable, collection_id, collection_version_id (+ limit, offset in the project scope).

import '../knowledge/knowledge.css';
import './experience.css';
import { RefreshCw } from 'lucide-react';
import { useEffect, useMemo } from 'react';
import { useLocation } from 'react-router-dom';
import { useFeature } from '../../api/capabilities';
import { getProject } from '../../api/endpoints';
import { getKnowledgeCollection, listKnowledge, type KnowledgeList } from '../../api/knowledge';
import type { Project, UUID } from '../../api/types';
import { projectCapabilities } from '../../auth/permissions';
import { useOptionalCurrentProject } from '../../components/shell/CurrentProject';
import { EmptyState, ErrorState, ErrorText, Skeleton, SkeletonRows } from '../../components/ui/States';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { useAssetQuery, useUrlSearchText } from '../assets/useAssetQuery';
import { CollectionDetail } from '../knowledge/CollectionDetail';
import { CollectionList } from '../knowledge/CollectionList';
import KnowledgeTab from '../knowledge/KnowledgeTab';
import { ALL_SCOPE_LIMIT, mergeCollections, scopeLoad, sumTotals, type ScopeCount } from './experience';
import { scopeProjectIds, usePerProject } from './perProject';
import { scopeValue } from './scope';
import type { ViewScope } from './types';

export interface ExperienceViewProps {
  scope: ViewScope;
  /** Accessible projects (null while loading). */
  projects: readonly Project[] | null;
  /** The scope's project for a project scope. */
  project: Project | null;
}

export default function ExperienceView({ scope, projects, project }: ExperienceViewProps) {
  if (scope.kind === 'all') return <AllProjectsExperience projects={projects} />;
  return project && project.id === scope.projectId ? <ProjectExperience project={project} /> : <DetailSkeleton />;
}

/** Number of archived collections in the scope (for the view tab count); null while unknown. */
export function useExperienceCount(scope: ViewScope, projects: readonly Project[] | null): number | null {
  return useExperienceCountState(scope, projects).count;
}

/**
 * Same count with `partial`: in the all-projects scope a project whose request failed is left out of the sum
 * (never counted as zero), and `partial` says so.
 */
export function useExperienceCountState(scope: ViewScope, projects: readonly Project[] | null): ScopeCount {
  const collaborationEnabled = useFeature('collaboration');
  const ids = collaborationEnabled ? scopeProjectIds(scope, projects) : null;
  const { entries } = usePerProject(scopeValue(scope), ids, countCollections, { keep: 'placeholder' });
  return ids ? sumTotals(ids, entries) : { count: null, partial: false };
}

const countCollections = (projectId: UUID, signal: AbortSignal) => listKnowledge(projectId, { limit: 1 }, { signal }).then((r) => r.total);

// ─── scopes ─────────────────────────────────────────────────────────────────

const projectLabel = (p: Pick<Project, 'name' | 'code'>) => `${p.name} · ${p.code}`;

function ProjectExperience({ project }: { project: Project }) {
  const shell = useOptionalCurrentProject();
  const caps = useMemo(() => projectCapabilities(project), [project]);
  const reloadProject = shell?.reloadProjects ?? noop;
  return <KnowledgeTab project={project} caps={caps} reloadProject={reloadProject} />;
}

function noop() {}

function DetailSkeleton() {
  const { t } = useI18n();
  return (
    <div className="panel">
      <div className="panel-b stack-sm" role="status" aria-label={t('common.loading')}>
        <Skeleton w="40%" h={22} />
        <Skeleton w="60%" />
        <SkeletonRows rows={3} cols={3} />
      </div>
    </div>
  );
}

function AllProjectsExperience({ projects }: { projects: readonly Project[] | null }) {
  const { t, tn, fmt } = useI18n();
  const location = useLocation();
  const query = useAssetQuery();
  const { search, patch } = query;
  const [q, setQ] = useUrlSearchText(query, 'q');
  const dq = search.get('q') ?? '';
  const reusableOnly = search.get('reusable') === '1';
  const collectionId = search.get('collection_id') ?? '';
  const versionId = search.get('collection_version_id') ?? '';
  const filtered = !!(dq || reusableOnly);

  const ids = useMemo(() => projects?.map((p) => p.id) ?? null, [projects]);
  const byId = useMemo(() => new Map((projects ?? []).map((p) => [p.id, p])), [projects]);
  // A new search keeps the previous rows on screen until each project answers it; a failed project shows none.
  const { entries, retry } = usePerProject<KnowledgeList>(
    `${dq}\n${reusableOnly}`,
    ids,
    (id, signal) => listKnowledge(id, { q: dq, reusable: reusableOnly, limit: ALL_SCOPE_LIMIT }, { signal }),
    { keep: 'placeholder' },
  );
  const load = scopeLoad(ids ?? [], entries);
  const settled = !!ids && load.pending === 0;
  const items = useMemo(() => mergeCollections((ids ?? []).map((id) => entries.get(id)?.data?.items ?? [])), [ids, entries]);
  // The server total of each project that answered (beyond the 100 shown when truncated); failed projects are listed apart.
  const total = (ids ?? []).reduce((n, id) => n + (entries.get(id)?.data?.total ?? 0), 0);
  const truncated = (ids ?? []).filter((id) => {
    const d = entries.get(id)?.data;
    return !!d && d.total > d.items.length;
  });
  const anyData = (ids ?? []).some((id) => entries.get(id)?.data);

  // No collection chosen yet: once every project answered, show the newest one (URL replaced).
  useEffect(() => {
    if (collectionId || !settled || !items.length) return;
    patch({ collection_id: items[0].id });
  }, [collectionId, settled, items, patch]);

  const detail = useResource((signal) => getKnowledgeCollection(collectionId, { version_id: versionId }, { signal }), [collectionId, versionId], { enabled: !!collectionId });
  // Only the response for the current selection is shown; a previous collection's data never fills the gap.
  const current = detail.data && detail.data.id === collectionId && (!versionId || detail.data.version?.id === versionId) ? detail.data : undefined;
  // The collection's own project decides what the person may do with it (a deep link may name a project outside the list).
  const listed = current ? byId.get(current.project_id) : undefined;
  const fetched = useResource((signal) => getProject(current!.project_id, { signal }), [current?.project_id], { enabled: !!current && !!projects && !listed });
  const owner = listed ?? (fetched.data && fetched.data.id === current?.project_id ? fetched.data : undefined);
  const caps = useMemo(() => projectCapabilities(owner), [owner]);
  const returnTo = `${location.pathname}${location.search}`;

  const selectCollection = (id: string) => {
    // Re-clicking the open collection still clears an explicit (possibly unreadable) manifest version.
    if (id === collectionId && !versionId) return;
    patch({ collection_id: id, collection_version_id: null });
  };
  const retryFailed = () => load.failed.forEach(retry);

  if (!ids) return <DetailSkeleton />;
  if (!ids.length) return <EmptyState title={t('rx.all.noProjects')} />;
  if (settled && load.failed.length === ids.length) return <ErrorState error={entries.get(load.failed[0])?.error} title={t('rx.all.failedAll')} onRetry={retryFailed} />;
  if (settled && !items.length && !filtered && !load.failed.length) return <EmptyState title={t('rx.all.empty.title')} body={t('rx.all.empty.body')} />;

  let main;
  if (!collectionId) {
    main = settled && !items.length ? null : (
      <div className="panel kb-main-empty">
        <div className="panel-b faint small">{t('kb.pickCollection')}</div>
      </div>
    );
  } else if (detail.error && !current) {
    main = <ErrorState error={detail.error} onRetry={detail.reload} />;
  } else if (current && !owner && fetched.error) {
    main = <ErrorState error={fetched.error} onRetry={fetched.reload} />;
  } else if (!current || !owner) {
    main = <DetailSkeleton />;
  } else {
    main = (
      <CollectionDetail
        key={`${current.id}:${current.version?.id ?? ''}`}
        project={owner}
        caps={caps}
        collection={current}
        refreshing={detail.loading}
        returnTo={returnTo}
        showProject
        onVersion={(id) => patch({ collection_version_id: id })}
        onChanged={() => {
          detail.reload();
          retry(current.project_id);
        }}
      />
    );
  }

  return (
    <div className="stack">
      {(load.pending > 0 || load.failed.length > 0 || truncated.length > 0) && (
        <div className="rx-load stack-sm">
          {load.pending > 0 ? (
            <p className="faint small" role="status">
              {t('rx.load.progress', { done: fmt.number(load.projects - load.pending), total: fmt.number(load.projects) })}
            </p>
          ) : (
            load.failed.length > 0 && (
              <p className="faint small" role="status">
                {t('rx.load.partial', { loaded: fmt.number(load.loaded), total: fmt.number(load.projects) })}
              </p>
            )
          )}
          {load.failed.length > 0 && (
            <div className="callout err slim" role="alert">
              <div className="stack-sm grow">
                <b>{tn('rx.load.failed', load.failed.length, { count: fmt.number(load.failed.length) })}</b>
                <ul className="rx-load-list">
                  {load.failed.map((id) => {
                    const p = byId.get(id);
                    return (
                      <li key={id}>
                        <span className="strong ellip">{p ? projectLabel(p) : id}</span>
                        <span className="small grow ellip">
                          <ErrorText error={entries.get(id)?.error} />
                        </span>
                        <button type="button" className="btn sm" onClick={() => retry(id)} aria-label={t('rx.load.retryProject', { project: p ? projectLabel(p) : id })}>
                          <RefreshCw /> {t('common.retry')}
                        </button>
                      </li>
                    );
                  })}
                </ul>
              </div>
            </div>
          )}
          {truncated.length > 0 && (
            <div className="callout info slim" role="note">
              <ul className="rx-load-list">
                {truncated.map((id) => {
                  const p = byId.get(id);
                  return (
                    <li key={id} className="small">
                      {t('rx.load.truncated', { project: p ? projectLabel(p) : id, shown: fmt.number(ALL_SCOPE_LIMIT), total: fmt.number(entries.get(id)?.data?.total ?? 0) })}
                    </li>
                  );
                })}
              </ul>
            </div>
          )}
        </div>
      )}
      <div className="kb-layout">
        <CollectionList
          items={items}
          total={total}
          initial={!anyData && load.pending > 0}
          loading={load.pending > 0}
          selectedId={collectionId}
          onSelect={selectCollection}
          q={q}
          onQ={setQ}
          reusableOnly={reusableOnly}
          onReusable={(v) => patch({ reusable: v })}
          onClear={() => {
            setQ('');
            patch({ q: null, reusable: null });
          }}
          projectOf={(c) => byId.get(c.project_id)}
        />
        <div className="kb-main">{main}</div>
      </div>
    </div>
  );
}

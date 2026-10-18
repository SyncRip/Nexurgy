// Loads the materials catalogue of a view scope (design §1, handoff §4): one project's sources, every accessible
// project's (≈4 at a time, each with its own load state and retry — see perProject.ts).
// While the same scope reloads, its previous rows stay visible.

import { useMemo, useRef } from 'react';
import type { Project, UUID } from '../../api/types';
import { buildProjectCatalog, mergeCatalogs, type ProjectCatalog, type ProjectSources } from './catalog';
import { scopeProjectIds, usePerProject } from './perProject';
import { scopeValue } from './scope';
import { loadProjectSources } from './sources';
import type { CatalogItem, CatalogProject, CatalogState, ProjectLoadState, ViewScope } from './types';

const catalogProject = (id: UUID, p: Project | undefined): CatalogProject => ({ id, code: p?.code ?? '', name: p?.name ?? '' });

/**
 * `projects` = the accessible projects (from listProjects); null while they load.
 * project scope → that project's sources (loaded even when it is not in `projects`: the API decides access, and an
 * error is reported on that project's load state); all → every project in `projects`.
 */
export function useCatalog(scope: ViewScope, projects: readonly Project[] | null): CatalogState {
  const ids = scopeProjectIds(scope, projects);
  const waiting = ids === null;
  const { entries, retry, reload } = usePerProject(scopeValue(scope), ids, loadProjectSources);

  // Built catalogues per loaded sources object (rebuilt only when the sources or the project's code / name change).
  const built = useRef(new WeakMap<ProjectSources, { project: CatalogProject; catalog: ProjectCatalog }>());
  const projectRows = useMemo(() => new Map((projects ?? []).map((p) => [p.id, p] as const)), [projects]);

  return useMemo<CatalogState>(() => {
    const base = { reload, retryProject: retry };
    const states: ProjectLoadState[] = [];
    const lists: CatalogItem[][] = [];
    for (const [id, e] of entries) {
      const project = catalogProject(id, projectRows.get(id));
      let catalog: ProjectCatalog | null = null;
      if (e.data) {
        const hit = built.current.get(e.data);
        catalog = hit && hit.project.code === project.code && hit.project.name === project.name ? hit.catalog : buildProjectCatalog(project, e.data);
        if (hit?.catalog !== catalog) built.current.set(e.data, { project, catalog });
        lists.push(catalog.items);
      }
      states.push({
        project,
        status: e.status,
        ...(e.status === 'error' ? { error: e.error } : {}),
        ...(catalog?.truncated.length ? { truncated: catalog.truncated } : {}),
      });
    }
    const pending = [...entries.values()].every((e) => e.status === 'loading' && !e.data);
    return {
      ...base,
      items: lists.length === 1 ? lists[0] : mergeCatalogs(lists),
      projects: states,
      initial: waiting || (states.length > 0 && pending),
      loading: waiting || states.some((s) => s.status === 'loading'),
    };
  }, [waiting, entries, projectRows, reload, retry]);
}

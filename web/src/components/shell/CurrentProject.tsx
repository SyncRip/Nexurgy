// Current project for the whole shell (R1.2 §04). Area pages (/parts, /flows,
// /resources, /project) read it; links may name a project with ?project=<id>,
// and pages of one object (part, run, task…) make their project current via
// useShellProject. The choice is remembered per user (features/project/currentProject.ts).

import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { listProjects } from '../../api/endpoints';
import type { Project } from '../../api/types';
import { PROJECT_PARAM, projectSwitchTarget } from '../../app/nav';
import { useAuth } from '../../auth/AuthProvider';
import { isProjectId, projectStorage, readCurrentProject, resolveCurrentProject, writeCurrentProject } from '../../features/project/currentProject';
import { useResource } from '../../hooks/useResource';

export interface CurrentProjectValue {
  /** Resolved current project id (null: the user has no project yet, or the list is still loading without a stored choice). */
  projectId: string | null;
  /** The current project as listed (may lag behind a fresh getProject). */
  project: Project | null;
  projects: Project[] | undefined;
  projectsError: unknown;
  projectsLoading: boolean;
  reloadProjects: () => void;
  /** Remember a project as current (no navigation). */
  setProjectId: (id: string) => void;
  /** Make a project current and leave pages that belong to the previous one. */
  switchProject: (id: string) => void;
}

const CurrentProjectContext = createContext<CurrentProjectValue | null>(null);

export function CurrentProjectProvider({ children }: { children: ReactNode }) {
  const { user } = useAuth();
  const userId = user?.id ?? 'anonymous';
  const location = useLocation();
  const navigate = useNavigate();
  const storage = useMemo(() => projectStorage(), []);
  const [stored, setStored] = useState<string | null>(() => readCurrentProject(storage, userId));
  useEffect(() => setStored(readCurrentProject(storage, userId)), [storage, userId]);

  const list = useResource((signal) => listProjects({}, { signal }), [userId]);
  const urlParam = new URLSearchParams(location.search).get(PROJECT_PARAM);
  const urlId = isProjectId(urlParam) ? urlParam : null;
  const projectId = resolveCurrentProject(list.data, urlId, stored);

  // A link that names a project makes it current for the next pages too.
  useEffect(() => {
    if (urlId && urlId !== stored) {
      writeCurrentProject(storage, userId, urlId);
      setStored(urlId);
    }
  }, [urlId, stored, storage, userId]);

  const setProjectId = useCallback(
    (id: string) => {
      if (!isProjectId(id)) return;
      writeCurrentProject(storage, userId, id);
      setStored(id);
    },
    [storage, userId],
  );

  const pathname = location.pathname;
  const switchProject = useCallback(
    (id: string) => {
      setProjectId(id);
      navigate(projectSwitchTarget(pathname));
    },
    [setProjectId, navigate, pathname],
  );

  const projects = list.data;
  const value = useMemo<CurrentProjectValue>(
    () => ({
      projectId,
      project: projects?.find((p) => p.id === projectId) ?? null,
      projects,
      projectsError: list.error,
      projectsLoading: list.loading,
      reloadProjects: list.reload,
      setProjectId,
      switchProject,
    }),
    [projectId, projects, list.error, list.loading, list.reload, setProjectId, switchProject],
  );
  return <CurrentProjectContext.Provider value={value}>{children}</CurrentProjectContext.Provider>;
}

export function useCurrentProject(): CurrentProjectValue {
  const v = useContext(CurrentProjectContext);
  if (!v) throw new Error('useCurrentProject must be used inside <CurrentProjectProvider>');
  return v;
}

/** Same, but null outside the shell (pages rendered in isolation, tests). */
export function useOptionalCurrentProject(): CurrentProjectValue | null {
  return useContext(CurrentProjectContext);
}

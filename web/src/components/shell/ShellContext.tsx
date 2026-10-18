// Pages tell the shell what to show: breadcrumbs, the project the page
// belongs to (which also becomes the current project, see CurrentProject.tsx),
// and whether the top-bar project switcher applies (a page with its own scope
// picker hides it).

import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import type { Project } from '../../api/types';
import { useOptionalCurrentProject } from './CurrentProject';

export interface Crumb {
  label: string;
  to?: string;
  /** Render in mono (codes, part numbers). */
  mono?: boolean;
}

interface ShellValue {
  railCollapsed: boolean;
  setRailCollapsed: (value: boolean) => void;
  listDensity: 'comfortable' | 'compact';
  setListDensity: (value: 'comfortable' | 'compact') => void;
  crumbs: Crumb[];
  setCrumbs: (c: Crumb[]) => void;
  project: Project | null;
  setProject: (p: Project | null) => void;
  /** The top-bar project switcher is hidden while a page asks for it (useHideProjectSwitcher). */
  projectSwitcherHidden: boolean;
  /** +1 / -1: pages that currently hide the switcher. */
  hideProjectSwitcher: (delta: 1 | -1) => void;
}

const ShellContext = createContext<ShellValue | null>(null);

function savedPreference(key: string): string | null {
  try { return localStorage.getItem(key); } catch { return null; }
}

export function ShellProvider({ children }: { children: ReactNode }) {
  const [railCollapsed, setRailCollapsed] = useState(() => savedPreference('partroute.railCollapsed') === 'true');
  const [listDensity, setListDensity] = useState<'comfortable' | 'compact'>(() => savedPreference('partroute.listDensity') === 'compact' ? 'compact' : 'comfortable');
  useEffect(() => {
    try {
      localStorage.setItem('partroute.railCollapsed', String(railCollapsed));
      localStorage.setItem('partroute.listDensity', listDensity);
    } catch { /* The current page preferences remain effective when browser storage is disabled. */ }
  }, [railCollapsed, listDensity]);
  const [crumbs, setCrumbs] = useState<Crumb[]>([]);
  const [project, setProject] = useState<Project | null>(null);
  const [hiders, setHiders] = useState(0);
  const hideProjectSwitcher = useCallback((delta: 1 | -1) => setHiders((n) => Math.max(0, n + delta)), []);
  const projectSwitcherHidden = hiders > 0;
  const value = useMemo(
    () => ({ crumbs, setCrumbs, project, setProject, projectSwitcherHidden, hideProjectSwitcher, railCollapsed, setRailCollapsed, listDensity, setListDensity }),
    [crumbs, project, projectSwitcherHidden, hideProjectSwitcher, railCollapsed, listDensity],
  );
  return <ShellContext.Provider value={value}>{children}</ShellContext.Provider>;
}

export function useShell(): ShellValue {
  const v = useContext(ShellContext);
  if (!v) throw new Error('useShell must be used inside <ShellProvider>');
  return v;
}

/** Set the breadcrumbs for the current page; cleared on unmount. */
export function useCrumbs(crumbs: Crumb[]): void {
  const { setCrumbs } = useShell();
  const key = JSON.stringify(crumbs);
  useEffect(() => {
    setCrumbs(JSON.parse(key) as Crumb[]);
  }, [key, setCrumbs]);
  useEffect(() => () => setCrumbs([]), [setCrumbs]);
}

/** Declare the project the page belongs to (null clears it on unmount); it becomes the current project. */
export function useShellProject(project: Project | null | undefined): void {
  const { setProject } = useShell();
  const setCurrent = useOptionalCurrentProject()?.setProjectId;
  useEffect(() => {
    if (project) setProject(project);
  }, [project, setProject]);
  const projectId = project?.id;
  useEffect(() => {
    if (projectId) setCurrent?.(projectId);
  }, [projectId, setCurrent]);
  useEffect(() => () => setProject(null), [setProject]);
}

/**
 * Hide the top project switcher when the page provides its own scope selector or lists all projects; restore it on unmount.
 */
export function useHideProjectSwitcher(): void {
  const { hideProjectSwitcher } = useShell();
  useEffect(() => {
    hideProjectSwitcher(1);
    return () => hideProjectSwitcher(-1);
  }, [hideProjectSwitcher]);
}

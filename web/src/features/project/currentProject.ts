// Current project (R1.2 §04): the project every area page works on. The choice
// is remembered per user in this browser; anything stored that the user can no
// longer see falls back to their first active project. Storage can be missing
// (private mode, blocked site data) — the app then simply forgets the choice.

import type { Project } from '../../api/types';

export const CURRENT_PROJECT_PREFIX = 'partroute.currentProject.v1.';

export interface KeyValueStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function isProjectId(v: unknown): v is string {
  return typeof v === 'string' && UUID_RE.test(v);
}

export function currentProjectKey(userId: string): string {
  return `${CURRENT_PROJECT_PREFIX}${userId}`;
}

/** localStorage when it can be used, else null. */
export function projectStorage(): KeyValueStorage | null {
  try {
    const s = globalThis.localStorage;
    if (!s) return null;
    const probe = `${CURRENT_PROJECT_PREFIX}probe`;
    s.setItem(probe, '1');
    s.removeItem(probe);
    return s;
  } catch {
    return null;
  }
}

export function readCurrentProject(storage: KeyValueStorage | null, userId: string): string | null {
  if (!storage) return null;
  try {
    const v = storage.getItem(currentProjectKey(userId));
    return isProjectId(v) ? v : null;
  } catch {
    return null;
  }
}

export function writeCurrentProject(storage: KeyValueStorage | null, userId: string, projectId: string | null): void {
  if (!storage) return;
  try {
    if (projectId && isProjectId(projectId)) storage.setItem(currentProjectKey(userId), projectId);
    else storage.removeItem(currentProjectKey(userId));
  } catch {
    /* quota / blocked storage: the choice lasts until a reload */
  }
}

/**
 * The project to work on: the one a link names (even when it is not in the
 * list yet — the page then shows its own 403 / 404), else the stored choice if
 * still visible, else the first active project, else the first one.
 */
export function resolveCurrentProject(
  projects: readonly Pick<Project, 'id' | 'status'>[] | undefined,
  urlId: string | null,
  storedId: string | null,
): string | null {
  if (isProjectId(urlId)) return urlId;
  if (!projects) return isProjectId(storedId) ? storedId : null;
  if (storedId && projects.some((p) => p.id === storedId)) return storedId;
  return (projects.find((p) => p.status !== 'archived') ?? projects[0])?.id ?? null;
}

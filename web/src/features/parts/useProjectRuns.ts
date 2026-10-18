// The project's runs, fetched once per page (newest first, one window) and
// refreshed while one of them is still active — the source of every part
// card's state and progress on /parts and the workbench.

import { useEffect } from 'react';
import { listRuns } from '../../api/endpoints';
import type { RunSummary } from '../../api/types';
import { useResource } from '../../hooks/useResource';
import { TERMINAL_RUN } from '../workflow/model';

/** The list endpoint's maximum page. */
export const RUN_WINDOW = 200;
const POLL_MS = 8000;

export interface ProjectRuns {
  runs: RunSummary[] | undefined;
  /** The window was full: parts without a run in it may have older runs. */
  windowFull: boolean;
  error: unknown;
  reload: () => void;
}

export function useProjectRuns(projectId: string | null, reloadKey = 0): ProjectRuns {
  const res = useResource((signal) => listRuns(projectId!, { limit: RUN_WINDOW }, { signal }), [projectId, reloadKey], { enabled: !!projectId });
  // Rows of another project (switch in flight) are never shown under this one.
  const runs = res.data && res.data.every((r) => r.project_id === projectId) ? res.data : undefined;
  const active = (runs ?? []).some((r) => !TERMINAL_RUN.has(r.status));
  const { reload } = res;
  useEffect(() => {
    if (!active) return;
    const id = window.setInterval(reload, POLL_MS);
    return () => window.clearInterval(id);
  }, [active, reload]);
  return { runs, windowFull: (runs?.length ?? 0) >= RUN_WINDOW, error: res.error, reload };
}

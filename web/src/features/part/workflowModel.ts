// Part workflow tab (R1.2 §07): the part's runs grouped by the workflow they
// ran. Each run is an immutable snapshot (G2) — the tab only reads them; a new
// attempt is a new run (start or rerun), never an edit of an old one.

import type { RunSummary } from '../../api/types';

/** Group key of runs without a template: AI modeling in the part workspace. */
export const WORKSPACE_GROUP = 'workspace';

export interface RunGroup {
  /** workflow_id, or WORKSPACE_GROUP. */
  key: string;
  workflowId: string | null;
  /** Workflow name when known, else the latest run's title. */
  name: string;
  /** Newest first. */
  runs: RunSummary[];
  latest: RunSummary;
}

const newestFirst = (a: RunSummary, b: RunSummary) => b.created_at.localeCompare(a.created_at) || b.id.localeCompare(a.id);

export function groupRuns(runs: readonly RunSummary[], workflowNames: ReadonlyMap<string, string>): RunGroup[] {
  const by = new Map<string, RunSummary[]>();
  for (const r of runs) {
    const key = r.origin === 'workflow' && r.workflow_id ? r.workflow_id : WORKSPACE_GROUP;
    const list = by.get(key) ?? [];
    list.push(r);
    by.set(key, list);
  }
  const groups = [...by.entries()].map(([key, list]) => {
    const sorted = list.slice().sort(newestFirst);
    const workflowId = key === WORKSPACE_GROUP ? null : key;
    return { key, workflowId, name: (workflowId && workflowNames.get(workflowId)) || sorted[0].title, runs: sorted, latest: sorted[0] };
  });
  // The workflow touched most recently first.
  return groups.sort((a, b) => newestFirst(a.latest, b.latest));
}

/**
 * The run to show: the requested one if it is in the group, else the group's
 * latest run.
 */
export function pickRun(group: RunGroup | undefined, runId: string | null): RunSummary | undefined {
  if (!group) return undefined;
  return (runId && group.runs.find((r) => r.id === runId)) || group.latest;
}

/** Group of a run (to open the right group from a ?run= link). */
export function groupOfRun(groups: readonly RunGroup[], runId: string | null): RunGroup | undefined {
  return runId ? groups.find((g) => g.runs.some((r) => r.id === runId)) : undefined;
}

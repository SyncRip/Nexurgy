// The workflows of one part (R1.2 §07, prototype "part canvas"): the part's
// own editable workflows (workflow.part_id) and the template / workspace runs
// that touched the part. Runs are immutable snapshots; editing a workflow only
// changes what the *next* run uses.

import type { RunSummary, Workflow } from '../../../api/types';
import { WORKSPACE_GROUP, groupRuns } from '../workflowModel';

export interface PartFlow {
  /** workflow id, or WORKSPACE_GROUP for AI modeling runs. */
  key: string;
  name: string;
  /** The part's own workflow (editable by workflow members); null for template / workspace runs. */
  workflow: Workflow | null;
  /** Template the runs came from (runs of a project template), when known. */
  templateId: string | null;
  /** Newest first. */
  runs: RunSummary[];
}

export function partFlows(partWorkflows: readonly Workflow[], runs: readonly RunSummary[], templateNames: ReadonlyMap<string, string>): PartFlow[] {
  const own = new Map(partWorkflows.map((w) => [w.id, w]));
  const names = new Map(templateNames);
  for (const w of partWorkflows) names.set(w.id, w.name);
  const groups = groupRuns(runs, names);
  const flows: PartFlow[] = groups.map((g) => {
    const w = g.workflowId ? (own.get(g.workflowId) ?? null) : null;
    return { key: g.key, name: w?.name ?? g.name, workflow: w, templateId: w || g.key === WORKSPACE_GROUP ? null : g.workflowId, runs: g.runs };
  });
  // Part workflows that never ran yet.
  for (const w of partWorkflows) if (!flows.some((f) => f.key === w.id)) flows.push({ key: w.id, name: w.name, workflow: w, templateId: null, runs: [] });
  const activity = (f: PartFlow) => f.runs[0]?.created_at ?? f.workflow?.updated_at ?? '';
  return flows.sort((a, b) => activity(b).localeCompare(activity(a)));
}

/** "Run 3 of 5": runs are numbered oldest first. */
export function runNumber(flow: PartFlow, runId: string): number {
  const i = flow.runs.findIndex((r) => r.id === runId);
  return i < 0 ? 0 : flow.runs.length - i;
}

export function pickFlow(flows: readonly PartFlow[], flowKey: string | null, runId: string | null): PartFlow | undefined {
  return flows.find((f) => f.key === flowKey) ?? (runId ? flows.find((f) => f.runs.some((r) => r.id === runId)) : undefined) ?? flows[0];
}

/** Node status counts for the canvas legend, in a fixed order. */
export const LEGEND_ORDER = ['succeeded', 'running', 'waiting_human', 'failed', 'ready', 'pending', 'skipped', 'cancelled'] as const;

export function legend(counts: Partial<Record<string, number>>): { status: (typeof LEGEND_ORDER)[number]; n: number }[] {
  return LEGEND_ORDER.filter((s) => (counts[s] ?? 0) > 0).map((s) => ({ status: s, n: counts[s]! }));
}

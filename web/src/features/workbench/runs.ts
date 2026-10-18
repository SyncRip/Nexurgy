import type { RunSummary } from '../../api/types';
import { laneOfRun, type Lane } from '../flows/board';

const PRIORITY: Record<Lane, number> = { attention: 0, running: 1, draft: 2, done: 3 };

/** Reuse workflow board status semantics: prioritize pending and active runs, preserving recency order within each group. */
export function workbenchRuns<T extends Pick<RunSummary, 'id' | 'status' | 'created_at'>>(runs: readonly T[]): T[] {
  return [...runs].sort((a, b) => PRIORITY[laneOfRun(a)] - PRIORITY[laneOfRun(b)] || b.created_at.localeCompare(a.created_at) || b.id.localeCompare(a.id));
}

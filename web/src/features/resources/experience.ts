// Pure helpers of the experience view (docs/resources-knowledge-handoff.zh-CN.md §4, §10): the "all projects" scope
// asks every accessible project (usePerProject in perProject.ts), keeps each project's answer (or failure) apart and
// merges the collections in the server's own order. Counts are archived collections only; a failed project is never
// counted as zero.

import type { KnowledgeCollection } from '../../api/knowledge';
import type { PerProjectEntry } from './perProject';

/** Collections asked per project in the all-projects list (the server's maximum page). */
export const ALL_SCOPE_LIMIT = 100;

/** Newest first across projects, the same order the server uses inside one project (created_at desc, id desc). */
export function mergeCollections(lists: Iterable<readonly KnowledgeCollection[]>): KnowledgeCollection[] {
  const out: KnowledgeCollection[] = [];
  for (const l of lists) out.push(...l);
  return out.sort((a, b) => {
    const ta = a.created_at ? Date.parse(a.created_at) : Number.NEGATIVE_INFINITY;
    const tb = b.created_at ? Date.parse(b.created_at) : Number.NEGATIVE_INFINITY;
    if (ta !== tb) return tb - ta;
    return a.id < b.id ? 1 : a.id > b.id ? -1 : 0;
  });
}

export interface ScopeLoad {
  /** Projects in the scope. */
  projects: number;
  /** Projects that answered (ok). */
  loaded: number;
  /** Projects without an answer for the current query yet. */
  pending: number;
  /** Projects whose request failed, in scope order. */
  failed: string[];
}

export function scopeLoad<T>(ids: readonly string[], entries: ReadonlyMap<string, PerProjectEntry<T>>): ScopeLoad {
  let loaded = 0;
  let pending = 0;
  const failed: string[] = [];
  for (const id of ids) {
    const e = entries.get(id);
    if (!e || e.status === 'loading') pending++;
    else if (e.status === 'ok') loaded++;
    else failed.push(id);
  }
  return { projects: ids.length, loaded, pending, failed };
}

export interface ScopeCount {
  /** Sum over the projects that answered; null while a project has never answered. */
  count: number | null;
  /** Some project failed, so the sum leaves it out. */
  partial: boolean;
}

/**
 * Sum of per-project totals. A project still loading its first answer makes the count unknown (null); so does a
 * scope where every project failed (nothing is known, which is not zero).
 */
export function sumTotals(ids: readonly string[], entries: ReadonlyMap<string, PerProjectEntry<number>>): ScopeCount {
  let sum = 0;
  let answered = 0;
  let partial = false;
  for (const id of ids) {
    const e = entries.get(id);
    if (e?.status === 'error') partial = true;
    else if (e?.data !== undefined) {
      sum += e.data;
      answered++;
    } else return { count: null, partial: false };
  }
  return { count: partial && !answered ? null : sum, partial };
}

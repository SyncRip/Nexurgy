// A workflow change plan (workflow_edit) as lines a person can check before
// applying it. Pure: built from the server's diff and the base graph labels.

import type { AiWorkflowEditResult, AiWorkflowEditWarning } from '../../api/assistant';

export type PlanLine =
  | { kind: 'add_node'; id: string; type: string; label: string | null }
  | { kind: 'remove_node'; id: string; type: string; label: string | null }
  | { kind: 'params'; id: string; type: string; label: string | null; changes: { key: string; before: string; after: string }[] }
  | { kind: 'add_edge' | 'remove_edge'; from: string; to: string }
  | { kind: 'rename'; id: string; before: string | null; after: string | null };

function short(v: unknown): string {
  if (v === undefined) return '—';
  const s = typeof v === 'string' ? v : JSON.stringify(v);
  return s.length > 40 ? `${s.slice(0, 39)}…` : s;
}

export function planLines(r: Pick<AiWorkflowEditResult, 'diff' | 'graph_before' | 'graph_after'>): PlanLine[] {
  const label = (id: string) => (r.graph_before.nodes.find((n) => n.id === id) ?? r.graph_after.nodes.find((n) => n.id === id))?.label ?? null;
  const d = r.diff;
  return [
    ...d.added_nodes.map((n): PlanLine => ({ kind: 'add_node', id: n.id, type: n.type, label: n.label })),
    ...d.removed_nodes.map((n): PlanLine => ({ kind: 'remove_node', id: n.id, type: n.type, label: n.label })),
    ...d.changed_params.map(
      (c): PlanLine => ({ kind: 'params', id: c.node_id, type: c.type, label: label(c.node_id), changes: c.keys.map((k) => ({ key: k, before: short(c.before[k]), after: short(c.after[k]) })) }),
    ),
    ...d.renamed.map((x): PlanLine => ({ kind: 'rename', id: x.node_id, before: x.before, after: x.after })),
    ...d.added_edges.map((e): PlanLine => ({ kind: 'add_edge', from: `${e.source}.${e.source_port}`, to: `${e.target}.${e.target_port}` })),
    ...d.removed_edges.map((e): PlanLine => ({ kind: 'remove_edge', from: `${e.source}.${e.source_port}`, to: `${e.target}.${e.target_port}` })),
  ];
}

/** Warnings that need the person's eyes: auto-apply never applies a plan carrying one of these. */
const HOLD = new Set(['private_context', 'removes_review', 'workflow_moved_on']);

export function holdsAutoApply(warnings: readonly AiWorkflowEditWarning[]): boolean {
  return warnings.some((w) => HOLD.has(w.code));
}

/** Warnings worth showing (validation problems the workflow already had are left to the editor). */
export function shownWarnings(warnings: readonly AiWorkflowEditWarning[]): AiWorkflowEditWarning[] {
  return warnings.filter((w) => !w.preexisting);
}

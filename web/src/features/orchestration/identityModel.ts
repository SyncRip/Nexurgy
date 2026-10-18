// Generation reliability K (design .trellis/tasks/archive/2026-10/10-13-generation-reliability/design.md §2.5): the coverage block's
// identity classes and the frozen original target next to the current one. Pure helpers (identityModel.test.ts).

import type { AssemblyCoverage } from '../../api/orchestration';

export interface IdentityView {
  counts: { parts: number; subs: number; aliases: number; excluded: number; unresolved: number; unbound: number };
  baseline: { label: string; defs: number; insts: number } | null;
  current: { defs: number; insts: number };
  /** The current target differs from the original one (its changes are recorded on the spec). */
  changed: boolean;
}

/** null on blocks from before generation reliability K (no identity, no baseline). */
export function identityView(c: Pick<AssemblyCoverage, 'identity' | 'baseline' | 'target'>): IdentityView | null {
  if (!c.identity && !c.baseline) return null;
  const i = c.identity ?? { parts: 0, subassemblies: 0, aliases: 0, excluded: 0, unresolved: 0 };
  const b = c.baseline ?? null;
  const current = { defs: c.target.definitions, insts: c.target.instances };
  return {
    counts: { parts: i.parts, subs: i.subassemblies, aliases: i.aliases, excluded: i.excluded, unresolved: i.unresolved, unbound: i.unbound ?? 0 },
    baseline: b ? { label: b.label || '—', defs: b.definitions, insts: b.instances } : null,
    current,
    changed: !!b && (b.definitions !== current.defs || b.instances !== current.insts),
  };
}

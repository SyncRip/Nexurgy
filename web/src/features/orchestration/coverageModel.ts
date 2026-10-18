// Generation supplement B (partial assembly): pure helpers that read the coverage block of an assembly build
// (server services/spec_coverage.coverage) for the run card, the chain value, the assembly page and the delivery.
// The target never shrinks: what failed stays counted. Unit-tested in coverageModel.test.ts.

import type { AssemblyCoverage, CoverageMissing, CoverageStage, DecisionUse } from '../../api/orchestration';

/** Stages in the order the work happens (and the missing list is grouped). */
export const COVERAGE_STAGES: readonly CoverageStage[] = ['extraction', 'structure', 'modeling', 'placement', 'assembly'];

export interface CoverageCounts {
  /** Part definitions / instances the spec asks for under its root (the target). */
  targetDefinitions: number;
  targetInstances: number;
  subassemblies: number;
  /** Part definitions the spec holds at all (under the root or not). */
  recognized: number;
  /** Part definitions with valid geometry; missing or invalid legacy counts remain unknown. */
  built: number | null;
  /** Instances installed (stated placement, mates or a recorded AI placement decision) — along the whole parent chain
   * up to the root (generation reliability §2.6). */
  placed: number;
  /** Positioned only locally, without a resolved position up to the root assembly (never installed). */
  localOnly: number;
  /** Of the installed instances, those placed by a recorded Turbo AI decision (generation supplement F). */
  decided: number;
  /** Of the installed instances, those at an AI-estimated position (generation reliability round 2, P): installed, shown apart. */
  estimated: number;
  /** Instances only arranged for display by a recorded rule decision (never counted as installed). */
  arranged: number;
  /** Missing objects (definitions, sub-assemblies, instances); relations are listed but not counted here. */
  failed: number;
}

function builtDefinitionCount(c: AssemblyCoverage | null | undefined): number | null {
  const value = c?.built?.definitions;
  return typeof value === 'number' && Number.isInteger(value) && value >= 0 ? value : null;
}

export function coverageCounts(c: AssemblyCoverage): CoverageCounts {
  return {
    targetDefinitions: c.target.definitions,
    targetInstances: c.target.instances,
    subassemblies: c.target.subassemblies,
    recognized: c.target.recognized_definitions,
    built: builtDefinitionCount(c),
    placed: c.placed.instances,
    localOnly: c.local_only?.instances ?? 0,
    decided: c.decided?.instances ?? 0,
    estimated: c.estimated?.instances ?? 0,
    arranged: c.arranged?.instances ?? 0,
    failed: c.missing.filter((m) => m.kind !== 'relation').length,
  };
}

export interface MissingGroup {
  stage: CoverageStage | string;
  items: CoverageMissing[];
}

/** The missing items grouped by stage, in work order (unknown stages last), empty groups left out. */
export function groupMissing(c: AssemblyCoverage | null | undefined): MissingGroup[] {
  const items = c?.missing ?? [];
  const known = COVERAGE_STAGES.map((stage) => ({ stage, items: items.filter((m) => m.stage === stage) }));
  const other = items.filter((m) => !(COVERAGE_STAGES as readonly string[]).includes(m.stage));
  return [...known, ...(other.length ? [{ stage: 'other', items: other }] : [])].filter((g) => g.items.length > 0);
}

/** Separate marks of a product (design §2.2: never folded into one state). */
export interface CoverageMarks {
  /** An assembly exists but does not hold its whole target. */
  partial: boolean;
  /** No assembly was made; this alone says nothing about usable parts. */
  noAssembly: boolean;
  /** Valid part geometry reported by this build; legacy missing counts remain unknown. */
  parts: 'available' | 'none' | 'unknown';
  /** AI / rule decisions it rests on (0: none). */
  aiDecisions: number;
}

export function coverageMarks(c: AssemblyCoverage | null | undefined, aiDecisions?: number | null): CoverageMarks {
  const built = builtDefinitionCount(c);
  const parts = built === null ? 'unknown' : built === 0 ? 'none' : 'available';
  return { partial: c?.status === 'partial', noAssembly: c?.status === 'none', parts, aiDecisions: Math.max(0, aiDecisions ?? 0) };
}

/** The coverage block stored on a root assembly revision (``spec_ref.coverage``), when it has one. */
export function coverageOf(specRef: Record<string, unknown> | null | undefined): AssemblyCoverage | null {
  const c = specRef?.coverage as Partial<AssemblyCoverage> | undefined;
  return c && typeof c === 'object' && typeof c.status === 'string' && c.target && Array.isArray(c.missing) ? (c as AssemblyCoverage) : null;
}

/** AI / rule decisions among assumption records (a person's own answers are not AI decisions). */
export function aiDecisionCount(assumptions: readonly { author?: string | null }[] | null | undefined): number {
  return (assumptions ?? []).filter((a) => a && a.author !== 'person').length;
}

interface FailureLike {
  code?: string | null;
  recovery?: string | null;
  retryable?: boolean | null;
}

/** "Continue remaining work": the build left definitions to rebuild and continuing can do it (the server re-checks). */
export function canContinueBuild(f: FailureLike | null | undefined): boolean {
  return !!f && f.recovery === 'retry_definition' && f.retryable === true;
}

// --- generation reliability §2.6 (package L): the parent chain and where decisions were used -----------------------------

export interface SubassemblyPlacement {
  /** Sub-assembly instances (spec keys) placed up to the root. */
  placed: number;
  total: number;
  /** Kept sub-assembly instances without a position (their parts are not installed). */
  unplaced: string[];
}

/** The sub-assembly part of the coverage; null on blocks from before or without sub-assemblies. */
export function subassemblyPlacement(c: AssemblyCoverage | null | undefined): SubassemblyPlacement | null {
  const s = c?.subassembly_placement;
  if (!s || !s.total) return null;
  return { placed: s.placed, total: s.total, unplaced: [...(s.unplaced ?? [])] };
}

/** Stages a decision can be used in, in work order. */
export const USE_STAGES: readonly string[] = ['spec', 'modeling', 'placement', 'drawing', 'export'];

export interface UseGroup {
  stage: string;
  /** What used it: a revision label ("P-001 R1", "D07.00 A2"), a delivered file path, else the object id. */
  objects: string[];
  /** The spec keys it reached there (definitions, instances), each once. */
  refs: string[];
}

/** Where a decision was used, grouped by stage in work order (unknown stages last), each object and ref once. */
export function decisionUses(uses: readonly DecisionUse[] | null | undefined): UseGroup[] {
  const groups = new Map<string, UseGroup>();
  for (const u of uses ?? []) {
    if (!u || !u.stage) continue;
    const g = groups.get(u.stage) ?? { stage: u.stage, objects: [], refs: [] };
    const what = u.object?.label || u.object?.path || u.object?.id || '';
    if (what && !g.objects.includes(what)) g.objects.push(what);
    for (const r of Array.isArray(u.ref) ? u.ref : u.ref ? [u.ref] : []) if (!g.refs.includes(String(r))) g.refs.push(String(r));
    groups.set(u.stage, g);
  }
  const rank = (s: string) => (USE_STAGES.includes(s) ? USE_STAGES.indexOf(s) : USE_STAGES.length);
  return [...groups.values()].sort((a, b) => rank(a.stage) - rank(b.stage));
}

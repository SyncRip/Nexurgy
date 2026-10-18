// Pure helpers of the execution-process view (no React): status tones, what is open by default, the one-line summary.
// Design: .trellis/tasks/10-17-agent-progress-visibility/design.md §3.

import type { ProgressAssumption, ProgressStage, ProgressUnit, RunProgress } from '../../api/progress';
import type { Tone } from '../../components/ui/Badges';
import { durationBetween } from '../workflow/model';

const TERMINAL = new Set(['succeeded', 'failed', 'partial', 'cancelled']);
const STAGE_ACTIVE = new Set(['ready', 'running', 'waiting_human']);
const UNIT_FAILED = new Set(['failed', 'blocked', 'needs_clarification']);

export const isTerminalProgress = (p: RunProgress | null | undefined): boolean => !!p && TERMINAL.has(p.status);

export function stageTone(status: string): Tone {
  if (status === 'succeeded') return 'ok';
  if (status === 'failed') return 'err';
  if (status === 'waiting_human') return 'warn';
  if (STAGE_ACTIVE.has(status)) return 'accent';
  return 'pending';
}

export function unitTone(status: string): Tone {
  if (status === 'built' || status === 'done') return 'ok';
  if (status === 'failed') return 'err';
  if (UNIT_FAILED.has(status)) return 'warn';
  if (status === 'pending') return 'accent';
  return 'pending';
}

export const unitFailed = (u: ProgressUnit): boolean => UNIT_FAILED.has(u.status);

/** A stage is shown open while it runs or when it (or one of its units) failed; finished stages stay closed. */
export function stageOpenByDefault(s: ProgressStage): boolean {
  return STAGE_ACTIVE.has(s.status) || s.status === 'failed' || s.units.some(unitFailed);
}

/** The whole view starts open while the run is active or when it failed; a successful run reads as one line. */
export function openByDefault(p: RunProgress): boolean {
  if (!isTerminalProgress(p)) return true;
  return p.status !== 'succeeded' || p.stages.some((s) => s.status === 'failed' || s.units.some(unitFailed));
}

/** Elapsed time of a stage or the run; `now` advances it while it runs. */
export function elapsedMs(item: { started_at: string | null; finished_at: string | null }, now: number): number | null {
  return durationBetween(item.started_at, item.finished_at, now);
}

export interface ProgressSummary {
  parts: number;
  partsBuilt: number;
  calls: number;
  failedCalls: number;
  tokens: number | null;
  assumptions: number;
  checksSkipped: boolean;
}

export function summarize(p: RunProgress): ProgressSummary {
  const decisions = p.stages.flatMap((s) => [...s.decisions, ...s.units.flatMap((u) => u.decisions)]);
  return {
    parts: p.totals.units,
    partsBuilt: p.totals.units_built,
    calls: p.totals.calls,
    failedCalls: p.totals.failed_calls,
    tokens: totalTokens(p.totals),
    assumptions: decisions.filter((d) => d.kind === 'assumption' && !isLegacyModelNote(d)).length,
    checksSkipped: decisions.some((d) => d.kind === 'check_skipped'),
  };
}

/** Units worth listing: part definitions always; other units (spec chunks …) only when they called a model. */
export function visibleUnits(s: ProgressStage): ProgressUnit[] {
  return s.units.filter((u) => u.kind === 'definition' || u.kind === 'model' || unitFailed(u) || (u.calls?.count ?? 0) > 0);
}

/** Failed units remain visible even beyond the compact preview limit. */
export function previewUnits(units: ProgressUnit[], limit: number): ProgressUnit[] {
  return units.filter((u, index) => index < limit || unitFailed(u));
}

export function totalTokens(c: { tokens_in: number | null; tokens_out: number | null }): number | null {
  return c.tokens_in === null || c.tokens_out === null ? null : c.tokens_in + c.tokens_out;
}

/** Older modeling replies stored plain explanatory text, without making a structured AI decision. */
export function isLegacyModelNote(a: ProgressAssumption): boolean {
  return !!(a.basis || a.text) && !a.subject && !a.field && a.adopted == null && !a.author && !a.unit
    && !a.trigger && !a.state && a.confirmation == null && a.replaced_by == null && !a.problems?.length
    && !a.affected?.length && !a.alternatives?.length && !a.sources?.length;
}

export interface DisclosureState {
  identity: string;
  status: string;
  failures: string[];
  open: boolean;
}

/** Ordinary snapshots preserve the person's choice; a new failure or lifecycle transition resets the default. */
export function reconcileDisclosure(previous: DisclosureState | null, next: Omit<DisclosureState, 'open'>, defaultOpen: boolean): DisclosureState {
  if (!previous || previous.identity !== next.identity || previous.status !== next.status) return { ...next, open: defaultOpen };
  if (next.failures.join('\n') === previous.failures.join('\n')) return previous;
  return { ...next, open: previous.open || next.failures.some((key) => !previous.failures.includes(key)) };
}

export function stageFailures(stage: ProgressStage): string[] {
  return [
    ...(stage.status === 'failed' ? [stage.node_id] : []),
    ...stage.units.filter(unitFailed).map((u) => `${stage.node_id}/${u.key}/${u.status}`),
  ];
}

export function progressDisclosure(previous: DisclosureState | null, runId: string, p: RunProgress | null, active: boolean): DisclosureState {
  return reconcileDisclosure(previous, {
    identity: runId,
    status: p?.status ?? (active ? 'running' : 'unknown'),
    failures: p?.stages.flatMap(stageFailures) ?? [],
  }, p ? openByDefault(p) : active);
}

/** The work-unit key without its kind ("spec_chunk:2" → "2"). */
export function unitIndex(key: string): string {
  const i = key.indexOf(':');
  return i < 0 ? '' : key.slice(i + 1);
}

/** "12.3k" style token counts. */
export function compactTokens(n: number): string {
  if (n < 1000) return String(n);
  if (n < 100_000) return `${(n / 1000).toFixed(1).replace(/\.0$/, '')}k`;
  return `${Math.round(n / 1000)}k`;
}

/** An adopted value as people read it: lists as "(4, 324, 0)", objects as "key: value" pairs, numbers trimmed. */
export function valueText(v: unknown): string {
  if (v === null || v === undefined) return '';
  if (Array.isArray(v)) return `(${v.map(valueText).join(', ')})`;
  if (typeof v === 'object') return Object.entries(v as Record<string, unknown>).map(([k, x]) => `${k}: ${valueText(x)}`).join(', ');
  if (typeof v === 'number') return String(+v.toFixed(3));
  return String(v);
}

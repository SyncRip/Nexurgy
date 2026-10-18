// Pure helpers of the W7 Turbo UI (no React; unit-tested in model.test.ts).

import type { Assumption, CheckStatus, ExecutionPolicy } from '../../api/engineering';

/** The policy of a server row; rows from before W7 (no field) are standard, never Turbo. */
export function policyOf(row: { execution_policy?: ExecutionPolicy | string | null } | null | undefined): ExecutionPolicy {
  return row?.execution_policy === 'turbo' ? 'turbo' : 'standard';
}

function fmtNumber(v: number): string {
  const r = Math.round(v * 1000) / 1000;
  return Object.is(r, -0) ? '0' : String(r);
}

function fmtValue(v: unknown): string {
  if (v === null || v === undefined) return '';
  if (typeof v === 'number') return fmtNumber(v);
  if (typeof v === 'boolean') return v ? 'true' : 'false';
  if (typeof v === 'string') return v;
  if (Array.isArray(v)) {
    if (v.every((x) => typeof x === 'number')) return v.map((x) => fmtNumber(x as number)).join(' × ');
    return v.map((x) => (Array.isArray(x) ? `(${fmtValue(x).replace(/ × /g, ', ')})` : fmtValue(x))).join('; ');
  }
  if (typeof v === 'object') {
    return Object.entries(v as Record<string, unknown>)
      .filter(([, x]) => x !== null && x !== undefined)
      .map(([k, x]) => `${k}: ${fmtValue(x)}`)
      .join(', ');
  }
  return String(v);
}

/** The adopted value with its unit ("100 × 60 × 10 mm"), or null when the assumption leaves the value open. */
export function formatAdopted(a: Pick<Assumption, 'adopted' | 'unit'>): string | null {
  const text = fmtValue(a.adopted);
  if (!text) return null;
  return a.unit ? `${text} ${a.unit}` : text;
}

/** The part of a verification block that says why an artifact is not verified yet (W0 §2.2). */
export interface VerificationReasons {
  execution_policy?: ExecutionPolicy | string | null;
  check_status?: string | null;
  unverified?: boolean;
  pending_checks?: string[] | null;
  unconfirmed_assumptions?: unknown[] | null;
  corrected_assumptions?: unknown[] | null;
  upstream_unverified?: unknown;
}

/** Whether an artifact needs the verification panel: Turbo origin, a skipped / open check, an AI / rule value no
 * person decided, a corrected value, or an unverified upstream. A standard manual artifact that simply has no
 * requirement check (``not_requested``) needs nothing. */
export function needsVerification(v: VerificationReasons | null | undefined): boolean {
  if (!v) return false;
  return (
    v.execution_policy === 'turbo' ||
    v.check_status === 'skipped_turbo' ||
    !!v.upstream_unverified ||
    !!v.pending_checks?.length ||
    !!v.unconfirmed_assumptions?.length ||
    !!v.corrected_assumptions?.length
  );
}

// ─── S6 status precedence (agent execution layer design §3.2): failed > not verified > passed ──────────────────

/** Whether a check failed: the explicit status of the row when given, else the verification block's own. */
export function checkFailed(v: Pick<VerificationReasons, 'check_status'> | null | undefined, checkStatus?: string | null): boolean {
  return (checkStatus ?? v?.check_status) === 'failed';
}

/** "Done · not verified" applies (instead of a bare check status): unverified for one of the reasons above, and no
 * check failed — a failed check always reads "check failed", never "done · not verified". */
export function showsUnverified(v: VerificationReasons | null | undefined): boolean {
  return !!v?.unverified && !checkFailed(v) && needsVerification(v);
}

/** For rows that show their check status chip separately: whether the "done · not verified" chip may stand next to
 * it. Never beside a failed check (the two would contradict each other). */
export function unverifiedBesideCheck(v: Pick<VerificationReasons, 'unverified' | 'check_status'> | null | undefined, checkStatus?: string | null): boolean {
  return !!v?.unverified && !checkFailed(v, checkStatus);
}

/** The one artifact-level state of a verification block: ``failed`` (whatever ``unverified`` says), then
 * ``unverified`` ("done · not verified"), else the check status itself (``passed`` …). Null without a block. */
export type ArtifactCheckState = CheckStatus | 'unverified';

const CHECK_STATUSES: readonly CheckStatus[] = ['not_requested', 'skipped_turbo', 'pending', 'passed', 'failed', 'unknown'];

export function artifactCheckState(v: VerificationReasons | null | undefined): ArtifactCheckState | null {
  if (!v) return null;
  if (checkFailed(v)) return 'failed';
  if (showsUnverified(v)) return 'unverified';
  const s = v.check_status;
  if (!s) return null;
  return (CHECK_STATUSES as readonly string[]).includes(s) ? (s as CheckStatus) : 'unknown';
}

/** Run level (S6): only a run that succeeded as a whole may read "done · not verified"; a failed or partial run
 * (a required step failed) reads failed, its products' states are shown on the products. */
export function runShowsUnverified(run: { status: string; execution_policy?: ExecutionPolicy | string | null }): boolean {
  return run.status === 'succeeded' && policyOf(run) === 'turbo';
}

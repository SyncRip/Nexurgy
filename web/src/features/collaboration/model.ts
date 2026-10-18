// Pure helpers of the collaboration UI (no React): the four internal stages a
// node phase maps to, idempotency keys for plan / start / fork requests, and the
// per-criterion decision readiness of a human final acceptance. Unit-tested in
// model.test.ts.

import type { CriterionResult, NodeTaskPhase } from '../../api/collaboration';
import { IDEMPOTENCY_KEY_MAX } from '../../api/collaboration';

// ─── internal stages (design §0: execute / check / submit / review are phases, not tasks) ──

export const STAGES = ['execute', 'check', 'submit', 'review'] as const;
export type Stage = (typeof STAGES)[number];
/** skipped (W7 Turbo): the check / submit / review stages a Turbo node never runs. */
export type StageState = 'done' | 'active' | 'todo' | 'failed' | 'blocked' | 'skipped';

/** State of each internal stage for a node phase (progress dots on the node card). */
export function stageStates(phase: NodeTaskPhase): Record<Stage, StageState> {
  switch (phase) {
    case 'blocked':
      return { execute: 'blocked', check: 'todo', submit: 'todo', review: 'todo' };
    case 'ready':
      return { execute: 'todo', check: 'todo', submit: 'todo', review: 'todo' };
    case 'executing':
    case 'rework':
      return { execute: 'active', check: 'todo', submit: 'todo', review: 'todo' };
    case 'submitted':
      return { execute: 'done', check: 'done', submit: 'active', review: 'todo' };
    case 'in_review':
      return { execute: 'done', check: 'done', submit: 'done', review: 'active' };
    case 'accepted':
      return { execute: 'done', check: 'done', submit: 'done', review: 'done' };
    case 'failed':
      return { execute: 'failed', check: 'todo', submit: 'todo', review: 'todo' };
    case 'cancelled':
      return { execute: 'todo', check: 'todo', submit: 'todo', review: 'todo' };
    case 'completed_unverified':
      return { execute: 'done', check: 'skipped', submit: 'skipped', review: 'skipped' };
  }
}

/** The phase a node shows (S6, failed > not verified > passed): a Turbo node whose released outputs failed their
 * check reads "check failed", never "done · not verified". Other phases show as they are. */
export function displayPhase(node: { phase: NodeTaskPhase; verification?: { check_status?: string | null } | null }): NodeTaskPhase | 'check_failed' {
  return node.phase === 'completed_unverified' && node.verification?.check_status === 'failed' ? 'check_failed' : node.phase;
}

/** Failed nodes of a run for its progress line (S6: a failed node never counts as done): ``failed`` = phase failed
 * plus Turbo nodes whose check failed; ``checkFailed`` = the latter, which the server counts under "unverified". */
export function failedNodeCounts(nodes: readonly { phase: NodeTaskPhase; verification?: { check_status?: string | null } | null }[]): { failed: number; checkFailed: number } {
  let failed = 0;
  let checkFailed = 0;
  for (const n of nodes) {
    const shown = displayPhase(n);
    if (shown === 'failed') failed++;
    else if (shown === 'check_failed') {
      failed++;
      checkFailed++;
    }
  }
  return { failed, checkFailed };
}

/** Phases in which the node still needs something to happen. */
export const OPEN_PHASES: ReadonlySet<NodeTaskPhase> = new Set(['blocked', 'ready', 'executing', 'submitted', 'in_review', 'rework']);

// ─── idempotency keys (8–120 chars; the same content must give the same key) ──

/** FNV-1a 32-bit hash as 8 hex characters (stable across sessions). */
export function stableHash(text: string): string {
  let h = 0x811c9dc5;
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h.toString(16).padStart(8, '0');
}

/** Canonical JSON (sorted keys) so equal requests hash equally regardless of key order. */
export function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value && typeof value === 'object') {
    const o = value as Record<string, unknown>;
    return `{${Object.keys(o)
      .sort()
      .filter((k) => o[k] !== undefined)
      .map((k) => `${JSON.stringify(k)}:${canonical(o[k])}`)
      .join(',')}}`;
  }
  return JSON.stringify(value ?? null);
}

/** A random per-page seed so two browsers never produce the same key for the same content. */
export function newSeed(): string {
  const bytes = new Uint8Array(6);
  if (typeof crypto !== 'undefined' && crypto.getRandomValues) crypto.getRandomValues(bytes);
  else for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
}

/**
 * `<prefix>-<seed>-<hash of parts>`: repeated clicks with the same content reuse the
 * key (the server returns the original draft / instance), changed content gets a new one.
 */
export function idempotencyKey(prefix: string, seed: string, parts: unknown): string {
  const key = `${prefix}-${seed}-${stableHash(canonical(parts))}`;
  return key.length > IDEMPOTENCY_KEY_MAX ? key.slice(0, IDEMPOTENCY_KEY_MAX) : key.padEnd(8, '0');
}

// ─── human final acceptance (UI03, C05) ────────────────────────────────────

export interface CriterionEntry {
  result: CriterionResult | null;
  reason: string;
  evidence: string[];
}

export interface AcceptanceReadiness {
  /** Required criteria without a result. */
  undecided: number;
  /** Decided criteria without a reason (the server needs 1–4000 chars). */
  missingReason: number;
  /** Required criteria decided fail / unknown. */
  notPassed: number;
  /** Every required criterion has a result and every decided one a reason: a reject can be sent. */
  complete: boolean;
  /** complete and every required criterion passes: an accept can be sent. */
  acceptable: boolean;
}

export function acceptanceReadiness(criteria: { id: string; required: boolean }[], entries: Record<string, CriterionEntry | undefined>): AcceptanceReadiness {
  let undecided = 0;
  let missingReason = 0;
  let notPassed = 0;
  for (const c of criteria) {
    const e = entries[c.id];
    if (!e?.result) {
      if (c.required) undecided++;
      continue;
    }
    if (!e.reason.trim()) missingReason++;
    if (c.required && e.result !== 'pass') notPassed++;
  }
  const complete = undecided === 0 && missingReason === 0;
  return { undecided, missingReason, notPassed, complete, acceptable: complete && notPassed === 0 };
}

/** The decided entries as the C05 body (undecided optional criteria are left out). */
export function criterionDecisionsBody(entries: Record<string, CriterionEntry | undefined>) {
  return Object.entries(entries)
    .filter((kv): kv is [string, CriterionEntry] => !!kv[1]?.result)
    .map(([criterion_id, e]) => ({ criterion_id, result: e.result as CriterionResult, reason: e.reason.trim(), evidence_asset_version_ids: e.evidence }));
}

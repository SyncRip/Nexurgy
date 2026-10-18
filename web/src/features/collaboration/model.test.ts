import { describe, expect, it } from 'vitest';
import { acceptanceReadiness, canonical, criterionDecisionsBody, displayPhase, failedNodeCounts, idempotencyKey, stableHash, stageStates } from './model';

describe('S6 node status', () => {
  it('a Turbo node whose outputs failed their check reads check_failed, not "done · not verified"', () => {
    expect(displayPhase({ phase: 'completed_unverified', verification: { check_status: 'failed' } })).toBe('check_failed');
    expect(displayPhase({ phase: 'completed_unverified', verification: { check_status: 'skipped_turbo' } })).toBe('completed_unverified');
    expect(displayPhase({ phase: 'completed_unverified', verification: null })).toBe('completed_unverified');
    expect(displayPhase({ phase: 'accepted' })).toBe('accepted');
  });

  it('progress counts failed nodes (and Turbo nodes whose check failed) as failed, never as done', () => {
    const nodes = [
      { phase: 'failed' as const },
      { phase: 'completed_unverified' as const, verification: { check_status: 'failed' } },
      { phase: 'completed_unverified' as const, verification: { check_status: 'skipped_turbo' } },
      { phase: 'accepted' as const },
    ];
    expect(failedNodeCounts(nodes)).toEqual({ failed: 2, checkFailed: 1 });
    expect(failedNodeCounts([])).toEqual({ failed: 0, checkFailed: 0 });
  });
});

describe('collaboration stage dots', () => {
  it('maps every phase to the four internal stages', () => {
    expect(stageStates('accepted')).toEqual({ execute: 'done', check: 'done', submit: 'done', review: 'done' });
    expect(stageStates('in_review').review).toBe('active');
    expect(stageStates('submitted')).toMatchObject({ execute: 'done', check: 'done', submit: 'active' });
    expect(stageStates('rework').execute).toBe('active');
    expect(stageStates('blocked').execute).toBe('blocked');
    expect(stageStates('failed').execute).toBe('failed');
  });
});

describe('idempotency keys', () => {
  it('is stable for the same content regardless of key order and changes with the content', () => {
    const a = idempotencyKey('plan', 'abc123', { demand: 'x', mode: 'full_auto' });
    const b = idempotencyKey('plan', 'abc123', { mode: 'full_auto', demand: 'x' });
    const c = idempotencyKey('plan', 'abc123', { demand: 'y', mode: 'full_auto' });
    expect(a).toBe(b);
    expect(a).not.toBe(c);
    expect(a.length).toBeGreaterThanOrEqual(8);
    expect(a.length).toBeLessThanOrEqual(120);
  });

  it('hashes deterministically and drops undefined fields', () => {
    expect(stableHash('partroute')).toBe(stableHash('partroute'));
    expect(canonical({ b: 1, a: undefined, c: [1, { z: null }] })).toBe('{"b":1,"c":[1,{"z":null}]}');
  });
});

describe('human final acceptance readiness', () => {
  const criteria = [
    { id: 'c1', required: true },
    { id: 'c2', required: true },
    { id: 'c3', required: false },
  ];

  it('needs every required criterion decided with a reason before any decision', () => {
    const r = acceptanceReadiness(criteria, { c1: { result: 'pass', reason: 'ok', evidence: [] } });
    expect(r).toMatchObject({ undecided: 1, complete: false, acceptable: false });
    const r2 = acceptanceReadiness(criteria, { c1: { result: 'pass', reason: 'ok', evidence: [] }, c2: { result: 'pass', reason: '', evidence: [] } });
    expect(r2).toMatchObject({ undecided: 0, missingReason: 1, complete: false });
  });

  it('accepts only when every required criterion passes; unknown / fail still allow a reject', () => {
    const entries = { c1: { result: 'pass' as const, reason: 'ok', evidence: ['v1'] }, c2: { result: 'unknown' as const, reason: 'no evidence', evidence: [] } };
    const r = acceptanceReadiness(criteria, entries);
    expect(r).toMatchObject({ complete: true, acceptable: false, notPassed: 1 });
    expect(acceptanceReadiness(criteria, { ...entries, c2: { result: 'pass', reason: 'fine', evidence: [] } }).acceptable).toBe(true);
    expect(criterionDecisionsBody(entries)).toEqual([
      { criterion_id: 'c1', result: 'pass', reason: 'ok', evidence_asset_version_ids: ['v1'] },
      { criterion_id: 'c2', result: 'unknown', reason: 'no evidence', evidence_asset_version_ids: [] },
    ]);
  });
});

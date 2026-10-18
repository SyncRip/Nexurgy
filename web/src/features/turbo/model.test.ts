import { describe, expect, it } from 'vitest';
import { artifactCheckState, checkFailed, formatAdopted, needsVerification, policyOf, runShowsUnverified, showsUnverified, unverifiedBesideCheck } from './model';

describe('S6 status precedence: failed > not verified > passed', () => {
  const turbo = { execution_policy: 'turbo', unverified: true, pending_checks: [], unconfirmed_assumptions: [] };

  it('a failed check reads failed whatever unverified says', () => {
    const v = { ...turbo, check_status: 'failed' };
    expect(checkFailed(v)).toBe(true);
    expect(showsUnverified(v)).toBe(false);
    expect(artifactCheckState(v)).toBe('failed');
    // also with AI values still waiting for a person
    expect(artifactCheckState({ ...v, unconfirmed_assumptions: [{ id: 'A-01', subject: 'S.length' }] })).toBe('failed');
    expect(artifactCheckState({ execution_policy: 'standard', check_status: 'failed', unverified: true })).toBe('failed');
  });

  it('checks not done (skipped / pending / not requested) or unconfirmed values read "done · not verified"', () => {
    expect(artifactCheckState({ ...turbo, check_status: 'skipped_turbo' })).toBe('unverified');
    expect(artifactCheckState({ ...turbo, check_status: 'pending' })).toBe('unverified');
    expect(artifactCheckState({ execution_policy: 'standard', check_status: 'passed', unverified: true, unconfirmed_assumptions: [{ id: 'A-01' }] })).toBe('unverified');
  });

  it('a passed check reads passed; a manual revision without checks reads not requested', () => {
    expect(artifactCheckState({ execution_policy: 'standard', check_status: 'passed', unverified: false })).toBe('passed');
    expect(artifactCheckState({ execution_policy: 'turbo', check_status: 'passed', unverified: false })).toBe('passed');
    expect(artifactCheckState({ execution_policy: 'standard', check_status: 'not_requested', unverified: true })).toBe('not_requested');
    expect(artifactCheckState({ check_status: 'odd' })).toBe('unknown');
    expect(artifactCheckState(null)).toBeNull();
  });

  it('never shows "done · not verified" beside a failed check chip (row status or block status)', () => {
    expect(unverifiedBesideCheck({ unverified: true, check_status: 'skipped_turbo' })).toBe(true);
    expect(unverifiedBesideCheck({ unverified: true, check_status: 'failed' })).toBe(false);
    expect(unverifiedBesideCheck({ unverified: true, check_status: 'skipped_turbo' }, 'failed')).toBe(false);
    expect(unverifiedBesideCheck({ unverified: false, check_status: 'passed' })).toBe(false);
    expect(unverifiedBesideCheck(undefined)).toBe(false);
  });

  it('run level: only a Turbo run that succeeded reads "done · not verified"; failed / partial never do', () => {
    expect(runShowsUnverified({ status: 'succeeded', execution_policy: 'turbo' })).toBe(true);
    expect(runShowsUnverified({ status: 'partial', execution_policy: 'turbo' })).toBe(false);
    expect(runShowsUnverified({ status: 'failed', execution_policy: 'turbo' })).toBe(false);
    expect(runShowsUnverified({ status: 'running', execution_policy: 'turbo' })).toBe(false);
    expect(runShowsUnverified({ status: 'succeeded', execution_policy: 'standard' })).toBe(false);
  });
});

describe('turbo model', () => {
  it('reads a missing policy as standard, never as turbo', () => {
    expect(policyOf(null)).toBe('standard');
    expect(policyOf({})).toBe('standard');
    expect(policyOf({ execution_policy: 'turbo' })).toBe('turbo');
    expect(policyOf({ execution_policy: 'something' })).toBe('standard');
  });

  it('formats adopted values with their unit', () => {
    expect(formatAdopted({ adopted: [100, 60, 10], unit: 'mm' })).toBe('100 × 60 × 10 mm');
    expect(formatAdopted({ adopted: [[0, 0, 10]], unit: 'mm' })).toBe('(0, 0, 10) mm');
    expect(formatAdopted({ adopted: 1, unit: 'pcs' })).toBe('1 pcs');
    expect(formatAdopted({ adopted: true, unit: null })).toBe('true');
    expect(formatAdopted({ adopted: '6061-T6 aluminium', unit: null })).toBe('6061-T6 aluminium');
    expect(formatAdopted({ adopted: null, unit: 'mm' })).toBeNull();
  });

  it('shows the verification panel for a standard drawing of an unverified revision (review finding 11)', () => {
    const standardDrawing = { execution_policy: 'standard', check_status: 'passed', unverified: true, pending_checks: [], unconfirmed_assumptions: [] };
    expect(needsVerification(standardDrawing)).toBe(false);
    const upstream = { ...standardDrawing, upstream_unverified: { type: 'model_revision', id: 'r1' } };
    expect(needsVerification(upstream)).toBe(true);
    expect(showsUnverified(upstream)).toBe(true);
    expect(needsVerification({ ...standardDrawing, unconfirmed_assumptions: [{ id: 'A-01', subject: 'R1.size' }] })).toBe(true);
    expect(needsVerification({ ...standardDrawing, pending_checks: ['model.gate'] })).toBe(true);
    // a manual revision without a requirement list: unverified (no check), but nothing a person can act on
    expect(needsVerification({ execution_policy: 'standard', check_status: 'not_requested', unverified: true })).toBe(false);
    expect(showsUnverified({ execution_policy: 'turbo', check_status: 'passed', unverified: false })).toBe(false);
    expect(needsVerification(null)).toBe(false);
  });
});

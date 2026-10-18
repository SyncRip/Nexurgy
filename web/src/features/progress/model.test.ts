import { describe, expect, it } from 'vitest';
import { calls, run, stage, unit } from './test-fixtures';
import {
  compactTokens,
  elapsedMs,
  isTerminalProgress,
  openByDefault,
  previewUnits,
  progressDisclosure,
  reconcileDisclosure,
  stageFailures,
  stageOpenByDefault,
  stageTone,
  summarize,
  unitIndex,
  unitTone,
  valueText,
  visibleUnits,
} from './model';

describe('progress model', () => {
  it.each(['standard', 'turbo'])('counts explicit decisions, excluding legacy model notes regardless of %s policy or old totals', (policy) => {
    const p = run('succeeded', [stage('model', 'succeeded', [unit('model', 'built', {
      decisions: [{ kind: 'assumption', text: 'The shaft axis is Z' }, { kind: 'assumption', basis: 'The base is at Z=0' }],
    })])], { assumptions: 2 });
    p.policy = policy;
    expect(summarize(p).assumptions).toBe(0);
    p.stages[0].units[0].decisions.push(
      { kind: 'assumption', text: 'Origin datum', adopted: 0 },
      { kind: 'assumption', text: 'Rotation disabled', adopted: false },
    );
    p.stages[0].decisions.push(
      { kind: 'assumption', text: 'Missing stock size', trigger: 'missing' },
      { kind: 'assumption', text: 'A person corrected the datum', confirmation: { decision: 'corrected' } },
      { kind: 'assumption', text: 'Recorded incomplete decision', problems: ['no_subject'] },
    );
    for (const total of [7, 5]) {
      p.totals.assumptions = total;
      expect(summarize(p).assumptions).toBe(5);
    }
  });

  it('collapses at success, reopens at failure, and preserves manual closure between ordinary snapshots', () => {
    const initial = progressDisclosure(null, 'r', run('running', [stage('assembly', 'running')]), true);
    expect(initial.open).toBe(true);
    const closed = { ...initial, open: false };
    expect(progressDisclosure(closed, 'r', run('running', [stage('assembly', 'running')]), true)).toBe(closed);
    expect(progressDisclosure(initial, 'r', run('succeeded', [stage('assembly', 'succeeded')]), false).open).toBe(false);
    expect(progressDisclosure(closed, 'r', run('partial', [stage('assembly', 'failed')]), false).open).toBe(true);
    expect(progressDisclosure(closed, 'new-run', null, true).open).toBe(true);
  });

  it('reveals a newly failed unit even when its stage status stays running and it lies beyond the preview', () => {
    const s = stage('assembly', 'running', Array.from({ length: 15 }, (_, i) => unit(`definition:D-${i}`, i === 14 ? 'failed' : 'built')));
    const before = { identity: 'assembly', status: 'running', failures: [], open: false };
    expect(reconcileDisclosure(before, { ...before, failures: stageFailures(s) }, true).open).toBe(true);
    const shown = previewUnits(visibleUnits(s), 12);
    expect(shown.map((u) => u.key)).toContain('definition:D-14');
    expect(shown).toHaveLength(13);
  });

  it('retains unknown usage and keeps a built single-part model visible without calls', () => {
    expect(summarize(run('succeeded', [], { tokens_in: null, tokens_out: 200 })).tokens).toBeNull();
    expect(summarize(run('succeeded', [], { tokens_in: 200, tokens_out: null })).tokens).toBeNull();
    expect(summarize(run('succeeded', [], { tokens_in: 0, tokens_out: 0 })).tokens).toBe(0);
    expect(visibleUnits(stage('model', 'succeeded', [unit('model', 'built')]))).toHaveLength(1);
  });

  it('opens a running or failed run and reads a successful one as one line', () => {
    expect(openByDefault(run('running', [stage('spec', 'succeeded'), stage('assembly', 'running')]))).toBe(true);
    expect(openByDefault(run('succeeded', [stage('spec', 'succeeded'), stage('assembly', 'succeeded')]))).toBe(false);
    expect(openByDefault(run('partial', [stage('assembly', 'failed')]))).toBe(true);
    // a successful run whose part still failed is shown open at that part
    expect(openByDefault(run('succeeded', [stage('assembly', 'succeeded', [unit('definition:D-01', 'blocked')])]))).toBe(true);
  });

  it('opens the stage that runs or failed, not the finished ones', () => {
    expect(stageOpenByDefault(stage('assembly', 'running'))).toBe(true);
    expect(stageOpenByDefault(stage('assembly', 'failed'))).toBe(true);
    expect(stageOpenByDefault(stage('spec', 'succeeded'))).toBe(false);
    expect(stageOpenByDefault(stage('assembly', 'succeeded', [unit('definition:D-02', 'failed')]))).toBe(true);
  });

  it('pairs every status with a tone', () => {
    expect([stageTone('succeeded'), stageTone('failed'), stageTone('running'), stageTone('waiting_human'), stageTone('pending')]).toEqual([
      'ok', 'err', 'accent', 'warn', 'pending',
    ]);
    expect(['built', 'done', 'failed', 'blocked', 'needs_clarification', 'pending', 'not_started'].map(unitTone)).toEqual([
      'ok', 'ok', 'err', 'warn', 'warn', 'accent', 'pending',
    ]);
  });

  it('lists part definitions always and other units only when they called a model', () => {
    const s = stage('spec', 'succeeded', [
      unit('definition:D-01', 'pending'),
      unit('spec_chunk:1', 'done', { calls: calls(1) }),
      unit('skeleton:1', 'done', { calls: calls(0) }),
    ]);
    expect(visibleUnits(s).map((u) => u.key)).toEqual(['definition:D-01', 'spec_chunk:1']);
  });

  it('summarizes parts, calls, tokens, assumptions and skipped checks', () => {
    const p = run(
      'partial',
      [stage('assembly', 'failed', [unit('definition:D-01', 'built', {
        decisions: [{ kind: 'check_skipped', checks: ['model.gate'] }, { kind: 'assumption', subject: 'part.width', adopted: 20 }],
      })], { decisions: [{ kind: 'assumption', subject: 'placement', trigger: 'missing' }] })],
      { calls: 9, failed_calls: 3, tokens_in: 70_000, tokens_out: 41_500, assumptions: 2, units: 4, units_built: 4 },
    );
    expect(summarize(p)).toEqual({ parts: 4, partsBuilt: 4, calls: 9, failedCalls: 3, tokens: 111_500, assumptions: 2, checksSkipped: true });
  });

  it('knows when a run ended and how long things took', () => {
    expect(isTerminalProgress(run('running', []))).toBe(false);
    expect(['succeeded', 'failed', 'partial', 'cancelled'].every((s) => isTerminalProgress(run(s, [])))).toBe(true);
    expect(isTerminalProgress(null)).toBe(false);
    expect(elapsedMs({ started_at: '2026-10-17T13:01:00Z', finished_at: '2026-10-17T13:02:30Z' }, 0)).toBe(90_000);
    expect(elapsedMs({ started_at: '2026-10-17T13:01:00Z', finished_at: null }, Date.parse('2026-10-17T13:01:10Z'))).toBe(10_000);
    expect(elapsedMs({ started_at: null, finished_at: null }, 0)).toBeNull();
  });

  it('writes adopted values the way people read them', () => {
    expect(valueText({ rotate: [0, 0, 0], translate: [4, 324.00004, 0] })).toBe('rotate: (0, 0, 0), translate: (4, 324, 0)');
    expect([valueText(20), valueText(true), valueText(null), valueText('mm')]).toEqual(['20', 'true', '', 'mm']);
  });

  it('formats token counts and unit indexes', () => {
    expect([compactTokens(812), compactTokens(8197), compactTokens(14_000), compactTokens(111_500)]).toEqual(['812', '8.2k', '14k', '112k']);
    expect([unitIndex('spec_chunk:2'), unitIndex('definition:D-03'), unitIndex('spec_resolve')]).toEqual(['2', 'D-03', '']);
  });
});

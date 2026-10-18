import { describe, expect, it } from 'vitest';
import type { AssemblyCoverage, CoverageMissing } from '../../api/orchestration';
import {
  aiDecisionCount,
  canContinueBuild,
  coverageCounts,
  coverageMarks,
  coverageOf,
  decisionUses,
  groupMissing,
  subassemblyPlacement,
} from './coverageModel';

const item = (stage: string, key: string, kind = 'part'): CoverageMissing => ({
  stage,
  key,
  name: key,
  kind,
  code: 'x',
  reason: 'r',
  quantity: 1,
  instances: [],
  recovery: 'retry_definition',
});

// the acceptance example of the brief: 43 definitions / 43 instances, 36 built and installed, 7 failed
const COV: AssemblyCoverage = {
  status: 'partial',
  target: { basis: 'spec', definitions: 43, instances: 43, subassemblies: 0, subassembly_instances: 0, recognized_definitions: 43 },
  built: { definitions: 36 },
  placed: { definitions: 36, instances: 36 },
  arranged: { definitions: 0, instances: 0 },
  missing: [...Array.from({ length: 7 }, (_, i) => item('modeling', `D-${i}`)), item('assembly', 'M-01', 'relation')],
  missing_counts: { modeling: 7, assembly: 1 },
};

describe('coverage helpers', () => {
  it('counts the target, the actual and the failed objects without shrinking the target', () => {
    expect(coverageCounts(COV)).toEqual({
      targetDefinitions: 43,
      targetInstances: 43,
      subassemblies: 0,
      recognized: 43,
      built: 36,
      placed: 36,
      localOnly: 0,
      decided: 0,
      estimated: 0,
      arranged: 0,
      failed: 7, // the dropped relation is listed, not counted as a failed object
    });
    // supplement F: installed by a Turbo AI placement decision (a subset of placed; absent key = none)
    expect(coverageCounts({ ...COV, decided: { definitions: 4, instances: 5 } }).decided).toBe(5);
    // round 2 (P): of those, at an AI-estimated position (installed, shown apart)
    expect(coverageCounts({ ...COV, estimated: { definitions: 2, instances: 3 } }).estimated).toBe(3);
  });

  it('groups the missing items by stage in work order', () => {
    const c = { ...COV, missing: [item('placement', 'I-3'), item('structure', 'D-9'), item('modeling', 'D-1'), item('weird', 'Z')] };
    expect(groupMissing(c).map((g) => [g.stage, g.items.map((m) => m.key)])).toEqual([
      ['structure', ['D-9']],
      ['modeling', ['D-1']],
      ['placement', ['I-3']],
      ['other', ['Z']],
    ]);
    expect(groupMissing(null)).toEqual([]);
  });

  it('keeps partial, no-assembly and AI decisions apart, and continues only a retryable rebuild', () => {
    expect(coverageMarks(COV, 3)).toEqual({ partial: true, noAssembly: false, parts: 'available', aiDecisions: 3 });
    expect(coverageMarks({ ...COV, status: 'none' }, null)).toEqual({ partial: false, noAssembly: true, parts: 'available', aiDecisions: 0 });
    expect(coverageMarks({ ...COV, status: 'complete' })).toEqual({ partial: false, noAssembly: false, parts: 'available', aiDecisions: 0 });
    expect(canContinueBuild({ code: 'assembly_partial', recovery: 'retry_definition', retryable: true })).toBe(true);
    expect(canContinueBuild({ code: 'assembly_partial', recovery: 'fix_spec', retryable: false })).toBe(false);
    expect(canContinueBuild({ code: 'assembly_partial', recovery: 'run_standard', retryable: false })).toBe(false);
    expect(canContinueBuild(null)).toBe(false);
  });

  it('distinguishes zero valid parts from an unknown legacy count', () => {
    expect(coverageMarks({ ...COV, status: 'none', built: { definitions: 0 } })).toMatchObject({ noAssembly: true, parts: 'none' });
    const legacy = coverageOf({ coverage: { ...COV, status: 'none', built: undefined } });
    expect(coverageMarks(legacy)).toMatchObject({ noAssembly: true, parts: 'unknown' });
    expect(coverageMarks(undefined)).toMatchObject({ noAssembly: false, parts: 'unknown' });
  });

  it('reads the coverage a revision stores and counts AI decisions, not person answers', () => {
    expect(coverageOf({ coverage: COV, sha256: 'x' })).toBe(COV);
    expect(coverageOf({ coverage: { status: 'partial' } })).toBeNull();
    expect(coverageOf(null)).toBeNull();
    expect(aiDecisionCount([{ author: 'ai_turbo' }, { author: 'rule' }, { author: 'person' }, {}])).toBe(3);
  });

  it('reads the parent chain (local only, sub-assemblies placed up to the root) and where a decision was used', () => {
    const c = { ...COV, local_only: { definitions: 2, instances: 4 }, subassembly_placement: { placed: 1, total: 3, unplaced: ['I-10', 'I-30'] } };
    expect(coverageCounts(c).localOnly).toBe(4);
    expect(subassemblyPlacement(c)).toEqual({ placed: 1, total: 3, unplaced: ['I-10', 'I-30'] });
    expect(subassemblyPlacement(COV)).toBeNull(); // a block from before
    const asm = { type: 'assembly_revision', id: 'a', label: 'D07.00 A2' };
    expect(
      decisionUses([
        { stage: 'export', object: { type: 'file', path: 'parts/P-1_R1.step' }, ref: 'D-04' },
        { stage: 'placement', object: asm, ref: ['I-04'] },
        { stage: 'modeling', object: { type: 'model_revision', id: 'm', label: 'P-1 R1' }, ref: 'D-04' },
        { stage: 'placement', object: asm, ref: ['I-04', 'I-05'] },
      ]),
    ).toEqual([
      { stage: 'modeling', objects: ['P-1 R1'], refs: ['D-04'] },
      { stage: 'placement', objects: ['D07.00 A2'], refs: ['I-04', 'I-05'] },
      { stage: 'export', objects: ['parts/P-1_R1.step'], refs: ['D-04'] },
    ]);
    expect(decisionUses(undefined)).toEqual([]);
  });
});

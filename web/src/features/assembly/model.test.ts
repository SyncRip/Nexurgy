import { describe, expect, it } from 'vitest';
import type { RevisionBrief, SceneNode } from '../../api/assemblies';
import type { AssemblyCoverage } from '../../api/orchestration';
import { assemblyListStatus, checkRows, decompose, findNode, flattenTree, pathKey, placementMatrix, safeCheckStatus, underPath } from './model';

const node = (path: string[], children: SceneNode[] = []): SceneNode =>
  ({ instance_id: path[path.length - 1], name: path.join('>'), path, children, world: placementMatrix([0, 0, 0], [0, 0, 0]) }) as unknown as SceneNode;

describe('assembly model helpers', () => {
  it('never calls a partial, missing or failed assembly completed, while preserving legacy list behavior', () => {
    const coverage: AssemblyCoverage = {
      status: 'partial',
      target: { basis: 'spec', definitions: 4, instances: 6, subassemblies: 1, subassembly_instances: 1, recognized_definitions: 4 },
      built: { definitions: 4 }, placed: { definitions: 3, instances: 4 },
      local_only: { definitions: 1, instances: 2 }, arranged: { definitions: 0, instances: 0 }, missing: [], missing_counts: {},
    };
    const revision: Pick<RevisionBrief, 'status' | 'coverage' | 'verification' | 'check_status'> = {
      status: 'ready', check_status: 'skipped_turbo', coverage,
      verification: { unverified: true, execution_policy: 'turbo', check_status: 'skipped_turbo', acceptance_status: 'not_requested', assumptions: [] },
    };
    expect(assemblyListStatus(revision)).toEqual({ coverage, failed: false, incompleteUnverified: true, completedUnverified: false });
    expect(assemblyListStatus({ ...revision, coverage: { ...coverage, status: 'none' } })).toMatchObject({ incompleteUnverified: true, completedUnverified: false });
    expect(assemblyListStatus({ ...revision, coverage: { ...coverage, status: 'complete' } })).toMatchObject({ incompleteUnverified: false, completedUnverified: true });
    expect(assemblyListStatus({ ...revision, coverage: undefined })).toMatchObject({ coverage: null, completedUnverified: true });
    expect(assemblyListStatus({ ...revision, check_status: 'failed' })).toMatchObject({ incompleteUnverified: false, completedUnverified: false });
    expect(assemblyListStatus({ ...revision, status: 'failed', coverage: undefined })).toMatchObject({ failed: true, completedUnverified: false });
    expect(assemblyListStatus({ ...revision, verification: { ...revision.verification, unverified: false } })).toMatchObject({ incompleteUnverified: false, completedUnverified: false });
  });

  it('decomposes what placementMatrix composes (X → Y → Z, then translate)', () => {
    const m = placementMatrix([40, 0, 0], [0, 0, 180]);
    expect(decompose(m)).toEqual({ translate: [40, 0, 0], rotate: [0, 0, 180] });
    const r = decompose(placementMatrix([1.5, -2, 3], [10, 20, 30]));
    expect(r.translate).toEqual([1.5, -2, 3]);
    expect(r.rotate.map((x) => Math.round(x * 1000) / 1000)).toEqual([10, 20, 30]);
    // gimbal lock keeps a valid read-out
    const g = decompose(placementMatrix([0, 0, 0], [0, 90, 45]));
    expect(g.rotate[1]).toBeCloseTo(90, 6);
  });

  it('flattens the tree respecting collapsed nodes and finds nodes by path key', () => {
    const tree = [node(['I-1']), node(['I-2'], [node(['I-2', 'I-1']), node(['I-2', 'I-2'])])];
    expect(flattenTree(tree, new Set()).map((r) => [r.key, r.depth])).toEqual([
      ['I-1', 0],
      ['I-2', 0],
      ['I-2/I-1', 1],
      ['I-2/I-2', 1],
    ]);
    expect(flattenTree(tree, new Set(['I-2'])).map((r) => r.key)).toEqual(['I-1', 'I-2']);
    expect(findNode(tree, 'I-2/I-2')?.instance_id).toBe('I-2');
    expect(underPath('I-2/I-1', 'I-2')).toBe(true);
    expect(underPath('I-20', 'I-2')).toBe(false);
    expect(pathKey(['a', 'b'])).toBe('a/b');
  });

  it('lists the required checks even before they ran and never turns unknown states into success', () => {
    const rows = checkRows({ checks: { 'assembly.bom': { check: 'assembly.bom', status: 'passed' } } });
    expect(rows.map((r) => [r.name, r.record?.status ?? null])).toEqual([
      ['assembly.interference', null],
      ['assembly.mate_residual', null],
      ['assembly.bom', 'passed'],
    ]);
    expect(safeCheckStatus('weird')).toBe('unknown');
    expect(safeCheckStatus('skipped_turbo')).toBe('skipped_turbo');
  });
});

import { describe, expect, it } from 'vitest';
import type { DeliveryLevels, LevelFile, LevelObject, SubassemblyPackage } from '../../api/orchestration';
import { levelGaps, levelLines, objectLabel, protocolCell, representationCell, subPackageState } from './levelsModel';

const ok: LevelFile = { status: 'generated' };
const obj = (number: string, rev: string, step = ok, dwg = ok): LevelObject => ({ key: number, number, name: number, rev, rev_no: 1, step, dwg });
const z = { generated: 0, missing: 0, failed: 0, protocol_unmet: 0 };

describe('layered delivery helpers', () => {
  it('counts each level and lists the objects that lack a STEP or a DWG', () => {
    const l: DeliveryLevels = {
      assembly: [obj('LV', 'A1')],
      subassembly: [obj('LV-SA', 'A1'), obj('LV-SB', 'A1', ok, { status: 'failed', code: 'drawing_budget_exceeded' })],
      part: [obj('W8', 'R1'), { ...obj('SHIM', ''), rev: null, rev_no: null, step: { status: 'missing' }, dwg: { status: 'missing' }, coverage: true }],
      counts: { assembly: { objects: 1, step: z, dwg: z }, subassembly: { objects: 2, step: z, dwg: z }, part: { objects: 2, step: z, dwg: z } },
    };
    expect(levelLines(l)).toEqual([
      { level: 'assembly', objects: 1, step: 1, dwg: 1, complete: true },
      { level: 'subassembly', objects: 2, step: 2, dwg: 1, complete: false },
      { level: 'part', objects: 2, step: 1, dwg: 1, complete: false },
    ]);
    expect(levelGaps(l).map((o) => [o.level, objectLabel(o)])).toEqual([
      ['subassembly', 'LV-SB A1'],
      ['part', 'SHIM'],
    ]);
    const sub = (package_id: string | null, completeness: string): SubassemblyPackage => ({
      assembly_id: 'a', assembly_revision_id: 'r', number: 'LV-SC', name: 'SUB-C', rev_no: 2, depth: 2, instances: 1, package_id, completeness,
    });
    expect([subPackageState(sub('p', 'complete')), subPackageState(sub('p', 'partial')), subPackageState(sub(null, 'failed'))]).toEqual([
      'complete', 'partial', 'failed',
    ]);
    expect(objectLabel(sub(null, 'failed'))).toBe('LV-SC A2');
  });

  it('reads the STEP protocol against the required one and the representation of the build record', () => {
    expect(protocolCell({ status: 'generated', protocol: 'AP203', reexported_from: 'AP214' })).toEqual({
      protocol: 'AP203', met: true, alternative: null, reexportedFrom: 'AP214',
    });
    const unmet: LevelFile = { status: 'protocol_unmet', protocol: 'AP214', alternative: { path: 'parts/P_R1.AP214.step', protocol: 'AP214' } };
    expect(protocolCell(unmet)).toEqual({ protocol: 'AP214', met: false, alternative: 'AP214', reexportedFrom: null });
    expect(protocolCell({ status: 'generated', protocol: 'AP203' }, 'AP214').met).toBe(false);
    const plate: LevelObject = {
      ...obj('NP', 'R1'), representation: 'exact',
      build_record: { representation: 'simplified', created: [], usable: true, complete: false,
                      not_created: [{ requirement: 'text "严禁带电打开"', reason: 'text_not_built' }] },
    };
    expect(representationCell(plate)).toEqual({ level: 'simplified', notCreated: ['text "严禁带电打开"'], complete: false });
    expect(representationCell(obj('W8', 'R1'))).toEqual({ level: null, notCreated: [], complete: null });
  });
});

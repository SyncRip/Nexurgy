import { describe, expect, it } from 'vitest';
import type { AiCallRecord, ClarificationGap, Drawing, DrawingPage, Requirement } from '../../api/types';
import {
  acadRelease,
  annotationState,
  callTokens,
  cadDrawingFormat,
  cadErrorFamily,
  CAD_ERROR_CODES,
  contourText,
  coverageOf,
  drawingPages,
  dwgInfo,
  evidenceParts,
  extraPageTitle,
  formatLabel,
  gapKind,
  geomCheckState,
  geomCheckTone,
  isCadErrorCode,
  isLegacyDrawing,
  itemsByRequirement,
  ledgerTotals,
  packageFileName,
  pageFile,
  pageFileName,
  pageFiles,
  parseOffset,
  profileFieldProblem,
  requirementBasis,
  requirementFacts,
  sectionMode,
  selectPage,
  stateTone,
  summaryCounts,
} from './status';
import { en } from '../../i18n/en';

const file = (format: string, sha: string, version?: string) => ({ format, sha256: sha, bytes: 10, path: `pages/p_${sha}.${format}`, ...(version ? { version } : {}) });

const pkgPages: DrawingPage[] = [
  { index: 2, role: 'section', title: 'Section A-A', files: [file('dwg', 'd2', 'AC1015'), file('svg', 's2'), file('dxf', 'x2', 'AC1015')] },
  { index: 1, role: 'overview', title: 'Overview', files: [file('dxf', 'x1', 'AC1015'), file('svg', 's1')] },
];

function drawing(over: Partial<Drawing> = {}): Drawing {
  return {
    id: 'd-1234567890',
    part_id: 'p1',
    model_revision_id: 'r1',
    rev_no: 3,
    kind: 'generated',
    file_version_id: null,
    svg_sha: 's1',
    dxf_sha: 'x1',
    config: {},
    views: [],
    node_run_id: null,
    created_at: null,
    ...over,
  };
}

describe('geometry check state', () => {
  it('keeps passed / failed, maps other values to not_evaluated and null to none', () => {
    expect(geomCheckState('passed')).toBe('passed');
    expect(geomCheckState('failed')).toBe('failed');
    expect(geomCheckState('not_evaluated')).toBe('not_evaluated');
    expect(geomCheckState('something_new')).toBe('not_evaluated');
    expect(geomCheckState(null)).toBe('none');
    expect(geomCheckState(undefined)).toBe('none');
  });
  it('never uses the ok tone for anything but passed', () => {
    expect(geomCheckTone('passed')).toBe('ok');
    expect(geomCheckTone('failed')).toBe('err');
    expect(geomCheckTone('not_evaluated')).toBe('pending');
    expect(geomCheckTone('none')).toBe('pending');
  });
  it('counts summary items with missing statuses as 0', () => {
    expect(summaryCounts({ items: { passed: 3, failed: 1, not_evaluated: 2 } })).toEqual({ passed: 3, failed: 1, other: 2, total: 6 });
    expect(summaryCounts(null)).toEqual({ passed: 0, failed: 0, other: 0, total: 0 });
  });
  it('groups check items by requirement id', () => {
    const m = itemsByRequirement([
      { requirement_id: 'R1', kind: 'envelope', status: 'passed', message: '' },
      { requirement_id: 'R2', kind: 'hole', status: 'failed', message: '', instance: 0 },
      { requirement_id: 'R2', kind: 'hole', status: 'passed', message: '', instance: 1 },
      { requirement_id: null, kind: 'solid', status: 'failed', message: 'two solids' },
    ]);
    expect(m.get('R2')?.length).toBe(2);
    expect(m.get('')?.[0].message).toBe('two solids');
  });
});

describe('requirements', () => {
  const hole: Requirement = {
    id: 'R2',
    kind: 'hole',
    label: '4×Ø9 blind 8',
    diameter: 9,
    entries: [
      [10, 10, 15],
      [50, 10, 15],
      [10, 40, 15],
      [50, 40, 15],
    ],
    through: false,
    depth: 8,
    evidence: [
      { source: 'request', ref: '4×Ø9 盲孔深 8' },
      { source: 'drawing_dimension', ref: '1A3', value: 'Ø9' },
    ],
  };
  it('derives the basis: assumed wins over derived over stated', () => {
    expect(requirementBasis({})).toBe('stated');
    expect(requirementBasis({ derived: ['depth'] })).toBe('derived');
    expect(requirementBasis({ derived: ['depth'], assumed: ['through'] })).toBe('assumed');
  });
  it('lists the key values of a blind hole pattern', () => {
    const f = requirementFacts(hole);
    expect(f.map((x) => x.key)).toEqual(['diameter', 'count', 'through', 'depth']);
    expect(f[0].value).toBe('Ø9');
    expect(f[1].value).toBe('4');
    expect(f[2].bool).toBe(false);
    expect(f[3].value).toBe('8');
  });
  it('does not show a depth for a through hole and keeps an unknown size as "?"', () => {
    expect(requirementFacts({ ...hole, through: true, depth: null }).some((x) => x.key === 'depth')).toBe(false);
    const env = requirementFacts({ id: 'R1', kind: 'envelope', label: 'plate', size: [120, 80, null] });
    expect(env[0].value).toBe('120 × 80 × ?');
  });
  it('describes contours without language', () => {
    expect(contourText({ kind: 'rect', center: [0, 0], size: [40, 20], corner_radius: 2 })).toBe('▭ 40 × 20 @ (0, 0) R2');
    expect(contourText({ kind: 'circle', center: [10, 5], diameter: 20 })).toBe('Ø20 @ (10, 5)');
    expect(contourText({ kind: 'polygon', points: [[0, 0], [1, 0], [0, 1]] })).toBe('⬠ 3 pts');
  });
  it('splits literal quotes from drawing / geometry ids', () => {
    const p = evidenceParts(hole.evidence);
    expect(p.quotes.map((e) => e.ref)).toEqual(['4×Ø9 盲孔深 8']);
    expect(p.ids.map((e) => e.ref)).toEqual(['1A3']);
  });
});

describe('drawing package pages and formats', () => {
  it('orders package pages by index and selects the requested one, else the first', () => {
    const pages = drawingPages(drawing({ pages: pkgPages }));
    expect(pages.map((p) => p.index)).toEqual([1, 2]);
    expect(selectPage(pages, 2)?.role).toBe('section');
    expect(selectPage(pages, 9)?.index).toBe(1);
    expect(selectPage(pages, null)?.index).toBe(1);
    expect(selectPage([], 1)).toBeNull();
  });
  it('renders a legacy drawing as one overview page from the page-1 aliases', () => {
    const d = drawing({ pages: [] });
    expect(isLegacyDrawing(d)).toBe(true);
    const pages = drawingPages(d);
    expect(pages).toHaveLength(1);
    expect(pages[0].files.map((f) => f.format)).toEqual(['svg', 'dxf']);
    expect(drawingPages(drawing({ pages: [], svg_sha: null, dxf_sha: null }))).toEqual([]);
  });
  it('lists every format of a page as SVG, DXF, DWG with the release name', () => {
    const files = pageFiles(pkgPages[0]);
    expect(files.map((f) => f.format)).toEqual(['svg', 'dxf', 'dwg']);
    expect(files.map(formatLabel)).toEqual(['SVG', 'DXF R2000', 'DWG R2000']);
    expect(pageFile(pkgPages[1], 'dwg')).toBeNull();
    expect(acadRelease('AC1032')).toBe('R2018');
    expect(acadRelease('AC9999')).toBe('AC9999');
    expect(acadRelease(null)).toBeNull();
  });
  it('shows a page title only when it says more than the role', () => {
    expect(extraPageTitle({ title: 'OVERVIEW', role: 'overview' }, 'Overview')).toBeNull();
    expect(extraPageTitle({ title: '总图', role: 'overview' }, '总图')).toBeNull();
    expect(extraPageTitle({ title: 'SECTION A-A', role: 'section' }, 'Section')).toBe('SECTION A-A');
    expect(extraPageTitle({ title: '', role: 'section' }, 'Section')).toBeNull();
  });
  it('names downloads after part, revision, page and role', () => {
    expect(pageFileName('PL-100', 3, pkgPages[0], { format: 'dwg' })).toBe('PL-100_R3_p2_section.dwg');
    expect(pageFileName('PL 100/x', null, { index: 1, role: 'overview' }, { format: 'SVG' })).toBe('PL-100-x_p1_overview.svg');
    expect(packageFileName('PL-100', 3, 'd-1234567890')).toBe('PL-100_R3_d-123456_package.zip');
  });
});

describe('annotation and DWG status', () => {
  const checks = (dwg: NonNullable<Drawing['checks']>['dwg'], status: 'passed' | 'failed' | null = 'passed') => ({ annotation: { status, failed: 0, warnings: 0 }, dwg });
  it('reads the annotation verdict, legacy drawings have none', () => {
    expect(annotationState(drawing({ pages: pkgPages, manifest: { schema: 'x' }, checks: checks({ status: 'passed' }) }))).toBe('passed');
    expect(annotationState(drawing({ pages: [] }))).toBe('legacy');
    expect(annotationState(null, { annotation_status: 'failed' })).toBe('failed');
    expect(annotationState(null, { annotation_status: null })).toBe('unknown');
  });
  it('reports a generated DWG with its release', () => {
    const i = dwgInfo(drawing({ pages: pkgPages, manifest: { schema: 'x' }, checks: checks({ status: 'passed', target: 'AC1015' }) }));
    expect(i.state).toBe('passed');
    expect(i.release).toBe('R2000');
    expect(stateTone(i.state)).toBe('ok');
  });
  it('keeps the reason of an unavailable DWG and the code / page of a failed one', () => {
    const u = dwgInfo(drawing({ pages: pkgPages, manifest: { schema: 'x' }, checks: checks({ status: 'unavailable', reason: 'LibreDWG not installed' }) }));
    expect(u).toMatchObject({ state: 'unavailable', reason: 'LibreDWG not installed', release: null });
    expect(stateTone(u.state)).toBe('warn');
    const f = dwgInfo(drawing({ pages: pkgPages, manifest: { schema: 'x' }, checks: checks({ status: 'failed', code: 'dwg_readback_mismatch', message: 'text lost', page: 2 }) }));
    expect(f).toMatchObject({ state: 'failed', reason: 'text lost', code: 'dwg_readback_mismatch', page: 2 });
    expect(stateTone(f.state)).toBe('err');
  });
  it('falls back to the brief and never invents a status', () => {
    expect(dwgInfo(null, { dwg_status: 'unavailable' }).state).toBe('unavailable');
    expect(dwgInfo(null, { dwg_status: null }).state).toBe('unknown');
    expect(dwgInfo(drawing({ pages: [] })).state).toBe('legacy');
  });
  it('always returns the three coverage lists', () => {
    expect(coverageOf(null)).toEqual({ covered: [], uncovered: [], unknown: [] });
    const c = coverageOf(drawing({ coverage: { covered: [{ requirement_id: 'R5', category: 'material', text: 'Q235' }], uncovered: [], unknown: [] } }));
    expect(c.covered[0].text).toBe('Q235');
    const m = coverageOf(drawing({ coverage: null, manifest: { schema: 'x', coverage: { covered: [], uncovered: [{ requirement_id: 'R6', category: 'thread', text: 'M8' }], unknown: [] } } }));
    expect(m.uncovered[0].text).toBe('M8');
  });
});

describe('file and error helpers', () => {
  it('recognises DXF / DWG by name (DWG uploads keep kind "other") or by the drawing_dxf kind', () => {
    expect(cadDrawingFormat('BHD1.DWG', 'other')).toBe('dwg');
    expect(cadDrawingFormat('plate.dxf', 'other')).toBe('dxf');
    expect(cadDrawingFormat('noext', 'drawing_dxf')).toBe('dxf');
    expect(cadDrawingFormat('part.step', 'model_step')).toBeNull();
    expect(cadDrawingFormat(null)).toBeNull();
  });
  it('maps CAD error codes to their detail family', () => {
    expect(cadErrorFamily('requirements_not_met')).toBe('requirements');
    expect(cadErrorFamily('unsupported')).toBe('unsupported');
    expect(cadErrorFamily('simulation_unsupported')).toBe('simulation');
    expect(cadErrorFamily('drawing_check_failed')).toBe('annotation');
    expect(cadErrorFamily('step_assembly')).toBe('step');
    expect(cadErrorFamily('profile_open_contour')).toBe('profile');
    expect(cadErrorFamily('drawing_needs_reconstruct')).toBe('profile');
    expect(cadErrorFamily('dwg_timeout')).toBe('dwg');
    expect(cadErrorFamily('drawing_format_mismatch')).toBe('drawing_input');
    expect(cadErrorFamily('package_file_missing')).toBe('package');
    expect(cadErrorFamily('part_busy')).toBe('other');
    expect(cadErrorFamily(null)).toBe('other');
  });
  it('has a localized message for every CAD error code', () => {
    for (const c of CAD_ERROR_CODES) expect(Object.prototype.hasOwnProperty.call(en, `error.${c}`), c).toBe(true);
    expect(isCadErrorCode('step_multibody')).toBe(true);
    expect(isCadErrorCode('part_busy')).toBe(false);
  });
  it('classifies clarification gaps', () => {
    const g = (x: Partial<ClarificationGap>): ClarificationGap => ({ requirement_id: null, kind: 'hole', label: 'R2', field: 'depth', reason: 'missing', ...x });
    expect(gapKind(g({ kind: 'drawing', field: 'sheet', reason: 'ambiguous' }))).toBe('sheet');
    expect(gapKind(g({ kind: 'drawing', field: 'units' }))).toBe('units');
    expect(gapKind(g({ field: 'evidence', reason: 'conflict' }))).toBe('conflict');
    expect(gapKind(g({ reason: 'assumed', field: 'through' }))).toBe('field');
  });
});

describe('form values', () => {
  it('reads the section mode of stored params', () => {
    expect(sectionMode('auto')).toBe('auto');
    expect(sectionMode(undefined)).toBe('auto');
    expect(sectionMode('none')).toBe('none');
    expect(sectionMode({ plane: 'YZ', offset: 0 })).toBe('plane');
    expect(sectionMode({ plane: 'XY', offset: 0 })).toBe('auto');
  });
  it('parses offsets without turning empty text into 0', () => {
    expect(parseOffset('')).toBeNull();
    expect(parseOffset('-')).toBeNull();
    expect(parseOffset('abc')).toBeNull();
    expect(parseOffset('0')).toBe(0);
    expect(parseOffset('-12,5')).toBe(-12.5);
  });
  it('requires thickness and scale (no defaults) and keeps 0 valid where allowed', () => {
    expect(profileFieldProblem(null, { positive: true })).toBe('required');
    expect(profileFieldProblem(0, { positive: true })).toBe('positive');
    expect(profileFieldProblem(2, { positive: true })).toBeNull();
    expect(profileFieldProblem(0)).toBeNull();
    expect(profileFieldProblem(Number.NaN)).toBe('required');
  });
});

describe('AI-call ledger totals', () => {
  const call = (x: Partial<AiCallRecord>): Pick<AiCallRecord, 'status' | 'tokens_in' | 'tokens_out' | 'latency_ms' | 'simulated'> => ({ status: 'ok', tokens_in: 100, tokens_out: 20, latency_ms: 900, simulated: false, ...x });
  it('sums reported usage and counts failures', () => {
    const tot = ledgerTotals([call({}), call({ status: 'error', tokens_in: 50, tokens_out: 0 })]);
    expect(tot).toMatchObject({ calls: 2, failed: 1, tokensIn: 150, tokensOut: 20, incomplete: false, latencyMs: 1800 });
  });
  it('does not count the numbers of simulated calls as usage', () => {
    const tot = ledgerTotals([call({ simulated: true, tokens_in: 4820, tokens_out: 45 }), call({})]);
    expect(tot.tokensIn).toBe(100);
    expect(tot.incomplete).toBe(true);
    expect(callTokens({ simulated: true, tokens_in: 1, tokens_out: 2 })).toEqual({ tokensIn: null, tokensOut: null });
  });
  it('keeps unknown usage unknown (null), never 0', () => {
    const tot = ledgerTotals([call({ tokens_in: null, tokens_out: null, latency_ms: null, simulated: true })]);
    expect(tot.tokensIn).toBeNull();
    expect(tot.tokensOut).toBeNull();
    expect(tot.latencyMs).toBeNull();
    expect(tot.incomplete).toBe(true);
    expect(tot.simulated).toBe(true);
  });
});

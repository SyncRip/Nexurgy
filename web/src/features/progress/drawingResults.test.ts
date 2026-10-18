import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { AssemblyDrawingPackage } from '../../api/assemblyDrawings';
import type { ProgressDrawing } from '../../api/progress';
import type { Drawing } from '../../api/types';
import { drawingResultIdentity, loadDrawingResults } from './drawingResults';

const mocks = vi.hoisted(() => ({ assembly: vi.fn(), drawing: vi.fn() }));
vi.mock('../../api/assemblyDrawings', () => ({ getAssemblyDrawing: mocks.assembly }));
vi.mock('../../api/endpoints', () => ({ getDrawing: mocks.drawing }));
const signal = () => new AbortController().signal;
const assembly: ProgressDrawing = { kind: 'assembly_drawing', id: 'pkg', node_id: 'draw', assembly_revision_id: 'a1', manifest_sha256: 'manifest', completeness: 'partial' };
const drawing: ProgressDrawing = { kind: 'drawing', id: 'drawing', node_id: 'draw', model_revision_id: 'r1', manifest_sha256: null };
const pkg = () => ({
  id: 'pkg', assembly_id: 'assembly', assembly_revision_id: 'a1', manifest_sha256: 'manifest', label: 'D1', rev_label: 'A1',
  completeness: 'partial', check_status: 'failed', verification: { unverified: true },
  pages: [{ key: 'GA-1', index: 1, title: 'Cart arrangement', status: 'partial',
    files: [{ format: 'svg', sha256: 'svg' }], dwg: { status: 'failed' } },
  { key: 'D-1', index: 2, title: 'Detail', status: 'failed', files: [] }],
}) as unknown as AssemblyDrawingPackage;

beforeEach(() => vi.resetAllMocks());

describe('saved drawing result metadata', () => {
  it('preserves the exact assembly package, page identity, incomplete pages and failed DWG state', async () => {
    mocks.assembly.mockResolvedValue(pkg());
    const result = await loadDrawingResults(assembly, signal());
    expect(result.pages).toHaveLength(2);
    expect(result.pages[0]).toMatchObject({ key: 'GA-1', status: 'partial', preview: { kind: 'assembly_svg', drawingId: 'pkg', pageKey: 'GA-1', sha: 'svg' } });
    expect(result.pages[1].preview).toBeNull();
    expect(result.issues).toEqual(expect.arrayContaining(['partial', 'checksFailed', 'dwgFailed']));
    expect(result.issues).not.toContain('unverified');
    expect(result.sourcePath).toBe('/assemblies/assembly/structure?rev=a1');
    expect(mocks.drawing).not.toHaveBeenCalled();
  });

  it('uses legacy SVG aliases and keeps the exact drawing and source model versions in links', async () => {
    mocks.drawing.mockResolvedValue({ id: 'drawing', part_id: 'part', model_revision_id: 'r1', rev_no: 1,
      kind: 'generated', file_version_id: null, config: {}, views: [], node_run_id: null, created_at: null,
      pages: [], svg_sha: 'legacy-svg', dxf_sha: null } satisfies Drawing);
    const result = await loadDrawingResults(drawing, signal());
    expect(result.pages).toHaveLength(1);
    expect(result.pages[0].preview).toEqual({ kind: 'drawing_svg', sha: 'legacy-svg' });
    expect(result.pages[0].path).toBe('/parts/part/drawings?drawing=drawing&page=1');
    expect(result.sourcePath).toBe('/parts/part/model?rev=r1');
  });

  it('retains every ordinary drawing page, including equal images and missing previews, with package warnings', async () => {
    mocks.drawing.mockResolvedValue({ id: 'drawing', part_id: 'part', model_revision_id: 'r1', rev_no: 1,
      manifest_sha256: 'manifest', checks: { annotation: { status: 'failed' } }, dwg_status: 'failed',
      verification: { unverified: true },
      pages: [
        { index: 1, role: 'overview', title: 'Overview', files: [{ format: 'svg', sha256: 'saved-svg' }] },
        { index: 2, role: 'section', title: 'Section', files: [{ format: 'svg', sha256: 'saved-svg' }] },
        { index: 3, role: 'section', title: 'Unfinished section', files: [] },
      ],
    });
    const result = await loadDrawingResults({ ...drawing, manifest_sha256: 'manifest' }, signal());
    expect(result.pages.map((page) => page.key)).toEqual(['1', '2', '3']);
    expect(result.pages.map((page) => page.preview)).toEqual([
      { kind: 'drawing_svg', sha: 'saved-svg' }, { kind: 'drawing_svg', sha: 'saved-svg' }, null,
    ]);
    expect(result.pages[2]).toMatchObject({ status: 'missing', path: '/parts/part/drawings?drawing=drawing&page=3' });
    expect(result.issues).toEqual(['checksFailed', 'dwgFailed']);
    expect(result.sourcePath).toBe('/parts/part/model?rev=r1');
  });

  it.each([{ id: 'other' }, { assembly_revision_id: 'latest' }, { manifest_sha256: 'other' }])('rejects wrong assembly identity: %j', async (patch) => {
    mocks.assembly.mockResolvedValue({ ...pkg(), ...patch });
    await expect(loadDrawingResults(assembly, signal())).rejects.toThrow('identity');
  });

  it.each([{ id: 'other' }, { model_revision_id: 'latest' }])('rejects wrong part drawing identity: %j', async (patch) => {
    mocks.drawing.mockResolvedValue({ id: 'drawing', model_revision_id: 'r1', ...patch });
    await expect(loadDrawingResults(drawing, signal())).rejects.toThrow('identity');
  });

  it('gives immutable descriptor changes a new identity while polling copies keep the same identity', () => {
    expect(drawingResultIdentity({ ...assembly })).toBe(drawingResultIdentity(assembly));
    expect(drawingResultIdentity({ ...assembly, assembly_revision_id: 'a2' })).not.toBe(drawingResultIdentity(assembly));
    expect(drawingResultIdentity({ ...drawing, id: 'drawing2' })).not.toBe(drawingResultIdentity(drawing));
  });

  it('discards metadata that finishes after a different run or package has replaced it', async () => {
    const controller = new AbortController();
    let finish!: (saved: AssemblyDrawingPackage) => void;
    mocks.assembly.mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    const pending = loadDrawingResults(assembly, controller.signal);
    controller.abort();
    finish(pkg());
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' });
  });
});

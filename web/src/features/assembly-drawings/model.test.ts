import { describe, expect, it } from 'vitest';
import type { DrawingPage } from '../../api/assemblyDrawings';
import { buildConfig, DEFAULT_FORM, editablePages, formProblem, missingFormats, overlayEdits, safeCheck, safeCompleteness } from './model';

const page = (key: string, formats: string[]): DrawingPage =>
  ({ key, files: formats.map((format) => ({ format, path: `pages/${key}.${format}`, sha256: 'x', bytes: 1, content_type: '', version: null })) }) as unknown as DrawingPage;

describe('assembly drawing helpers', () => {
  it('never reads an unknown status as success', () => {
    expect(safeCheck('passed')).toBe('passed');
    expect(safeCheck('ok')).toBe('unknown');
    expect(safeCheck(undefined)).toBe('unknown');
    expect(safeCompleteness('complete')).toBe('complete');
    expect(safeCompleteness('done')).toBe('partial');
  });

  it('reports a failed DWG as missing instead of substituting', () => {
    expect(missingFormats(page('GA-1', ['dxf', 'svg']))).toEqual(['dwg']);
    expect(missingFormats(page('GA-1', ['dxf', 'svg', 'dwg']))).toEqual([]);
    expect(editablePages([page('GA-1', ['dxf', 'svg']), page('D1-1', ['dwg'])]).map((p) => p.key)).toEqual(['GA-1']);
  });

  it('builds the generate config from the dialog', () => {
    expect(buildConfig(DEFAULT_FORM)).toEqual({ views: ['front', 'top', 'right'], bom_view: 'flattened', section: 'auto' });
    const form = {
      ...DEFAULT_FORM,
      section: 'plane' as const,
      plane: 'YZ' as const,
      offset: '12.5',
      notes: ' a \n\n b',
      keyDims: [{ label: 'pitch', axis: 'x' as const, a: 'I-0002', aDatum: 'origin' as const, b: 'I-0003', bDatum: 'origin' as const }],
    };
    expect(formProblem(form)).toBeNull();
    const cfg = buildConfig(form);
    expect(cfg.section).toEqual({ plane: 'YZ', offset: 12.5 });
    expect(cfg.notes).toEqual(['a', 'b']);
    expect(cfg.key_dimensions).toEqual([{ id: 'K1', label: 'pitch', axis: 'x', a: { path: ['I-0002'], datum: 'origin' }, b: { path: ['I-0003'], datum: 'origin' } }]);
    expect(formProblem({ ...form, offset: 'x' })).toBe('offset');
    expect(formProblem({ ...form, views: [] })).toBe('views');
    expect(formProblem({ ...form, keyDims: [{ ...form.keyDims[0], b: '' }] })).toBe('keyDim');
  });

  it('turns the overlay editor into layout edits only', () => {
    const edits = overlayEdits(
      'GA-1',
      [
        { kind: 'balloon', ref: '1', dx: '0', dy: '5' },
        { kind: 'balloon', ref: '2', dx: '', dy: '' },
        { kind: 'dimension', ref: 'P1-D1', dx: '-3', dy: '0' },
      ],
      ['  note  ', ''],
    );
    expect(edits).toEqual([
      { op: 'move_balloon', page: 'GA-1', item: '1', by: [0, 5] },
      { op: 'move_dimension_text', page: 'GA-1', dimension: 'P1-D1', by: [-3, 0] },
      { op: 'add_note', page: 'GA-1', text: 'note' },
    ]);
  });
});

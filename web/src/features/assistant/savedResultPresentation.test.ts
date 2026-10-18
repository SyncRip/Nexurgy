import { describe, expect, it } from 'vitest';
import type { AiOperation, AiRef, AiResultItem, CadResult } from '../../api/assistant';
import type { ProgressDrawing, RunProgress } from '../../api/progress';
import type { DrawingBrief } from '../../api/types';
import { run, stage } from '../progress/test-fixtures';
import { matchesSavedResult, savedResultPresentation } from './savedResultPresentation';

const saved: CadResult = {
  event_id: 'event', object_type: 'part_model', object_id: 'part-1', version_id: 'r2', version_label: 'R2',
  base_version_id: 'r1', asset_version_id: 'av2', requirements: [], changes: [], checks: {}, artifacts: [],
  undo: { eligible: true, expected_head: 'r2' },
};
const item: AiResultItem = {
  ref: { key: 'part_revision:r2', kind: 'part_revision', project_id: 'project', object_id: 'r2', part_id: 'part-1',
    title: 'Wheel', source_path: '/parts/part-1/model?rev=r2' } as AiRef,
  asset_type: 'part_model', asset_version_id: 'av2', asset_id: 'asset-1', label: 'R2', base: null,
  run_id: 'run-1', node_id: 'model', availability: 'ready', publish_state: 'draft', actions: ['view', 'continue'],
};
const refItem = (ref: Partial<AiRef>): AiResultItem => ({ ...item, ref: { ...item.ref, ...ref } });
const operation = (items = [item]): AiOperation => ({ status: 'succeeded', run: { id: 'run-1' }, result: {
  kind: 'model', cad_result: saved, items,
} } as AiOperation);

describe('saved output presentation identity', () => {
  it('shares the whole exact model in its project, and keeps prior revisions available', () => {
    const previous = { ...refItem({ key: 'part_revision:r1', object_id: 'r1' }), asset_version_id: 'av1' };
    const op = operation([previous, item]);
    const view = savedResultPresentation(op, 'project');
    expect(view.primary).toBe(item);
    expect(view.remaining).toEqual([previous]);
    expect(view.representedParts).toEqual([{ runId: 'run-1', partId: 'part-1', revisionId: 'r2' }]);
    expect(op.result && 'items' in op.result && op.result.items).toEqual([previous, item]);
  });

  it.each([
    { project_id: 'other' }, { project_id: '' }, { part_id: 'part-2' }, { part_id: null },
    { object_id: 'r1' }, { kind: 'part' as const }, { kind: 'cad_selection' as const },
    { instance: { path: ['wheel'] } }, { entities: { ids: ['circle'] } }, { asset_type: 'drawing' },
  ])('does not treat conflicting or incomplete reference evidence as equality: %j', (ref) => {
    expect(matchesSavedResult(saved, refItem(ref), 'project')).toBe(false);
  });

  it('does not use equal names, labels, paths or conflicting asset versions as identity', () => {
    expect(matchesSavedResult(saved, { ...item, asset_type: 'drawing' }, 'project')).toBe(false);
    expect(matchesSavedResult(saved, { ...item, asset_version_id: 'another-version' }, 'project')).toBe(false);
    expect(matchesSavedResult(saved, item, null)).toBe(false);
    expect(matchesSavedResult({ ...saved, version_id: '' }, item, 'project')).toBe(false);
  });

  it('accepts exact assembly and drawing document owners and refuses subset references', () => {
    const assembly = { ...saved, object_type: 'assembly' as const, object_id: 'assembly-1' };
    const assemblyItem = { ...refItem({ kind: 'assembly_revision', assembly_id: 'assembly-1', part_id: null }), asset_type: 'assembly_model' };
    expect(matchesSavedResult(assembly, assemblyItem, 'project')).toBe(true);
    expect(matchesSavedResult(assembly, { ...assemblyItem, ref: { ...assemblyItem.ref, assembly_id: undefined } }, 'project')).toBe(false);
    expect(matchesSavedResult(assembly, { ...assemblyItem, ref: { ...assemblyItem.ref, instance: { path: ['wheel'] } } }, 'project')).toBe(false);
    const drawing = { ...saved, object_type: 'drawing_document' as const, object_id: 'document-1' };
    const documentItem = { ...refItem({ kind: 'drawing_document_version', document_id: 'document-1', part_id: null }), asset_type: 'drawing_document' };
    expect(matchesSavedResult(drawing, documentItem, 'project')).toBe(true);
    expect(matchesSavedResult(drawing, { ...documentItem, ref: { ...documentItem.ref, document_id: 'other-document' } }, 'project')).toBe(false);
  });

  it('uses a consistent exact asset version for drawing outputs, never a missing or mismatching pointer', () => {
    const drawing = { ...saved, object_type: 'part_drawing' as const };
    const asset = { ...refItem({ kind: 'asset_version', object_id: 'av2', asset_type: 'drawing' }), asset_type: 'drawing' };
    expect(matchesSavedResult(drawing, asset, 'project')).toBe(true);
    expect(matchesSavedResult(drawing, { ...asset, asset_version_id: null }, 'project')).toBe(false);
    expect(matchesSavedResult(drawing, { ...asset, ref: { ...asset.ref, object_id: 'av1' } }, 'project')).toBe(false);
    expect(matchesSavedResult(drawing, { ...asset, ref: { ...asset.ref, part_id: 'other-part' } }, 'project')).toBe(false);
  });

  it('keeps ambiguous normalized records and failed-operation products separate', () => {
    expect(savedResultPresentation(operation([item, { ...item, node_id: 'other-node' }]), 'project').primary).toBeNull();
    expect(savedResultPresentation({ ...operation(), status: 'failed' }, 'project')).toEqual({ primary: null, remaining: [item], representedParts: [], primaryDrawing: null });
  });

  it('never hides another run gallery or an assembly component from a root assembly card', () => {
    expect(savedResultPresentation({ ...operation(), run: { id: 'other-run', status: 'succeeded', title: 'Other' } }, 'project').representedParts).toEqual([]);
    const op = operation();
    if (op.result) op.result.cad_result = { ...saved, object_type: 'assembly', object_id: 'assembly-1' };
    expect(savedResultPresentation(op, 'project').representedParts).toEqual([]);
  });
});

describe('saved part drawing presentation', () => {
  const descriptor: Extract<ProgressDrawing, { kind: 'drawing' }> = {
    kind: 'drawing', id: 'drawing-1', node_id: 'drawing', model_revision_id: 'r2', manifest_sha256: 'manifest-1',
  };
  const brief: DrawingBrief = { drawing_id: 'drawing-1', pages: 2, manifest_sha256: 'manifest-1',
    annotation_status: 'skipped_turbo', dwg_status: 'unverified' };
  const drawingItem: AiResultItem = { ...item, asset_type: 'drawing', node_id: 'drawing', base: item.ref,
    ref: { ...item.ref, kind: 'asset_version', object_id: 'av2', asset_type: 'drawing' } };
  const drawingOperation = (): AiOperation => ({ ...operation([drawingItem]), kind: 'drawing_generate',
    part: { id: 'part-1', name: 'Wheel' }, result: { kind: 'drawing', items: [drawingItem], drawings: [brief],
      cad_result: { ...saved, object_type: 'part_drawing', version_id: 'drawing-1' } },
  });
  const progress = (): RunProgress => ({ ...run('succeeded', [stage('drawing', 'succeeded')]),
    run_id: 'run-1', drawings: [descriptor] });

  it('moves the exact same-run drawing and its whole package into the primary delivery without changing source data', () => {
    const op = drawingOperation();
    const p = progress();
    const before = JSON.stringify([op, p]);
    const view = savedResultPresentation(op, 'project', p);
    expect(view.primary).toBe(drawingItem);
    expect(view.primaryDrawing).toEqual({ runId: 'run-1', drawing: descriptor, brief });
    expect(view.primaryDrawing?.brief).toBe(brief);
    expect(view.primaryDrawing?.drawing).toBe(descriptor);
    expect(view.representedParts).toEqual([]);
    expect(JSON.stringify([op, p])).toBe(before);
  });

  it('does not combine an absent progress snapshot, another project/run/node or a legacy package', () => {
    expect(savedResultPresentation(drawingOperation(), 'project').primaryDrawing).toBeNull();
    expect(savedResultPresentation(drawingOperation(), null, progress()).primaryDrawing).toBeNull();
    expect(savedResultPresentation(drawingOperation(), 'other-project', progress()).primaryDrawing).toBeNull();
    expect(savedResultPresentation(drawingOperation(), 'project', { ...progress(), run_id: 'other-run' }).primaryDrawing).toBeNull();
    for (const patch of [{ node_id: 'other-node' }, { id: 'other-drawing' }, { model_revision_id: 'r1' },
      { model_revision_id: null }, { manifest_sha256: 'other-manifest' }, { manifest_sha256: null }]) {
      expect(savedResultPresentation(drawingOperation(), 'project', { ...progress(), drawings: [{ ...descriptor, ...patch }] }).primaryDrawing).toBeNull();
    }
  });

  it('keeps conflicting or incomplete normalized source ownership and incomplete operations separate', () => {
    for (const base of [null, { ...item.ref, project_id: 'other-project' }, { ...item.ref, part_id: 'other-part' },
      { ...item.ref, object_id: 'r1' }, { ...item.ref, kind: 'part' as const }, { ...item.ref, asset_type: 'drawing' }]) {
      const op = drawingOperation();
      if (op.result?.kind === 'drawing') op.result.items = [{ ...drawingItem, base }];
      expect(savedResultPresentation(op, 'project', progress()).primaryDrawing).toBeNull();
    }
    for (const status of ['failed', 'running', 'cancelled'] as const) {
      expect(savedResultPresentation({ ...drawingOperation(), status }, 'project', progress()).primaryDrawing).toBeNull();
    }
    const op = drawingOperation();
    if (op.result?.kind === 'drawing') op.result.items = [{ ...drawingItem, run_id: 'other-run' }];
    expect(savedResultPresentation(op, 'project', progress()).primaryDrawing).toBeNull();
  });

  it('preserves ambiguous manifests and duplicate package briefs instead of choosing one', () => {
    const op = drawingOperation();
    if (op.result?.kind === 'drawing') op.result.drawings = [brief, { ...brief, annotation_status: 'failed' }];
    expect(savedResultPresentation(op, 'project', progress()).primaryDrawing).toBeNull();
    expect(savedResultPresentation(drawingOperation(), 'project', { ...progress(),
      drawings: [descriptor, { ...descriptor, node_id: 'reused', manifest_sha256: 'other-manifest' }] }).primaryDrawing).toBeNull();
  });

  it('retains unrelated packages while allowing identical saved-page reuse across source nodes', () => {
    const p = { ...progress(), drawings: [descriptor, { ...descriptor, node_id: 'reused' },
      { ...descriptor, id: 'drawing-2', node_id: 'second' },
      { kind: 'assembly_drawing' as const, id: 'assembly-drawing', node_id: 'assembly', assembly_revision_id: 'a1',
        manifest_sha256: 'assembly-manifest', completeness: 'partial' }] };
    const op = drawingOperation();
    if (op.result?.kind === 'drawing') op.result.drawings = [brief, { ...brief, drawing_id: 'drawing-2' }];
    expect(savedResultPresentation(op, 'project', p).primaryDrawing?.drawing).toBe(descriptor);
    expect(p.drawings).toHaveLength(4);
    expect(op.result?.kind === 'drawing' && op.result.drawings).toHaveLength(2);
  });
});

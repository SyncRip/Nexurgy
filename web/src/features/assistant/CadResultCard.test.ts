import { createElement as h } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import type { AiOperation, CadResult } from '../../api/assistant';
import { I18nProvider } from '../../i18n/I18nProvider';
import { ThemeProvider } from '../../prefs/ThemeProvider';
import { CadResultCard, cadResultPath } from './CadResultCard';
import { resultPreviewArtifact, savedAssemblyPreviewTarget, savedDrawingPreviewTarget } from './CadResultPreview';

vi.mock('./AssistantProvider', () => ({ useAssistantSession: () => ({ store: {}, keepDockOnLeave: vi.fn() }) }));

function resultCard(type: CadResult['object_type']): string {
  const result: CadResult = {
    event_id: 'saved-event', object_type: type, object_id: 'object-1', version_id: 'saved-v1', version_label: 'A1',
    base_version_id: null, asset_version_id: null, requirements: [], changes: [], checks: {},
    artifacts: [{ kind: 'step', sha256: 'saved-step', filename: 'cart.step' }],
    undo: { eligible: false, reason: 'no_previous_version', expected_head: 'saved-v1' },
  };
  return renderToStaticMarkup(h(MemoryRouter, null, h(I18nProvider, null, h(ThemeProvider, null,
    h(CadResultCard, { op: { id: 'op-1' } as AiOperation, result, archived: false })))));
}

describe('CAD result links', () => {
  it('opens each saved domain at the returned exact version', () => {
    expect(cadResultPath({ object_type: 'part_model', object_id: 'part-1', version_id: 'r2' })).toBe('/parts/part-1/model?rev=r2');
    expect(cadResultPath({ object_type: 'part_drawing', object_id: 'part-1', version_id: 'd2' })).toBe('/parts/part-1/drawings?drawing=d2');
    expect(cadResultPath({ object_type: 'assembly', object_id: 'asm-1', version_id: 'a2' })).toBe('/assemblies/asm-1/structure?rev=a2');
    expect(cadResultPath({ object_type: 'drawing_document', object_id: 'doc-1', version_id: 'v2' })).toBe('/drafting/doc-1?version=v2');
  });

  it('names the saved assembly and links both its preview and action to the exact revision', () => {
    const html = resultCard('assembly');
    expect(html).toContain('Assembly model');
    expect(html).toContain('View 3D assembly');
    expect(html.match(/href="\/assemblies\/object-1\/structure\?rev=saved-v1"/g)).toHaveLength(2);
    expect(html).toContain('as-cad-preview-link');
    expect(html).toContain('cart.step');
  });

  it('opens a saved part from its preview and workspace action at the same exact version', () => {
    const html = resultCard('part_model');
    expect(html.match(/href="\/parts\/object-1\/model\?rev=saved-v1"/g)).toHaveLength(2);
    expect(html).toContain('as-cad-preview-link');
    expect(html).toContain('Open workspace');
    expect(html).toContain('cart.step');
    expect(html).not.toContain('View 3D assembly');
  });
});

describe('saved CAD previews', () => {
  const mesh = { kind: 'mesh', sha256: 'exact-mesh' };
  const glb = { kind: 'glb', sha256: 'exact-glb' };
  const image = { kind: 'svg', sha256: 'exact-sheet' };
  it('uses an actual saved image ahead of a 3D artifact', () => {
    expect(resultPreviewArtifact([glb, mesh, image])).toBe(image);
  });
  it('uses the saved mesh, then the saved GLB if there is no image', () => {
    expect(resultPreviewArtifact([glb, mesh])).toBe(mesh);
    expect(resultPreviewArtifact([glb])).toBe(glb);
  });
  it('binds standalone preview to the result version without following the document head', () => {
    expect(savedDrawingPreviewTarget({ object_type: 'drawing_document', object_id: 'doc-1', version_id: 'old-v1' })).toEqual({ documentId: 'doc-1', versionId: 'old-v1' });
    expect(savedDrawingPreviewTarget({ object_type: 'drawing_document', object_id: 'doc-1', version_id: '' })).toBeUndefined();
    expect(savedDrawingPreviewTarget({ object_type: 'part_drawing', object_id: 'part-1', version_id: 'd1' })).toBeUndefined();
  });
  it('binds an assembly preview to its saved revision, never an unspecified head', () => {
    expect(savedAssemblyPreviewTarget({ object_type: 'assembly', object_id: 'cart', version_id: 'old-a1' }))
      .toEqual({ assemblyId: 'cart', revisionId: 'old-a1' });
    expect(savedAssemblyPreviewTarget({ object_type: 'assembly', object_id: 'cart', version_id: '' })).toBeUndefined();
    expect(savedAssemblyPreviewTarget({ object_type: 'part_model', object_id: 'cart', version_id: 'r1' })).toBeUndefined();
  });
  it('shows no preview for downloads that cannot be rendered', () => {
    expect(resultPreviewArtifact([{ kind: 'step', sha256: 'step' }, { kind: 'parts_zip', sha256: 'zip' }])).toBeNull();
    expect(resultPreviewArtifact([])).toBeNull();
  });
});

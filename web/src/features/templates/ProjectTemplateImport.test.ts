import { describe, expect, it } from 'vitest';
import type { WorkflowGraph } from '../../api/types';
import { templateReferences } from './ProjectTemplateImport';

describe('project template reference mapping', () => {
  it('maps each supported immutable reference and deduplicates identical sources by kind', () => {
    const graph: WorkflowGraph = { nodes: [
      { id: 'file-a', type: 'file_input', label: 'Drawing file', params: { file_version_id: 'shared-id' } },
      { id: 'file-b', type: 'file_input', params: { file_version_id: 'shared-id' } },
      { id: 'model', type: 'model_input', params: { revision_id: 'shared-id' } },
      { id: 'drawing', type: 'drawing_reconstruct', params: { drawing_id: 'drawing-v1' } },
      { id: 'spec', type: 'engineering_spec', params: { spec_version_id: 'spec-v2' } },
    ].map((node) => ({ ...node, position: { x: 0, y: 0 } })), edges: [], groups: [] };
    expect(templateReferences(graph)).toEqual([
      { kind: 'file_version', source_id: 'shared-id', label: 'Drawing file' },
      { kind: 'model_revision', source_id: 'shared-id', label: 'model' },
      { kind: 'drawing', source_id: 'drawing-v1', label: 'drawing' },
      { kind: 'engineering_spec_version', source_id: 'spec-v2', label: 'spec' },
    ]);
  });
  it('leaves advanced and unknown bindings in the source graph for authoritative server validation', () => {
    const graph: WorkflowGraph = { nodes: [
      { id: 'model', type: 'model_3d', params: { model_config_id: 'global-model', context: { private: true } } },
      { id: 'invalid', type: 'file_input', params: { file_version_id: null } },
    ].map((node) => ({ ...node, position: { x: 0, y: 0 } })), edges: [], groups: [] };
    const before = JSON.stringify(graph);
    expect(templateReferences(graph)).toEqual([]);
    expect(JSON.stringify(graph)).toBe(before);
  });
});

import { describe, expect, it } from 'vitest';
import type { JsonSchema } from '../../api/agents';
import { schemaParams } from './toolParams';

// Shapes copied from the server registry's schemas (GET /agent-tools → args_schema).
const DRAWING: JsonSchema = {
  type: 'object',
  properties: {
    model_asset_version_id: { anyOf: [{ format: 'uuid', type: 'string' }, { type: 'null' }], default: null, description: 'Part model version to draw' },
    views: { anyOf: [{ items: { type: 'string' }, type: 'array' }, { type: 'null' }], default: null },
    scale: { anyOf: [{ type: 'number' }, { type: 'string' }, { type: 'null' }], default: null },
    projection: { anyOf: [{ enum: ['third', 'first'], type: 'string' }, { type: 'null' }], default: null },
  },
};
const DELIVER: JsonSchema = {
  $defs: { Deliverable: { title: 'Deliverable', type: 'object', properties: {} } },
  type: 'object',
  properties: {
    deliverables: { items: { $ref: '#/$defs/Deliverable' }, type: 'array', description: 'One entry per output slot' },
    note: { default: '', type: 'string' },
    include: { anyOf: [{ items: { enum: ['step', 'glb'], type: 'string' }, type: 'array' }, { type: 'null' }], default: null },
    run_checks: { default: true, type: 'boolean' },
  },
  required: ['deliverables'],
};

describe('schemaParams', () => {
  it('reads name, type, required, description and a meaningful default', () => {
    expect(schemaParams(DRAWING).map((p) => [p.name, p.type, p.required])).toEqual([
      ['model_asset_version_id', 'UUID', false],
      ['views', 'string[]', false],
      ['scale', 'number | string', false],
      ['projection', 'third | first', false],
    ]);
    const deliver = schemaParams(DELIVER);
    expect(deliver.map((p) => [p.name, p.type, p.required, p.defaultValue])).toEqual([
      ['deliverables', 'Deliverable[]', true, undefined],
      ['note', 'string', false, undefined],
      ['include', '(step | glb)[]', false, undefined],
      ['run_checks', 'bool', false, 'true'],
    ]);
    expect(deliver[0].description).toBe('One entry per output slot');
    expect(schemaParams(undefined)).toEqual([]);
  });
});

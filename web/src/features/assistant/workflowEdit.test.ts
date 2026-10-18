import { describe, expect, it } from 'vitest';
import type { AiWorkflowEditResult } from '../../api/assistant';
import { holdsAutoApply, planLines, shownWarnings } from './workflowEdit';

const graph = (nodes: { id: string; type: string; label?: string }[]) => ({ nodes: nodes.map((n) => ({ ...n, position: { x: 0, y: 0 }, params: {} })), edges: [], groups: [] });

describe('planLines', () => {
  it('lists every change of the diff in a stable order', () => {
    const r = {
      graph_before: graph([{ id: 'quote', type: 'quote', label: 'Quote' }, { id: 'old', type: 'export' }]),
      graph_after: graph([{ id: 'quote', type: 'quote', label: 'Quote' }, { id: 'rv', type: 'human_review', label: 'Review' }]),
      diff: {
        added_nodes: [{ id: 'rv', type: 'human_review', label: 'Review', params: {}, position: { x: 0, y: 0 } }],
        removed_nodes: [{ id: 'old', type: 'export', label: null }],
        changed_params: [{ node_id: 'quote', type: 'quote', keys: ['margin_pct'], before: { margin_pct: 15 }, after: { margin_pct: 8 } }],
        added_edges: [{ id: 'e1', source: 'quote', source_port: 'quote', target: 'rv', target_port: 'subject' }],
        removed_edges: [],
        renamed: [{ node_id: 'quote', before: 'Quote', after: 'Unit quote' }],
        empty: false,
      },
    } as unknown as AiWorkflowEditResult;
    expect(planLines(r)).toEqual([
      { kind: 'add_node', id: 'rv', type: 'human_review', label: 'Review' },
      { kind: 'remove_node', id: 'old', type: 'export', label: null },
      { kind: 'params', id: 'quote', type: 'quote', label: 'Quote', changes: [{ key: 'margin_pct', before: '15', after: '8' }] },
      { kind: 'rename', id: 'quote', before: 'Quote', after: 'Unit quote' },
      { kind: 'add_edge', from: 'quote.quote', to: 'rv.subject' },
    ]);
  });
});

describe('warnings', () => {
  it('holds auto-apply for private text, removed reviews and moved-on workflows', () => {
    expect(holdsAutoApply([{ code: 'missing_param', message: '' }])).toBe(false);
    expect(holdsAutoApply([{ code: 'private_context', message: '' }])).toBe(true);
    expect(holdsAutoApply([{ code: 'removes_review', message: '' }])).toBe(true);
  });
  it('hides problems the workflow already had', () => {
    expect(shownWarnings([{ code: 'missing_param', message: '', preexisting: true }, { code: 'missing_param', message: '' }])).toHaveLength(1);
  });
});

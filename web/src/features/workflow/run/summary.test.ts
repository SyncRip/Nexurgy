import { describe, expect, it } from 'vitest';
import type { NodeRun } from '../../../api/types';
import { nodeSummary, quoteReviewed } from './summary';

const nr = (node_type: string, outputs: Record<string, unknown>, inputs: Record<string, unknown> = {}, status: NodeRun['status'] = 'succeeded') => ({
  node_type,
  status,
  inputs,
  outputs,
});

describe('nodeSummary', () => {
  it('is empty until the node succeeded', () => {
    expect(nodeSummary(undefined)).toBeNull();
    expect(nodeSummary(nr('model_3d', { model: { type: 'model', rev_no: 2 } }, {}, 'running'))).toBeNull();
    expect(nodeSummary(nr('export', {}))).toBeNull();
  });

  it('reads a model revision and its requirement check', () => {
    const s = nodeSummary(
      nr('model_3d', {
        model: { type: 'model', rev_no: 3, mesh_only: false },
        ai_model: 'simulated',
        requirements_check: { status: 'passed', items: { passed: 11, failed: 1 }, requirements: { total: 12, geometric: 10 } },
      }),
    );
    expect(s).toEqual({ kind: 'model', rev: 3, reqPassed: 11, reqTotal: 12, check: 'passed', meshOnly: false });
  });

  it('shows no requirement ratio without a check report', () => {
    expect(nodeSummary(nr('model_input', { model: { type: 'model', rev_no: 1 } }))).toMatchObject({ kind: 'model', reqPassed: null, reqTotal: null });
  });

  it('takes the drawing revision from the model input', () => {
    const s = nodeSummary(nr('drawing', { drawing: { type: 'drawing', pages: 3, svg_sha: 'abc', annotation_status: 'passed', dwg_status: 'unavailable' } }, { model: { type: 'model', rev_no: 3 } }));
    expect(s).toEqual({ kind: 'drawing', pages: 3, rev: 3, svgSha: 'abc', annotation: 'passed', dwg: 'unavailable' });
  });

  it('reads the first quote tier', () => {
    const s = nodeSummary(nr('quote', { quote: { type: 'quote', currency: 'CNY', quote_id: 'q1', first_tier: { qty: 100, unit_inc_tax_fen: 28747 } } }));
    expect(s).toEqual({ kind: 'quote', qty: 100, unitFen: 28747, currency: 'CNY', quoteId: 'q1' });
  });

  it('reads text, file, plan and spec outputs', () => {
    expect(nodeSummary(nr('text_input', { text: { type: 'text', text: 'Bracket 120 x 80' } }))).toEqual({ kind: 'text', text: 'Bracket 120 x 80' });
    expect(nodeSummary(nr('file_input', { file: { type: 'file', name: 'BRK_customer.pdf' } }))).toMatchObject({ kind: 'file', name: 'BRK_customer.pdf', ext: 'PDF' });
    expect(nodeSummary(nr('export', { bundle: { type: 'file', name: 'export.zip', blob_sha256: 's' } }))).toMatchObject({ kind: 'file', ext: 'ZIP', downloadSha: 's' });
    expect(nodeSummary(nr('process_plan', { plan: { type: 'plan', estimated: true, priceable: false } }))).toEqual({ kind: 'plan', estimated: true, priceable: false });
    expect(nodeSummary(nr('drawing_intake', { spec: { type: 'spec', estimated: true, method: 'ai' } }))).toEqual({ kind: 'spec', estimated: true, method: 'ai' });
  });

  it('names what a review approved', () => {
    expect(nodeSummary(nr('human_review', { subject: { type: 'plan' } }))).toEqual({ kind: 'review', subject: 'plan' });
  });
});

describe('quoteReviewed', () => {
  it('needs a succeeded review of the same quote', () => {
    const review = (status: NodeRun['status'], quote_id: string) => ({ node_type: 'human_review', status, inputs: { subject: { type: 'quote', quote_id } } });
    expect(quoteReviewed('q1', [review('waiting_human', 'q1')])).toBe(false);
    expect(quoteReviewed('q1', [review('succeeded', 'q2')])).toBe(false);
    expect(quoteReviewed('q1', [review('succeeded', 'q1')])).toBe(true);
    expect(quoteReviewed(null, [review('succeeded', 'q1')])).toBe(false);
  });
});

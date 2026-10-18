import { createElement as h, type ReactElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { manufacturingBundleInfo, manufacturingTotal, readManufacturingValue, type ManufacturingQuote } from '../../api/manufacturing';
import type { NodeRun } from '../../api/types';
import { ToastProvider } from '../../components/ui/Toast';
import { translate } from '../../i18n/core';
import { en } from '../../i18n/en';
import { makeFormatters } from '../../i18n/format';
import { I18nProvider, type TFunction } from '../../i18n/I18nProvider';
import { compactLine, ResultBlock, StateBlock } from '../workflow/canvas/NodeResult';
import { nodeErrorText } from '../workflow/labels';
import { PortValue } from '../workflow/run/Artifacts';
import { nodeSummary } from '../workflow/run/summary';
import { ManufacturingValueView } from './ManufacturingValue';
import { manufacturingCounts, manufacturingItemLinks } from './model';

const item = () => ({ item: '1.2', part_id: 'part-A', model_revision_id: 'rev-A', number: 'AX-01', name: 'Axle', procurement: 'make', quantity_per_assembly: 4, status: 'quoted', process_plan_id: 'plan-A', quote_id: 'quote-A', priceable: true, reason_code: null });
const rawQuote = () => ({
  type: 'manufacturing_quote', node_run_id: 'node-quote', assembly_id: 'assembly-A', assembly_revision_id: 'assembly-rev-A', bom_sha256: 'hash',
  estimated: true, pricing_basis: 'synthetic_demo_rates', complete: true, commercial_status: 'draft', price_complete: false,
  currency: 'CNY', quantities: [1], items: [item()], tiers: [{ quantity: 1, known_fen: 12500, total_fen: null, unknown_count: 1, complete: false }],
});

function quote(value: unknown = rawQuote()): ManufacturingQuote {
  const parsed = readManufacturingValue(value);
  if (parsed?.type !== 'manufacturing_quote') throw new Error('Expected a quote batch');
  return parsed;
}

function render(node: ReactElement, language = 'en') {
  vi.stubGlobal('localStorage', { getItem: () => language });
  return renderToStaticMarkup(h(MemoryRouter, null, h(I18nProvider, null, h(ToastProvider, null, node))));
}
const t: TFunction = (key, vars) => translate(en, key, vars);
const fmt = makeFormatters('en');
const nodeRun = (outputs: Record<string, unknown>, status: NodeRun['status'] = 'succeeded'): NodeRun => ({
  id: 'node-run', node_id: 'quote', node_type: 'assembly_quote', status, outputs, inputs: {}, attempt: 1, max_attempts: 1,
  error: null, steps: [], started_at: null, finished_at: null, updated_at: null,
});
afterEach(() => vi.unstubAllGlobals());

describe('stored manufacturing batch boundary', () => {
  it('only recognizes the two explicit batch kinds', () => {
    for (const value of [null, [], {}, { type: 'quote' }, { type: ['manufacturing_quote'] }]) expect(readManufacturingValue(value)).toBeNull();
    expect(quote().data_issues).toBe(false);
  });

  it('preserves the known subtotal and refuses a full total while labour or purchase costs are unknown', () => {
    const v = quote();
    expect(v.tiers[0].known_fen).toBe(12500);
    expect(manufacturingTotal(v, v.tiers[0])).toBeNull();
    const html = render(h(ManufacturingValueView, { value: v }));
    expect(html).toContain('Known subtotal');
    expect(html).toContain('125.00');
    expect(html).toContain('Full total</dt><dd>Awaiting pricing</dd>');
    expect(html).toContain('assembly labour');
    expect(html).toContain('Estimate · example rates');
    expect(html).toContain('not a confirmed supplier price');
  });

  it('accepts a real zero only with complete explicit price coverage', () => {
    const complete = { ...rawQuote(), price_complete: true, tiers: [{ quantity: 1, known_fen: 0, total_fen: 0, unknown_count: 0, complete: true }] };
    const valid = quote(complete);
    expect(manufacturingTotal(valid, valid.tiers[0])).toBe(0);
    for (const patch of [{ price_complete: false }, { complete: false }, { currency: 'USD' }, { pricing_basis: 'supplier' }, { items: [{ ...item(), status: 'unpriced' }] }]) {
      const v = quote({ ...complete, ...patch });
      expect(manufacturingTotal(v, v.tiers[0])).toBeNull();
    }
  });

  it('never coerces nulls, strings, unsafe amounts or an unsupported currency into CNY prices', () => {
    for (const amount of [null, '0', false, -1, Infinity, Number.MAX_SAFE_INTEGER + 1]) {
      const v = quote({ ...rawQuote(), price_complete: true, tiers: [{ quantity: 1, known_fen: amount, total_fen: amount, unknown_count: 0, complete: true }] });
      expect(v.tiers[0].known_fen).toBeNull();
      expect(manufacturingTotal(v, v.tiers[0])).toBeNull();
      expect(render(h(ManufacturingValueView, { value: v }))).not.toContain('0.00');
    }
    const foreign = quote({ ...rawQuote(), currency: 'USD' });
    expect(foreign.currency).toBeNull();
    expect(render(h(ManufacturingValueView, { value: foreign }))).not.toContain('125.00');
  });

  it('keeps invalid and excluded rows visible and never counts them as produced', () => {
    const v = quote({ ...rawQuote(), items: [item(), null, { ...item(), item: '2', status: 'excluded', procurement: 'buy', process_plan_id: null, quote_id: null, reason_code: 'purchase_cost_required' }] });
    expect(v.items).toHaveLength(3);
    expect(v.data_issues).toBe(true);
    expect(v.complete).toBe(false);
    expect(manufacturingCounts(v)).toEqual({ total: 3, produced: 1, excluded: 1, unresolved: 1 });
    const html = render(h(ManufacturingValueView, { value: v }));
    expect(html).toContain('Details missing');
    expect(html).toContain('Partial');
    expect(html).toContain('purchase cost is missing');
  });

  it('does not accept missing tiers or duplicate/mismatched quantity coverage', () => {
    for (const patch of [{ tiers: [] }, { tiers: [null] }, { quantities: [1, 10] }, { quantities: [1, 1] }, { quantities: ['1'] }]) {
      expect(quote({ ...rawQuote(), ...patch }).data_issues).toBe(true);
    }
    const tier = { quantity: 1, known_fen: 0, total_fen: 0, unknown_count: 0, complete: true };
    const duplicate = quote({ ...rawQuote(), quantities: [1, 1], price_complete: true, tiers: [tier, { ...tier, quantity: 10 }] });
    expect(duplicate.data_issues).toBe(true);
    expect(manufacturingTotal(duplicate, duplicate.tiers[1])).toBeNull();
  });
});

describe('manufacturing results in existing run views', () => {
  it('translates actual saved manufacturing reason codes and unresolved design recovery', () => {
    const reasons = ['purchase_cost_required', 'unsupported_representation', 'quantity_out_of_range', 'plan_missing', 'plan_inputs_mismatch'];
    const value = quote({ ...rawQuote(), items: reasons.map((reason_code, i) => ({ ...item(), item: String(i + 1), status: 'unpriced', quote_id: null, reason_code })) });
    for (const language of ['en', 'zh']) {
      const html = render(h(ManufacturingValueView, { value }), language);
      for (const code of reasons) expect(html).not.toContain(code);
    }
    expect(nodeErrorText(t, 'design_choice_required', 'raw server message')).toContain('start a new run');
    expect(nodeErrorText(t, 'manufacturing_plan_partial', 'raw server message')).toContain('Available plans');
    const failed = { ...nodeRun({}, 'failed'), error: { code: 'design_choice_required', message: 'raw server message', details: {} } };
    expect(render(h(StateBlock, { nr: failed, hasUpstream: true }), 'zh')).toContain('请将所需选择补充到需求输入中');
  });

  it('links each part to exactly its saved model, plan and quote, independent of the run part', () => {
    const raw = rawQuote();
    raw.items.push({ ...item(), part_id: 'part-B', model_revision_id: 'rev-B', process_plan_id: 'plan-B', quote_id: 'quote-B' });
    const html = render(h(PortValue, { value: raw, partId: 'unrelated-run-part' }));
    for (const suffix of ['A', 'B']) {
      expect(html).toContain(`/parts/part-${suffix}/model?rev=rev-${suffix}`);
      expect(html).toContain(`/parts/part-${suffix}/process?plan=plan-${suffix}`);
      expect(html).toContain(`/parts/part-${suffix}/quote?quote=quote-${suffix}`);
    }
    expect(html).toContain('/assemblies/assembly-A/structure?rev=assembly-rev-A');
    expect(html).toContain('4 per assembly');
    expect(html).not.toContain('unrelated-run-part');
    const missing = quote({ ...rawQuote(), items: [{ ...item(), model_revision_id: null, process_plan_id: null, quote_id: null }] }).items[0];
    expect(manufacturingItemLinks(missing)).toEqual({ model: null, plan: null, quote: null });
  });

  it('renders plan statuses and bilingual copy without losing nonmanufactured or failed parts', () => {
    const v = readManufacturingValue({ ...rawQuote(), type: 'manufacturing_plan', items: ['planned', 'excluded', 'unsupported', 'failed'].map((status, i) => ({ ...item(), item: String(i + 1), status })) });
    expect(v?.type).toBe('manufacturing_plan');
    expect(v?.items.every(row => row.quote_id === null)).toBe(true);
    const html = render(h(ManufacturingValueView, { value: v! }), 'zh');
    expect(html).toContain('已生成 1/4 份零件工艺单');
    expect(html).toContain('暂不支持');
    expect(html).toContain('失败');
    expect(html).toContain('示例费率估算');
  });

  it('keeps compact summaries small and preserves running or failed node precedence', () => {
    const succeeded = nodeRun({ quotes: rawQuote() });
    expect(compactLine(t, fmt, succeeded, null)).toContain('1/1 part estimates');
    expect(compactLine(t, fmt, nodeRun(succeeded.outputs!, 'running'), null)).toBe(t('cv.state.running'));
    expect(compactLine(t, fmt, nodeRun(succeeded.outputs!, 'failed'), null)).toContain(`${t('cv.state.failed')} ·`);
    const compact = render(h(ResultBlock, { nr: succeeded, runs: undefined }));
    expect(compact).toContain('Awaiting pricing');
    expect(compact).toContain('Estimate · example rates');
    expect(compact).not.toContain('/parts/part-A/');
    const failed = render(h(StateBlock, { nr: nodeRun(succeeded.outputs!, 'failed'), hasUpstream: true }));
    expect(failed).toContain('Failed');
    expect(failed).toContain('1/1 part estimates');
  });

  it('carries bundle incompleteness into both the download view and canvas summary', () => {
    const bundle = { type: 'file', name: 'manufacturing.zip', pricing_basis: 'synthetic_demo_rates', estimated: true, complete: false, price_complete: false, geometry_complete: false };
    expect(manufacturingBundleInfo({ type: 'file', name: 'ordinary.zip' })).toBeNull();
    const nr = nodeRun({ bundle });
    expect(compactLine(t, fmt, nr, nodeSummary(nr))).toContain('Awaiting pricing');
    const html = render(h(PortValue, { value: bundle, partId: null }));
    expect(html).toContain('This package is incomplete');
    expect(html).toContain('Geometry or drawings are incomplete');
    expect(html).toContain('Estimate · example rates');
  });
});

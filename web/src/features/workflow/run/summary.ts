// Result summary shown on a run canvas node card (R1.2 prototype: "Drawing package, 3 pages · R3",
// "R3 · Requirements 12/12", the quote's unit price…). Pure: built from the node run's
// recorded inputs / outputs only — nothing is estimated or invented here.

import type { NodeRun } from '../../../api/types';
import { manufacturingBundleInfo, type ManufacturingBundleInfo } from '../../../api/manufacturing';

type Obj = Record<string, unknown>;

function obj(v: unknown): Obj | null {
  return typeof v === 'object' && v !== null && !Array.isArray(v) ? (v as Obj) : null;
}

function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

function str(v: unknown): string | null {
  return typeof v === 'string' && v ? v : null;
}

/** The first port value of a given artifact type among the outputs (ports are named per node type). */
function firstOfType(values: Obj | null | undefined, type: string): Obj | null {
  for (const v of Object.values(values ?? {})) {
    const o = obj(v);
    if (o && o.type === type) return o;
  }
  return null;
}

export type NodeSummary =
  | { kind: 'text'; text: string }
  | { kind: 'file'; name: string; ext: string; downloadSha: string | null; manufacturing: ManufacturingBundleInfo | null }
  | { kind: 'model'; rev: number | null; reqPassed: number | null; reqTotal: number | null; check: string | null; meshOnly: boolean }
  | { kind: 'drawing'; pages: number | null; rev: number | null; svgSha: string | null; annotation: string | null; dwg: string | null }
  | { kind: 'spec'; estimated: boolean; method: string | null }
  | { kind: 'plan'; estimated: boolean; priceable: boolean }
  | { kind: 'quote'; qty: number | null; unitFen: number | null; currency: string; quoteId: string | null }
  | { kind: 'review'; subject: string | null };

/** What the node produced, or null when it produced nothing worth showing (not finished, no outputs). */
export function nodeSummary(nr: Pick<NodeRun, 'node_type' | 'status' | 'inputs' | 'outputs'> | undefined): NodeSummary | null {
  if (!nr || nr.status !== 'succeeded') return null;
  const out = obj(nr.outputs) ?? {};
  const inputs = obj(nr.inputs) ?? {};
  if (nr.node_type === 'human_review') {
    const subject = obj(out.subject) ?? obj(inputs.subject);
    return { kind: 'review', subject: str(subject?.type) };
  }
  const text = firstOfType(out, 'text');
  if (text) return { kind: 'text', text: str(text.text) ?? '' };
  const model = firstOfType(out, 'model');
  if (model) {
    const check = obj(out.requirements_check);
    const items = obj(check?.items);
    const reqs = obj(check?.requirements);
    const total = num(reqs?.total);
    return {
      kind: 'model',
      rev: num(model.rev_no),
      // Only a real check report gives "passed / total"; without one the card shows the revision alone.
      reqPassed: total !== null ? (num(items?.passed) ?? 0) : null,
      reqTotal: total,
      check: str(check?.status),
      meshOnly: model.mesh_only === true,
    };
  }
  const drawing = firstOfType(out, 'drawing');
  if (drawing) {
    const src = firstOfType(inputs, 'model');
    return {
      kind: 'drawing',
      pages: num(drawing.pages),
      rev: num(src?.rev_no),
      svgSha: str(drawing.svg_sha),
      annotation: str(drawing.annotation_status),
      dwg: str(drawing.dwg_status),
    };
  }
  const quote = firstOfType(out, 'quote');
  if (quote) {
    const tier = obj(quote.first_tier);
    return { kind: 'quote', qty: num(tier?.qty), unitFen: num(tier?.unit_inc_tax_fen), currency: str(quote.currency) ?? 'CNY', quoteId: str(quote.quote_id) };
  }
  const plan = firstOfType(out, 'plan');
  if (plan) return { kind: 'plan', estimated: plan.estimated === true, priceable: plan.priceable !== false };
  const spec = firstOfType(out, 'spec');
  if (spec) return { kind: 'spec', estimated: spec.estimated === true, method: str(spec.method) };
  const file = firstOfType(out, 'file');
  if (file) {
    const name = str(file.name) ?? '';
    const dot = name.lastIndexOf('.');
    return { kind: 'file', name, ext: dot > 0 ? name.slice(dot + 1).toUpperCase().slice(0, 4) : '', downloadSha: str(file.blob_sha256), manufacturing: manufacturingBundleInfo(file) };
  }
  return null;
}

/** Whether a quote was approved by a succeeded review node of the same run (the card says "reviewed", never "formal" on its own). */
export function quoteReviewed(quoteId: string | null, runs: Iterable<Pick<NodeRun, 'node_type' | 'status' | 'inputs'>>): boolean {
  if (!quoteId) return false;
  for (const n of runs) {
    if (n.node_type !== 'human_review' || n.status !== 'succeeded') continue;
    const subject = obj(obj(n.inputs)?.subject);
    if (subject?.type === 'quote' && subject.quote_id === quoteId) return true;
  }
  return false;
}

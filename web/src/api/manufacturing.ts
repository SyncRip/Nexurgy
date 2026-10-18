/** Stored manufacturing batches are decoded once at the UI boundary. Unknown amounts stay unknown. */
type Obj = Record<string, unknown>;
const object = (value: unknown): Obj | null => typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Obj : null;
const text = (value: unknown): string | null => typeof value === 'string' && value.trim() ? value : null;
const integer = (value: unknown, min = 0): number | null => typeof value === 'number' && Number.isSafeInteger(value) && value >= min ? value : null;
const boolean = (value: unknown): boolean | null => typeof value === 'boolean' ? value : null;

export type ManufacturingStatus = 'planned' | 'quoted' | 'excluded' | 'unsupported' | 'unpriced' | 'failed' | 'unknown';

export interface ManufacturingItem {
  readonly item: string | null;
  readonly part_id: string | null;
  readonly model_revision_id: string | null;
  readonly number: string | null;
  readonly name: string | null;
  readonly procurement: string | null;
  readonly quantity_per_assembly: number | null;
  readonly status: ManufacturingStatus;
  readonly process_plan_id: string | null;
  readonly quote_id: string | null;
  readonly priceable: boolean | null;
  readonly reason_code: string | null;
  readonly valid: boolean;
}

interface ManufacturingBase {
  readonly node_run_id: string | null;
  readonly assembly_id: string | null;
  readonly assembly_revision_id: string | null;
  readonly bom_sha256: string | null;
  readonly estimated: boolean | null;
  readonly pricing_basis: string | null;
  readonly complete: boolean;
  readonly data_issues: boolean;
  readonly items: readonly ManufacturingItem[];
}

export interface ManufacturingPlan extends ManufacturingBase {
  readonly type: 'manufacturing_plan';
}

export interface ManufacturingTier {
  readonly quantity: number | null;
  readonly known_fen: number | null;
  readonly total_fen: number | null;
  readonly unknown_count: number | null;
  readonly complete: boolean;
  readonly valid: boolean;
}

export interface ManufacturingQuote extends ManufacturingBase {
  readonly type: 'manufacturing_quote';
  readonly commercial_status: string | null;
  readonly price_complete: boolean;
  readonly quantities: readonly (number | null)[];
  readonly tiers: readonly ManufacturingTier[];
  readonly currency: 'CNY' | null;
}

export type ManufacturingValue = ManufacturingPlan | ManufacturingQuote;

function itemOf(value: unknown, kind: ManufacturingValue['type']): ManufacturingItem {
  const v = object(value) ?? {};
  const allowed = kind === 'manufacturing_plan' ? ['planned', 'excluded', 'unsupported', 'failed'] : ['quoted', 'excluded', 'unpriced', 'failed'];
  const status = typeof v.status === 'string' && allowed.includes(v.status) ? v.status as ManufacturingStatus : 'unknown';
  const row = {
    item: text(v.item), part_id: text(v.part_id), model_revision_id: text(v.model_revision_id),
    number: text(v.number), name: text(v.name), procurement: text(v.procurement),
    quantity_per_assembly: integer(v.quantity_per_assembly, 1), status,
    process_plan_id: text(v.process_plan_id), quote_id: kind === 'manufacturing_quote' ? text(v.quote_id) : null,
    priceable: boolean(v.priceable), reason_code: text(v.reason_code),
  };
  const hasOutput = status === 'planned' || status === 'quoted';
  const valid = !!row.item && !!row.part_id && row.quantity_per_assembly !== null && status !== 'unknown'
    && (!hasOutput || (!!row.model_revision_id && !!row.process_plan_id)) && (status !== 'quoted' || !!row.quote_id);
  return { ...row, valid };
}

function tierOf(value: unknown): ManufacturingTier {
  const v = object(value) ?? {};
  const quantity = integer(v.quantity, 1);
  const known_fen = integer(v.known_fen);
  const total_fen = integer(v.total_fen);
  const unknown_count = integer(v.unknown_count);
  const valid = quantity !== null && known_fen !== null && unknown_count !== null && boolean(v.complete) !== null
    && (v.total_fen === null || total_fen !== null);
  return { quantity, known_fen, total_fen, unknown_count, complete: v.complete === true && valid, valid };
}

/** Keep malformed rows visible as unknown; dropping them could make an incomplete batch look complete. */
export function readManufacturingValue(value: unknown): ManufacturingValue | null {
  const v = object(value);
  if (!v || (v.type !== 'manufacturing_plan' && v.type !== 'manufacturing_quote')) return null;
  const kind = v.type;
  const items = Array.isArray(v.items) ? v.items.map(row => itemOf(row, kind)) : [];
  const base = {
    node_run_id: text(v.node_run_id), assembly_id: text(v.assembly_id), assembly_revision_id: text(v.assembly_revision_id),
    bom_sha256: text(v.bom_sha256), estimated: boolean(v.estimated), pricing_basis: text(v.pricing_basis), items,
  };
  const issues = !base.node_run_id || !base.assembly_id || !base.assembly_revision_id || !base.bom_sha256
    || base.estimated !== true || base.pricing_basis !== 'synthetic_demo_rates' || boolean(v.complete) === null
    || !Array.isArray(v.items) || !items.length || items.some(row => !row.valid);
  if (v.type === 'manufacturing_plan') return { ...base, type: v.type, complete: v.complete === true && !issues, data_issues: issues };
  const quantities = Array.isArray(v.quantities) ? v.quantities.map(q => integer(q, 1)) : [];
  const tiers = Array.isArray(v.tiers) ? v.tiers.map(tierOf) : [];
  const currency = v.currency === 'CNY' ? 'CNY' : null;
  const data_issues = issues || !currency || v.commercial_status !== 'draft' || boolean(v.price_complete) === null
    || !quantities.length || quantities.some(q => q === null) || !tiers.length || tiers.some(tier => !tier.valid)
    || new Set(quantities).size !== quantities.length || quantities.length !== tiers.length
    || quantities.some(q => tiers.filter(tier => tier.quantity === q).length !== 1);
  return {
    ...base, type: v.type, currency, quantities, tiers, commercial_status: text(v.commercial_status), data_issues,
    complete: v.complete === true && !data_issues, price_complete: v.price_complete === true && !data_issues,
  };
}

export function manufacturingValueOf(node: { readonly outputs?: Record<string, unknown> | null } | undefined): ManufacturingValue | null {
  for (const value of Object.values(node?.outputs ?? {})) {
    const batch = readManufacturingValue(value);
    if (batch) return batch;
  }
  return null;
}

/** The full price needs explicit completeness at both levels. A valid known subtotal can still be zero. */
export function manufacturingTotal(quote: ManufacturingQuote, tier: ManufacturingTier): number | null {
  return quote.currency === 'CNY' && quote.price_complete && quote.complete && !quote.data_issues
    && quote.items.every(row => row.valid && row.status === 'quoted')
    && tier.valid && tier.complete && tier.unknown_count === 0 ? tier.total_fen : null;
}

export interface ManufacturingBundleInfo {
  readonly complete: boolean;
  readonly price_complete: boolean;
  readonly geometry_complete: boolean | null;
}

export function manufacturingBundleInfo(value: unknown): ManufacturingBundleInfo | null {
  const v = object(value);
  return v?.type === 'file' && v.pricing_basis === 'synthetic_demo_rates'
    ? { complete: v.complete === true, price_complete: v.price_complete === true, geometry_complete: boolean(v.geometry_complete) } : null;
}

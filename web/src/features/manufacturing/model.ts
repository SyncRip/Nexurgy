import { manufacturingTotal, type ManufacturingItem, type ManufacturingValue } from '../../api/manufacturing';
import { partOutputPath } from '../../app/versionLinks';
import type { TFunction } from '../../i18n/I18nProvider';

export function manufacturingCounts(value: ManufacturingValue) {
  const produced = value.items.filter(row => row.valid && (row.status === 'planned' || row.status === 'quoted')).length;
  const excluded = value.items.filter(row => row.valid && row.status === 'excluded').length;
  return { total: value.items.length, produced, excluded, unresolved: value.items.length - produced - excluded };
}

export function manufacturingLine(t: TFunction, value: ManufacturingValue): string {
  const counts = manufacturingCounts(value);
  const line = t(value.type === 'manufacturing_plan' ? 'mfg.plans.count' : 'mfg.quotes.count', { n: counts.produced, total: counts.total });
  if (value.type !== 'manufacturing_quote') return line;
  const pricePending = !value.tiers.length || value.tiers.some(tier => manufacturingTotal(value, tier) === null);
  return `${line} · ${t(value.pricing_basis === 'synthetic_demo_rates' ? 'mfg.estimate' : 'mfg.basisUnknown')}${pricePending ? ` · ${t('mfg.pricePending')}` : ''}`;
}

/** Batch rows belong to different parts. No run-level part or latest-version fallback is valid here. */
export function manufacturingItemLinks(row: ManufacturingItem) {
  return {
    model: row.part_id && row.model_revision_id ? partOutputPath(row.part_id, 'model', row.model_revision_id) : null,
    plan: row.part_id && row.process_plan_id ? partOutputPath(row.part_id, 'process', row.process_plan_id) : null,
    quote: row.part_id && row.quote_id ? partOutputPath(row.part_id, 'quote', row.quote_id) : null,
  };
}

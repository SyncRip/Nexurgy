import { Calculator, CircleCheck, CircleMinus, CircleX, ClipboardList, ExternalLink, FlaskConical, TriangleAlert } from 'lucide-react';
import { Link } from 'react-router-dom';
import { manufacturingTotal, type ManufacturingBundleInfo, type ManufacturingItem, type ManufacturingStatus, type ManufacturingValue } from '../../api/manufacturing';
import { assemblyRevisionPath } from '../../app/versionLinks';
import { Chip, type Tone } from '../../components/ui/Badges';
import { hasKey } from '../../i18n/core';
import { useI18n } from '../../i18n/I18nProvider';
import { manufacturingCounts, manufacturingItemLinks, manufacturingLine } from './model';
import './manufacturing.css';

const STATUS_TONE: Record<ManufacturingStatus, Tone> = {
  planned: 'ok', quoted: 'measure', excluded: 'pending', unsupported: 'warn', unpriced: 'warn', failed: 'err', unknown: 'warn',
};

export function ManufacturingEstimateBadge({ synthetic = true }: { synthetic?: boolean }) {
  const { t } = useI18n();
  return <Chip tone="warn" icon={<FlaskConical aria-hidden />} title={t(synthetic ? 'mfg.estimateNote' : 'mfg.basisUnknown')}>
    {t(synthetic ? 'mfg.estimate' : 'mfg.basisUnknown')}
  </Chip>;
}

export function ManufacturingBundleNotice({ value, compact = false }: { value: ManufacturingBundleInfo; compact?: boolean }) {
  const { t } = useI18n();
  return <div className="mfg-notice stack-sm">
    <ManufacturingEstimateBadge />
    {!compact && <span className="small muted">{t('mfg.estimateNote')}</span>}
    {!value.price_complete && <span className="small">{t(compact ? 'mfg.pricePending' : 'mfg.priceIncomplete')}</span>}
    {value.geometry_complete !== true && <span className="small">{t('mfg.geometryIncomplete')}</span>}
    {!value.complete && <span className="small">{t('mfg.bundleIncomplete')}</span>}
  </div>;
}

function ManufacturingRow({ row, index }: { row: ManufacturingItem; index: number }) {
  const { t, fmt } = useI18n();
  const links = manufacturingItemLinks(row);
  const status = row.valid ? row.status : 'unknown';
  const Icon = status === 'failed' ? CircleX : status === 'excluded' ? CircleMinus : status === 'planned' || status === 'quoted' ? CircleCheck : TriangleAlert;
  const reasonKey = `mfg.reason.${row.reason_code}`;
  return <li className="mfg-row">
    <div className="mfg-row-head">
      <strong>{row.number || row.item || t('mfg.rowFallback', { n: index + 1 })}{row.name ? ` · ${row.name}` : ''}</strong>
      <Chip tone={STATUS_TONE[status]} icon={<Icon aria-hidden />}>{t(`mfg.status.${status}`)}</Chip>
    </div>
    <span className="small muted">{t('mfg.perAssembly', { n: row.quantity_per_assembly === null ? '—' : fmt.number(row.quantity_per_assembly) })}</span>
    {row.reason_code && <span className="small">{hasKey(reasonKey) ? t(reasonKey) : row.reason_code}</span>}
    {row.priceable === false && <span className="small">{t('wf.art.notPriceable')}</span>}
    <div className="mfg-links">
      {links.model && <Link to={links.model}>{t('mfg.openModel')} <ExternalLink aria-hidden /></Link>}
      {links.plan && <Link to={links.plan}>{t('wf.art.openPlan')} <ExternalLink aria-hidden /></Link>}
      {links.quote && <Link to={links.quote}>{t('mfg.openQuote')} <ExternalLink aria-hidden /></Link>}
    </div>
  </li>;
}

/** The compact variant fits the existing canvas result slot; full results keep every BOM row and exact link. */
export function ManufacturingValueView({ value, compact = false }: { value: ManufacturingValue; compact?: boolean }) {
  const { t, fmt } = useI18n();
  const counts = manufacturingCounts(value);
  const quote = value.type === 'manufacturing_quote' ? value : null;
  const fullPrice = quote && quote.tiers.length > 0 && quote.tiers.every(tier => manufacturingTotal(quote, tier) !== null);
  const money = (fen: number | null) => fen !== null && quote?.currency === 'CNY' ? fmt.number(fen / 100, { style: 'currency', currency: 'CNY' }) : '—';
  return <div className={`mfg-value stack-sm${compact ? ' compact' : ''}`}>
    <div className="mfg-row-head">
      <span className="mfg-kind">{quote ? <Calculator aria-hidden /> : <ClipboardList aria-hidden />}{t(quote ? 'wf.ptype.manufacturing_quote' : 'wf.ptype.manufacturing_plan')}</span>
      <ManufacturingEstimateBadge synthetic={value.pricing_basis === 'synthetic_demo_rates'} />
      {!value.complete && <Chip tone="warn" icon={<TriangleAlert aria-hidden />}>{t('orch.completeness.partial')}</Chip>}
    </div>
    <strong className="small">{quote ? t('mfg.quotes.count', { n: counts.produced, total: counts.total }) : manufacturingLine(t, value)}</strong>
    {(counts.excluded > 0 || counts.unresolved > 0) && <span className="small muted">{t('mfg.batchExceptions', { excluded: counts.excluded, unresolved: counts.unresolved })}</span>}
    {value.data_issues && <span className="small err-text">{t('mfg.invalidData')}</span>}
    {quote && !fullPrice && <span className="small">{t(compact ? 'mfg.pricePending' : 'mfg.priceIncomplete')}</span>}
    {!compact && <>
      <span className="small muted">{t(value.pricing_basis === 'synthetic_demo_rates' ? 'mfg.estimateNote' : 'mfg.basisUnknown')}</span>
      {value.assembly_id && value.assembly_revision_id && <Link className="mfg-source" to={assemblyRevisionPath(value.assembly_id, value.assembly_revision_id)}>{t('mfg.openAssembly')} <ExternalLink aria-hidden /></Link>}
      {quote && quote.tiers.length > 0 && <ul className="mfg-tiers" aria-label={t('mfg.tiers')}>
        {quote.tiers.map((tier, i) => {
          const total = manufacturingTotal(quote, tier);
          return <li className="mfg-tier" key={i}>
            <strong>{t('mfg.tierQuantity', { n: tier.quantity === null ? '—' : fmt.number(tier.quantity) })}</strong>
            <dl>
              <div><dt>{t('mfg.knownSubtotal')}</dt><dd>{money(tier.known_fen)}</dd></div>
              <div><dt>{t('mfg.total')}</dt><dd>{total === null ? t('mfg.pricePending') : money(total)}</dd></div>
            </dl>
            {total === null && <span className="small muted">{tier.unknown_count !== null && tier.unknown_count > 0 ? t('mfg.unknownCount', { n: tier.unknown_count }) : t('mfg.unknownCosts')}</span>}
          </li>;
        })}
      </ul>}
      {value.items.length ? <ul className="mfg-rows" aria-label={t('mfg.parts')}>{value.items.map((row, i) => <ManufacturingRow key={i} row={row} index={i} />)}</ul> : <span className="small muted">{t('mfg.noItems')}</span>}
    </>}
  </div>;
}

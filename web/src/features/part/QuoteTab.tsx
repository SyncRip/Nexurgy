import { exactVersion } from '../assistant/exactVersion';
// Part workspace — Quote tab: quote from the latest process plan (quantity
// tiers, margin, tax, validity), traceable cost lines, operation costs,
// alerts, staleness and quote approval. "Formal" only when the part status
// says every confirmation holds on current data. "Add to AI" on the whole
// quote, each cost line / total of a tier (quote_field: the server reads the
// value and formula) and on selected text (excerpt) — for explanations only;
// the AI never changes a quote.

import { BadgeCheck,Calculator,ChevronDown,ChevronRight,Info,Receipt } from 'lucide-react';
import { Fragment,useEffect,useRef,useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import type { QuoteFieldKey } from '../../api/assistant';
import { isApiError } from '../../api/client';
import { createQuote,getPartStatus,listProcessPlans,listQuotes } from '../../api/endpoints';
import type { PartPipelineStatus,PlanGap,Quote,QuoteTier } from '../../api/types';
import { isHistorical } from '../../app/versionLinks';
import { Chip } from '../../components/ui/Badges';
import { HistoricalBanner } from '../../components/ui/HistoricalBanner';
import { EmptyState,ErrorNotice,ErrorState,SkeletonRows } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import type { PartTabProps } from '../../pages/part/registry';

import { useAssistant,useAssistantPageContext } from '../assistant/AssistantProvider';
import { quoteFieldRef,quoteRef } from '../assistant/refs';
import { AutoSelectionCapture,aiSourceProps } from '../assistant/SelectionContext';
import { CostBreakdownBar } from './CostBreakdownBar';
import { ApprovalActions,ApprovalChip,Details,EstimatedBadge,KV,ReasonList,SourceList,StaleBanner,useBilingual,useLabel,useMemberNames,useMoney,useNum } from './common';
import { NumField } from './model/fields';
import { StatusBanner } from './StatusBanner';
import { ManufacturingEstimateBadge } from '../manufacturing/ManufacturingValue';
import './workspace.css';

function parseQuantities(s: string): number[] | null {
  const parts = s
    .split(/[,，\s]+/)
    .map((x) => x.trim())
    .filter(Boolean);
  if (!parts.length || parts.length > 20) return null;
  const nums = parts.map(Number);
  if (nums.some((n) => !Number.isInteger(n) || n < 1 || n > 1_000_000)) return null;
  return [...new Set(nums)].sort((a, b) => a - b);
}

export default function QuoteTab({ part, project, caps }: PartTabProps) {
  const { t, fmt } = useI18n();
  const toast = useToast();
  const names = useMemberNames(project.id);
  const money = useMoney();
  const statusRes = useResource((signal) => getPartStatus(part.id, { signal }), [part.id]);
  const plansRes = useResource((signal) => listProcessPlans(part.id, { signal }), [part.id]);
  const quotesRes = useResource((signal) => listQuotes(part.id, { signal }), [part.id]);
  // ?quote=<id> opens that quote (links from AI references).
  const [search] = useSearchParams();
  const quoteParam = search.get('quote');
  const [selId, setSelId] = useState<string | null>(quoteParam);
  useEffect(() => {
    if (quoteParam) setSelId(quoteParam);
  }, [quoteParam]);
  const [qty, setQty] = useState('1, 10, 50, 100');
  const [margin, setMargin] = useState(15);
  const [tax, setTax] = useState(13);
  const [validity, setValidity] = useState(30);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const reloadAll = () => {
    statusRes.reload();
    quotesRes.reload();
    plansRes.reload();
  };

  // Offer the quote on screen to the assistant (never added automatically).
  const shown = exactVersion(quotesRes.data, selId);
  useAssistantPageContext({
    projectId: project.id,
    label: t('ar.quote.pageLabel', { number: part.number }),
    partId: part.id,
    object: { type: 'quote', id: part.id, version: shown?.id ?? null },
    target: shown ? quoteRef(project.id, shown.id) : null,
  });

  if (quotesRes.error && !quotesRes.data) return <ErrorState error={quotesRes.error} onRetry={quotesRes.reload} />;
  if (!quotesRes.data || !plansRes.data) return <SkeletonRows rows={5} />;

  const quotes = quotesRes.data;
  const plan = plansRes.data[0] ?? null;
  const quote = shown;
  const quantities = parseQuantities(qty);
  const marginOk = margin >= 0 && margin <= 200;
  const taxOk = tax >= 0 && tax <= 100;
  const validityOk = Number.isInteger(validity) && validity >= 1 && validity <= 365;
  const canCreate = caps.quote && !part.archived_at && !!plan && plan.plan.priceable && !!quantities && marginOk && taxOk && validityOk;

  const create = async () => {
    if (!plan || !quantities) return;
    setBusy(true);
    setError(null);
    try {
      const q = await createQuote(plan.id, { params: { quantities, margin_pct: margin, tax_pct: tax, validity_days: validity } });
      toast(t('ws.quote.created'));
      setSelId(q.id);
      reloadAll();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  const refusal = isApiError(error) && error.code === 'not_priceable' ? ((error.details.reasons as PlanGap[] | undefined) ?? []) : [];

  const missingAsked = !!selId && !quotes.some((q) => q.id === selId);

  return (
    <div className="stack">
      {/* S6: the exact quote a link asked for; a newer quote exists → say so. */}
      {missingAsked && <p className="callout err slim">{t('ver.missing')}</p>}
      {quote && isHistorical(quote.id, quotes[0]?.id) && (
        <HistoricalBanner current={quoteScope(t, fmt, quotes[0]!)} onOpenCurrent={() => setSelId(quotes[0]!.id)} />
      )}
      <StatusBanner status={statusRes.data} error={statusRes.error} onRetry={statusRes.reload} caps={caps} names={names} onChanged={reloadAll} />
      <div className="ws-split">
        <aside className="stack-sm ws-split-side">
          {caps.quote && !part.archived_at && (
            <section className="panel">
              <div className="panel-h">
                <Calculator aria-hidden className="measure-ico" />
                <h3>{t('ws.quote.create')}</h3>
              </div>
              <div className="panel-b stack-sm">
                {!plan ? (
                  <p className="faint small">{t('ws.quote.noPlan')}</p>
                ) : (
                  <>
                    <p className="small">
                      {t('ws.quote.fromPlan', { date: fmt.dateTime(plan.created_at) })}{' '}
                      {plan.staleness.stale && <Chip tone="stale">{t('ws.st.stale')}</Chip>} <ApprovalChip approval={plan.approval} names={names} />
                    </p>
                    {!plan.plan.priceable && (
                      <div className="callout err slim">
                        <span>{t('ws.quote.planNotPriceable')}</span>
                      </div>
                    )}
                    <div className={`ws-f ${quantities ? '' : 'has-error'}`}>
                      <label htmlFor="q-qty">{t('ws.quote.quantities')}</label>
                      <input id="q-qty" className="input sm mono" value={qty} onChange={(e) => setQty(e.target.value)} aria-invalid={!quantities || undefined} />
                      {quantities ? <span className="ws-f-hint">{t('ws.quote.quantitiesHint')}</span> : <span className="ws-f-err">{t('ws.quote.quantitiesErr')}</span>}
                    </div>
                    <div className="ws-row3">
                      <NumField label={t('ws.quote.margin')} unit="%" value={margin} onChange={setMargin} error={marginOk ? undefined : t('ws.v.rangeSimple', { min: 0, max: 200 })} />
                      <NumField label={t('ws.quote.tax')} unit="%" value={tax} onChange={setTax} error={taxOk ? undefined : t('ws.v.rangeSimple', { min: 0, max: 100 })} />
                      <NumField label={t('ws.quote.validity')} unit={t('ws.quote.days')} integer value={validity} onChange={setValidity} error={validityOk ? undefined : t('ws.v.rangeSimple', { min: 1, max: 365 })} />
                    </div>
                    <button type="button" className="btn primary" disabled={!canCreate || busy} onClick={() => void create()}>
                      <Receipt /> {busy ? t('common.working') : t('ws.quote.createBtn')}
                    </button>
                    <ErrorNotice error={error} />
                    <ReasonList reasons={refusal} />
                  </>
                )}
              </div>
            </section>
          )}
          <section className="panel">
            <div className="panel-h">
              <Receipt aria-hidden className="measure-ico" />
              <h3>{t('ws.quote.list')}</h3>
            </div>
            {!quotes.length ? (
              <div className="panel-b">
                <EmptyState compact title={t('ws.quote.none')} body={caps.quote ? t('ws.quote.noneBody') : t('ws.quote.noneViewer')} />
              </div>
            ) : (
              <ul className="ws-list" role="listbox" aria-label={t('ws.quote.list')}>
                {quotes.map((q, i) => (
                  <li key={q.id}>
                    <button type="button" role="option" aria-selected={quote?.id === q.id} className={`ws-list-item ${quote?.id === q.id ? 'active' : ''}`} onClick={() => setSelId(q.id)}>
                      <span className="row">
                        <span className="mono strong">{q.result.tiers.map((x) => x.qty).join(' / ')}</span>
                        {i === 0 && <span className="chip measure">{t('ws.plan.latest')}</span>}
                        {q.staleness.stale && <Chip tone="stale">{t('ws.st.stale')}</Chip>}
                        {q.result.pricing_basis === 'synthetic_demo_rates' ? <ManufacturingEstimateBadge /> : q.result.estimated && <EstimatedBadge />}
                        <span className="grow" />
                        <ApprovalChip approval={q.approval} names={names} />
                      </span>
                      <span className="faint small">
                        {fmt.dateTime(q.created_at)} · {q.result.tiers[0] ? `${money(q.result.tiers[0].unit_inc_tax_fen)} @${q.result.tiers[0].qty}` : '—'}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </aside>
        <section className="ws-split-main">
          {quote ? (
            <QuoteView quote={quote} projectId={project.id} status={statusRes.data} canApprove={caps.quote} names={names} onChanged={reloadAll} />
          ) : (
            <div className="panel">
              <EmptyState title={t('ws.quote.none')} body={caps.quote ? t('ws.quote.noneBody') : t('ws.quote.noneViewer')} icon={<Receipt />} />
            </div>
          )}
        </section>
      </div>
      <AutoSelectionCapture />
    </div>
  );
}

type T = ReturnType<typeof useI18n>['t'];
type Fmt = ReturnType<typeof useI18n>['fmt'];

/** "Quote #1a2b3c4d · 2026-10-08" — the exact quote a reference points at. */
function quoteScope(t: T, fmt: Fmt, quote: Pick<Quote, 'id' | 'created_at'>): string {
  return t('ar.quote.scope', { id: quote.id.slice(0, 8), date: fmt.date(quote.created_at) });
}

/** Icon button adding one field of one tier (the server fills value, currency and formula). */
function FieldAi({ projectId, quote, tier, field, fieldLabel }: { projectId: string; quote: Quote; tier: QuoteTier; field: QuoteFieldKey; fieldLabel: string }) {
  const { t, fmt } = useI18n();
  const { queuePending } = useAssistant();
  const marker = useRef<HTMLSpanElement>(null);
  const label = t('ar.quote.fieldScope', { quote: quoteScope(t, fmt, quote), qty: fmt.number(tier.qty), field: fieldLabel });
  useEffect(() => {
    const row = marker.current?.closest('tr') ?? marker.current?.parentElement;
    if (!row) return;
    const choose = () => queuePending({ input: quoteFieldRef(projectId, quote.id, tier.qty, field), label, target: true, automatic: true });
    row.tabIndex = 0;
    row.addEventListener('click', choose);
    row.addEventListener('focus', choose);
    return () => { row.removeEventListener('click', choose); row.removeEventListener('focus', choose); };
  }, [projectId, quote.id, tier.qty, field, label, queuePending]);
  return <span ref={marker} aria-hidden />;
}

function QuoteView({
  quote,
  projectId,
  status,
  canApprove,
  names,
  onChanged,
}: {
  quote: Quote;
  projectId: string;
  status: PartPipelineStatus | undefined;
  canApprove: boolean;
  names: (id: string) => string;
  onChanged: () => void;
}) {
  const { t, fmt } = useI18n();
  const money = useMoney();
  const num = useNum();
  const bi = useBilingual();
  const label = useLabel();
  const r = quote.result;
  const [open, setOpen] = useState<number | null>(null);
  const synthetic = r.pricing_basis === 'synthetic_demo_rates';
  const formal = !synthetic && r.commercial_status !== 'draft' && !!status?.formal && status.quote?.id === quote.id;
  return (
    <div className="stack-sm" {...aiSourceProps('quote', quote.id, projectId)}>
      <StaleBanner staleness={quote.staleness} what={t('ws.quote.thisQuote')} />
      <section className={`panel ws-quote ${formal ? 'formal' : ''}`}>
        <div className="panel-h">
          <h3>{t('ws.quote.title')}</h3>
          {formal ? (
            <Chip tone="ok" icon={<BadgeCheck aria-hidden />}>
              {t('ws.st.formal')}
            </Chip>
          ) : (
            <Chip tone="pending">{t('ws.quote.draft')}</Chip>
          )}
          {synthetic ? <ManufacturingEstimateBadge /> : r.estimated && <EstimatedBadge title={t('ws.plan.estimatedFields', { fields: r.estimated_fields.join(', ') || '—' })} />}
          <span className="grow" />
          <ApprovalChip approval={quote.approval} names={names} />
          <></>
        </div>
        <div className="panel-b stack-sm">
          {synthetic && <p className="callout warn slim">{t('mfg.estimateNote')}</p>}
          {!formal && <p className="small muted">{t('ws.quote.notFormalNote')}</p>}
          {!quote.approval?.valid && <ApprovalActions partId={quote.part_id} subjectType="quote" subjectId={quote.id} stage="quote" can={canApprove} onDone={onChanged} />}
          <KV
            items={[
              [t('ws.quote.date'), r.quote_date],
              [t('ws.quote.validUntil'), r.valid_until],
              [t('ws.quote.materialValid'), r.material_price_valid_until],
              [t('ws.quote.params'), <span className="mono">{t('ws.quote.paramsVal', { margin: quote.params.margin_pct ?? 15, tax: quote.params.tax_pct ?? 13 })}</span>],
              [t('ws.quote.programming'), <span className="mono">{money(r.programming_fen)}</span>],
              [t('ws.quote.batchFixed'), <span className="mono">{money(r.batch_fixed_fen)}</span>],
              [t('ws.plan.rules'), <span className="mono small">{r.rules_version}</span>],
              [t('ws.plan.createdBy'), `${names(quote.created_by)} · ${fmt.dateTime(quote.created_at)}`],
            ]}
          />
        </div>
      </section>

      {r.alerts.length > 0 && (
        <div className="stack-sm">
          {r.alerts.map((a, i) => (
            <div key={i} className={`callout slim ${a.level === 'blocking' ? 'err' : a.level === 'warning' ? 'warn' : 'info'}`}>
              <Info aria-hidden />
              <span>
                {bi(a)} <code className="error-code">{a.code}</code>
              </span>
            </div>
          ))}
        </div>
      )}

      <section className="panel">
        <div className="panel-h">
          <h3>{t('ws.quote.tiers')}</h3>
          <span className="faint small">{t('ws.quote.tiersHint')}</span>
        </div>
        <div className="table-wrap flat">
          <table className="t dense">
            <thead>
              <tr>
                <th className="exp" />
                <th className="r">{t('ws.quote.qty')}</th>
                <th className="r">{t('ws.quote.unitCost')}</th>
                <th className="r">{t('ws.quote.unitEx')}</th>
                <th className="r">{t('ws.quote.unitInc')}</th>
                <th className="r">{t('ws.quote.orderEx')}</th>
                <th className="r">{t('ws.quote.orderTax')}</th>
                <th className="r">{t('ws.quote.orderInc')}</th>
                <th className="r">{t('ws.quote.lead')}</th>
              </tr>
            </thead>
            <tbody>
              {r.tiers.map((tier, i) => (
                <Fragment key={tier.qty}>
                  <tr className="clickable" tabIndex={0} aria-expanded={open === i} onClick={() => setOpen(open === i ? null : i)} onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && (e.preventDefault(), setOpen(open === i ? null : i))}>
                    <td className="exp">{open === i ? <ChevronDown className="inline-ico" /> : <ChevronRight className="inline-ico" />}</td>
                    <td className="r mono strong">{fmt.number(tier.qty)}</td>
                    <td className="r mono">{money(tier.unit_cost_fen)}</td>
                    <td className="r mono">{money(tier.unit_ex_tax_fen)}</td>
                    <td className="r mono strong">{money(tier.unit_inc_tax_fen)}</td>
                    <td className="r mono">{money(tier.order_ex_tax_fen)}</td>
                    <td className="r mono">{money(tier.order_tax_fen)}</td>
                    <td className="r mono strong">{money(tier.order_inc_tax_fen)}</td>
                    <td className="r mono" title={tier.lead_formula}>
                      {tier.lead_days}
                    </td>
                  </tr>
                  {open === i && (
                    <tr className="sub-row">
                      <td colSpan={9}>
                        <CostBreakdownBar lines={tier.lines} money={money} />
                        <table className="ws-lines">
                          <tbody>
                            {tier.lines.map((l) => (
                              <tr key={l.key}>
                                <td>
                                  {label(l)} {l.estimated && <EstimatedBadge />}
                                </td>
                                <td className="r mono">{money(l.fen)}</td>
                                <td className="ws-line-f">
                                  <Details summary={<span className="mono small ellip">{l.formula}</span>}>
                                    <p className="mono small">{l.formula}</p>
                                    <SourceList sources={l.sources} />
                                  </Details>
                                </td>
                                <td className="ar-col-act">
                                  <FieldAi projectId={projectId} quote={quote} tier={tier} field={l.key} fieldLabel={label(l)} />
                                </td>
                              </tr>
                            ))}
                            <tr>
                              <td className="strong">{t('ws.quote.unitCost')}</td>
                              <td className="r mono strong">{money(tier.unit_cost_fen)}</td>
                              <td />
                              <td className="ar-col-act">
                                <FieldAi projectId={projectId} quote={quote} tier={tier} field="unit_cost_fen" fieldLabel={t('ws.quote.unitCost')} />
                              </td>
                            </tr>
                            <tr>
                              <td>{t('ws.quote.profit')}</td>
                              <td className="r mono">{money(tier.profit_fen)}</td>
                              <td />
                              <td />
                            </tr>
                            <tr>
                              <td>{t('ws.quote.unitTax')}</td>
                              <td className="r mono">{money(tier.tax_fen)}</td>
                              <td />
                              <td />
                            </tr>
                            <tr>
                              <td className="strong">{t('ws.quote.unitInc')}</td>
                              <td className="r mono strong">{money(tier.unit_inc_tax_fen)}</td>
                              <td />
                              <td className="ar-col-act">
                                <FieldAi projectId={projectId} quote={quote} tier={tier} field="unit_inc_tax_fen" fieldLabel={t('ws.quote.unitInc')} />
                              </td>
                            </tr>
                            <tr>
                              <td className="strong">{t('ws.quote.orderInc')}</td>
                              <td className="r mono strong">{money(tier.order_inc_tax_fen)}</td>
                              <td />
                              <td className="ar-col-act">
                                <FieldAi projectId={projectId} quote={quote} tier={tier} field="order_inc_tax_fen" fieldLabel={t('ws.quote.orderInc')} />
                              </td>
                            </tr>
                            <tr>
                              <td>{t('ws.quote.lead')}</td>
                              <td className="r mono">{tier.lead_days}</td>
                              <td className="mono small faint">{tier.lead_formula}</td>
                              <td className="ar-col-act">
                                <FieldAi projectId={projectId} quote={quote} tier={tier} field="lead_days" fieldLabel={t('ws.quote.lead')} />
                              </td>
                            </tr>
                          </tbody>
                        </table>
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
        <div className="panel-b">
          <p className="faint small">{t('ar.quote.hint')}</p>
        </div>
      </section>

      {r.op_costs.length > 0 && (
        <section className="panel">
          <div className="panel-h">
            <h3>{t('ws.quote.opCosts')}</h3>
          </div>
          <div className="table-wrap flat">
            <table className="t dense">
              <thead>
                <tr>
                  <th>{t('ws.plan.op')}</th>
                  <th>{t('ws.plan.name')}</th>
                  <th>{t('ws.plan.machine')}</th>
                  <th className="r">{t('ws.quote.rate')}</th>
                  <th className="r">{t('ws.plan.cycle')}</th>
                  <th className="r">{t('ws.quote.cycleCost')}</th>
                  <th className="r">{t('ws.plan.setup')}</th>
                  <th className="r">{t('ws.quote.setupCost')}</th>
                </tr>
              </thead>
              <tbody>
                {r.op_costs.map((c) => (
                  <tr key={c.op_id} title={c.formula}>
                    <td className="mono">{c.op_id}</td>
                    <td>
                      {label(c)} {c.estimated && <EstimatedBadge />}
                    </td>
                    <td className="mono small">{c.machine_id}</td>
                    <td className="r mono">{money(c.rate_fen_per_h)}/h</td>
                    <td className="r mono">{num(c.cycle_min, 2)}</td>
                    <td className="r mono">{money(c.cycle_fen)}</td>
                    <td className="r mono">{num(c.setup_min, 1)}</td>
                    <td className="r mono">{money(c.setup_fen)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </div>
  );
}

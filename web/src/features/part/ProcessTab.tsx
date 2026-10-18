import { listAssetVersions,listProjectAssets } from '../../api/assets';
import { useAssistantPageContext } from '../assistant/AssistantProvider';
import { exactVersion } from '../assistant/exactVersion';
// Part workspace — Process tab: pipeline status, deterministic process plan
// from the latest revision or a drawing intake, plan review (blank,
// operations, gaps, assumptions) and process approval; drawing-only intakes.

import { ClipboardList,FilePlus2,ListChecks,Plus,ScrollText } from 'lucide-react';
import { Fragment,useState,type ReactNode } from 'react';
import { useSearchParams } from 'react-router-dom';
import { createProcessPlan,getPartStatus,listIntakes,listProcessPlans,listRevisions } from '../../api/endpoints';
import type { Intake,PlanParams,ProcessPlan } from '../../api/types';
import { isHistorical } from '../../app/versionLinks';
import { Chip } from '../../components/ui/Badges';
import { HistoricalBanner } from '../../components/ui/HistoricalBanner';
import { EmptyState,ErrorNotice,ErrorState,SkeletonRows } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import type { PartTabProps } from '../../pages/part/registry';
// [F modules] "Save as process module" entry.
import { SaveAsModuleButton } from '../modules/ModuleView';
import { ApprovalActions,ApprovalChip,Details,EstimatedBadge,KV,SourceList,StaleBanner,useBilingual,useLabel,useMemberNames,useNum } from './common';
import { IntakeDialog,IntakeList } from './IntakePanel';
import { CheckField,NumField,SelectField } from './model/fields';
import { StatusBanner } from './StatusBanner';
import './workspace.css';

export default function ProcessTab({ part, project, caps }: PartTabProps) {
  const { t, fmt } = useI18n();
  const toast = useToast();
  const names = useMemberNames(project.id);
  const statusRes = useResource((signal) => getPartStatus(part.id, { signal }), [part.id]);
  const plansRes = useResource((signal) => listProcessPlans(part.id, { signal }), [part.id]);
  const intakesRes = useResource((signal) => listIntakes(part.id, { signal }), [part.id]);
  const revsRes = useResource((signal) => listRevisions(part.id, { signal }), [part.id]);
  // ?plan=<id> opens that exact plan (S6 version links from runs, assets and results); clicks keep it in the URL.
  const [search, setSearch] = useSearchParams();
  const selId = search.get('plan');
  const setSelId = (id: string) => {
    const next = new URLSearchParams(search);
    next.set('plan', id);
    setSearch(next, { replace: true });
  };
  const [intakeOpen, setIntakeOpen] = useState(false);
  const [source, setSource] = useState<'revision' | 'intake'>('revision');
  const [intakeId, setIntakeId] = useState('');
  const [params, setParams] = useState<PlanParams>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const reloadAll = () => {
    statusRes.reload();
    plansRes.reload();
    intakesRes.reload();
  };

  const shownPlan = selId ? plansRes.data?.find((p) => p.id === selId) : plansRes.data?.[0];
  const planAsset = useResource(async (signal) => {
    const assets = await listProjectAssets(project.id, { type: 'process_plan', part_id: part.id }, { signal });
    const versions = await Promise.all(assets.map((asset) => listAssetVersions(asset.id, { signal })));
    return versions.flat().find((v) => v.target?.kind === 'process_plan' && v.target.id === shownPlan?.id);
  }, [project.id, part.id, shownPlan?.id], { enabled: !!shownPlan });
  useAssistantPageContext({ projectId: project.id, partId: part.id, label: `${part.number} · ${t('part.tab.process')}`,
    object: { type: 'process_plan', id: part.id, version: shownPlan?.id ?? null },
    target: shownPlan && planAsset.data?.target?.id === shownPlan.id ? { kind: 'asset_version', object_id: planAsset.data.id, project_id: project.id } : null });

  if (plansRes.error && !plansRes.data) return <ErrorState error={plansRes.error} onRetry={plansRes.reload} />;
  if (!plansRes.data || !intakesRes.data || !revsRes.data) return <SkeletonRows rows={5} />;

  const plans = plansRes.data;
  const intakes = intakesRes.data;
  const latest = revsRes.data.find((r) => r.status === 'ready') ?? null;
  const plan = exactVersion(plans, selId);
  const effectiveSource = source === 'revision' && !latest && intakes.length ? 'intake' : source;
  const chosenIntake = intakes.find((i) => i.id === intakeId) ?? intakes[0] ?? null;
  const canCreate = caps.process && !part.archived_at && (effectiveSource === 'revision' ? !!latest && !latest.mesh_only : !!chosenIntake);

  const create = async (override?: Intake) => {
    setBusy(true);
    setError(null);
    try {
      const body = override
        ? { intake_id: override.id, params }
        : effectiveSource === 'revision'
          ? { model_revision_id: latest!.id, params }
          : { intake_id: chosenIntake!.id, params };
      const p = await createProcessPlan(part.id, body);
      toast(t('ws.plan.created'));
      setSelId(p.id);
      reloadAll();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  const missingAsked = !!selId && !plans.some((p) => p.id === selId);

  return (
    <div className="stack">
      {/* S6: the exact plan a link asked for; a newer plan exists → say so. */}
      {missingAsked && <p className="callout err slim">{t('ver.missing')}</p>}
      {plan && isHistorical(plan.id, plans[0]?.id) && (
        <HistoricalBanner current={`${planSourceLabel(plans[0]!, revsRes.data, t)} · ${fmt.dateTime(plans[0]!.created_at)}`} onOpenCurrent={() => setSelId(plans[0]!.id)} />
      )}
      <StatusBanner status={statusRes.data} error={statusRes.error} onRetry={statusRes.reload} caps={caps} names={names} onChanged={reloadAll} />
      <div className="ws-split">
        <aside className="stack-sm ws-split-side">
          {caps.process && !part.archived_at && (
            <section className="panel">
              <div className="panel-h">
                <FilePlus2 aria-hidden className="measure-ico" />
                <h3>{t('ws.plan.create')}</h3>
              </div>
              <div className="panel-b stack-sm">
                <div className="ws-ai-mode">
                  <label className="check">
                    <input type="radio" name="plan-src" checked={effectiveSource === 'revision'} disabled={!latest} onChange={() => setSource('revision')} />
                    {latest ? t('ws.plan.fromRevision', { rev: latest.label }) : t('ws.plan.noRevision')}
                  </label>
                  <label className="check">
                    <input type="radio" name="plan-src" checked={effectiveSource === 'intake'} disabled={!intakes.length} onChange={() => setSource('intake')} />
                    {t('ws.plan.fromIntake')}
                  </label>
                </div>
                {latest?.mesh_only && effectiveSource === 'revision' && <p className="small warn-text">{t('ws.plan.meshOnly')}</p>}
                {effectiveSource === 'intake' && intakes.length > 0 && (
                  <SelectField<string>
                    label={t('ws.plan.intake')}
                    value={chosenIntake?.id ?? ''}
                    options={intakes.map((i, n) => ({ value: i.id, label: `${n === 0 ? `${t('ws.intake.latest')} · ` : ''}${fmt.dateTime(i.created_at)} · ${i.summary.bbox_mm.join('×')}` }))}
                    onChange={setIntakeId}
                  />
                )}
                <Details summary={t('ws.plan.params')}>
                  <div className="stack-sm">
                    <div className="ws-row2">
                      <NumField label={t('ws.plan.allowance')} unit="mm" value={params.allowance_mm ?? null} placeholder={t('ws.plan.default')} onChange={(v) => setParams((p) => ({ ...p, allowance_mm: v }))} onClear={() => setParams(({ allowance_mm: _a, ...p }) => p)} />
                      <NumField label={t('ws.plan.increment')} unit="mm" value={params.stock_increment_mm ?? null} placeholder={t('ws.plan.default')} onChange={(v) => setParams((p) => ({ ...p, stock_increment_mm: v }))} onClear={() => setParams(({ stock_increment_mm: _s, ...p }) => p)} />
                    </div>
                    <SelectField<'min_bbox' | 'max_bbox'>
                      label={t('ws.plan.throughRule')}
                      value={params.through_hole_depth_rule ?? 'min_bbox'}
                      options={[
                        { value: 'min_bbox', label: t('ws.plan.minBbox') },
                        { value: 'max_bbox', label: t('ws.plan.maxBbox') },
                      ]}
                      onChange={(v) => setParams((p) => ({ ...p, through_hole_depth_rule: v }))}
                    />
                    <SelectField<string>
                      label={t('ws.plan.surface')}
                      value={params.surface_treatment ?? ''}
                      options={[
                        { value: '', label: t('ws.plan.surface.none') },
                        { value: 'anodize', label: t('ws.plan.surface.anodize') },
                        { value: 'black_oxide', label: t('ws.plan.surface.black_oxide') },
                        { value: 'passivate', label: t('ws.plan.surface.passivate') },
                      ]}
                      onChange={(v) => setParams((p) => ({ ...p, surface_treatment: (v || null) as PlanParams['surface_treatment'] }))}
                    />
                    <CheckField label={t('ws.plan.blockUnsupported')} checked={!!params.block_on_unsupported} onChange={(v) => setParams((p) => ({ ...p, block_on_unsupported: v }))} />
                  </div>
                </Details>
                <button type="button" className="btn primary" disabled={!canCreate || busy} onClick={() => void create()}>
                  <ClipboardList /> {busy ? t('common.working') : t('ws.plan.createBtn')}
                </button>
                <ErrorNotice error={error} />
              </div>
            </section>
          )}

          <section className="panel">
            <div className="panel-h">
              <ListChecks aria-hidden className="measure-ico" />
              <h3>{t('ws.plan.list')}</h3>
            </div>
            {!plans.length ? (
              <div className="panel-b">
                <EmptyState compact title={t('ws.plan.none')} body={caps.process ? t('ws.plan.noneBody') : t('ws.plan.noneViewer')} />
              </div>
            ) : (
              <ul className="ws-list" role="listbox" aria-label={t('ws.plan.list')}>
                {plans.map((p, i) => (
                  <li key={p.id}>
                    <button type="button" role="option" aria-selected={plan?.id === p.id} className={`ws-list-item ${plan?.id === p.id ? 'active' : ''}`} onClick={() => setSelId(p.id)}>
                      <span className="row">
                        <span className="strong">{planSourceLabel(p, revsRes.data!, t)}</span>
                        {i === 0 && <span className="chip measure">{t('ws.plan.latest')}</span>}
                        {p.staleness.stale && <Chip tone="stale">{t('ws.st.stale')}</Chip>}
                        {p.plan.estimated && <EstimatedBadge />}
                        <span className="grow" />
                        <ApprovalChip approval={p.approval} names={names} />
                      </span>
                      <span className="faint small">
                        {fmt.dateTime(p.created_at)} · {names(p.created_by)} · {p.rules_version}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section className="panel">
            <div className="panel-h">
              <ScrollText aria-hidden className="measure-ico" />
              <h3>{t('ws.intake.title')}</h3>
              <span className="grow" />
              {caps.design && !part.archived_at && (
                <button type="button" className="btn sm" onClick={() => setIntakeOpen(true)}>
                  <Plus /> {t('ws.intake.new')}
                </button>
              )}
            </div>
            <div className="panel-b">
              <IntakeList intakes={intakes} latestId={intakes[0]?.id ?? null} caps={caps} names={names} onChanged={reloadAll} onPlan={caps.process && !part.archived_at ? (it) => void create(it) : undefined} />
            </div>
          </section>
        </aside>

        <section className="ws-split-main">
          {plan ? (
            <PlanView
              plan={plan}
              canApprove={caps.process}
              names={names}
              onChanged={reloadAll}
              sourceLabel={planSourceLabel(plan, revsRes.data, t)}
              moduleAction={
                caps.process && !part.archived_at ? (
                  <SaveAsModuleButton projectId={project.id} plan={plan} label={planSourceLabel(plan, revsRes.data, t)} labels={plan.plan.estimated ? ['estimated'] : []} />
                ) : undefined
              }
            />
          ) : (
            <div className="panel">
              <EmptyState title={t('ws.plan.none')} body={caps.process ? t('ws.plan.noneBody') : t('ws.plan.noneViewer')} icon={<ClipboardList />} />
            </div>
          )}
        </section>
      </div>
      {intakeOpen && (
        <IntakeDialog
          part={part}
          project={project}
          onClose={() => setIntakeOpen(false)}
          onCreated={() => {
            setIntakeOpen(false);
            setSource('intake');
            setIntakeId('');
            reloadAll();
          }}
        />
      )}
    </div>
  );
}

function planSourceLabel(p: ProcessPlan, revs: { id: string; label: string }[], t: ReturnType<typeof useI18n>['t']): string {
  if (p.model_revision_id) return revs.find((r) => r.id === p.model_revision_id)?.label ?? t('ws.plan.fromModel');
  return t('ws.plan.fromIntakeShort');
}

function PlanView({
  plan,
  canApprove,
  names,
  onChanged,
  sourceLabel,
  moduleAction,
}: {
  plan: ProcessPlan;
  canApprove: boolean;
  names: (id: string) => string;
  onChanged: () => void;
  sourceLabel: string;
  /** "Save as process module" (process role). */
  moduleAction?: ReactNode;
}) {
  const { t, fmt } = useI18n();
  const bi = useBilingual();
  const label = useLabel();
  const num = useNum();
  const body = plan.plan;
  const blocking = body.gaps.filter((g) => g.severity === 'blocking');
  const warnings = body.gaps.filter((g) => g.severity !== 'blocking');
  const [open, setOpen] = useState<string | null>(null);
  return (
    <div className="stack-sm">
      <StaleBanner staleness={plan.staleness} what={t('ws.plan.thisPlan')} />
      <section className="panel">
        <div className="panel-h">
          <h3>
            {t('ws.plan.title')} · <span className="mono">{sourceLabel}</span>
          </h3>
          {body.estimated && <EstimatedBadge title={t('ws.plan.estimatedFields', { fields: body.estimated_fields.join(', ') || '—' })} />}
          {body.priceable ? <Chip tone="ok">{t('ws.plan.priceable')}</Chip> : <Chip tone="err">{t('ws.plan.notPriceable')}</Chip>}
          <span className="grow" />
          <ApprovalChip approval={plan.approval} names={names} />
          {moduleAction}
        </div>
        <div className="panel-b stack-sm">
          {!plan.approval?.valid && <ApprovalActions partId={plan.part_id} subjectType="process_plan" subjectId={plan.id} stage="process" can={canApprove} onDone={onChanged} />}
          <KV
            items={[
              [t('ws.plan.material'), <span>{label({ label_zh: body.material_name_zh, label_en: body.material_name_en }) || body.material_input} <span className="faint mono">{body.material_id ?? ''}</span></span>],
              [t('ws.plan.bbox'), <span className="mono">{plan.summary.bbox_mm.map((x) => num(x, 2)).join(' × ')} mm</span>],
              [t('ws.plan.programming'), <span className="mono">{num(body.programming_min, 1)} min</span>],
              [t('ws.plan.totalSetup'), <span className="mono">{num(body.total_setup_min, 1)} min</span>],
              [t('ws.plan.totalCycle'), <span className="mono">{num(body.total_cycle_min, 2)} min</span>],
              [t('ws.plan.rules'), <span className="mono small">{body.rules_version}</span>],
              [t('ws.plan.createdBy'), `${names(plan.created_by)} · ${fmt.dateTime(plan.created_at)}`],
            ]}
          />
          {body.blank && (
            <Details summary={<span>{t('ws.plan.blank')}: <span className="mono">{body.blank.dims_mm.map((x) => num(x, 1)).join(' × ')} mm</span>{body.blank.mass_kg !== null ? <span className="mono"> · {num(body.blank.mass_kg, 3)} kg</span> : null}{body.blank.estimated ? <> <EstimatedBadge /></> : null}</span>}>
              <p className="mono small">{body.blank.formula}</p>
              <SourceList sources={body.blank.sources} />
            </Details>
          )}
        </div>
      </section>

      {blocking.length > 0 && (
        <div className="callout err">
          <div>
            <strong>{t('ws.plan.blocking')}</strong>
            <ul className="ws-reasons">
              {blocking.map((g, i) => (
                <li key={i}>
                  {bi(g)} {g.rule_id && <code className="error-code">{g.rule_id}</code>}
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}
      {warnings.length > 0 && (
        <div className="callout warn">
          <div>
            <strong>{t('ws.plan.gaps')}</strong>
            <ul className="ws-reasons">
              {warnings.map((g, i) => (
                <li key={i}>
                  {bi(g)} {g.rule_id && <code className="error-code">{g.rule_id}</code>}
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}

      <section className="panel">
        <div className="panel-h">
          <h3>{t('ws.plan.ops')}</h3>
          <span className="faint small">{t('ws.plan.opsHint')}</span>
        </div>
        <div className="table-wrap flat">
          <table className="t dense">
            <thead>
              <tr>
                <th>{t('ws.plan.op')}</th>
                <th>{t('ws.plan.name')}</th>
                <th>{t('ws.plan.machine')}</th>
                <th className="r">{t('ws.plan.setup')}</th>
                <th className="r">{t('ws.plan.cycle')}</th>
                <th>{t('ws.plan.formula')}</th>
              </tr>
            </thead>
            <tbody>
              {body.operations.map((op) => (
                <Fragment key={op.op_id}>
                  <tr className="clickable" tabIndex={0} onClick={() => setOpen(open === op.op_id ? null : op.op_id)} onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && (e.preventDefault(), setOpen(open === op.op_id ? null : op.op_id))} aria-expanded={open === op.op_id}>
                    <td className="mono">{op.op_id}</td>
                    <td>
                      {label(op)} {op.estimated && <EstimatedBadge />}
                    </td>
                    <td className="mono small">{op.machine_id ?? (op.subcontract_id ? `${t('ws.plan.subcontract')}: ${op.subcontract_id}` : '—')}</td>
                    <td className="r mono">{num(op.setup_min, 1)}</td>
                    <td className="r mono">{num(op.cycle_min, 2)}</td>
                    <td className="mono small ellip ws-formula">{op.formula}</td>
                  </tr>
                  {open === op.op_id && (
                    <tr className="sub-row">
                      <td colSpan={6}>
                        <div className="stack-sm">
                          <p className="mono small">{op.formula}</p>
                          {Object.keys(op.inputs).length > 0 && (
                            <p className="small">
                              <span className="faint">{t('ws.plan.inputs')}: </span>
                              <span className="mono">
                                {Object.entries(op.inputs)
                                  .map(([k, v]) => `${k}=${v}`)
                                  .join(' · ')}
                              </span>
                            </p>
                          )}
                          {op.lead_days > 0 && <p className="small">{t('ws.plan.leadDays', { n: op.lead_days })}</p>}
                          <SourceList sources={op.sources} />
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
        <div className="panel-b">
          <Details summary={t('ws.plan.programmingFormula')}>
            <p className="mono small">{body.programming_formula}</p>
            <SourceList sources={body.programming_sources} />
          </Details>
        </div>
      </section>

      {body.assumptions.length > 0 && (
        <section className="panel">
          <div className="panel-h">
            <h3>{t('ws.plan.assumptions')}</h3>
          </div>
          <ul className="ws-reasons panel-b">
            {body.assumptions.map((a, i) => (
              <li key={i}>
                {bi(a)} {a.rule_id && <code className="error-code">{a.rule_id}</code>}
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

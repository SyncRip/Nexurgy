import { Check, ExternalLink, TriangleAlert } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { applyDraftingPlan, type AiDraftingModifyResult, type AiOperation } from '../../api/assistant';
import { ErrorNotice } from '../../components/ui/States';
import { useI18n } from '../../i18n/I18nProvider';
import { useAssistantSession } from './AssistantProvider';
import { AS_ERRORS, SimChip, tk } from './common';
import { planApplyBody, planConfirmationKey, planOutcome } from './planApply';
import { draftingCommandPreview } from './draftingPlanPreview';
import { PlanIdentity } from './PlanIdentity';

export function DraftingPlanCard({ op, plan, archived }: { op: AiOperation; plan: AiDraftingModifyResult; archived: boolean }) {
  const { t } = useI18n();
  const { store, bumpList, keepDockOnLeave } = useAssistantSession();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [confirmedPlan, setConfirmedPlan] = useState<string | null>(null);
  const identity = planConfirmationKey(plan);
  const confirmed = confirmedPlan === identity;
  useEffect(() => { setConfirmedPlan(null); setError(null); }, [identity]);
  const body = planApplyBody(plan, confirmed);
  const invalid = planApplyBody(plan, true) === null;
  const state = planOutcome(op) ?? 'plan_ready';
  const draft = plan.plan;
  const removals = [...new Set([...plan.removals, ...draft.removed_entity_ids])];
  const baseHref = `/drafting/${encodeURIComponent(plan.document_id)}?version=${encodeURIComponent(plan.base_version_id)}`;

  const apply = async () => {
    if (!body || archived || busy) return;
    setBusy(true); setError(null);
    try {
      await applyDraftingPlan(op.id, body);
      await store.refresh(op.conversation_id);
      bumpList();
    } catch (e) { setError(e); }
    finally { setBusy(false); }
  };

  return <section className={`as-wfe as-drafting-plan ${state}`}>
    <div className="as-wfe-head">
      {state !== 'plan_ready' && <Check aria-hidden />}
      <b>{t(`core.plan.${state}`)}</b>
      <Link className="linkish small" to={baseHref} onClick={keepDockOnLeave}><ExternalLink className="inline-ico" />{t('core.plan.viewBase', { version: draft.base_version_no })}</Link>
      {op.simulated && <SimChip />}
    </div>
    {plan.summary && <p className="as-wfe-summary">{plan.summary}</p>}
    {state === 'plan_ready' && <p className="as-op-note">{t('core.plan.notSaved')}</p>}
    {plan.changes.length > 0 && <ul className="as-wfe-list">{plan.changes.map((change, i) => <li key={i}>{change}</li>)}</ul>}
    <details className="as-plan-identity"><summary>{t('core.plan.objects')}</summary>
    {!!draft.selection && <p className="small as-plan-ids">{t('core.plan.selection')}: {draft.selection.ids.join(', ')}{draft.selection.sheet_id ? ` · ${draft.selection.sheet_id}` : ''}</p>}
    {draft.affected_entity_ids.length > 0 && <p className="small as-plan-ids">{t('core.plan.affected')}: {draft.affected_entity_ids.join(', ')}</p>}
    </details>
    {removals.length > 0 && <p className="small as-plan-ids warn-text">{t('core.plan.removals')}: {removals.join(', ')}</p>}
    <ol className="as-wfe-list">{draft.commands.flatMap((command) => draftingCommandPreview(t, command)).map((line, i) => <li key={i}>{line}</li>)}</ol>
    <details className="as-plan-commands">
      <summary>{t('core.plan.commands', { n: draft.commands.length })}</summary>
      <ol>{draft.commands.map((command, i) => <li key={i}><b>{tk(t, `core.command.${command.op}`, command.op)}</b><pre>{JSON.stringify(command, null, 2)}</pre></li>)}</ol>
    </details>
    {plan.warnings.length > 0 && <ul className="as-wfe-warn">{plan.warnings.map((warning, i) => <li key={i}>
      <TriangleAlert aria-hidden />{warning.code === 'dimension_text_override'
        ? t('core.plan.dimensionOverride', { id: String(warning.entity_id ?? ''), measured: String(warning.measured ?? '—'), displayed: String(warning.displayed ?? '—') })
        : typeof warning.message === 'string' ? warning.message : JSON.stringify(warning)}
    </li>)}</ul>}
    {plan.questions.length > 0 && <div className="callout warn slim"><b>{t('core.plan.questions')}</b><ul>{plan.questions.map((question, i) => <li key={i}>{question}</li>)}</ul></div>}
    <PlanIdentity hash={plan.plan_hash} head={plan.base_version_id} />
    {invalid && <p className="callout err slim">{t('core.plan.invalid')}</p>}
    {state === 'plan_ready' && !plan.application && <div className="as-wfe-acts">
      {removals.length > 0 && <label className="as-wfe-warn row"><TriangleAlert aria-hidden />
        <input type="checkbox" checked={confirmed} onChange={(event) => setConfirmedPlan(event.target.checked ? identity : null)} disabled={busy || archived || invalid} />
        <span>{t('core.plan.confirmRemovals', { n: removals.length })}</span>
      </label>}
      <button type="button" className="btn sm primary" disabled={busy || archived || !body || draft.commands.length === 0} onClick={() => void apply()}><Check />{t(busy ? 'core.plan.applying' : 'core.plan.apply')}</button>
    </div>}
    <ErrorNotice error={error} overrides={{ ...AS_ERRORS, version_conflict: 'as.err.head_changed' }} />
  </section>;
}

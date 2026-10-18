// AI workspace cards of the W8 assembly operations.
// - assembly_create: every chain step (spec → assembly → drawings → delivery) with its recorded value and status,
//   the assumptions the products rest on (author AI / rule, never "confirmed") and the open questions with the way
//   forward (answer them on the spec page). A partial or failed chain is shown as such.
// - assembly_modify: the planned W4 edit ops on an exact base revision with their previewed impact; applying writes a
//   new revision, never automatically, and a plan that removes anything needs an explicit confirmation.

import { Check, ExternalLink, TriangleAlert } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { applyAssemblyPlan, type AiAssemblyChain, type AiAssemblyModifyResult, type AiOperation } from '../../api/assistant';
import { assemblyRevisionPath } from '../../app/versionLinks';
import { Chip } from '../../components/ui/Badges';
import { ErrorNotice } from '../../components/ui/States';
import { useI18n } from '../../i18n/I18nProvider';
import { useAssistantSession } from '../assistant/AssistantProvider';
import { AS_ERRORS, SimChip } from '../assistant/common';
import { planApplyBody, planConfirmationKey, planOutcome } from '../assistant/planApply';
import { PlanIdentity } from '../assistant/PlanIdentity';
import { AssumptionList } from '../turbo/Turbo';
import { nodeErrorText, nodeStatusLabel } from '../workflow/labels';
import { ChainValueView } from './ChainValue';

/** ``onContinue``: "Continue remaining work" on a partial assembly step (generation supplement B; continues the request's run). */
export function AssemblyChainView({ chain, onContinue }: { chain: AiAssemblyChain; onContinue?: () => Promise<unknown> }) {
  const { t } = useI18n();
  return (
    <div className="orch-chain" aria-label={t('orch.chain.title')}>
      {chain.steps.map((s) => (
        <section key={s.node_id} className="stack-sm">
          <div className="row wrap">
            <span className="strong small">{t(`orch.step.${s.node_id}` as 'orch.step.spec')}</span>
            <Chip tone={s.status === 'succeeded' ? 'ok' : s.status === 'failed' ? 'err' : s.status === 'skipped' ? 'stale' : 'pending'}>
              {nodeStatusLabel(t, s.status)}
            </Chip>
            {s.error?.code && <span className="small err-text">{nodeErrorText(t, s.error.code, s.error.message)}</span>}
          </div>
          {s.value && <ChainValueView value={s.value} onContinue={s.node_id === 'assembly' ? onContinue : undefined} />}
        </section>
      ))}
      {chain.questions.length > 0 && (
        <section className="orch-questions">
          <span className="small strong">{t('orch.questions', { n: chain.questions.length })}</span>
          <ul>
            {chain.questions.map((q, i) => (
              <li key={i}>
                {q.id && <span className="mono">{q.id} </span>}
                {q.definition && <span className="mono">{q.definition} · </span>}
                {q.question}
              </li>
            ))}
          </ul>
          <p className="faint small">{t('orch.questions.next')}</p>
        </section>
      )}
      <AssumptionList items={chain.assumptions} title={t('orch.assumptions.title')} />
    </div>
  );
}

function opLine(t: ReturnType<typeof useI18n>['t'], o: AiAssemblyModifyResult['ops'][number]): string {
  const s = (v: unknown) => (v === undefined || v === null ? '' : typeof v === 'string' ? v : JSON.stringify(v));
  switch (o.op) {
    case 'move_instance':
      return t('orch.op.move', { instance: s(o.instance), delta: s((o.delta as { translate?: unknown } | undefined)?.translate ?? o.placement) });
    case 'remove_instance':
      return t('orch.op.remove', { instance: s(o.instance) });
    case 'set_quantity':
      return t('orch.op.quantity', { n: s(o.quantity) });
    case 'replace_definition':
      return t('orch.op.replace', { instance: s(o.instance) });
    case 'update_definition':
      return t('orch.op.update', { def: s(o.part_id ?? o.assembly_id) });
    default:
      return `${o.op} ${s(o.instance ?? o.mate_id ?? '')}`.trim();
  }
}

export function AssemblyModifyCard({ op, plan, archived }: { op: AiOperation; plan: AiAssemblyModifyResult; archived: boolean }) {
  const { t } = useI18n();
  const { store, bumpList } = useAssistantSession();
  const [busy, setBusy] = useState(false);
  const [confirmedPlan, setConfirmedPlan] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const app = plan.application;
  const undone = planOutcome(op) === 'undone';
  const removals = plan.removals.length > 0;
  const identity = planConfirmationKey(plan);
  const confirm = confirmedPlan === identity;
  useEffect(() => { setConfirmedPlan(null); setError(null); }, [identity]);
  const applyBody = planApplyBody(plan, removals && confirm);
  const invalid = planApplyBody(plan, true) === null;
  const apply = async () => {
    if (!applyBody) return;
    setBusy(true);
    setError(null);
    try {
      await applyAssemblyPlan(op.id, applyBody);
      store.reload();
      bumpList();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className={`as-wfe ${undone ? 'undone' : app ? 'applied' : plan.ops.length ? 'pending' : 'empty'}`}>
      <div className="as-wfe-head">
        {app && <Check aria-hidden />}
        <b>{undone ? t('core.plan.undone') : app ? t('orch.plan.applied', { rev: app.rev_no }) : plan.ops.length ? t('orch.plan.pending', { n: plan.ops.length }) : t('orch.plan.empty')}</b>
        <span className="faint small">
          · {plan.assembly_number} A{plan.base_rev_no}
        </span>
        {op.simulated && <SimChip />}
      </div>
      {plan.summary && <p className="as-wfe-summary">{plan.summary}</p>}
      {plan.ops.length > 0 && (
        <ol className="as-wfe-list">
          {plan.ops.map((o, i) => (
            <li key={i}>{opLine(t, o)}</li>
          ))}
        </ol>
      )}
      {plan.questions.length > 0 && (
        <ul className="as-wfe-warn">
          {plan.questions.map((q, i) => (
            <li key={i}>{q}</li>
          ))}
        </ul>
      )}
      {plan.impact && (
        <p className="faint small">
          {t('orch.plan.impact', {
            instances: plan.impact.affected_instances.length,
            bom: plan.impact.bom_changes.length,
            stale: plan.impact.will_be_stale.length,
          })}
        </p>
      )}
      {removals && !app && (
        <label className="as-wfe-warn row">
          <TriangleAlert aria-hidden />
          <input type="checkbox" checked={confirm} onChange={(e) => setConfirmedPlan(e.target.checked ? identity : null)} disabled={busy || archived || invalid} />
          <span>{t('orch.plan.confirmRemovals', { n: plan.removals.length })}</span>
        </label>
      )}
      <PlanIdentity hash={plan.plan_hash} head={plan.base_revision_id} />
      {invalid && !app && <p className="callout err slim">{t('core.plan.invalid')}</p>}
      <div className="as-wfe-acts">
        {!app && plan.ops.length > 0 && (
          <button type="button" className="btn sm primary" onClick={() => void apply()} disabled={busy || archived || !applyBody}>
            <Check /> {busy ? t('orch.plan.applying') : t('orch.plan.apply')}
          </button>
        )}
        {app && (
          <Link to={assemblyRevisionPath(plan.assembly_id, app.revision_id)} className="btn sm">
            <ExternalLink /> {t('orch.plan.open', { rev: app.rev_no })}
          </Link>
        )}
        <span className="faint small">{t('orch.plan.note')}</span>
      </div>
      <ErrorNotice error={error} overrides={{ ...AS_ERRORS, revision_conflict: 'as.err.head_changed' }} />
    </div>
  );
}

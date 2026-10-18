// Result of a workflow_edit operation (R1.2 prototype "Edit workflow with AI"): the
// plan as a list of changes, then Apply / undo. Nothing changes before the
// person applies it; historical plans retain their explicit auto-apply preference. Applying writes the
// part workflow's next version, so only the NEXT run uses it — past runs never
// change (G2), and the AI never approves anything (G3).
//
// AI work panel: the same plan opens in the panel ("view changes in panel"); a plan about one node names it, and a
// plan asked from a run says it changes the workflow the next run uses — the run itself stays as it ran.

import { Check,Crosshair,PanelRight,PlayCircle,RotateCcw,TriangleAlert } from 'lucide-react';
import { useEffect,useRef,useState } from 'react';
import { applyAiOperation,undoAiOperation,type AiOperation,type AiWorkflowEditResult,type AiWorkflowEditWarning } from '../../api/assistant';
import type { WorkflowGraph } from '../../api/types';
import type { MessageKey } from '../../i18n/en';
import { useI18n,type TFunction } from '../../i18n/I18nProvider';
import { emitWorkflowChanged } from '../workflow/events';
import { nodeDisplayName } from '../workflow/labels';
import { useAssistantSession } from './AssistantProvider';
import { AsError,SimChip } from './common';
import { useWorkPanel } from './panel/WorkPanelContext';
import { holdsAutoApply,planLines,shownWarnings,type PlanLine } from './workflowEdit';
import { useWorkflowAi } from './WorkflowGovernance';
import { planApplyBody } from './planApply';
import { PlanIdentity } from './PlanIdentity';

function lineText(t: TFunction, l: PlanLine): string {
  const name = (id: string, type: string, label: string | null) => `${nodeDisplayName(t, { type, label })} (${id})`;
  switch (l.kind) {
    case 'add_node':
      return t('as.wfe.addNode', { name: name(l.id, l.type, l.label) });
    case 'remove_node':
      return t('as.wfe.removeNode', { name: name(l.id, l.type, l.label) });
    case 'params':
      return t('as.wfe.params', { name: name(l.id, l.type, l.label), changes: l.changes.map((c) => `${c.key}: ${c.before} → ${c.after}`).join('; ') });
    case 'rename':
      return t('as.wfe.rename', { id: l.id, before: l.before ?? '—', after: l.after ?? '—' });
    case 'add_edge':
      return t('as.wfe.addEdge', { from: l.from, to: l.to });
    case 'remove_edge':
      return t('as.wfe.removeEdge', { from: l.from, to: l.to });
  }
}

/** A plan warning in the person's language (workflow_edit and workflow_create plans); unknown codes keep the server text. */
/** `fromRun`: the plan was asked on a run, so a moved-on version is the one that run used (not a reference's). */
export function warningText(t: TFunction, w: AiWorkflowEditWarning, fromRun = false): string {
  if (w.code === 'run_unchanged') return t('fc.warn.run_unchanged');
  if (w.code === 'workflow_moved_on' && fromRun) return t('fc.warn.movedOnRun', { v: w.pinned_version ?? '', base: w.base_version ?? '' });
  if (w.code === 'focus_node_missing') return t('fc.warn.focus_node_missing');
  const known = ['missing_param', 'removes_review', 'workflow_moved_on', 'private_context'];
  return known.includes(w.code) ? t(`as.wfe.warn.${w.code}` as MessageKey, { node: w.node_id ?? '', param: w.param ?? '', v: w.pinned_version ?? '' }) : w.message;
}

/** The node a plan is about, by its name on the canvas (the planned graph first: a renamed node reads as renamed). */
function focusName(t: TFunction, id: string, ...graphs: WorkflowGraph[]): string {
  for (const g of graphs) {
    const n = g.nodes.find((x) => x.id === id);
    if (n) return nodeDisplayName(t, n);
  }
  return id;
}

export function WorkflowEditCard({ op, plan, archived }: { op: AiOperation; plan: AiWorkflowEditResult; archived: boolean }) {
  const { t, fmt } = useI18n();
  const { store, bumpList } = useAssistantSession();
  const panel = useWorkPanel();
  const wfAi = useWorkflowAi();
  const [busy, setBusy] = useState<null | 'apply' | 'undo'>(null);
  const [error, setError] = useState<unknown>(null);
  const lines = planLines(plan);
  const fromRun = plan.from_run ?? null;
  // A plan asked from a run says "this run stays unchanged" in its own line; the warning would repeat it.
  const warnings = shownWarnings(plan.warnings).filter((w) => !(fromRun && w.code === 'run_unchanged'));
  const app = plan.application;
  const empty = plan.diff.empty || lines.length === 0;
  const applyBody = planApplyBody(plan);

  const act = async (kind: 'apply' | 'undo') => {
    if (kind === 'apply' && !applyBody) return;
    setBusy(kind);
    setError(null);
    try {
      const res = kind === 'apply' ? await applyAiOperation(op.id, applyBody!) : await undoAiOperation(op.id);
      emitWorkflowChanged(res.workflow.id, res.workflow.version);
      store.reload(); // the operation and the re-pinned workflow reference
      bumpList();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(null);
    }
  };

  // Auto-apply (the person's switch): only a plan that became ready while this card was on screen, never one
  // with warnings that need a person's eyes, and never twice.
  const sawPending = useRef(op.status !== 'succeeded');
  const autoDone = useRef(false);
  useEffect(() => {
    if (plan.plan_hash !== undefined || !applyBody || !wfAi?.autoApply || !sawPending.current || autoDone.current || archived) return;
    if (app || empty || holdsAutoApply(plan.warnings)) return;
    autoDone.current = true;
    void act('apply');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wfAi?.autoApply, app, empty, archived, plan.plan_hash]);

  const head = app?.state === 'applied'
    ? t('as.wfe.applied', { from: app.version_before, to: app.version_after })
    : app?.state === 'undone'
      ? t('as.wfe.undone', { v: app.undo_version ?? '' })
      : empty
        ? t('as.wfe.empty')
        : t('as.wfe.pending', { n: lines.length });

  return (
    <div className={`as-wfe ${app?.state ?? (empty ? 'empty' : 'pending')}`}>
      <div className="as-wfe-head">
        {app?.state === 'applied' && <Check aria-hidden />}
        <b>{head}</b>
        <span className="faint small">· {plan.workflow_name}</span>
        {op.simulated && <SimChip />}
      </div>
      {plan.focus_node_id && (
        <p className="as-wfe-line">
          <Crosshair aria-hidden /> {t('fc.edit.focus', { name: focusName(t, plan.focus_node_id, plan.graph_after, plan.graph_before) })}
        </p>
      )}
      {fromRun && (
        <p className="as-wfe-line is-run">
          <PlayCircle aria-hidden /> {t('fc.edit.fromRun', { v: plan.base_version })}
        </p>
      )}
      {plan.summary && <p className="as-wfe-summary">{plan.summary}</p>}
      {lines.length > 0 && (
        <ol className="as-wfe-list">
          {lines.map((l, i) => (
            <li key={i}>{lineText(t, l)}</li>
          ))}
        </ol>
      )}
      {warnings.length > 0 && (
        <ul className="as-wfe-warn">
          {warnings.map((w, i) => (
            <li key={i}>
              <TriangleAlert aria-hidden /> {warningText(t, w, !!plan.from_run)}
            </li>
          ))}
        </ul>
      )}
      <PlanIdentity hash={plan.plan_hash} version={plan.base_version} />
      {!applyBody && !app && <p className="callout err slim">{t('core.plan.invalid')}</p>}
      <div className="as-wfe-acts">
        {wfAi && !app && plan.plan_hash === undefined && <label className="as-wf-auto"><input type="checkbox" checked={wfAi.autoApply} onChange={(e) => wfAi.setAutoApply(e.target.checked)} disabled={archived} />{t('as.wf.autoApply')}</label>}
        {!app && !empty && (
          <button type="button" className="btn sm primary" onClick={() => void act('apply')} disabled={busy !== null || archived || !applyBody}>
            <Check /> {busy === 'apply' ? t('as.wfe.applying') : t('as.wfe.apply')}
          </button>
        )}
        {app?.state === 'applied' && (
          <button type="button" className="btn sm" onClick={() => void act('undo')} disabled={busy !== null || archived}>
            <RotateCcw /> {busy === 'undo' ? t('as.wfe.undoing') : t('as.wfe.undo')}
          </button>
        )}
        {!empty && (
          <button type="button" className="btn sm" onClick={() => panel.open({ kind: 'plan', operationId: op.id }, { nodeId: plan.focus_node_id ?? null })}>
            <PanelRight /> {t('fc.edit.viewInPanel')}
          </button>
        )}
        <span className="faint small">{app?.state === 'applied' ? t('as.wfe.nextRun', { when: fmt.relative(app.at) }) : t('as.wfe.note')}</span>
      </div>
      <AsError error={error} />
    </div>
  );
}

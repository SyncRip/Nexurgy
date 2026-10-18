// Result of a workflow_create operation (.trellis/tasks/archive/2026-10/10-12-ai-work-panel design §2.5): a new part workflow
// planned from a built-in template — its steps in order, the warnings (missing inputs …) and two actions: preview the
// plan in the work panel, or create it. Nothing exists before the person clicks Create; creating writes the part
// workflow (v1), adds a workflow card to the conversation and makes the workflow the chat target. No undo: an unwanted
// workflow is archived.

import { Check, PanelRight, Plus, TriangleAlert } from 'lucide-react';
import { useState } from 'react';
import { applyAiOperation, type AiOperation, type AiWorkflowCreateResult } from '../../api/assistant';
import { isApiError } from '../../api/client';
import { useI18n } from '../../i18n/I18nProvider';
import { emitWorkflowChanged } from '../workflow/events';
import { nodeDisplayName } from '../workflow/labels';
import { topoOrder } from '../workflow/model';
import { useAssistantSession } from './AssistantProvider';
import { AsError, SimChip } from './common';
import { useWorkPanel } from './panel/WorkPanelContext';
import { warningText } from './WorkflowEditCard';
import { shownWarnings } from './workflowEdit';
import { planApplyBody } from './planApply';
import { PlanIdentity } from './PlanIdentity';

export function WorkflowCreateCard({ op, plan, archived }: { op: AiOperation; plan: AiWorkflowCreateResult; archived: boolean }) {
  const { t, fmt } = useI18n();
  const { store, bumpList } = useAssistantSession();
  const panel = useWorkPanel();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const app = plan.application;
  const graph = plan.graph_after;
  const ids = graph.nodes.map((n) => n.id);
  const order = topoOrder(ids, graph.edges) ?? ids;
  const byId = new Map(graph.nodes.map((n) => [n.id, n]));
  const added = new Set(plan.diff.added_nodes.map((n) => n.id));
  const warnings = shownWarnings(plan.warnings);
  const applyBody = planApplyBody(plan);

  const create = async () => {
    if (!applyBody) return;
    setBusy(true);
    setError(null);
    try {
      const res = await applyAiOperation(op.id, applyBody);
      emitWorkflowChanged(res.workflow.id, res.workflow.version);
      store.reload(); // the applied operation, the workflow card and the new target
      bumpList();
      panel.open({ kind: 'workflow', id: res.workflow.id });
    } catch (e) {
      // Created elsewhere (another tab): show what the server has instead of an error.
      if (isApiError(e) && e.code === 'already_applied') store.reload();
      else setError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className={`as-wfe as-wfc ${app ? 'applied' : 'pending'}`}>
      <div className="as-wfe-head">
        {app && <Check aria-hidden />}
        <b>{app ? t('fc.create.created', { name: plan.name, v: app.version }) : t('fc.create.pending', { n: order.length })}</b>
        <span className="faint small">· {t('fc.create.part', { part: plan.part_number })}</span>
        {op.simulated && <SimChip />}
      </div>
      {!app && <p className="as-wfc-name">{plan.name}</p>}
      {plan.summary && <p className="as-wfe-summary">{plan.summary}</p>}
      <ol className="as-wfc-chain" aria-label={t('fc.create.pending', { n: order.length })}>
        {order.map((id) => {
          const n = byId.get(id)!;
          return (
            <li key={id} title={id}>
              {nodeDisplayName(t, n)}
              {added.has(id) && <span className="as-wfc-new">{t('fc.create.added')}</span>}
            </li>
          );
        })}
      </ol>
      {warnings.length > 0 && (
        <ul className="as-wfe-warn">
          {warnings.map((w, i) => (
            <li key={i}>
              <TriangleAlert aria-hidden /> {warningText(t, w)}
            </li>
          ))}
        </ul>
      )}
      <PlanIdentity hash={plan.plan_hash} />
      {!applyBody && !app && <p className="callout err slim">{t('core.plan.invalid')}</p>}
      <div className="as-wfe-acts">
        {app ? (
          <button type="button" className="btn sm primary" onClick={() => panel.open({ kind: 'workflow', id: app.workflow_id })}>
            <PanelRight /> {t('fc.create.open')}
          </button>
        ) : (
          <>
            <button type="button" className="btn sm primary" onClick={() => void create()} disabled={busy || archived || !applyBody}>
              <Plus /> {busy ? t('fc.create.creating') : t('fc.create.create')}
            </button>
            <button type="button" className="btn sm" onClick={() => panel.open({ kind: 'plan', operationId: op.id })}>
              <PanelRight /> {t('fc.create.preview')}
            </button>
          </>
        )}
        <span className="faint small">{app ? t('fc.create.appliedNote', { when: fmt.relative(app.at) }) : t('fc.create.note')}</span>
      </div>
      <AsError error={error} />
    </div>
  );
}

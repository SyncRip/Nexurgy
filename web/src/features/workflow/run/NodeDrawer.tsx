// Node detail drawer of the run page: status, attempts, timing, inputs,
// outputs (artifacts), error with friendly text, and internal steps. Agent
// (business_task) nodes add "Execution process": the agent execution in brief, live.

import { ArrowUp, Hand, RotateCcw, TriangleAlert, X } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { getNodeTask } from '../../../api/collaboration';
import { retryRunNode } from '../../../api/endpoints';
import type { WorkflowRun } from '../../../api/types';
import { ErrorNotice } from '../../../components/ui/States';
import { AiFailureCauses } from '../../../components/ai/AiFailureCauses';
import { useToast } from '../../../components/ui/Toast';
import { useI18n } from '../../../i18n/I18nProvider';
import { NodeStatusChip, NodeTypeIcon } from '../icons';
import { nodeDisplayName, nodeErrorText, nodeTypeDoc, nodeTypeLabel } from '../labels';
import { durationBetween, formatDuration, type DefMap } from '../model';
import { isSimulated, PortValues, SimulatedBadge } from './Artifacts';
import { StepList } from './Steps';
import type { Verification } from '../../../api/engineering';
import { AssumptionList, VerificationChips } from '../../turbo/Turbo';
import { confirmSourceOf } from '../../turbo/policyModel';
import { UpstreamConfirmPanel } from '../../turbo/UpstreamConfirm';
import { ExecutionProcess } from '../../agentExec/ExecutionProcess';
import { useResource } from '../../../hooks/useResource';

export function NodeDrawer({
  run,
  nodeId,
  defs,
  canAct,
  onClose,
  onRun,
  onGoToPending,
}: {
  run: WorkflowRun;
  nodeId: string;
  defs: DefMap;
  canAct: boolean;
  onClose: () => void;
  onRun: (r: WorkflowRun) => void;
  onGoToPending: () => void;
}) {
  const { t, fmt } = useI18n();
  const toast = useToast();
  const node = run.nodes.find((n) => n.node_id === nodeId);
  const gnode = run.graph.nodes.find((n) => n.id === nodeId);
  const def = defs.get(node?.node_type ?? gnode?.type ?? '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const ref = useRef<HTMLElement>(null);
  // Agent nodes (business_task): the node task lists the agent executions shown under "Execution process" (execution layer P1).
  const agentNode = node?.node_type === 'business_task';
  const nodeTask = useResource((signal) => getNodeTask(run.id, nodeId, { signal }), [run.id, nodeId, node?.status, node?.attempt], { enabled: agentNode });

  useEffect(() => {
    ref.current?.focus();
  }, [nodeId]);

  useEffect(() => {
    setError(null);
  }, [nodeId]);

  if (!node) return null;
  const type = node.node_type;
  const dur = durationBetween(node.started_at, node.finished_at);
  const sim = isSimulated(node.outputs, node.steps);
  // W7: a Turbo node's product is unverified and carries the assumptions it rests on.
  const verification = ((node.outputs ?? {}) as { verification?: Verification }).verification ?? null;
  const canRetry = canAct && node.status === 'failed' && !run.cancel_requested;

  const retry = async () => {
    setBusy(true);
    setError(null);
    try {
      onRun(await retryRunNode(run.id, node.node_id));
      toast(t('wf.run.retried'));
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  const errDetails = node.error?.details && Object.keys(node.error.details).length ? node.error.details : null;
  // S5: a standard step on a Turbo spec asks a person to confirm the spec's values; the entry sits right here.
  const confirmSource = confirmSourceOf(node.error);

  return (
    <aside
      className="wf-drawer"
      aria-label={t('wf.drawer.title', { name: gnode ? nodeDisplayName(t, gnode) : nodeId })}
      tabIndex={-1}
      ref={ref}
      onKeyDown={(e) => {
        if (e.key === 'Escape') {
          e.stopPropagation();
          onClose();
        }
      }}
    >
      <header className="wf-drawer-head">
        <span className={`wf-insp-ico cat-${def?.category ?? 'unknown'}`}>
          <NodeTypeIcon type={type} />
        </span>
        <div className="wf-insp-titles">
          <h2 className="ellip">{gnode ? nodeDisplayName(t, gnode) : nodeId}</h2>
          <span className="faint small">
            {nodeTypeLabel(t, type)} · <span className="mono">{nodeId}</span>
          </span>
        </div>
        <button type="button" className="btn ghost icon" onClick={onClose} aria-label={t('common.close')}>
          <X />
        </button>
      </header>
      <div className="wf-drawer-body">
        <div className="row">
          <NodeStatusChip status={node.status} />
          {sim && <SimulatedBadge />}
          {def?.uses_ai && <span className="chip measure">AI</span>}
          {verification && <VerificationChips verification={verification} />}
        </div>
        {verification?.assumptions?.length ? <AssumptionList items={verification.assumptions} /> : null}
        <dl className="wf-kv">
          <div>
            <dt>{t('wf.drawer.attempt')}</dt>
            <dd className="mono">
              {node.attempt}/{node.max_attempts}
            </dd>
          </div>
          <div>
            <dt>{t('wf.drawer.started')}</dt>
            <dd>{fmt.dateTime(node.started_at)}</dd>
          </div>
          <div>
            <dt>{t('wf.drawer.finished')}</dt>
            <dd>{fmt.dateTime(node.finished_at)}</dd>
          </div>
          <div>
            <dt>{t('wf.drawer.duration')}</dt>
            <dd className="mono">{dur === null ? '—' : formatDuration(dur)}</dd>
          </div>
        </dl>
        {def && <p className="faint small">{nodeTypeDoc(t, type, def.doc)}</p>}

        {node.status === 'waiting_human' && (
          <div className="callout warn">
            <Hand aria-hidden />
            <span className="grow">{t('wf.drawer.waiting')}</span>
            <button type="button" className="btn sm" onClick={onGoToPending}>
              <ArrowUp /> {t('wf.drawer.goToPending')}
            </button>
          </div>
        )}

        {node.error && (
          <section className="wf-drawer-sec">
            <h3>{t('wf.drawer.error')}</h3>
            <div className="callout err">
              <TriangleAlert aria-hidden />
              <div className="stack-sm grow">
                <span>{nodeErrorText(t, node.error.code, node.error.message)}</span>
                {node.error.message && <span className="small muted">{node.error.message}</span>}
                {node.error.code && <code className="error-code">{t('common.errorCode', { code: node.error.code })}</code>}
                <AiFailureCauses details={errDetails} />
                {errDetails && <pre className="wf-json small">{JSON.stringify(errDetails, null, 2)}</pre>}
              </div>
            </div>
            {node.attempt < node.max_attempts && node.status !== 'failed' && <p className="faint small">{t('wf.drawer.willRetry')}</p>}
          </section>
        )}

        {confirmSource && canAct && (
          <UpstreamConfirmPanel projectId={run.project_id} source={{ source_type: confirmSource.source_type, source_id: confirmSource.source_id }}
            hint={t('pol.confirm.retryHint')} />
        )}

        {canRetry && (
          <div className="stack-sm">
            <button type="button" className="btn primary sm" onClick={() => void retry()} disabled={busy}>
              <RotateCcw /> {busy ? t('common.working') : t('wf.drawer.retry')}
            </button>
            <p className="faint small">{t('wf.drawer.retryHint')}</p>
            <ErrorNotice error={error} />
          </div>
        )}

        {agentNode && (
          <section className="wf-drawer-sec">
            <h3>{t('collab.node.tab.process')}</h3>
            {nodeTask.error && !nodeTask.data ? (
              <ErrorNotice error={nodeTask.error} onRetry={nodeTask.reload} />
            ) : nodeTask.data ? (
              <ExecutionProcess executions={nodeTask.data.executions ?? []} onChanged={nodeTask.reload} />
            ) : null}
          </section>
        )}

        <section className="wf-drawer-sec">
          <h3>{t('wf.drawer.outputs')}</h3>
          <PortValues values={node.outputs} portNames={def?.outputs.map((p) => p.name) ?? []} partId={run.part_id} />
        </section>

        <section className="wf-drawer-sec">
          <h3>{t('wf.drawer.inputs')}</h3>
          <PortValues values={node.inputs} portNames={def?.inputs.map((p) => p.name) ?? []} partId={run.part_id} />
        </section>

        {gnode && Object.keys(gnode.params ?? {}).length > 0 && (
          <details className="wf-drawer-sec">
            <summary>
              <h3>{t('wf.drawer.params')}</h3>
            </summary>
            <pre className="wf-json small">{JSON.stringify(gnode.params, null, 2)}</pre>
          </details>
        )}

        <section className="wf-drawer-sec">
          <h3>
            {t('wf.drawer.steps')} <span className="faint small">({node.steps?.length ?? 0})</span>
          </h3>
          {def?.uses_ai && <p className="faint small">{t('wf.drawer.stepsHint')}</p>}
          <StepList steps={node.steps ?? []} />
        </section>
      </div>
    </aside>
  );
}

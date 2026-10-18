import { useAssistantPageContext } from '../AssistantProvider';
// Run pane: one run, live (prd §3.4). The run canvas with steps waiting for a person or failed highlighted, and the
// selected step's drawer, where a person answers questions, approves / sends back and retries through the existing
// run services (the AI approves nothing). A run never changes: it shows the workflow version it ran.

import '@xyflow/react/dist/style.css';
import '../../workflow/workflow.css';
import { PlayCircle, Workflow as WorkflowIcon } from 'lucide-react';
import { useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import { getProject, listWorkflowNodeTypes } from '../../../api/endpoints';
import type { UUID } from '../../../api/types';
import { paths } from '../../../app/nav';
import { projectCapabilities } from '../../../auth/permissions';
import { PolicyChip } from '../../../components/engineering/EngineeringChips';
import { ErrorState } from '../../../components/ui/States';
import { useResource } from '../../../hooks/useResource';
import { useI18n } from '../../../i18n/I18nProvider';
import { RunStatusChip } from '../../workflow/icons';
import { ObjectStateBadge } from '../../orchestration/StateBadge';
import { runDeliveryState } from '../../orchestration/status';
import { nodeDisplayName, nodeStatusLabel } from '../../workflow/labels';
import { useLookups } from '../../workflow/lookups';
import { defMap } from '../../workflow/model';
import { NodeDrawer } from '../../workflow/run/NodeDrawer';
import { RunCanvas } from '../../workflow/run/RunCanvas';
import { useLiveRun } from '../../workflow/run/useLiveRun';
import { fullPageHref, runHighlight, type NodeSource } from './model';
import { PaneFrame, PaneLoading } from './PaneFrame';
import { useWorkPanel } from './WorkPanelContext';
import { usePanelInternals } from './WorkPanelProvider';

export function RunPane({ runId }: { runId: UUID }) {
  const { t, tn } = useI18n();
  const navigate = useNavigate();
  const wp = useWorkPanel();
  const { selectNode, locked } = usePanelInternals();
  const live = useLiveRun(runId);
  const run = live.run;
  const defsRes = useResource((signal) => listWorkflowNodeTypes({ signal }), []);
  const defs = useMemo(() => defMap(defsRes.data ?? []), [defsRes.data]);
  const projectRes = useResource((signal) => getProject(run!.project_id, { signal }), [run?.project_id], { enabled: !!run });
  const lookups = useLookups(run?.project_id);
  const highlight = useMemo(() => (run ? runHighlight(run) : undefined), [run]);

  useAssistantPageContext(run ? { projectId: run.project_id, label: run.title || t('wf.run.untitled'), object: { type: 'run', id: run.id, version: run.id }, target: { kind: 'run', object_id: run.id, project_id: run.project_id, ...(wp.state.nodeId ? { node: { node_id: wp.state.nodeId } } : {}) } } : null, locked);
  const icon = <PlayCircle />;
  const kind = t('wp.kind.run');
  if (!run) {
    if (live.error) {
      return (
        <PaneFrame icon={icon} kind={kind} title={kind} fullHref={fullPageHref({ kind: 'run', id: runId })}>
          <div className="wp-pad">
            <ErrorState error={live.error} onRetry={live.reload} />
          </div>
        </PaneFrame>
      );
    }
    return <PaneLoading icon={icon} kind={kind} />;
  }

  const source: NodeSource = { kind: 'run', objectId: run.id, projectId: run.project_id };
  const nodeId = wp.state.nodeId;
  const selected = nodeId && run.nodes.some((n) => n.node_id === nodeId) ? nodeId : null;
  const canAct = projectRes.data ? projectCapabilities(projectRes.data).workflow : false;
  const waiting = (run.node_counts.waiting_human ?? 0) + (run.node_counts.failed ?? 0);

  return (
    <PaneFrame
      icon={icon}
      kind={kind}
      title={run.title || t('wf.run.untitled')}
      chips={
        <>
          <RunStatusChip status={run.status} />
          <ObjectStateBadge state={runDeliveryState(run)} />
          {run.execution_policy === 'turbo' && <PolicyChip policy="turbo" />}
          {run.workflow_version !== null && (
            <span className="mono faint" title={t('wp.run.versionHint')}>
              {t('wp.run.version', { v: run.workflow_version })}
            </span>
          )}
        </>
      }
      fullHref={fullPageHref({ kind: 'run', id: run.id })}
      source={source}
      bodyClass={`wp-split ${selected ? 'has-drawer' : ''}`}
    >
      <div className="wp-canvas wf-run-canvas">
        {defsRes.data ? (
          <RunCanvas run={run} defs={defs} lookups={lookups} selected={selected} onSelect={(id) => selectNode(id, source)} highlight={highlight} compact />
        ) : defsRes.error ? (
          <ErrorState error={defsRes.error} onRetry={defsRes.reload} />
        ) : (
          <div className="wf-canvas-skel" />
        )}
      </div>
      <div className="wp-side">
        {selected ? (
          <NodeDrawer
            key={selected}
            run={run}
            nodeId={selected}
            defs={defs}
            canAct={canAct}
            onClose={() => wp.focusNode(null)}
            onRun={live.setRun}
            onGoToPending={() => navigate(paths.run(run.id))}
          />
        ) : (
          <div className="wp-pad stack-sm">
            <p className="muted small">{waiting ? tn('wp.run.attention', waiting, { n: waiting }) : t('wp.run.pick')}</p>
            {run.nodes
              .filter((n) => n.status === 'waiting_human' || n.status === 'failed')
              .map((n) => {
                const g = run.graph.nodes.find((x) => x.id === n.node_id);
                return (
                  <button key={n.node_id} type="button" className={`wp-attn ns-${n.status}`} onClick={() => selectNode(n.node_id, source)}>
                    <span className="ellip">{g ? nodeDisplayName(t, g) : n.node_id}</span>
                    <span className="faint small">{nodeStatusLabel(t, n.status)}</span>
                  </button>
                );
              })}
          </div>
        )}
        {run.workflow_id && (
          <p className="wp-foot faint small">
            <span>{t('wp.run.unchanged')}</span>
            <button type="button" className="wp-link" onClick={() => wp.open({ kind: 'workflow', id: run.workflow_id! })}>
              <WorkflowIcon aria-hidden /> {t('wp.run.openWorkflow')}
            </button>
          </p>
        )}
      </div>
    </PaneFrame>
  );
}

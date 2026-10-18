// Plan pane: a workflow_edit / workflow_create plan previewed on a read-only canvas (prd §3.3). New nodes, changed
// nodes and the node the request was about are highlighted; what the plan removes is drawn as a ghost from the base
// graph. Apply / undo act on the same operation as the chat card; afterwards the panel shows the workflow. Only the
// next run uses an applied plan — past runs never change (a plan made from a run says that run stays as it is).

import '@xyflow/react/dist/style.css';
import '../../workflow/workflow.css';
import { Background, BackgroundVariant, Controls, ReactFlow, ReactFlowProvider, useReactFlow, type NodeChange } from '@xyflow/react';
import { Check, FileDiff, Info, RotateCcw, TriangleAlert, Workflow as WorkflowIcon } from 'lucide-react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  applyAiOperation,
  getAiOperation,
  undoAiOperation,
  workflowCreateResult,
  workflowEditResult,
} from '../../../api/assistant';
import { listWorkflowNodeTypes } from '../../../api/endpoints';
import type { UUID, WorkflowGraph } from '../../../api/types';
import { Chip } from '../../../components/ui/Badges';
import { ErrorState } from '../../../components/ui/States';
import { useResource } from '../../../hooks/useResource';
import { useI18n, type TFunction } from '../../../i18n/I18nProvider';
import { useTheme } from '../../../prefs/ThemeProvider';
import { CanvasProvider, type CanvasCtx } from '../../workflow/canvas/CanvasContext';
import { NODE_TYPES } from '../../workflow/canvas/NodeCard';
import { emitWorkflowChanged } from '../../workflow/events';
import { computeFrames, graphToFlow, isFrameId, type AnyFlowNode, type WfEdge } from '../../workflow/flow';
import { edgeHighlightClass, nodeHighlightClass, type CanvasHighlight } from '../../workflow/highlight';
import { nodeDisplayName } from '../../workflow/labels';
import { useLookups, type Lookups } from '../../workflow/lookups';
import { defMap, layoutIfOverlapping, type DefMap } from '../../workflow/model';
import { useAssistantSession } from '../AssistantProvider';
import { AsError, OpStatusChip, SimChip } from '../common';
import { planLines, shownWarnings, type PlanLine } from '../workflowEdit';
import { warningText } from '../WorkflowEditCard';
import { planApplyBody } from '../planApply';
import { PlanIdentity } from '../PlanIdentity';
import { fullPageHref, planPreview } from './model';
import { PaneFrame, PaneLoading } from './PaneFrame';
import { useWorkPanel } from './WorkPanelContext';
import { usePanelInternals } from './WorkPanelProvider';

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

/** Node a plan line is about (selects it on the canvas). */
function lineNode(l: PlanLine): string | null {
  return 'id' in l ? l.id : null;
}

export function PlanPane({ operationId }: { operationId: UUID }) {
  const { t, fmt } = useI18n();
  const wp = useWorkPanel();
  const { selectNode } = usePanelInternals();
  const { store, bumpList } = useAssistantSession();
  const inDetail = store.detail?.operations.find((o) => o.id === operationId) ?? null;
  // An operation of another conversation (or not yet merged into the detail) is read on its own.
  const fetched = useResource((signal) => getAiOperation(operationId, { signal }), [operationId], { enabled: !inDetail });
  const op = inDetail ?? fetched.data ?? null;
  const edit = op ? workflowEditResult(op) : null;
  const create = op ? workflowCreateResult(op) : null;
  const plan = edit ?? create;
  const defsRes = useResource((signal) => listWorkflowNodeTypes({ signal }), []);
  const defs = useMemo(() => defMap(defsRes.data ?? []), [defsRes.data]);
  const lookups = useLookups(store.detail?.conversation.project.id);
  const preview = useMemo(
    () => (plan ? planPreview({ diff: plan.diff, graph_after: plan.graph_after, graph_before: edit?.graph_before ?? null, focus_node_id: edit?.focus_node_id ?? null }) : null),
    [plan, edit],
  );
  const lines = useMemo(() => (plan ? planLines({ diff: plan.diff, graph_before: edit?.graph_before ?? { nodes: [], edges: [], groups: [] }, graph_after: plan.graph_after }) : []), [plan, edit]);
  const [busy, setBusy] = useState<null | 'apply' | 'undo'>(null);
  const [error, setError] = useState<unknown>(null);

  const icon = <FileDiff />;
  const kind = t('wp.kind.plan');
  if (!op) {
    if (fetched.error) {
      return (
        <PaneFrame icon={icon} kind={kind} title={kind}>
          <div className="wp-pad">
            <ErrorState error={fetched.error} onRetry={fetched.reload} />
          </div>
        </PaneFrame>
      );
    }
    return <PaneLoading icon={icon} kind={kind} />;
  }
  if (!plan || !preview) {
    // Still planning, failed, or not a workflow plan.
    return (
      <PaneFrame icon={icon} kind={kind} title={kind} chips={<OpStatusChip status={op.status} />}>
        <div className="wp-pad stack-sm">
          <p className="muted">{op.status === 'succeeded' ? t('wp.plan.notPlan') : op.status === 'failed' || op.status === 'cancelled' ? t('wp.plan.noPlan') : t('wp.plan.pending')}</p>
          {op.error && (
            <p className="faint small">
              {op.error.message} <code className="error-code">{op.error.code}</code>
            </p>
          )}
        </div>
      </PaneFrame>
    );
  }

  const archived = !!store.detail?.conversation.archived;
  const app = edit ? edit.application : create?.application ?? null;
  const applied = app?.state === 'applied';
  const empty = plan.diff.empty || lines.length === 0;
  const workflowId = edit ? edit.workflow_id : create?.application?.workflow_id ?? null;
  const warnings = shownWarnings(plan.warnings).filter((w) => w.code !== 'run_unchanged');
  const title = edit ? edit.workflow_name : create!.name;
  const applyBody = planApplyBody(plan);

  const act = async (what: 'apply' | 'undo') => {
    if (what === 'apply' && !applyBody) return;
    setBusy(what);
    setError(null);
    try {
      const res = what === 'undo' ? await undoAiOperation(op.id) : await applyAiOperation(op.id, applyBody!);
      emitWorkflowChanged(res.workflow.id, res.workflow.version);
      store.reload(); // the operation, the re-pinned references (and a workflow card for workflow_create)
      bumpList();
      wp.open({ kind: 'workflow', id: res.workflow.id });
    } catch (e) {
      setError(e);
    } finally {
      setBusy(null);
    }
  };

  let state: string;
  if (edit?.application?.state === 'applied') state = t('as.wfe.applied', { from: edit.application.version_before, to: edit.application.version_after });
  else if (edit?.application?.state === 'undone') state = t('as.wfe.undone', { v: edit.application.undo_version ?? '' });
  else if (create?.application) state = t('wp.plan.created', { v: create.application.version });
  else if (empty) state = create ? t('wp.plan.asTemplate', { n: plan.graph_after.nodes.length }) : t('as.wfe.empty');
  else state = t('as.wfe.pending', { n: lines.length });

  const hl = preview.highlight;
  const counts = { added: 0, changed: 0, focus: 0, removed: 0 };
  for (const h of Object.values(hl.nodes ?? {})) if (h in counts) counts[h as keyof typeof counts] += 1;

  return (
    <PaneFrame
      icon={icon}
      kind={edit ? t('wp.kind.plan') : t('wp.kind.planCreate')}
      title={title}
      chips={
        <>
          {edit ? <span className="mono faint">{t('wp.plan.base', { v: edit.base_version })}</span> : <span className="mono faint">{create!.template_key}</span>}
          {applied && (
            <Chip tone="ok" icon={<Check aria-hidden />} className="sm">
              {t('wp.plan.applied')}
            </Chip>
          )}
          {op.simulated && <SimChip />}
        </>
      }
      fullHref={fullPageHref({ kind: 'plan', operationId }, { partId: plan.part_id, workflowId })}
      bodyClass="wp-split"
    >
      <div className="wp-canvas wf-run-canvas">
        {defsRes.data ? (
          <PreviewCanvas graph={preview.graph} highlight={hl} defs={defs} lookups={lookups} selected={wp.state.nodeId} onSelect={(id) => selectNode(id, null)} />
        ) : defsRes.error ? (
          <ErrorState error={defsRes.error} onRetry={defsRes.reload} />
        ) : (
          <div className="wf-canvas-skel" />
        )}
        <div className="wp-legend" aria-label={t('wp.plan.legend')}>
          {(['added', 'changed', 'focus', 'removed'] as const)
            .filter((k) => counts[k] > 0)
            .map((k) => (
              <span key={k} className={`wp-legend-item hl-${k}`}>
                <i aria-hidden /> {t(`wp.hl.${k}`)} {counts[k]}
              </span>
            ))}
        </div>
      </div>
      <div className="wp-side">
        <div className="wp-plan-head">
          <b>{state}</b>
        </div>
        {plan.summary && <p className="wp-plan-summary">{plan.summary}</p>}
        {lines.length > 0 && (
          <ol className="wp-plan-list">
            {lines.map((l, i) => {
              const id = lineNode(l);
              return (
                <li key={i}>
                  {id && preview.graph.nodes.some((n) => n.id === id) ? (
                    <button type="button" className={`wp-link ${wp.state.nodeId === id ? 'active' : ''}`} onClick={() => wp.focusNode(id)}>
                      {lineText(t, l)}
                    </button>
                  ) : (
                    lineText(t, l)
                  )}
                </li>
              );
            })}
          </ol>
        )}
        {warnings.length > 0 && (
          <ul className="wp-plan-warn">
            {warnings.map((w, i) => (
              <li key={i}>
                <TriangleAlert aria-hidden /> {warningText(t, w, !!edit?.from_run)}
              </li>
            ))}
          </ul>
        )}
        <p className="wp-plan-note">
          <Info aria-hidden />
          <span>
            {edit?.from_run
              ? t('wp.plan.fromRun', { v: edit.from_run.workflow_version ?? '?' })
              : create
                ? t('wp.plan.createNote')
                : app?.state === 'applied'
                  ? t('as.wfe.nextRun', { when: fmt.relative(app.at) })
                  : t('wp.plan.note')}
          </span>
        </p>
        <PlanIdentity hash={plan.plan_hash} version={edit?.base_version} />
        {!applyBody && !app && <p className="callout err slim">{t('core.plan.invalid')}</p>}
        <div className="wp-plan-acts">
          {!app && (!empty || !!create) && (
            <button type="button" className="btn sm primary" onClick={() => void act('apply')} disabled={busy !== null || archived || !applyBody}>
              <Check /> {busy === 'apply' ? t('as.wfe.applying') : create ? t('wp.plan.create') : t('as.wfe.apply')}
            </button>
          )}
          {edit?.application?.state === 'applied' && (
            <button type="button" className="btn sm" onClick={() => void act('undo')} disabled={busy !== null || archived}>
              <RotateCcw /> {busy === 'undo' ? t('as.wfe.undoing') : t('as.wfe.undo')}
            </button>
          )}
          {app && workflowId && (
            <button type="button" className="btn sm" onClick={() => wp.open({ kind: 'workflow', id: workflowId })}>
              <WorkflowIcon /> {t('wp.plan.openWorkflow')}
            </button>
          )}
          <button type="button" className="btn sm ghost" onClick={wp.close}>
            {t('common.close')}
          </button>
        </div>
        <AsError error={error} />
      </div>
    </PaneFrame>
  );
}

/** Read-only canvas of a planned graph with its highlights (ghost nodes / edges for what the plan removes). */
function PreviewCanvas(props: { graph: WorkflowGraph; highlight: CanvasHighlight; defs: DefMap; lookups: Lookups; selected: string | null; onSelect: (id: string | null) => void }) {
  return (
    <ReactFlowProvider>
      <PreviewInner {...props} />
    </ReactFlowProvider>
  );
}

function PreviewInner({ graph, highlight, defs, lookups, selected, onSelect }: { graph: WorkflowGraph; highlight: CanvasHighlight; defs: DefMap; lookups: Lookups; selected: string | null; onSelect: (id: string | null) => void }) {
  const { resolved } = useTheme();
  const rf = useReactFlow<AnyFlowNode, WfEdge>();
  const laid = useMemo(() => layoutIfOverlapping(graph), [graph]);
  const base = useMemo(() => graphToFlow(laid, defs), [laid, defs]);
  const [dims, setDims] = useState<Record<string, { width: number; height: number }>>({});
  const onNodesChange = useCallback((changes: NodeChange<AnyFlowNode>[]) => {
    const upd: Record<string, { width: number; height: number }> = {};
    for (const c of changes) if (c.type === 'dimensions' && c.dimensions && !isFrameId(c.id)) upd[c.id] = c.dimensions;
    if (Object.keys(upd).length) setDims((d) => ({ ...d, ...upd }));
  }, []);
  const nodes = useMemo<AnyFlowNode[]>(() => {
    const real = base.nodes.map((n) => ({ ...n, selected: n.id === selected, draggable: false, connectable: false, measured: dims[n.id], className: nodeHighlightClass(highlight.nodes?.[n.id]) }));
    return [...computeFrames(base.groups, real, null, false), ...real];
  }, [base, selected, dims, highlight.nodes]);
  const edges = useMemo<WfEdge[]>(() => base.edges.map((e) => ({ ...e, className: `${e.className ?? ''}${edgeHighlightClass(highlight.edges?.[e.id])}`, selectable: false })), [base, highlight.edges]);
  const connectedInputs = useMemo(() => {
    const m = new Map<string, Set<string>>();
    for (const e of laid.edges) m.set(e.target, (m.get(e.target) ?? new Set()).add(e.target_port));
    return m;
  }, [laid.edges]);
  const ctx = useMemo<CanvasCtx>(() => ({ defs, mode: 'edit', readOnly: true, errorsByNode: new Map(), connectedInputs, lookups }), [defs, connectedInputs, lookups]);

  useEffect(() => {
    if (!selected) return;
    const n = base.nodes.find((x) => x.id === selected);
    if (!n) return;
    const internal = rf.getInternalNode(selected);
    const w = internal?.measured?.width ?? 248;
    const h = internal?.measured?.height ?? 140;
    void rf.setCenter(n.position.x + w / 2, n.position.y + h / 2, { zoom: Math.max(rf.getZoom(), 0.8), duration: 300 });
  }, [selected, base, rf]);

  return (
    <CanvasProvider value={ctx}>
      <ReactFlow<AnyFlowNode, WfEdge>
        nodes={nodes}
        edges={edges}
        nodeTypes={NODE_TYPES}
        onNodesChange={onNodesChange}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable
        deleteKeyCode={null}
        onNodeClick={(_e, n) => !isFrameId(n.id) && onSelect(n.id)}
        onPaneClick={() => onSelect(null)}
        fitView
        fitViewOptions={{ padding: 0.15, maxZoom: 1 }}
        minZoom={0.15}
        maxZoom={2}
        colorMode={resolved}
      >
        <Background variant={BackgroundVariant.Dots} gap={16} size={1.2} />
        <Controls showInteractive={false} />
      </ReactFlow>
    </CanvasProvider>
  );
}

// Part workspace — the part's workflow canvas. One bar (workflow, run, mode,
// actions), the canvas, and a side
// panel with the AI assistant (whole workflow as context unless something
// narrower is added) and the selected node's details.
//
// Governance (G2): a run is never changed. "Edit" changes the part's own
// workflow, which only affects the next run; "Run" creates a new run and the
// old ones stay in the history. Template runs are copied into a part workflow
// before editing — the template itself is untouched. Decisions and retries
// still happen on nodes (node drawer / run page), never by the AI.

import { ReactFlowProvider } from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { CheckSquare,Clock,Copy,Download,ExternalLink,Eye,FolderOpen,MoreHorizontal,Pencil,Play,Plus,RotateCw,SquareMousePointer,Workflow as WorkflowIcon } from 'lucide-react';
import { useEffect,useMemo,useState,type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { Link,useNavigate,useSearchParams } from 'react-router-dom';
import { downloadBlob } from '../../../api/blobs';
import { createWorkflow,getRevision,listRuns,listWorkflowNodeTypes,listWorkflows,rerunRun,startWorkflowRun } from '../../../api/endpoints';
import type { Project,Workflow,WorkflowRun } from '../../../api/types';
import { paths } from '../../../app/nav';
import type { ProjectCapabilities } from '../../../auth/permissions';
import { PolicyChip } from '../../../components/engineering/EngineeringChips';
import { Dialog } from '../../../components/ui/Dialog';
import { Menu,type MenuItem } from '../../../components/ui/Menu';
import { EmptyState,ErrorState,ErrorText,SkeletonRows } from '../../../components/ui/States';
import { useToast } from '../../../components/ui/Toast';
import { useResource } from '../../../hooks/useResource';
import { useI18n } from '../../../i18n/I18nProvider';
import type { PartTabProps } from '../../../pages/part/registry';
import { useAssistantPageContext } from '../../assistant/AssistantProvider';
import { runRef } from '../../assistant/refs';
import { RerunDialog } from '../../templates/RerunDialog';
import { SaveRunTemplateAction } from '../../templates/SaveRunTemplateAction';
import '../../templates/templates.css';
import { WorkflowEditor } from '../../workflow/editor/WorkflowEditor';
import { onWorkflowChanged } from '../../workflow/events';
import { RunStatusChip } from '../../workflow/icons';
import { nodeDisplayName } from '../../workflow/labels';
import { useLookups } from '../../workflow/lookups';
import { defMap,TERMINAL_RUN } from '../../workflow/model';
import { NodeDrawer } from '../../workflow/run/NodeDrawer';
import { RunCanvas } from '../../workflow/run/RunCanvas';
import { Timeline } from '../../workflow/run/Timeline';
import { useLiveRun } from '../../workflow/run/useLiveRun';
import '../../workflow/workflow.css';
import { WORKSPACE_GROUP } from '../workflowModel';
import './flow.css';
import { legend,partFlows,pickFlow,runNumber,type PartFlow } from './model';
import { NewFlowDialog } from './NewFlowDialog';

type Side = 'node';

/** Side panel: the assistant, or the selected node / step list. */
function SidePanel({ side, setSide, run, selected, onSelect, defs, canAct, onRun, nodeLabel }: {
  side: Side;
  setSide: (s: Side) => void;
  run: WorkflowRun | undefined;
  selected: string | null;
  onSelect: (id: string | null) => void;
  defs: ReturnType<typeof defMap>;
  canAct: boolean;
  onRun: (r: WorkflowRun) => void;
  nodeLabel: string | null;
}) {
  const { t } = useI18n();
  const navigate = useNavigate();
  return (
    <aside className="pflow-side" aria-label={t('pflow.side')}>
      <div className="pflow-side-tabs" role="tablist">
        <button type="button" role="tab" aria-selected={side === 'node'} className={`pflow-side-tab ${side === 'node' ? 'active' : ''}`} onClick={() => setSide('node')}>
          <SquareMousePointer aria-hidden /> <span className="ellip">{nodeLabel ?? t('pflow.side.noNode')}</span>
        </button>
      </div>
      <div className="pflow-side-body">
        {!run ? (
          <p className="faint small pflow-side-empty">{t('pflow.side.noRun')}</p>
        ) : selected && run.nodes.some((n) => n.node_id === selected) ? (
          <NodeDrawer run={run} nodeId={selected} defs={defs} canAct={canAct} onClose={() => onSelect(null)} onRun={onRun} onGoToPending={() => navigate(paths.run(run.id))} />
        ) : (
          <>
            <p className="faint small pflow-side-empty">{t('pflow.side.pickNode')}</p>
            <Timeline run={run} selected={selected} onSelect={onSelect} />
          </>
        )}
      </div>
    </aside>
  );
}

/** The selected run on the read-only canvas, live while active. */
function RunPane({ runId, project, caps, side, setSide, onRunChange, tools }: {
  runId: string;
  project: Project;
  caps: ProjectCapabilities;
  side: Side;
  setSide: (s: Side) => void;
  onRunChange: (r: WorkflowRun | undefined) => void;
  tools: ReactNode;
  /** "BRK-1001 · Design to quote" (the AI panel's conversation scope). */
  /** The part's own workflow (the AI may propose changes to it). */
  /** That workflow: the AI's default target, so a change request plans a change of it (workflow_edit). */
}) {
  const { t } = useI18n();
  const live = useLiveRun(runId);
  const run = live.run && live.run.id === runId ? live.run : undefined;
  const defsRes = useResource((signal) => listWorkflowNodeTypes({ signal }), []);
  const defs = useMemo(() => defMap(defsRes.data ?? []), [defsRes.data]);
  const lookups = useLookups(project.id);
  const [selected, setSelected] = useState<string | null>(null);
  useEffect(() => onRunChange(run), [run, onRunChange]);

  // The workflow AI works on the whole run unless the user adds something narrower (R1.2 §06).
  const title = run?.title || t('wf.run.untitled');
  useAssistantPageContext(
    run
      ? {
          projectId: run.project_id,
          label: t('ar.run.scope', { title }),
          ...(run.task ? { taskId: run.task.id } : {}),
          ...(run.part_id ? { partId: run.part_id } : {}),
          object: { type: 'run', id: run.id, version: run.id },
          target: { ...runRef(run.project_id, run.id), ...(selected ? { node: { node_id: selected } } : {}) },
        }
      : null,
  );

  const select = (id: string | null) => {
    setSelected(id);
    if (id) setSide('node');
  };

  const gnode = run?.graph.nodes.find((n) => n.id === selected);
  const nodeLabel = selected && gnode ? nodeDisplayName(t, gnode) : selected;

  return (
    <div className="pflow-body">
      <div className="pflow-canvas wf-run-canvas">
        {live.error && !run ? (
          <ErrorState error={live.error} onRetry={live.reload} />
        ) : run && defsRes.data ? (
          <RunCanvas run={run} defs={defs} lookups={lookups} selected={selected} onSelect={select} tools={tools} />
        ) : (
          <div className="wf-canvas-skel" />
        )}
        {run && (
          <div className="pflow-legend" aria-label={t('pflow.legend')}>
            {legend(run.node_counts).map((x) => (
              <span key={x.status} className={`pflow-legend-item st-${x.status}`}>
                <i aria-hidden /> {t(`ws.nodeStatus.${x.status}`)} {x.n}
              </span>
            ))}
          </div>
        )}
      </div>
      <SidePanel side={side} setSide={setSide} run={run} selected={selected} onSelect={select} defs={defs} canAct={caps.workflow} onRun={live.setRun} nodeLabel={nodeLabel} />
    </div>
  );
}

/** Rerun of a part workflow run on the workflow's current graph (S5): the policy it will be frozen with (inherited from
 * the run it repeats unless the person picks one) is shown before the run starts. */
function RerunOwnDialog({ run, onClose, onSubmit }: { run: Pick<WorkflowRun, 'id' | 'project_id' | 'title'>; onClose: () => void; onSubmit: () => Promise<void> }) {
  const { t } = useI18n();
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    setBusy(true);
    try {
      await onSubmit();
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog
      title={t('tpl.rerun.title')}
      subtitle={run.title || t('wf.run.untitled')}
      onClose={onClose}
      busy={busy}
      onSubmit={() => void submit()}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={busy}>
            <RotateCw /> {busy ? t('wf.runDialog.starting') : t('tpl.rerun.submit')}
          </button>
        </>
      }
    >
      <p className="muted small">{t('tpl.rerun.explain')}</p>
      <></>
    </Dialog>
  );
}

function flowName(t: ReturnType<typeof useI18n>['t'], f: PartFlow): string {
  if (f.key === WORKSPACE_GROUP) return t('pwf.workspaceGroup');
  return f.workflow ? f.name : t('pflow.templateRuns', { name: f.name });
}

export default function PartFlowTab({ part, project, caps, barSlot }: PartTabProps) {
  const { t, tn, fmt } = useI18n();
  const toast = useToast();
  const navigate = useNavigate();
  const [search, setSearch] = useSearchParams();
  const [side, setSide] = useState<Side>('node');
  const [mode, setMode] = useState<'view' | 'edit'>('view');
  const [dialog, setDialog] = useState<'new' | 'rerun' | 'rerunOwn' | null>(null);
  const [busy, setBusy] = useState(false);
  const [currentRun, setCurrentRun] = useState<WorkflowRun | undefined>(undefined);
  const [editorDirty, setEditorDirty] = useState(false);


  const runs = useResource((signal) => listRuns(project.id, { part_id: part.id, limit: 200 }, { signal }), [project.id, part.id]);
  const own = useResource((signal) => listWorkflows(project.id, { part_id: part.id }, { signal }), [project.id, part.id]);
  // An AI change plan applied / undone in the side panel: reload the part's workflows (the next run uses it).
  const ownIds = (own.data ?? []).map((w) => w.id).join(',');
  useEffect(
    () =>
      onWorkflowChanged(({ workflowId, version }) => {
        if (!ownIds.split(',').includes(workflowId)) return;
        own.reload();
        toast(t('cv.ai.applied', { v: version }));
      }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [ownIds],
  );

  const templates = useResource((signal) => listWorkflows(project.id, { include_archived: true }, { signal }), [project.id]);
  const defsRes = useResource((signal) => listWorkflowNodeTypes({ signal }), []);
  const names = useMemo(() => new Map((templates.data ?? []).map((w) => [w.id, w.name])), [templates.data]);
  const flows = useMemo(() => partFlows(own.data ?? [], runs.data ?? [], names), [own.data, runs.data, names]);

  const runParam = search.get('run');
  const flow = pickFlow(flows, search.get('flow'), runParam);
  const run = flow?.runs.find((r) => r.id === runParam) ?? flow?.runs[0];
  const canEdit = caps.workflow && !part.archived_at;
  const editing = mode === 'edit' && !!flow?.workflow;

  // A part workflow that never ran opens in the editor (only once the run list is known).
  const runsLoaded = !!runs.data;
  useEffect(() => {
    if (runsLoaded && flow?.workflow && !flow.runs.length && canEdit) setMode('edit');
  }, [runsLoaded, flow?.key, flow?.workflow, flow?.runs.length, canEdit]);

  // Keep the run list fresh while a run is active.
  const anyActive = (runs.data ?? []).some((r) => !TERMINAL_RUN.has(r.status));
  useEffect(() => {
    if (!anyActive) return;
    const id = window.setInterval(runs.reload, 8000);
    return () => window.clearInterval(id);
  }, [anyActive, runs.reload]);

  const go = (next: { flow?: string; run?: string | null }) => {
    const q = new URLSearchParams(search);
    if (next.flow !== undefined) q.set('flow', next.flow);
    if (next.run) q.set('run', next.run);
    else if (next.run === null) q.delete('run');
    setSearch(q, { replace: true });
  };

  const switchFlow = (key: string) => {
    if (editorDirty && !window.confirm(t('pflow.leaveDirty'))) return;
    setMode('view');
    go({ flow: key, run: null });
  };

  /** A new run of the part workflow on its current graph (the old runs stay as they are). The policy is chosen per
   * click: "Turbo" is explicit and confirmed (W7); "Run" names none, so the server uses the project default (standard
   * unless the project's "default Turbo" is on, S5) and records that source. */
  const startRun = async (w: Workflow) => {
    setBusy(true);
    try {
      const r = await startWorkflowRun(w.id, { part_id: part.id });
      toast(t('pflow.started'));
      setMode('view');
      runs.reload();
      go({ flow: w.id, run: r.id });
    } catch (e) {
      toast(<ErrorText error={e} />, { tone: 'err' });
    } finally {
      setBusy(false);
    }
  };

  /** Rerun with this run's inputs on the part workflow's current graph (policy chosen in RerunOwnDialog, S5). */
  const rerunOwn = async (runId: string, w: Workflow) => {
    setBusy(true);
    try {
      const r = await rerunRun(runId, {});
      setDialog(null);
      toast(t('tpl.rerun.started'));
      runs.reload();
      go({ flow: w.id, run: r.id });
    } catch (e) {
      toast(<ErrorText error={e} />, { tone: 'err' });
    } finally {
      setBusy(false);
    }
  };

  /** Template runs are edited as the part's own copy of the run's graph; the template is untouched. */
  const copyToPart = async () => {
    if (!flow || !currentRun) return;
    setBusy(true);
    try {
      const w = await createWorkflow(project.id, { name: `${part.number} · ${flow.name}`.slice(0, 200), graph: currentRun.graph, part_id: part.id });
      toast(t('pflow.copied'));
      own.reload();
      setMode('edit');
      go({ flow: w.id, run: null });
    } catch (e) {
      toast(<ErrorText error={e} />, { tone: 'err' });
    } finally {
      setBusy(false);
    }
  };

  const error = runs.error || own.error;
  if (error && (!runs.data || !own.data)) return <ErrorState error={error} onRetry={() => (runs.reload(), own.reload())} />;
  if (!runs.data || !own.data) return <SkeletonRows rows={6} />;

  const liveStatus = currentRun && run && currentRun.id === run.id ? currentRun.status : run?.status;
  const task = run?.task ?? null;
  const latest = part.latest_revision;

  /** STEP of the part's latest revision (the same file as the model tab's export). */
  const downloadStep = async () => {
    if (!latest) return;
    setBusy(true);
    try {
      const rev = await getRevision(latest.id);
      if (!rev.step_sha) throw new Error('no_step');
      await downloadBlob(rev.step_sha, `${part.number}_R${rev.rev_no}.step`);
    } catch (e) {
      toast(e instanceof Error && e.message === 'no_step' ? t('ws.rev.noStep') : <ErrorText error={e} />, { tone: 'err' });
    } finally {
      setBusy(false);
    }
  };

  const more: MenuItem[] = flow
    ? [
        ...(canEdit ? [{ id: 'new', label: t('pflow.newFlow'), icon: <Plus />, onSelect: () => setDialog('new') }] : []),
        ...(!editing && canEdit && flow.workflow && run && TERMINAL_RUN.has(run.status)
          ? [{ id: 'rerun', label: t('tpl.rerun.action'), icon: <RotateCw />, onSelect: () => setDialog('rerunOwn') }]
          : []),
        ...(!editing && canEdit && !flow.workflow && flow.key !== WORKSPACE_GROUP && currentRun?.origin === 'workflow' && TERMINAL_RUN.has(currentRun.status)
          ? [{ id: 'rerunTpl', label: t('tpl.rerun.action'), icon: <RotateCw />, onSelect: () => setDialog('rerun') }]
          : []),
        ...(run ? [{ id: 'runPage', label: t('pwf.run.open'), icon: <ExternalLink />, onSelect: () => navigate(paths.run(run.id)), separator: true }] : []),
      ]
    : [];

  const bar = flow && (
    <div className="pflow-bar">
      {run && !editing && liveStatus && <RunStatusChip status={liveStatus} />}
      {run && !editing && run.execution_policy === 'turbo' && <PolicyChip policy="turbo" />}
      <span className="grow" />
      {task && !editing && (
        <Link to={paths.task(task.id)} className="pflow-task" title={t('cv.task.hint')}>
          <CheckSquare aria-hidden /> <span className="ellip">{t('cv.task', { name: task.title })}</span>
        </Link>
      )}
      {!!flow.runs.length && run && !editing && (
        <label className="pflow-sel" title={t('cv.runs.hint')}>
          <Clock aria-hidden />
          <span className="sr-only">{t('pflow.pickRun')}</span>
          <select value={run.id} onChange={(e) => go({ flow: flow.key, run: e.target.value })}>
            {flow.runs.map((r) => (
              <option key={r.id} value={r.id}>
                {r.id === flow.runs[0].id ? t('cv.runs.current') : t('pflow.runOption', { n: runNumber(flow, r.id), status: t(`ws.runStatus.${r.status}`), when: fmt.relative(r.created_at) })}
              </option>
            ))}
          </select>
          <span className="mono">{tn('cv.runs.count', flow.runs.length, { n: flow.runs.length })}</span>
        </label>
      )}
      <label className="pflow-sel">
        <WorkflowIcon aria-hidden />
        <span className="sr-only">{t('pflow.pickFlow')}</span>
        <select value={flow.key} onChange={(e) => switchFlow(e.target.value)}>
          {flows.map((f) => (
            <option key={f.key} value={f.key}>
              {flowName(t, f)}
            </option>
          ))}
        </select>
      </label>
      {flow.workflow && canEdit && (
        <div className="seg" role="group" aria-label={t('pflow.mode')}>
          <button type="button" className="seg-btn" aria-pressed={!editing} onClick={() => (editorDirty && !window.confirm(t('pflow.leaveDirty')) ? null : setMode('view'))} disabled={!flow.runs.length}>
            <Eye /> {t('pflow.view')}
          </button>
          <button type="button" className="seg-btn" aria-pressed={editing} onClick={() => setMode('edit')}>
            <Pencil /> {t('pflow.edit')}
          </button>
        </div>
      )}
      {!editing && canEdit && !flow.workflow && flow.key !== WORKSPACE_GROUP && (
        <button type="button" className="btn sm primary" onClick={() => void copyToPart()} disabled={busy || !currentRun} title={t('pflow.copyHint')}>
          <Copy /> {t('pflow.copyToEdit')}
        </button>
      )}
      <Link to={paths.part(part.id, 'files')} className="btn sm">
        <FolderOpen /> <span className="hide-sm">{t('part.tab.files')}</span>
      </Link>
      {latest && !latest.mesh_only && (
        <button type="button" className="btn sm" onClick={() => void downloadStep()} disabled={busy} title={t('ws.rev.dlStep')}>
          <Download /> STEP
        </button>
      )}
      {!editing && currentRun && currentRun.id === run?.id && (
        <SaveRunTemplateAction key={currentRun.id} project={project} run={currentRun} />
      )}
      {more.length > 0 && <Menu label={t('common.moreActions')} trigger={<MoreHorizontal />} items={more} triggerClassName="btn sm icon" />}
    </div>
  );

  // Canvas toolbar (prototype: add node · run | zoom): editing happens in the editor; a run is always a new run.
  const addNode = flow?.workflow && canEdit ? () => setMode('edit') : canEdit && flow && !flow.workflow && flow.key !== WORKSPACE_GROUP ? () => void copyToPart() : undefined;
  const runNew = flow?.workflow && canEdit ? () => void startRun(flow.workflow!) : undefined;

  const tools = (
    <>
      {(addNode || runNew) && (
        <div className="cv-grp">
          {addNode && (
            <button type="button" className="hl" onClick={addNode} disabled={busy} title={flow?.workflow ? t('cv.tools.addHint') : t('pflow.copyHint')}>
              <Plus aria-hidden /> {t('cv.tools.add')}
            </button>
          )}
          {runNew && (
            <button type="button" onClick={runNew} disabled={busy} title={t('cv.tools.runHint')}>
              <Play aria-hidden /> {t('cv.tools.run')}
            </button>
          )}

        </div>
      )}
    </>
  );

  return (
    <div className={`pflow ${barSlot ? 'ported' : ''}`}>
      {!flow ? (
        <EmptyState
          icon={<WorkflowIcon className="big-ico" aria-hidden />}
          title={t('pwf.empty')}
          body={canEdit ? t('pflow.emptyBody') : t('pwf.emptyBodyViewer')}
          action={
            canEdit ? (
              <button type="button" className="btn primary" onClick={() => setDialog('new')}>
                <Plus /> {t('pflow.newFlow')}
              </button>
            ) : undefined
          }
        />
      ) : (
        <>
          {barSlot ? createPortal(bar, barSlot) : bar}
          {editing && flow.workflow ? (
            <div className="pflow-editor">
              {defsRes.data ? (
                <ReactFlowProvider>
                  <WorkflowEditor
                    key={flow.workflow.id}
                    initial={flow.workflow}
                    project={project}
                    defs={defsRes.data}
                    onWorkflowChange={() => own.reload()}
                    onDirtyChange={setEditorDirty}
                    // Saved first by the editor; the run uses exactly the saved graph.
                    onRequestRun={() => void startRun(flow.workflow!)}
                    back={flow.runs.length ? { label: t('cv.backToRuns'), onClick: () => (editorDirty && !window.confirm(t('pflow.leaveDirty')) ? null : setMode('view')) } : null}
                  />
                </ReactFlowProvider>
              ) : (
                <div className="wf-canvas-skel" />
              )}
              <p className="pflow-note faint small">{t('pflow.editNote')}</p>
            </div>
          ) : run ? (
            <RunPane
              key={run.id}
              runId={run.id}
              project={project}
              caps={caps}
              side={side}
              setSide={setSide}
              onRunChange={setCurrentRun}
              tools={tools}
            />
          ) : (
            <EmptyState icon={<WorkflowIcon className="big-ico" aria-hidden />} title={t('pflow.noRuns')} body={t('pflow.noRunsBody')} />
          )}
        </>
      )}
      {dialog === 'new' && (
        <NewFlowDialog
          part={part}
          onClose={() => setDialog(null)}
          onCreated={(w) => {
            setDialog(null);
            own.reload();
            setMode('edit');
            go({ flow: w.id, run: null });
          }}
        />
      )}
      {dialog === 'rerunOwn' && flow?.workflow && run && (
        <RerunOwnDialog run={run} onClose={() => setDialog(null)} onSubmit={() => rerunOwn(run.id, flow.workflow!)} />
      )}
      {dialog === 'rerun' && currentRun && (
        <RerunDialog
          run={currentRun}
          onClose={() => setDialog(null)}
          onStarted={(r) => {
            setDialog(null);
            toast(t('tpl.rerun.started'));
            runs.reload();
            go({ run: r.id });
          }}
        />
      )}
    </div>
  );
}


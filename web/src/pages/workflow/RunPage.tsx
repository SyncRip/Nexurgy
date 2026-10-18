// /runs/:runId — live run view: header (task, template version or draft / legacy run,
// purpose, rerun lineage), pending actions, frozen inputs and output assets,
// canvas + timeline, node drawer, and "rerun" (a new run with rerun_of).
// "Add to AI" references this run or one exact output row (asset version +
// run / node / output key — never "the latest run").

import '@xyflow/react/dist/style.css';
import { ArrowLeft,Ban,Bot,CircleStop,Hand,LayoutList,MessageSquarePlus,Network,PackageOpen,PlayCircle,Radio,RefreshCw,RotateCw,TriangleAlert,Workflow as WorkflowIcon } from 'lucide-react';
import { useEffect,useMemo,useRef,useState } from 'react';
import { Link,useNavigate,useParams,useSearchParams } from 'react-router-dom';
import { useFeature } from '../../api/capabilities';
import { isApiError } from '../../api/client';
import { cancelRun,getPart,getProject,listWorkflowNodeTypes } from '../../api/endpoints';
import { paths } from '../../app/nav';
import { projectCapabilities } from '../../auth/permissions';
import { PolicyChip,UnverifiedChip } from '../../components/engineering/EngineeringChips';
import { useCrumbs,useShellProject } from '../../components/shell/ShellContext';
import { ConfirmDialog } from '../../components/ui/Dialog';
import { Menu,type MenuItem } from '../../components/ui/Menu';
import { EmptyState,ErrorNotice,ErrorState,Skeleton,SkeletonRows } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { useAssistant,useAssistantPageContext } from '../../features/assistant/AssistantProvider';
import { runOutputRef,runRef } from '../../features/assistant/refs';
import { AiCallLedger } from '../../features/cad/AiCallLedger';
import { RunCallsMeta } from '../../features/cad/RunCallsMeta';
import { ExecutionContextLine,ObjectStateBadge } from '../../features/orchestration/StateBadge';
import { runDeliveryState,runExecutionContext } from '../../features/orchestration/status';
import { RerunDialog } from '../../features/templates/RerunDialog';
import { RunContextMeta,RunIoPanels,RunPurpose } from '../../features/templates/RunContext';
import { SaveRunTemplateAction } from '../../features/templates/SaveRunTemplateAction';
import '../../features/templates/templates.css';
import { policyOf,runShowsUnverified } from '../../features/turbo/model';
import { PolicySourceTag } from '../../features/turbo/PolicySource';
import { AssumptionList } from '../../features/turbo/Turbo';
import { RunStatusChip } from '../../features/workflow/icons';
import { nodeDisplayName,nodeTypeLabel } from '../../features/workflow/labels';
import { useLookups } from '../../features/workflow/lookups';
import { ProgressTimeline } from '../../features/progress/ProgressTimeline';
import { ProgressResults } from '../../features/progress/ProgressResults';
import { defMap,durationBetween,formatDuration,TERMINAL_RUN } from '../../features/workflow/model';
import { isSimulated,SimulatedBadge } from '../../features/workflow/run/Artifacts';
import { NodeDrawer } from '../../features/workflow/run/NodeDrawer';
import { ReviewPanel } from '../../features/workflow/run/Review';
import { RunCanvas } from '../../features/workflow/run/RunCanvas';
import { Timeline } from '../../features/workflow/run/Timeline';
import { useLiveRun } from '../../features/workflow/run/useLiveRun';
import '../../features/workflow/workflow.css';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { useMediaQuery } from '../../prefs/ThemeProvider';

export default function RunPage() {
  const tasksEnabled = useFeature('tasks');
  const collaborationEnabled = useFeature('collaboration');
  const { t, fmt } = useI18n();
  const toast = useToast();
  const navigate = useNavigate();
  const { runId = '' } = useParams();
  const [search, setSearch] = useSearchParams();
  const live = useLiveRun(runId, { progress: true });
  const run = live.run;
  const projectId = run?.project_id;
  const project = useResource((signal) => getProject(projectId!, { signal }), [projectId], { enabled: !!projectId });
  const part = useResource((signal) => getPart(run!.part_id!, { signal }), [run?.part_id], { enabled: !!run?.part_id });
  const defsRes = useResource((signal) => listWorkflowNodeTypes({ signal }), []);
  const defs = useMemo(() => defMap(defsRes.data ?? []), [defsRes.data]);
  const lookups = useLookups(projectId);
  const narrow = useMediaQuery('(max-width: 900px)');
  const [view, setView] = useState<'canvas' | 'list' | null>(null);
  const effectiveView = view ?? (narrow ? 'list' : 'canvas');
  const [cancelOpen, setCancelOpen] = useState(false);
  const [cancelBusy, setCancelBusy] = useState(false);
  const [cancelError, setCancelError] = useState<unknown>(null);
  const [rerunOpen, setRerunOpen] = useState(false);
  const pendingRef = useRef<HTMLElement>(null);
  const selected = search.get('node');
  const setSelected = (id: string | null) => {
    const next = new URLSearchParams(search);
    if (id) next.set('node', id);
    else next.delete('node');
    setSearch(next, { replace: true });
  };

  useShellProject(project.data);
  useCrumbs([
    { label: t('area.flows'), to: paths.flows() },
    ...(project.data ? [{ label: t('tpl.nav.monitor'), to: paths.runs(project.data.id) }] : []),
    ...(run ? [{ label: run.title || t('wf.run.untitled') }] : []),
  ]);

  // Tick for the header duration while the run is active.
  const [now, setNow] = useState(Date.now());
  const active = !!run && !TERMINAL_RUN.has(run.status);
  useEffect(() => {
    if (!active) return;
    const h = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(h);
  }, [active]);

  // Offer the run to the assistant (never added automatically).
  const assistant = useAssistant();
  const runTitle = run?.title || t('wf.run.untitled');
  useAssistantPageContext(
    run
      ? {
          projectId: run.project_id,
          label: t('ar.run.scope', { title: runTitle }),
          ...(run.task ? { taskId: run.task.id } : {}),
          ...(run.part_id ? { partId: run.part_id } : {}),
          object: { type: 'run', id: run.id, version: run.id },
          target: { ...runRef(run.project_id, run.id), ...(selected ? { node: { node_id: selected } } : {}) },
        }
      : null,
  );

  if (live.error && !run) {
    if (isApiError(live.error) && live.error.status === 404) {
      return (
        <div className="page">
          <EmptyState title={t('wf.run.notFound.title')} body={t('wf.run.notFound.body')} />
        </div>
      );
    }
    return (
      <div className="page">
        <ErrorState error={live.error} onRetry={live.reload} />
      </div>
    );
  }
  if (!run) {
    return (
      <div className="page">
        <Skeleton w="40%" h={24} />
        <SkeletonRows rows={6} cols={4} />
      </div>
    );
  }

  const caps = projectCapabilities(project.data);
  const waiting = run.nodes.filter((n) => n.status === 'waiting_human' && n.review);
  const graphNode = (id: string) => run.graph.nodes.find((n) => n.id === id);
  const nameOf = (id: string) => {
    const g = graphNode(id);
    return g ? nodeDisplayName(t, g) : id;
  };
  const failed = run.nodes.filter((n) => n.status === 'failed');
  const turbo = policyOf(run) === 'turbo';
  const simulated = run.nodes.some((n) => isSimulated(n.outputs, n.steps));
  const dur = durationBetween(run.started_at ?? run.created_at, run.finished_at, now);
  const canCancel = caps.workflow && !TERMINAL_RUN.has(run.status) && !run.cancel_requested;
  // Reruns need a workflow template (workspace runs have none); the server checks the rest.
  const canRerun = caps.workflow && run.origin === 'workflow' && !!run.workflow_id
    && (tasksEnabled || !(run.task_id || run.task))
    && (collaborationEnabled || !run.graph.nodes.some((node) => node.type === 'business_task'));
  const runError = run.error && typeof run.error === 'object' ? (run.error as { code?: string; message?: string }) : null;

  const doCancel = async () => {
    setCancelBusy(true);
    setCancelError(null);
    try {
      live.setRun(await cancelRun(run.id));
      toast(t('wf.run.cancelledToast'));
      setCancelOpen(false);
    } catch (e) {
      setCancelError(e);
    } finally {
      setCancelBusy(false);
    }
  };

  const liveMode = TERMINAL_RUN.has(run.status) ? 'idle' : live.mode;

  // "Add to AI": the run itself, or one exact output row.
  const taskId = run.task?.id ?? null;
  const outputs = run.outputs ?? [];
  const aiItems: MenuItem[] = [
    {
      id: 'run',
      label: t('ar.run.whole', { title: runTitle }),
      icon: <PlayCircle />,
      onSelect: () => void assistant.addContext(runRef(run.project_id, run.id), { taskId }),
    },
    ...outputs.map((o, i) => {
      const missing = !!o.version?.missing;
      const asset = missing ? t('ar.run.outputMissing') : `${o.version?.asset_name ?? ''} ${o.version?.label ?? ''}`.trim() || o.asset_version_id.slice(0, 8);
      return {
        id: `out:${o.node_id}:${o.output_key}:${o.attempt}`,
        label: t(o.attempt > 1 ? 'ar.run.outputAttempt' : 'ar.run.output', { node: nameOf(o.node_id), asset, n: o.attempt }),
        icon: <PackageOpen />,
        separator: i === 0,
        disabled: missing,
        onSelect: () => void assistant.addContext(runOutputRef(run.project_id, run.id, o), { taskId }),
      };
    }),
  ];
  const counts = run.node_counts;
  const total = run.nodes.length || 1;

  return (
    <div className="wf-run-page">
      <header className="wf-run-head">
        <div className="wf-run-title-row">
          <Link
            to={run.task ? paths.runs(run.project_id, { task: run.task.id }) : paths.runs(run.project_id)}
            className="btn ghost icon sm"
            aria-label={t('tpl.run.backToMonitor')}
            title={t('tpl.run.backToMonitor')}
          >
            <ArrowLeft />
          </Link>
          <h1 className="ellip">{run.title || t('wf.run.untitled')}</h1>
          <RunStatusChip status={run.status} large />
          {/* generation reliability N: the state of what the run delivered, apart from the run / check status */}
          <ObjectStateBadge state={runDeliveryState(run)} />
          {turbo && <PolicyChip policy="turbo" />}
          {turbo && <PolicySourceTag source={run.policy_source} sourceVersion={run.policy_source_version} />}
          {/* S6: only a run that succeeded reads "done · not verified"; a partial run is failed (products show their own states). */}
          {runShowsUnverified(run) && <UnverifiedChip />}
          <span className={`wf-origin ${run.origin}`}>
            {run.origin === 'workspace' ? <Bot aria-hidden /> : <WorkflowIcon aria-hidden />} {t(`wf.origin.${run.origin}`)}
          </span>
          {simulated && <SimulatedBadge />}
          <span className="grow" />
          <span className={`wf-live ${liveMode}`} title={t(`wf.live.${liveMode}Title`)}>
            <Radio aria-hidden /> {t(`wf.live.${liveMode}`)}
          </span>
          <button type="button" className="btn sm icon" onClick={live.reload} aria-label={t('wf.run.refresh')} title={t('wf.run.refresh')}>
            <RefreshCw />
          </button>
          <Menu
            label={t('ar.run.menuLabel')}
            triggerClassName="btn sm"
            trigger={
              <>
                <MessageSquarePlus aria-hidden /> {t('ar.run.menu')}
              </>
            }
            header={outputs.length ? undefined : t('ar.run.noOutputs')}
            items={aiItems}
          />
          {run.id === runId && <SaveRunTemplateAction key={run.id} project={project.data} run={run} />}
          {canRerun && (
            <button type="button" className="btn sm" onClick={() => setRerunOpen(true)} title={t('tpl.rerun.hint')}>
              <RotateCw /> {t('tpl.rerun.action')}
            </button>
          )}
          {canCancel && (
            <button type="button" className="btn sm danger" onClick={() => setCancelOpen(true)}>
              <CircleStop /> {t('wf.run.cancel')}
            </button>
          )}
        </div>
        <ExecutionContextLine ctx={runExecutionContext(run)} />
        <dl className="wf-run-meta">
          <RunContextMeta run={run} />
          <RunCallsMeta run={run} />
          <div>
            <dt>{t('wf.run.meta.part')}</dt>
            <dd>
              {run.part_id ? (
                <Link to={paths.part(run.part_id)} className="linkish mono">
                  {part.data ? `${part.data.number} · ${part.data.name}` : run.part_id.slice(0, 8)}
                </Link>
              ) : (
                <span className="faint">—</span>
              )}
            </dd>
          </div>
          <div>
            <dt>{t('wf.run.meta.created')}</dt>
            <dd title={fmt.dateTime(run.created_at)}>{fmt.dateTime(run.created_at)}</dd>
          </div>
          <div>
            <dt>{t('wf.run.meta.started')}</dt>
            <dd>{fmt.dateTime(run.started_at)}</dd>
          </div>
          <div>
            <dt>{t('wf.run.meta.finished')}</dt>
            <dd>{fmt.dateTime(run.finished_at)}</dd>
          </div>
          <div>
            <dt>{t('wf.run.meta.duration')}</dt>
            <dd className="mono">{dur === null ? '—' : formatDuration(dur)}</dd>
          </div>
        </dl>
        <div className="wf-progress" aria-label={t('wf.run.progress')}>
          {(['succeeded', 'running', 'ready', 'waiting_human', 'failed', 'cancelled', 'skipped', 'pending'] as const).map((s) =>
            counts[s] ? <span key={s} className={`seg ns-${s}`} style={{ flexGrow: counts[s] }} title={`${t(`wf.nstatus.${s}`)}: ${counts[s]}`} /> : null,
          )}
        </div>
        <p className="faint small wf-progress-legend">
          {t('wf.run.progressText', { done: counts.succeeded ?? 0, total })}
          {failed.length ? ` · ${t('wf.run.failedCount', { n: failed.length })}` : ''}
          {waiting.length ? ` · ${t('wf.run.waitingCount', { n: waiting.length })}` : ''}
        </p>
        <RunPurpose run={run} />
      </header>

      {run.cancel_requested && !TERMINAL_RUN.has(run.status) && (
        <div className="callout warn slim">
          <Ban aria-hidden />
          <span>{t('wf.run.cancelling')}</span>
        </div>
      )}
      {turbo && (
        <div className="callout warn slim" role="note">
          <TriangleAlert aria-hidden />
          <span>{t('tb.run.note')}</span>
        </div>
      )}
      {run.status === 'partial' && (
        <div className="callout err slim" role="status">
          <TriangleAlert aria-hidden />
          <span>{t('tb.run.partialNote')}</span>
        </div>
      )}
      {(run.assumptions ?? []).length > 0 && (
        <details className="panel">
          <summary className="panel-h">
            {t('tb.assumptions.title')} ({(run.assumptions ?? []).length})
          </summary>
          <div className="panel-b">
            <AssumptionList items={run.assumptions} />
          </div>
        </details>
      )}
      {runError && (runError.message || runError.code) && (
        <div className="callout err slim">
          <TriangleAlert aria-hidden />
          <span>
            {runError.message} {runError.code && <code className="error-code">{runError.code}</code>}
          </span>
        </div>
      )}
      {live.error && run ? <ErrorNotice error={live.error} onRetry={live.reload} /> : null}

      {waiting.length > 0 && (
        <section className="wf-pending" ref={pendingRef} aria-labelledby="wf-pending-h" tabIndex={-1}>
          <h2 id="wf-pending-h">
            <Hand aria-hidden /> {t('wf.pending.title', { n: waiting.length })}
          </h2>
          <div className="wf-pending-list">
            {waiting.map((n) => (
              <article key={n.node_id} className={`wf-pending-card ${selected === n.node_id ? 'selected' : ''}`}>
                <header>
                  <button type="button" className="linkish strong" onClick={() => setSelected(n.node_id)}>
                    {nameOf(n.node_id)}
                  </button>
                  <span className="faint small">
                    {nodeTypeLabel(t, n.node_type)} · <span className="mono">{n.node_id}</span>
                  </span>
                  <span className="grow" />
                  <span className="faint small">{t('wf.tl.waitingSince', { time: fmt.time(n.updated_at) })}</span>
                </header>
                <ReviewPanel run={run} node={n} lookups={lookups} onRun={live.setRun} onReload={live.reload} />
              </article>
            ))}
          </div>
        </section>
      )}

      <RunIoPanels run={run} />

      {/* The execution process: each stage's work units (parts, spec chunks …), model calls and decisions. */}
      {!!live.progressError && <p className="prog-note" role="status">{t('prog.loadError')} <button type="button" className="linkish" onClick={live.reload}>{t('common.retry')}</button></p>}
      {live.progress && <ProgressResults progress={live.progress} />}
      {live.progress && live.progress.stages.some((s) => s.units.length > 0 || s.calls.count > 0) && (
        <ProgressTimeline progress={live.progress} variant="full" />
      )}

      <div className="wf-run-toolbar">
        <h2 className="wf-section-h">{t('wf.run.stages')}</h2>
        <span className="grow" />
        <div className="seg" role="group" aria-label={t('wf.run.view')}>
          <button type="button" className="seg-btn" aria-pressed={effectiveView === 'canvas'} onClick={() => setView('canvas')}>
            <Network /> {t('wf.run.viewCanvas')}
          </button>
          <button type="button" className="seg-btn" aria-pressed={effectiveView === 'list'} onClick={() => setView('list')}>
            <LayoutList /> {t('wf.run.viewList')}
          </button>
        </div>
      </div>

      <div className={`wf-run-body view-${effectiveView} ${selected ? 'has-drawer' : ''}`}>
        {effectiveView === 'canvas' && (
          <div className="wf-run-canvas">
            {defsRes.data ? <RunCanvas run={run} defs={defs} lookups={lookups} selected={selected} onSelect={setSelected} /> : <div className="wf-canvas-skel" />}
          </div>
        )}
        <div className="wf-run-side">
          <Timeline run={run} selected={selected} onSelect={setSelected} />
        </div>
        {selected && run.nodes.some((n) => n.node_id === selected) && (
          <NodeDrawer
            run={run}
            nodeId={selected}
            defs={defs}
            canAct={caps.workflow}
            onClose={() => setSelected(null)}
            onRun={live.setRun}
            onGoToPending={() => {
              pendingRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
              pendingRef.current?.focus({ preventScroll: true });
            }}
          />
        )}
      </div>

      {/* Model-call ledger (every attempt, failed calls included); loaded when opened. */}
      {(run.nodes.some((n) => defs.get(n.node_type)?.uses_ai) || !defsRes.data) && <AiCallLedger runId={run.id} refreshKey={`${run.status}:${run.nodes.map((n) => n.status).join(',')}`} />}

      {canRerun && rerunOpen && (
        <RerunDialog
          run={run}
          onClose={() => setRerunOpen(false)}
          onStarted={(r) => {
            setRerunOpen(false);
            toast(t('tpl.rerun.started'));
            navigate(paths.run(r.id));
          }}
        />
      )}
      {cancelOpen && (
        <ConfirmDialog
          title={t('wf.run.cancelTitle')}
          body={t('wf.run.cancelBody')}
          confirmLabel={t('wf.run.cancel')}
          danger
          busy={cancelBusy}
          error={<ErrorNotice error={cancelError} />}
          onConfirm={() => void doCancel()}
          onClose={() => {
            setCancelOpen(false);
            setCancelError(null);
          }}
        />
      )}
    </div>
  );
}

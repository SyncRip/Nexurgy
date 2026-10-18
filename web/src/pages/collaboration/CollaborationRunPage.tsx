import { useFeature } from '../../api/capabilities';
// /collaboration/runs/:runId — collaboration workbench (UI02, design §5.2):
// title, mode badge, pause / resume, run record link and server-side stats;
// task graph (React Flow, lazy) or list sharing one server snapshot; the node
// detail panel (?node=); the "resources of this instance" bar. Polls
// GET /runs/{id}/collaboration every 10 s while the tab is visible.

import '../../features/workflow/workflow.css';
import '../../features/collaboration/collaboration.css';
import '../../features/tasks/tasks.css';
import { ArrowLeft, Ban, BookOpen, ChevronRight, CircleCheck, CircleStop, CircleX, Clock, FileText, GitFork, Hand, LayoutList, ListChecks, Network, PauseCircle, PlayCircle, RefreshCw, Shapes, ShieldQuestion, TriangleAlert } from 'lucide-react';
import { lazy, Suspense, useMemo, useState } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { isApiError } from '../../api/client';
import {
  forkSemiAuto,
  getCollaboration,
  pauseCollaboration,
  RESOURCE_KINDS,
  resumeCollaboration,
  type CollaborationOut,
  type NodeTaskActionOut,
} from '../../api/collaboration';
import { cancelRun, getProject, getRun } from '../../api/endpoints';
import { paths } from '../../app/nav';
import { useCrumbs, useShellProject } from '../../components/shell/ShellContext';
import { Chip } from '../../components/ui/Badges';
import { Dialog } from '../../components/ui/Dialog';
import { Field } from '../../components/ui/Form';
import { EmptyState, ErrorNotice, ErrorState, Skeleton, SkeletonRows } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { CL_ERRORS, ModeChip, PausedChip, useVisiblePoll } from '../../features/collaboration/common';
import { failedNodeCounts, idempotencyKey, newSeed } from '../../features/collaboration/model';
import { CollabError } from '../../features/collaboration/NodeActions';
import { NodeDetail } from '../../features/collaboration/NodeDetail';
import { NodeTaskCard } from '../../features/collaboration/NodeTaskCard';
import { useMembers } from '../../features/tasks/common';
import { PolicyChip, UnverifiedChip } from '../../components/engineering/EngineeringChips';
import { policyOf, runShowsUnverified } from '../../features/turbo/model';
import { AssumptionList } from '../../features/turbo/Turbo';
import { RunStatusChip } from '../../features/workflow/icons';
import { TERMINAL_RUN } from '../../features/workflow/model';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { useMediaQuery } from '../../prefs/ThemeProvider';

const CollaborationCanvas = lazy(() => import('../../features/collaboration/BusinessTaskNode'));

const POLL_MS = 10_000;

function PauseDialog({ data, onClose, onDone }: { data: CollaborationOut; onClose: () => void; onDone: (c: CollaborationOut) => void }) {
  const { t } = useI18n();
  const paused = !!data.collaboration.paused_at;
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const out = paused
        ? await resumeCollaboration(data.run.id, { expected_version: data.collaboration.control_version })
        : await pauseCollaboration(data.run.id, { expected_version: data.collaboration.control_version, reason: reason.trim() });
      onDone(out);
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  };
  return (
    <Dialog
      title={t(paused ? 'collab.run.resumeTitle' : 'collab.run.pauseTitle')}
      onClose={onClose}
      busy={busy}
      size="sm"
      onSubmit={() => void submit()}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={busy}>
            {busy ? t('common.working') : t(paused ? 'collab.run.resume' : 'collab.run.pause')}
          </button>
        </>
      }
    >
      <div className="stack-sm">
        <p className="muted">{t(paused ? 'collab.run.resumeBody' : 'collab.run.pauseBody')}</p>
        {!paused && (
          <Field label={t('tk.reason')} optional>
            <textarea className="input" rows={2} maxLength={2000} value={reason} onChange={(e) => setReason(e.target.value)} />
          </Field>
        )}
        <CollabError error={error} onReload={onClose} />
      </div>
    </Dialog>
  );
}

function ForkDialog({ data, onClose, onDone }: { data: CollaborationOut; onClose: () => void; onDone: (c: CollaborationOut) => void }) {
  const { t } = useI18n();
  const members = useMembers(data.run.project_id);
  const [reviewer, setReviewer] = useState('');
  const [reason, setReason] = useState('');
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [seed] = useState(newSeed);
  const people = (members.list ?? []).filter((m) => m.role !== 'viewer');
  const submit = async () => {
    setTouched(true);
    if (!reviewer || !reason.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const key = idempotencyKey('fork', seed, { run: data.run.id, reviewer, reason: reason.trim() });
      onDone(await forkSemiAuto(data.run.id, { expected_version: data.collaboration.control_version, reviewer_user_id: reviewer, idempotency_key: key, reason: reason.trim() }));
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  };
  return (
    <Dialog
      title={t('collab.run.forkTitle')}
      subtitle={t('collab.run.forkSubtitle')}
      onClose={onClose}
      busy={busy}
      onSubmit={() => void submit()}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={busy}>
            <GitFork /> {busy ? t('common.working') : t('collab.run.fork')}
          </button>
        </>
      }
    >
      <div className="stack">
        <p className="muted small">{t('collab.run.forkBody')}</p>
        <Field label={t('collab.run.forkReviewer')} error={touched && !reviewer ? t('validation.required', { field: t('collab.run.forkReviewer') }) : null}>
          <select className="select" value={reviewer} onChange={(e) => setReviewer(e.target.value)} disabled={!members.list}>
            <option value="">{members.list ? t('collab.form.choose') : t('common.loading')}</option>
            {people.map((m) => (
              <option key={m.id} value={m.id}>
                {m.name} · {t(`role.${m.role}`)}
              </option>
            ))}
          </select>
        </Field>
        <Field label={t('tk.reason')} error={touched && !reason.trim() ? t('validation.required', { field: t('tk.reason') }) : null}>
          <textarea className="input" rows={2} maxLength={2000} value={reason} onChange={(e) => setReason(e.target.value)} />
        </Field>
        {members.error ? <ErrorNotice error={members.error} onRetry={members.reload} /> : null}
        <CollabError error={error} onReload={onClose} />
      </div>
    </Dialog>
  );
}

export default function CollaborationRunPage() {
  const collaborationEnabled = useFeature('collaboration');
  const { t, fmt } = useI18n();
  const toast = useToast();
  const navigate = useNavigate();
  const { runId = '' } = useParams();
  const [search, setSearch] = useSearchParams();
  const res = useResource((signal) => getCollaboration(runId, { signal }), [runId]);
  // Never show another run's data while this one loads.
  const data = res.data && res.data.run.id === runId ? res.data : undefined;
  const projectId = data?.run.project_id;
  const project = useResource((signal) => getProject(projectId!, { signal }), [projectId], { enabled: !!projectId });
  // The frozen graph (edges) comes from the run snapshot; loaded once per run.
  const graphRes = useResource((signal) => getRun(runId, { signal }), [runId], { enabled: !!data });
  const narrow = useMediaQuery('(max-width: 900px)');
  const [view, setView] = useState<'graph' | 'list' | null>(null);
  const effectiveView = view ?? (narrow ? 'list' : 'graph');
  const [dialog, setDialog] = useState<'pause' | 'fork' | 'cancel' | null>(null);
  const [cancelBusy, setCancelBusy] = useState(false);
  const [cancelError, setCancelError] = useState<unknown>(null);
  const selected = search.get('node');
  const setSelected = (id: string | null) => {
    const next = new URLSearchParams(search);
    if (id) next.set('node', id);
    else next.delete('node');
    setSearch(next, { replace: true });
  };

  const terminal = !!data && TERMINAL_RUN.has(data.run.status);
  const keepPolling = !!data && (!terminal || data.collaboration.archive_status === 'pending');
  useVisiblePoll(res.reload, POLL_MS, keepPolling);

  useShellProject(project.data);
  useCrumbs([
    { label: t('area.flows'), to: paths.flows() },
    ...(projectId ? [{ label: t('nav.collaboration'), to: paths.collaboration(projectId) }] : []),
    ...(data ? [{ label: data.run.title || t('collab.run.untitled') }] : []),
  ]);

  const seqOf = useMemo(() => new Map((data?.node_tasks ?? []).map((n, i) => [n.node_id, i + 1])), [data?.node_tasks]);

  if (res.error && !data) {
    if (isApiError(res.error) && res.error.status === 404) {
      return (
        <div className="page">
          <EmptyState
            title={t('collab.run.notFound')}
            body={t('collab.run.notFoundBody')}
            action={
              <Link className="btn" to={paths.run(runId)}>
                <FileText /> {t('collab.run.openRecord')}
              </Link>
            }
          />
        </div>
      );
    }
    return (
      <div className="page">
        <ErrorState error={res.error} onRetry={res.reload} overrides={CL_ERRORS} />
      </div>
    );
  }
  if (!data) {
    return (
      <div className="page">
        <Skeleton w="40%" h={24} />
        <SkeletonRows rows={6} cols={4} />
      </div>
    );
  }

  const c = data.collaboration;
  const mode = c.execution_mode;
  const turbo = policyOf(c) === 'turbo';
  const paused = !!c.paused_at;
  const p = data.progress;
  // S6: failed nodes never count as done — a Turbo node whose check failed leaves the server's "unverified" count.
  const failedN = failedNodeCounts(data.node_tasks);
  const unverifiedN = Math.max(0, (p.unverified ?? 0) - failedN.checkFailed);
  const current = selected ? data.node_tasks.find((n) => n.node_id === selected) ?? null : null;
  const canPause = data.actions.includes(paused ? 'resume' : 'pause');
  const canFork = collaborationEnabled && data.actions.includes('fork_semi_auto');
  const canCancel = data.actions.includes('cancel') && !terminal;

  const onNode = (n: NodeTaskActionOut) => {
    res.setData((prev) => (prev ? { ...prev, node_tasks: prev.node_tasks.map((x) => (x.node_id === n.node_id ? { ...x, ...n } : x)) } : (prev as unknown as CollaborationOut)));
    // Progress / activity / other nodes may change too: refresh the whole snapshot.
    res.reload();
  };

  const doCancel = async () => {
    setCancelBusy(true);
    setCancelError(null);
    try {
      await cancelRun(data.run.id);
      toast(t('wf.run.cancelledToast'));
      setDialog(null);
      res.reload();
    } catch (e) {
      setCancelError(e);
    } finally {
      setCancelBusy(false);
    }
  };

  return (
    <div className="cl-run">
      <header className="cl-run-head">
        <div className="cl-run-title">
          <Link to={paths.collaboration(data.run.project_id)} className="btn ghost icon sm" aria-label={t('collab.run.back')} title={t('collab.run.back')}>
            <ArrowLeft />
          </Link>
          <h1 className="ellip" title={data.run.title}>
            {data.run.title || t('collab.run.untitled')}
            <span className="faint"> · {t('collab.run.workbench')}</span>
          </h1>
          <ModeChip mode={mode} large />
          {turbo && <PolicyChip policy="turbo" />}
          <RunStatusChip status={data.run.status} />
          {/* S6: run level reads "done · not verified" only once the run succeeded; a failed run never says "done". */}
          {runShowsUnverified({ status: data.run.status, execution_policy: c.execution_policy }) && unverifiedN > 0 && <UnverifiedChip />}
          {paused && <PausedChip />}
          <span className="grow" />
          <button type="button" className="btn sm icon" onClick={res.reload} aria-label={t('wf.run.refresh')} title={t('wf.run.refresh')} disabled={res.loading}>
            <RefreshCw />
          </button>
          {canPause && (
            <button type="button" className="btn sm" onClick={() => setDialog('pause')}>
              {paused ? <PlayCircle /> : <PauseCircle />} {t(paused ? 'collab.run.resume' : 'collab.run.pause')}
            </button>
          )}
          {canFork && (
            <button type="button" className="btn sm" onClick={() => setDialog('fork')} title={t('collab.run.forkSubtitle')}>
              <GitFork /> {t('collab.run.fork')}
            </button>
          )}
          <Link className="btn sm" to={paths.run(data.run.id)}>
            <FileText /> {t('collab.run.openRecord')}
          </Link>
          {canCancel && (
            <button type="button" className="btn sm danger" onClick={() => setDialog('cancel')}>
              <CircleStop /> {t('wf.run.cancel')}
            </button>
          )}
        </div>
        <ul className="cl-stats" aria-label={t('collab.run.stats')}>
          <li className="cl-stat">
            <ListChecks aria-hidden /> {t('collab.run.statTotal', { n: fmt.number(p.total) })}
          </li>
          <li className="cl-stat">
            <Clock aria-hidden /> {t('collab.run.statExecuted')} <b>{p.executed}/{p.total}</b>
          </li>
          <li className="cl-stat ok">
            <CircleCheck aria-hidden /> {t('collab.run.statAccepted')} <b>{p.accepted}/{p.total}</b>
          </li>
          {turbo && (
            <li className="cl-stat warn">
              <ShieldQuestion aria-hidden /> {t('tb.progress.unverified', { n: unverifiedN })}
            </li>
          )}
          {failedN.failed > 0 && (
            <li className="cl-stat err">
              <CircleX aria-hidden /> {t('ver.stat.failed')} <b>{failedN.failed}</b>
            </li>
          )}
          <li className={`cl-stat ${p.blocked ? 'err' : ''}`}>
            <Hand aria-hidden /> {t('collab.run.statBlocked')} <b>{p.blocked}</b>
          </li>
          {c.forked_from_run_id && (
            <li className="cl-stat">
              <GitFork aria-hidden />
              <Link to={paths.collaborationRun(c.forked_from_run_id)}>{t('collab.run.forkedFrom')}</Link>
            </li>
          )}
        </ul>
        <p className="faint small">{turbo ? t('tb.run.note') : t(mode === 'full_auto' ? 'collab.run.fullAutoHint' : 'collab.run.semiAutoHint')}</p>
      </header>
      {data.run.status === 'partial' && (
        <div className="callout err slim" role="status">
          <TriangleAlert aria-hidden />
          <span>{t('tb.run.partialNote')}</span>
        </div>
      )}
      {(c.assumptions ?? []).length > 0 && (
        <details className="panel">
          <summary className="panel-h">
            {t('tb.assumptions.title')} ({(c.assumptions ?? []).length})
          </summary>
          <div className="panel-b">
            <AssumptionList items={c.assumptions} />
          </div>
        </details>
      )}

      {paused && (
        <div className="callout warn slim" role="status">
          <PauseCircle aria-hidden />
          <span>
            {t('collab.run.pausedAt', { time: fmt.dateTime(c.paused_at) })}
            {c.pause_reason ? ` · ${c.pause_reason}` : ''}
          </span>
        </div>
      )}
      {data.run.cancel_requested && !terminal && (
        <div className="callout warn slim">
          <Ban aria-hidden />
          <span>{t('wf.run.cancelling')}</span>
        </div>
      )}
      {c.archive_status === 'failed' && (
        <div className="callout err slim" role="alert">
          <TriangleAlert aria-hidden />
          <span>{t('collab.run.archiveFailed')}</span>
        </div>
      )}
      {res.error ? <ErrorNotice error={res.error} onRetry={res.reload} overrides={CL_ERRORS} /> : null}

      <div className="cl-run-toolbar">
        <h2 className="wf-section-h">{t('collab.run.tasks')}</h2>
        <span className="grow" />
        <div className="seg" role="group" aria-label={t('wf.run.view')}>
          <button type="button" className="seg-btn" aria-pressed={effectiveView === 'graph'} onClick={() => setView('graph')}>
            <Network /> {t('collab.run.viewGraph')}
          </button>
          <button type="button" className="seg-btn" aria-pressed={effectiveView === 'list'} onClick={() => setView('list')}>
            <LayoutList /> {t('collab.run.viewList')}
          </button>
        </div>
      </div>

      <div className={`cl-run-body view-${effectiveView} ${current ? 'has-detail' : ''}`}>
        {!data.node_tasks.length ? (
          <EmptyState compact title={t('collab.run.noTasks')} body={t('collab.run.noTasksBody')} />
        ) : effectiveView === 'graph' ? (
          graphRes.error && !graphRes.data ? (
            <ErrorState error={graphRes.error} title={t('collab.run.graphFailed')} onRetry={graphRes.reload} />
          ) : graphRes.data ? (
            <Suspense fallback={<div className="cl-canvas"><div className="cl-canvas-skel" /></div>}>
              <CollaborationCanvas graph={graphRes.data.graph} tasks={data.node_tasks} seqOf={seqOf} selected={selected} onSelect={setSelected} />
            </Suspense>
          ) : (
            <div className="cl-canvas">
              <div className="cl-canvas-skel" />
            </div>
          )
        ) : (
          <ol className="cl-list" aria-label={t('collab.run.tasks')}>
            {data.node_tasks.map((n, i) => (
              <li key={n.node_id}>
                <NodeTaskCard seq={i + 1} title={n.title || n.node_id} task={n} selected={n.node_id === selected} onSelect={() => setSelected(n.node_id === selected ? null : n.node_id)} />
              </li>
            ))}
          </ol>
        )}
        {current && (
          <NodeDetail key={current.node_id} runId={data.run.id} node={current} mode={mode} activity={data.activity} onClose={() => setSelected(null)} onNode={onNode} onReload={res.reload} />
        )}
      </div>
      {selected && !current && (
        <div className="callout warn slim" role="status">
          <TriangleAlert aria-hidden />
          <span>{t('collab.run.nodeMissing', { node: selected })}</span>
        </div>
      )}

      <section className="cl-resources" aria-label={t('collab.run.resources')}>
        <Shapes aria-hidden />
        <span className="strong">{t('collab.run.resources')}</span>
        <span className="tk-count mono">{fmt.number(data.resources.total)}</span>
        <span className="cl-kinds">
          {RESOURCE_KINDS.filter((k) => (data.resources.by_kind[k] ?? 0) > 0).map((k) => (
            <span key={k} className="tag">
              {t(`collab.resourceKind.${k}`)} {data.resources.by_kind[k]}
            </span>
          ))}
        </span>
        {data.knowledge && (
          <Link className="small" to={paths.knowledge(data.run.project_id, data.knowledge.collection_id)}>
            <BookOpen className="inline-ico" aria-hidden /> {t('collab.run.knowledge')}
          </Link>
        )}
        {c.archive_status !== 'none' && <Chip tone={c.archive_status === 'archived' ? 'ok' : c.archive_status === 'failed' ? 'err' : 'pending'}>{t(`collab.archive.${c.archive_status}`)}</Chip>}
        <span className="grow" />
        <Link className="btn sm" to={paths.assetsTab(data.run.project_id, 'resources', { run_id: data.run.id })}>
          {t('collab.run.viewResources')} <ChevronRight />
        </Link>
      </section>

      {dialog === 'pause' && (
        <PauseDialog
          data={data}
          onClose={() => {
            setDialog(null);
            res.reload();
          }}
          onDone={(out) => {
            setDialog(null);
            // Show the answer now, and reload: that aborts a poll already in flight, whose older
            // snapshot would otherwise overwrite the new state (same as onNode).
            res.setData(out);
            res.reload();
            toast(t(out.collaboration.paused_at ? 'collab.run.pausedToast' : 'collab.run.resumedToast'));
          }}
        />
      )}
      {canFork && dialog === 'fork' && (
        <ForkDialog
          data={data}
          onClose={() => {
            setDialog(null);
            res.reload();
          }}
          onDone={(out) => {
            setDialog(null);
            // The new instance shows at once; the route change reloads it and aborts the old run's in-flight poll.
            res.setData(out);
            res.reload();
            toast(t('collab.run.forkedToast'));
            navigate(paths.collaborationRun(out.run.id));
          }}
        />
      )}
      {dialog === 'cancel' && (
        <Dialog
          title={t('collab.run.cancelTitle')}
          onClose={() => {
            setDialog(null);
            setCancelError(null);
          }}
          busy={cancelBusy}
          size="sm"
          role="alertdialog"
          footer={
            <>
              <button type="button" className="btn" onClick={() => setDialog(null)} disabled={cancelBusy}>
                {t('common.cancel')}
              </button>
              <button type="button" className="btn danger-solid" onClick={() => void doCancel()} disabled={cancelBusy} data-autofocus>
                {cancelBusy ? t('common.working') : t('wf.run.cancel')}
              </button>
            </>
          }
        >
          <div className="stack-sm">
            <p className="muted">{t('collab.run.cancelBody')}</p>
            <ErrorNotice error={cancelError} overrides={CL_ERRORS} />
          </div>
        </Dialog>
      )}
    </div>
  );
}

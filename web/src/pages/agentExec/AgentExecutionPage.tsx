// /agent-executions/:executionId — one agent execution, live while it runs and a full replay afterwards
// (plan §5.5, prototype docs/archive/agent/prototype.html #exec.<id>): identity bar with stop / pause-resume / open in
// node, the timeline on the left (stacked first on phones) and budget, outputs, inputs, attempts and verdict on
// the right. Everything comes from GET /agent-executions/{id} and its event stream; nothing is invented.

import '../../features/collaboration/collaboration.css';
import { Bot, CircleX, Cpu, FlaskConical, Gauge, Hand, Network, PauseCircle, PlayCircle, ShieldCheck, Timer, Workflow } from 'lucide-react';
import { useMemo, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import type { AgentExecution, ExecutionOutput } from '../../api/agentExecutions';
import { pauseAgentExecution, resumeAgentExecution } from '../../api/agentExecutions';
import { isApiError } from '../../api/client';
import { getCollaboration } from '../../api/collaboration';
import { getProject } from '../../api/endpoints';
import { paths } from '../../app/nav';
import { useCrumbs, useShellProject } from '../../components/shell/ShellContext';
import { Chip } from '../../components/ui/Badges';
import { Dialog } from '../../components/ui/Dialog';
import { Field } from '../../components/ui/Form';
import { EmptyState, ErrorNotice, ErrorState, Skeleton, SkeletonRows } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { BudgetMeters, ExecStatusChip, RuntimeChip, SimTag, StopButton, VersionLink, execStatusText } from '../../features/agentExec/common';
import { budgetRows, execDisplay, isExecLive, itemAnchorFor } from '../../features/agentExec/model';
import { Timeline } from '../../features/agentExec/Timeline';
import { spanMs, useExecutionLive, useNow } from '../../features/agentExec/useExecution';
import { AnswerQuestions } from '../../features/collaboration/AnswerQuestions';
import { executionRoleLabel } from '../../features/collaboration/common';
import { formatDuration } from '../../features/workflow/model';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';

function PauseDialog({ ex, resume, controlVersion, onClose, onDone }: { ex: AgentExecution; resume: boolean; controlVersion: number; onClose: () => void; onDone: () => void }) {
  const { t } = useI18n();
  const toast = useToast();
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      if (resume) await resumeAgentExecution(ex.id, { expected_version: controlVersion });
      else await pauseAgentExecution(ex.id, { expected_version: controlVersion, reason: reason.trim() });
      toast(t(resume ? 'ax.act.resumed' : 'ax.act.paused'));
      onDone();
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  };
  return (
    <Dialog
      title={t(resume ? 'ax.act.resumeTitle' : 'ax.act.pauseTitle')}
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
            {busy ? t('common.working') : t(resume ? 'ax.act.resume' : 'ax.act.pause')}
          </button>
        </>
      }
    >
      <div className="stack-sm">
        <p className="muted">{t(resume ? 'ax.act.resumeBody' : 'ax.act.pauseBody')}</p>
        {!resume && (
          <Field label={t('tk.reason')} optional>
            <textarea className="input" rows={2} maxLength={2000} value={reason} onChange={(e) => setReason(e.target.value)} />
          </Field>
        )}
        <ErrorNotice error={error} />
      </div>
    </Dialog>
  );
}

function InputRow({ x }: { x: unknown }) {
  if (x && typeof x === 'object') {
    const o = x as Record<string, unknown>;
    const str = (k: string) => (typeof o[k] === 'string' && o[k] ? (o[k] as string) : null);
    const vid = str('asset_version_id') ?? (o.asset_type ? str('id') : null);
    const name = str('label') ?? str('name') ?? str('title') ?? '';
    return (
      <li>
        {str('slot_key') && <span className="cl-slot">{str('slot_key')}</span>}
        {vid ? <VersionLink versionId={vid} label={name} /> : <span className="ellip">{name || JSON.stringify(o)}</span>}
      </li>
    );
  }
  return <li>{String(x)}</li>;
}

function OutputRow({ o, onJump }: { o: ExecutionOutput; onJump: (() => void) | null }) {
  const { t } = useI18n();
  return (
    <li>
      <VersionLink versionId={o.asset_version_id} label={o.label} />
      {o.path && (
        <span className="mono faint small" title={t('ax.out.workspaceFile')}>
          {o.path}
        </span>
      )}
      {o.state && (
        <Chip tone={o.state === 'formal' || o.state === 'accepted' ? 'ok' : 'pending'} className="cl-chip-sm">
          {o.state}
        </Chip>
      )}
      {onJump && (
        <button type="button" className="ax-linkbtn" onClick={onJump}>
          {t('ax.out.jump')}
        </button>
      )}
    </li>
  );
}

export default function AgentExecutionPage() {
  const { t, fmt } = useI18n();
  const { executionId = '' } = useParams();
  const live = useExecutionLive(executionId);
  const raw = live.detail.data;
  const ex = raw && raw.id === executionId ? raw : undefined;
  const projectId = ex?.project_id;
  const project = useResource((signal) => getProject(projectId!, { signal }), [projectId], { enabled: !!projectId });
  // The run gives the node title, the node task (answering a question) and the control version (pause / resume).
  const collab = useResource((signal) => getCollaboration(ex!.run_id, { signal }), [ex?.run_id], { enabled: !!ex?.run_id });
  const [dialog, setDialog] = useState<'pause' | 'resume' | null>(null);
  const [stopRequested, setStopRequested] = useState(false);
  const [jump, setJump] = useState<{ seq: number; nonce: number } | null>(null);
  const running = !!ex && isExecLive(ex.status) && !live.finished;
  const now = useNow(running);

  useShellProject(project.data);
  useCrumbs([
    { label: t('area.flows'), to: paths.flows() },
    { label: t('ax.crumb'), to: paths.flows('executions', projectId) },
    { label: executionId.slice(0, 8) },
  ]);

  const node = useMemo(() => collab.data?.node_tasks.find((n) => n.node_id === ex?.node_id) ?? null, [collab.data, ex?.node_id]);

  if (live.detail.error && !ex) {
    const e = live.detail.error;
    if (isApiError(e) && (e.status === 404 || e.status === 403))
      return (
        <div className="page">
          <EmptyState title={t('ax.notFound')} body={t('ax.notFoundBody')} />
        </div>
      );
    return (
      <div className="page">
        <ErrorState error={e} onRetry={live.detail.reload} title={isApiError(e) && e.isNotFound ? t('ax.unavailable') : undefined} />
      </div>
    );
  }
  if (!ex) {
    return (
      <div className="page">
        <Skeleton w="40%" h={24} />
        <SkeletonRows rows={6} cols={3} />
      </div>
    );
  }

  const display = execDisplay({ ...ex, stop_requested: stopRequested }, live.events);
  const canStop = ex.allowed_actions.includes('stop') && !stopRequested;
  const canPause = ex.allowed_actions.includes('pause');
  const canResume = ex.allowed_actions.includes('resume');
  const controlVersion = collab.data?.collaboration.control_version;
  const elapsed = spanMs(ex.started_at, running ? null : ex.finished_at, now);
  const lease = ex.lease_until ? Date.parse(ex.lease_until) - now : null;
  const rows = budgetRows(ex.budget, live.used, running ? elapsed : null);
  const nodeTitle = node?.title || ex.node_id;
  const runTitle = collab.data?.run.title || ex.run_id.slice(0, 8);
  const latestOnNode = node ? [...(node.executions ?? [])].sort((a, b) => (a.started_at ?? '').localeCompare(b.started_at ?? '')).pop()?.id === ex.id : false;
  const answerable = !!node && latestOnNode && node.phase === 'blocked' && node.blocked_reason?.code === 'agent_blocked';
  const reload = () => {
    live.detail.reload();
    collab.reload();
  };
  const ws = ex.runtime === 'workspace_agent' ? ex.runtime_options : null;

  return (
    <div className="page ax-page">
      {ex.simulated && (
        <div className="callout warn slim" role="note">
          <FlaskConical aria-hidden />
          <span>{t('ax.sim.banner')}</span>
        </div>
      )}
      <section className="panel ax-head">
        <div className="ax-head-top">
          <div className="ax-head-titles">
            <div className="ax-title-row">
              <h1>{nodeTitle}</h1>
              <ExecStatusChip display={display} />
              {ex.simulated && <SimTag />}
            </div>
            <div className="ax-meta">
              <span>
                <Bot aria-hidden />
                {ex.agent ? <Link to={paths.agentProfile(ex.project_id, ex.agent.id)}>{ex.agent.name}</Link> : '—'}
                {ex.agent?.version != null && <span className="mono faint">v{ex.agent.version}</span>}· {executionRoleLabel(t, ex.role)}
              </span>
              <span>
                <Workflow aria-hidden />
                <Link to={paths.collaborationRun(ex.run_id)} className="ax-run-title" title={runTitle}>{runTitle}</Link>
              </span>
              <span>
                <RuntimeChip runtime={ex.runtime} small />
              </span>
              <span>
                <Cpu aria-hidden />
                {ex.model?.model_id ? <span className="mono">{ex.model.model_id}</span> : <span className="faint">{t('ax.head.modelDefault')}</span>}
              </span>
              <span className="mono">
                {ex.id.slice(0, 8)} · {t('ax.head.attempt', { a: ex.attempt_no, r: ex.rework_no })}
              </span>
            </div>
          </div>
          <div className="ax-actions">
            {canPause && (
              <button type="button" className="btn sm" onClick={() => setDialog('pause')} disabled={controlVersion === undefined}>
                <PauseCircle /> {t('ax.act.pause')}
              </button>
            )}
            {canResume && (
              <button type="button" className="btn sm" onClick={() => setDialog('resume')} disabled={controlVersion === undefined}>
                <PlayCircle /> {t('ax.act.resume')}
              </button>
            )}
            {canStop && (
              <StopButton
                executionId={ex.id}
                onStopped={() => {
                  setStopRequested(true);
                  live.detail.reload();
                }}
              />
            )}
            <Link className="btn sm" to={paths.collaborationRun(ex.run_id, ex.node_id)}>
              <Network /> {t('ax.act.openNode')}
            </Link>
          </div>
        </div>
        <div className="ax-meta">
          {running && (
            <span>
              <i className="ax-pulse" aria-hidden /> {execStatusText(t, display)}
            </span>
          )}
          <span>{t('ax.head.started', { at: fmt.dateTime(ex.started_at) })}</span>
          {!running && ex.finished_at && <span>{t('ax.head.finished', { at: fmt.dateTime(ex.finished_at) })}</span>}
          {elapsed !== null && (
            <span>
              <Timer aria-hidden /> {t('ax.head.elapsed', { t: formatDuration(elapsed) })}
            </span>
          )}
          {running && lease !== null && <span className="mono">{lease > 0 ? t('ax.head.lease', { t: formatDuration(lease) }) : t('ax.head.leaseExpired')}</span>}
          {ex.runtime_impl && <span className="mono faint">{t('ax.side.runtimeImpl', { impl: ex.runtime_impl })}</span>}
          {ws && (
            <span>
              <ShieldCheck aria-hidden /> {t(ws.confirmation === 'risky' ? 'ax.ws.confirmRisky' : 'ax.ws.confirmNever')} · {t('ax.ws.networkOff')}
            </span>
          )}
        </div>
        {display === 'budget' && (
          <div className="callout err slim" role="note">
            <Gauge aria-hidden />
            <span>
              {t('collab.blocked.budget_exceeded')} <code className="error-code">budget_exceeded</code>
            </span>
          </div>
        )}
      </section>

      <div className="ax-grid">
        <Timeline
          items={live.items}
          state={live.state}
          streamError={live.streamError}
          jumpTo={jump}
          questionExtra={
            answerable && node ? (
              <AnswerQuestions
                runId={ex.run_id}
                node={node}
                onAnswered={() => {
                  reload();
                }}
              />
            ) : undefined
          }
        />
        <aside className="ax-side">
          <section className="panel">
            <div className="panel-h">
              <h3>{t('ax.side.budget')}</h3>
              <span className="grow" />
              <span className="faint small">{t('ax.side.budgetAside')}</span>
            </div>
            <div className="panel-b">
              <BudgetMeters rows={rows} />
            </div>
          </section>

          <section className="panel">
            <div className="panel-h">
              <h3>{t('ax.side.outputs')}</h3>
            </div>
            <div className="panel-b stack-sm">
              {ex.outputs.length ? (
                <>
                  <ul className="ax-list">
                    {ex.outputs.map((o) => {
                      const anchor = itemAnchorFor(live.items, o.event_seq);
                      return <OutputRow key={o.asset_version_id} o={o} onJump={anchor === null ? null : () => setJump({ seq: anchor, nonce: Date.now() })} />;
                    })}
                  </ul>
                  <p className="faint small">{t('ax.out.hint')}</p>
                </>
              ) : (
                <p className="faint small">{t('ax.out.none')}</p>
              )}
            </div>
          </section>

          {ex.verdict && (
            <section className="panel">
              <div className="panel-h">
                <h3>{t('ax.side.verdict')}</h3>
              </div>
              <div className="panel-b stack-sm">
                {ex.verdict.summary && <p className="ap-note small">{ex.verdict.summary}</p>}
                <ul className="ax-list">
                  {(ex.verdict.criteria ?? []).map((c) => (
                    <li key={c.criterion_id}>
                      <Chip tone={c.result === 'pass' ? 'ok' : c.result === 'fail' ? 'err' : 'warn'} className="cl-chip-sm">
                        {t(c.result === 'pass' ? 'collab.verdict.pass' : c.result === 'fail' ? 'collab.verdict.fail' : 'collab.verdict.unknown')}
                      </Chip>
                      <span className="small">{c.reason}</span>
                    </li>
                  ))}
                </ul>
              </div>
            </section>
          )}

          {ex.error && (ex.error.code || ex.error.message) && (
            <section className="panel">
              <div className="panel-h">
                <h3>{t('ax.side.error')}</h3>
              </div>
              <div className="panel-b">
                <div className="callout err slim">
                  {ex.error.code === 'agent_stopped' ? <Hand aria-hidden /> : <CircleX aria-hidden />}
                  <span>
                    {ex.error.message} {ex.error.code && <code className="error-code">{ex.error.code}</code>}
                  </span>
                </div>
              </div>
            </section>
          )}

          <section className="panel">
            <div className="panel-h">
              <h3>{t('ax.side.inputs')}</h3>
            </div>
            <div className="panel-b">
              {ex.inputs?.length ? (
                <ul className="ax-list">
                  {ex.inputs.map((x, i) => (
                    <InputRow key={i} x={x} />
                  ))}
                </ul>
              ) : (
                <p className="faint small">{t('ax.in.none')}</p>
              )}
            </div>
          </section>

          <section className="panel">
            <div className="panel-h">
              <h3>{t('ax.side.attempts')}</h3>
            </div>
            <div className="panel-b">
              <ul className="ax-list">
                {(ex.attempts?.length ? ex.attempts : [{ id: ex.id, role: ex.role, attempt_no: ex.attempt_no, rework_no: ex.rework_no, status: ex.status, started_at: ex.started_at }]).map((a) => (
                  <li key={a.id} className={a.id === ex.id ? 'is-current' : ''}>
                    {a.id === ex.id ? (
                      <span className="mono">{t('ax.attempt.row', { a: a.attempt_no, r: a.rework_no })}</span>
                    ) : (
                      <Link className="mono" to={paths.agentExecution(a.id)}>
                        {t('ax.attempt.row', { a: a.attempt_no, r: a.rework_no })}
                      </Link>
                    )}
                    <span className="faint small">{executionRoleLabel(t, a.role)}</span>
                    <ExecStatusChip display={a.id === ex.id ? display : a.status} small />
                    {a.id === ex.id && <span className="faint small">· {t('ax.attempt.current')}</span>}
                  </li>
                ))}
              </ul>
            </div>
          </section>
        </aside>
      </div>

      {dialog && controlVersion !== undefined && (
        <PauseDialog
          ex={ex}
          resume={dialog === 'resume'}
          controlVersion={controlVersion}
          onClose={() => setDialog(null)}
          onDone={() => {
            setDialog(null);
            reload();
          }}
        />
      )}
    </div>
  );
}

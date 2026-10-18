// Project → Stages (route /project/overview): the stages panel first
// (features/stages: readiness, stage reviews), then objective + lifecycle
// (owner actions with guards), task counts (no weighted percentage, design
// §4.4), active and recently failed runs, open impact notices and links to
// the secondary sections (parts / files / runs).

import {
  Boxes,
  CheckCircle2,
  CircleX,
  FolderOpen,
  Library,
  ListChecks,
  Milestone as MilestoneIcon,
  Pause,
  Pencil,
  Play,
  PlayCircle,
  RotateCcw,
  Target,
  OctagonX,
} from 'lucide-react';
import { useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { getOverview, projectLifecycle, TASK_STATUSES, type Lifecycle, type LifecycleAction, type OverviewRun, type ProjectFull } from '../../api/pm';
import { normalizeBlockers } from '../../api/stages';
import { paths } from '../../app/nav';
import { EmptyState, ErrorState, SkeletonRows } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import type { ProjectTabProps } from '../../pages/project/tabProps';
import { CompleteProjectDialog, ReopenProjectDialog } from '../stages/LifecycleDialogs';
import { MilestoneStatusChip } from '../stages/meta';
import { StagesPanel } from '../stages/StagesPanel';
import { ExecutionMonitorLink } from '../templates/MonitorLink';
import { RunStatusChip } from '../workflow/icons';
import '../workflow/workflow.css';
import { Count, LifecycleChip, ReasonDialog, TaskStatusChip } from './common';
import { EditPlanDialog } from './EditPlanDialog';
import { ImpactNoticeList } from './ImpactNotices';

const LIFECYCLE_ACTIONS: Record<Lifecycle, LifecycleAction[]> = {
  draft: ['start', 'cancel'],
  active: ['pause', 'complete', 'cancel'],
  on_hold: ['resume', 'cancel'],
  completed: ['reopen'],
  cancelled: ['reopen'],
};
const REASON_REQUIRED: ReadonlySet<LifecycleAction> = new Set(['pause', 'cancel', 'reopen']);

function ActionIcon({ action }: { action: LifecycleAction }) {
  switch (action) {
    case 'start':
    case 'resume':
      return <Play />;
    case 'pause':
      return <Pause />;
    case 'complete':
      return <CheckCircle2 />;
    case 'cancel':
      return <OctagonX />;
    default:
      return <RotateCcw />;
  }
}

function RunList({ runs, empty }: { runs: OverviewRun[]; empty: string }) {
  const { t, fmt } = useI18n();
  if (!runs.length) return <p className="faint small tk-empty-line">{empty}</p>;
  return (
    <ul className="tk-rows">
      {runs.map((r) => (
        <li key={r.id}>
          <Link to={`/runs/${r.id}`} className="tk-row-link">
            <RunStatusChip status={r.status} />
            <span className="grow ellip strong">{r.title || t('tk.run.untitled')}</span>
            {!r.task_id && <span className="faint small hide-sm">{t('tk.run.noTask')}</span>}
            <span className="faint small nowrap" title={fmt.dateTime(r.updated_at ?? r.created_at)}>
              {fmt.relative(r.updated_at ?? r.created_at)}
            </span>
          </Link>
          {r.task_id && (
            <Link to={paths.task(r.task_id)} className="tk-row-sub small">
              {t('tk.run.forTask')}
            </Link>
          )}
        </li>
      ))}
    </ul>
  );
}

function Panel({ title, icon, count, action, children }: { title: string; icon: ReactNode; count?: number; action?: ReactNode; children: ReactNode }) {
  return (
    <section className="panel tk-panel" aria-label={title}>
      <div className="panel-h">
        <span className="tk-panel-ico" aria-hidden>
          {icon}
        </span>
        <h3>{title}</h3>
        {count !== undefined && <Count n={count} />}
        <span className="grow" />
        {action}
      </div>
      <div className="panel-b">{children}</div>
    </section>
  );
}

export default function ProjectOverviewTab({ project, caps, reloadProject }: ProjectTabProps) {
  const { t, fmt } = useI18n();
  const toast = useToast();
  const res = useResource((signal) => getOverview(project.id, { signal }), [project.id]);
  const [action, setAction] = useState<LifecycleAction | null>(null);
  const [cancelRuns, setCancelRuns] = useState(false);
  const [editing, setEditing] = useState(false);

  if (res.error && !res.data) return <ErrorState error={res.error} onRetry={res.reload} />;
  const ov = res.data;
  if (!ov) return <SkeletonRows rows={8} cols={3} />;

  const p = ov.project;
  const archived = p.status === 'archived' || !!p.archived_at;
  const isOwner = caps.manage && !archived;
  const actions = LIFECYCLE_ACTIONS[p.lifecycle] ?? [];
  const counts = ov.tasks.by_status;
  const totalTasks = TASK_STATUSES.reduce((a, s) => a + (counts[s] ?? 0), 0);

  const lifecycleDone = (out: ProjectFull) => {
    setAction(null);
    setCancelRuns(false);
    toast(t('tk.lc.doneToast', { lifecycle: t(`tk.lifecycle.${out.lifecycle}`) }));
    res.reload();
    reloadProject();
  };
  const runLifecycle = async (a: LifecycleAction, reason: string) => {
    lifecycleDone(await projectLifecycle(p.id, { action: a, reason, expected_version: p.version, cancel_runs: a === 'cancel' ? cancelRuns : undefined }));
  };
  const current = ov.current_milestone ?? p.current_milestone;
  const completionBlockers = ov.completion ? normalizeBlockers(ov.completion.blockers) : [];

  return (
    <div className="tk-overview stack">
      {/* The stages come first on the stages tab (prototype #project.stages); objective and numbers are also in the strip above. */}
      <StagesPanel
        ov={ov}
        caps={caps}
        onChanged={() => {
          res.reload();
          reloadProject();
        }}
      />

      {/* objective & lifecycle */}
      <section className="panel tk-hero" aria-label={t('tk.ov.objective')}>
        <div className="tk-hero-main">
          <div className="row">
            <Target className="tk-hero-ico" aria-hidden />
            <span className="tk-label">{t('tk.ov.objective')}</span>
            <LifecycleChip lifecycle={p.lifecycle} />
          </div>
          {p.objective ? <p className="tk-objective">{p.objective}</p> : <p className="faint">{isOwner ? t('tk.ov.noObjectiveOwner') : t('tk.ov.noObjective')}</p>}
          {p.lifecycle === 'on_hold' && p.hold_reason && (
            <div className="callout warn slim">
              <Pause aria-hidden />
              <span>{t('tk.ov.holdReason', { reason: p.hold_reason })}</span>
            </div>
          )}
          {p.lifecycle === 'draft' && (
            <div className="callout info slim">
              <PlayCircle aria-hidden />
              <span>{t('tk.ov.draftHint')}</span>
            </div>
          )}
          <dl className="tk-meta">
            <div>
              <dt>{t('tk.ov.owner')}</dt>
              <dd>{p.owner ? p.owner.name : <span className="faint">{t('tk.unassigned')}</span>}</dd>
            </div>
            <div>
              <dt>{t('tk.ov.currentMilestone')}</dt>
              <dd>
                {current ? (
                  <span className="row">
                    <span>
                      {current.seq}. {current.name}
                    </span>
                    {current.status && <MilestoneStatusChip status={current.status} />}
                    {current.readiness && current.status === 'open' && (
                      <span className={`small ${current.readiness.ready ? 'ok-text' : 'warn-text'}`}>
                        {current.readiness.ready ? t('stg.readiness.readyShort') : t('stg.readiness.blockedShort', { n: current.readiness.blocker_codes.length })}
                      </span>
                    )}
                  </span>
                ) : (
                  <span className="faint">{t('tk.ov.noMilestones')}</span>
                )}
              </dd>
            </div>
            <div>
              <dt>{t('tk.ov.scope')}</dt>
              <dd className="mono">{t('tk.ov.doneOfScope', { done: ov.tasks.required_done, total: ov.tasks.required_total })}</dd>
            </div>
          </dl>
        </div>
        {isOwner && (
          <div className="tk-hero-actions">
            {p.lifecycle !== 'completed' && p.lifecycle !== 'cancelled' && (
              <button type="button" className="btn sm" onClick={() => setEditing(true)}>
                <Pencil /> {t('tk.ov.editPlan')}
              </button>
            )}
            {actions.map((a) => (
              <button key={a} type="button" className={`btn sm ${a === 'cancel' ? 'danger' : a === 'start' || a === 'resume' || a === 'complete' ? 'primary' : ''}`} onClick={() => setAction(a)}>
                <ActionIcon action={a} /> {t(`tk.lc.${a}`)}
              </button>
            ))}
          </div>
        )}
        {isOwner && p.lifecycle === 'active' && ov.completion && !ov.completion.ready && completionBlockers.length > 0 && (
          <p className="faint small tk-hero-note">{t('stg.complete.notReadyShort', { n: completionBlockers.length })}</p>
        )}
        {!caps.manage && <p className="faint small tk-hero-note">{t('tk.ov.ownerOnly')}</p>}
      </section>

      {/* task counts */}
      <section className="tk-stats" aria-label={t('tk.ov.taskCounts')}>
        {TASK_STATUSES.map((s) => (
          <Link key={s} to={paths.project(p.id, 'tasks') + `?status=${s}`} className="tk-stat">
            <span className="tk-stat-n mono">{fmt.number(counts[s] ?? 0)}</span>
            <TaskStatusChip status={s} />
          </Link>
        ))}
        <div className="tk-stat total">
          <span className="tk-stat-n mono">
            {fmt.number(ov.tasks.required_done)}/{fmt.number(ov.tasks.required_total)}
          </span>
          <span className="faint small">{t('tk.ov.requiredDone')}</span>
        </div>
      </section>

      {totalTasks === 0 && (
        <EmptyState
          compact
          title={t('tk.ov.emptyTitle')}
          body={t('tk.ov.emptyBody')}
          action={
            <div className="row">
              <Link className="btn primary" to={paths.project(p.id, 'tasks')}>
                <ListChecks /> {t('tk.ov.goCreateTask')}
              </Link>
              <Link className="btn" to={paths.project(p.id, 'assets')}>
                <Library /> {t('tk.ov.goAssets')}
              </Link>
            </div>
          }
        />
      )}

      <div className="tk-grid-2">
        {/* R1.2: my tasks and my approvals are on the personal workbench; this is the manager view. */}
        <Panel title={t('tk.ov.activeRuns')} icon={<PlayCircle />} count={ov.active_runs.length} action={<ExecutionMonitorLink projectId={p.id} filters={{ status: 'active' }} label={t('tk.ov.allRuns')} />}>
          <RunList runs={ov.active_runs} empty={t('tk.ov.noActiveRuns')} />
        </Panel>
        <Panel title={t('tk.ov.failedRuns')} icon={<CircleX />} count={ov.failed_runs_recent.length} action={<ExecutionMonitorLink projectId={p.id} filters={{ status: 'failed' }} />}>
          <RunList runs={ov.failed_runs_recent} empty={t('tk.ov.noFailedRuns')} />
        </Panel>
      </div>

      <Panel title={t('tk.ov.impacts')} icon={<OctagonX />} count={ov.open_impacts.length}>
        {ov.open_impacts.length ? (
          <ImpactNoticeList notices={ov.open_impacts} canResolve={caps.workflow && !archived} showTaskLink onChanged={res.reload} />
        ) : (
          <p className="faint small tk-empty-line">{t('tk.ov.noImpacts')}</p>
        )}
      </Panel>

      <nav className="tk-quicklinks" aria-label={t('tk.ov.moreViews')}>
        <span className="tk-label">{t('tk.ov.moreViews')}</span>
        <ExecutionMonitorLink projectId={p.id} className="btn sm" />
        <Link to={paths.project(p.id, 'parts')} className="btn sm">
          <Boxes /> {t('nav.parts')}
        </Link>
        <Link to={paths.project(p.id, 'files')} className="btn sm">
          <FolderOpen /> {t('nav.files')}
        </Link>
      </nav>

      {action === 'complete' && (
        <CompleteProjectDialog
          ov={ov}
          onClose={() => setAction(null)}
          onDone={lifecycleDone}
          onReload={() => {
            setAction(null);
            res.reload();
          }}
        />
      )}
      {action === 'reopen' && (
        <ReopenProjectDialog
          ov={ov}
          onClose={() => setAction(null)}
          onDone={lifecycleDone}
          onReload={() => {
            setAction(null);
            res.reload();
          }}
        />
      )}
      {action && action !== 'complete' && action !== 'reopen' && (
        <ReasonDialog
          title={t(`tk.lc.${action}.title`)}
          body={t(`tk.lc.${action}.body`)}
          reasonLabel={REASON_REQUIRED.has(action) ? t('tk.reason') : undefined}
          reasonRequired={REASON_REQUIRED.has(action)}
          confirmLabel={t(`tk.lc.${action}`)}
          danger={action === 'cancel'}
          onConfirm={(reason) => runLifecycle(action, reason)}
          onReload={() => {
            setAction(null);
            res.reload();
          }}
          onClose={() => {
            setAction(null);
            setCancelRuns(false);
          }}
        >
          {action === 'cancel' && ov.active_runs.length > 0 && (
            <label className="check">
              <input type="checkbox" checked={cancelRuns} onChange={(e) => setCancelRuns(e.target.checked)} />
              {t('tk.lc.cancelRuns', { n: ov.active_runs.length })}
            </label>
          )}
          {action === 'start' && !p.objective.trim() && (
            <div className="callout warn slim">
              <Target aria-hidden />
              <span>{t('tk.lc.startNeedsObjective')}</span>
            </div>
          )}
          {action === 'start' && !ov.milestones.length && (
            <div className="callout warn slim">
              <MilestoneIcon aria-hidden />
              <span>{t('stg.startNeedsStages')}</span>
            </div>
          )}
        </ReasonDialog>
      )}
      {editing && (
        <EditPlanDialog
          ov={ov}
          onClose={() => setEditing(false)}
          onReload={() => {
            setEditing(false);
            res.reload();
          }}
          onSaved={() => {
            setEditing(false);
            toast(t('tk.ov.planSaved'));
            res.reload();
            reloadProject();
          }}
        />
      )}
    </div>
  );
}

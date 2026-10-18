// Project → Tasks (prototype #project.tasks): the board by default (to do /
// in progress / in review / accepted, with part, run, deliverables, next step
// and the acceptance entry), or the list (every status incl. cancelled,
// milestone and priority columns). Status changes only happen through task
// actions on the task page, so the board has no drag and drop. Filters:
// "mine" (owner or reviewer), free text, milestone; the list adds status and
// owner.

import { CalendarClock, Columns3, LayoutList, Plus } from 'lucide-react';
import { useMemo, useState, type KeyboardEvent } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { listApprovalRequests } from '../../api/approvals';
import { listParts, listRuns } from '../../api/endpoints';
import { listMilestones, listTasks, TASK_STATUSES, type Task, type TaskStatus } from '../../api/pm';
import { paths } from '../../app/nav';
import { useAuth } from '../../auth/AuthProvider';
import { SearchInput } from '../../components/ui/Form';
import { EmptyState, ErrorState, SkeletonRows } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { useDebounced } from '../../hooks/keyboard';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { dueDayForDisplay } from '../../lib/dueDate';
import type { ProjectTabProps } from '../../pages/project/tabProps';
import { latestRunByTask, ownsOrReviews, type AcceptanceRef, type BoardTask } from './board';
import { BlockedChip, isOverdue, PersonName, PriorityChip, TaskStatusChip, useMembers } from './common';
import { CreateTaskDialog } from './CreateTaskDialog';
import { TaskBoard, type BoardData } from './TaskBoard';

type View = 'list' | 'board';
const VIEW_KEY = 'partroute.tasks.view';
const RUN_PAGE = 200;

function readView(): View {
  try {
    return localStorage.getItem(VIEW_KEY) === 'list' ? 'list' : 'board';
  } catch {
    return 'board';
  }
}

function DueText({ task }: { task: Task }) {
  const { t, fmt } = useI18n();
  if (!task.due_date) return <span className="faint">—</span>;
  const late = isOverdue(task.due_date, task.status);
  return (
    <span className={`nowrap ${late ? 'err-text' : ''}`} title={late ? t('tk.overdue') : t('tk.field.due')}>
      {late && <CalendarClock className="inline-ico" aria-hidden />} {fmt.date(dueDayForDisplay(task.due_date))}
      {late && <span className="sr-only"> ({t('tk.overdue')})</span>}
    </span>
  );
}

/** Part numbers, latest runs and pending acceptances for the board (one request each). */
function useBoardData(projectId: string, enabled: boolean): BoardData {
  const parts = useResource((signal) => listParts(projectId, { include_archived: true }, { signal }), [projectId], { enabled });
  const runs = useResource((signal) => listRuns(projectId, { limit: RUN_PAGE }, { signal }), [projectId], { enabled });
  const requests = useResource(
    (signal) => listApprovalRequests({ scope: 'all', project_id: projectId, kind: 'task_acceptance', status: 'pending', limit: 200 }, { signal }),
    [projectId],
    { enabled },
  );
  return useMemo(() => {
    const acceptance = new Map<string, AcceptanceRef>();
    for (const r of requests.data ?? []) {
      if (!r.task) continue;
      acceptance.set(r.task.id, { requestId: r.id, canDecide: r.can_decide, assigneeId: r.assignee_id, agent: r.decision_mode === 'agent' });
    }
    return {
      partNumber: new Map((parts.data ?? []).map((p) => [p.id, p.number])),
      runs: latestRunByTask(runs.data ?? []),
      runsComplete: !!runs.data && runs.data.length < RUN_PAGE,
      acceptance,
    };
  }, [parts.data, runs.data, requests.data]);
}

export default function ProjectTasksTab({ project, caps }: ProjectTabProps) {
  const { t, tn, fmt } = useI18n();
  const toast = useToast();
  const navigate = useNavigate();
  const { user } = useAuth();
  const [params, setParams] = useSearchParams();
  const status = (TASK_STATUSES as readonly string[]).includes(params.get('status') ?? '') ? (params.get('status') as TaskStatus) : '';
  const owner = params.get('owner') ?? '';
  const milestone = params.get('milestone') ?? '';
  const mine = params.get('mine') === '1';
  const [q, setQ] = useState(params.get('q') ?? '');
  const dq = useDebounced(q.trim(), 250);
  const [view, setViewState] = useState<View>(readView);
  const [creating, setCreating] = useState(false);

  const setParam = (k: string, v: string) => {
    const next = new URLSearchParams(params);
    if (v) next.set(k, v);
    else next.delete(k);
    setParams(next, { replace: true });
  };
  const setView = (v: View) => {
    setViewState(v);
    try {
      localStorage.setItem(VIEW_KEY, v);
    } catch {
      /* per-viewer convenience only */
    }
  };

  // The board shows every status as columns: the status and owner filters belong to the list.
  const listOnly = view === 'list';
  const ownerId = listOnly ? (owner === 'me' ? user?.id : owner || undefined) : undefined;
  const statusFilter = listOnly ? status : '';
  const res = useResource(
    (signal) => listTasks(project.id, { status: statusFilter || undefined, owner_id: ownerId, milestone_id: milestone || undefined, q: dq || undefined }, { signal }),
    [project.id, statusFilter, ownerId, milestone, dq],
  );
  const members = useMembers(project.id);
  const milestones = useResource((signal) => listMilestones(project.id, { signal }), [project.id]);
  const msName = useMemo(() => new Map((milestones.data ?? []).map((m) => [m.id, `${m.seq}. ${m.name}`])), [milestones.data]);
  const boardData = useBoardData(project.id, view === 'board');
  const all = (res.data ?? []) as BoardTask[];
  const tasks = mine ? all.filter((x) => ownsOrReviews(x, user?.id)) : all;
  const cancelled = view === 'board' ? tasks.filter((x) => x.status === 'cancelled').length : 0;
  const filtered = !!(mine || dq || milestone || (listOnly && (status || owner)));

  const clear = () => {
    setQ('');
    setParams(new URLSearchParams(), { replace: true });
  };
  const open = (task: Task) => navigate(paths.task(task.id));
  const onRowKey = (e: KeyboardEvent, task: Task) => {
    if (e.target !== e.currentTarget) return;
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      open(task);
    }
  };
  const showCancelled = () => {
    setView('list');
    const next = new URLSearchParams(params);
    next.set('status', 'cancelled');
    setParams(next, { replace: true });
  };

  let body;
  if (res.error && !res.data) body = <ErrorState error={res.error} onRetry={res.reload} />;
  else if (res.initial) body = <SkeletonRows cols={6} />;
  else if (!tasks.length)
    body = (
      <EmptyState
        compact={filtered}
        title={filtered ? t('common.noMatches') : t('tk.list.empty')}
        body={filtered ? undefined : t('tk.list.emptyBody')}
        action={
          filtered ? (
            <button type="button" className="btn" onClick={clear}>
              {t('common.clearFilters')}
            </button>
          ) : caps.workflow ? (
            <button type="button" className="btn primary" onClick={() => setCreating(true)}>
              <Plus /> {t('tk.list.new')}
            </button>
          ) : undefined
        }
      />
    );
  else if (view === 'board')
    body = (
      <>
        <TaskBoard tasks={tasks} members={members} userId={user?.id} data={boardData} refreshing={res.loading} />
        {cancelled > 0 && (
          <p className="faint small">
            {tn('pa.board.cancelled', cancelled, { count: fmt.number(cancelled) })}{' '}
            <button type="button" className="linkish" onClick={showCancelled}>
              {t('pa.board.showCancelled')}
            </button>
          </p>
        )}
      </>
    );
  else
    body = (
      <div className={`table-wrap ${res.loading ? 'is-refreshing' : ''}`}>
        <table className="t dense">
          <thead>
            <tr>
              <th>{t('tk.col.task')}</th>
              <th>{t('tk.col.status')}</th>
              <th className="hide-sm">{t('tk.col.owner')}</th>
              <th className="hide-md">{t('tk.col.milestone')}</th>
              <th className="hide-sm">{t('tk.col.priority')}</th>
              <th className="hide-sm">{t('tk.col.due')}</th>
            </tr>
          </thead>
          <tbody>
            {tasks.map((task) => (
              <tr key={task.id} className="clickable" tabIndex={0} onClick={() => open(task)} onKeyDown={(e) => onRowKey(e, task)} aria-label={t('tk.list.open', { key: task.key, title: task.title })}>
                <td>
                  <div className="cell-main">
                    <span className="mono faint">{task.key}</span>
                    <span className="strong ellip">{task.title}</span>
                    {task.blocked_reason && <BlockedChip reason={task.blocked_reason} />}
                  </div>
                  <div className="only-sm faint small">
                    <PersonName id={task.owner_id} members={members} /> · <DueText task={task} />
                  </div>
                </td>
                <td>
                  <TaskStatusChip status={task.status} />
                </td>
                <td className="hide-sm small">
                  <PersonName id={task.owner_id} members={members} />
                </td>
                <td className="hide-md small">{task.milestone_id ? msName.get(task.milestone_id) ?? '…' : <span className="faint">—</span>}</td>
                <td className="hide-sm">
                  <PriorityChip priority={task.priority} />
                </td>
                <td className="hide-sm small">
                  <DueText task={task} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );

  return (
    <div className="stack">
      <div className="toolbar tk-toolbar">
        <div className="tkb-filter" role="group" aria-label={t('pa.filter.label')}>
          <button type="button" className={`tkb-chip ${mine ? '' : 'on'}`} aria-pressed={!mine} onClick={() => setParam('mine', '')}>
            {t('pa.filter.all')}
          </button>
          <button type="button" className={`tkb-chip ${mine ? 'on' : ''}`} aria-pressed={mine} onClick={() => setParam('mine', '1')}>
            {t('pa.filter.mine')}
          </button>
        </div>
        <SearchInput value={q} onChange={setQ} placeholder={t('tk.list.search')} />
        {listOnly && (
          <>
            <select className="select" value={status} onChange={(e) => setParam('status', e.target.value)} aria-label={t('tk.filter.status')}>
              <option value="">{t('tk.filter.allStatuses')}</option>
              {TASK_STATUSES.map((s) => (
                <option key={s} value={s}>
                  {t(`tk.status.${s}`)}
                </option>
              ))}
            </select>
            <select className="select" value={owner} onChange={(e) => setParam('owner', e.target.value)} aria-label={t('tk.filter.owner')}>
              <option value="">{t('tk.filter.anyOwner')}</option>
              <option value="me">{t('tk.filter.mine')}</option>
              {(members.list ?? [])
                .filter((m) => m.id !== user?.id)
                .map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.name}
                  </option>
                ))}
            </select>
          </>
        )}
        <select className="select" value={milestone} onChange={(e) => setParam('milestone', e.target.value)} aria-label={t('tk.filter.milestone')}>
          <option value="">{t('tk.filter.anyMilestone')}</option>
          {(milestones.data ?? []).map((m) => (
            <option key={m.id} value={m.id}>
              {m.seq}. {m.name}
            </option>
          ))}
        </select>
        {filtered && (
          <button type="button" className="btn ghost sm" onClick={clear}>
            {t('common.clearFilters')}
          </button>
        )}
        <span className="grow" />
        <div className="seg" role="group" aria-label={t('tk.view.label')}>
          <button type="button" className="seg-btn" aria-pressed={view === 'board'} onClick={() => setView('board')}>
            <Columns3 /> {t('tk.view.board')}
          </button>
          <button type="button" className="seg-btn" aria-pressed={view === 'list'} onClick={() => setView('list')}>
            <LayoutList /> {t('tk.view.list')}
          </button>
        </div>
        {caps.workflow && (
          <button type="button" className="btn primary" onClick={() => setCreating(true)}>
            <Plus /> {t('tk.list.new')}
          </button>
        )}
      </div>
      <p className="faint small">{t(view === 'board' ? 'pa.board.hint' : 'tk.board.hint')}</p>
      {body}
      {creating && (
        <CreateTaskDialog
          project={project}
          members={members}
          isProjectOwner={caps.manage}
          onClose={() => setCreating(false)}
          onCreated={(task, failed) => {
            setCreating(false);
            if (failed) toast(t('tk.create.criteriaFailed', { n: failed }), { tone: 'err' });
            else toast(t('tk.create.created', { key: task.key }));
            navigate(paths.task(task.id));
          }}
        />
      )}
    </div>
  );
}

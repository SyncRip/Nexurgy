import { useFeature } from '../../api/capabilities';
// Project → runs section. Default: the workflow board (R1.2 prototype
// #flows.runs, features/flows/RunsBoard). ?view=list — or any monitor filter in
// the URL — shows the execution monitor (design §8.1): runs across tasks with
// filters for task (or independent exploration), template and version, status,
// initiator and source. Filters live in the query string so the overview, task
// pages and templates can link to a pre-filtered view (paths.runs(projectId, {task, status, …})).

import './workflow.css';
import '../templates/templates.css';
import { Kanban, List, RefreshCw, X } from 'lucide-react';
import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { useSearchParams } from 'react-router-dom';
import { listMembers, listRuns, listTemplateVersions, listWorkflows } from '../../api/endpoints';
import { listTasks } from '../../api/pm';
import type { RunOrigin, RunSummary } from '../../api/types';
import { useUser } from '../../auth/AuthProvider';
import { EmptyState, ErrorNotice, ErrorState, SkeletonRows } from '../../components/ui/States';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import type { ProjectTabProps } from '../../pages/project/tabProps';
import { runStatusLabel } from './labels';
import { TERMINAL_RUN } from './model';
import { RunTable } from './RunTable';
import { RunsBoard } from '../flows/RunsBoard';

export { NodeCountsBar } from './RunTable';

const PAGE = 50;
const NO_TASK = '__none';

/** Status presets of the filter; values are what the server receives (comma-separated). */
const STATUS_PRESETS: { id: string; statuses: string }[] = [
  { id: 'active', statuses: 'queued,running,waiting' },
  { id: 'waiting', statuses: 'waiting' },
  { id: 'failed', statuses: 'failed' },
  { id: 'succeeded', statuses: 'succeeded' },
  { id: 'cancelled', statuses: 'cancelled' },
];

function statusesOf(value: string): string | undefined {
  if (!value) return undefined;
  return STATUS_PRESETS.find((p) => p.id === value)?.statuses ?? value;
}

/** Same filters applied locally, so an older server that ignores a filter still shows the right rows. */
function matches(r: RunSummary, f: { task: string; unlinked: boolean; workflow: string; version: string; statuses?: string; by: string; origin: string }): boolean {
  if (f.unlinked && (r.task_id ?? r.task?.id)) return false;
  if (f.task && (r.task_id ?? r.task?.id) !== f.task) return false;
  if (f.workflow && r.workflow_id !== f.workflow) return false;
  if (f.version && (r.template_version_id ?? r.template_version?.id) !== f.version) return false;
  if (f.statuses && !f.statuses.split(',').includes(r.status)) return false;
  if (f.by && r.created_by && r.created_by !== f.by) return false;
  if (f.origin && r.origin !== f.origin) return false;
  return true;
}

function RunMonitor({ project, viewSwitch }: ProjectTabProps & { viewSwitch?: ReactNode }) {
  const { t } = useI18n();
  const me = useUser();
  const [search, setSearch] = useSearchParams();
  const task = search.get('task') ?? '';
  const unlinked = search.get('unlinked') === '1';
  const workflow = search.get('workflow') ?? '';
  const version = search.get('version') ?? '';
  const status = search.get('status') ?? '';
  const byParam = search.get('by') ?? '';
  const by = byParam === 'me' ? me.id : byParam;
  const origin = (search.get('origin') ?? '') as RunOrigin | '';
  const [limit, setLimit] = useState(PAGE);

  const setFilter = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(search);
    for (const [k, v] of Object.entries(patch)) {
      if (v) next.set(k, v);
      else next.delete(k);
    }
    setSearch(next, { replace: true });
    setLimit(PAGE);
  };

  const statuses = statusesOf(status);
  const filters = { task, unlinked, workflow, version, statuses, by, origin };
  const res = useResource(
    (signal) =>
      listRuns(
        project.id,
        {
          task_id: task || undefined,
          unlinked: unlinked || undefined,
          workflow_id: workflow || undefined,
          template_version_id: version || undefined,
          status: statuses,
          created_by: by || undefined,
          origin: origin || undefined,
          limit: limit + 1,
        },
        { signal },
      ),
    [project.id, task, unlinked, workflow, version, statuses, by, origin, limit],
  );
  const tasksEnabled = useFeature('tasks');
  const tasks = useResource((signal) => listTasks(project.id, {}, { signal }), [project.id, tasksEnabled], { enabled: tasksEnabled });
  const wfs = useResource((signal) => listWorkflows(project.id, { include_archived: true }, { signal }), [project.id]);
  const members = useResource((signal) => listMembers(project.id, { signal }), [project.id]);
  const versions = useResource((signal) => listTemplateVersions(workflow, { signal }), [workflow], { enabled: !!workflow });
  const wfName = useMemo(() => new Map((wfs.data ?? []).map((w) => [w.id, w.name])), [wfs.data]);
  const memberName = useMemo(() => new Map((members.data ?? []).map((m) => [m.user.id, m.user.name])), [members.data]);

  const all = res.data ?? [];
  const hasMore = all.length > limit;
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const runs = useMemo(() => all.slice(0, limit).filter((r) => matches(r, filters)), [all, limit, task, unlinked, workflow, version, statuses, by, origin]);
  const anyActive = runs.some((r) => !TERMINAL_RUN.has(r.status));
  const filtered = !!(task || unlinked || workflow || version || status || byParam || origin);

  // Keep the list fresh while something is running or waiting (quietly: no dimming).
  const { reload } = res;
  const [silent, setSilent] = useState(false);
  useEffect(() => {
    if (!anyActive) return;
    const h = window.setInterval(() => {
      setSilent(true);
      reload();
    }, 5000);
    return () => window.clearInterval(h);
  }, [anyActive, reload]);
  useEffect(() => setSilent(false), [task, unlinked, workflow, version, status, byParam, origin]);

  const taskValue = unlinked ? NO_TASK : task;
  const liveTasks = tasksEnabled ? tasks.data ?? [] : [];
  const taskLabel = task ? liveTasks.find((x) => x.id === task) : undefined;

  let body;
  if (res.error && !res.data) body = <ErrorState error={res.error} onRetry={res.reload} />;
  else if (res.initial) body = <SkeletonRows cols={7} />;
  else if (!runs.length)
    body = (
      <EmptyState
        compact={filtered}
        title={filtered ? t('common.noMatches') : t('wf.runs.empty')}
        body={filtered ? undefined : t('tpl.mon.emptyBody')}
        action={
          filtered ? (
            <button type="button" className="btn" onClick={() => setSearch(new URLSearchParams(), { replace: true })}>
              {t('common.clearFilters')}
            </button>
          ) : undefined
        }
      />
    );
  else
    body = (
      <>
        <RunTable
          runs={runs}
          refreshing={res.loading && !silent}
          workflowName={(id) => wfName.get(id)}
          personName={(id) => (id ? (id === me.id ? t('common.you') : memberName.get(id) ?? id.slice(0, 8)) : '—')}
        />
        {hasMore && (
          <div className="tpl-more">
            <button type="button" className="btn" onClick={() => setLimit((n) => n + PAGE)} disabled={res.loading}>
              {res.loading ? t('common.loading') : t('tpl.mon.loadMore')}
            </button>
          </div>
        )}
      </>
    );

  return (
    <div className="stack">
      {/* Standard toolbar row: real result count on the left; board/list switch
          and refresh on the right (shared shape with the board view). */}
      <div className="tpl-mon-head">
        <span className="fl-bar-count grow" aria-live="polite">
          {res.data ? t('fl.runs.count', { n: runs.length }) : ''}
        </span>
        {anyActive && <span className="faint small">{t('wf.runs.autoRefresh')}</span>}
        {viewSwitch}
        <button
          type="button"
          className="btn sm icon"
          onClick={() => {
            setSilent(false);
            res.reload();
          }}
          aria-label={t('wf.run.refresh')}
          title={t('wf.run.refresh')}
        >
          <RefreshCw />
        </button>
      </div>

      <div className="seg tpl-presets" role="group" aria-label={t('tpl.mon.presets')}>
        <button type="button" className="seg-btn" aria-pressed={!status} onClick={() => setFilter({ status: null })}>
          {t('common.all')}
        </button>
        {STATUS_PRESETS.slice(0, 3).map((p) => (
          <button key={p.id} type="button" className="seg-btn" aria-pressed={status === p.id} onClick={() => setFilter({ status: status === p.id ? null : p.id })}>
            {t(`tpl.mon.preset.${p.id}` as 'tpl.mon.preset.active')}
          </button>
        ))}
      </div>

      <div className="toolbar tpl-filters">
        {(tasksEnabled || !!taskValue) && <select
          className="select"
          value={taskValue}
          onChange={(e) => (e.target.value === NO_TASK ? setFilter({ task: null, unlinked: '1' }) : setFilter({ task: e.target.value || null, unlinked: null }))}
          aria-label={t('tpl.mon.filterTask')}
        >
          <option value="">{t('tpl.mon.allTasks')}</option>
          <option value={NO_TASK}>{t('tpl.mon.unlinked')}</option>
          {task && !taskLabel && <option value={task}>{t('tpl.mon.task')}</option>}
          {liveTasks.map((x) => (
            <option key={x.id} value={x.id}>
              {x.key} · {x.title}
            </option>
          ))}
        </select>}
        <select className="select" value={workflow} onChange={(e) => setFilter({ workflow: e.target.value || null, version: null })} aria-label={t('tpl.mon.filterTemplate')}>
          <option value="">{t('tpl.mon.allTemplates')}</option>
          {(wfs.data ?? []).map((w) => (
            <option key={w.id} value={w.id}>
              {w.name}
            </option>
          ))}
        </select>
        {workflow && (
          <select className="select" value={version} onChange={(e) => setFilter({ version: e.target.value || null })} aria-label={t('tpl.mon.filterVersion')}>
            <option value="">{t('tpl.mon.allVersions')}</option>
            {(versions.data ?? []).map((v) => (
              <option key={v.id} value={v.id}>
                v{v.version_no}
                {v.status === 'deprecated' ? ` · ${t('tpl.status.deprecated')}` : ''}
              </option>
            ))}
          </select>
        )}
        <select className="select" value={status} onChange={(e) => setFilter({ status: e.target.value || null })} aria-label={t('wf.runs.filterStatus')}>
          <option value="">{t('wf.runs.allStatuses')}</option>
          {STATUS_PRESETS.map((p) => (
            <option key={p.id} value={p.id}>
              {t(`tpl.mon.preset.${p.id}` as 'tpl.mon.preset.active')}
            </option>
          ))}
          <option value="queued">{runStatusLabel(t, 'queued')}</option>
          <option value="running">{runStatusLabel(t, 'running')}</option>
        </select>
        <select className="select" value={byParam} onChange={(e) => setFilter({ by: e.target.value || null })} aria-label={t('tpl.mon.filterBy')}>
          <option value="">{t('tpl.mon.anyone')}</option>
          <option value="me">{t('tpl.mon.me')}</option>
          {(members.data ?? [])
            .filter((m) => m.user.id !== me.id)
            .map((m) => (
              <option key={m.user.id} value={m.user.id}>
                {m.user.name}
              </option>
            ))}
        </select>
        <select className="select" value={origin} onChange={(e) => setFilter({ origin: e.target.value || null })} aria-label={t('wf.runs.filterOrigin')}>
          <option value="">{t('wf.runs.allOrigins')}</option>
          <option value="workflow">{t('wf.origin.workflow')}</option>
          <option value="workspace">{t('wf.origin.workspace')}</option>
        </select>
        {filtered && (
          <button type="button" className="btn ghost sm" onClick={() => setSearch(new URLSearchParams(), { replace: true })}>
            <X /> {t('common.clearFilters')}
          </button>
        )}
      </div>
      {res.error && res.data ? <ErrorNotice error={res.error} onRetry={res.reload} /> : null}
      {body}
    </div>
  );
}

/** Query keys of the monitor filters; any of them in the URL opens the list view. */
const FILTER_KEYS = ['task', 'unlinked', 'workflow', 'version', 'status', 'by', 'origin'] as const;

export function RunsTab(props: ProjectTabProps) {
  const { t } = useI18n();
  const [search, setSearch] = useSearchParams();
  const filtered = FILTER_KEYS.some((k) => search.get(k));
  const view = search.get('view') === 'list' || (filtered && search.get('view') !== 'board') ? 'list' : 'board';
  const setView = (v: 'board' | 'list') => {
    const next = new URLSearchParams(search);
    // The board has no filters: going back to it drops them.
    if (v === 'board') for (const k of [...FILTER_KEYS, 'view']) next.delete(k);
    else next.set('view', 'list');
    setSearch(next, { replace: true });
  };
  const viewSwitch = (
    <div className="seg" role="group" aria-label={t('fl.view.label')}>
      <button type="button" className="seg-btn" aria-pressed={view === 'board'} onClick={() => setView('board')}>
        <Kanban aria-hidden /> {t('fl.view.board')}
      </button>
      <button type="button" className="seg-btn" aria-pressed={view === 'list'} onClick={() => setView('list')}>
        <List aria-hidden /> {t('fl.view.list')}
      </button>
    </div>
  );
  return view === 'board' ? <RunsBoard {...props} viewSwitch={viewSwitch} onOpenList={() => setView('list')} /> : <RunMonitor {...props} viewSwitch={viewSwitch} />;
}

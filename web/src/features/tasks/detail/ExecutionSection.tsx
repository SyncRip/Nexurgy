// Execution: runs started for this task, and the shared start-run dialog
// (features/templates/StartRunDialog, task context fixed): a published template
// version, its inputs and parameters (POST /tasks/{id}/runs). The server freezes
// the task's current baseline and input versions into the run. A run succeeding
// never completes the task — deliverables and acceptance do.

import { Activity, Info, Play, PlayCircle } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { listWorkflows } from '../../../api/endpoints';
import { listTaskRuns } from '../../../api/pm';
import { paths } from '../../../app/nav';
import { EmptyState, ErrorNotice, ErrorState, SkeletonRows } from '../../../components/ui/States';
import { useToast } from '../../../components/ui/Toast';
import { useResource } from '../../../hooks/useResource';
import { useI18n } from '../../../i18n/I18nProvider';
import { RunTemplateRef } from '../../templates/common';
import { StartRunDialog } from '../../templates/StartRunDialog';
import { RunStatusChip } from '../../workflow/icons';
import { TERMINAL_RUN } from '../../workflow/model';
import { NodeCountsBar } from '../../workflow/RunTable';
import { Count } from '../common';
import type { TaskSectionProps } from './shared';

export function ExecutionSection({ task, reloadTask, projectActive }: TaskSectionProps) {
  const { t, fmt } = useI18n();
  const toast = useToast();
  const navigate = useNavigate();
  const runs = useResource((signal) => listTaskRuns(task.id, { signal }), [task.id]);
  const workflows = useResource((signal) => listWorkflows(task.project_id, { include_archived: true }, { signal }), [task.project_id]);
  const wfName = useMemo(() => new Map((workflows.data ?? []).map((w) => [w.id, w.name])), [workflows.data]);
  const [starting, setStarting] = useState(false);
  const canStart = task.permissions.participate && projectActive && task.status !== 'cancelled';
  const anyActive = (runs.data ?? []).some((r) => !TERMINAL_RUN.has(r.status));

  const { reload } = runs;
  useEffect(() => {
    if (!anyActive) return;
    const h = window.setInterval(reload, 5000);
    return () => window.clearInterval(h);
  }, [anyActive, reload]);

  return (
    <div className="stack">
      <div className="callout info">
        <Info aria-hidden />
        <span>{t('tk.run.notCompletionLong')}</span>
      </div>
      <section className="panel tk-panel" aria-label={t('tk.run.title.list')}>
        <div className="panel-h">
          <PlayCircle className="tk-panel-ico" aria-hidden />
          <h3>{t('tk.run.title.list')}</h3>
          {runs.data && <Count n={runs.data.length} />}
          <span className="grow" />
          <Link className="btn ghost sm" to={paths.runs(task.project_id, { task: task.id })}>
            <Activity /> {t('tpl.nav.monitor')}
          </Link>
          {canStart && (
            <button type="button" className="btn sm primary" onClick={() => setStarting(true)}>
              <Play /> {t('tk.run.start')}
            </button>
          )}
        </div>
        <div className="panel-b">
          {task.permissions.participate && !projectActive && <p className="faint small">{t('tk.run.projectInactive')}</p>}
          {task.status === 'cancelled' && <p className="faint small">{t('tk.run.taskCancelled')}</p>}
          {runs.error && !runs.data ? (
            <ErrorState error={runs.error} onRetry={runs.reload} />
          ) : !runs.data ? (
            <SkeletonRows rows={3} cols={5} />
          ) : !runs.data.length ? (
            <EmptyState compact title={t('tk.run.empty')} body={canStart ? t('tk.run.emptyBody') : undefined} />
          ) : (
            <div className="table-wrap">
              <table className="t dense">
                <thead>
                  <tr>
                    <th>{t('tk.run.col.run')}</th>
                    <th>{t('tk.run.col.status')}</th>
                    <th className="hide-sm">{t('tk.run.col.progress')}</th>
                    <th className="hide-md">{t('tk.run.col.template')}</th>
                    <th className="hide-sm">{t('tk.run.col.created')}</th>
                  </tr>
                </thead>
                <tbody>
                  {runs.data.map((r) => (
                    <tr key={r.id}>
                      <td>
                        <div className="tk-xs">
                          <Link to={`/runs/${r.id}`} className="strong">
                            {r.title || t('tk.run.untitled')}
                          </Link>
                          {r.purpose && <span className="faint small tk-clamp">{r.purpose}</span>}
                        </div>
                      </td>
                      <td>
                        <RunStatusChip status={r.status} />
                      </td>
                      <td className="hide-sm">
                        <NodeCountsBar counts={r.node_counts} />
                      </td>
                      <td className="hide-md small">
                        <RunTemplateRef run={r} name={r.workflow_id ? wfName.get(r.workflow_id) ?? t('tk.run.deletedWorkflow') : undefined} />
                      </td>
                      <td className="hide-sm small nowrap" title={fmt.dateTime(r.created_at)}>
                        {fmt.relative(r.created_at)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {workflows.error ? <ErrorNotice error={workflows.error} onRetry={workflows.reload} /> : null}
        </div>
      </section>
      {starting && (
        <StartRunDialog
          projectId={task.project_id}
          task={{ id: task.id, key: task.key, title: task.title, part_id: task.part_id }}
          onClose={() => setStarting(false)}
          onStarted={({ id }) => {
            setStarting(false);
            toast(t('tk.run.started'), { action: { label: t('tk.run.open'), onClick: () => navigate(paths.run(id)) } });
            runs.reload();
            reloadTask();
          }}
        />
      )}
    </div>
  );
}

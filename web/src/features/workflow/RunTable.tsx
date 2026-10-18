// Run list table shared by the execution monitor and a template's usage tab:
// run, status, steps, task, template version, initiator, created, duration.

import './workflow.css';
import { Bot, Workflow as WorkflowIcon } from 'lucide-react';
import type { KeyboardEvent, MouseEvent } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import type { RunSummary } from '../../api/types';
import { paths } from '../../app/nav';
import { useI18n } from '../../i18n/I18nProvider';
import { RunTemplateRef } from '../templates/common';
import { RunStatusChip } from './icons';
import { nodeStatusLabel } from './labels';
import { durationBetween, formatDuration, TERMINAL_RUN } from './model';

export function NodeCountsBar({ counts }: { counts: RunSummary['node_counts'] }) {
  const { t } = useI18n();
  const total = Object.values(counts).reduce((a, b) => a + (b ?? 0), 0);
  if (!total) return <span className="faint small">—</span>;
  const order = ['succeeded', 'running', 'ready', 'waiting_human', 'failed', 'cancelled', 'skipped', 'pending'] as const;
  const label = order
    .filter((s) => counts[s])
    .map((s) => `${nodeStatusLabel(t, s)} ${counts[s]}`)
    .join(', ');
  return (
    <span className="wf-mini" role="img" aria-label={label} title={label}>
      <span className="wf-progress sm">
        {order.map((s) => (counts[s] ? <span key={s} className={`seg ns-${s}`} style={{ flexGrow: counts[s] }} /> : null))}
      </span>
      <span className="mono faint small">
        {counts.succeeded ?? 0}/{total}
      </span>
    </span>
  );
}

/** Let links inside a clickable row work without also opening the row. */
const keepLinks = (e: MouseEvent<HTMLElement>) => {
  if ((e.target as HTMLElement).closest('a')) e.stopPropagation();
};

export function RunTable({
  runs,
  refreshing,
  hideTask,
  hideTemplate,
  workflowName,
  personName,
}: {
  runs: RunSummary[];
  refreshing?: boolean;
  hideTask?: boolean;
  hideTemplate?: boolean;
  /** Fallback names for runs whose response has no template_version (legacy / draft). */
  workflowName?: (id: string) => string | undefined;
  personName?: (id: string | undefined) => string;
}) {
  const { t, fmt } = useI18n();
  const navigate = useNavigate();
  const open = (r: RunSummary) => navigate(paths.run(r.id));
  const onRowKey = (e: KeyboardEvent, r: RunSummary) => {
    if (e.target !== e.currentTarget) return;
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      open(r);
    }
  };
  return (
    <div className={`table-wrap ${refreshing ? 'is-refreshing' : ''}`}>
      <table className="t dense">
        <thead>
          <tr>
            <th>{t('wf.runs.col.run')}</th>
            <th>{t('wf.runs.col.status')}</th>
            <th className="hide-sm">{t('wf.runs.col.progress')}</th>
            {!hideTask && <th className="hide-sm">{t('tpl.mon.col.task')}</th>}
            {!hideTemplate && <th className="hide-md">{t('tpl.mon.col.template')}</th>}
            <th className="hide-md">{t('tpl.mon.col.by')}</th>
            <th className="hide-md">{t('wf.runs.col.created')}</th>
            <th className="hide-md">{t('wf.runs.col.duration')}</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((r) => {
            const d = durationBetween(r.started_at ?? r.created_at, r.finished_at);
            return (
              <tr key={r.id} className="clickable" tabIndex={0} onClick={() => open(r)} onKeyDown={(e) => onRowKey(e, r)} aria-label={t('wf.runs.openRun', { title: r.title || t('wf.run.untitled') })}>
                <td>
                  <div className="cell-main">
                    <span className={`wf-origin ${r.origin} sm`} title={t(`wf.origin.${r.origin}`)}>
                      {r.origin === 'workspace' ? <Bot aria-hidden /> : <WorkflowIcon aria-hidden />}
                    </span>
                    <span className="strong ellip">{r.title || t('wf.run.untitled')}</span>
                  </div>
                  {r.purpose && <div className="faint small ellip tpl-purpose-cell">{r.purpose}</div>}
                  <div className="only-sm faint small">
                    <NodeCountsBar counts={r.node_counts} />
                    {!hideTask && r.task ? <span className="mono"> · {r.task.key}</span> : null}
                  </div>
                </td>
                <td>
                  <RunStatusChip status={r.status} />
                  {r.cancel_requested && !TERMINAL_RUN.has(r.status) && <span className="faint small"> · {t('wf.runs.cancelling')}</span>}
                </td>
                <td className="hide-sm">
                  <NodeCountsBar counts={r.node_counts} />
                </td>
                {!hideTask && (
                  <td className="hide-sm small" onClick={keepLinks}>
                    {r.task ? (
                      <Link to={paths.task(r.task.id)} className="tpl-ref" title={r.task.title}>
                        <span className="mono">{r.task.key}</span> <span className="ellip">{r.task.title}</span>
                      </Link>
                    ) : r.task_id ? (
                      <Link to={paths.task(r.task_id)} className="mono">
                        {t('tpl.mon.task')}
                      </Link>
                    ) : (
                      <span className="faint">{r.origin === 'workspace' ? '—' : t('tpl.mon.exploration')}</span>
                    )}
                  </td>
                )}
                {!hideTemplate && (
                  <td className="hide-md small" onClick={keepLinks}>
                    <RunTemplateRef run={r} name={r.workflow_id ? workflowName?.(r.workflow_id) : undefined} />
                  </td>
                )}
                <td className="hide-md small ellip">{personName ? personName(r.created_by) : '—'}</td>
                <td className="hide-md faint small nowrap" title={fmt.dateTime(r.created_at)}>
                  {fmt.relative(r.created_at)}
                </td>
                <td className="hide-md mono small">{d === null ? '—' : formatDuration(d)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

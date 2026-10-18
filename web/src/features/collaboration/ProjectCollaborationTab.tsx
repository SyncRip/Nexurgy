// Project section "Collaboration" (design §5.2): the collaboration instances
// (status, mode, server-side progress) with status / mode filters, the open
// drafts, and the "start a demand" entry.

import '../workflow/workflow.css';
import './collaboration.css';
import { FilePen, Plus } from 'lucide-react';
import { useMemo, type KeyboardEvent } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { EXECUTION_MODES, type ExecutionMode } from '../../api/agents';
import { isApiError } from '../../api/client';
import { listCollaborationDrafts, listCollaborationRuns, summaryRow, type CollaborationRow } from '../../api/collaboration';
import { RUN_STATUSES, type RunStatus } from '../../api/types';
import { paths } from '../../app/nav';
import { EmptyState, ErrorNotice, ErrorState, SkeletonRows } from '../../components/ui/States';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import type { ProjectTabProps } from '../../pages/project/tabProps';
import { RunStatusChip } from '../workflow/icons';
import { runStatusLabel } from '../workflow/labels';
import { CL_ERRORS, ModeChip, PausedChip } from './common';
import { PolicyChip } from '../../components/engineering/EngineeringChips';

function ProgressCell({ row }: { row: CollaborationRow }) {
  const { t } = useI18n();
  const p = row.progress;
  const pct = p.total ? Math.round((p.accepted / p.total) * 100) : 0;
  return (
    <span className="tk-xs">
      <span className="small">
        {t('collab.list.progress', { accepted: p.accepted, executed: p.executed, total: p.total })}
        {p.blocked ? <span className="err-text"> · {t('collab.list.blocked', { n: p.blocked })}</span> : null}
      </span>
      <span className="wf-progress sm" aria-hidden>
        {p.total > 0 && (
          <>
            <span className="seg ns-succeeded" style={{ flexGrow: p.accepted }} />
            <span className="seg ns-pending" style={{ flexGrow: Math.max(p.total - p.accepted, 0) }} />
          </>
        )}
      </span>
      <span className="sr-only">{pct}%</span>
    </span>
  );
}

export default function ProjectCollaborationTab({ project, caps }: ProjectTabProps) {
  const { t, fmt } = useI18n();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const status = (RUN_STATUSES as readonly string[]).includes(params.get('status') ?? '') ? (params.get('status') as RunStatus) : '';
  const mode = (EXECUTION_MODES as readonly string[]).includes(params.get('mode') ?? '') ? (params.get('mode') as ExecutionMode) : '';
  const runs = useResource((signal) => listCollaborationRuns(project.id, { status: status || undefined, mode: mode || undefined, limit: 100 }, { signal }), [project.id, status, mode]);
  const drafts = useResource((signal) => listCollaborationDrafts(project.id, { signal }), [project.id]);
  const rows = useMemo(() => (runs.data ?? []).map(summaryRow).filter((r): r is CollaborationRow => !!r), [runs.data]);
  const filtered = !!(status || mode);

  const setParam = (k: string, v: string) => {
    const next = new URLSearchParams(params);
    if (v) next.set(k, v);
    else next.delete(k);
    setParams(next, { replace: true });
  };
  const open = (r: CollaborationRow) => navigate(paths.collaborationRun(r.runId));
  const onRowKey = (e: KeyboardEvent, r: CollaborationRow) => {
    if (e.target !== e.currentTarget) return;
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      open(r);
    }
  };

  const newButton = caps.workflow ? (
    <Link className="btn primary" to={paths.collaborationNew(project.id)}>
      <Plus /> {t('collab.new')}
    </Link>
  ) : null;

  let body;
  if (runs.error && !runs.data) body = <ErrorState error={runs.error} title={isApiError(runs.error) && runs.error.isNotFound ? t('collab.list.unavailable') : undefined} onRetry={runs.reload} overrides={CL_ERRORS} />;
  else if (runs.initial) body = <SkeletonRows cols={5} />;
  else if (!rows.length)
    body = (
      <EmptyState
        compact={filtered}
        title={filtered ? t('common.noMatches') : t('collab.list.empty')}
        body={filtered ? undefined : t('collab.list.emptyBody')}
        action={
          filtered ? (
            <button type="button" className="btn" onClick={() => setParams(new URLSearchParams(), { replace: true })}>
              {t('common.clearFilters')}
            </button>
          ) : (
            newButton ?? undefined
          )
        }
      />
    );
  else
    body = (
      <div className={`table-wrap ${runs.loading ? 'is-refreshing' : ''}`}>
        <table className="t dense">
          <thead>
            <tr>
              <th>{t('collab.list.col.title')}</th>
              <th>{t('collab.list.col.mode')}</th>
              <th>{t('collab.list.col.status')}</th>
              <th className="hide-sm">{t('collab.list.col.progress')}</th>
              <th className="hide-md">{t('collab.list.col.created')}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.runId} className="clickable" tabIndex={0} onClick={() => open(r)} onKeyDown={(e) => onRowKey(e, r)} aria-label={t('collab.list.open', { title: r.title || t('collab.run.untitled') })}>
                <td>
                  <div className="cell-main">
                    <span className="strong ellip" title={r.title}>
                      {r.title || t('collab.run.untitled')}
                    </span>
                  </div>
                  <div className="only-sm">
                    <ProgressCell row={r} />
                  </div>
                </td>
                <td>
                  <ModeChip mode={r.mode} />
                  {r.policy === 'turbo' && <PolicyChip policy="turbo" />}
                </td>
                <td>
                  <span className="row">
                    {r.status ? <RunStatusChip status={r.status} /> : <span className="faint">—</span>}
                    {r.paused && <PausedChip />}
                  </span>
                </td>
                <td className="hide-sm">
                  <ProgressCell row={r} />
                </td>
                <td className="hide-md small" title={fmt.dateTime(r.createdAt)}>
                  {fmt.relative(r.createdAt)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );

  const draftList = (drafts.data ?? []).filter((w) => !w.archived_at);

  return (
    <div className="stack">
      <div className="row">
        <p className="muted small grow">{t('collab.list.intro')}</p>
        {newButton}
      </div>
      <div className="toolbar">
        <select className="select" value={status} onChange={(e) => setParam('status', e.target.value)} aria-label={t('collab.list.filterStatus')}>
          <option value="">{t('collab.list.allStatuses')}</option>
          {RUN_STATUSES.map((s) => (
            <option key={s} value={s}>
              {runStatusLabel(t, s)}
            </option>
          ))}
        </select>
        <select className="select" value={mode} onChange={(e) => setParam('mode', e.target.value)} aria-label={t('collab.list.filterMode')}>
          <option value="">{t('collab.list.allModes')}</option>
          {EXECUTION_MODES.map((m) => (
            <option key={m} value={m}>
              {t(`collab.mode.${m}`)}
            </option>
          ))}
        </select>
      </div>
      {body}

      {drafts.error && !drafts.data && !(isApiError(drafts.error) && drafts.error.isNotFound) ? <ErrorNotice error={drafts.error} onRetry={drafts.reload} /> : null}
      {draftList.length > 0 && (
        <section className="panel" aria-labelledby="cl-drafts-h">
          <div className="panel-h">
            <FilePen aria-hidden className="faint" />
            <h2 id="cl-drafts-h">{t('collab.list.drafts')}</h2>
            <span className="tk-count mono">{draftList.length}</span>
          </div>
          <ul className="tk-rows panel-b">
            {draftList.map((w) => (
              <li key={w.id}>
                <Link to={paths.collaborationDraft(w.id)} className="tk-row-link">
                  <span className="grow ellip">{w.name || t('collab.run.untitled')}</span>
                  <span className="faint small">{t('collab.list.draftUpdated', { time: fmt.relative(w.updated_at) })}</span>
                </Link>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

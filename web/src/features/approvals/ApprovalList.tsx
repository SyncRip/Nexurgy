// Approval request list shared by the project tab and the cross-project
// centre: scope (mine / created / all), status and kind filters. Rows open the
// request detail; "Your decision" marks requests the server says you can decide.

import './approvals.css';
import { Hand, Inbox } from 'lucide-react';
import { useEffect, useMemo, useState, type KeyboardEvent } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { REQUEST_KINDS, REQUEST_SCOPES, REQUEST_STATUSES, isDirectEngineering, listApprovalRequests, type ApprovalRequest, type RequestKind, type RequestScope } from '../../api/approvals';
import type { Project } from '../../api/types';
import { paths } from '../../app/nav';
import { ACTION_ROLES, STAGE_ACTION } from '../../auth/permissions';
import { EmptyState, ErrorState, SkeletonRows } from '../../components/ui/States';
import { useResource } from '../../hooks/useResource';
import { useI18n, type TFunction } from '../../i18n/I18nProvider';
import { dynLabel } from '../assets/meta';
import { RequestKindChip, RequestStatusChip, onApprovalsChanged, requestKindText } from './meta';

/** "Dana Designer" or "Any designer / owner" for unassigned engineering reviews. */
export function assigneeText(t: TFunction, r: Pick<ApprovalRequest, 'assignee' | 'kind' | 'stage'>): string {
  if (r.assignee) return r.assignee.name;
  if (r.kind === 'engineering') {
    const stage = (r.stage ?? 'design') as keyof typeof STAGE_ACTION;
    const roles = (ACTION_ROLES[STAGE_ACTION[stage] ?? 'design'] ?? []).map((x) => dynLabel(t, `role.${x}`, x)).join(' / ');
    return t('ap.anyRole', { roles });
  }
  return t('ap.unassigned');
}

export function ApprovalList({ projectId, projects }: { projectId?: string; projects?: Map<string, Pick<Project, 'id' | 'code' | 'name'>> }) {
  const { t, tn, fmt } = useI18n();
  const navigate = useNavigate();
  const [scope, setScope] = useState<RequestScope>('mine');
  const [status, setStatus] = useState<string>('pending');
  const [kind, setKind] = useState<RequestKind | ''>('');
  const res = useResource(
    (signal) => listApprovalRequests({ scope, project_id: projectId, status, kind, limit: 200 }, { signal }),
    [scope, projectId, status, kind],
  );
  const { reload } = res;
  useEffect(() => onApprovalsChanged(reload), [reload]);
  const rows = useMemo(() => res.data ?? [], [res.data]);
  const showProject = !projectId;
  const filtered = status !== 'pending' || !!kind;

  const open = (r: ApprovalRequest) => navigate(paths.approvalRequest(r.id));
  const onRowKey = (e: KeyboardEvent, r: ApprovalRequest) => {
    if (e.target !== e.currentTarget) return;
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      open(r);
    }
  };

  let body;
  if (res.error && !res.data) body = <ErrorState error={res.error} onRetry={res.reload} />;
  else if (res.initial) body = <SkeletonRows cols={6} />;
  else if (!rows.length)
    body = (
      <EmptyState
        compact
        icon={<Inbox className="big-ico" aria-hidden />}
        title={t(`ap.empty.${scope}`)}
        body={status === 'pending' ? t('ap.empty.pendingBody') : t('ap.empty.otherBody')}
        action={
          filtered ? (
            <button
              type="button"
              className="btn"
              onClick={() => {
                setStatus('pending');
                setKind('');
              }}
            >
              {t('common.clearFilters')}
            </button>
          ) : undefined
        }
      />
    );
  else
    body = (
      <div className={`table-wrap ${res.loading ? 'is-refreshing' : ''}`}>
        <table className="t dense ap-table">
          <caption className="sr-only">{t('ap.tableCaption')}</caption>
          <thead>
            <tr>
              <th>{t('ap.col.request')}</th>
              {showProject && <th className="hide-sm">{t('ap.col.project')}</th>}
              <th className="hide-sm">{t('ap.col.kind')}</th>
              <th className="hide-md">{t('ap.col.task')}</th>
              <th className="hide-md">{t('ap.col.assignee')}</th>
              <th>{t('ap.col.status')}</th>
              <th className="hide-sm">{t('ap.col.created')}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const p = projects?.get(r.project_id);
              return (
                <tr key={r.id} className="clickable" tabIndex={0} onClick={() => open(r)} onKeyDown={(e) => onRowKey(e, r)} aria-label={t('ap.openRequest', { title: r.title || requestKindText(t, r.kind, r.stage, isDirectEngineering(r)) })}>
                  <td>
                    <div className="cell-main">
                      <span className="ellip strong">{r.title || requestKindText(t, r.kind, r.stage, isDirectEngineering(r))}</span>
                      {r.can_decide && (
                        <span className="chip accent ap-yours" title={t('ap.yourDecisionHint')}>
                          <Hand aria-hidden /> {t('ap.yourDecision')}
                        </span>
                      )}
                    </div>
                    <div className="faint small only-sm">
                      {requestKindText(t, r.kind, r.stage, isDirectEngineering(r))}
                      {showProject && p ? ` · ${p.code}` : ''} · {fmt.relative(r.created_at)}
                    </div>
                  </td>
                  {showProject && (
                    <td className="hide-sm mono small nowrap" title={p?.name}>
                      {p ? (
                        <Link to={paths.project(p.id, 'approvals')} onClick={(e) => e.stopPropagation()}>
                          {p.code}
                        </Link>
                      ) : (
                        '—'
                      )}
                    </td>
                  )}
                  <td className="hide-sm">
                    <RequestKindChip kind={r.kind} stage={r.stage} direct={isDirectEngineering(r)} />
                  </td>
                  <td className="hide-md small">
                    {r.task ? (
                      <Link to={paths.task(r.task.id)} onClick={(e) => e.stopPropagation()} title={r.task.title}>
                        <span className="mono">{r.task.key}</span>
                      </Link>
                    ) : (
                      <span className="faint">—</span>
                    )}
                  </td>
                  <td className="hide-md small">{assigneeText(t, r)}</td>
                  <td>
                    <RequestStatusChip status={r.status} />
                  </td>
                  <td className="hide-sm faint small nowrap" title={fmt.dateTime(r.created_at)}>
                    {fmt.relative(r.created_at)}
                    {r.created_by_name && <div className="faint small ellip">{r.created_by_name}</div>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    );

  return (
    <div className="stack">
      <div className="toolbar">
        <div className="seg" role="group" aria-label={t('ap.filter.scope')}>
          {REQUEST_SCOPES.map((s) => (
            <button key={s} type="button" className="seg-btn" aria-pressed={scope === s} onClick={() => setScope(s)}>
              {t(`ap.scope.${s}`)}
            </button>
          ))}
        </div>
        <select className="select" value={status} onChange={(e) => setStatus(e.target.value)} aria-label={t('ap.filter.status')}>
          {REQUEST_STATUSES.map((s) => (
            <option key={s} value={s}>
              {t(`ap.status.${s}`)}
            </option>
          ))}
          <option value="all">{t('ap.filter.allStatuses')}</option>
        </select>
        <select className="select" value={kind} onChange={(e) => setKind(e.target.value as RequestKind | '')} aria-label={t('ap.filter.kind')}>
          <option value="">{t('ap.filter.allKinds')}</option>
          {REQUEST_KINDS.map((k) => (
            <option key={k} value={k}>
              {t(`ap.kind.${k}`)}
            </option>
          ))}
        </select>
        <span className="grow" />
        {!res.initial && rows.length > 0 && (
          <span className="faint small" aria-live="polite">
            {tn('ap.resultCount', rows.length, { count: fmt.number(rows.length) })}
          </span>
        )}
      </div>
      <p className="faint small">{t(`ap.scopeHint.${scope}`)}</p>
      {body}
    </div>
  );
}

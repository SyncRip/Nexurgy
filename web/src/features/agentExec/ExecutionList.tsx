// AI workflows → Execution records (prototype #executions): every agent execution of the project, filtered by agent,
// status and runtime (server-side filters of GET /agent-executions). Rows open the execution page.

import { Activity } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { AGENT_RUNTIMES } from '../../api/agents';
import { listAgentExecutions, listCursor, listItems, type AgentExecutionListItem } from '../../api/agentExecutions';
import { isApiError } from '../../api/client';
import { paths } from '../../app/nav';
import { EmptyState, ErrorNotice, ErrorState, SkeletonRows } from '../../components/ui/States';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { Trans } from '../../i18n/Trans';
import type { ProjectTabProps } from '../../pages/project/tabProps';
import { useAgentProfiles } from '../agents/common';
import { executionRoleLabel } from '../collaboration/common';
import { useFlowsData } from '../flows/data';
import { useMoney } from '../part/common';
import { ExecStatusChip, RuntimeChip, SimTag } from './common';

const STATUS_FILTERS = ['running', 'delivered', 'blocked', 'failed', 'cancelled'] as const;
const PAGE = 50;

function CostCell({ used }: { used: AgentExecutionListItem['used'] }) {
  const { t } = useI18n();
  const money = useMoney();
  if (used.cost_unknown || used.cost_fen === null || used.cost_fen === undefined)
    return (
      <span className="faint" title={t('ax.budget.unknownHint')}>
        {t('ax.budget.unknown')}
      </span>
    );
  return <>{money(used.cost_fen)}</>;
}

export default function ExecutionList({ project }: ProjectTabProps) {
  const { t, fmt } = useI18n();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const status = params.get('status') ?? '';
  const agent = params.get('agent_id') ?? '';
  const runtime = params.get('runtime') ?? '';
  const shared = useFlowsData(project.id);
  const own = useAgentProfiles(shared ? undefined : project.id, false);
  const profiles = (shared?.agents ?? own).data ?? [];
  const [more, setMore] = useState<AgentExecutionListItem[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [moreBusy, setMoreBusy] = useState(false);
  const [moreError, setMoreError] = useState<unknown>(null);
  const q = { project_id: project.id, status: status || undefined, agent_id: agent || undefined, runtime: runtime || undefined, limit: PAGE };
  const res = useResource((signal) => listAgentExecutions(q, { signal }), [project.id, status, agent, runtime]);

  useEffect(() => {
    setMore([]);
    setCursor(listCursor(res.data));
  }, [res.data]);

  const setFilter = (k: string, v: string) => {
    const next = new URLSearchParams(params);
    if (v) next.set(k, v);
    else next.delete(k);
    setParams(next, { replace: true });
  };
  const loadMore = async () => {
    if (!cursor) return;
    setMoreBusy(true);
    setMoreError(null);
    try {
      const page = await listAgentExecutions({ ...q, cursor });
      setMore((m) => [...m, ...listItems(page)]);
      setCursor(listCursor(page));
    } catch (e) {
      setMoreError(e);
    } finally {
      setMoreBusy(false);
    }
  };

  const rows = [...listItems(res.data), ...more];
  const filtered = !!(status || agent || runtime);

  let body;
  if (res.error && !res.data) {
    body = isApiError(res.error) && res.error.isNotFound ? <ErrorState error={res.error} title={t('ax.unavailable')} onRetry={res.reload} /> : <ErrorState error={res.error} onRetry={res.reload} />;
  } else if (res.initial) body = <SkeletonRows rows={5} cols={6} />;
  else if (!rows.length)
    body = filtered ? (
      <EmptyState
        compact
        title={t('ax.list.noMatch')}
        action={
          <button type="button" className="btn" onClick={() => setParams(new URLSearchParams(), { replace: true })}>
            {t('common.clearFilters')}
          </button>
        }
      />
    ) : (
      <EmptyState compact icon={<Activity className="big-ico" aria-hidden />} title={t('ax.list.empty')} body={t('ax.list.emptyBody')} />
    );
  else
    body = (
      <div className="table-wrap">
        <table className="t ax-table">
          <thead>
            <tr>
              <th>{t('ax.list.col.exec')}</th>
              <th>{t('ax.list.col.agent')}</th>
              <th className="hide-sm">{t('ax.list.col.node')}</th>
              <th className="hide-md">{t('ax.list.col.runtime')}</th>
              <th>{t('ax.list.col.status')}</th>
              <th className="num hide-sm">{t('ax.list.col.steps')}</th>
              <th className="num hide-md">{t('ax.list.col.cost')}</th>
              <th className="hide-md">{t('ax.list.col.started')}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((x) => (
              <tr key={x.id} onClick={() => navigate(paths.agentExecution(x.id))}>
                <td>
                  <span className="row">
                    <Link className="mono" to={paths.agentExecution(x.id)} onClick={(e) => e.stopPropagation()}>
                      {x.id.slice(0, 8)}
                    </Link>
                    {x.simulated && <SimTag small />}
                  </span>
                </td>
                <td>
                  {x.agent?.name ?? '—'}
                  <div className="faint small">{executionRoleLabel(t, x.role)}</div>
                </td>
                <td className="hide-sm">
                  <Link to={paths.collaborationRun(x.run_id, x.node_id)} onClick={(e) => e.stopPropagation()}>
                    {x.node_title || x.node_id}
                  </Link>
                </td>
                <td className="hide-md">
                  <RuntimeChip runtime={x.runtime} small />
                </td>
                <td>
                  <ExecStatusChip display={x.status} small />
                </td>
                <td className="num hide-sm">{x.used?.steps ?? '—'}</td>
                <td className="num hide-md">
                  <CostCell used={x.used ?? {}} />
                </td>
                <td className="hide-md faint small" title={fmt.dateTime(x.started_at)}>
                  {fmt.relative(x.started_at)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );

  return (
    <div className="stack">
      <p className="muted small">
        <Trans k="ax.list.intro" values={{ code: <b className="mono">{project.code}</b> }} />
      </p>
      <div className="ax-filters" role="group" aria-label={t('agents.filter.label')}>
        <label>
          {t('ax.list.status')}
          <select className="select sm" value={status} onChange={(e) => setFilter('status', e.target.value)}>
            <option value="">{t('ax.list.all')}</option>
            {STATUS_FILTERS.map((s) => (
              <option key={s} value={s}>
                {t(`ax.status.${s}`)}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t('ax.list.agent')}
          <select className="select sm" value={agent} onChange={(e) => setFilter('agent_id', e.target.value)}>
            <option value="">{t('ax.list.allAgents')}</option>
            {profiles.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t('ax.list.runtime')}
          <select className="select sm" value={runtime} onChange={(e) => setFilter('runtime', e.target.value)}>
            <option value="">{t('ax.list.all')}</option>
            {AGENT_RUNTIMES.map((r) => (
              <option key={r} value={r}>
                {t(`ax.rt.${r}`)}
              </option>
            ))}
          </select>
        </label>
      </div>
      {res.error && res.data ? <ErrorNotice error={res.error} onRetry={res.reload} /> : null}
      {body}
      {cursor && (
        <div className="row">
          <button type="button" className="btn" onClick={() => void loadMore()} disabled={moreBusy}>
            {moreBusy ? t('common.loading') : t('ax.list.more')}
          </button>
          <ErrorNotice error={moreError} />
        </div>
      )}
    </div>
  );
}

// Part workspace — Runs tab: workflow and workspace (AI modeling) runs that
// involve this part, linking to the run page. Refreshes while runs are active.

import { ExternalLink, PlayCircle, RefreshCw } from 'lucide-react';
import { useEffect } from 'react';
import { Link } from 'react-router-dom';
import { listRuns } from '../../api/endpoints';
import type { NodeStatus } from '../../api/types';
import { EmptyState, ErrorState, SkeletonRows } from '../../components/ui/States';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import type { PartTabProps } from '../../pages/part/registry';
import { RunStatusChip } from './common';
import './workspace.css';

const ACTIVE = new Set(['queued', 'running', 'waiting']);
const COUNT_ORDER: NodeStatus[] = ['succeeded', 'running', 'waiting_human', 'ready', 'pending', 'failed', 'cancelled', 'skipped'];

export default function RunsTab({ part, project }: PartTabProps) {
  const { t, fmt } = useI18n();
  const res = useResource((signal) => listRuns(project.id, { part_id: part.id, limit: 100 }, { signal }), [project.id, part.id]);
  const runs = res.data ?? [];
  const anyActive = runs.some((r) => ACTIVE.has(r.status));

  useEffect(() => {
    if (!anyActive) return;
    const id = setInterval(res.reload, 5000);
    return () => clearInterval(id);
  }, [anyActive, res.reload]);

  if (res.error && !res.data) return <ErrorState error={res.error} onRetry={res.reload} />;
  if (!res.data) return <SkeletonRows rows={5} />;

  return (
    <div className="stack-sm">
      <div className="toolbar">
        <span className="muted small">{t('ws.runs.intro')}</span>
        <span className="grow" />
        <button type="button" className="btn sm" onClick={res.reload} disabled={res.loading}>
          <RefreshCw className={res.loading ? 'spin-icon' : ''} /> {t('ws.runs.refresh')}
        </button>
      </div>
      {!runs.length ? (
        <EmptyState title={t('ws.runs.none')} body={t('ws.runs.noneBody')} icon={<PlayCircle />} />
      ) : (
        <div className="table-wrap">
          <table className="t dense">
            <thead>
              <tr>
                <th>{t('ws.runs.title')}</th>
                <th>{t('ws.runs.origin')}</th>
                <th>{t('ws.runs.status')}</th>
                <th>{t('ws.runs.nodes')}</th>
                <th>{t('ws.runs.created')}</th>
                <th>{t('ws.runs.finished')}</th>
                <th className="act" />
              </tr>
            </thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.id}>
                  <td>
                    <Link to={`/runs/${r.id}`} className="strong">
                      {r.title || r.id.slice(0, 8)}
                    </Link>
                    {r.cancel_requested && r.status !== 'cancelled' && <span className="faint small"> · {t('ws.runs.cancelRequested')}</span>}
                  </td>
                  <td className="small">{t(r.origin === 'workspace' ? 'ws.runs.workspace' : 'ws.runs.workflow')}</td>
                  <td>
                    <RunStatusChip status={r.status} />
                  </td>
                  <td className="small mono">
                    {COUNT_ORDER.filter((s) => r.node_counts[s])
                      .map((s) => `${t(`ws.nodeStatus.${s}`)} ${r.node_counts[s]}`)
                      .join(' · ') || '—'}
                  </td>
                  <td className="small" title={fmt.dateTime(r.created_at)}>
                    {fmt.relative(r.created_at)}
                  </td>
                  <td className="small">{r.finished_at ? fmt.dateTime(r.finished_at) : '—'}</td>
                  <td className="act">
                    <Link className="btn sm ghost" to={`/runs/${r.id}`} aria-label={t('ws.runs.open')}>
                      <ExternalLink />
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

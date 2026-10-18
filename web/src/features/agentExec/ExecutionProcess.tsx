// Node detail "Execution process" (plan §5.4, prototype #run.<id>): the node's latest agent execution in brief — status,
// current step, the last few events, steps / cost against the limits, "open the full record" and stop. Lives in
// the collaboration node panel and the part-canvas node drawer; the full record is /agent-executions/:id.

import { ExternalLink } from 'lucide-react';
import { useMemo } from 'react';
import { Link } from 'react-router-dom';
import type { AgentExecutionBrief } from '../../api/collaboration';
import { paths } from '../../app/nav';
import { ErrorNotice, Skeleton } from '../../components/ui/States';
import { useI18n } from '../../i18n/I18nProvider';
import { asActor, executionRoleLabel } from '../collaboration/common';
import { BudgetMeters, ExecStatusChip, RuntimeChip, SimTag, StopButton } from './common';
import { budgetRows, currentItem, execDisplay, isExecLive } from './model';
import { itemSummary, TimelineEntry } from './Timeline';
import { spanMs, useExecutionLive, useNow } from './useExecution';

const RECENT = 3;

/** The execution to show: a running one first, else the latest started. */
export function pickExecution(list: readonly AgentExecutionBrief[] | null | undefined): AgentExecutionBrief | null {
  const xs = [...(list ?? [])].sort((a, b) => (a.started_at ?? '').localeCompare(b.started_at ?? ''));
  return xs.filter((x) => x.status === 'running').pop() ?? xs.pop() ?? null;
}

function agentName(e: AgentExecutionBrief): string | null {
  const a = asActor(e.agent);
  return a?.agent?.name ?? null;
}

export function ExecutionProcess({ executions, onChanged }: { executions: readonly AgentExecutionBrief[]; onChanged?: () => void }) {
  const { t } = useI18n();
  const current = useMemo(() => pickExecution(executions), [executions]);
  const live = useExecutionLive(current?.id);
  const ex = live.detail.data && live.detail.data.id === current?.id ? live.detail.data : undefined;
  const running = !!ex && isExecLive(ex.status) && !live.finished;
  const now = useNow(running);
  const older = useMemo(() => [...executions].filter((x) => x.id !== current?.id).sort((a, b) => (b.started_at ?? '').localeCompare(a.started_at ?? '')), [executions, current?.id]);

  if (!current) return <p className="faint small">{t('ax.proc.none')}</p>;

  const display = ex ? execDisplay(ex, live.events) : current.status;
  const now1 = currentItem(live.items);
  const recent = live.items.slice(-RECENT);
  const elapsed = ex ? spanMs(ex.started_at, running ? null : ex.finished_at, now) : null;
  const rows = ex ? budgetRows(ex.budget, live.used, running ? elapsed : null) : [];

  return (
    <div className="ax-proc">
      <div className="row">
        <ExecStatusChip display={display} small />
        {ex && <RuntimeChip runtime={ex.runtime} small />}
        {ex?.simulated && <SimTag small />}
        <span className="faint small">
          {[ex?.agent?.name ?? agentName(current), executionRoleLabel(t, current.role), t('ax.head.attempt', { a: current.attempt_no, r: current.rework_no })].filter(Boolean).join(' · ')}
        </span>
      </div>
      {live.detail.error && !ex ? <ErrorNotice error={live.detail.error} onRetry={live.detail.reload} /> : null}

      <div>
        <h4>{t('ax.proc.current')}</h4>
        <div className="ax-now">
          {running && <i className="ax-pulse" aria-hidden />}
          <span>{now1 ? itemSummary(t, now1) : live.state === 'loading' ? t('ax.tl.loading') : t('ax.tl.empty')}</span>
        </div>
      </div>

      {ex ? <BudgetMeters rows={rows} only={['steps', 'tool_calls', 'cost']} /> : !live.detail.error ? <Skeleton w="100%" h={28} /> : null}

      {recent.length > 0 && (
        <div>
          <h4>{t('ax.proc.recent')}</h4>
          <ol className="ax-tl">
            {recent.map((i) => (
              <TimelineEntry key={i.seq} item={i} />
            ))}
          </ol>
        </div>
      )}

      <div className="row">
        <Link className="btn primary sm" to={paths.agentExecution(current.id)}>
          <ExternalLink /> {t('ax.proc.openFull')}
        </Link>
        {ex?.allowed_actions.includes('stop') && (
          <StopButton
            executionId={ex.id}
            onStopped={() => {
              live.detail.reload();
              onChanged?.();
            }}
          />
        )}
      </div>

      {older.length > 0 && (
        <details>
          <summary className="faint small">{t('ax.proc.older')}</summary>
          <ul className="ax-list">
            {older.map((o) => (
              <li key={o.id}>
                <Link className="mono" to={paths.agentExecution(o.id)}>
                  {t('ax.attempt.row', { a: o.attempt_no, r: o.rework_no })}
                </Link>
                <span className="faint small">{executionRoleLabel(t, o.role)}</span>
                <ExecStatusChip display={o.status} small />
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

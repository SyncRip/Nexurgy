// Workbench additions (plan §5.4, prototype #home): "Agents at work" — running executions of the current project,
// each stoppable — and the agent items of "waiting for me": questions and budget used up.
// The agent items come from GET /agent-todos (design §6.5).

import { ArrowRight, Bot, Gauge, MessageCircleQuestion } from 'lucide-react';
import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { listAgentExecutions, listAgentTodos, listItems, type AgentTodo, type AgentTodoKind } from '../../api/agentExecutions';
import { isApiError } from '../../api/client';
import type { Project } from '../../api/types';
import { paths } from '../../app/nav';
import { Chip } from '../../components/ui/Badges';
import { EmptyState, ErrorNotice, SkeletonRows } from '../../components/ui/States';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { dynT, useVisiblePoll } from '../collaboration/common';
import { ExecStatusChip, SimTag, StopButton } from './common';

// ─── to-do items ───────────────────────────────────────────────────────────

/** Agent items of "waiting for me" (GET /agent-todos). */
export const AGENT_TODO_KINDS: readonly AgentTodoKind[] = ['agent_question', 'budget_exhausted'];

/** Where an agent item is handled: its execution, else the node of the collaboration run. */
export function agentTodoPath(r: Pick<AgentTodo, 'execution_id' | 'run_id' | 'node_id'>): string {
  if (r.execution_id) return paths.agentExecution(r.execution_id);
  return paths.collaborationRun(r.run_id, r.node_id);
}

const GROUPS: { kind: AgentTodoKind; icon: ReactNode; title: 'ax.wb.todo.question' | 'ax.wb.todo.budget'; empty: 'ax.wb.todo.noQuestion' | 'ax.wb.todo.noBudget'; act: 'ax.wb.todo.answer' | 'ax.wb.todo.handle' }[] = [
  { kind: 'agent_question', icon: <MessageCircleQuestion aria-hidden />, title: 'ax.wb.todo.question', empty: 'ax.wb.todo.noQuestion', act: 'ax.wb.todo.answer' },
  { kind: 'budget_exhausted', icon: <Gauge aria-hidden />, title: 'ax.wb.todo.budget', empty: 'ax.wb.todo.noBudget', act: 'ax.wb.todo.handle' },
];

/** Agent groups of "waiting for me"; frozen lists omit empty groups. */
export function AgentTodoGroups({ projects, showEmpty = true }: { projects: Project[] | undefined; showEmpty?: boolean }) {
  const { t, fmt } = useI18n();
  const res = useResource((signal) => listAgentTodos({}, { signal }), []);
  useVisiblePoll(res.reload, POLL_MS, !res.error);
  const byId = new Map((projects ?? []).map((p) => [p.id, p]));
  if (res.error && !res.data) return <ErrorNotice error={res.error} onRetry={res.reload} />;
  if (!res.data) return null;
  const rows = res.data.items;
  return (
    <>
      {GROUPS.map((g) => {
        const mine = rows.filter((r) => r.kind === g.kind);
        if (!showEmpty && !mine.length) return null;
        return (
          <div key={g.kind} className="ax-todo-group">
            <h3>
              {g.icon} {t(g.title)}
            </h3>
            {mine.length ? (
              <ul className="wb-todo">
                {mine.map((r) => {
                  const sub = [byId.get(r.project_id)?.code, r.agent.name, r.run_title, r.at ? fmt.relative(r.at) : null].filter(Boolean).join(' · ');
                  // The server's reason is an English sentence: a known block code reads localized instead.
                  const reason = dynT(t, `ax.block.${r.code ?? ''}`, r.reason ?? '');
                  const first = g.kind === 'agent_question' ? r.questions?.[0] || reason : reason;
                  return (
                    <li key={r.node_task_id}>
                      <Link to={agentTodoPath(r)} className="wb-todo-row">
                        <Chip tone={g.kind === 'budget_exhausted' ? 'err' : 'warn'} icon={g.icon} className="wb-type">
                          {t(g.title)}
                        </Chip>
                        <span className="wb-todo-main">
                          <span className="ellip">{r.node_title || first || t(g.title)}</span>
                          <small className="ellip">{[first && r.node_title ? first : null, sub].filter(Boolean).join(' · ')}</small>
                        </span>
                        <span className="wb-todo-open">
                          {t(g.act)} <ArrowRight aria-hidden />
                        </span>
                      </Link>
                    </li>
                  );
                })}
              </ul>
            ) : (
              <p className="ax-todo-empty">{t(g.empty)}</p>
            )}
          </div>
        );
      })}
    </>
  );
}

// ─── agents at work ────────────────────────────────────────────────────────

const POLL_MS = 15_000;
const SHOWN = 6;

export function RunningAgentsCard({ projectId, canStop }: { projectId: string; canStop: boolean }) {
  const { t } = useI18n();
  const res = useResource((signal) => listAgentExecutions({ project_id: projectId, status: 'running', limit: SHOWN + 1 }, { signal }), [projectId]);
  useVisiblePoll(res.reload, POLL_MS, !res.error);
  const rows = listItems(res.data).filter((x) => x.project_id === projectId);
  return (
    <section className="panel wb-card" aria-labelledby="wb-agents">
      <header className="wb-card-head">
        <h2 id="wb-agents">
          <Bot aria-hidden /> {t('ax.wb.live.title')}
          {rows.length > 0 && <span className="wb-count">{rows.length > SHOWN ? `${SHOWN}+` : rows.length}</span>}
        </h2>
        <Link to={paths.flows('executions', projectId)} className="wb-more-link">
          {t('ax.wb.live.all')} <ArrowRight aria-hidden />
        </Link>
      </header>
      {res.error && !res.data ? (
        isApiError(res.error) && res.error.isNotFound ? (
          <p className="faint small">{t('ax.unavailable')}</p>
        ) : (
          <ErrorNotice error={res.error} onRetry={res.reload} />
        )
      ) : !res.data ? (
        <SkeletonRows rows={2} cols={2} />
      ) : !rows.length ? (
        <EmptyState compact icon={<Bot className="big-ico" aria-hidden />} title={t('ax.wb.live.empty')} />
      ) : (
        <ul className="ax-live">
          {rows.slice(0, SHOWN).map((x) => (
            <li key={x.id} className="ax-live-item">
              <div>
                <div className="who">
                  {x.agent?.name ?? '—'} <span className="faint">· {x.node_title || x.node_id}</span>
                </div>
              </div>
              <div className="acts">
                <ExecStatusChip display={x.status} small />
                {x.simulated && <SimTag small />}
                {canStop && <StopButton executionId={x.id} onStopped={res.reload} />}
              </div>
              <div className="what">
                <i className="ax-pulse" aria-hidden />
                <span>{typeof x.used?.steps === 'number' ? t('ax.wb.live.step', { n: x.used.steps }) : t('ax.status.running')}</span>
                <Link to={paths.agentExecution(x.id)}>
                  {t('ax.wb.live.open')} <ArrowRight className="inline-ico" aria-hidden />
                </Link>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

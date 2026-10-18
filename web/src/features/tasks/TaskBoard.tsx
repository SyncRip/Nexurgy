// Task board of the project page (prototype #project.tasks): four columns —
// to do / in progress / in review / accepted. Each card shows the linked part
// and its latest workflow run, the deliverables, the next step, owner,
// reviewer and due day. Status only changes on the task page (no drag and
// drop), and the "Accept" button only opens the decision page (G3): the
// person still reads the submission and decides there.

import './board.css';
import { CalendarClock, ClipboardCheck, User } from 'lucide-react';
import { Link } from 'react-router-dom';
import { isMissingVersion, listTaskDeliverables, type Deliverable } from '../../api/pm';
import type { RunSummary } from '../../api/types';
import { paths } from '../../app/nav';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { dueDayForDisplay, shortDay } from '../../lib/dueDate';
import { NodeCountsBar } from '../workflow/RunTable';
import { BOARD_COLUMNS, boardNext, groupByColumn, type AcceptanceRef, type BoardNext, type BoardTask } from './board';
import { Chip, type Tone } from '../../components/ui/Badges';
import { isOverdue, PriorityChip, TaskStatusIcon, type Members } from './common';

const LANE_TONE: Record<(typeof BOARD_COLUMNS)[number], Tone> = { todo: 'pending', in_progress: 'measure', in_review: 'warn', done: 'ok' };

export interface BoardData {
  /** Part number by id. */
  partNumber: Map<string, string>;
  /** Latest run of each task. */
  runs: Map<string, RunSummary>;
  /** The run list was complete (not cut at its page size). */
  runsComplete: boolean;
  /** Pending acceptance request of each task in review. */
  acceptance: Map<string, AcceptanceRef>;
}

/** Deliverable chips: selected drafts while in progress, the submitted ones in review. */
function Deliverables({ task, items }: { task: BoardTask; items: Deliverable[] | undefined }) {
  const { t } = useI18n();
  if (task.status === 'todo') return <span className="faint small">{t('pa.card.noDeliverables')}</span>;
  if (!items) return null;
  if (!items.length) return <span className="faint small">{t('pa.card.noDeliverables')}</span>;
  return (
    <>
      {items.map((d) => (
        <span key={d.id} className="tkb-del-chip" title={isMissingVersion(d.version) ? undefined : d.version.asset_name}>
          {isMissingVersion(d.version) ? t('pa.card.missingVersion') : `${d.version.asset_name} · ${d.version.label}`}
        </span>
      ))}
      {task.status === 'in_progress' && <small className="faint">{t('pa.card.notSubmitted')}</small>}
    </>
  );
}

function NextStep({ next, task, members }: { next: BoardNext; task: BoardTask; members: Members }) {
  const { t } = useI18n();
  switch (next.kind) {
    case 'accept':
      return (
        <div className="tkb-next act">
          <span>{t('pa.next.accept')}</span>
          {/* Opens the decision page; it never approves by itself. */}
          <Link className="btn sm primary" to={paths.approvalRequest(next.requestId)}>
            <ClipboardCheck /> {t('pa.next.acceptBtn')}
          </Link>
        </div>
      );
    case 'submit':
      return (
        <div className={`tkb-next ${next.mine ? 'act' : ''}`}>
          <span>{t('pa.next.submit', { n: next.count })}</span>
          {next.mine && (
            <Link className="btn sm" to={paths.task(task.id)}>
              {t('pa.next.submitBtn')}
            </Link>
          )}
        </div>
      );
    case 'awaitAcceptance':
      return <div className="tkb-next">{next.agent ? t('pa.next.awaitAgent') : next.reviewerId ? t('pa.next.await', { name: members.name(next.reviewerId) }) : t('pa.next.awaitNobody')}</div>;
    case 'start':
      return <div className="tkb-next">{next.mine ? t('pa.next.startMine') : next.ownerId ? t('pa.next.start', { name: members.name(next.ownerId) }) : t('pa.next.startNobody')}</div>;
    case 'blocked':
      return (
        <div className="tkb-next warn" title={next.reason}>
          {t('pa.next.blocked', { reason: next.reason })}
        </div>
      );
    case 'waitingOn':
      return <div className="tkb-next">{t('pa.next.waitingOn', { keys: next.keys.join(', ') })}</div>;
    case 'pickDeliverables':
      return <div className="tkb-next">{t('pa.next.pick')}</div>;
    case 'working':
      return <div className="tkb-next faint">{t('pa.next.working')}</div>;
    case 'done':
      return <div className="tkb-next">{t('pa.next.done')}</div>;
    default:
      return null;
  }
}

/** Part and latest run: links to the run on the part canvas (or the run page). */
function FlowLink({ task, data }: { task: BoardTask; data: BoardData }) {
  const { t } = useI18n();
  const run = data.runs.get(task.id);
  const partId = run?.part_id ?? task.part_id;
  if (!run && !partId) return null;
  const number = partId ? data.partNumber.get(partId) : undefined;
  // "No run yet" only where it is actionable (collaboration node tasks run inside a collaboration run, not a task run).
  const claimNoRun = data.runsComplete && (task.status === 'todo' || task.status === 'in_progress');
  const to = run ? (run.part_id ? `${paths.part(run.part_id, 'workflow')}?run=${encodeURIComponent(run.id)}` : paths.run(run.id)) : paths.part(partId!);
  return (
    <Link className="tkb-flow" to={to} aria-label={t('pa.card.openFlow', { name: run?.title || number || task.key })}>
      {number ? <span className="mono">{number}</span> : <span className="faint small">{t('pa.card.noPart')}</span>}
      <span className="faint ellip">{run ? run.title || t('tk.run.untitled') : claimNoRun ? t('pa.card.noRun') : ''}</span>
      {run && <NodeCountsBar counts={run.node_counts} />}
    </Link>
  );
}

function BoardCard({ task, members, userId, data }: { task: BoardTask; members: Members; userId: string | null | undefined; data: BoardData }) {
  const { t, fmt } = useI18n();
  const wantsDeliverables = task.status === 'in_progress' || task.status === 'in_review';
  const del = useResource((signal) => listTaskDeliverables(task.id, { signal }), [task.id, task.version], { enabled: wantsDeliverables });
  const next = boardNext(task, { userId, deliverables: del.data ? del.data.length : null, acceptance: data.acceptance.get(task.id) });
  const late = isOverdue(task.due_date, task.status);
  return (
    <article className="tkb-card" aria-label={`${task.key} ${task.title}`}>
      <div className="tkb-h">
        <Link to={paths.task(task.id)} className="tkb-title">
          {task.title}
        </Link>
        <span className="mono faint small">{task.key}</span>
      </div>
      {(task.priority === 'high' || task.priority === 'urgent') && (
        <span className="row">
          <PriorityChip priority={task.priority} hideNormal />
        </span>
      )}
      <FlowLink task={task} data={data} />
      {task.status !== 'done' && (
        <div className="tkb-del">
          <Deliverables task={task} items={del.data} />
        </div>
      )}
      <NextStep next={next} task={task} members={members} />
      <div className="tkb-f">
        <span>
          <User aria-hidden /> {members.name(task.owner_id)}
        </span>
        <span>{t('pa.card.reviewer', { name: task.reviewer_id ? members.name(task.reviewer_id) : t('tk.unassigned') })}</span>
        {task.due_date && (
          <span className={`mono ${late ? 'err-text' : ''}`} title={fmt.date(dueDayForDisplay(task.due_date))}>
            {late && <CalendarClock aria-hidden />}
            {t(late ? 'pa.card.overdue' : 'pa.card.due', { date: shortDay(task.due_date) })}
          </span>
        )}
      </div>
    </article>
  );
}

export function TaskBoard({
  tasks,
  members,
  userId,
  data,
  refreshing,
}: {
  tasks: BoardTask[];
  members: Members;
  userId: string | null | undefined;
  data: BoardData;
  refreshing?: boolean;
}) {
  const { t, fmt } = useI18n();
  const cols = groupByColumn(tasks);
  return (
    <div className={`tkb-board ${refreshing ? 'is-refreshing' : ''}`}>
      {BOARD_COLUMNS.map((c) => (
        <section key={c} className="tkb-lane" aria-label={t(`pa.col.${c}`)}>
          <header className="tkb-lane-h">
            <Chip tone={LANE_TONE[c]} icon={<TaskStatusIcon status={c} />}>
              {t(`pa.col.${c}`)}
            </Chip>
            <b className="mono">{fmt.number(cols[c].length)}</b>
          </header>
          {cols[c].length ? (
            cols[c].map((task) => <BoardCard key={task.id} task={task} members={members} userId={userId} data={data} />)
          ) : (
            <span className="faint small tkb-empty">{t('pa.board.empty')}</span>
          )}
        </section>
      ))}
    </div>
  );
}

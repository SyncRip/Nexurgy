import { useFeature } from '../../api/capabilities';
// AI workflow board (R1.2 prototype #flows.runs): drafts / running / needs
// attention / finished, one card per run with its node progress and, when it
// needs someone, why. Cards only open the run (or part canvas / collaboration
// page); nothing on the board decides an approval or edits a run.

import './flows.css';
import { Bot, ListFilter, RefreshCw, Waypoints } from 'lucide-react';
import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { listApprovalRequests } from '../../api/approvals';
import { listCollaborationDrafts, listCollaborationRuns, summaryRow } from '../../api/collaboration';
import { getRun, listParts, listRuns } from '../../api/endpoints';
import type { NodeRun, Part, RunSummary, Workflow } from '../../api/types';
import { paths } from '../../app/nav';
import { useUser } from '../../auth/AuthProvider';
import { EmptyState, ErrorNotice, ErrorState, SkeletonRows } from '../../components/ui/States';
import { useResource } from '../../hooks/useResource';
import { useI18n, type TFunction } from '../../i18n/I18nProvider';
import type { ProjectTabProps } from '../../pages/project/tabProps';
import { dynT, useMembers } from '../tasks/common';
import { RunStatusChip } from '../workflow/icons';
import { nodeStatusLabel, stageLabel } from '../workflow/labels';
import { TERMINAL_RUN } from '../workflow/model';
import { NodeCountsBar } from '../workflow/RunTable';
import { BOARD_LIMIT, useFlowsData } from './data';
import { buildBoard, CELL_ORDER, LANES, needOf, progressOf, requestsByRun, runHref, runsNeedingDetail, type BoardItem, type Lane, type NeedReason } from './board';

/** Finished cards shown before "show all". */
const DONE_VISIBLE = 8;

export function NodeCells({ counts }: { counts: RunSummary['node_counts'] }) {
  const { t } = useI18n();
  const p = progressOf(counts);
  if (!p.total) return <span className="faint small">{t('fl.card.noNodes')}</span>;
  if (!p.cells.length) return <NodeCountsBar counts={counts} />;
  const label = [
    t('fl.card.progress', { done: p.succeeded, total: p.total }),
    ...CELL_ORDER.filter((s) => counts[s]).map((s) => `${nodeStatusLabel(t, s)} ${counts[s]}`),
  ].join(', ');
  return (
    <span className="fl-cells" role="img" aria-label={label} title={label}>
      <span className="fl-cells-row" aria-hidden>
        {p.cells.map((s, i) => (
          <i key={i} className={`fl-cell ns-${s}`} />
        ))}
      </span>
      <span className="mono faint small">
        {p.succeeded}/{p.total}
      </span>
    </span>
  );
}

function needText(t: TFunction, need: NeedReason): string {
  const more = 'more' in need && need.more > 0 ? ` · ${t('fl.need.more', { n: need.more })}` : '';
  switch (need.kind) {
    case 'approval':
      return `${need.stage ? t('fl.need.approval', { stage: stageLabel(t, need.stage) }) : t('fl.need.approvalAny')} · ${need.assignee ? t('fl.need.assignee', { name: need.assignee }) : t('fl.need.anyMember')}${more}`;
    case 'acceptance':
      return `${t('fl.need.acceptance', { title: need.title })}${need.assignee ? ` · ${t('fl.need.assignee', { name: need.assignee })}` : ''}${more}`;
    case 'question':
      return `${t('fl.need.question', { text: need.text })}${more}`;
    case 'manual_entry':
      return t('fl.need.manualEntry');
    case 'waiting':
      return need.n ? t('fl.need.waiting', { n: need.n }) : t('fl.need.waitingAny');
    case 'failed':
      return need.n ? t('fl.need.failed', { n: need.n }) : t('fl.need.failedAny');
    case 'cancelling':
      return t('fl.need.cancelling');
  }
}

const NEED_TONE: Record<NeedReason['kind'], string> = { approval: 'warn', acceptance: 'warn', question: 'warn', manual_entry: 'warn', waiting: 'warn', failed: 'err', cancelling: 'pending' };

interface CardCtx {
  parts: Map<string, Part>;
  collab: Map<string, string | null>;
  collabIds: ReadonlySet<string>;
  requests: ReturnType<typeof requestsByRun>;
  /** Nodes of waiting runs whose reason needs the run detail. */
  nodes: Map<string, NodeRun[]>;
  person: (id: string | undefined) => string;
}

function RunCard({ run, ctx }: { run: RunSummary; ctx: CardCtx }) {
  const { t, fmt } = useI18n();
  const part = run.part_id ? ctx.parts.get(run.part_id) : undefined;
  const isCollab = ctx.collab.has(run.id);
  const mode = ctx.collab.get(run.id);
  const need = needOf(run, ctx.requests.get(run.id), ctx.nodes.get(run.id));
  const title = run.title || t('wf.run.untitled');
  let source: string;
  if (isCollab) source = `${t('fl.card.collab')}${mode ? ` · ${t(`collab.mode.${mode}` as 'collab.mode.full_auto')}` : ''}`;
  else if (run.origin === 'workspace') source = t('fl.card.workspace');
  else if (run.template_version) source = `${run.template_version.name} v${run.template_version.version_no}`;
  else source = '';
  return (
    <Link className="fl-card" to={runHref(run, ctx.collabIds)} aria-label={t('fl.card.open', { title })}>
      <span className="fl-card-title">
        {part && <span className="mono">{part.number}</span>}
        {part && <span className="faint"> · </span>}
        <span>{title}</span>
      </span>
      <span className="fl-card-sub">
        <span className="mono">{run.id.slice(0, 8)}</span>
        {source && <span className="ellip"> · {source}</span>}
        {(isCollab || run.origin === 'workspace') && (
          <span className="fl-card-kind" aria-hidden>
            {isCollab ? <Waypoints /> : <Bot />}
          </span>
        )}
      </span>
      <NodeCells counts={run.node_counts} />
      {need && <span className={`fl-need ${NEED_TONE[need.kind]}`}>{needText(t, need)}</span>}
      {(run.status === 'cancelled' || run.status === 'queued') && (
        <span>
          <RunStatusChip status={run.status} />
        </span>
      )}
      {run.task && <span className="fl-card-task">{t('fl.card.task', { key: run.task.key, status: dynT(t, `tk.status.${run.task.status}`, run.task.status) })}</span>}
      <span className="fl-card-sub" title={fmt.dateTime(run.updated_at ?? run.created_at)}>
        {ctx.person(run.created_by)} · {fmt.relative(run.updated_at ?? run.created_at)}
      </span>
    </Link>
  );
}

function DraftCard({ w }: { w: Workflow }) {
  const { t, fmt } = useI18n();
  const n = w.graph.nodes.length;
  return (
    <Link className="fl-card" to={paths.collaborationDraft(w.id)} aria-label={t('fl.card.open', { title: w.name })}>
      <span className="fl-card-title">{w.name || t('collab.run.untitled')}</span>
      <span className="fl-card-sub wrap">
        <span>{t('fl.card.draft', { n })}</span>
        <span className="fl-card-kind" aria-hidden>
          <Waypoints />
        </span>
      </span>
      <NodeCells counts={{ pending: n }} />
      <span className="fl-card-sub" title={fmt.dateTime(w.updated_at)}>
        {t('fl.card.draftUpdated', { time: fmt.relative(w.updated_at) })}
      </span>
    </Link>
  );
}

function LaneColumn({ lane, items, ctx }: { lane: Lane; items: BoardItem[]; ctx: CardCtx }) {
  const { t } = useI18n();
  const [all, setAll] = useState(false);
  const capped = lane === 'done' && !all && items.length > DONE_VISIBLE;
  const shown = capped ? items.slice(0, DONE_VISIBLE) : items;
  const id = `fl-lane-${lane}`;
  return (
    <section className={`fl-lane lane-${lane}`} aria-labelledby={id}>
      <h3 className="fl-lane-h" id={id}>
        <span>{t(`fl.lane.${lane}`)}</span>
        <b className="mono">{items.length}</b>
      </h3>
      {shown.map((x) => (x.kind === 'run' ? <RunCard key={x.run.id} run={x.run} ctx={ctx} /> : <DraftCard key={x.workflow.id} w={x.workflow} />))}
      {!items.length && <span className="faint small fl-lane-empty">{t('fl.lane.empty')}</span>}
      {lane === 'done' && items.length > DONE_VISIBLE && (
        <button type="button" className="btn ghost sm" onClick={() => setAll((v) => !v)}>
          {all ? t('fl.lane.showLess') : t('fl.lane.showAll', { n: items.length })}
        </button>
      )}
      {lane === 'done' && items.length > 0 && <p className="faint small fl-lane-note">{t('fl.lane.doneNote')}</p>}
    </section>
  );
}

export function RunsBoard({ project, onOpenList, viewSwitch }: ProjectTabProps & { onOpenList: () => void; viewSwitch?: ReactNode }) {
  const { t } = useI18n();
  const me = useUser();
  const shared = useFlowsData(project.id);
  const ownRuns = useResource((signal) => listRuns(project.id, { limit: BOARD_LIMIT + 1 }, { signal }), [project.id], { enabled: !shared });
  const collaborationEnabled = useFeature('collaboration');
  const ownDrafts = useResource((signal) => listCollaborationDrafts(project.id, { signal }), [project.id, collaborationEnabled], { enabled: !shared && collaborationEnabled });
  const runs = shared?.runs ?? ownRuns;
  const drafts = shared?.drafts ?? (collaborationEnabled ? ownDrafts : { ...ownDrafts, data: [] });
  const collab = useResource((signal) => listCollaborationRuns(project.id, { limit: 200 }, { signal }), [project.id]);
  const requests = useResource((signal) => listApprovalRequests({ scope: 'all', project_id: project.id, status: 'pending', limit: 200 }, { signal }), [project.id]);
  const parts = useResource((signal) => listParts(project.id, { include_archived: true }, { signal }), [project.id]);
  const members = useMembers(project.id);

  const limited = (runs.data?.length ?? 0) > BOARD_LIMIT;
  const visibleRuns = useMemo(() => (runs.data ?? []).slice(0, BOARD_LIMIT), [runs.data]);
  const board = useMemo(() => buildBoard(visibleRuns, drafts.data ?? []), [visibleRuns, drafts.data]);
  const anyActive = visibleRuns.some((r) => !TERMINAL_RUN.has(r.status));

  const byRun = useMemo(() => requestsByRun(requests.data ?? []), [requests.data]);
  // Questions / manual entry are only in the run detail: load it for the few waiting runs without a request.
  const detailIds = requests.data ? runsNeedingDetail(visibleRuns, byRun) : [];
  const detailKey = detailIds.join(',');
  const details = useResource((signal) => Promise.all(detailKey.split(',').map((id) => getRun(id, { signal }))), [detailKey], { enabled: !!detailKey });

  const ctx: CardCtx = useMemo(() => {
    const modes = new Map(
      (collab.data ?? [])
        .map(summaryRow)
        .filter((r): r is NonNullable<ReturnType<typeof summaryRow>> => !!r)
        .map((r) => [r.runId, r.mode] as const),
    );
    return {
      parts: new Map((parts.data ?? []).map((p) => [p.id, p])),
      collab: modes,
      collabIds: new Set(modes.keys()),
      requests: byRun,
      nodes: new Map((details.data ?? []).map((r) => [r.id, r.nodes])),
      person: (id) => (id ? (id === me.id ? t('common.you') : members.name(id)) : '—'),
    };
  }, [parts.data, collab.data, byRun, details.data, members, me.id, t]);

  // Keep the board fresh while something runs or waits (runs, their reasons and collaboration state).
  const reloadRuns = runs.reload;
  const reloadRequests = requests.reload;
  const reloadCollab = collab.reload;
  useEffect(() => {
    if (!anyActive) return;
    const h = window.setInterval(() => {
      reloadRuns();
      reloadRequests();
      reloadCollab();
    }, 5000);
    return () => window.clearInterval(h);
  }, [anyActive, reloadRuns, reloadRequests, reloadCollab]);

  const refresh = () => {
    runs.reload();
    drafts.reload();
    requests.reload();
    collab.reload();
  };

  let body;
  if (runs.error && !runs.data) body = <ErrorState error={runs.error} onRetry={runs.reload} />;
  else if (runs.initial) body = <SkeletonRows cols={4} />;
  else if (!visibleRuns.length && !board.draft.length) body = <EmptyState title={t('fl.board.empty')} body={t('fl.board.emptyBody')} />;
  else
    body = (
      <div className="fl-board" aria-label={t('fl.board.label')} role="group">
        {LANES.map((lane) => (
          <LaneColumn key={lane} lane={lane} items={board[lane]} ctx={ctx} />
        ))}
      </div>
    );

  return (
    <div className="stack">
      {/* Standard toolbar row: real result count on the left; board/list switch
          and refresh on the right (shared shape with the list view). */}
      <div className="fl-board-bar">
        <span className="fl-bar-count" aria-live="polite">
          {runs.data ? t('fl.board.count', { n: LANES.reduce((sum, lane) => sum + board[lane].length, 0) }) : ''}
        </span>
        <span className="grow" />
        {anyActive && <span className="faint small">{t('wf.runs.autoRefresh')}</span>}
        {viewSwitch}
        <button type="button" className="btn sm icon" onClick={refresh} aria-label={t('wf.run.refresh')} title={t('wf.run.refresh')}>
          <RefreshCw />
        </button>
      </div>
      {runs.error && runs.data ? <ErrorNotice error={runs.error} onRetry={runs.reload} /> : null}
      {requests.error && !requests.data ? <ErrorNotice error={requests.error} onRetry={requests.reload} /> : null}
      {body}
      {limited && (
        <p className="faint small">
          {t('fl.board.limited', { n: BOARD_LIMIT })}{' '}
          <button type="button" className="btn ghost sm" onClick={onOpenList}>
            <ListFilter aria-hidden /> {t('fl.board.openList')}
          </button>
        </p>
      )}
    </div>
  );
}

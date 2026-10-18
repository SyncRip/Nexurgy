// Node detail panel of the workbench (UI02, 360–440px; full screen on phones):
// tabs task / criteria / submission / activity, the execution timeline, budget
// against the frozen limits, inputs / candidates / formal outputs, and the
// actions the server allows. Full-auto nodes never show a human approve
// button; semi-auto nodes link to the human final acceptance (UI03).

import { Activity, CheckCircle2, CircleDashed, CircleDot, CircleX, ExternalLink, Eye, Hand, History, Info, ListChecks, Package, Send, X } from 'lucide-react';
import { useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import type { ExecutionMode } from '../../api/agents';
import type { ActivityEntry, AgentExecutionBrief, CriterionDecision, NodeTaskActionOut, NodeTaskOut, ReviewResult } from '../../api/collaboration';
import { getTask, type Criterion } from '../../api/pm';
import { paths } from '../../app/nav';
import { Chip } from '../../components/ui/Badges';
import { ErrorNotice, SkeletonRows } from '../../components/ui/States';
import { useResource } from '../../hooks/useResource';
import { useI18n, type TFunction } from '../../i18n/I18nProvider';
import { HashText } from '../assets/meta';
import { ruleSummary } from '../tasks/RuleSpecEditor';
import { SubmissionStatusChip, TaskStatusChip, VersionRef } from '../tasks/common';
import { TurboNodeNote } from '../turbo/TurboNode';
import { formatDuration } from '../workflow/model';
import { ActorLabel, ExecutionStatusChip, PhaseChip, StageDots, asActor, blockedReasonText, dynT, executionRoleLabel } from './common';
import { AnswerQuestions } from './AnswerQuestions';
import { EvidenceReads } from './EvidenceReads';
import { displayPhase } from './model';
import { NodeActions } from './NodeActions';
import { ExecutionProcess } from '../agentExec/ExecutionProcess';

// process: the agent's live execution in brief (agent execution layer P1, prototype #run.<id>); agent nodes only.
const ALL_TABS = ['task', 'criteria', 'submission', 'process', 'activity'] as const;
type TabId = (typeof ALL_TABS)[number];

function TabIcon({ id }: { id: TabId }) {
  switch (id) {
    case 'task':
      return <Package aria-hidden />;
    case 'criteria':
      return <ListChecks aria-hidden />;
    case 'submission':
      return <Send aria-hidden />;
    case 'process':
      return <Activity aria-hidden />;
    default:
      return <History aria-hidden />;
  }
}

// ─── verdicts ──────────────────────────────────────────────────────────────

export function VerdictChip({ result }: { result: CriterionDecision['result'] | string }) {
  const { t } = useI18n();
  if (result === 'pass')
    return (
      <Chip tone="ok" icon={<CheckCircle2 aria-hidden />} className="cl-chip-sm">
        {t('collab.verdict.pass')}
      </Chip>
    );
  if (result === 'fail')
    return (
      <Chip tone="err" icon={<CircleX aria-hidden />} className="cl-chip-sm">
        {t('collab.verdict.fail')}
      </Chip>
    );
  return (
    <Chip tone="warn" icon={<CircleDashed aria-hidden />} className="cl-chip-sm">
      {t('collab.verdict.unknown')}
    </Chip>
  );
}

/** Per-criterion verdict table (final decision or pre-review); a tool_agent review also lists what it read (P4). */
export function VerdictTable({ decisions, criteria }: { decisions: CriterionDecision[]; criteria?: Map<string, Pick<Criterion, 'seq' | 'description'>> }) {
  const { t } = useI18n();
  if (!decisions.length) return <p className="faint small">{t('collab.verdict.none')}</p>;
  return (
    <div className="table-wrap">
      <table className="cl-verdicts">
        <thead>
          <tr>
            <th>{t('collab.verdict.criterion')}</th>
            <th>{t('collab.verdict.result')}</th>
            <th>{t('collab.verdict.reason')}</th>
          </tr>
        </thead>
        <tbody>
          {decisions.map((d) => {
            const c = criteria?.get(d.criterion_id);
            return (
              <tr key={d.criterion_id}>
                <td>{c ? `#${c.seq} ${c.description}` : <span className="mono faint">{d.criterion_id.slice(0, 8)}</span>}</td>
                <td>
                  <VerdictChip result={d.result} />
                </td>
                <td>
                  <span className="ap-note">{d.reason}</span>
                  {d.evidence_asset_version_ids.length > 0 && <span className="faint small"> · {t('collab.verdict.evidenceN', { n: d.evidence_asset_version_ids.length })}</span>}
                  {d.evidence && d.evidence.length > 0 && <EvidenceReads reads={d.evidence} />}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/** Agent pre-review, always labelled as not the final decision. */
export function PreReviewBlock({ result, agentName, at, criteria, open = false }: { result: ReviewResult | null; agentName: ReactNode; at: string | null; criteria?: Map<string, Pick<Criterion, 'seq' | 'description'>>; open?: boolean }) {
  const { t, fmt } = useI18n();
  const verdicts = result?.criteria ?? [];
  const passed = verdicts.length > 0 && verdicts.every((v) => v.result === 'pass');
  return (
    <details className="cl-sec panel" open={open}>
      <summary className="panel-h">
        <h3>{t('collab.preReview.title')}</h3>
        <span className="warn-text small">({t('collab.preReview.notFinal')})</span>
      </summary>
      <div className="panel-b stack-sm">
        <div className="row small">
          {verdicts.length ? <VerdictChip result={passed ? 'pass' : verdicts.some((v) => v.result === 'fail') ? 'fail' : 'unknown'} /> : null}
          <span className="faint">
            {agentName} · {fmt.dateTime(at)}
          </span>
        </div>
        {result?.summary && <p className="ap-note small">{result.summary}</p>}
        <VerdictTable decisions={verdicts} criteria={criteria} />
      </div>
    </details>
  );
}

// ─── timeline ──────────────────────────────────────────────────────────────

function ExecIcon({ status }: { status: string }) {
  if (status === 'delivered') return <CheckCircle2 aria-hidden />;
  if (status === 'failed' || status === 'blocked') return <CircleX aria-hidden />;
  if (status === 'running') return <CircleDot aria-hidden />;
  return <CircleDashed aria-hidden />;
}

function nextStepText(t: TFunction, node: NodeTaskOut, mode: ExecutionMode | null): string {
  const humanExec = node.executor.kind === 'human';
  switch (node.phase) {
    case 'ready':
      return humanExec ? t('collab.next.readyHuman') : t('collab.next.readyAgent');
    case 'executing':
      return humanExec ? t('collab.next.executingHuman') : t('collab.next.executingAgent');
    case 'submitted':
      return node.pre_reviewer ? t('collab.next.preReview') : t('collab.next.submitted');
    case 'in_review':
      return mode === 'full_auto' || node.reviewer?.kind === 'agent' ? t('collab.next.agentReview') : t('collab.next.humanReview');
    case 'rework':
      return t('collab.next.rework');
    case 'blocked':
      return node.blocked_reason ? blockedReasonText(t, node.blocked_reason) : t('collab.node.waitingUpstream');
    case 'accepted':
      return t('collab.next.accepted');
    case 'failed':
      return t('collab.next.failed');
    case 'completed_unverified':
      return displayPhase(node) === 'check_failed' ? t('ver.node.checkFailed') : t('tb.node.next');
    default:
      return t('collab.next.cancelled');
  }
}

function Timeline({ node, mode }: { node: NodeTaskOut; mode: ExecutionMode | null }) {
  const { t, fmt } = useI18n();
  const execs: AgentExecutionBrief[] = [...(node.executions ?? [])].sort((a, b) => (a.started_at ?? '').localeCompare(b.started_at ?? ''));
  const final = node.phase === 'accepted' || node.phase === 'cancelled';
  return (
    <ol className="cl-timeline" aria-label={t('collab.node.timeline')}>
      {execs.map((e) => (
        <li key={e.id}>
          <span className={`cl-tl-dot ${e.status === 'delivered' ? 'ok' : e.status === 'failed' || e.status === 'blocked' ? 'err' : e.status === 'running' ? 'active' : ''}`}>
            <ExecIcon status={e.status} />
          </span>
          <span className="cl-tl-main">
            <span className="row">
              <span className="strong">{executionRoleLabel(t, e.role)}</span>
              <ExecutionStatusChip status={e.status} />
            </span>
            <span className="faint small">
              <ActorLabel actor={asActor(e.agent)} /> · {t('collab.node.attempt', { a: e.attempt_no, r: e.rework_no })}
            </span>
            <span className="faint small">
              {fmt.dateTime(e.started_at)}
              {e.duration_ms ? ` · ${formatDuration(e.duration_ms)}` : ''}
            </span>
            {e.error && (e.error.message || e.error.code) && (
              <span className="small err-text">
                {e.error.message} {e.error.code && <code className="error-code">{e.error.code}</code>}
              </span>
            )}
          </span>
        </li>
      ))}
      <li>
        <span className={`cl-tl-dot ${node.phase === 'accepted' ? 'ok' : node.phase === 'blocked' || node.phase === 'failed' ? 'err' : final ? '' : 'active'}`}>
          {node.phase === 'accepted' ? <CheckCircle2 aria-hidden /> : node.phase === 'blocked' ? <Hand aria-hidden /> : <CircleDot aria-hidden />}
        </span>
        <span className="cl-tl-main">
          <span className="strong">{dynT(t, `collab.phase.${node.phase}`, node.phase)}</span>
          <span className="faint small">{nextStepText(t, node, mode)}</span>
        </span>
      </li>
    </ol>
  );
}

// ─── budget ────────────────────────────────────────────────────────────────

function Budget({ node }: { node: NodeTaskOut }) {
  const { t, fmt } = useI18n();
  const b = node.budget ?? {};
  const l = node.limits ?? {};
  const pair = (used: number | undefined, max: number | null | undefined) => `${fmt.number(used ?? 0)} / ${max === null || max === undefined ? '—' : fmt.number(max)}`;
  const costUnknown = b.cost_unknown !== false || b.cost_fen === null || b.cost_fen === undefined;
  const money = (fen: number) => {
    try {
      return fmt.number(fen / 100, { style: 'currency', currency: 'CNY' });
    } catch {
      return `${fmt.number(fen / 100)} CNY`;
    }
  };
  return (
    <dl className="cl-budget">
      <div>
        <dt>{t('collab.budget.steps')}</dt>
        <dd>{pair(b.steps, l.max_steps)}</dd>
      </div>
      <div>
        <dt>{t('collab.budget.duration')}</dt>
        <dd>
          {b.duration_ms ? formatDuration(b.duration_ms) : '0'}
          {l.max_duration_s ? ` / ${t('collab.budget.perAttempt', { s: fmt.number(l.max_duration_s) })}` : ''}
        </dd>
      </div>
      <div>
        <dt>{t('collab.budget.attempts')}</dt>
        <dd>{pair(b.attempts, l.max_attempts)}</dd>
      </div>
      <div>
        <dt>{t('collab.budget.reworks')}</dt>
        <dd>{pair(b.reworks, l.max_reworks)}</dd>
      </div>
      <div>
        <dt>{t('collab.budget.cost')}</dt>
        <dd>
          {costUnknown ? <span className="faint">{t('collab.budget.costUnknown')}</span> : money(b.cost_fen as number)}
          {' / '}
          {l.max_cost_fen === null || l.max_cost_fen === undefined ? <span className="faint">{t('agents.costUnset')}</span> : money(l.max_cost_fen)}
        </dd>
      </div>
    </dl>
  );
}

// ─── panel ─────────────────────────────────────────────────────────────────

export function NodeDetail({
  runId,
  node,
  mode,
  activity,
  onClose,
  onNode,
  onReload,
}: {
  runId: string;
  node: NodeTaskOut;
  mode: ExecutionMode | null;
  activity: ActivityEntry[];
  onClose: () => void;
  onNode: (n: NodeTaskActionOut) => void;
  onReload: () => void;
}) {
  const { t, fmt } = useI18n();
  const [tab, setTab] = useState<TabId>('task');
  const hasAgent = node.executor.kind === 'agent' || (node.executions ?? []).length > 0;
  const TABS: readonly TabId[] = hasAgent ? ALL_TABS : ALL_TABS.filter((x) => x !== 'process');
  useEffect(() => {
    if (!hasAgent && tab === 'process') setTab('task');
  }, [hasAgent, tab]);
  const tabRefs = useRef<Record<string, HTMLButtonElement | null>>({});
  const ref = useRef<HTMLElement>(null);
  const taskId = node.task?.id ?? node.task_id;
  // Criteria (with ids) come from the task; loaded for the criteria / submission tabs only.
  const taskRes = useResource((signal) => getTask(taskId!, { signal }), [taskId, node.version], { enabled: !!taskId && (tab === 'criteria' || tab === 'submission') });
  const critMap = new Map((taskRes.data?.criteria ?? []).map((c) => [c.id, c]));

  useEffect(() => {
    ref.current?.focus();
  }, [node.node_id]);

  const onTabKey = (e: KeyboardEvent<HTMLButtonElement>, i: number) => {
    let j = -1;
    if (e.key === 'ArrowRight') j = (i + 1) % TABS.length;
    else if (e.key === 'ArrowLeft') j = (i - 1 + TABS.length) % TABS.length;
    else if (e.key === 'Home') j = 0;
    else if (e.key === 'End') j = TABS.length - 1;
    if (j < 0) return;
    e.preventDefault();
    setTab(TABS[j]);
    tabRefs.current[TABS[j]]?.focus();
  };

  const sub = node.current_submission;
  const semi = mode === 'semi_auto';
  const requestId = node.final_decision?.request_id ?? sub?.request_id ?? null;
  const nodeActivity = activity.filter((a) => a.node_id === node.node_id);
  const preAgent = asActor(node.pre_review?.agent ?? null);

  return (
    <aside className="cl-detail" aria-label={t('collab.node.detailLabel', { title: node.title })} tabIndex={-1} ref={ref} onKeyDown={(e) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        onClose();
      }
    }}>
      <header className="cl-detail-head">
        <h2 className="ellip" title={node.title}>
          {node.title || node.node_id}
        </h2>
        <PhaseChip phase={displayPhase(node)} />
        <button type="button" className="btn ghost icon sm" onClick={onClose} aria-label={t('collab.node.close')}>
          <X />
        </button>
      </header>
      <div className="tabs" role="tablist" aria-label={t('collab.node.tabs')}>
        {TABS.map((id, i) => (
          <button
            key={id}
            ref={(el) => {
              tabRefs.current[id] = el;
            }}
            type="button"
            role="tab"
            id={`cl-tab-${id}`}
            aria-selected={tab === id}
            aria-controls={`cl-panel-${id}`}
            tabIndex={tab === id ? 0 : -1}
            className={`tab ${tab === id ? 'active' : ''}`}
            onClick={() => setTab(id)}
            onKeyDown={(e) => onTabKey(e, i)}
          >
            <TabIcon id={id} />
            <span>{t(`collab.node.tab.${id}`)}</span>
          </button>
        ))}
      </div>
      <div className="cl-detail-body" role="tabpanel" id={`cl-panel-${tab}`} aria-labelledby={`cl-tab-${tab}`}>
        {tab === 'task' && (
          <>
            <StageDots phase={node.phase} />
            <TurboNodeNote node={node} />
            {node.phase === 'blocked' && node.blocked_reason && (
              <div className="callout err slim" role="note">
                <Hand aria-hidden />
                <span>
                  {blockedReasonText(t, node.blocked_reason)} <code className="error-code">{node.blocked_reason.code}</code>
                </span>
              </div>
            )}
            {node.phase === 'blocked' && node.blocked_reason?.code === 'agent_blocked' && (
              <AnswerQuestions runId={runId} node={node} onAnswered={() => onReload()} />
            )}
            {(node.clarifications ?? []).length > 0 && (
              <details className="cl-answers-done">
                <summary>{t('collab.answers.given', { n: node.clarifications!.length })}</summary>
                <ul>
                  {node.clarifications!.map((c, i) => (
                    <li key={i}>
                      <span className="faint">{c.question}</span> — {c.answer}
                    </li>
                  ))}
                </ul>
              </details>
            )}
            {node.objective && <p className="ap-note">{node.objective}</p>}
            <dl className="cl-kv">
              <dt>{t('collab.node.taskKey')}</dt>
              <dd>
                {node.task ? (
                  <span className="row">
                    <Link to={paths.task(node.task.id)} className="mono">
                      {node.task.key}
                    </Link>
                    <TaskStatusChip status={node.task.status} />
                  </span>
                ) : (
                  <span className="faint">{t('collab.node.noTask')}</span>
                )}
              </dd>
              <dt>{t('collab.node.executor')}</dt>
              <dd>
                <ActorLabel actor={node.executor} />
              </dd>
              <dt>{semi ? t('collab.node.reviewerHuman') : t('collab.node.reviewerAgent')}</dt>
              <dd>
                <ActorLabel actor={node.reviewer} fallback={node.verification || node.turbo_binding ? t('tb.draft.noReviewer') : undefined} />
              </dd>
              {node.pre_reviewer && (
                <>
                  <dt>{t('collab.node.preReviewer')}</dt>
                  <dd>
                    <ActorLabel actor={node.pre_reviewer} />
                  </dd>
                </>
              )}
            </dl>

            <section className="cl-sec">
              <h3>{t('collab.node.inputs')}</h3>
              {node.bound_inputs?.length ? (
                <ul className="cl-slots">
                  {node.bound_inputs.map((b) => (
                    <li key={b.slot_key}>
                      <span className="cl-slot">{b.slot_key}</span>
                      <VersionRef v={b.version} compact />
                      {b.from_node_id && <span className="faint small">← {b.from_node_id}</span>}
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="faint small">{node.input_slots?.length ? t('collab.node.inputsPending') : t('collab.node.noInputs')}</p>
              )}
            </section>
            <section className="cl-sec">
              <h3>{t('collab.node.candidates')}</h3>
              {node.candidates?.length ? (
                <ul className="cl-slots">
                  {node.candidates.map((v) => (
                    <li key={v.id}>
                      <VersionRef v={v} compact />
                      <Chip tone="pending" className="cl-chip-sm">
                        {t('collab.node.candidateTag')}
                      </Chip>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="faint small">{t('collab.node.noCandidates')}</p>
              )}
            </section>
            {node.formal_outputs?.length > 0 && (
              <section className="cl-sec">
                <h3>{t('collab.node.formal')}</h3>
                <ul className="cl-slots">
                  {node.formal_outputs.map((f) => (
                    <li key={f.slot_key}>
                      <span className="cl-slot">{f.slot_key}</span>
                      <VersionRef v={f.version} compact />
                    </li>
                  ))}
                </ul>
              </section>
            )}

            <section className="cl-sec">
              <h3>{t('collab.node.progress')}</h3>
              <Timeline node={node} mode={mode} />
            </section>
            <section className="cl-sec">
              <h3>{t('collab.node.budget')}</h3>
              <Budget node={node} />
            </section>

            <section className="cl-sec">
              <h3>{t('collab.node.actions')}</h3>
              {mode === 'full_auto' ? (
                <div className="callout info slim" role="note">
                  <Info aria-hidden />
                  <span>{t('collab.node.fullAutoNote')}</span>
                </div>
              ) : semi && node.phase === 'in_review' && requestId ? (
                <Link className="btn sm" to={paths.approvalRequest(requestId)}>
                  <Eye /> {t('collab.node.openAcceptance')}
                </Link>
              ) : null}
              <NodeActions runId={runId} node={node} onNode={onNode} onReload={onReload} />
              {!node.actions.length && <p className="faint small">{t('collab.node.noActions')}</p>}
            </section>
          </>
        )}

        {tab === 'criteria' && (
          <>
            {taskRes.error && !taskRes.data ? (
              <ErrorNotice error={taskRes.error} onRetry={taskRes.reload} />
            ) : !taskRes.data ? (
              <SkeletonRows rows={3} cols={2} />
            ) : taskRes.data.criteria.length ? (
              <ol className="cl-crit-list">
                {[...taskRes.data.criteria]
                  .sort((a, b) => a.seq - b.seq)
                  .map((c) => {
                    const fin = node.final_decision?.criterion_decisions?.find((d) => d.criterion_id === c.id);
                    const pre = node.pre_review?.result?.criteria?.find((d) => d.criterion_id === c.id);
                    return (
                      <li key={c.id} className="cl-crit">
                        <div className="cl-crit-head">
                          <span className="mono faint">#{c.seq}</span>
                          <span className="strong grow">{c.description}</span>
                          <Chip tone={c.required ? 'measure' : 'pending'} className="cl-chip-sm">
                            {c.required ? t('ap.crit.required') : t('ap.crit.optional')}
                          </Chip>
                          {c.manual_required && (
                            <Chip tone="measure" className="cl-chip-sm">
                              {t('ap.crit.manual')}
                            </Chip>
                          )}
                        </div>
                        <span className="tag-list">
                          {ruleSummary(t, c).map((x) => (
                            <span key={x} className="tag">
                              {x}
                            </span>
                          ))}
                        </span>
                        {fin && (
                          <div className="row small">
                            <span className="faint">{t('collab.verdict.final')}</span>
                            <VerdictChip result={fin.result} />
                            <span className="ap-note">{fin.reason}</span>
                          </div>
                        )}
                        {pre && (
                          <div className="row small">
                            <span className="faint">{t('collab.verdict.pre')}</span>
                            <VerdictChip result={pre.result} />
                            <span className="ap-note">{pre.reason}</span>
                          </div>
                        )}
                      </li>
                    );
                  })}
              </ol>
            ) : (
              <p className="faint small">{t('ap.acc.noCriteria')}</p>
            )}
          </>
        )}

        {tab === 'submission' && (
          <>
            <section className="cl-sec">
              <h3>{t('collab.node.currentSubmission')}</h3>
              {sub ? (
                <dl className="cl-kv">
                  <dt>{t('ap.acc.submission')}</dt>
                  <dd className="row">
                    <span className="mono">#{sub.seq}</span>
                    <SubmissionStatusChip status={sub.status} />
                  </dd>
                  <dt>{t('ap.acc.snapshotHash')}</dt>
                  <dd>
                    <HashText hash={sub.snapshot_hash} chars={16} />
                  </dd>
                  <dt>{t('ap.acc.submittedAt')}</dt>
                  <dd>{fmt.dateTime(sub.submitted_at)}</dd>
                  {sub.note && (
                    <>
                      <dt>{t('ap.acc.note')}</dt>
                      <dd className="ap-note">{sub.note}</dd>
                    </>
                  )}
                  {sub.request_id && (
                    <>
                      <dt>{t('collab.node.request')}</dt>
                      <dd>
                        <Link to={paths.approvalRequest(sub.request_id)}>
                          <ExternalLink className="inline-ico" aria-hidden /> {semi ? t('collab.node.openAcceptance') : t('collab.node.viewRequest')}
                        </Link>
                      </dd>
                    </>
                  )}
                </dl>
              ) : (
                <p className="faint small">{t('collab.node.noSubmission')}</p>
              )}
            </section>
            {node.pre_review && (
              <PreReviewBlock result={node.pre_review.result} agentName={<ActorLabel actor={preAgent} />} at={node.pre_review.at} criteria={critMap} />
            )}
            <section className="cl-sec">
              <h3>{t('collab.node.finalDecision')}</h3>
              {node.final_decision ? (
                <div className="stack-sm">
                  <div className="row small">
                    <Chip tone={node.final_decision.decision === 'approved' ? 'ok' : 'err'} icon={node.final_decision.decision === 'approved' ? <CheckCircle2 aria-hidden /> : <CircleX aria-hidden />}>
                      {t(node.final_decision.decision === 'approved' ? 'collab.decision.approved' : 'collab.decision.rejected')}
                    </Chip>
                    <ActorLabel actor={node.final_decision.actor} />
                    <span className="faint">{fmt.dateTime(node.final_decision.at)}</span>
                  </div>
                  {node.final_decision.comment && <p className="ap-note small">“{node.final_decision.comment}”</p>}
                  <VerdictTable decisions={node.final_decision.criterion_decisions ?? []} criteria={critMap} />
                </div>
              ) : (
                <p className="faint small">{t('collab.node.noDecision')}</p>
              )}
            </section>
          </>
        )}

        {tab === 'process' && hasAgent && <ExecutionProcess executions={node.executions ?? []} onChanged={onReload} />}

        {tab === 'activity' && (
          <>
            {nodeActivity.length ? (
              <ul className="cl-activity">
                {nodeActivity.map((a, i) => (
                  <li key={`${a.at}-${i}`}>
                    <span className="small">{dynT(t, `collab.activity.${a.kind}`, a.message || a.kind)}</span>
                    <span className="faint small">
                      <ActorLabel actor={a.actor} fallback={t('collab.actor.system')} /> · {fmt.dateTime(a.at)}
                    </span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="faint small">{t('collab.node.noActivity')}</p>
            )}
          </>
        )}
      </div>
    </aside>
  );
}

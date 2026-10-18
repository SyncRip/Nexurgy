// Flow cards of a conversation (.trellis/tasks/archive/2026-10/10-12-ai-work-panel design §2.4, §5): a run or a workflow the
// conversation follows, shown for a `card` message. The run card is live (useLiveRun: SSE with a polling fallback,
// nothing kept open once the run finished): one deterministic sentence of where it is, the steps that need a person
// handled right here through the run's own services (answer, approve / send back against the review's target hash,
// retry), and the exact versions it produced with "view" and "continue from this result". The workflow card shows
// the version and what blocks a run, and starts one in the conversation. Everything else opens in the work panel.
// The AI decides nothing here: every action is the person's, checked by the server.
//
// (Named FlowCards.tsx, not FlowCard.tsx: on a case-insensitive file system './FlowCard' would resolve to the pure
// helpers in flowCard.ts.)

import {
  ArrowRightCircle,
  Check,
  CheckCircle2,
  Eye,
  Hand,
  MessageCircleQuestionMark,
  PanelRight,
  Play,
  PlayCircle,
  RotateCcw,
  ScanText,
  Send,
  Sparkles,
  TriangleAlert,
  Undo2,
  UserCheck,
  Workflow as WorkflowIcon,
} from 'lucide-react';
import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { startConversationRun, type AiCardPayload, type AiConversationDetail, type AiMessage } from '../../api/assistant';
import { isApiError } from '../../api/client';
import { decideRunNode, getWorkflow, retryRunNode } from '../../api/endpoints';
import type { ApprovalDecision, ApprovalStage, NodeStatus, UUID, WorkflowRun } from '../../api/types';
import { paths } from '../../app/nav';
import { assetWorkspacePath } from '../../app/versionLinks';
import { ACTION_ROLES, STAGE_ACTION } from '../../auth/permissions';
import { PolicyChip, UnverifiedChip } from '../../components/engineering/EngineeringChips';
import { Chip } from '../../components/ui/Badges';
import { ErrorNotice } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { useResource } from '../../hooks/useResource';
import type { ErrorOverrides } from '../../i18n/errors';
import { useI18n, type TFunction } from '../../i18n/I18nProvider';
import type { FlowCardKey } from '../../i18n/messages/flowCard.en';
import { GeomCheckChip } from '../cad/Verdicts';
import { ExecutionContextLine, ObjectStateBadge } from '../orchestration/StateBadge';
import { runDeliveryState, runExecutionContext } from '../orchestration/status';
import { runShowsUnverified } from '../turbo/model';
import { onWorkflowChanged } from '../workflow/events';
import { RunStatusChip } from '../workflow/icons';
import { graphErrorText, nodeDisplayName, nodeErrorText, stageLabel } from '../workflow/labels';
import { formatDuration } from '../workflow/model';
import { isSimulated } from '../workflow/run/Artifacts';
import { useLiveRun } from '../workflow/run/useLiveRun';
import '../workflow/workflow.css';
import { useAssistant, useAssistantSession } from './AssistantProvider';
import { AsError, ResultLabels, SimChip, tk } from './common';
import type { ComposerPrefill } from './Composer';
import { pendingItems, resultItems, runDigest, runOutcome, type PendingItem, type ResultItem, type RunDigest, type RunOutcome } from './flowCard';
import { useWorkPanel } from './panel/WorkPanelContext';
import { runOutputRef, runRef } from './refs';

type Prefill = (p: Omit<ComposerPrefill, 'nonce'>) => void;

export function FlowCard({ m, payload, detail, archived, onPrefill }: { m: AiMessage; payload: AiCardPayload; detail: AiConversationDetail; archived: boolean; onPrefill: Prefill }) {
  return payload.object.kind === 'run' ? (
    <RunCard key={payload.object.id} runId={payload.object.id} workflowId={payload.workflow_id ?? null} m={m} detail={detail} archived={archived} onPrefill={onPrefill} />
  ) : (
    <WorkflowCard key={payload.object.id} workflowId={payload.object.id} m={m} detail={detail} archived={archived} />
  );
}

// ─── text ──────────────────────────────────────────────────────────────────

const DIGEST_KEY: Record<NodeStatus, FlowCardKey> = {
  succeeded: 'fc.digest.succeeded',
  waiting_human: 'fc.digest.waiting_human',
  running: 'fc.digest.running',
  ready: 'fc.digest.ready',
  failed: 'fc.digest.failed',
  pending: 'fc.digest.pending',
  cancelled: 'fc.digest.cancelled',
  skipped: 'fc.digest.skipped',
};

function digestText(t: TFunction, d: RunDigest): string {
  if (d.allDone) return t('fc.digest.allDone', { n: d.total });
  if (!d.segments.length) return t('fc.digest.empty');
  const parts = d.segments.map((s) => t(DIGEST_KEY[s.status], { name: nodeDisplayName(t, s) }));
  if (d.more > 0) parts.push(t('fc.digest.more', { n: d.more }));
  return parts.join(t('fc.digest.sep'));
}

/** A run that did not succeed (failed, partial) never reads "finished" (S6). */
function outcomeText(t: TFunction, o: RunOutcome): string {
  const vars = { done: o.done, total: o.total, failed: o.failed, results: o.results };
  const head = o.status === 'succeeded' ? t('fc.outcome.succeeded', vars) : o.status === 'cancelled' ? t('fc.outcome.cancelled', vars) : t('fc.outcome.failed', vars);
  return o.durationMs !== null ? head + t('fc.outcome.took', { d: formatDuration(o.durationMs) }) : head;
}

/** The workflow's name when the conversation knows it (a reference to it), else null. */
function knownWorkflowName(detail: AiConversationDetail, workflowId: UUID | null): string | null {
  if (!workflowId) return null;
  const refs = [...detail.composer.references, ...detail.messages.flatMap((m) => [...(m.target ? [m.target] : []), ...m.references])];
  return refs.find((r) => r.kind === 'workflow' && (r.workflow_id ?? r.object_id) === workflowId)?.title ?? null;
}

// ─── run card ──────────────────────────────────────────────────────────────

/** Whether the element is (near) the visible part of its scroll container; true where IntersectionObserver is missing. */
function useOnScreen(el: HTMLElement | null): boolean {
  const [on, setOn] = useState(true);
  useEffect(() => {
    if (!el || typeof IntersectionObserver === 'undefined') return;
    const io = new IntersectionObserver((entries) => setOn(entries.some((e) => e.isIntersecting)), { rootMargin: '200px 0px' });
    io.observe(el);
    return () => io.disconnect();
  }, [el]);
  return on;
}

function RunCard({ runId, workflowId, m, detail, archived, onPrefill }: { runId: UUID; workflowId: UUID | null; m: AiMessage; detail: AiConversationDetail; archived: boolean; onPrefill: Prefill }) {
  const { t } = useI18n();
  const panel = useWorkPanel();
  const assistant = useAssistant();
  // Only a card on screen streams its run (each stream is a connection and a server read loop); one scrolled out of
  // view keeps its last state and reconnects when it comes back.
  const [root, setRoot] = useState<HTMLElement | null>(null);
  const onScreen = useOnScreen(root);
  const live = useLiveRun(runId, { live: onScreen });
  const [summaryBusy, setSummaryBusy] = useState(false);
  const run = live.run;
  const projectId = detail.conversation.project.id;

  /** "View": locate the node when this run is already open next to the chat, else open the run there. */
  const showNode = (nodeId: string) => {
    const o = panel.state.object;
    if (panel.inline && o?.kind === 'run' && o.id === runId) panel.focusNode(nodeId);
    else panel.open({ kind: 'run', id: runId }, { nodeId });
  };
  const openPanel = () => panel.open({ kind: 'run', id: runId });

  if (!run) {
    return (
      <article ref={setRoot} className="as-flow" aria-label={m.text}>
        <header className="as-flow-h">
          <PlayCircle className="as-flow-ico" aria-hidden />
          <span className="as-flow-kind">{t('fc.run.kind')}</span>
          <span className="as-flow-title">{m.text}</span>
        </header>
        {live.error ? <AsError error={live.error} onRetry={live.reload} /> : <p className="as-op-progress" role="status">{t('fc.loading')}</p>}
        <footer className="as-flow-foot">
          <button type="button" className="btn sm" onClick={openPanel}>
            <PanelRight /> {t('fc.open')}
          </button>
        </footer>
      </article>
    );
  }

  const outcome = runOutcome(run);
  const pending = pendingItems(run);
  const results = resultItems(run);
  const simulated = run.nodes.some((n) => isSimulated(n.outputs, n.steps));
  const wfName = knownWorkflowName(detail, workflowId ?? run.workflow_id);
  const target = assistant.pending.references.find((r) => r.target)?.input ?? null;

  const askSummary = async () => {
    setSummaryBusy(true);
    // The run is a reference of the question (not its target); the person reads the question and sends it.
    await assistant.addContext(runRef(projectId, run.id), { asTarget: false, open: false });
    setSummaryBusy(false);
    onPrefill({ text: t('fc.run.askSummaryText', { title: run.title }), operation: 'ask' });
  };

  return (
    <article ref={setRoot} className={`as-flow st-${run.status}`} aria-label={`${t('fc.run.kind')} · ${run.title}`}>
      <header className="as-flow-h">
        <PlayCircle className="as-flow-ico" aria-hidden />
        <span className="as-flow-kind">{t('fc.run.kind')}</span>
        <span className="as-flow-title">{run.title}</span>
        <RunStatusChip status={run.status} />
        {/* generation reliability N: the state of what the run delivered, apart from the run / check status */}
        <ObjectStateBadge state={runDeliveryState(run)} />
        {run.execution_policy === 'turbo' && <PolicyChip policy="turbo" />}
        {runShowsUnverified(run) && <UnverifiedChip />}
        {simulated && <SimChip />}
      </header>
      <ExecutionContextLine ctx={runExecutionContext(run)} />
      {(wfName || run.workflow_version !== null) && (
        <p className="as-flow-sub">{wfName ? t('fc.run.workflow', { name: wfName, v: run.workflow_version ?? '—' }) : t('fc.run.workflowVersion', { v: run.workflow_version ?? '—' })}</p>
      )}
      <p className="as-flow-digest">{outcome ? outcomeText(t, outcome) : digestText(t, runDigest(run))}</p>

      {pending.length > 0 && (
        <section className="as-flow-sec" aria-label={t('fc.pending.title', { n: pending.length })}>
          <h4 className="as-flow-sec-h">{t('fc.pending.title', { n: pending.length })}</h4>
          {pending.map((p) => (
            <PendingRow key={p.key} item={p} run={run} archived={archived} onRun={live.setRun} onReload={live.reload} onView={showNode} />
          ))}
        </section>
      )}

      {results.length > 0 && (
        <section className="as-flow-sec" aria-label={t('fc.results.title', { n: results.length })}>
          <h4 className="as-flow-sec-h">{t('fc.results.title', { n: results.length })}</h4>
          <ul className="as-flow-res-list">
            {results.map((r) => (
              <ResultRow
                key={`${r.nodeId}:${r.outputKey}:${r.assetVersionId}`}
                item={r}
                run={run}
                projectId={projectId}
                archived={archived}
                isTarget={target?.kind === 'run_output' && target.object_id === r.assetVersionId && target.output?.run_id === run.id && target.output.node_id === r.nodeId && target.output.output_key === r.outputKey}
              />
            ))}
          </ul>
        </section>
      )}

      {outcome && (
        <div className="as-flow-end">
          {outcome.results > 0 && <p className="as-op-note">{t('fc.outcome.note')}</p>}
          <button type="button" className="btn sm" onClick={() => void askSummary()} disabled={archived || summaryBusy} title={t('fc.run.askSummaryHint')}>
            <Sparkles /> {t('fc.run.askSummary')}
          </button>
        </div>
      )}

      <footer className="as-flow-foot">
        <button type="button" className="btn sm" onClick={openPanel}>
          <PanelRight /> {t('fc.open')}
        </button>
      </footer>
    </article>
  );
}

// ─── blocking items ────────────────────────────────────────────────────────

interface RowProps<K extends PendingItem['kind']> {
  item: Extract<PendingItem, { kind: K }>;
  run: WorkflowRun;
  archived: boolean;
  onRun: (r: WorkflowRun) => void;
  onReload: () => void;
  onView: (nodeId: string) => void;
}

function PendingRow(p: { item: PendingItem } & Omit<RowProps<PendingItem['kind']>, 'item'>) {
  const { item, ...rest } = p;
  switch (item.kind) {
    case 'clarification':
      return <ClarificationRow item={item} {...rest} />;
    case 'approval':
      return <ApprovalRow item={item} {...rest} />;
    case 'failed':
      return <FailedRow item={item} {...rest} />;
    default:
      return <PanelRow item={item} {...rest} />;
  }
}

const DECISION_ERRORS: ErrorOverrides = {
  forbidden: 'wf.review.forbidden',
  answers_required: 'wf.apiErr.answers_required',
  subject_changed: 'wf.review.changed',
  subject_stale: 'wf.review.stale',
};

/** A decision refused because the step moved on (changed subject, no longer waiting): reload the run. */
function DecisionError({ error, onReload }: { error: unknown; onReload: () => void }) {
  if (!error) return null;
  const moved = isApiError(error) && ['subject_changed', 'subject_stale', 'not_waiting'].includes(error.code);
  return <ErrorNotice error={error} overrides={DECISION_ERRORS} onRetry={moved ? onReload : undefined} />;
}

/** can_decide is false: who can (the server decides; the buttons are not shown). */
function CannotDecide({ stage }: { stage: ApprovalStage }) {
  const { t } = useI18n();
  const roles = ACTION_ROLES[STAGE_ACTION[stage]].map((r) => t(`role.${r}`)).join(' / ');
  return (
    <p className="as-flow-note">
      <Hand aria-hidden className="inline-ico" /> {t('wf.review.cannotDecide', { roles })}
    </p>
  );
}

function ClarificationRow({ item, run, archived, onRun, onReload, onView }: RowProps<'clarification'>) {
  const { t } = useI18n();
  const toast = useToast();
  const [answers, setAnswers] = useState<string[]>(() => item.questions.map(() => ''));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const complete = answers.length === item.questions.length && answers.every((a) => a.trim());

  const submit = async () => {
    if (!complete) return;
    setBusy(true);
    setError(null);
    try {
      onRun(await decideRunNode(run.id, item.nodeId, { decision: 'approved', answers: answers.map((a) => a.trim()) }));
      toast(t('wf.clar.sentToast'));
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form
      className="as-flow-item as-clarify"
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
    >
      <div className="as-clarify-h">
        <MessageCircleQuestionMark aria-hidden /> {t('fc.clar.title', { name: nodeDisplayName(t, item) })}
      </div>
      {item.canDecide ? (
        item.questions.map((q, i) => (
          <label key={i} className="as-clarify-q">
            <span>
              {i + 1}. {q}
            </span>
            <input
              className="input sm"
              value={answers[i] ?? ''}
              maxLength={2000}
              disabled={archived || busy}
              required
              onChange={(e) => setAnswers((a) => a.map((x, j) => (j === i ? e.target.value : x)))}
            />
          </label>
        ))
      ) : (
        <ol className="as-flow-qs">
          {item.questions.map((q, i) => (
            <li key={i}>{q}</li>
          ))}
        </ol>
      )}
      <div className="as-flow-acts">
        {item.canDecide && (
          <button type="submit" className="btn sm primary" disabled={archived || busy || !complete}>
            <Send /> {busy ? t('common.working') : t('fc.clar.submit')}
          </button>
        )}
        <button type="button" className="btn sm ghost" onClick={() => onView(item.nodeId)}>
          <Eye /> {t('fc.view')}
        </button>
      </div>
      {!item.canDecide && <CannotDecide stage="design" />}
      <DecisionError error={error} onReload={onReload} />
    </form>
  );
}

function ApprovalRow({ item, run, archived, onRun, onReload, onView }: RowProps<'approval'>) {
  const { t } = useI18n();
  const toast = useToast();
  const [comment, setComment] = useState('');
  const [busy, setBusy] = useState<ApprovalDecision | null>(null);
  const [error, setError] = useState<unknown>(null);
  const hasComment = !!comment.trim();

  const decide = async (decision: ApprovalDecision) => {
    if (decision === 'rejected' && !hasComment) return;
    setBusy(decision);
    setError(null);
    try {
      // Same decision as the run page and the approval center, against the reviewed version (target hash).
      onRun(await decideRunNode(run.id, item.nodeId, { decision, comment: comment.trim(), expected_hash: item.targetHash }));
      toast(decision === 'approved' ? t('wf.review.approvedToast') : t('wf.review.rejectedToast'));
    } catch (e) {
      setError(e);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="as-flow-item">
      <div className="as-flow-item-h">
        <UserCheck aria-hidden />
        <span>{t('fc.approval.title', { name: nodeDisplayName(t, item) })}</span>
        <span className="faint small">· {stageLabel(t, item.stage)}</span>
      </div>
      {item.instructions && <p className="as-flow-note">{item.instructions}</p>}
      {item.canDecide && (
        <input
          className="input sm as-flow-comment"
          value={comment}
          maxLength={2000}
          placeholder={t('fc.approval.commentHint')}
          aria-label={t('fc.approval.comment')}
          disabled={archived || busy !== null}
          onChange={(e) => setComment(e.target.value)}
        />
      )}
      <div className="as-flow-acts">
        <button type="button" className="btn sm" onClick={() => onView(item.nodeId)}>
          <Eye /> {t('fc.approval.basis')}
        </button>
        {item.canDecide && (
          <>
            <button type="button" className="btn sm primary" onClick={() => void decide('approved')} disabled={archived || busy !== null}>
              <Check /> {busy === 'approved' ? t('common.working') : t('fc.approval.approve')}
            </button>
            <button
              type="button"
              className="btn sm danger"
              onClick={() => void decide('rejected')}
              disabled={archived || busy !== null || !hasComment}
              title={hasComment ? undefined : t('fc.approval.needComment')}
            >
              <Undo2 /> {busy === 'rejected' ? t('common.working') : t('fc.approval.sendBack')}
            </button>
          </>
        )}
      </div>
      {!item.canDecide && <CannotDecide stage={item.stage} />}
      <DecisionError error={error} onReload={onReload} />
    </div>
  );
}

/** Manual entry (a form too large for a card) and waiting steps without details: explained, handled in the panel. */
function PanelRow({ item, onView }: RowProps<'manual_entry' | 'waiting'>) {
  const { t } = useI18n();
  const name = nodeDisplayName(t, item);
  const manual = item.kind === 'manual_entry';
  return (
    <div className="as-flow-item">
      <div className="as-flow-item-h">
        {manual ? <ScanText aria-hidden /> : <Hand aria-hidden />}
        <span>{manual ? t('fc.manual.title', { name }) : t('fc.waiting.title', { name })}</span>
      </div>
      {manual && <p className="as-flow-note">{(item.reason && tk(t, `wf.manual.reason.${item.reason}`, '')) || t('fc.manual.body')}</p>}
      <div className="as-flow-acts">
        <button type="button" className="btn sm primary" onClick={() => onView(item.nodeId)}>
          <PanelRight /> {t('fc.manual.handle')}
        </button>
      </div>
      {manual && !item.canDecide && <CannotDecide stage="design" />}
    </div>
  );
}

function FailedRow({ item, run, archived, onRun, onView }: RowProps<'failed'>) {
  const { capabilities } = useAssistantSession();
  // Retrying a step needs the project "workflow" action, the same one workflow_edit needs (services/assistant.OP_ACTION).
  const mayRetry = capabilities?.operations.workflow_edit?.reason !== 'no_permission';
  const { t } = useI18n();
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const retry = async () => {
    setBusy(true);
    setError(null);
    try {
      onRun(await retryRunNode(run.id, item.nodeId));
      toast(t('wf.run.retried'));
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="as-flow-item is-err">
      <div className="as-flow-item-h">
        <TriangleAlert aria-hidden />
        <span>{t('fc.failed.title', { name: nodeDisplayName(t, item) })}</span>
        <span className="faint small">· {t('fc.failed.attempts', { a: item.attempt, m: item.maxAttempts })}</span>
      </div>
      <p className="as-flow-note">
        {nodeErrorText(t, item.code, item.message)}
        {item.code && <code className="error-code">{t('common.errorCode', { code: item.code })}</code>}
      </p>
      <div className="as-flow-acts">
        {item.canRetry && mayRetry && (
          <button type="button" className="btn sm primary" onClick={() => void retry()} disabled={archived || busy}>
            <RotateCcw /> {busy ? t('common.working') : t('fc.failed.retry')}
          </button>
        )}
        {item.canRetry && !mayRetry && <span className="faint small">{t('fc.failed.noPermission')}</span>}
        <button type="button" className="btn sm" onClick={() => onView(item.nodeId)}>
          <Eye /> {t('fc.view')}
        </button>
      </div>
      <ErrorNotice error={error} />
    </div>
  );
}

// ─── results ───────────────────────────────────────────────────────────────

function ResultRow({ item, run, projectId, archived, isTarget }: { item: ResultItem; run: WorkflowRun; projectId: UUID; archived: boolean; isTarget: boolean }) {
  const { t } = useI18n();
  const toast = useToast();
  const panel = useWorkPanel();
  const assistant = useAssistant();
  const session = useAssistantSession();
  const [busy, setBusy] = useState(false);
  const typeLabel = tk(t, `tk.assetType.${item.assetType ?? ''}`, item.assetType ?? item.outputKey);
  const name = item.name ?? nodeDisplayName(t, { type: item.nodeType, label: item.nodeLabel });
  const revision = item.target?.kind === 'model_revision' ? item.target.id : null;
  // Other outputs open their own page at this exact version (versionLinks), else the asset version page.
  const viewPath = revision
    ? null
    : (item.target && item.assetType ? assetWorkspacePath(item.assetType, { id: item.target.id, part_id: item.target.partId }) : null) ??
      (item.assetId ? paths.assetVersion(item.assetId, item.assetVersionId) : null);

  const continueFrom = async () => {
    setBusy(true);
    await assistant.addContext(runOutputRef(projectId, run.id, { node_id: item.nodeId, output_key: item.outputKey, asset_version_id: item.assetVersionId }), {
      asTarget: true,
      open: false,
    });
    setBusy(false);
    toast(t('as.result.continued', { label: `${typeLabel} ${item.versionLabel ?? ''}`.trim() }), { tone: 'info' });
  };

  return (
    <li className="as-flow-res">
      <div className="as-flow-res-h">
        <span className="as-result-type">{typeLabel}</span>
        <span className="as-flow-res-name">{name}</span>
        {item.versionLabel ? <span className="as-result-ver mono">{item.versionLabel}</span> : <span className="faint small">{t('fc.results.missing')}</span>}
        {item.availability && <span className="faint small">{tk(t, `as.avail.${item.availability}`, item.availability)}</span>}
        <ResultLabels labels={item.labels} />
        {item.assetType === 'part_model' && <GeomCheckChip status={item.check} short />}
        {isTarget && (
          <Chip tone="accent" className="sm">
            {t('as.result.isTarget')}
          </Chip>
        )}
      </div>
      {!item.missing && (
        <div className="as-flow-acts">
          {revision ? (
            <button type="button" className="btn sm" onClick={() => panel.open({ kind: 'revision', id: revision })}>
              <Eye /> {t('fc.view')}
            </button>
          ) : (
            viewPath && (
              <Link className="btn sm" to={viewPath} onClick={() => session.keepDockOnLeave()}>
                <Eye /> {t('fc.view')}
              </Link>
            )
          )}
          <button
            type="button"
            className="btn sm"
            onClick={() => void continueFrom()}
            disabled={busy || archived || isTarget}
            title={t('as.result.continueHint', { label: `${typeLabel} ${item.versionLabel ?? ''}`.trim() })}
          >
            <ArrowRightCircle /> {t('as.result.continue')}
          </button>
        </div>
      )}
    </li>
  );
}

// ─── workflow card ─────────────────────────────────────────────────────────

const MAX_PROBLEMS = 3;

function WorkflowCard({ workflowId, m, detail, archived }: { workflowId: UUID; m: AiMessage; detail: AiConversationDetail; archived: boolean }) {
  const { t } = useI18n();
  const toast = useToast();
  const panel = useWorkPanel();
  const { store, bumpList, requestIds } = useAssistantSession();
  const wf = useResource((signal) => getWorkflow(workflowId, { signal }), [workflowId]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const reloadWf = wf.reload;

  // A new version saved in the panel or by a plan: show it.
  useEffect(
    () =>
      onWorkflowChanged((e) => {
        if (e.workflowId === workflowId) reloadWf();
      }),
    [workflowId, reloadWf],
  );

  const w = wf.data;
  const problems = w?.validation ?? [];
  const blocked = !w || problems.length > 0 || !!w.archived_at;

  // One client_request_id per attempt, kept until the server answered: a click after a lost response returns the
  // run that was already started instead of a second one.
  const scope = `flowcard.run.${m.id}`;
  const start = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await startConversationRun(detail.conversation.id, { workflow_id: workflowId, client_request_id: requestIds.take(scope) });
      requestIds.forget(scope);
      store.reload(); // the run card message
      bumpList();
      toast(t('fc.run.started', { title: res.run.title }), { tone: 'info' });
      panel.open({ kind: 'run', id: res.run.id });
    } catch (e) {
      // Refused (nothing started): the next click is a new attempt.
      if (isApiError(e) && e.status >= 400 && e.status < 500) requestIds.forget(scope);
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <article className={`as-flow wf ${problems.length ? 'has-problems' : ''}`} aria-label={`${t('fc.wf.kind')} · ${w?.name ?? m.text}`}>
      <header className="as-flow-h">
        <WorkflowIcon className="as-flow-ico" aria-hidden />
        <span className="as-flow-kind">{t('fc.wf.kind')}</span>
        <span className="as-flow-title">{w?.name ?? m.text}</span>
        {w && <span className="as-result-ver mono">v{w.version}</span>}
        {w?.archived_at && (
          <Chip tone="pending" className="sm">
            {t('fc.wf.archived')}
          </Chip>
        )}
      </header>
      {w ? (
        <>
          <p className="as-flow-sub">{t('fc.wf.nodes', { n: w.graph.nodes.length })}</p>
          {problems.length > 0 ? (
            <div className="as-flow-problems">
              <p className="as-flow-digest">
                <TriangleAlert aria-hidden className="inline-ico warn-text" /> {t('fc.wf.problems', { n: problems.length })}
              </p>
              <ul className="as-wfe-warn">
                {problems.slice(0, MAX_PROBLEMS).map((e, i) => {
                  const node = e.node_id ? w.graph.nodes.find((n) => n.id === e.node_id) : undefined;
                  return (
                    <li key={i} title={e.message}>
                      {node ? `${nodeDisplayName(t, node)}: ` : ''}
                      {graphErrorText(t, e, node?.type)}
                    </li>
                  );
                })}
                {problems.length > MAX_PROBLEMS && <li>{t('fc.wf.problemsMore', { n: problems.length - MAX_PROBLEMS })}</li>}
              </ul>
            </div>
          ) : (
            <p className="as-flow-digest">
              <CheckCircle2 aria-hidden className="inline-ico" /> {t('fc.wf.ready')}
            </p>
          )}
        </>
      ) : wf.error ? (
        <AsError error={wf.error} onRetry={wf.reload} />
      ) : (
        <p className="as-op-progress" role="status">
          {t('fc.loading')}
        </p>
      )}
      <footer className="as-flow-foot">
        <button type="button" className="btn sm" onClick={() => panel.open({ kind: 'workflow', id: workflowId })}>
          <PanelRight /> {t('fc.wf.openPanel')}
        </button>
        <button
          type="button"
          className="btn sm primary"
          onClick={() => void start()}
          disabled={archived || busy || blocked}
          title={problems.length ? t('fc.wf.runBlocked') : undefined}
        >
          <Play /> {busy ? t('fc.wf.starting') : t('fc.wf.run')}
        </button>
        <span className="faint small">{problems.length ? t('fc.wf.runBlocked') : t('fc.wf.note')}</span>
      </footer>
      <AsError error={error} />
    </article>
  );
}

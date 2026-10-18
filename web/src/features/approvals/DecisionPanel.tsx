// Approve / reject a request with a comment, sending the target hash the
// reviewer saw. Explains every refusal the server can return and shows an
// idempotent repeat (the same decision again) as "nothing changed".

import { Bot,Check,CircleCheck,CircleHelp,CircleX,Hand,Info,RefreshCw,TriangleAlert,UserCog,X } from 'lucide-react';
import { useId,useMemo,useState } from 'react';
import {
decideApprovalRequest,
getApprovalRequest,
reassignApprovalRequest,
type ApprovalRequestDetail,
type CheckReport,
type FrozenCriterion,
type StaleReasonsDetails,
} from '../../api/approvals';
import { isApiError } from '../../api/client';
import type { CriterionResult,NodeTaskOut } from '../../api/collaboration';
import { listMembers } from '../../api/endpoints';
import type { ApprovalDecision,ApprovalStage,Role } from '../../api/types';
import { ACTION_ROLES,STAGE_ACTION } from '../../auth/permissions';
import { Dialog } from '../../components/ui/Dialog';
import { Field } from '../../components/ui/Form';
import { LimitedTextarea } from '../../components/ui/LimitedText';
import { ErrorNotice } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { dynLabel } from '../assets/meta';
import { ActorLabel,CL_ERRORS,asActor } from '../collaboration/common';
import { acceptanceReadiness,criterionDecisionsBody,type CriterionEntry } from '../collaboration/model';
import { PreReviewBlock,VerdictTable } from '../collaboration/NodeDetail';
import { BlockerList } from '../stages/meta';
import { notifyApprovalsChanged } from './meta';
import { FailingChecks } from './TargetSnapshot';

function Reloadable({ tone, text, code, onReload }: { tone: 'warn' | 'err'; text: string; code: string; onReload: () => void }) {
  const { t } = useI18n();
  return (
    <div className={`callout ${tone}`} role="alert">
      <TriangleAlert aria-hidden />
      <div className="grow stack-sm">
        <span>{text}</span>
        <code className="error-code">{t('common.errorCode', { code })}</code>
      </div>
      <button type="button" className="btn sm" onClick={onReload}>
        <RefreshCw /> {t('ap.reload')}
      </button>
    </div>
  );
}

export function DecisionError({ error, onReload }: { error: unknown; onReload: () => void }) {
  const { t, lang } = useI18n();
  if (!error) return null;
  if (isApiError(error)) {
    const d = error.details as Record<string, unknown>;
    switch (error.code) {
      case 'request_closed':
        return <Reloadable tone="warn" code={error.code} onReload={onReload} text={t('ap.err.request_closed', { status: dynLabel(t, `ap.status.${String(d.status ?? '')}`, String(d.status ?? '')) })} />;
      case 'target_changed':
      case 'subject_changed':
        return <Reloadable tone="warn" code={error.code} onReload={onReload} text={t('ap.err.target_changed')} />;
      case 'request_stale':
      case 'not_waiting':
        return <Reloadable tone="warn" code={error.code} onReload={onReload} text={t('ap.err.request_stale')} />;
      case 'submission_closed':
        return <Reloadable tone="warn" code={error.code} onReload={onReload} text={t('ap.err.submission_closed', { status: dynLabel(t, `as.subStatus.${String(d.status ?? '')}`, String(d.status ?? '')) })} />;
      case 'subject_stale': {
        const reasons = (d as StaleReasonsDetails).reasons ?? [];
        return (
          <div className="callout err" role="alert">
            <TriangleAlert aria-hidden />
            <div className="stack-sm">
              <span>{t('ap.err.subject_stale')}</span>
              {reasons.length > 0 && (
                <ul className="ap-reasons">
                  {reasons.map((r, i) => (
                    <li key={i}>{lang === 'zh' ? r.message_zh || r.message_en : r.message_en || r.message_zh}</li>
                  ))}
                </ul>
              )}
              <code className="error-code">{t('common.errorCode', { code: error.code })}</code>
            </div>
          </div>
        );
      }
      case 'submission_no_longer_valid': {
        const checks = d.checks as CheckReport | undefined;
        return (
          <div className="callout err" role="alert">
            <TriangleAlert aria-hidden />
            <div className="stack-sm">
              <span>{t('ap.err.submission_no_longer_valid')}</span>
              {checks && <FailingChecks report={checks} />}
              <code className="error-code">{t('common.errorCode', { code: error.code })}</code>
            </div>
          </div>
        );
      }
      case 'review_no_longer_valid':
        return (
          <div className="callout err" role="alert">
            <TriangleAlert aria-hidden />
            <div className="stack-sm grow">
              <span>{t('ap.err.review_no_longer_valid')}</span>
              <BlockerList blockers={d.blockers} />
              <code className="error-code">{t('common.errorCode', { code: error.code })}</code>
            </div>
          </div>
        );
      case 'review_closed':
        return <Reloadable tone="warn" code={error.code} onReload={onReload} text={t('ap.err.review_closed', { status: dynLabel(t, `ap.status.${String(d.status ?? '')}`, String(d.status ?? '')) })} />;
      case 'project_not_active':
        return (
          <div className="callout err" role="alert">
            <TriangleAlert aria-hidden />
            <div className="stack-sm">
              <span>{t('ap.err.project_not_active', { lifecycle: dynLabel(t, `ap.lifecycle.${String(d.lifecycle ?? '')}`, String(d.lifecycle ?? '')) })}</span>
              <code className="error-code">{t('common.errorCode', { code: error.code })}</code>
            </div>
          </div>
        );
    }
  }
  return (
    <ErrorNotice
      error={error}
      overrides={{
        self_review_forbidden: 'ap.err.self_review_forbidden',
        not_assignee: 'ap.err.not_assignee',
        forbidden: 'ap.err.forbidden',
        project_archived: 'ap.err.project_archived',
        stage_subject_mismatch: 'error.stage_subject_mismatch',
        subject_not_ready: 'error.subject_not_ready',
        ...CL_ERRORS,
      }}
    />
  );
}

/** Why the current user cannot decide a pending request (best explanation from what the client knows). */
function cannotDecideText(t: ReturnType<typeof useI18n>['t'], detail: ApprovalRequestDetail, meId: string, myRole: Role | null | undefined): string {
  if (detail.kind === 'task_acceptance' || detail.kind === 'milestone_review') {
    const stageReview = detail.kind === 'milestone_review';
    if (detail.assignee_id && detail.assignee_id !== meId) return t('ap.cannot.assignedTo', { name: detail.assignee?.name ?? '—' });
    if (detail.created_by === meId) return t(stageReview ? 'ap.cannot.selfStageReview' : 'ap.cannot.selfReview');
    if (myRole === 'viewer') return t(stageReview ? 'ap.cannot.viewerStage' : 'ap.cannot.viewer');
    return t('ap.cannot.projectState');
  }
  const stage = (detail.stage ?? 'design') as ApprovalStage;
  const roles = ACTION_ROLES[STAGE_ACTION[stage]] ?? [];
  if (detail.assignee_id && detail.assignee_id !== meId) return t('ap.cannot.assignedTo', { name: detail.assignee?.name ?? '—' });
  if (!myRole || !roles.includes(myRole)) return t('ap.cannot.role', { roles: roles.map((r) => dynLabel(t, `role.${r}`, r)).join(' / ') });
  return t('ap.cannot.projectState');
}

export function DecisionPanel({
  detail,
  meId,
  myRole,
  onDetail,
  onReload,
}: {
  detail: ApprovalRequestDetail;
  meId: string;
  myRole: Role | null | undefined;
  onDetail: (d: ApprovalRequestDetail) => void;
  onReload: () => void;
}) {
  const { t, fmt } = useI18n();
  const toast = useToast();
  const cid = useId();
  const [comment, setComment] = useState('');
  const [busy, setBusy] = useState<ApprovalDecision | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [repeat, setRepeat] = useState<ApprovalDecision | null>(null);
  const pending = detail.status === 'pending';
  const last = detail.decisions[detail.decisions.length - 1];
  const checksFailNow = detail.kind === 'task_acceptance' && pending && detail.target.current_checks && !detail.target.current_checks.ok;
  const stageReview = detail.kind === 'milestone_review';
  const stageReadiness = detail.target.readiness ?? detail.target.milestone_review?.readiness ?? null;
  const stageFailsNow = stageReview && pending && !!stageReadiness && !stageReadiness.ready;
  const direct = detail.kind === 'engineering' && detail.target.subject && 'type' in detail.target.subject && detail.target.subject.type === 'process_module';
  const changed = detail.kind === 'engineering' && pending && detail.target.subject && 'changed' in detail.target.subject && detail.target.subject.changed;

  const decide = async (decision: ApprovalDecision) => {
    setBusy(decision);
    setError(null);
    setRepeat(null);
    try {
      // The page may be stale (decided elsewhere meanwhile): count the decisions
      // the server has right now, so an idempotent repeat is recognisable.
      const fresh = await getApprovalRequest(detail.id).catch(() => null);
      const before = (fresh ?? detail).decisions.length;
      const res = await decideApprovalRequest(detail.id, { decision, comment: comment.trim(), expected_hash: detail.target_hash });
      onDetail(res);
      notifyApprovalsChanged();
      if (res.decisions.length === before && res.status === decision) {
        // Idempotent repeat: the request had already been closed with this decision; nothing changed.
        setRepeat(decision);
      } else {
        setComment('');
        toast(t(decision === 'approved' ? 'ap.approvedToast' : 'ap.rejectedToast'));
      }
    } catch (e) {
      setError(e);
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="panel ap-decide" aria-labelledby="ap-decide-h">
      <div className="panel-h">
        <Hand aria-hidden className="faint" />
        <h2 id="ap-decide-h">{t('ap.decide.title')}</h2>
      </div>
      <div className="panel-b stack-sm">
        {repeat && (
          <div className="callout info" role="status">
            <Info aria-hidden />
            <span>{t('ap.decide.repeat', { status: t(`ap.status.${repeat}`) })}</span>
          </div>
        )}
        {!pending ? (
          <div className={`callout ${detail.status === 'approved' ? 'ok' : detail.status === 'rejected' ? 'err' : 'pending'}`} role="status">
            <Info aria-hidden />
            <div className="stack-sm">
              <span>
                {detail.status === 'approved' || detail.status === 'rejected'
                  ? t(detail.status === 'approved' ? 'ap.decide.closedApproved' : 'ap.decide.closedRejected', {
                      who: last?.actor_name ?? '—',
                      time: fmt.dateTime(last?.created_at ?? detail.closed_at),
                    })
                  : t(`ap.decide.closed.${detail.status}`)}
              </span>
              {last?.comment && <span className="muted">“{last.comment}”</span>}
            </div>
          </div>
        ) : detail.can_decide ? (
          <>
            <p className="small faint">
              {t(detail.kind === 'task_acceptance' ? 'ap.decide.respAcceptance' : stageReview ? 'ap.decide.respStage' : direct ? 'ap.decide.respModule' : 'ap.decide.respEngineering')}
            </p>
            {stageFailsNow && (
              <div className="callout warn slim" role="note">
                <TriangleAlert aria-hidden />
                <span>{t('ap.decide.stageFailsHint')}</span>
              </div>
            )}
            {checksFailNow && (
              <div className="callout warn slim" role="note">
                <TriangleAlert aria-hidden />
                <span>{t('ap.decide.checksFailHint')}</span>
              </div>
            )}
            {changed && (
              <div className="callout warn slim" role="note">
                <TriangleAlert aria-hidden />
                <span>{t('ap.decide.changedHint')}</span>
              </div>
            )}
            <div className="field">
              <label htmlFor={cid}>
                {t('ap.decide.comment')} <span className="opt">· {t('common.optional')}</span>
              </label>
              <LimitedTextarea id={cid} className="input" rows={3} limit="description" value={comment} placeholder={t('ap.decide.commentPlaceholder')} onChange={(e) => setComment(e.target.value)} />
            </div>
            <DecisionError error={error} onReload={onReload} />
            <div className="row">
              <button type="button" className="btn primary" disabled={!!busy} onClick={() => void decide('approved')}>
                <Check /> {busy === 'approved' ? t('common.working') : t(detail.kind === 'task_acceptance' ? 'ap.decide.accept' : stageReview ? 'ap.decide.acceptStage' : 'ap.decide.approve')}
              </button>
              <button type="button" className="btn danger" disabled={!!busy} onClick={() => void decide('rejected')}>
                <X /> {busy === 'rejected' ? t('common.working') : t('ap.decide.reject')}
              </button>
            </div>
            <p className="small faint">{t('ap.decide.hashNote')}</p>
          </>
        ) : (
          <div className="callout pending" role="note">
            <Hand aria-hidden />
            <span>{cannotDecideText(t, detail, meId, myRole)}</span>
          </div>
        )}
      </div>
    </section>
  );
}

// ─── per-criterion human final acceptance (UI03, C05) ─────────────────────

const RESULTS: CriterionResult[] = ['pass', 'fail', 'unknown'];

function ResultIcon({ r }: { r: CriterionResult }) {
  return r === 'pass' ? <CircleCheck aria-hidden /> : r === 'fail' ? <CircleX aria-hidden /> : <CircleHelp aria-hidden />;
}

/**
 * Collaboration task acceptance: a pass / fail / unknown verdict, reason and
 * evidence (only the submission's items) per criterion, the agent pre-review
 * (collapsed, never final) and an overall comment. Accept is possible only
 * when every required criterion passes and the server says this person may
 * decide; reject needs a comment. A refused decision keeps everything typed.
 */
export function CriterionDecisionPanel({
  detail,
  meId,
  myRole,
  nodeTask,
  onDetail,
  onReload,
}: {
  detail: ApprovalRequestDetail;
  meId: string;
  myRole: Role | null | undefined;
  /** The node (pre-review, actors); undefined while loading or unavailable. */
  nodeTask: NodeTaskOut | undefined;
  onDetail: (d: ApprovalRequestDetail) => void;
  onReload: () => void;
}) {
  const { t, fmt } = useI18n();
  const toast = useToast();
  const cid = useId();
  const snap = detail.target.submission?.snapshot;
  const criteria: FrozenCriterion[] = useMemo(() => [...(snap?.criteria ?? [])].sort((a, b) => a.seq - b.seq), [snap]);
  const items = snap?.items ?? [];
  const [entries, setEntries] = useState<Record<string, CriterionEntry>>({});
  const [comment, setComment] = useState('');
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState<ApprovalDecision | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [repeat, setRepeat] = useState<ApprovalDecision | null>(null);
  const pending = detail.status === 'pending';
  const agentDecides = detail.decision_mode === 'agent' || detail.node_task?.execution_mode === 'full_auto';
  const ready = acceptanceReadiness(criteria, entries);
  const critMap = new Map(criteria.map((c) => [c.id, c]));
  const last = detail.decisions[detail.decisions.length - 1];

  const entry = (id: string): CriterionEntry => entries[id] ?? { result: null, reason: '', evidence: [] };
  const setEntry = (id: string, patch: Partial<CriterionEntry>) => setEntries((xs) => ({ ...xs, [id]: { ...entry(id), ...patch } }));

  const acceptBlock = !detail.can_decide ? t('ap.cl.cannotDecide') : ready.undecided ? t('ap.cl.undecided', { n: ready.undecided }) : ready.missingReason ? t('ap.cl.missingReason', { n: ready.missingReason }) : ready.notPassed ? t('ap.cl.notPassed', { n: ready.notPassed }) : null;
  const rejectBlock = !detail.can_decide ? t('ap.cl.cannotDecide') : ready.undecided ? t('ap.cl.undecided', { n: ready.undecided }) : ready.missingReason ? t('ap.cl.missingReason', { n: ready.missingReason }) : !comment.trim() ? t('ap.cl.rejectNeedsComment') : null;

  const decide = async (decision: ApprovalDecision) => {
    setTouched(true);
    if (decision === 'approved' ? acceptBlock : rejectBlock) return;
    setBusy(decision);
    setError(null);
    setRepeat(null);
    try {
      const fresh = await getApprovalRequest(detail.id).catch(() => null);
      const before = (fresh ?? detail).decisions.length;
      const res = await decideApprovalRequest(detail.id, { decision, comment: comment.trim(), expected_hash: detail.target_hash, criterion_decisions: criterionDecisionsBody(entries) });
      onDetail(res);
      notifyApprovalsChanged();
      if (res.decisions.length === before && res.status === decision) setRepeat(decision);
      else toast(t(decision === 'approved' ? 'ap.cl.acceptedToast' : 'ap.cl.rejectedToast'));
    } catch (e) {
      // Keep every verdict, reason and comment: the person re-checks and tries again.
      setError(e);
    } finally {
      setBusy(null);
    }
  };

  const pre = nodeTask?.pre_review ?? null;

  return (
    <section className="panel ap-decide" aria-labelledby="ap-cl-h">
      <div className="panel-h">
        <Hand aria-hidden className="faint" />
        <h2 id="ap-cl-h">{t('ap.cl.title')}</h2>
        <span className="grow" />
        <span className="faint small">{t('ap.cl.requiredAllPass')}</span>
      </div>
      <div className="panel-b stack">
        {repeat && (
          <div className="callout info" role="status">
            <Info aria-hidden />
            <span>{t('ap.decide.repeat', { status: t(`ap.status.${repeat}`) })}</span>
          </div>
        )}
        {agentDecides && (
          <div className="callout info" role="note">
            <Bot aria-hidden />
            <span>{t('ap.cl.agentDecides')}</span>
          </div>
        )}

        {pending && !agentDecides ? (
          <>
            {!detail.can_decide && (
              <div className="callout pending" role="note">
                <Hand aria-hidden />
                <span>{detail.assignee_id && detail.assignee_id !== meId ? t('ap.cannot.assignedTo', { name: detail.assignee?.name ?? '—' }) : myRole === 'viewer' ? t('ap.cannot.viewer') : t('ap.cl.cannotDecide')}</span>
              </div>
            )}
            <ol className="cl-decisions">
              {criteria.map((c) => {
                const e = entry(c.id);
                const missing = touched && c.required && !e.result;
                const reasonMissing = touched && !!e.result && !e.reason.trim();
                const linked = items.filter((i) => i.criterion_ids.includes(c.id));
                return (
                  <li key={c.id} className={`cl-decision ${missing ? 'is-missing' : ''}`}>
                    <div className="cl-crit-head">
                      <span className="mono faint">#{c.seq}</span>
                      <span className="strong grow">{c.description || t('ap.crit.noDescription')}</span>
                      <span className={`chip ${c.required ? 'measure' : 'pending'} cl-chip-sm`}>{c.required ? t('ap.crit.required') : t('ap.crit.optional')}</span>
                      {c.manual_required && <span className="chip measure cl-chip-sm">{t('ap.crit.manual')}</span>}
                    </div>
                    <div className="cl-results" role="radiogroup" aria-label={t('ap.cl.resultFor', { n: c.seq })} aria-required={c.required}>
                      {RESULTS.map((r) => (
                        <label key={r} className={`cl-result ${r} ${e.result === r ? 'on' : ''}`}>
                          <input type="radio" name={`${cid}-r-${c.id}`} checked={e.result === r} disabled={!detail.can_decide || !!busy} onChange={() => setEntry(c.id, { result: r })} />
                          <ResultIcon r={r} />
                          {t(`collab.verdict.${r}`)}
                        </label>
                      ))}
                    </div>
                    {missing && <span className="field-error">{t('ap.cl.chooseResult')}</span>}
                    <div className="field">
                      <label htmlFor={`${cid}-reason-${c.id}`}>{t('ap.cl.reason')}</label>
                      <LimitedTextarea
                        id={`${cid}-reason-${c.id}`}
                        className="input"
                        rows={2}
                        limit="description"
                        value={e.reason}
                        disabled={!detail.can_decide || !!busy}
                        aria-invalid={reasonMissing || undefined}
                        onChange={(ev) => setEntry(c.id, { reason: ev.target.value })}
                      />
                      {reasonMissing && <span className="field-error">{t('ap.cl.reasonRequired')}</span>}
                    </div>
                    <fieldset className="cl-evidence tk-fieldset" disabled={!detail.can_decide || !!busy}>
                      <legend className="tk-label">{t('ap.cl.evidence')}</legend>
                      {items.length ? (
                        items.map((i) => (
                          <label key={i.asset_version_id} className="check">
                            <input
                              type="checkbox"
                              checked={e.evidence.includes(i.asset_version_id)}
                              onChange={(ev) =>
                                setEntry(c.id, { evidence: ev.target.checked ? [...e.evidence, i.asset_version_id].slice(0, 100) : e.evidence.filter((x) => x !== i.asset_version_id) })
                              }
                            />
                            <span className="mono">{i.label}</span> {dynLabel(t, `as.type.${i.asset_type}`, i.asset_type)}
                            {linked.includes(i) && <span className="faint small">· {t('ap.cl.linked')}</span>}
                          </label>
                        ))
                      ) : (
                        <span className="faint small">{t('ap.cl.noItems')}</span>
                      )}
                    </fieldset>
                  </li>
                );
              })}
            </ol>

            {pre ? (
              <PreReviewBlock result={pre.result} agentName={<ActorLabel actor={asActor(pre.agent)} />} at={pre.at} criteria={critMap} />
            ) : nodeTask?.pre_reviewer ? (
              <p className="faint small">{t('ap.cl.noPreReview')}</p>
            ) : null}

            <div className="field">
              <label htmlFor={`${cid}-comment`}>{t('ap.cl.comment')}</label>
              <LimitedTextarea
                id={`${cid}-comment`}
                className="input"
                rows={3}
                limit="description"
                value={comment}
                disabled={!detail.can_decide || !!busy}
                placeholder={t('ap.cl.commentPlaceholder')}
                onChange={(e) => setComment(e.target.value)}
              />
              <span className="field-hint">{t('ap.cl.commentHint')}</span>
            </div>
            <DecisionError error={error} onReload={onReload} />
            {error ? <p className="small faint">{t('ap.cl.keptInput')}</p> : null}
            <div className="cl-acc-foot">
              <button type="button" className="btn primary" disabled={!!busy || !!acceptBlock} onClick={() => void decide('approved')} aria-describedby={`${cid}-why`}>
                <Check /> {busy === 'approved' ? t('common.working') : t('ap.cl.accept')}
              </button>
              <button type="button" className="btn danger" disabled={!!busy || !detail.can_decide} onClick={() => void decide('rejected')}>
                <X /> {busy === 'rejected' ? t('common.working') : t('ap.cl.reject')}
              </button>
            </div>
            <p id={`${cid}-why`} className={`small ${acceptBlock ? 'warn-text' : 'faint'}`}>
              {acceptBlock ?? t('ap.cl.acceptReady')}
            </p>
            {touched && rejectBlock && <p className="small warn-text">{t('ap.cl.rejectBlocked', { reason: rejectBlock })}</p>}
            <p className="small faint">{t('ap.decide.hashNote')}</p>
          </>
        ) : !pending ? (
          <div className="stack-sm">
            <div className={`callout ${detail.status === 'approved' ? 'ok' : detail.status === 'rejected' ? 'err' : 'pending'}`} role="status">
              <Info aria-hidden />
              <div className="stack-sm">
                <span>{detail.status === 'approved' || detail.status === 'rejected' ? t(detail.status === 'approved' ? 'ap.cl.closedApproved' : 'ap.cl.closedRejected') : t(`ap.decide.closed.${detail.status}`)}</span>
                {last && (
                  <span className="row small">
                    {last.actor ? <ActorLabel actor={last.actor} /> : <span>{last.actor_name ?? '—'}</span>}
                    <span className="faint">{fmt.dateTime(last.created_at)}</span>
                  </span>
                )}
                {last?.comment && <span className="muted">“{last.comment}”</span>}
              </div>
            </div>
            {last?.criterion_decisions && last.criterion_decisions.length > 0 && <VerdictTable decisions={last.criterion_decisions} criteria={critMap} />}
            {pre && <PreReviewBlock result={pre.result} agentName={<ActorLabel actor={asActor(pre.agent)} />} at={pre.at} criteria={critMap} />}
          </div>
        ) : (
          pre && <PreReviewBlock result={pre.result} agentName={<ActorLabel actor={asActor(pre.agent)} />} at={pre.at} criteria={critMap} />
        )}
      </div>
    </section>
  );
}

// ─── reassign ──────────────────────────────────────────────────────────────

export function ReassignDialog({ detail, onClose, onDone }: { detail: ApprovalRequestDetail; onClose: () => void; onDone: (d: ApprovalRequestDetail) => void }) {
  const { t } = useI18n();
  const toast = useToast();
  const members = useResource((signal) => listMembers(detail.project_id, { signal }), [detail.project_id]);
  const [assignee, setAssignee] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [touched, setTouched] = useState(false);
  const eligible = useMemo(() => {
    const list = members.data ?? [];
    if (detail.kind === 'task_acceptance' || detail.kind === 'milestone_review') return list.filter((m) => m.role !== 'viewer');
    const roles = ACTION_ROLES[STAGE_ACTION[(detail.stage ?? 'design') as ApprovalStage]] ?? [];
    return list.filter((m) => roles.includes(m.role));
  }, [members.data, detail.kind, detail.stage]);

  const submit = async () => {
    setTouched(true);
    if (!assignee) return;
    setBusy(true);
    setError(null);
    try {
      const res = await reassignApprovalRequest(detail.id, assignee);
      onDone(res);
      notifyApprovalsChanged();
      toast(t('ap.reassign.toast', { name: res.assignee?.name ?? '—' }));
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      title={t('ap.reassign.title')}
      subtitle={detail.title}
      onClose={onClose}
      busy={busy}
      size="sm"
      onSubmit={() => void submit()}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={busy || !members.data}>
            <UserCog /> {busy ? t('common.working') : t('ap.reassign.submit')}
          </button>
        </>
      }
    >
      <div className="stack-sm">
        <p className="small muted">
          {t(detail.kind === 'task_acceptance' ? 'ap.reassign.hintAcceptance' : detail.kind === 'milestone_review' ? 'ap.reassign.hintStage' : 'ap.reassign.hintEngineering')}
        </p>
        {members.error && !members.data ? (
          <ErrorNotice error={members.error} onRetry={members.reload} />
        ) : (
          <Field label={t('ap.reassign.assignee')} error={touched && !assignee ? t('ap.reassign.required') : null}>
            <select className="select" value={assignee} onChange={(e) => setAssignee(e.target.value)} disabled={!members.data}>
              <option value="">{members.data ? t('ap.reassign.choose') : t('common.loading')}</option>
              {eligible.map((m) => (
                <option key={m.user.id} value={m.user.id}>
                  {m.user.name} · {dynLabel(t, `role.${m.role}`, m.role)}
                  {m.user.id === detail.assignee_id ? ` · ${t('ap.reassign.current')}` : ''}
                </option>
              ))}
            </select>
          </Field>
        )}
        <ErrorNotice
          error={error}
          overrides={{
            not_a_member: 'ap.err.not_a_member',
            read_only_member: 'ap.err.read_only_member',
            self_review_forbidden: 'ap.err.reassign_self_review',
            assignee_cannot_decide: 'ap.err.assignee_cannot_decide',
            request_closed: 'ap.err.request_closed_plain',
            forbidden: 'ap.err.forbidden',
          }}
        />
      </div>
    </Dialog>
  );
}

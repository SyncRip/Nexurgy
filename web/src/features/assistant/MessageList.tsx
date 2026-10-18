// The conversation (design §7.3): user requests with their immutable snapshot
// (target + references as sent), operation cards, answers with the
// references they cite, read-only historical operation choices, notices and flow cards (a run / workflow the
// conversation follows, AI work panel). AI-native: a main-agent turn shows as its
// turn card (step timeline), a sub-agent's card carries a badge, and the notice of
// a finished sub-agent reads as one line.

import { Info, MessageSquareText, Pin, User, Workflow } from 'lucide-react';
import { useLayoutEffect, useMemo, useRef, useState } from 'react';
import { cardPayload, isOrchestratorTurn, type AiConversationDetail, type AiMessage, type AiOperation, type AiRef } from '../../api/assistant';
import { EmptyState } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { useI18n } from '../../i18n/I18nProvider';
import { AgentRoleBadge, AgentRoleIcon } from '../agent-team/AgentRole';
import { isCoordinatedOperation, roleForOperation } from '../agent-team/roles';
import { useAssistantSession } from './AssistantProvider';
import { AssistantMarkdown } from './AssistantMarkdown';
import { AsError, MaterialSaved, opKindLabel, RefChip, SimChip, tk } from './common';
import type { ComposerPrefill } from './Composer';
import { FlowCard } from './FlowCards';
import { FollowUpActions } from './FollowUpActions';
import { answerFollowUps } from './followUps';
import { conversationEntries, currentJourney } from './journey';
import { JourneyCard } from './JourneyCard';
import { focusJourney, JourneyNavigator } from './JourneyNavigator';
import { clipConstraint, hasActiveOperations, MAX_CONSTRAINT_TEXT, previousUserMessage, retriedByMap } from './model';
import { OperationCard } from './OperationCard';
import { subAgentNotice } from './orchestrator';
import { toolLabel, TurnCard } from './TurnCard';
import './journey.css';

const MAX_CONSTRAINTS = 20;

/** The scrolling container of an embedded panel (null for the dock / full page, whose list scrolls itself). */
function embeddedHost(el: HTMLElement): HTMLElement | null {
  if (!el.closest('.as-embedded')) return null;
  for (let p = el.parentElement; p; p = p.parentElement) {
    const oy = getComputedStyle(p).overflowY;
    if (oy === 'auto' || oy === 'scroll') return p;
  }
  return null;
}

/** Only the latest exchange may choose the initial saved result; later questions/failures and live work win. */
export function initialAssemblyOperation(detail: Pick<AiConversationDetail, 'messages' | 'operations'>): string | null {
  if (hasActiveOperations(detail)) return null;
  const operations = new Map(detail.operations.map((op) => [op.id, op]));
  for (let i = detail.messages.length - 1; i >= 0; i -= 1) {
    const message = detail.messages[i];
    if (message.role === 'user') return null;
    const op = message.operation_id ? operations.get(message.operation_id) : undefined;
    if (op && op.status !== 'succeeded') return null;
    if (message.kind !== 'operation') continue;
    const saved = op?.result?.cad_result;
    return saved?.object_type === 'assembly' && saved.object_id && saved.version_id ? op!.id : null;
  }
  return null;
}

export type MessageScrollPosition = { conversationId: string | null; follow: boolean };
type ScrollHost = Pick<HTMLElement, 'scrollTop' | 'scrollHeight' | 'clientHeight' | 'getBoundingClientRect'>;
type SavedResultPosition = { top: number; previewTop: number; previewBottom: number };

/** Embedded panels scroll an ancestor; listen on that same host so reaching its bottom resumes following. */
export function trackMessageScroll(host: ScrollHost & Pick<HTMLElement, 'addEventListener' | 'removeEventListener'>,
  position: MessageScrollPosition): () => void {
  const onScroll = () => { position.follow = host.scrollHeight - host.scrollTop - host.clientHeight < 48; };
  host.addEventListener('scroll', onScroll, { passive: true });
  return () => host.removeEventListener('scroll', onScroll);
}

/** Entry can reveal a clipped saved model once. Polls and new messages keep the reader's existing follow choice. */
export function positionMessages(host: ScrollHost, position: MessageScrollPosition, conversationId: string,
  savedResult?: () => SavedResultPosition): void {
  const entering = position.conversationId !== conversationId;
  if (!entering && !position.follow) return;
  position.conversationId = conversationId;
  host.scrollTop = host.scrollHeight;
  if (entering && savedResult) {
    const top = host.getBoundingClientRect().top;
    const saved = savedResult();
    if (saved.previewTop < top || saved.previewBottom > top + host.clientHeight) {
      host.scrollTop += saved.top - top - 12;
    }
  }
  position.follow = host.scrollHeight - host.scrollTop - host.clientHeight < 48;
}

export function MessageList({ detail, onPrefill }: { detail: AiConversationDetail; onPrefill: (p: Omit<ComposerPrefill, 'nonce'>) => void }) {
  const { t } = useI18n();
  const scroller = useRef<HTMLDivElement>(null);
  const scrollPosition = useRef<MessageScrollPosition>({ conversationId: null, follow: true });
  const archived = detail.conversation.archived;
  const ops = useMemo(() => new Map(detail.operations.map((o) => [o.id, o])), [detail.operations]);
  const retried = useMemo(() => retriedByMap(detail.operations), [detail.operations]);
  const entries = useMemo(() => conversationEntries(detail), [detail.messages, detail.operations]);
  const current = useMemo(() => currentJourney(entries), [entries]);
  const last = detail.messages[detail.messages.length - 1];

  // Keep a saved assembly visible on entry even when the mobile viewport cannot fit its trailing details.
  // Subsequent updates follow only while the reader remains at the bottom, as for ordinary conversations.
  useLayoutEffect(() => {
    const el = scroller.current;
    if (!el) return;
    const entering = scrollPosition.current.conversationId !== detail.conversation.id;
    const entryOp = entering ? initialAssemblyOperation(detail) : null;
    const card = entryOp ? [...el.querySelectorAll<HTMLElement>('[data-op-id]')].find((node) => node.dataset.opId === entryOp)
      ?.querySelector<HTMLElement>('.as-cad-result') : null;
    const preview = card?.querySelector<HTMLElement>('.as-cad-preview');
    const host = embeddedHost(el) ?? el;
    positionMessages(host, scrollPosition.current, detail.conversation.id, card && preview ? () => ({
      top: card.getBoundingClientRect().top,
      previewTop: preview.getBoundingClientRect().top,
      previewBottom: preview.getBoundingClientRect().bottom,
    }) : undefined);
    // A grouped task has its history after its result. Start at the useful task area once;
    // polling and image loads must not keep moving a person who scrolled away.
    if (entering && current) {
      const target = [...el.querySelectorAll<HTMLElement>('[data-journey-id]')].find((node) => node.dataset.journeyId === current.id);
      if (target) {
        host.scrollTop += target.getBoundingClientRect().top - host.getBoundingClientRect().top - 12;
        scrollPosition.current.follow = host.scrollHeight - host.scrollTop - host.clientHeight < 48;
      }
    }
    return trackMessageScroll(host, scrollPosition.current);
  }, [detail.conversation.id, last?.id, detail.operations, current]);

  const renderMessage = (m: AiMessage, historical = false) => <Message m={m} detail={detail} op={m.operation_id ? ops.get(m.operation_id) : undefined}
    retriedBy={m.operation_id ? (retried.get(m.operation_id) ?? null) : null} archived={archived} historical={historical} onPrefill={onPrefill} />;

  return (
    <>
    {current && <JourneyNavigator journey={current} onNavigate={() => { if (scroller.current) focusJourney(scroller.current, current); }} />}
    <div
      className="as-messages"
      ref={scroller}
      role="log"
      aria-label={t('as.messages')}
      aria-live="polite"
      tabIndex={0}
    >
      {detail.conversation.forked_from && <p className="as-notice"><Info aria-hidden /> {t('as.msg.forked')}</p>}
      {detail.messages.length === 0 ? (
        <EmptyState compact icon={<MessageSquareText className="as-empty-ico" aria-hidden />} title={t('as.msg.emptyTitle')} body={t('as.msg.emptyBody')} />
      ) : (
        entries.map((entry) => entry.kind === 'journey'
          ? <JourneyCard key={`journey:${entry.journey.id}`} journey={entry.journey} detail={detail} retried={retried} renderMessage={renderMessage} />
          : <div key={entry.message.id} data-message-id={entry.message.id}>{renderMessage(entry.message)}</div>)
      )}
    </div>
    </>
  );
}

function Message({
  m,
  detail,
  op,
  retriedBy,
  archived,
  historical,
  onPrefill,
}: {
  m: AiMessage;
  detail: AiConversationDetail;
  op: AiOperation | undefined;
  retriedBy: string | null;
  archived: boolean;
  historical: boolean;
  onPrefill: (p: Omit<ComposerPrefill, 'nonce'>) => void;
}) {
  if (m.role === 'user') return <UserMessage m={m} detail={detail} archived={archived} />;
  if (m.kind === 'notice') return <NoticeMessage m={m} operations={detail.operations} />;
  if (m.kind === 'card') {
    // A client without the card's object kind still reads the server's one-line text.
    const payload = cardPayload(m);
    return <div className="as-msg assistant">{payload ? <FlowCard m={m} payload={payload} detail={detail} archived={archived} onPrefill={onPrefill} /> : <p className="as-msg-text">{m.text}</p>}</div>;
  }
  if (m.kind === 'operation') {
    return (
      <div className="as-msg assistant">
        {op && isOrchestratorTurn(op) ? (
          <TurnCard op={op} detail={detail} retriedBy={retriedBy} archived={archived} />
        ) : op ? (
          <OperationCard op={op} retriedBy={retriedBy} archived={archived} />
        ) : (
          <p className="as-msg-text">{m.text}</p>
        )}
      </div>
    );
  }
  if (m.kind === 'choice') return <ChoiceMessage m={m} detail={detail} />;
  return <AnswerMessage m={m} detail={detail} op={op} archived={archived} historical={historical} />;
}

function Snapshot({ m }: { m: AiMessage }) {
  const { t } = useI18n();
  if (!m.target && !m.references.length) return null;
  return (
    <div className="as-snap" role="group" aria-label={t('as.msg.snapshot')}>
      <span className="as-snap-label">{t('as.msg.snapshot')}</span>
      {m.target && <RefChip refItem={m.target} status={m.ref_status?.[m.target.key]} target compact />}
      {m.references.map((r) => (
        <RefChip key={r.key} refItem={r} status={m.ref_status?.[r.key]} compact />
      ))}
    </div>
  );
}

function UserMessage({ m, detail, archived }: { m: AiMessage; detail: AiConversationDetail; archived: boolean }) {
  const { t, fmt } = useI18n();
  const toast = useToast();
  const { store } = useAssistantSession();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const constraints = detail.conversation.constraints;
  const pinned = constraints.some((c) => c.source_seq === m.seq);
  const full = constraints.length >= MAX_CONSTRAINTS;
  const retryOf = typeof m.payload?.retry_of === 'string';
  const long = m.text.trim().length > MAX_CONSTRAINT_TEXT;

  const pin = async () => {
    setBusy(true);
    setError(null);
    // The API keeps at most 500 characters: shorten visibly instead of letting the server cut silently.
    const clip = clipConstraint(m.text);
    try {
      await store.patch({
        constraints: [...constraints.map((c) => ({ id: c.id, text: c.text, source_seq: c.source_seq })), { text: clip.text, source_seq: m.seq }],
        expected_version: detail.conversation.version,
      });
      if (clip.clipped) toast(t('as.msg.pinClipped', { n: MAX_CONSTRAINT_TEXT }), { tone: 'info' });
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="as-msg user">
      <div className="as-msg-meta">
        <User aria-hidden /> <span>{t('as.msg.you')}</span>
        <span className="faint" title={fmt.dateTime(m.created_at)}>
          {fmt.time(m.created_at)}
        </span>
        {retryOf && <span className="faint">· {t('as.msg.retry')}</span>}
        <span className="grow" />
        {!archived && (
          <button
            type="button"
            className="as-icon-btn"
            onClick={() => void pin()}
            disabled={busy || pinned || full}
            aria-label={pinned ? t('as.msg.pinned') : t('as.msg.pin')}
            title={pinned ? t('as.msg.pinned') : full ? t('as.ctx.constraintsFull') : long ? t('as.msg.pinLong', { n: MAX_CONSTRAINT_TEXT }) : t('as.msg.pin')}
          >
            <Pin />
          </button>
        )}
      </div>
      <p className="as-msg-text">{m.text}</p>
      <MaterialSaved payload={m.payload} />
      <Snapshot m={m} />
      <AsError error={error} />
    </div>
  );
}

function NoticeMessage({ m, operations }: { m: AiMessage; operations: readonly AiOperation[] }) {
  const { t, fmt } = useI18n();
  // The server writes the notice text (e.g. the new task binding); the code is a fallback label only.
  const code = typeof m.payload?.code === 'string' ? m.payload.code : '';
  // AI-native: a sub-agent finished and woke the main agent — which one, and how it ended.
  const sub = subAgentNotice(m.payload, operations);
  const coordinated = isCoordinatedOperation(sub?.kind);
  const agent = sub ? (coordinated && sub.kind ? opKindLabel(t, sub.kind)
    : sub.agent ? `${t(`team.role.${sub.agent}`)} · AI` : sub.kind ? opKindLabel(t, sub.kind) : t('as.orch.agent.generic')) : '';
  const text = sub
    ? sub.outcome
      ? t(`as.orch.notice.${sub.outcome}`, { agent })
      : coordinated
      ? t(`as.orch.workflowNotice.${sub.status === 'succeeded' || sub.status === 'failed' ? sub.status : 'other'}`,
        { workflow: agent, status: tk(t, `as.status.${sub.status ?? ''}`, sub.status ?? '?') })
      : sub.status === 'succeeded' || sub.status === 'failed'
      ? t(`as.orch.notice.${sub.status}`, { agent })
      : t('as.orch.notice.other', { agent, status: tk(t, `as.status.${sub.status ?? ''}`, sub.status ?? '?') })
    : m.payload?.event === 'confirmation_declined'
      ? t('as.orch.notice.declined', { tool: toolLabel(t, String(m.payload.tool ?? '')) })
      : m.text || tk(t, `as.snotice.${code}`, code);
  return (
    <p className={`as-notice ${sub ? `as-notice-sub st-${sub.status ?? 'other'}` : ''}`}>
      {coordinated ? <Workflow aria-hidden /> : sub?.agent ? <AgentRoleIcon role={sub.agent} /> : <Info aria-hidden />} <span>{text}</span>
      <span className="faint"> · {fmt.time(m.created_at)}</span>
    </p>
  );
}

/** Find the references an answer cites, among its own and the request it answers. */
function citedRefs(m: AiMessage, request: AiMessage | null): AiRef[] {
  const keys = Array.isArray(m.payload?.cited_keys) ? (m.payload.cited_keys as unknown[]).filter((k): k is string => typeof k === 'string') : [];
  if (!keys.length) return [];
  const pool = [...(m.target ? [m.target] : []), ...m.references, ...(request?.target ? [request.target] : []), ...(request?.references ?? [])];
  const byKey = new Map(pool.map((r) => [r.key, r]));
  return keys.map((k) => byKey.get(k)).filter((r): r is AiRef => !!r);
}

/** One read-only look-up behind an answer (P4: payload.consulted of a tool-agent answer). */
interface Consulted {
  call_index: number;
  tool: string;
  ok: boolean;
  kind?: string;
  id?: string | null;
  title?: string;
  page?: number;
  pages?: number;
  count?: number;
  error_code?: string | null;
}

function consultedOf(m: AiMessage): Consulted[] {
  const raw = m.payload?.consulted;
  return Array.isArray(raw) ? raw.filter((x): x is Consulted => !!x && typeof x === 'object' && typeof (x as Consulted).tool === 'string') : [];
}

/** "What was consulted": what the assistant looked up (read only; it never changes data). */
function ConsultedList({ items }: { items: Consulted[] }) {
  const { t } = useI18n();
  return (
    <details className="as-consulted">
      <summary>{t('as.msg.consulted', { n: items.length })}</summary>
      <ul>
        {items.map((c) => (
          <li key={c.call_index}>
            {tk(t, `as.consulted.tool.${c.tool}`, c.tool)}
            {c.title && <b> {c.title}</b>}
            {c.page != null && c.pages != null && <span className="faint"> · {t('as.consulted.page', { page: c.page, pages: c.pages })}</span>}
            {c.count != null && <span className="faint"> · {t('as.consulted.count', { n: c.count })}</span>}
            {!c.ok && <span className="warn-text"> · {t('as.consulted.failed', { code: c.error_code ?? '?' })}</span>}
          </li>
        ))}
      </ul>
      <p className="faint small">{t('as.msg.consultedNote')}</p>
    </details>
  );
}

function AnswerMessage({ m, detail, op, archived, historical }: {
  m: AiMessage; detail: AiConversationDetail; op: AiOperation | undefined; archived: boolean; historical: boolean;
}) {
  const { t, fmt } = useI18n();
  const request = previousUserMessage(detail.messages, m.seq);
  const cited = citedRefs(m, request);
  const simulated = op?.simulated || m.payload?.simulated === true;
  const consulted = consultedOf(m);
  // A reply without an operation is historical main-assistant text. Unknown typed operations keep their label.
  const role = m.payload?.runtime === 'orchestrator' || !op ? 'manager' : roleForOperation(op.kind);
  return (
    <div className="as-msg assistant">
      <div className="as-msg-meta">
        {role ? <AgentRoleBadge role={role} compact /> : <span>{opKindLabel(t, op!.kind)}</span>}
        {historical && <span className="as-answer-history-label">{t('as.journey.answer.earlier')}</span>}
        {simulated && <SimChip />}
        <span className="faint" title={fmt.dateTime(m.created_at)}>
          {fmt.time(m.created_at)}
        </span>
      </div>
      <AssistantMarkdown text={m.text} />
      {cited.length > 0 && (
        <div className="as-snap" role="group" aria-label={t('as.msg.cited')}>
          <span className="as-snap-label">{t('as.msg.cited')}</span>
          {cited.map((r) => (
            <RefChip key={r.key} refItem={r} status={m.ref_status?.[r.key] ?? request?.ref_status?.[r.key]} compact />
          ))}
        </div>
      )}
      {consulted.length > 0 && <ConsultedList items={consulted} />}
      {!archived && answerFollowUps(m).length > 0 && <FollowUpActions key={`${detail.conversation.id}:${m.id}`} message={m} detail={detail} historical={historical} />}
    </div>
  );
}

function ChoiceMessage({ m, detail }: { m: AiMessage; detail: AiConversationDetail }) {
  const { t } = useI18n();
  const reqSeq = typeof m.payload?.request_seq === 'number' ? m.payload.request_seq : null;
  const request = (reqSeq !== null ? detail.messages.find((x) => x.seq === reqSeq && x.role === 'user') : undefined) ?? previousUserMessage(detail.messages, m.seq);
  const choices = Array.isArray(m.payload?.choices) ? [...new Set((m.payload.choices as unknown[]).filter((c): c is string => typeof c === 'string'))] : [];
  const reason = typeof m.payload?.reason === 'string' ? m.payload.reason : null;

  return (
    <div className="as-msg assistant as-historical-choice">
      <div className="as-msg-meta">
        <AgentRoleBadge role="manager" compact />
      </div>
      <p className="faint small">{t('as.msg.historicalChoice')}</p>
      {m.text && <p className="as-msg-text">{m.text}</p>}
      {reason && <p className="faint small">{tk(t, `as.choice.reason.${reason}`, reason)}</p>}
      {choices.length > 0 && <ul>{choices.map((kind) => <li key={kind}>{opKindLabel(t, kind)}</li>)}</ul>}
      {request && <Snapshot m={request} />}
    </div>
  );
}

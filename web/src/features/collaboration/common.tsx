// Shared vocabulary of the collaboration UI: mode / phase / execution chips,
// actor labels (person or agent), the four-stage progress dots and the error
// code overrides. Status is always icon + text; colour only reinforces it.
// Cobalt blue is the action accent and never means "accepted".

import './collaboration.css';
import {
  Ban,
  Bot,
  CheckCircle2,
  CircleDashed,
  CircleDot,
  CircleX,
  Clock,
  Eye,
  Hand,
  LoaderCircle,
  PauseCircle,
  RotateCcw,
  Send,
  ShieldCheck,
  ShieldQuestion,
  Sparkles,
  UserRound,
} from 'lucide-react';
import { useEffect, useRef, type ReactNode } from 'react';
import type { ActorOut, ActorRef, AgentBrief, AgentProfile, ExecutionMode } from '../../api/agents';
import type { AgentRef, ExecutionRole, ExecutionStatus, NodeTaskPhase } from '../../api/collaboration';
import { Chip, type Tone } from '../../components/ui/Badges';
import { hasKey } from '../../i18n/core';
import type { MessageKey } from '../../i18n/en';
import type { ErrorOverrides } from '../../i18n/errors';
import { useI18n, type TFunction } from '../../i18n/I18nProvider';
import type { Members } from '../tasks/common';
import { STAGES, stageStates, type StageState } from './model';

/** Translate a dynamic key when it exists, else return the fallback. */
export function dynT(t: TFunction, key: string, fallback: string, vars?: Record<string, string | number>): string {
  return hasKey(key) ? t(key as MessageKey, vars) : fallback;
}

// ─── execution mode ────────────────────────────────────────────────────────

export function ModeChip({ mode, large }: { mode: ExecutionMode | null | undefined; large?: boolean }) {
  const { t } = useI18n();
  if (!mode) return <span className="faint">—</span>;
  return (
    <Chip tone={mode === 'full_auto' ? 'measure' : 'warn'} icon={mode === 'full_auto' ? <Bot aria-hidden /> : <UserRound aria-hidden />} className={large ? 'cl-chip-lg' : ''}>
      {t(`collab.mode.${mode}`)}
    </Chip>
  );
}

// ─── node phase ────────────────────────────────────────────────────────────

const PHASE_META: Record<NodeTaskPhase | 'check_failed', { tone: Tone; icon: ReactNode }> = {
  blocked: { tone: 'err', icon: <Hand aria-hidden /> },
  ready: { tone: 'pending', icon: <Clock aria-hidden /> },
  executing: { tone: 'measure', icon: <LoaderCircle aria-hidden /> },
  submitted: { tone: 'warn', icon: <Send aria-hidden /> },
  in_review: { tone: 'warn', icon: <Eye aria-hidden /> },
  rework: { tone: 'warn', icon: <RotateCcw aria-hidden /> },
  accepted: { tone: 'ok', icon: <CheckCircle2 aria-hidden /> },
  failed: { tone: 'err', icon: <CircleX aria-hidden /> },
  cancelled: { tone: 'pending', icon: <Ban aria-hidden /> },
  // W7 Turbo: real outputs released without review / acceptance (never "accepted").
  completed_unverified: { tone: 'warn', icon: <ShieldQuestion aria-hidden /> },
  // S6: a Turbo node whose outputs failed their check (display only; see model.displayPhase).
  check_failed: { tone: 'err', icon: <CircleX aria-hidden /> },
};

export function phaseLabel(t: TFunction, phase: string): string {
  return dynT(t, `collab.phase.${phase}`, phase.replace(/_/g, ' '));
}

/** A node's phase as icon + text; pass ``displayPhase(node)`` so a Turbo node whose check failed reads "check failed". */
export function PhaseChip({ phase, compact }: { phase: NodeTaskPhase | 'check_failed' | string; compact?: boolean }) {
  const { t } = useI18n();
  const m = PHASE_META[phase as keyof typeof PHASE_META] ?? { tone: 'pending' as Tone, icon: <CircleDashed aria-hidden /> };
  return (
    <Chip tone={m.tone} icon={m.icon} className={compact ? 'cl-chip-sm' : ''}>
      {phaseLabel(t, phase)}
    </Chip>
  );
}

export function PausedChip() {
  const { t } = useI18n();
  return (
    <Chip tone="warn" icon={<PauseCircle aria-hidden />}>
      {t('collab.run.paused')}
    </Chip>
  );
}

// ─── agent executions ──────────────────────────────────────────────────────

const EXEC_META: Record<ExecutionStatus, { tone: Tone; icon: ReactNode }> = {
  running: { tone: 'measure', icon: <LoaderCircle aria-hidden /> },
  delivered: { tone: 'ok', icon: <CheckCircle2 aria-hidden /> },
  blocked: { tone: 'err', icon: <Hand aria-hidden /> },
  failed: { tone: 'err', icon: <CircleX aria-hidden /> },
  superseded: { tone: 'stale', icon: <RotateCcw aria-hidden /> },
  cancelled: { tone: 'pending', icon: <Ban aria-hidden /> },
};

export function ExecutionStatusChip({ status }: { status: ExecutionStatus | string }) {
  const { t } = useI18n();
  const m = EXEC_META[status as ExecutionStatus] ?? { tone: 'pending' as Tone, icon: <CircleDashed aria-hidden /> };
  return (
    <Chip tone={m.tone} icon={m.icon} className="cl-chip-sm">
      {dynT(t, `collab.node.execStatus.${status}`, status)}
    </Chip>
  );
}

export function executionRoleLabel(t: TFunction, role: ExecutionRole | string): string {
  return dynT(t, `collab.node.execRole.${role}`, role);
}

// ─── actors ────────────────────────────────────────────────────────────────

export function ActorIcon({ kind }: { kind: 'human' | 'agent' }) {
  return kind === 'agent' ? <Bot aria-hidden /> : <UserRound aria-hidden />;
}

/** Name of an actor_out (person or agent); disabled agents are marked. */
export function ActorLabel({ actor, fallback }: { actor: ActorOut | null | undefined; fallback?: ReactNode }) {
  const { t } = useI18n();
  if (!actor) return <span className="faint">{fallback ?? t('collab.actor.none')}</span>;
  const name = actor.kind === 'agent' ? actor.agent?.name ?? t('collab.actor.unknownAgent') : actor.user?.name ?? t('tk.formerMember', { id: actor.id.slice(0, 8) });
  const disabled = actor.kind === 'agent' && actor.agent && !actor.agent.enabled;
  return (
    <span className={`cl-actor ${actor.kind}`} title={t(`collab.actor.${actor.kind}`)}>
      <ActorIcon kind={actor.kind} />
      <span className="ellip">{name}</span>
      {disabled && <span className="faint small">· {t('collab.actor.disabled')}</span>}
    </span>
  );
}

/** An execution / pre-review agent as actor_out, whichever shape the server sent. */
export function asActor(a: AgentRef | null | undefined): ActorOut | null {
  if (!a) return null;
  if ('kind' in a && (a.kind === 'agent' || a.kind === 'human')) return a as ActorOut;
  const b = a as AgentBrief;
  return { kind: 'agent', id: b.id, user: null, agent: b };
}

/** Name of an ActorRef from the draft graph, resolved through the member and agent lists. */
export function ActorRefLabel({ actor, members, agents }: { actor: ActorRef | null | undefined; members: Members; agents: AgentProfile[] | undefined }) {
  const { t } = useI18n();
  if (!actor) return <span className="faint">{t('collab.actor.none')}</span>;
  if (actor.kind === 'agent') {
    const a = actor.agent_profile_id ? agents?.find((x) => x.id === actor.agent_profile_id) : undefined;
    return (
      <span className="cl-actor agent" title={t('collab.actor.agent')}>
        <Bot aria-hidden />
        <span className="ellip">{a ? a.name : actor.agent_profile_id ? t('collab.actor.unknownAgent') : t('collab.actor.none')}</span>
        {a && !a.enabled && <span className="faint small">· {t('collab.actor.disabled')}</span>}
      </span>
    );
  }
  return (
    <span className="cl-actor human" title={t('collab.actor.human')}>
      <UserRound aria-hidden />
      <span className="ellip">{actor.user_id ? members.name(actor.user_id) : t('collab.actor.none')}</span>
    </span>
  );
}

// ─── stage dots ────────────────────────────────────────────────────────────

function StageIcon({ state }: { state: StageState }) {
  switch (state) {
    case 'done':
      return <CheckCircle2 aria-hidden />;
    case 'active':
      return <CircleDot aria-hidden />;
    case 'failed':
      return <CircleX aria-hidden />;
    case 'blocked':
      return <Hand aria-hidden />;
    case 'skipped':
      return <ShieldQuestion aria-hidden />;
    default:
      return <CircleDashed aria-hidden />;
  }
}

/** The four internal stages (execute / check / submit / review) derived from the node phase. */
export function StageDots({ phase, compact }: { phase: NodeTaskPhase; compact?: boolean }) {
  const { t } = useI18n();
  const states = stageStates(phase);
  return (
    <ol className={`cl-stages ${compact ? 'compact' : ''}`} aria-label={t('collab.stages.label')}>
      {STAGES.map((s) => (
        <li key={s} className={`cl-stage st-${states[s]}`} title={`${t(`collab.stage.${s}`)} · ${t(`collab.stageState.${states[s]}`)}`}>
          <span className="cl-stage-dot">
            <StageIcon state={states[s]} />
          </span>
          <span className="cl-stage-label">{t(`collab.stage.${s}`)}</span>
          <span className="sr-only">: {t(`collab.stageState.${states[s]}`)}</span>
        </li>
      ))}
    </ol>
  );
}

// ─── misc chips ────────────────────────────────────────────────────────────

export function SimulatedChip() {
  const { t } = useI18n();
  return (
    <Chip tone="stale" icon={<Sparkles aria-hidden />} title={t('collab.simulatedHint')}>
      {t('collab.simulated')}
    </Chip>
  );
}

export function AcceptedChip({ children }: { children?: ReactNode }) {
  const { t } = useI18n();
  return (
    <Chip tone="ok" icon={<ShieldCheck aria-hidden />}>
      {children ?? t('collab.phase.accepted')}
    </Chip>
  );
}

/** Localized blocked reason (the server message stays as tooltip / fallback). */
export function blockedReasonText(t: TFunction, reason: { code: string; message: string } | null | undefined): string {
  if (!reason) return '';
  return dynT(t, `collab.blocked.${reason.code}`, reason.message || reason.code);
}

// ─── errors ────────────────────────────────────────────────────────────────

/** Collaboration error codes → friendly messages (raw codes stay visible next to them). */
export const CL_ERRORS: ErrorOverrides = {
  idempotency_mismatch: 'collab.err.idempotency_mismatch',
  draft_not_ready: 'collab.err.draft_not_ready',
  ai_unavailable: 'collab.err.ai_unavailable',
  ai_bad_response: 'collab.err.ai_bad_response',
  use_collaboration_runs: 'collab.err.use_collaboration_runs',
  collaboration_mixed_nodes: 'collab.err.collaboration_mixed_nodes',
  self_review: 'collab.err.self_review',
  manual_in_full_auto: 'collab.err.manual_in_full_auto',
  tool_not_allowed: 'collab.err.tool_not_allowed',
  part_required: 'collab.err.part_required',
  agent_not_found: 'collab.err.agent_not_found',
  agent_disabled: 'collab.err.agent_disabled',
  agent_purpose_mismatch: 'collab.err.agent_purpose_mismatch',
  agent_mode_unsupported: 'collab.err.agent_mode_unsupported',
  not_a_member: 'collab.err.not_a_member',
  read_only_member: 'collab.err.read_only_member',
  actor_not_allowed: 'collab.err.actor_not_allowed',
  mode_forbids: 'collab.err.mode_forbids',
  mode_forbids_human_decision: 'collab.err.mode_forbids_human_decision',
  criteria_incomplete: 'collab.err.criteria_incomplete',
  criteria_not_passed: 'collab.err.criteria_not_passed',
  evidence_unknown: 'collab.err.evidence_unknown',
  run_paused: 'collab.err.run_paused',
  run_not_full_auto: 'collab.err.run_not_full_auto',
  project_not_active: 'tk.err.project_not_active',
  forbidden: 'error.forbidden',
};

/** Friendly text of a graph / contract validation error (A03). */
export function validationErrorText(t: TFunction, e: { code: string; message: string; param?: string }): string {
  return dynT(t, `collab.verr.${e.code}`, dynT(t, `wf.verr.${e.code}`, e.message || e.code), { param: e.param ?? '' });
}

// ─── polling ───────────────────────────────────────────────────────────────

/**
 * Call `tick` every `ms` while `enabled` and the document is visible; a hidden
 * tab stops polling and polls once when it becomes visible again.
 */
export function useVisiblePoll(tick: () => void, ms: number, enabled: boolean): void {
  const ref = useRef(tick);
  ref.current = tick;
  useEffect(() => {
    if (!enabled) return;
    let handle: number | null = null;
    const start = () => {
      if (handle === null) handle = window.setInterval(() => ref.current(), ms);
    };
    const stop = () => {
      if (handle !== null) {
        window.clearInterval(handle);
        handle = null;
      }
    };
    const onVisibility = () => {
      if (document.visibilityState === 'visible') {
        ref.current();
        start();
      } else stop();
    };
    if (document.visibilityState === 'visible') start();
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      stop();
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [ms, enabled]);
}

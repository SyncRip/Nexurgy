// Approval vocabulary: request status / kind chips and the "approvals changed"
// signal that keeps the shell badge in step after a decision.

import { Ban,Blocks,CircleCheck,CircleX,Clock,Milestone,Replace,ShieldCheck,Undo2,UserCheck } from 'lucide-react';
import type { ReactNode } from 'react';
import type { RequestStatus } from '../../api/approvals';
import { Chip,type Tone } from '../../components/ui/Badges';
import { useI18n,type TFunction } from '../../i18n/I18nProvider';
import { dynLabel,stageText } from '../assets/meta';

const STATUS_META: Record<RequestStatus, { tone: Tone; icon: ReactNode }> = {
  pending: { tone: 'warn', icon: <Clock aria-hidden /> },
  approved: { tone: 'ok', icon: <CircleCheck aria-hidden /> },
  rejected: { tone: 'err', icon: <CircleX aria-hidden /> },
  withdrawn: { tone: 'pending', icon: <Undo2 aria-hidden /> },
  superseded: { tone: 'stale', icon: <Replace aria-hidden /> },
};

export function RequestStatusChip({ status }: { status: string }) {
  const { t } = useI18n();
  const m = STATUS_META[status as RequestStatus] ?? { tone: 'pending' as Tone, icon: <Ban aria-hidden /> };
  return (
    <Chip tone={m.tone} icon={m.icon}>
      {dynLabel(t, `ap.status.${status}`, status)}
    </Chip>
  );
}

/**
 * "Design review" / "Process review" / "Quote review" / "Task acceptance" /
 * "Stage review"; `direct` = an engineering review opened on an asset version
 * without a run step (process module review).
 */
export function requestKindText(t: TFunction, kind: string, stage: string | null | undefined, _direct = false): string {
  if (kind === 'task_acceptance') return t('ap.kind.task_acceptance');
  if (kind === 'milestone_review') return t('ap.kind.milestone_review');
  if (kind === 'engineering') return stage ? t('ap.kind.engineeringStage', { stage: stageText(t, stage) }) : t('ap.kind.engineering');
  return dynLabel(t, `ap.kind.${kind}`, kind);
}

export function RequestKindChip({ kind, stage, direct = false }: { kind: string; stage: string | null | undefined; direct?: boolean }) {
  const { t } = useI18n();
  const icon =
    kind === 'task_acceptance' ? <UserCheck aria-hidden /> : kind === 'milestone_review' ? <Milestone aria-hidden /> : direct ? <Blocks aria-hidden /> : <ShieldCheck aria-hidden />;
  return (
    <Chip tone={kind === 'engineering' ? 'measure' : 'accent'} icon={icon}>
      {requestKindText(t, kind, stage, direct)}
    </Chip>
  );
}

// ─── cross-component refresh signal ─────────────────────────────────────────

const EVENT = 'partroute:approvals-changed';

/** Tell listeners (the shell badge, open lists) that a request changed. */
export function notifyApprovalsChanged(): void {
  try {
    window.dispatchEvent(new Event(EVENT));
  } catch {
    /* non-browser */
  }
}

export function onApprovalsChanged(fn: () => void): () => void {
  window.addEventListener(EVENT, fn);
  return () => window.removeEventListener(EVENT, fn);
}

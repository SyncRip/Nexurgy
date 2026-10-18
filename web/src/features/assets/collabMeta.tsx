// Vocabulary of the collaboration layer as seen from the asset center: resource
// use kinds, per-task acceptance, knowledge eligibility, index states, usage and
// confirmation states, execution modes and actors. Every status is icon + text;
// cobalt blue is the action accent and never means "accepted". Publish state, task
// acceptance and knowledge eligibility are always shown as separate chips.

import {
  BadgeCheck,
  Ban,
  Bot,
  CircleCheck,
  CircleDashed,
  CircleX,
  Eye,
  FileSearch,
  Flag,
  History,
  Hourglass,
  Import,
  Link2,
  LoaderCircle,
  Minus,
  Recycle,
  ShieldAlert,
  TriangleAlert,
  UserCheck,
} from 'lucide-react';
import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import type { CollectionStatus, IndexStatus, ItemIndexStatus, ItemKind, KnowledgeUsageKind, UsageConfirmation } from '../../api/knowledge';
import { effectiveAcceptance, type AcceptanceStatus, type ActorOut, type ExecutionMode, type ResourceKind, type TaskAcceptance } from '../../api/resources';
import { paths } from '../../app/nav';
import { Chip, type Tone } from '../../components/ui/Badges';
import { useI18n, type TFunction } from '../../i18n/I18nProvider';
import { dynLabel, runPath } from './meta';

type Meta = { tone: Tone; icon: ReactNode };

// ─── resource use kinds ─────────────────────────────────────────────────────

const KIND_META: Record<ResourceKind, Meta> = {
  input: { tone: 'measure', icon: <Import aria-hidden /> },
  candidate: { tone: 'pending', icon: <CircleDashed aria-hidden /> },
  formal: { tone: 'ok', icon: <BadgeCheck aria-hidden /> },
  failed: { tone: 'err', icon: <CircleX aria-hidden /> },
  superseded: { tone: 'warn', icon: <History aria-hidden /> },
  evidence: { tone: 'stale', icon: <FileSearch aria-hidden /> },
};

export const resourceKindText = (t: TFunction, kind: string) => dynLabel(t, `rs.kind.${kind}`, kind);

export function ResourceKindChip({ kind }: { kind: ResourceKind | string }) {
  const { t } = useI18n();
  const m = KIND_META[kind as ResourceKind] ?? KIND_META.candidate;
  return (
    <Chip tone={m.tone} icon={m.icon} title={dynLabel(t, `rs.kindHint.${kind}`, '')}>
      {resourceKindText(t, kind)}
    </Chip>
  );
}

// ─── acceptance of *this* task ──────────────────────────────────────────────

const ACC_META: Record<AcceptanceStatus, Meta> = {
  not_applicable: { tone: 'pending', icon: <Minus aria-hidden /> },
  unsubmitted: { tone: 'pending', icon: <CircleDashed aria-hidden /> },
  in_review: { tone: 'warn', icon: <Eye aria-hidden /> },
  approved: { tone: 'ok', icon: <CircleCheck aria-hidden /> },
  rejected: { tone: 'err', icon: <Ban aria-hidden /> },
  invalidated: { tone: 'stale', icon: <ShieldAlert aria-hidden /> },
};

export const acceptanceText = (t: TFunction, status: AcceptanceStatus) => t(`rs.acc.${status}`);

/** Acceptance for this task: an approval that is no longer current reads as "invalidated", never as approved. */
/** `kind`: the resource use (or knowledge item kind) the acceptance belongs to; only formal / superseded uses lose an approval. */
export function AcceptanceChip({ acceptance, plain, kind }: { acceptance: TaskAcceptance | null | undefined; plain?: boolean; kind?: string | null }) {
  const { t } = useI18n();
  const status = effectiveAcceptance(acceptance, kind === 'history' ? 'superseded' : kind);
  const m = ACC_META[status];
  const hint = dynLabel(t, `rs.accHint.${status}`, '');
  if (plain)
    return (
      <span className={`as-acc as-acc-${status}`} title={hint || undefined}>
        {m.icon}
        <span>{acceptanceText(t, status)}</span>
      </span>
    );
  return (
    <Chip tone={m.tone} icon={m.icon} title={hint || undefined}>
      {acceptanceText(t, status)}
    </Chip>
  );
}

// ─── knowledge ──────────────────────────────────────────────────────────────

export function ReusableChip({ reusable }: { reusable: boolean }) {
  const { t } = useI18n();
  return reusable ? (
    <Chip tone="ok" icon={<Recycle aria-hidden />} title={t('kb.reusableHint.yes')}>
      {t('kb.reusable.yes')}
    </Chip>
  ) : (
    <Chip tone="pending" icon={<Ban aria-hidden />} title={t('kb.reusableHint.no')}>
      {t('kb.reusable.no')}
    </Chip>
  );
}

const INDEX_META: Record<IndexStatus, Meta> = {
  pending: { tone: 'pending', icon: <CircleDashed aria-hidden /> },
  indexing: { tone: 'measure', icon: <LoaderCircle aria-hidden /> },
  ready: { tone: 'ok', icon: <CircleCheck aria-hidden /> },
  partial: { tone: 'warn', icon: <TriangleAlert aria-hidden /> },
  failed: { tone: 'err', icon: <CircleX aria-hidden /> },
};

export function IndexStatusChip({ status }: { status: IndexStatus | string }) {
  const { t } = useI18n();
  const m = INDEX_META[status as IndexStatus] ?? INDEX_META.pending;
  return (
    <Chip tone={m.tone} icon={m.icon} title={t('kb.indexHint')}>
      {dynLabel(t, `kb.index.${status}`, status)}
    </Chip>
  );
}

const ITEM_INDEX_META: Record<ItemIndexStatus, Meta> = {
  pending: { tone: 'pending', icon: <CircleDashed aria-hidden /> },
  ready: { tone: 'ok', icon: <CircleCheck aria-hidden /> },
  failed: { tone: 'err', icon: <CircleX aria-hidden /> },
  not_applicable: { tone: 'pending', icon: <Minus aria-hidden /> },
};

export function ItemIndexChip({ status }: { status: ItemIndexStatus | string }) {
  const { t } = useI18n();
  const m = ITEM_INDEX_META[status as ItemIndexStatus] ?? ITEM_INDEX_META.pending;
  return (
    <Chip tone={m.tone} icon={m.icon}>
      {dynLabel(t, `kb.itemIndex.${status}`, status)}
    </Chip>
  );
}

const ITEM_KIND_META: Record<ItemKind, Meta> = {
  formal: { tone: 'ok', icon: <BadgeCheck aria-hidden /> },
  history: { tone: 'stale', icon: <History aria-hidden /> },
  candidate: { tone: 'pending', icon: <CircleDashed aria-hidden /> },
  failed: { tone: 'err', icon: <CircleX aria-hidden /> },
  evidence: { tone: 'stale', icon: <FileSearch aria-hidden /> },
};

export function ItemKindChip({ kind }: { kind: ItemKind | string }) {
  const { t } = useI18n();
  const m = ITEM_KIND_META[kind as ItemKind] ?? ITEM_KIND_META.candidate;
  return (
    <Chip tone={m.tone} icon={m.icon}>
      {dynLabel(t, `kb.itemKind.${kind}`, kind)}
    </Chip>
  );
}

export function UsageKindChip({ usage }: { usage: KnowledgeUsageKind | string }) {
  const { t } = useI18n();
  return usage === 'baseline' ? (
    <Chip tone="accent" icon={<Flag aria-hidden />} title={t('kb.usageHint.baseline')}>
      {t('kb.usage.baseline')}
    </Chip>
  ) : (
    <Chip tone="measure" icon={<Link2 aria-hidden />} title={t('kb.usageHint.reference')}>
      {dynLabel(t, `kb.usage.${usage}`, usage)}
    </Chip>
  );
}

const CONFIRM_META: Record<UsageConfirmation, Meta> = {
  none: { tone: 'pending', icon: <Minus aria-hidden /> },
  pending: { tone: 'warn', icon: <Hourglass aria-hidden /> },
  confirmed: { tone: 'ok', icon: <CircleCheck aria-hidden /> },
  rejected: { tone: 'err', icon: <Ban aria-hidden /> },
};

export function ConfirmationChip({ confirmation }: { confirmation: UsageConfirmation | string }) {
  const { t } = useI18n();
  const m = CONFIRM_META[confirmation as UsageConfirmation] ?? CONFIRM_META.none;
  return (
    <Chip tone={m.tone} icon={m.icon}>
      {dynLabel(t, `kb.confirm.${confirmation}`, confirmation)}
    </Chip>
  );
}

export function CollectionStatusChip({ status }: { status: CollectionStatus | string }) {
  const { t } = useI18n();
  return status === 'deprecated' ? (
    <Chip tone="stale" icon={<Ban aria-hidden />}>
      {t('kb.status.deprecated')}
    </Chip>
  ) : (
    <Chip tone="measure" icon={<BadgeCheck aria-hidden />}>
      {t('kb.status.active')}
    </Chip>
  );
}

// ─── execution mode & actors ────────────────────────────────────────────────

export function ExecutionModeChip({ mode }: { mode: ExecutionMode | null | undefined }) {
  const { t } = useI18n();
  if (!mode) return <span className="faint small">{t('rs.mode.none')}</span>;
  return mode === 'full_auto' ? (
    <Chip tone="measure" icon={<Bot aria-hidden />}>
      {t('rs.mode.full_auto')}
    </Chip>
  ) : (
    <Chip tone="accent" icon={<UserCheck aria-hidden />}>
      {t('rs.mode.semi_auto')}
    </Chip>
  );
}

/** Name of a human or agent actor; agents are marked so a decision by an agent never reads as a person. */
export function ActorName({ actor }: { actor: ActorOut | null | undefined }) {
  const { t } = useI18n();
  if (!actor) return <span className="faint">{t('common.none')}</span>;
  if (actor.kind === 'agent')
    return (
      <span className="as-actor" title={t('rs.actor.agent')}>
        <Bot aria-hidden className="inline-ico as-actor-ico" />
        {actor.agent?.name ?? t('rs.actor.agentUnnamed')}
      </span>
    );
  return <span className="as-actor">{actor.user?.name ?? t('as.someone')}</span>;
}

/** The acceptance decider of a resource row, from the flat task_acceptance ids. */
export function acceptanceActor(t: TFunction, a: TaskAcceptance | null | undefined, memberName: (id: string | null | undefined) => string | undefined): string {
  if (!a || !a.actor_kind) return '';
  if (a.actor_kind === 'agent') return t('rs.actor.agentDecided');
  return memberName(a.actor_user_id ?? a.actor_id) ?? t('as.someone');
}

// ─── links ──────────────────────────────────────────────────────────────────

/** The page that shows a run: the collaboration workbench for collaboration instances, the run page for tool runs. */
export function runPageOf(runId: string, nodeId: string | null | undefined, mode: ExecutionMode | null | undefined): string {
  return mode ? paths.collaborationRun(runId, nodeId ?? undefined) : runPath(runId, nodeId);
}

export function TaskLink({ id, number, title, className = 'as-use-link' }: { id: string; number: number | null | undefined; title: string | null | undefined; className?: string }) {
  const { t } = useI18n();
  return (
    <Link to={paths.task(id)} className={className}>
      {number !== null && number !== undefined && <span className="mono strong">T-{number}</span>} <span className="ellip">{title || t('rs.untitledTask')}</span>
    </Link>
  );
}

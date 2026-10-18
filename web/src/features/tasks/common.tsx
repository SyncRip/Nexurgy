// Shared pieces of the tasks UI: status chips, asset-version references with
// their quality labels, member names, guard-aware error notices and a reason
// dialog. Status is always icon + text; colour only reinforces it.

import './tasks.css';
import {
  AlertTriangle,
  Ban,
  Box,
  Calculator,
  CheckCircle2,
  CircleDashed,
  ClipboardList,
  Eye,
  FileText,
  Flag,
  FlaskConical,
  Hand,
  Import,
  Loader,
  OctagonX,
  PauseCircle,
  PencilLine,
  PlayCircle,
  RefreshCw,
  Ruler,
  ScanText,
  ShieldCheck,
  Triangle,
  Undo2,
} from 'lucide-react';
import { useMemo, useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { isApiError } from '../../api/client';
import { listMembers } from '../../api/endpoints';
import {
  isMissingVersion,
  type AssetType,
  type AssetVersion,
  type ImpactStatus,
  type Lifecycle,
  type MissingVersion,
  type RequestStatus,
  type RunBrief,
  type SubmissionStatus,
  type TaskPriority,
  type TaskStatus,
} from '../../api/pm';
import type { Member, Role, UUID } from '../../api/types';
import { paths } from '../../app/nav';
import { useAuth } from '../../auth/AuthProvider';
import { Chip, type Tone } from '../../components/ui/Badges';
import { Dialog } from '../../components/ui/Dialog';
import { Field } from '../../components/ui/Form';
import { ErrorNotice } from '../../components/ui/States';
import { useResource } from '../../hooks/useResource';
import type { MessageKey } from '../../i18n/en';
import type { ErrorOverrides } from '../../i18n/errors';
import { useI18n, useT, type TFunction } from '../../i18n/I18nProvider';
import { hasKey } from '../../i18n/core';
import { LimitedTextarea } from '../../components/ui/LimitedText';

/** Translate a dynamic key when it exists, else return the fallback. */
export function dynT(t: TFunction, key: string, fallback: string, vars?: Record<string, string | number>): string {
  return hasKey(key) ? t(key as MessageKey, vars) : fallback;
}

// ─── status chips ──────────────────────────────────────────────────────────

const TASK_TONE: Record<TaskStatus, Tone> = { todo: 'pending', in_progress: 'measure', in_review: 'warn', done: 'ok', cancelled: 'pending' };

export function TaskStatusIcon({ status }: { status: TaskStatus }) {
  switch (status) {
    case 'in_progress':
      return <PlayCircle aria-hidden />;
    case 'in_review':
      return <Eye aria-hidden />;
    case 'done':
      return <CheckCircle2 aria-hidden />;
    case 'cancelled':
      return <Ban aria-hidden />;
    default:
      return <CircleDashed aria-hidden />;
  }
}

export function TaskStatusChip({ status }: { status: TaskStatus }) {
  const t = useT();
  return (
    <Chip tone={TASK_TONE[status] ?? 'pending'} icon={<TaskStatusIcon status={status} />}>
      {t(`tk.status.${status}`)}
    </Chip>
  );
}

const LIFECYCLE_TONE: Record<Lifecycle, Tone> = { draft: 'pending', active: 'ok', on_hold: 'warn', completed: 'measure', cancelled: 'err' };

export function LifecycleChip({ lifecycle }: { lifecycle: Lifecycle }) {
  const t = useT();
  const icon =
    lifecycle === 'draft' ? <PencilLine aria-hidden /> : lifecycle === 'active' ? <PlayCircle aria-hidden /> : lifecycle === 'on_hold' ? <PauseCircle aria-hidden /> : lifecycle === 'completed' ? <CheckCircle2 aria-hidden /> : <OctagonX aria-hidden />;
  return (
    <Chip tone={LIFECYCLE_TONE[lifecycle] ?? 'pending'} icon={icon}>
      {t(`tk.lifecycle.${lifecycle}`)}
    </Chip>
  );
}

const PRIORITY_TONE: Record<TaskPriority, Tone> = { low: 'pending', normal: 'pending', high: 'warn', urgent: 'err' };

export function PriorityChip({ priority, hideNormal }: { priority: TaskPriority; hideNormal?: boolean }) {
  const t = useT();
  if (hideNormal && priority === 'normal') return null;
  return (
    <Chip tone={PRIORITY_TONE[priority] ?? 'pending'} icon={<Flag aria-hidden />} title={t('tk.field.priority')}>
      {t(`tk.priority.${priority}`)}
    </Chip>
  );
}

export function BlockedChip({ reason }: { reason: string }) {
  const t = useT();
  return (
    <Chip tone="err" icon={<Hand aria-hidden />} title={reason}>
      {t('tk.blocked')}
    </Chip>
  );
}

const SUBMISSION_TONE: Record<SubmissionStatus, Tone> = { pending: 'warn', approved: 'ok', rejected: 'err', withdrawn: 'pending' };

export function SubmissionStatusChip({ status }: { status: SubmissionStatus }) {
  const t = useT();
  const icon = status === 'approved' ? <CheckCircle2 aria-hidden /> : status === 'rejected' ? <OctagonX aria-hidden /> : status === 'withdrawn' ? <Undo2 aria-hidden /> : <Eye aria-hidden />;
  return (
    <Chip tone={SUBMISSION_TONE[status] ?? 'pending'} icon={icon}>
      {t(`tk.subStatus.${status}`)}
    </Chip>
  );
}

const REQUEST_TONE: Record<RequestStatus, Tone> = { pending: 'warn', approved: 'ok', rejected: 'err', withdrawn: 'pending', superseded: 'stale' };

export function RequestStatusChip({ status }: { status: RequestStatus }) {
  const t = useT();
  const icon = status === 'approved' ? <CheckCircle2 aria-hidden /> : status === 'rejected' ? <OctagonX aria-hidden /> : status === 'pending' ? <Eye aria-hidden /> : <Undo2 aria-hidden />;
  return (
    <Chip tone={REQUEST_TONE[status] ?? 'pending'} icon={icon}>
      {t(`tk.reqStatus.${status}`)}
    </Chip>
  );
}

export function ImpactStatusChip({ status }: { status: ImpactStatus }) {
  const t = useT();
  return (
    <Chip tone={status === 'open' ? 'warn' : 'pending'} icon={status === 'open' ? <AlertTriangle aria-hidden /> : <CheckCircle2 aria-hidden />}>
      {t(`tk.impactStatus.${status}`)}
    </Chip>
  );
}

// ─── assets ────────────────────────────────────────────────────────────────

export function AssetTypeIcon({ type }: { type: AssetType | null | undefined }) {
  switch (type) {
    case 'part_model':
      return <Box aria-hidden />;
    case 'drawing':
      return <FileText aria-hidden />;
    case 'process_plan':
      return <ClipboardList aria-hidden />;
    case 'quote':
      return <Calculator aria-hidden />;
    case 'intake':
      return <ScanText aria-hidden />;
    default:
      return <FileText aria-hidden />;
  }
}

export function assetTypeLabel(t: TFunction, type: string | null | undefined): string {
  return type ? dynT(t, `tk.assetType.${type}`, type) : t('common.none');
}

const LABEL_META: Record<string, { tone: Tone; icon: ReactNode }> = {
  simulated: { tone: 'stale', icon: <FlaskConical aria-hidden /> },
  estimated: { tone: 'warn', icon: <Ruler aria-hidden /> },
  mesh_reference: { tone: 'pending', icon: <Triangle aria-hidden /> },
  imported: { tone: 'measure', icon: <Import aria-hidden /> },
  from_failed_run: { tone: 'err', icon: <OctagonX aria-hidden /> },
};

/** Quality / provenance labels of an asset version (simulated, estimated …). Never hidden. */
export function VersionLabels({ labels }: { labels: string[] | null | undefined }) {
  const t = useT();
  if (!labels?.length) return null;
  return (
    <span className="tk-labels">
      {labels.map((l) => {
        const meta = LABEL_META[l] ?? { tone: 'pending' as Tone, icon: null };
        return (
          <Chip key={l} tone={meta.tone} icon={meta.icon} title={dynT(t, `tk.labelHint.${l}`, l)}>
            {dynT(t, `tk.label.${l}`, l)}
          </Chip>
        );
      })}
    </span>
  );
}

export function AvailabilityChip({ availability }: { availability: string }) {
  const t = useT();
  if (availability === 'ready') return null;
  return (
    <Chip tone={availability === 'failed' ? 'err' : 'pending'} icon={availability === 'failed' ? <OctagonX aria-hidden /> : <Loader aria-hidden />}>
      {dynT(t, `tk.availability.${availability}`, availability)}
    </Chip>
  );
}

export function EngineeringChip({ engineering }: { engineering: AssetVersion['engineering'] }) {
  const t = useT();
  if (!engineering) return null;
  const stage = dynT(t, `tk.stage.${engineering.stage}`, engineering.stage);
  return engineering.approved ? (
    <Chip tone="ok" icon={<ShieldCheck aria-hidden />}>
      {t('tk.version.approved', { stage })}
    </Chip>
  ) : (
    <Chip tone="pending" icon={<CircleDashed aria-hidden />}>
      {t('tk.version.notApproved', { stage })}
    </Chip>
  );
}

/** One asset version: type, asset name, label (R3 …), quality labels; links to the asset page. */
export function VersionRef({ v, link = true, showEngineering = false, compact = false }: { v: AssetVersion | MissingVersion | null | undefined; link?: boolean; showEngineering?: boolean; compact?: boolean }) {
  const t = useT();
  if (!v) return <span className="faint">{t('common.none')}</span>;
  if (isMissingVersion(v)) {
    return (
      <Chip tone="err" icon={<AlertTriangle aria-hidden />} title={v.id}>
        {t('tk.version.missing')}
      </Chip>
    );
  }
  const name = (
    <>
      <span className="tk-vref-type" title={assetTypeLabel(t, v.asset_type)}>
        <AssetTypeIcon type={v.asset_type} />
      </span>
      <span className="tk-vref-name ellip">{v.asset_name}</span>
      <span className="mono strong">{v.label}</span>
    </>
  );
  return (
    <span className={`tk-vref ${compact ? 'compact' : ''}`}>
      {link ? (
        <Link className="tk-vref-main" to={paths.asset(v.asset_id, v.id)} aria-label={t('tk.version.open', { name: v.asset_name, label: v.label })}>
          {name}
        </Link>
      ) : (
        <span className="tk-vref-main">{name}</span>
      )}
      <AvailabilityChip availability={v.availability} />
      <VersionLabels labels={v.labels} />
      {showEngineering && <EngineeringChip engineering={v.engineering} />}
    </span>
  );
}

// ─── members ───────────────────────────────────────────────────────────────

export interface MemberInfo {
  id: UUID;
  name: string;
  email: string;
  role: Role;
}

export interface Members {
  list: MemberInfo[] | undefined;
  error: unknown;
  reload: () => void;
  byId: Map<UUID, MemberInfo>;
  /** Display name (falls back to a short id for removed members). */
  name: (id: UUID | null | undefined) => string;
}

export function useMembers(projectId: UUID | undefined): Members {
  const t = useT();
  const res = useResource((signal) => listMembers(projectId!, { signal }), [projectId], { enabled: !!projectId });
  return useMemo(() => {
    const list = res.data?.map((m: Member) => ({ id: m.user.id, name: m.user.name, email: m.user.email, role: m.role }));
    const byId = new Map((list ?? []).map((m) => [m.id, m]));
    return {
      list,
      error: res.error,
      reload: res.reload,
      byId,
      name: (id) => (id ? byId.get(id)?.name ?? t('tk.formerMember', { id: id.slice(0, 8) }) : t('tk.unassigned')),
    };
  }, [res.data, res.error, res.reload, t]);
}

export function PersonName({ id, members }: { id: UUID | null | undefined; members: Members }) {
  const t = useT();
  const { user } = useAuth();
  if (!id) return <span className="faint">{t('tk.unassigned')}</span>;
  return (
    <span className="tk-person">
      {members.name(id)}
      {user?.id === id && <span className="faint"> ({t('common.you')})</span>}
    </span>
  );
}

// ─── dates ─────────────────────────────────────────────────────────────────

/** Server ISO timestamp → value for <input type="date"> (UTC calendar date; we store noon UTC). */
export function toDateInput(iso: string | null | undefined): string {
  return iso && /^\d{4}-\d{2}-\d{2}/.test(iso) ? iso.slice(0, 10) : '';
}

/** <input type="date"> value → API datetime (noon UTC keeps the calendar day in most time zones). */
export function fromDateInput(value: string): string | null {
  return /^\d{4}-\d{2}-\d{2}$/.test(value) ? `${value}T12:00:00Z` : null;
}

export function isOverdue(due: string | null | undefined, status: TaskStatus): boolean {
  if (!due || status === 'done' || status === 'cancelled') return false;
  const today = new Date();
  const todayKey = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`;
  return toDateInput(due) < todayKey;
}

// ─── errors ────────────────────────────────────────────────────────────────

/** Server error codes of the management layer → friendly messages (raw code stays visible). */
export const TK_ERRORS: ErrorOverrides = {
  task_locked: 'tk.err.task_locked',
  withdraw_first: 'tk.err.withdraw_first',
  active_runs: 'tk.err.active_runs',
  invalid_transition: 'tk.err.invalid_transition',
  reason_required: 'tk.err.reason_required',
  project_not_active: 'tk.err.project_not_active',
  project_archived: 'tk.err.project_archived',
  project_closed: 'tk.err.project_closed',
  project_not_completable: 'tk.err.project_not_completable',
  objective_required: 'tk.err.objective_required',
  task_in_review: 'tk.err.task_in_review',
  task_cancelled: 'tk.err.task_cancelled',
  task_not_in_progress: 'tk.err.task_not_in_progress',
  reviewer_required: 'tk.err.reviewer_required',
  self_review_forbidden: 'tk.err.self_review_forbidden',
  no_deliverables: 'tk.err.no_deliverables',
  submission_checks_failed: 'tk.err.submission_checks_failed',
  submission_pending: 'tk.err.submission_pending',
  submission_closed: 'tk.err.submission_closed',
  dependency_cycle: 'tk.err.dependency_cycle',
  dependency_other_project: 'tk.err.dependency_other_project',
  not_a_member: 'tk.err.not_a_member',
  read_only_member: 'tk.err.read_only_member',
  invalid_slot: 'tk.err.invalid_slot',
  baseline_empty: 'tk.err.baseline_empty',
  unknown_criterion: 'tk.err.unknown_criterion',
  workflow_archived: 'tk.err.workflow_archived',
  title_required: 'tk.err.title_required',
  use_transitions: 'tk.err.use_transitions',
  unknown_asset_type: 'tk.err.unknown_asset_type',
  last_owner: 'error.last_owner',
};

const GUARD_COUNT_KEYS = ['open_tasks', 'active_runs', 'pending_requests', 'open_impacts'] as const;

/** Explains a guard refusal: counts (project completion), lifecycle / status, runs. */
export function GuardDetails({ error }: { error: unknown }) {
  const t = useT();
  if (!isApiError(error) || (error.status !== 409 && error.status !== 422)) return null;
  const d = error.details as Record<string, unknown>;
  const lines: ReactNode[] = [];
  for (const k of GUARD_COUNT_KEYS) {
    const v = d[k];
    if (typeof v === 'number' && v > 0) lines.push(<li key={k}>{t(`tk.guard.${k}`, { n: v })}</li>);
  }
  if (typeof d.lifecycle === 'string') lines.push(<li key="lc">{t('tk.guard.lifecycle', { lifecycle: dynT(t, `tk.lifecycle.${d.lifecycle}`, d.lifecycle) })}</li>);
  if (typeof d.status === 'string') lines.push(<li key="st">{t('tk.guard.status', { status: dynT(t, `tk.status.${d.status}`, d.status) })}</li>);
  if (typeof d.current_version === 'number') lines.push(<li key="cv">{t('tk.guard.currentVersion', { n: d.current_version })}</li>);
  const runs = Array.isArray(d.runs) ? (d.runs as RunBrief[]) : [];
  if (!lines.length && !runs.length) return null;
  return (
    <div className="tk-guard small">
      {lines.length > 0 && <ul className="tk-plain-list">{lines}</ul>}
      {runs.length > 0 && (
        <ul className="tk-plain-list">
          {runs.map((r) => (
            <li key={r.id}>
              <Link to={`/runs/${r.id}`}>{r.title || t('tk.run.untitled')}</Link> <span className="faint">· {dynT(t, `wf.rstatus.${r.status}`, r.status)}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** ErrorNotice with the tasks error vocabulary, guard details and a reload button on version conflicts. */
export function TkError({ error, onReload, onRetry }: { error: unknown; onReload?: () => void; onRetry?: () => void }) {
  const t = useT();
  if (!error) return null;
  const conflict = isApiError(error) && error.code === 'version_conflict';
  return (
    <div className="stack-sm">
      <ErrorNotice error={error} overrides={TK_ERRORS} onRetry={onRetry} />
      <GuardDetails error={error} />
      {conflict && onReload && (
        <div>
          <button type="button" className="btn sm" onClick={onReload}>
            <RefreshCw /> {t('tk.reloadLatest')}
          </button>
        </div>
      )}
    </div>
  );
}

// ─── reason dialog ─────────────────────────────────────────────────────────

/** Confirm an action that may need a reason (pause, cancel, reopen …). */
export function ReasonDialog({
  title,
  body,
  reasonLabel,
  reasonRequired,
  confirmLabel,
  danger,
  onConfirm,
  onClose,
  children,
  onReload,
}: {
  title: ReactNode;
  body?: ReactNode;
  reasonLabel?: string;
  reasonRequired?: boolean;
  /** Hide the reason box entirely. */
  confirmLabel: ReactNode;
  danger?: boolean;
  /** Resolve to close; throw to show the error. */
  onConfirm: (reason: string) => Promise<void>;
  onClose: () => void;
  children?: ReactNode;
  onReload?: () => void;
}) {
  const t = useT();
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [touched, setTouched] = useState(false);
  const missing = !!reasonRequired && !reason.trim();
  const submit = async () => {
    setTouched(true);
    if (missing) return;
    setBusy(true);
    setError(null);
    try {
      await onConfirm(reason.trim());
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  };
  return (
    <Dialog
      title={title}
      onClose={onClose}
      busy={busy}
      size="sm"
      onSubmit={() => void submit()}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className={`btn ${danger ? 'danger-solid' : 'primary'}`} disabled={busy}>
            {busy ? t('common.working') : confirmLabel}
          </button>
        </>
      }
    >
      <div className="stack-sm">
        {body && <p className="muted">{body}</p>}
        {reasonLabel !== undefined && (
          <Field label={reasonLabel} optional={!reasonRequired} error={touched && missing ? t('validation.required', { field: reasonLabel }) : null}>
            <LimitedTextarea className="input" rows={3} limit="description" value={reason} onChange={(e) => setReason(e.target.value)} />
          </Field>
        )}
        {children}
        <TkError error={error} onReload={onReload} />
      </div>
    </Dialog>
  );
}

/** Small "(n)" count used in section headers. */
export function Count({ n }: { n: number }) {
  const { fmt } = useI18n();
  return <span className="tk-count mono">{fmt.number(n)}</span>;
}

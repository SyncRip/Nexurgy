// Shared building blocks of the part workspace panels: bilingual server
// messages, money, approval state + actions, staleness banners, badges.

import { AlertTriangle,BadgeCheck,Ban,CheckCircle2,CircleDashed,Clock,ExternalLink,FlaskConical,Hand,Hourglass,Loader2,Sigma,XCircle } from 'lucide-react';
import { useMemo,useState,type ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import { createPartApprovalRequest,getApprovalSubject,listPartApprovalRequests } from '../../api/approvals';
import { listMembers } from '../../api/endpoints';
import type { ApprovalStage,ApprovalState,ApprovalSubjectType,Bilingual,RevisionSource,RunStatus,SourceRef,StaleReason,Staleness,UUID } from '../../api/types';
import { paths } from '../../app/nav';
import { Chip } from '../../components/ui/Badges';
import { ErrorNotice } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { notifyApprovalsChanged } from '../approvals/meta';
import './workspace.css';

/** Pick the server's message in the current language. */
export function useBilingual(): (m: Bilingual | null | undefined) => string {
  const { lang } = useI18n();
  return (m) => (m ? (lang === 'zh' ? m.message_zh || m.message_en : m.message_en || m.message_zh) : '');
}

/** Pick label_zh / label_en. */
export function useLabel(): (m: { label_zh?: string | null; label_en?: string | null } | null | undefined) => string {
  const { lang } = useI18n();
  return (m) => (m ? ((lang === 'zh' ? m.label_zh || m.label_en : m.label_en || m.label_zh) ?? '') : '');
}

/** Integer fen → "¥1,234.56" (yuan, 2 decimals). */
export function useMoney(): (fen: number | null | undefined) => string {
  const { locale } = useI18n();
  return useMemo(() => {
    const f = new Intl.NumberFormat(locale, { style: 'currency', currency: 'CNY', minimumFractionDigits: 2, maximumFractionDigits: 2 });
    return (fen) => (fen === null || fen === undefined || Number.isNaN(fen) ? '—' : f.format(fen / 100));
  }, [locale]);
}

/** Number with fixed decimals in the current locale. */
export function useNum(): (n: number | null | undefined, digits?: number) => string {
  const { fmt } = useI18n();
  return (n, digits = 2) => fmt.number(n, { minimumFractionDigits: 0, maximumFractionDigits: digits });
}

/** user id → display name for this project. */
export function useMemberNames(projectId: UUID): (id: UUID | null | undefined) => string {
  const res = useResource((signal) => listMembers(projectId, { signal }), [projectId]);
  return (id) => {
    if (!id) return '—';
    const m = res.data?.find((x) => x.user.id === id);
    return m ? m.user.name : id.slice(0, 8);
  };
}

export function SimulatedBadge({ title }: { title?: string }) {
  const { t } = useI18n();
  return (
    <Chip tone="warn" icon={<FlaskConical aria-hidden />} title={title ?? t('ws.simulated.hint')}>
      {t('ws.simulated')}
    </Chip>
  );
}

export function EstimatedBadge({ title }: { title?: string }) {
  const { t } = useI18n();
  return (
    <Chip tone="stale" icon={<Sigma aria-hidden />} title={title ?? t('ws.estimated.hint')}>
      {t('ws.estimated')}
    </Chip>
  );
}

const RUN_TONE: Record<RunStatus, 'ok' | 'warn' | 'err' | 'pending' | 'measure'> = {
  queued: 'pending',
  running: 'measure',
  waiting: 'warn',
  succeeded: 'ok',
  partial: 'err', // S6: a required step failed — failed, never "partly done"
  failed: 'err',
  cancelled: 'pending',
};

export function RunStatusChip({ status }: { status: RunStatus }) {
  const { t } = useI18n();
  const icon =
    status === 'succeeded' ? <CheckCircle2 aria-hidden /> : status === 'failed' || status === 'partial' ? <XCircle aria-hidden /> : status === 'running' ? <Loader2 className="spin-icon" aria-hidden /> : status === 'waiting' ? <Hand aria-hidden /> : status === 'cancelled' ? <Ban aria-hidden /> : <Clock aria-hidden />;
  return (
    <Chip tone={RUN_TONE[status] ?? 'pending'} icon={icon}>
      {t(`ws.runStatus.${status}`)}
    </Chip>
  );
}

export function RevSourceChip({ source }: { source: RevisionSource }) {
  const { t } = useI18n();
  const tone = source === 'ai' ? 'accent' : source === 'import' ? 'measure' : 'pending';
  return (
    <Chip tone={tone} className="sm">
      {t(`revSource.${source}`)}
    </Chip>
  );
}

/** Latest decision for one stage. `valid` = approved and the subject is unchanged. */
export function ApprovalChip({ approval, names }: { approval: ApprovalState | null | undefined; names?: (id: string) => string }) {
  const { t, fmt } = useI18n();
  if (!approval) {
    return (
      <Chip tone="pending" icon={<CircleDashed aria-hidden />}>
        {t('ws.appr.none')}
      </Chip>
    );
  }
  const who = !approval.user_id ? t('rs.actor.agentDecided') : names ? names(approval.user_id) : '';
  const title = `${who ? `${who} · ` : ''}${t(`role.${approval.role}`)} · ${fmt.dateTime(approval.at)}${approval.comment ? ` — ${approval.comment}` : ''}`;
  if (approval.decision === 'rejected')
    return (
      <Chip tone="err" icon={<XCircle aria-hidden />} title={title}>
        {t('ws.appr.rejected')}
      </Chip>
    );
  if (!approval.valid)
    return (
      <Chip tone="stale" icon={<Hourglass aria-hidden />} title={title}>
        {t('ws.appr.outdated')}
      </Chip>
    );
  return (
    <Chip tone="ok" icon={<BadgeCheck aria-hidden />} title={title}>
      {t('ws.appr.approved')}
    </Chip>
  );
}

export function ReasonList({ reasons }: { reasons: (StaleReason | Bilingual)[] }) {
  const bi = useBilingual();
  if (!reasons.length) return null;
  return (
    <ul className="ws-reasons">
      {reasons.map((r, i) => (
        <li key={i}>{bi(r)}</li>
      ))}
    </ul>
  );
}

export function StaleBanner({ staleness, what }: { staleness: Staleness | null | undefined; what: string }) {
  const { t } = useI18n();
  if (!staleness?.stale) return null;
  return (
    <div className="callout warn" role="status">
      <AlertTriangle aria-hidden />
      <div className="grow">
        <strong>{t('ws.stale.title', { what })}</strong>
        {staleness.current?.rev_no ? <span className="muted"> · {t('ws.stale.current', { rev: `R${staleness.current.rev_no}` })}</span> : null}
        <ReasonList reasons={staleness.reasons} />
      </div>
    </div>
  );
}

/** Small table of rule constants a formula used. */
export function SourceList({ sources }: { sources: SourceRef[] }) {
  const { t } = useI18n();
  if (!sources.length) return <span className="faint small">{t('ws.noSources')}</span>;
  return (
    <ul className="ws-sources">
      {sources.map((s, i) => (
        <li key={`${s.id}-${i}`}>
          <code>{s.id}</code> <span className="mono">{s.value}</span>
          {s.unit ? <span className="faint"> {s.unit}</span> : null}
          <span className="faint"> · {s.source}</span>
          {s.version ? <span className="faint"> · v{s.version}</span> : null}
        </li>
      ))}
    </ul>
  );
}

/**
 * Approve / reject buttons for one subject + stage. The server checks the role
 * and that the subject is still current; 409 subject_stale reasons are shown.
 */
export function ApprovalActions({ partId, subjectType, subjectId, stage, can, onDone, compact }: {
  partId: UUID; subjectType: ApprovalSubjectType; subjectId: UUID; stage: ApprovalStage; can: boolean; onDone: () => void; compact?: boolean;
}) {
  const { t } = useI18n();
  const navigate = useNavigate();
  const toast = useToast();
  const query = { subject_type: subjectType, subject_id: subjectId, stage };
  const res = useResource(async (signal) => {
    const [subject, requests] = await Promise.all([getApprovalSubject(partId, query, { signal }), listPartApprovalRequests(partId, query, { signal })]);
    return { subject, requests };
  }, [partId, subjectType, subjectId, stage], { enabled: can });
  const data = res.data?.subject.subject_id === subjectId ? res.data : undefined;
  const [opened, setOpened] = useState<{ subjectId: string; requestId: string } | null>(null);
  const pendingId = opened?.subjectId === subjectId ? opened.requestId : data?.requests[0]?.id;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  if (!can) return null;
  const submit = async () => {
    if (pendingId) { navigate(paths.approvalRequest(pendingId)); return; }
    if (!data?.subject.can_request || busy) return;
    setBusy(true); setError(null);
    try {
      const result = await createPartApprovalRequest(partId, { ...query, expected_hash: data.subject.current_hash });
      setOpened({ subjectId, requestId: result.id });
      notifyApprovalsChanged(); onDone(); toast(t('core.review.created'));
      navigate(paths.approvalRequest(result.id));
    } catch (e) { setError(e); res.reload(); }
    finally { setBusy(false); }
  };
  return <div className="stack-sm"><button type="button" className={`btn ${compact ? 'sm' : ''} primary`} disabled={busy || (!pendingId && !data?.subject.can_request)} onClick={() => void submit()}>
    {pendingId ? <ExternalLink /> : <BadgeCheck />} {t(busy ? 'common.working' : pendingId ? 'core.review.pending' : 'core.review.request')}
  </button>{data && !pendingId && !data.subject.can_request && <p className="faint small">{t('core.review.subjectUnavailable')}</p>}<ErrorNotice error={error ?? res.error} onRetry={res.reload} /><ReasonList reasons={data?.subject.stale_reasons ?? []} /></div>;
}

/** Label/value grid. */
export function KV({ items }: { items: [ReactNode, ReactNode][] }) {
  return (
    <dl className="ws-kv">
      {items.map(([k, v], i) => (
        <div key={i}>
          <dt>{k}</dt>
          <dd>{v}</dd>
        </div>
      ))}
    </dl>
  );
}

/** Collapsible JSON / text detail. */
export function Details({ summary, children, open }: { summary: ReactNode; children: ReactNode; open?: boolean }) {
  return (
    <details className="ws-details" open={open}>
      <summary>{summary}</summary>
      <div className="ws-details-b">{children}</div>
    </details>
  );
}

export function jsonPreview(v: unknown, max = 4000): string {
  let s: string;
  try {
    s = typeof v === 'string' ? v : JSON.stringify(v, null, 2);
  } catch {
    s = String(v);
  }
  return s.length > max ? `${s.slice(0, max)}\n…` : s;
}

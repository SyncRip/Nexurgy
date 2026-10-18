// Part workspace — History tab: one timeline of revisions, drawings, plans,
// quotes and approvals (who / when / what). Approvals show whether they are
// still valid for the current data.

import { BadgeCheck, Calculator, ClipboardList, FileText, GitCommitHorizontal, History, XCircle } from 'lucide-react';
import { useMemo, useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { listApprovals, listDrawings, listProcessPlans, listQuotes, listRevisions } from '../../api/endpoints';
import { partOutputPath } from '../../app/versionLinks';
import { Chip } from '../../components/ui/Badges';
import { EmptyState, ErrorState, SkeletonRows } from '../../components/ui/States';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import type { PartTabProps } from '../../pages/part/registry';
import { RevSourceChip, SimulatedBadge, useMemberNames } from './common';
import { isSimulated } from './model/RevisionBar';
import './workspace.css';

type Kind = 'revision' | 'approval' | 'drawing' | 'plan' | 'quote';
interface TimelineEvent {
  id: string;
  at: string;
  kind: Kind;
  who: string | null;
  title: ReactNode;
  detail?: ReactNode;
  icon: ReactNode;
  tone: string;
}

const KINDS: Kind[] = ['revision', 'approval', 'drawing', 'plan', 'quote'];

export default function HistoryTab({ part, project }: PartTabProps) {
  const { t, fmt } = useI18n();
  const names = useMemberNames(project.id);
  const revs = useResource((s) => listRevisions(part.id, { signal: s }), [part.id]);
  const apprs = useResource((s) => listApprovals(part.id, { signal: s }), [part.id]);
  const dws = useResource((s) => listDrawings(part.id, { signal: s }), [part.id]);
  const plans = useResource((s) => listProcessPlans(part.id, { signal: s }), [part.id]);
  const quotes = useResource((s) => listQuotes(part.id, { signal: s }), [part.id]);
  const [filter, setFilter] = useState<Kind | 'all'>('all');

  const events = useMemo<TimelineEvent[]>(() => {
    const out: TimelineEvent[] = [];
    const revLabel = new Map((revs.data ?? []).map((r) => [r.id, r.label]));
    for (const r of revs.data ?? []) {
      out.push({
        id: `r${r.id}`,
        at: r.created_at ?? '',
        kind: 'revision',
        who: r.created_by,
        icon: <GitCommitHorizontal aria-hidden />,
        tone: 'accent',
        title: (
          <span className="row">
            <span>{t('ws.hist.revision', { rev: r.label })}</span>
            <RevSourceChip source={r.source} />
            {isSimulated(r) && <SimulatedBadge />}
            {r.mesh_only && <Chip tone="pending">{t('rev.meshOnly')}</Chip>}
            <Link className="linkish small" to={partOutputPath(part.id, 'model', r.id)}>
              {t('ws.hist.open')}
            </Link>
          </span>
        ),
        detail: [r.request_text ? `“${r.request_text}”` : null, r.note || null, r.parent_id ? t('ws.hist.parent', { rev: revLabel.get(r.parent_id) ?? '?' }) : null].filter(Boolean).join(' · ') || undefined,
      });
    }
    const subject = (type: string, id: string) => {
      if (type === 'model_revision') return revLabel.get(id) ?? t('ws.hist.revisionShort');
      if (type === 'process_plan') return t('ws.hist.planShort');
      if (type === 'quote') return t('ws.hist.quoteShort');
      if (type === 'intake') return t('ws.st.intake');
      if (type === 'drawing') return t('ws.hist.drawingShort');
      return type;
    };
    for (const a of apprs.data ?? []) {
      out.push({
        id: `a${a.id}`,
        at: a.created_at,
        kind: 'approval',
        who: a.user_id,
        icon: a.decision === 'approved' ? <BadgeCheck aria-hidden /> : <XCircle aria-hidden />,
        tone: a.decision === 'approved' ? 'ok' : 'err',
        title: (
          <span className="row">
            <span>{t(a.decision === 'approved' ? 'ws.hist.approved' : 'ws.hist.rejected', { stage: t(`ws.stage.${a.stage}`), subject: subject(a.subject_type, a.subject_id) })}</span>
            {a.decision === 'approved' && (a.valid ? <Chip tone="ok">{t('ws.hist.valid')}</Chip> : <Chip tone="stale">{t('ws.hist.superseded')}</Chip>)}
            <span className="faint small">{t(`role.${a.role}`)}</span>
          </span>
        ),
        detail: a.comment || undefined,
      });
    }
    for (const d of dws.data ?? []) {
      out.push({ id: `d${d.id}`, at: d.created_at ?? '', kind: 'drawing', who: null, icon: <FileText aria-hidden />, tone: 'measure', title: t('ws.hist.drawing', { rev: d.rev_no ? `R${d.rev_no}` : '—' }) });
    }
    for (const p of plans.data ?? []) {
      out.push({
        id: `p${p.id}`,
        at: p.created_at ?? '',
        kind: 'plan',
        who: p.created_by,
        icon: <ClipboardList aria-hidden />,
        tone: 'measure',
        title: t('ws.hist.plan', { source: p.model_revision_id ? (revLabel.get(p.model_revision_id) ?? '?') : t('ws.st.intake') }),
      });
    }
    for (const q of quotes.data ?? []) {
      out.push({
        id: `q${q.id}`,
        at: q.created_at ?? '',
        kind: 'quote',
        who: q.created_by,
        icon: <Calculator aria-hidden />,
        tone: 'measure',
        title: t('ws.hist.quote', { tiers: q.result.tiers.map((x) => x.qty).join(' / ') }),
      });
    }
    return out.sort((a, b) => b.at.localeCompare(a.at));
  }, [revs.data, apprs.data, dws.data, plans.data, quotes.data, t, part.id]);

  const firstError = revs.error ?? apprs.error ?? dws.error ?? plans.error ?? quotes.error;
  if (firstError && !revs.data)
    return (
      <ErrorState
        error={firstError}
        onRetry={() => {
          revs.reload();
          apprs.reload();
          dws.reload();
          plans.reload();
          quotes.reload();
        }}
      />
    );
  if (!revs.data || !apprs.data) return <SkeletonRows rows={6} />;

  const shown = filter === 'all' ? events : events.filter((e) => e.kind === filter);
  return (
    <div className="stack-sm">
      <div className="toolbar">
        <div className="seg" role="group" aria-label={t('ws.hist.filter')}>
          <button type="button" className="seg-btn" aria-pressed={filter === 'all'} onClick={() => setFilter('all')}>
            {t('common.all')} <span className="faint">{events.length}</span>
          </button>
          {KINDS.map((k) => (
            <button key={k} type="button" className="seg-btn" aria-pressed={filter === k} onClick={() => setFilter(k)}>
              {t(`ws.hist.kind.${k}`)} <span className="faint">{events.filter((e) => e.kind === k).length}</span>
            </button>
          ))}
        </div>
      </div>
      {!shown.length ? (
        <EmptyState title={t('ws.hist.empty')} icon={<History />} />
      ) : (
        <ol className="ws-timeline">
          {shown.map((e) => (
            <li key={e.id} className={`ws-tl tone-${e.tone}`}>
              <span className="ws-tl-ico">{e.icon}</span>
              <div className="ws-tl-body">
                <div className="ws-tl-title">{e.title}</div>
                {e.detail && <div className="small muted">{e.detail}</div>}
                <div className="faint small">
                  {e.who ? `${names(e.who)} · ` : ''}
                  <time dateTime={e.at} title={fmt.dateTime(e.at)}>
                    {fmt.relative(e.at)}
                  </time>
                </div>
              </div>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

// Stage vocabulary: stage / review status chips and readable blocker lists for
// stage readiness, stage-review refusals and the project completion guard.
// Status is always icon + text; colour only reinforces it.

import './stages.css';
import { CheckCircle2, CircleDashed, CircleX, Clock, Eye, OctagonX, Replace, TriangleAlert, Undo2 } from 'lucide-react';
import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { normalizeBlockers, type StageBlocker } from '../../api/stages';
import { paths } from '../../app/nav';
import { Chip, type Tone } from '../../components/ui/Badges';
import { hasKey } from '../../i18n/core';
import type { MessageKey } from '../../i18n/en';
import { useI18n, type TFunction } from '../../i18n/I18nProvider';
import { assetTypeLabel } from '../assets/meta';

const MS_META: Record<string, { tone: Tone; icon: ReactNode }> = {
  open: { tone: 'pending', icon: <CircleDashed aria-hidden /> },
  in_review: { tone: 'warn', icon: <Eye aria-hidden /> },
  accepted: { tone: 'ok', icon: <CheckCircle2 aria-hidden /> },
};

export function MilestoneStatusChip({ status }: { status: string | undefined }) {
  const { t } = useI18n();
  const s = status ?? 'open';
  const m = MS_META[s] ?? MS_META.open;
  return (
    <Chip tone={m.tone} icon={m.icon}>
      {dyn(t, `stg.status.${s}`, s)}
    </Chip>
  );
}

const REVIEW_META: Record<string, { tone: Tone; icon: ReactNode }> = {
  pending: { tone: 'warn', icon: <Clock aria-hidden /> },
  approved: { tone: 'ok', icon: <CheckCircle2 aria-hidden /> },
  rejected: { tone: 'err', icon: <CircleX aria-hidden /> },
  withdrawn: { tone: 'pending', icon: <Undo2 aria-hidden /> },
  superseded: { tone: 'stale', icon: <Replace aria-hidden /> },
};

export function ReviewStatusChip({ status }: { status: string }) {
  const { t } = useI18n();
  const m = REVIEW_META[status] ?? { tone: 'pending' as Tone, icon: <OctagonX aria-hidden /> };
  return (
    <Chip tone={m.tone} icon={m.icon}>
      {dyn(t, `stg.reviewStatus.${status}`, status)}
    </Chip>
  );
}

/** Translate a dynamic key when it exists, else fall back (underscores become spaces). */
export function dyn(t: TFunction, key: string, fallback: string, vars?: Record<string, string | number>): string {
  return hasKey(key) ? t(key as MessageKey, vars) : fallback.replace(/_/g, ' ');
}

function listOf(v: unknown): string[] {
  return Array.isArray(v) ? v.map((x) => (x && typeof x === 'object' ? String((x as { name?: unknown; title?: unknown }).name ?? (x as { title?: unknown }).title ?? '') : String(x))).filter(Boolean) : [];
}

/** Readable text of one blocker (stage readiness, review refusal or completion guard). */
export function blockerText(t: TFunction, b: StageBlocker, prefix: 'stg.blocker' | 'stg.completion' = 'stg.blocker'): string {
  const names = listOf(b.names ?? b.milestones ?? b.asset_types ?? b.missing ?? b.tasks);
  const types = listOf(b.asset_types ?? b.missing_types ?? (b.asset_type ? [b.asset_type] : [])).map((x) => assetTypeLabel(t, x));
  const key = `${prefix}.${b.code}`;
  if (!hasKey(key)) return b.message || b.code.replace(/_/g, ' ');
  const n = typeof b.count === 'number' ? b.count : typeof b.n === 'number' ? b.n : names.length;
  return t(key as MessageKey, {
    n,
    names: names.join(', ') || '—',
    types: types.join(', ') || '—',
    task: [b.key, b.title].filter(Boolean).join(' ') || '—',
    status: typeof b.status === 'string' ? dyn(t, `tk.status.${b.status}`, b.status) : '—',
    lifecycle: typeof b.lifecycle === 'string' ? dyn(t, `tk.lifecycle.${b.lifecycle}`, b.lifecycle) : '—',
  });
}

/** Bulleted blocker list; task blockers link to the task. Accepts the list or the dict shape. */
export function BlockerList({ blockers, prefix = 'stg.blocker', tone = 'err' }: { blockers: unknown; prefix?: 'stg.blocker' | 'stg.completion'; tone?: 'err' | 'warn' }) {
  const { t } = useI18n();
  const list = normalizeBlockers(blockers);
  if (!list.length) return null;
  return (
    <ul className={`stg-blockers ${tone}`}>
      {list.map((b, i) => (
        <li key={`${b.code}-${i}`} title={b.message}>
          <TriangleAlert aria-hidden />
          <span className="grow">
            {blockerText(t, b, prefix)}
            {typeof b.task_id === 'string' && (
              <>
                {' '}
                <Link to={paths.task(b.task_id)} className="small">
                  {t('stg.openTask')}
                </Link>
              </>
            )}
          </span>
          <code className="error-code">{b.code}</code>
        </li>
      ))}
    </ul>
  );
}

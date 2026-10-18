// Empty, loading (skeleton) and error states shared by every page.

import { AlertTriangle, Lock, PowerOff, RefreshCw, SearchX, type LucideIcon } from 'lucide-react';
import type { ReactNode } from 'react';
import type { MessageKey } from '../../i18n/en';
import { describeError, type ErrorOverrides } from '../../i18n/errors';
import { useT } from '../../i18n/I18nProvider';
import { Button } from './Button';
import './States.css';

/** Isometric wireframe block used as the empty-state mark, over a soft brand halo. */
export function BlueprintMark({ size = 72 }: { size?: number }) {
  return (
    <span className="bp-mark-wrap">
      <svg width={size} height={size} viewBox="0 0 72 72" aria-hidden className="bp-mark">
        <g fill="none" strokeWidth="1.2" strokeLinejoin="round">
          <path d="M36 10 60 23v26L36 62 12 49V23z" stroke="var(--line-2)" />
          <path d="M12 23l24 13 24-13M36 36v26" stroke="var(--line-2)" />
          <path d="M36 10 60 23 36 36 12 23z" stroke="var(--measure)" strokeDasharray="3 3" />
          <circle cx="36" cy="23" r="4.5" stroke="var(--accent)" />
        </g>
      </svg>
    </span>
  );
}

/**
 * Why there is nothing to show. Each kind has its own mark and default copy,
 * so "no access" or "filtered out" never reads as "empty".
 */
export type EmptyKind = 'empty' | 'no-result' | 'no-permission' | 'disabled' | 'error';

const EMPTY_COPY: Record<EmptyKind, { title: MessageKey; body?: MessageKey }> = {
  empty: { title: 'emptyState.empty.title' },
  'no-result': { title: 'emptyState.noResult.title', body: 'emptyState.noResult.body' },
  'no-permission': { title: 'emptyState.noPermission.title', body: 'emptyState.noPermission.body' },
  disabled: { title: 'emptyState.disabled.title', body: 'emptyState.disabled.body' },
  error: { title: 'error.loadFailed' },
};

const KIND_ICON: Partial<Record<EmptyKind, LucideIcon>> = { 'no-result': SearchX, 'no-permission': Lock, disabled: PowerOff };

function KindMark({ kind, compact }: { kind: EmptyKind; compact?: boolean }) {
  if (kind === 'error') {
    return (
      <span className="err-mark" aria-hidden>
        <AlertTriangle />
      </span>
    );
  }
  const Icon = KIND_ICON[kind];
  if (!Icon) return <BlueprintMark size={compact ? 48 : 72} />;
  return (
    <span className="pr-empty-mark" aria-hidden>
      <Icon />
    </span>
  );
}

export interface EmptyStateProps {
  /** Defaults to 'empty', which renders exactly as before kinds existed. */
  kind?: EmptyKind;
  /** Defaults to the kind's copy. */
  title?: ReactNode;
  /** Defaults to the kind's copy; pass null to show none. */
  body?: ReactNode;
  /** The next step. Replaces the built-in retry / clear-filters buttons. */
  action?: ReactNode;
  icon?: ReactNode;
  compact?: boolean;
  onRetry?: () => void;
  onClearFilters?: () => void;
}

export function EmptyState({ kind = 'empty', title, body, action, icon, compact, onRetry, onClearFilters }: EmptyStateProps) {
  const t = useT();
  const copy = EMPTY_COPY[kind];
  const shownBody = body === undefined && copy.body ? t(copy.body) : body;
  const builtIn = (onRetry || onClearFilters) && (
    <>
      {onClearFilters && <Button onClick={onClearFilters}>{t('common.clearFilters')}</Button>}
      {onRetry && (
        <Button icon={<RefreshCw />} onClick={onRetry}>
          {t('common.retry')}
        </Button>
      )}
    </>
  );
  const next = action ?? builtIn;
  return (
    <div className={['empty-state', compact && 'compact', kind === 'error' && 'error'].filter(Boolean).join(' ')} data-kind={kind} role={kind === 'error' ? 'alert' : undefined}>
      {icon ?? <KindMark kind={kind} compact={compact} />}
      <div className="empty-copy">
        <h3>{title ?? t(copy.title)}</h3>
        {shownBody && <p>{shownBody}</p>}
      </div>
      {next && <div className="empty-action">{next}</div>}
    </div>
  );
}

/** Translated message + the raw server code in small mono text. */
export function ErrorText({ error, overrides }: { error: unknown; overrides?: ErrorOverrides }) {
  const t = useT();
  const d = describeError(error, overrides);
  return (
    <span className="error-text">
      <span>{t(d.key)}</span>
      {d.code && (
        <code className="error-code" title={d.raw}>
          {t('common.errorCode', { code: d.code })}
        </code>
      )}
    </span>
  );
}

/** Inline error callout (forms, dialogs). */
export function ErrorNotice({ error, overrides, onRetry }: { error: unknown; overrides?: ErrorOverrides; onRetry?: () => void }) {
  const t = useT();
  if (!error) return null;
  return (
    <div className="callout err" role="alert">
      <AlertTriangle aria-hidden />
      <div className="grow">
        <ErrorText error={error} overrides={overrides} />
      </div>
      {onRetry && (
        <button type="button" className="btn sm" onClick={onRetry}>
          <RefreshCw /> {t('common.retry')}
        </button>
      )}
    </div>
  );
}

/** Full-area error state (failed page / tab load). */
export function ErrorState({ error, title, onRetry, overrides }: { error: unknown; title?: ReactNode; onRetry?: () => void; overrides?: ErrorOverrides }) {
  return <EmptyState kind="error" title={title} body={<ErrorText error={error} overrides={overrides} />} onRetry={onRetry} />;
}

export function Skeleton({ w = '100%', h = 12, className = '' }: { w?: number | string; h?: number; className?: string }) {
  return <span className={`skel ${className}`} style={{ width: w, height: h }} aria-hidden />;
}

/** Table-shaped skeleton for list pages. */
export function SkeletonRows({ rows = 6, cols = 5 }: { rows?: number; cols?: number }) {
  const t = useT();
  return (
    <div className="skel-table" role="status" aria-label={t('common.loading')}>
      {Array.from({ length: rows }, (_, r) => (
        <div className="skel-row" key={r}>
          {Array.from({ length: cols }, (_, c) => (
            <Skeleton key={c} w={c === 0 ? '18%' : c === 1 ? '32%' : `${10 + ((r * 7 + c * 13) % 12)}%`} />
          ))}
        </div>
      ))}
    </div>
  );
}

export function SkeletonCards({ count = 6 }: { count?: number }) {
  const t = useT();
  return (
    <div className="card-grid" role="status" aria-label={t('common.loading')}>
      {Array.from({ length: count }, (_, i) => (
        <div className="pcard skel-card" key={i}>
          <Skeleton w="35%" h={14} />
          <Skeleton w="70%" h={18} />
          <Skeleton w="90%" />
          <Skeleton w="50%" />
        </div>
      ))}
    </div>
  );
}

export function Spinner({ label }: { label?: string }) {
  return <span className="spinner" role={label ? 'status' : undefined} aria-label={label} />;
}

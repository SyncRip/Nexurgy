// Shared bits of the engineering spec UI (W2): status / read-state / readiness / fact chips and value formatting.
// Unknown enum values never render as success: they fall back to a neutral chip with the raw value.

import { AlertTriangle, CheckCircle2, CircleDashed, CircleSlash, Clock, Eye, EyeOff, FileWarning, FlaskConical, HelpCircle, Loader2, PauseCircle, XCircle } from 'lucide-react';
import type { ReactNode } from 'react';
import type { FactStatus } from '../../api/engineering';
import type { ReadStatus, Readiness, SpecVersionStatus } from '../../api/spec';
import { Chip, type Tone } from '../../components/ui/Badges';
import { hasKey } from '../../i18n/core';
import type { MessageKey } from '../../i18n/en';
import { useI18n, type TFunction } from '../../i18n/I18nProvider';

/** Translate a dynamic key when it exists, else the raw value (unknown enums never read as success). */
export function tk(t: TFunction, key: string, fallback: string, vars?: Record<string, string | number>): string {
  return hasKey(key) ? t(key as MessageKey, vars) : fallback;
}

const STATUS_TONE: Record<SpecVersionStatus, Tone> = { extracting: 'pending', ready: 'ok', needs_answers: 'warn', failed: 'err' };

export function SpecStatusChip({ status }: { status: SpecVersionStatus | string }) {
  const { t } = useI18n();
  const known = status in STATUS_TONE;
  const icon: ReactNode =
    status === 'extracting' ? <Loader2 className="spin-icon" aria-hidden /> : status === 'ready' ? <CheckCircle2 aria-hidden /> : status === 'needs_answers' ? <HelpCircle aria-hidden /> : status === 'failed' ? <XCircle aria-hidden /> : <CircleDashed aria-hidden />;
  return (
    <Chip tone={known ? STATUS_TONE[status as SpecVersionStatus] : 'pending'} icon={icon}>
      {tk(t, `spec.status.${status}`, status)}
    </Chip>
  );
}

export function SimulatedChip() {
  const { t } = useI18n();
  return (
    <Chip tone="pending" icon={<FlaskConical aria-hidden />} title={t('spec.simulatedHint')}>
      {t('spec.simulated')}
    </Chip>
  );
}

const READ_TONE: Record<ReadStatus, Tone> = { read: 'ok', partial: 'warn', unread: 'stale', failed: 'err' };

/** Read state of a source (read / partial / unread / failed), computed by the server from the chunks it sent. */
export function SourceReadChip({ status, title }: { status: ReadStatus | string; title?: string }) {
  const { t } = useI18n();
  const known = status in READ_TONE;
  const icon = status === 'read' ? <Eye aria-hidden /> : status === 'failed' ? <FileWarning aria-hidden /> : <EyeOff aria-hidden />;
  return (
    <Chip tone={known ? READ_TONE[status as ReadStatus] : 'pending'} icon={icon} title={title}>
      {tk(t, `spec.read.${status}`, status)}
    </Chip>
  );
}

/**
 * Read state of an AI context item / planner source: "content" (read evidence), "partial" (only the ranges)
 * or "metadata" (a label-only reference). Used by the AI workspace context inspector too.
 */
export function ReadStateChip({ read, ranges }: { read: string; ranges?: string[] | null }) {
  const { t } = useI18n();
  if (read === 'content')
    return (
      <Chip tone="ok" icon={<Eye aria-hidden />} className="spec-chip-sm" title={t('spec.read.contentHint')}>
        {t('spec.read.content')}
      </Chip>
    );
  if (read === 'partial')
    return (
      <Chip tone="warn" icon={<Eye aria-hidden />} className="spec-chip-sm" title={t('spec.read.partialHint', { ranges: (ranges ?? []).join('; ') })}>
        {t('spec.read.partial')}
      </Chip>
    );
  return (
    <Chip tone="stale" icon={<EyeOff aria-hidden />} className="spec-chip-sm" title={t('spec.read.metadataHint')}>
      {t('spec.read.metadata')}
    </Chip>
  );
}

const READY_TONE: Record<Readiness, Tone> = { ready: 'ok', blocked: 'warn', partial: 'warn' };

export function ReadinessChip({ state, questions }: { state: Readiness | string; questions?: string[] }) {
  const { t } = useI18n();
  const known = state in READY_TONE;
  const icon = state === 'ready' ? <CheckCircle2 aria-hidden /> : state === 'blocked' ? <PauseCircle aria-hidden /> : <Clock aria-hidden />;
  return (
    <Chip tone={known ? READY_TONE[state as Readiness] : 'pending'} icon={icon} className="spec-chip-sm" title={questions?.length ? t('spec.ready.hint', { qs: questions.join(', ') }) : undefined}>
      {tk(t, `spec.ready.${state}`, state)}
    </Chip>
  );
}

const FACT_TONE: Record<FactStatus, Tone> = { asserted: 'ok', derived: 'measure', assumed: 'warn', conflicting: 'err', unknown: 'stale' };
export type FactIcon = 'check' | 'slash' | 'alert' | 'help';
const FACT_ICON: Record<FactStatus, FactIcon> = { asserted: 'check', derived: 'check', assumed: 'slash', conflicting: 'alert', unknown: 'help' };

/** Tone and icon of a fact status. Only asserted / derived get the check mark; an unrecognised status is neutral
 * (pending tone, question mark), never shown as confirmed. */
export function factStatusLook(status: string): { tone: Tone; icon: FactIcon } {
  return status in FACT_TONE ? { tone: FACT_TONE[status as FactStatus], icon: FACT_ICON[status as FactStatus] } : { tone: 'pending', icon: 'help' };
}

export function FactStatusChip({ status }: { status: FactStatus | string }) {
  const { t } = useI18n();
  const look = factStatusLook(status);
  const icon =
    look.icon === 'check' ? <CheckCircle2 aria-hidden /> : look.icon === 'slash' ? <CircleSlash aria-hidden /> : look.icon === 'alert' ? <AlertTriangle aria-hidden /> : <HelpCircle aria-hidden />;
  return (
    <Chip tone={look.tone} icon={icon} className="spec-chip-sm">
      {tk(t, `spec.fact.status.${status}`, status)}
    </Chip>
  );
}

/** "60 mm", "Q235A", "?" for a missing value; objects are shown as compact JSON. */
export function fmtValue(value: unknown, unit?: string | null): string {
  if (value === null || value === undefined || value === '') return '?';
  const v = typeof value === 'object' ? JSON.stringify(value) : String(value);
  return unit ? `${v} ${unit}` : v;
}

export function fmtXyz(v: number[] | null | undefined): string {
  return (v ?? []).map((x) => String(Math.round(x * 1000) / 1000)).join(', ');
}

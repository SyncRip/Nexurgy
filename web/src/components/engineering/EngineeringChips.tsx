// Status chips shared by assemblies, drawings, drafting, specs and Turbo runs. Each dimension has its own
// chip so a skipped check never reads as "passed" and a finished Turbo run never reads as "accepted".

import { AlertTriangle, CheckCircle2, CircleDashed, CircleSlash, Clock, Gauge, ShieldQuestion, XCircle, Zap } from 'lucide-react';
import type { AcceptanceStatus, CheckStatus, ExecutionPolicy, RepresentationLevel } from '../../api/engineering';
import { useI18n } from '../../i18n/I18nProvider';
import { Chip, type Tone } from '../ui/Badges';

export function PolicyChip({ policy }: { policy: ExecutionPolicy }) {
  const { t } = useI18n();
  if (policy === 'turbo')
    return (
      <Chip tone="warn" icon={<Zap aria-hidden />} title={t('me.policy.turboHint')}>
        {t('me.policy.turbo')}
      </Chip>
    );
  return (
    <Chip tone="pending" icon={<Gauge aria-hidden />} title={t('me.policy.standardHint')}>
      {t('me.policy.standard')}
    </Chip>
  );
}

/** A generated result that still needs verification or review, regardless of execution policy. */
export function UnverifiedChip({ title }: { title?: string }) {
  const { t } = useI18n();
  return (
    <Chip tone="warn" icon={<ShieldQuestion aria-hidden />} title={title ?? t('me.unverified.hint')}>
      {t('me.unverified')}
    </Chip>
  );
}

const CHECK_TONE: Record<CheckStatus, Tone> = {
  not_requested: 'pending',
  skipped_turbo: 'warn',
  pending: 'pending',
  passed: 'ok',
  failed: 'err',
  unknown: 'stale',
};

function CheckIcon({ status }: { status: CheckStatus }) {
  switch (status) {
    case 'passed':
      return <CheckCircle2 aria-hidden />;
    case 'failed':
      return <XCircle aria-hidden />;
    case 'skipped_turbo':
      return <CircleSlash aria-hidden />;
    case 'pending':
      return <Clock aria-hidden />;
    case 'unknown':
      return <AlertTriangle aria-hidden />;
    default:
      return <CircleDashed aria-hidden />;
  }
}

export function CheckStatusChip({ status, label }: { status: CheckStatus; label?: string }) {
  const { t } = useI18n();
  return (
    <Chip tone={CHECK_TONE[status]} icon={<CheckIcon status={status} />}>
      {label ? `${label} · ` : ''}
      {t(`me.check.${status}`)}
    </Chip>
  );
}

const ACCEPT_TONE: Record<AcceptanceStatus, Tone> = { not_requested: 'pending', pending: 'pending', accepted: 'ok', rejected: 'err' };

export function AcceptanceChip({ status }: { status: AcceptanceStatus }) {
  const { t } = useI18n();
  return <Chip tone={ACCEPT_TONE[status]}>{t(`me.accept.${status}`)}</Chip>;
}

export function StaleChip({ title }: { title?: string }) {
  const { t } = useI18n();
  return (
    <Chip tone="stale" icon={<AlertTriangle aria-hidden />} title={title ?? t('me.stale.hint')}>
      {t('me.stale')}
    </Chip>
  );
}

export function RepresentationChip({ level }: { level: RepresentationLevel }) {
  const { t } = useI18n();
  if (level === 'exact') return null;
  return (
    <Chip tone="measure" title={t(`me.rep.${level}.hint`)}>
      {t(`me.rep.${level}`)}
    </Chip>
  );
}

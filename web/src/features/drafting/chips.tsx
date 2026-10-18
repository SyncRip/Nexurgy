// Small status pieces of the drafting workspace. Execution (output), engineering check and scale are separate
// chips; an unknown enum never renders as a success.

import { AlertTriangle, Bot, CheckCircle2, FlaskConical, Ruler, ScanLine } from 'lucide-react';
import type { EntityOrigin, OutputStatus, ScaleStatus, VersionBrief } from '../../api/drafting';
import { CheckStatusChip, PolicyChip, UnverifiedChip } from '../../components/engineering/EngineeringChips';
import { unverifiedBesideCheck } from '../turbo/model';
import { Chip } from '../../components/ui/Badges';
import { useI18n } from '../../i18n/I18nProvider';

export function OutputChip({ status, dwg }: { status: OutputStatus | string; dwg?: string | null }) {
  const { t } = useI18n();
  if (status === 'succeeded') return <Chip tone="ok" icon={<CheckCircle2 aria-hidden />}>{t('dr.output.succeeded')}</Chip>;
  return (
    <Chip tone="warn" icon={<AlertTriangle aria-hidden />} title={t('dr.output.partialHint')}>
      {t('dr.output.partial')}
      {dwg ? ` · DWG ${dwg === 'failed' ? t('dr.dwg.failed') : dwg === 'unavailable' ? t('dr.dwg.unavailable') : dwg === 'unverified' ? t('dr.dwg.unverified') : dwg}` : ''}
    </Chip>
  );
}

const SCALE_TONE = { native: 'ok', calibrated: 'ok', assumed: 'warn', unknown: 'err' } as const;

export function ScaleChip({ status }: { status: ScaleStatus | string }) {
  const { t } = useI18n();
  const tone = SCALE_TONE[status as ScaleStatus] ?? 'stale';
  const known = status in SCALE_TONE;
  return (
    <Chip tone={tone} icon={<Ruler aria-hidden />} title={known ? t(`dr.scale.${status as ScaleStatus}.hint`) : undefined}>
      {known ? t(`dr.scale.${status as ScaleStatus}`) : status}
    </Chip>
  );
}

export function SimulatedChip() {
  const { t } = useI18n();
  return (
    <Chip tone="stale" icon={<FlaskConical aria-hidden />} title={t('dr.simulated.hint')}>
      {t('dr.simulated')}
    </Chip>
  );
}

export function OriginTag({ origin, modified }: { origin: EntityOrigin | string; modified?: boolean }) {
  const { t } = useI18n();
  const key = (['source', 'ai', 'person', 'generated'] as const).includes(origin as EntityOrigin) ? origin : 'generated';
  return (
    <span className={`dr-origin dr-origin-${key}`}>
      {origin === 'ai' ? <Bot aria-hidden /> : origin === 'source' ? <ScanLine aria-hidden /> : null}
      {t(`dr.origin.${key as EntityOrigin}`)}
      {modified ? ` · ${t('dr.origin.modified')}` : ''}
    </span>
  );
}

/** Policy, check, verification and output chips of one version. */
export function VersionChips({ v, compact }: { v: VersionBrief; compact?: boolean }) {
  return (
    <span className="dr-chips">
      <PolicyChip policy={v.execution_policy} />
      <CheckStatusChip status={v.check_status} />
      {unverifiedBesideCheck(v.verification, v.check_status) && v.execution_policy === 'turbo' && <UnverifiedChip />}
      {!compact && <ScaleChip status={v.scale_status} />}
      {v.output_status !== 'succeeded' && <OutputChip status={v.output_status} dwg={v.dwg_status} />}
      {v.simulated && <SimulatedChip />}
    </span>
  );
}

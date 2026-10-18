// Generation reliability N (design §2.8): the shared state badge of a generated object, the Turbo boundary note of a
// delivery and the execution context line of a run / operation / Agent tool result. The vocabulary and tones are in
// status.ts; the check status keeps its own chip (CheckStatusChip) next to the state, never replaced by it.

import { Ban, Bot, CheckCircle2, CircleDashed, Eye, FileCheck2, FlaskConical, Gauge, Link2, ShieldAlert, ShieldCheck } from 'lucide-react';
import type { ReactNode } from 'react';
import { Chip } from '../../components/ui/Badges';
import { PolicyChip } from '../../components/engineering/EngineeringChips';
import type { MessageKey } from '../../i18n/en';
import { useI18n } from '../../i18n/I18nProvider';
import { asObjectState, budgetParts, checksLine, levelStateCounts, STATE_TONE, type ChecksReport, type ExecutionContext, type ObjectState } from './status';
import type { DeliveryLevels } from '../../api/orchestration';
import './orchestration.css';

const ICON: Record<ObjectState, ReactNode> = {
  verified: <CheckCircle2 aria-hidden />,
  generated: <FileCheck2 aria-hidden />,
  generated_ai: <Bot aria-hidden />,
  partial: <CircleDashed aria-hidden />,
  blocked: <Ban aria-hidden />,
  display_only: <Eye aria-hidden />,
  simulated: <FlaskConical aria-hidden />,
};

/** The object state as one chip with its explanation; nothing for a value the server did not send (callers keep their
 * previous marks for products made before the state existed). */
export function ObjectStateBadge({ state, className }: { state: unknown; className?: string }) {
  const { t } = useI18n();
  const st = asObjectState(state);
  if (!st) return null;
  return (
    <Chip tone={STATE_TONE[st]} icon={ICON[st]} title={t(`st.hint.${st}` as MessageKey)} className={className}>
      {t(`st.state.${st}` as MessageKey)}
    </Chip>
  );
}

/** A layered delivery's objects counted per state (header of the per-level view). */
export function LevelStateSummary({ levels }: { levels: Pick<DeliveryLevels, 'assembly' | 'subassembly' | 'part'> }) {
  const { t } = useI18n();
  const counts = levelStateCounts(levels);
  if (!counts.length) return null;
  return (
    <span className="st-levels" aria-label={t('st.levels.label')}>
      {counts.map(({ state, n }) => (
        <Chip key={state} tone={STATE_TONE[state]} icon={ICON[state]} title={t(`st.hint.${state}` as MessageKey)} className="sm">
          {t('st.count', { label: t(`st.state.${state}` as MessageKey), n })}
        </Chip>
      ))}
    </span>
  );
}

/** "Turbo disabled 16 checks · 0 actual calls" (or the standard run's count) from the delivery's check report. */
export function ClosedChecksNote({ checks }: { checks: ChecksReport | null | undefined }) {
  const { t } = useI18n();
  const line = checksLine(checks);
  if (!line) return null;
  const text =
    line.kind === 'turbo_invoked'
      ? t('st.checks.turboInvoked', { n: line.invokedClosed })
      : line.kind === 'turbo_clean'
        ? t('st.checks.turboClean', { closed: line.closed, n: line.invokedClosed })
        : t('st.checks.standard', { n: line.invoked });
  return (
    <span className={`st-checks small ${line.kind === 'turbo_invoked' ? 'err-text' : 'faint'}`} title={t('st.checks.hint')}>
      {line.kind === 'turbo_invoked' ? <ShieldAlert aria-hidden /> : <ShieldCheck aria-hidden />} {text}
      {line.incomplete ? ` · ${t('st.checks.incomplete')}` : ''}
    </span>
  );
}

/** Policy, where it came from, the input versions, the target and the budget, on one line. */
export function ExecutionContextLine({ ctx }: { ctx: ExecutionContext | null }) {
  const { t } = useI18n();
  if (!ctx) return null;
  const source = ctx.source
    ? ctx.source === 'inherited' && ctx.sourceLabel
      ? t('st.ctx.sourceOf', { source: t(`pol.source.short.${ctx.source}`), label: ctx.sourceLabel })
      : t('st.ctx.source', { source: t(`pol.source.short.${ctx.source}`) })
    : t('st.ctx.noSource');
  const b = budgetParts(ctx.budget);
  const budget = [b.calls !== null ? t('st.ctx.calls', { n: b.calls }) : null, b.minutes !== null ? t('st.ctx.minutes', { n: b.minutes }) : null,
    b.seconds !== null ? t('st.ctx.seconds', { n: b.seconds }) : null].filter(Boolean);
  return (
    <p className="st-ctx small" aria-label={t('st.ctx.policy')}>
      <PolicyChip policy={ctx.policy} />
      <span className="faint">
        <Link2 aria-hidden className="inline-ico" /> {source}
      </span>
      <span className="faint" title={ctx.inputs.join('\n') || undefined}>
        {ctx.inputs.length ? t('st.ctx.inputs', { n: ctx.inputs.length }) : t('st.ctx.noInputs')}
      </span>
      {ctx.target && <span className="faint ellip">{t('st.ctx.target', { label: ctx.target })}</span>}
      <span className="faint">
        <Gauge aria-hidden className="inline-ico" /> {budget.length ? `${t('st.ctx.budget')} ${budget.join(' · ')}` : t('st.ctx.noBudget')}
      </span>
    </p>
  );
}

import { ArrowDown, Box, CheckCircle2, ClipboardCheck, Hand, Loader2, OctagonX, TriangleAlert } from 'lucide-react';
import { Chip, type Tone } from '../../components/ui/Badges';
import { useI18n } from '../../i18n/I18nProvider';
import type { GenerationJourney, JourneyStatus } from './journey';
import { revealOperation } from './OperationCard';

const TONE: Record<JourneyStatus, Tone> = {
  waiting: 'warn', running: 'measure', plan_ready: 'warn', failed: 'err', partial: 'warn',
  cancelled: 'pending', generated: 'measure', complete: 'pending',
};

export function JourneyStatusChip({ status }: { status: JourneyStatus }) {
  const { t } = useI18n();
  const Icon = status === 'running' ? Loader2 : status === 'waiting' ? Hand : status === 'plan_ready' ? ClipboardCheck
    : status === 'generated' ? Box : status === 'complete' ? CheckCircle2 : status === 'cancelled' ? OctagonX : TriangleAlert;
  return <Chip tone={TONE[status]} icon={<Icon className={status === 'running' ? 'spin-icon' : undefined} aria-hidden />}>
    {t(`as.journey.status.${status}`)}
  </Chip>;
}

/** Jump within this conversation only. Explicit navigation reveals folded history before focusing it. */
export function focusJourney(root: ParentNode, journey: GenerationJourney): HTMLElement | null {
  const article = [...root.querySelectorAll<HTMLElement>('[data-journey-id]')].find((el) => el.dataset.journeyId === journey.id);
  if (!article) return null;
  let target: HTMLElement | null = null;
  if (journey.status === 'complete') target = article.querySelector<HTMLElement>('[data-journey-answer]');
  if (!target && journey.focusOperationId) {
    const operation = revealOperation(article, journey.focusOperationId);
    const selector = journey.status === 'waiting' ? '.as-design-choice, .as-confirm, .as-clarify, .callout.warn'
      : journey.status === 'plan_ready' ? '.as-wfe'
      : journey.status === 'generated' ? '.as-cad-result, .prog-results, .as-results'
      : journey.status === 'failed' || journey.status === 'partial' ? '.as-op-fail, .as-step.st-failed, .callout.err'
      : null;
    target = (selector && operation?.querySelector<HTMLElement>(selector)) || operation;
  }
  target ??= article;
  // The target may be an error in a technical disclosure inside a visible operation.
  for (let p = target.parentElement; p; p = p.parentElement) if (p.tagName === 'DETAILS') (p as HTMLDetailsElement).open = true;
  if (!target.hasAttribute('tabindex')) target.tabIndex = -1;
  target.focus({ preventScroll: true });
  target.scrollIntoView({ block: 'start', behavior: 'auto' });
  return target;
}

export function JourneyNavigator({ journey, onNavigate }: { journey: GenerationJourney; onNavigate: () => void }) {
  const { t } = useI18n();
  return <nav className="as-journey-nav" aria-label={t('as.journey.current')}>
    <div className="as-journey-nav-main">
      <span className="as-journey-eyebrow">{t('as.journey.current')}</span>
      <span className="as-journey-nav-title" title={journey.request.text}>{journey.request.text}</span>
    </div>
    <JourneyStatusChip status={journey.status} />
    <button type="button" className="btn sm as-journey-jump" onClick={onNavigate}>
      <ArrowDown aria-hidden /> {t(`as.journey.jump.${journey.status}`)}
    </button>
  </nav>;
}

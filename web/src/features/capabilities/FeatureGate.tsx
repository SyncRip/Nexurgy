import type { ReactNode } from 'react';
import { featureEnabled, useCapabilities } from '../../api/capabilities';
import type { FeatureName } from '../../api/types';
import { EmptyState, ErrorState, SkeletonRows } from '../../components/ui/States';
import { useI18n } from '../../i18n/I18nProvider';

/** Use for new/list surfaces only. Historical details and run completion keep their own server action checks. */
export function FeatureGate({ feature, children }: { feature: FeatureName; children: ReactNode }) {
  const state = useCapabilities();
  const { t } = useI18n();
  if (featureEnabled(feature, state)) return children;
  if (state.error) return <ErrorState error={state.error} onRetry={state.refresh} />;
  if (state.status !== 'ready') return <SkeletonRows />;
  return <EmptyState title={t('features.disabled', { feature: t(`features.${feature}`) })} body={t('features.disabledBody')} />;
}

export function FeatureNotice({ feature }: { feature: FeatureName }) {
  const state = useCapabilities();
  const { t } = useI18n();
  if (featureEnabled(feature, state)) return null;
  if (state.error) return <ErrorState error={state.error} onRetry={state.refresh} />;
  return <p className="callout" role="status">{state.status === 'ready' ? t('features.historyOnly') : t('status.checking')}</p>;
}

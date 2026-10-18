import { useI18n } from '../../i18n/I18nProvider';

/** The saved plan identity lets a person compare the displayed plan with a retry or conflict. */
export function PlanIdentity({ hash, head, version }: { hash?: string; head?: string; version?: number }) {
  const { t } = useI18n();
  if (!hash) return null;
  return <details className="as-plan-identity">
    <summary>{t('core.plan.identity')}</summary>
    <dl>
      {head && <div><dt>{t('core.plan.base')}</dt><dd><code>{head}</code></dd></div>}
      {version !== undefined && <div><dt>{t('core.plan.base')}</dt><dd>v{version}</dd></div>}
      <div><dt>{t('core.plan.hash')}</dt><dd><code>{hash}</code></dd></div>
    </dl>
  </details>;
}

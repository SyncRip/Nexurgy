// Project default for new work. Continuations and retries retain their frozen execution settings.

import './policy.css';
import { Zap } from 'lucide-react';
import { useI18n } from '../../i18n/I18nProvider';

export function DefaultTurboSwitch({ checked, onChange, disabled }: { checked: boolean; onChange: (on: boolean) => void; disabled?: boolean }) {
  const { t } = useI18n();
  return (
    <label className="pol-switch">
      <input type="checkbox" role="switch" checked={checked} aria-checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} />
      <span className="pol-switch-title">
        <Zap aria-hidden /> {t('pol.project.title')} · {checked ? t('pol.project.on') : t('pol.project.off')}
      </span>
      <span className="pol-switch-desc">{t('pol.project.hint')}</span>
    </label>
  );
}

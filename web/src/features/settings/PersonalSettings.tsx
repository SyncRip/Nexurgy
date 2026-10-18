import { useState } from 'react';
import { getPreferences,updatePreferences,type AccountPreferences } from '../../api/settings';
import { useUser } from '../../auth/AuthProvider';
import { LanguageToggle,ThemeSegmented } from '../../components/shell/Prefs';
import { ErrorNotice,SkeletonRows } from '../../components/ui/States';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';

export function PersonalSettings() {
  const { t } = useI18n();
  const user = useUser();
  const prefs = useResource((signal) => getPreferences({ signal }), [user.id]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const change = async (value: AccountPreferences['ai_autonomy']) => {
    if (!prefs.data) return;
    setBusy(true); setError(null);
    try { prefs.setData(await updatePreferences({ ai_autonomy: value, expected_version: prefs.data.version })); }
    catch (e) { setError(e); prefs.reload(); }
    finally { setBusy(false); }
  };
  return <section className="panel"><div className="panel-h"><h2>{t('core.settings.personal')}</h2></div><div className="panel-b stack">
    <div className="field"><span>{t('prefs.language')}</span><LanguageToggle /></div>
    <div className="field"><span>{t('prefs.theme')}</span><ThemeSegmented /></div>
    <p className="faint small">{t('prefs.storedLocally')}</p>
    {prefs.data ? <fieldset className="stack-sm"><legend>{t('core.autonomy.title')}</legend>{(['auto', 'step'] as const).map((value) => <label className="check" key={value}><input type="radio" name="account-autonomy" checked={prefs.data?.ai_autonomy === value} disabled={busy} onChange={() => void change(value)} />{t(`core.autonomy.${value}`)}</label>)}<p className="faint small">{t('core.autonomy.hint')}</p></fieldset> : !prefs.error && <SkeletonRows rows={2} />}
    <ErrorNotice error={error ?? prefs.error} onRetry={prefs.reload} />
  </div></section>;
}

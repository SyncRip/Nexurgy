// Language and theme controls (top bar, mobile drawer, landing page, login).

import { Check, Monitor, Moon, Sun } from 'lucide-react';
import { LANGS } from '../../i18n/core';
import { useI18n } from '../../i18n/I18nProvider';
import { THEME_PREFS, useTheme, type ThemePref } from '../../prefs/ThemeProvider';
import { Menu } from '../ui/Menu';

const LANG_SHORT = { en: 'EN', zh: '中' } as const;

export function LanguageToggle() {
  const { lang, setLang, t } = useI18n();
  return (
    <div className="seg" role="group" aria-label={t('prefs.language')}>
      {LANGS.map((l) => (
        <button key={l} type="button" className="seg-btn" aria-pressed={lang === l} onClick={() => setLang(l)} title={t(`lang.${l}`)} lang={l === 'zh' ? 'zh-CN' : 'en'}>
          {LANG_SHORT[l]}
        </button>
      ))}
    </div>
  );
}

function ThemeIcon({ pref }: { pref: ThemePref }) {
  return pref === 'dark' ? <Moon /> : pref === 'light' ? <Sun /> : <Monitor />;
}

export function ThemeToggle() {
  const { t } = useI18n();
  const { pref, setPref } = useTheme();
  return (
    <Menu
      label={t('prefs.themeCurrent', { theme: t(`theme.${pref}`) })}
      trigger={<ThemeIcon pref={pref} />}
      items={THEME_PREFS.map((p) => ({
        id: p,
        label: t(`theme.${p}`),
        icon: pref === p ? <Check /> : <ThemeIcon pref={p} />,
        onSelect: () => setPref(p),
      }))}
    />
  );
}

/** Inline radio-style theme picker (drawer, settings). */
export function ThemeSegmented() {
  const { t } = useI18n();
  const { pref, setPref } = useTheme();
  return (
    <div className="seg" role="group" aria-label={t('prefs.theme')}>
      {THEME_PREFS.map((p) => (
        <button key={p} type="button" className="seg-btn" aria-pressed={pref === p} onClick={() => setPref(p)}>
          <ThemeIcon pref={p} /> {t(`theme.${p}`)}
        </button>
      ))}
    </div>
  );
}

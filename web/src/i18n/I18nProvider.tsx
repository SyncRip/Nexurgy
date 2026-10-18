// In-house i18n: a context with the current language, typed t() / tn(), and
// Intl formatters. Switching language only re-renders consumers; the router
// tree is never keyed by language, so page state and drafts survive a switch.

import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import type { MessageKey } from './en';
import { DEFAULT_LANG, DICTS, LANG_STORAGE_KEY, LOCALE, isLang, translate, translatePlural, type Lang, type PluralBase, type Vars } from './core';
import { makeFormatters, type Formatters } from './format';

export type TFunction = (key: MessageKey, vars?: Vars) => string;
export type TnFunction = (base: PluralBase, count: number, vars?: Vars) => string;

interface I18nValue {
  lang: Lang;
  locale: string;
  setLang: (l: Lang) => void;
  t: TFunction;
  tn: TnFunction;
  fmt: Formatters;
}

const I18nContext = createContext<I18nValue | null>(null);

function readStoredLang(): Lang {
  try {
    const v = localStorage.getItem(LANG_STORAGE_KEY);
    return isLang(v) ? v : DEFAULT_LANG;
  } catch {
    return DEFAULT_LANG;
  }
}

export function I18nProvider({ children }: { children: ReactNode }) {
  const [lang, setLangState] = useState<Lang>(readStoredLang);

  const setLang = useCallback((l: Lang) => {
    setLangState(l);
    try {
      localStorage.setItem(LANG_STORAGE_KEY, l);
    } catch {
      /* private mode: the choice lasts for this session only */
    }
  }, []);

  useEffect(() => {
    document.documentElement.lang = LOCALE[lang];
  }, [lang]);

  // Keep tabs in sync when the language changes in another tab.
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === LANG_STORAGE_KEY && isLang(e.newValue)) setLangState(e.newValue);
    };
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, []);

  const value = useMemo<I18nValue>(() => {
    const dict = DICTS[lang];
    return {
      lang,
      locale: LOCALE[lang],
      setLang,
      t: (key, vars) => translate(dict, key, vars),
      tn: (base, count, vars) => translatePlural(dict, lang, base, count, vars),
      fmt: makeFormatters(LOCALE[lang]),
    };
  }, [lang, setLang]);

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nValue {
  const v = useContext(I18nContext);
  if (!v) throw new Error('useI18n must be used inside <I18nProvider>');
  return v;
}

/** Shorthand for the translate function. */
export function useT(): TFunction {
  return useI18n().t;
}

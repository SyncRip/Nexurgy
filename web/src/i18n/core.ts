// Pure i18n helpers (no React), unit-tested in i18n.test.ts.

import { en, type MessageKey } from './en';
import { zh } from './zh';

export type Lang = 'en' | 'zh';
export const LANGS: readonly Lang[] = ['en', 'zh'];
export const DEFAULT_LANG: Lang = 'en';
export const LANG_STORAGE_KEY = 'partroute.lang';

export type Dict = Record<MessageKey, string>;
export const DICTS: Record<Lang, Dict> = { en, zh };

/** BCP 47 tag used for <html lang> and Intl. */
export const LOCALE: Record<Lang, string> = { en: 'en', zh: 'zh-CN' };

export type Vars = Record<string, string | number>;

/** Replace {name} placeholders. Unknown placeholders are left as-is so a missing var is visible. */
export function interpolate(template: string, vars?: Vars): string {
  if (!vars) return template;
  return template.replace(/\{(\w+)\}/g, (m, k: string) => (Object.prototype.hasOwnProperty.call(vars, k) ? String(vars[k]) : m));
}

/** Keys that have plural variants, written as `<base>_one` / `<base>_other`. */
export type PluralBase = MessageKey extends infer K ? (K extends `${infer B}_other` ? B : never) : never;

export function translate(dict: Dict, key: MessageKey, vars?: Vars): string {
  const s = dict[key] ?? en[key] ?? key;
  return interpolate(s, vars);
}

export function translatePlural(dict: Dict, lang: Lang, base: PluralBase, count: number, vars?: Vars): string {
  const cat = new Intl.PluralRules(LOCALE[lang]).select(count);
  const key = (`${base}_${cat}` in dict ? `${base}_${cat}` : `${base}_other`) as MessageKey;
  return translate(dict, key, { count, ...vars });
}

export function isLang(v: unknown): v is Lang {
  return v === 'en' || v === 'zh';
}

/** Has the dictionary a message for this (dynamic) key? Used for API error codes. */
export function hasKey(key: string): key is MessageKey {
  return Object.prototype.hasOwnProperty.call(en, key);
}

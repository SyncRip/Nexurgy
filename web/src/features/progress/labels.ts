import { hasKey } from '../../i18n/core';
import type { TFunction } from '../../i18n/I18nProvider';
import { unitIndex } from './model';
import { nodeErrorText } from '../workflow/labels';

export const translated = (t: TFunction, key: string, fallback: string, vars?: Record<string, string | number>) => hasKey(key) ? t(key, vars) : fallback;

export function purposeLabel(t: TFunction, purpose: string | null): string {
  if (!purpose) return t('cad.run.unknown');
  const key = `prog.purpose.${purpose}`;
  if (hasKey(key)) return t(key);
  if (purpose.endsWith('_repair')) return t('prog.purpose.repair', { purpose: purposeLabel(t, purpose.slice(0, -7)) });
  return translated(t, `core.purpose.${purpose}`, purpose);
}

export function unitLabel(t: TFunction, key: string, kind = key.split(':')[0]): string {
  if (kind === 'definition') return unitIndex(key);
  return translated(t, `prog.kind.${kind}`, key, { n: unitIndex(key) });
}

export function failureLabel(t: TFunction, error: { diagnostic_code?: string | null; code?: string | null; failure_class?: string | null; message?: string | null }): string {
  const diagnostic = `diag.code.${error.diagnostic_code ?? ''}`;
  if (hasKey(diagnostic)) return t(diagnostic);
  return translated(t, `cad.fclass.${error.failure_class ?? ''}`, nodeErrorText(t, error.code ?? undefined, error.message ?? undefined));
}
